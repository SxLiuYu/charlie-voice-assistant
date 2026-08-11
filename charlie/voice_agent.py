"""
Charlie - 语音Agent核心
语音闭环: ASR(百度) → 大脑(ARK+Qwen-Agent+MCP) → TTS(百度)
连接韧性: Session复用 + 自动重试 + 异常降级
对话记忆: 跨请求保留历史上下文，支持多轮连续对话，持久化到磁盘
"""
import os, sys, json, copy, base64, requests, datetime, time, logging, asyncio, re, random, tempfile, threading, fcntl
from typing import Optional, Generator, Tuple, List, Dict, Any, Callable
from contextlib import contextmanager
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

log = logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", PROJECT_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

FINNA = os.getenv("FINNA_BASE", "https://www.finna.com.cn/v1")
ARK_BASE = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
ARK_KEY = os.getenv("ARK_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "ark-code-latest")

EMPTY_ASR_TEXT = "(未识别到语音)"
EMPTY_ASR_REPLY = "抱歉，我没听清，请再说一遍。"
INTENT_FAILURE_THRESHOLD = int(os.getenv("ASSISTANT_KID_INTENT_FAILURE_THRESHOLD", "2"))
INTENT_FAILURE_COOLDOWN = float(os.getenv("ASSISTANT_KID_INTENT_FAILURE_COOLDOWN", "30"))

# 从 agent/ 子模块导入
from agent.intent import LOW_INTENT_ASR_REPLY, is_low_intent_asr, is_garbled_asr
from agent.cache import _cache_get, _cache_set, _cache_get_interrupted, _cache_lock, _cache, _CACHE_TTL, _CACHE_MAX
from agent.history import (
    _history, _sessions, MAX_HISTORY, MAX_SESSIONS, _history_lock, HISTORY_FILE, HISTORY_LOCK_FILE,
    _get_history_file, _history_file_sig, _locked_history_file, _read_history_file_locked, _read_history_file,
    _get_history, _history_snapshot, _searchable_history, _session_summaries,
    _save_history, _append_history, _load_history,
    _estimate_tokens, _estimate_msg_tokens, _trim_history_tokens,
    reset_history, _context_summaries, REMINDERS_FILE, _history_save_seq,
)
from agent.preferences import (
    PREFS_FILE, PREFS_LOCK_FILE, _preferences, _prefs_lock, _preferences_revision, _preferences_file_lock, _preferences_save_seq, _preferences_file_signature,
    _locked_preferences, _preferences_file_signature_now, _read_locked_preferences, _write_preferences_temp_locked, _write_locked_preferences,
    _bump_preferences_revision, _load_preferences, _refresh_preferences_if_changed, _save_preferences, _commit_preferences,
    set_preference, get_preference, preference_count, list_preferences,
    preferences_etag_token, preferences_snapshot, preferences_conditional, del_preference,
)
from agent.retry import MAX_RETRIES, RETRY_BACKOFF, RETRY_AFTER_CAP, _retry, _http_error_message, _exception_message, _is_retryable_http_status, _retry_after_delay
from agent.asr_tts import (
    TTS_CACHE_MAX_CHARS, TTS_CACHE_TTL, TTS_CACHE_MAX, TTS_VOICE, TTS_MODEL,
    TTS_FAILURE_COOLDOWN, TTS_FAILURE_THRESHOLD, _LOCAL_TTS_ENABLED,
    _tts_cache, _tts_unavailable_until, _tts_failures, _tts_lock, _tts_speed,
    BAIDU_APP_ID, BAIDU_API_KEY, BAIDU_SECRET_KEY, _baidu_token, _baidu_token_lock, BAIDU_TOKEN_FILE,
    _asr_fallback_times, _asr_lock,
    TTSUnavailableError,
    _clean_for_tts, _tts_cache_get, _tts_cache_put,
    tts, tts_status, _tts_cleaned_to_mp3, tts_to_mp3,
    asr, _vosk_asr, _baidu_get_token, _asr_baidu, _tts_baidu,
    runtime_audio_path, write_audio_file, runtime_temp_audio_path,
    _TTS_BOLD_RE, _TTS_HEADER_RE, _TTS_BLOCKQUOTE_RE, _TTS_TABLE_PIPE_RE,
    _TTS_CODE_BLOCK_RE, _TTS_INLINE_CODE_RE, _TTS_LIST_ITEM_RE, _TTS_MARKDOWN_LINK_RE, _TTS_WHITESPACE_RE,
)
from agent.state import update_user_state, get_user_state, _infer_user_state, _user_state, _user_state_lock
from agent.system_msg import _build_system_msg, _build_user_profile, invalidate_system_msg_cache, _MCP_SYSTEM_PROMPTS, _current_user_input, _current_user_id


# ===== 连接池复用(调优: max_connections=10, keep_alive=30s) =====
import requests.adapters
_session = requests.Session()
_session.headers.update({"Connection": "keep-alive"})
# 连接池调优: 每个主机最多10个连接, 超时30秒
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,    # 连接池大小
    pool_maxsize=10,        # 最大连接数
    max_retries=0,          # 重试由_retry()处理
    pool_block=False,       # 不阻塞, 满了直接新建
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
# ===== 大脑: deepseek-v4-flash + Qwen-Agent + MCP =====
_UNKNOWN_KWARG_RE = re.compile(r"got an unexpected keyword argument '([^']+)'")


def _wrap_openai_create_unknown_kwargs(create_fn):
    """把 OpenAI SDK 不认识的上游私有参数移入 extra_body 后重试一次。"""
    if getattr(create_fn, "_charlie_compat_wrapped", False):
        return create_fn

    def wrapped(*args, **kwargs):
        try:
            return create_fn(*args, **kwargs)
        except TypeError as exc:
            match = _UNKNOWN_KWARG_RE.search(str(exc))
            if not match:
                raise
            unknown_key = match.group(1)
            if unknown_key not in kwargs:
                raise
            retry_kwargs = copy.deepcopy(kwargs)
            extra_body = retry_kwargs.pop("extra_body", None)
            if not isinstance(extra_body, dict):
                extra_body = {}
            extra_body[unknown_key] = retry_kwargs.pop(unknown_key)
            retry_kwargs["extra_body"] = extra_body
            log.info(f"[brain] SDK不支持参数 {unknown_key}，改用 extra_body 重试")
            return create_fn(*args, **retry_kwargs)

    wrapped._charlie_compat_wrapped = True
    return wrapped


def _install_openai_compat(brain) -> None:
    """为 Qwen-Agent 主大脑和记忆大脑安装 OpenAI 参数兼容层。
    同时修补 ARK API 的 tool_call_id 兼容性问题。"""
    targets = [getattr(brain, "llm", None)]
    mem = getattr(brain, "mem", None)
    if mem is not None:
        targets.append(getattr(mem, "llm", None))
    for llm in targets:
        if llm is None:
            continue
        for attr in ("_chat_complete_create", "_complete_create"):
            original = getattr(llm, attr, None)
            if callable(original):
                setattr(llm, attr, _wrap_openai_create_unknown_kwargs(original))
        # 修补 ARK API tool_call_id 兼容性:
        # Qwen-Agent 把 function_id 放在 'id' 字段, 但 ARK 需要 'tool_call_id'
        # 注意: 不能用 @staticmethod — 在函数内部定义时 staticmethod 描述符
        # 赋值给实例属性后调用会失败(TypeError: descriptor), 必须用普通函数
        if not getattr(llm, '_ark_tool_call_id_patched', False):
            original_convert = llm._conv_qwen_agent_messages_to_oai
            def _patched_convert(messages):
                new_messages = original_convert(messages)
                for msg in new_messages:
                    if msg.get('role') == 'tool' and 'id' in msg and 'tool_call_id' not in msg:
                        msg['tool_call_id'] = msg.pop('id')
                return new_messages
            llm._conv_qwen_agent_messages_to_oai = _patched_convert
            llm._ark_tool_call_id_patched = True


def _build_brain(mcp_set="all"):
    """构建大脑, mcp_set控制加载哪些MCP: none/all/单个MCP名"""
    # 内存检查: 防止OOM崩溃
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.percent > 90:
            raise RuntimeError(f"内存不足({mem.percent}%), 拒绝构建大脑防OOM")
        log.info(f"[brain] 内存检查通过: {mem.percent}% ({(mem.total-mem.available)//1073741824:.1f}GB可用)")
    except ImportError:
        pass
    from qwen_agent.agents import Assistant
    llm_cfg = {
        'model': ARK_MODEL, 'model_type': 'oai',  # 火山引擎 ARK deepseek-v4, prefix-cache更优
        'api_base': ARK_BASE, 'api_key': ARK_KEY,
        'generate_cfg': {'use_raw_api': True, 'extra_body': {'extra_body': {'enable_thinking': False}}, 'max_tokens': 512},
    }
    # MCP服务器配置(可通过MCP_SERVERS环境变量控制启用哪些, 逗号分隔)
    # PyInstaller打包后: sys.executable 指向 frozen exe, 用 --mcp <name> 自启动子进程
    _is_frozen = getattr(sys, 'frozen', False)
    _py = sys.executable
    _mcp_args = lambda name: [name] if _is_frozen else [name + ".py"]
    _mcp_cwd = os.path.dirname(_py) if _is_frozen else os.getcwd()
    all_mcp = {
        # amap-maps 和 magic-info 都指向同一套工具 (去掉 npx 依赖)
        "amap-maps": {"command": _py,
            "args": ["--mcp", "magic-info"] if _is_frozen else ["magic-info.py"], "cwd": _mcp_cwd},
        "magic-info": {"command": _py,
            "args": ["--mcp", "magic-info"] if _is_frozen else ["magic-info.py"], "cwd": _mcp_cwd},
        "magic-music": {"command": _py,
            "args": ["--mcp", "magic-music"] if _is_frozen else ["magic-music.py"], "cwd": _mcp_cwd},
        "magic-reminder": {"command": _py,
            "args": ["--mcp", "magic-reminder"] if _is_frozen else ["magic-reminder.py"], "cwd": _mcp_cwd},
        "magic-notes": {"command": _py,
            "args": ["--mcp", "magic-notes"] if _is_frozen else ["magic-notes.py"], "cwd": _mcp_cwd},
        "magic-system": {"command": _py,
            "args": ["--mcp", "magic-system"] if _is_frozen else ["magic-system.py"], "cwd": _mcp_cwd},
        "magic-life": {"command": _py,
            "args": ["--mcp", "magic-life"] if _is_frozen else ["magic-life.py"], "cwd": _mcp_cwd},
        "magic-scenes": {"command": _py,
            "args": ["--mcp", "magic-scenes"] if _is_frozen else ["magic-scenes.py"], "cwd": _mcp_cwd},
        "magic-apps": {"command": _py,
            "args": ["--mcp", "magic-apps"] if _is_frozen else ["magic-apps.py"], "cwd": _mcp_cwd},
        "magic-feishu": {"command": _py,
            "args": ["--mcp", "magic-feishu"] if _is_frozen else ["magic-feishu.py"], "cwd": _mcp_cwd},
        "magic-douyin": {"command": _py,
            "args": ["--mcp", "magic-douyin"] if _is_frozen else ["magic-douyin.py"], "cwd": _mcp_cwd},
        "magic-taobao": {"command": _py,
            "args": ["--mcp", "magic-taobao"] if _is_frozen else ["magic-taobao.py"], "cwd": _mcp_cwd},
        "magic-evolution": {"command": _py,
            "args": ["--mcp", "magic-evolution"] if _is_frozen else ["magic-evolution.py"], "cwd": _mcp_cwd},
        "magic-summary": {"command": _py,
            "args": ["--mcp", "magic-summary"] if _is_frozen else ["magic-summary.py"], "cwd": _mcp_cwd},
        "magic-wardrobe": {"command": _py,
            "args": ["--mcp", "magic-wardrobe"] if _is_frozen else ["magic-wardrobe.py"], "cwd": _mcp_cwd},
        "magic-browser": {"command": _py,
            "args": ["--mcp", "magic-browser"] if _is_frozen else ["magic-browser.py"], "cwd": _mcp_cwd},
        "baize-skills": {"command": _py,
            "args": ["--mcp", "baize-skills"] if _is_frozen else ["baize_skills_mcp.py"], "cwd": _mcp_cwd,
            "env": {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY", ""),
                    "ALIYUN_API_KEY": os.getenv("ALIYUN_API_KEY", "")}},
        "filesystem": {"command": _py,
            "args": ["--mcp", "filesystem"] if _is_frozen else ["magic-notes.py"], "cwd": _mcp_cwd},
        "ac-control": {"command": _py,
            "args": ["--mcp", "ac-control"] if _is_frozen else ["mcp_ir_control.py"], "cwd": _mcp_cwd},
    }
    # 按意图路由选择MCP
    enabled_env = os.getenv("MCP_SERVERS", "amap-maps,baize-skills,filesystem,magic-music,magic-reminder,magic-notes,magic-system,magic-info,magic-life,magic-scenes,magic-evolution,magic-summary,magic-wardrobe,magic-browser,magic-apps,magic-feishu,magic-douyin,magic-taobao").split(",")
    enabled_env = [s.strip() for s in enabled_env if s.strip()]
    if mcp_set == "none":
        mcp_servers = {}
    elif mcp_set == "all":
        mcp_servers = {k: v for k, v in all_mcp.items() if k in enabled_env}
    else:
        mcp_servers = {mcp_set: all_mcp[mcp_set]} if mcp_set in all_mcp else {}
    log.info(f"[brain] 构建大脑 mcp_set={mcp_set}, 启用{len(mcp_servers)}个MCP: {list(mcp_servers.keys())}")
    tools = [{"mcpServers": mcp_servers}] if mcp_servers else []
    brain = Assistant(llm=llm_cfg, name='Charlie',
        system_message=_build_system_msg(mcp_set),
        function_list=tools)
    _install_openai_compat(brain)
    return brain

# ===== 意图路由: 用LLM快速判断需要哪些MCP =====
_brains = {}              # key=mcp_set, value=Assistant实例
_brain_build_time = 0     # 首次构建时间
# 叙事性记忆: 当前用户输入(brain()调用时刷新, _build_system_msg读取)
_current_user_input = ""
_current_user_id = "default"  # 多用户支持: 当前活跃用户ID
_brain_failures = 0       # 连续失败计数
_brain_total_failures = 0  # 累计失败总数（不重置）
_brain_last_failure = 0   # 上次失败时间戳
_brain_last_success = 0   # 上次成功时间戳
_brain_lock = threading.Lock()  # 大脑状态锁(熔断/缓存/失败计数)
_MAX_BRAIN_FAILURES = 5   # 连续失败5次后自动重建大脑(429限流单独阈值10)
_intent_failures = 0
_intent_disabled_until = 0.0
_intent_cache_lock = threading.Lock()  # 意图分类缓存锁(防止多线程并发)
from collections import OrderedDict
_intent_cache: OrderedDict = OrderedDict()  # LRU + TTL 意图缓存
_INTENT_CACHE_MAX = 100
_INTENT_CACHE_TTL = 3600  # 1小时

def _intent_cache_set(text: str, intent: str):
    """写入意图缓存, 自动 LRU 淘汰"""
    _intent_cache[text] = (intent, time.time())
    _intent_cache.move_to_end(text)
    if len(_intent_cache) > _INTENT_CACHE_MAX:
        _intent_cache.popitem(last=False)  # 淘汰最旧

def intent_classifier_status() -> Dict[str, Any]:
    """只读本地意图分类熔断状态。"""
    now = time.time()
    with _intent_cache_lock:
        remaining = max(0.0, _intent_disabled_until - now)
        consecutive_failures = _intent_failures
    return {
        "circuit_open": remaining > 0,
        "remaining_seconds": round(remaining, 1),
        "consecutive_failures": consecutive_failures,
        "failure_threshold": INTENT_FAILURE_THRESHOLD,
        "cooldown_seconds": INTENT_FAILURE_COOLDOWN,
    }

def _classify_intent(text: str) -> str:
    """意图分类: 关键词预判 → ARK API (prefix-cache命中后0.3s), 返回MCP组合名"""
    global _intent_failures, _intent_disabled_until
    # 缓存: 相同文本不重复分类 (LRU + TTL, 不与对话缓存冲突)
    now = time.time()
    with _intent_cache_lock:
        cached = _intent_cache.get(text)
        if cached and now - cached[1] < _INTENT_CACHE_TTL:
            _intent_cache.move_to_end(text)  # LRU: 命中后移到尾部
            cached_intent = cached[0]
            log.info(f"[intent] 缓存命中: {text[:30]} → {cached_intent}")
            return cached_intent
        if now < _intent_disabled_until:
            log.info(f"[intent] 分类冷却中，{_intent_disabled_until - now:.0f}秒内默认none")
            return "none"
    # 快速关键词预判: 如果文本明确包含领域关键词, 直接返回, 跳过LLM调用
    _KEYWORD_MAP = [
        ({"天气", "气温", "下雨", "温度", "几度", "穿什么", "今天天气", "明天天气", "今天冷", "今天热"}, "amap-maps"),
        ({"地图", "导航", "附近", "我在哪", "路线", "怎么走", "到哪"}, "amap-maps"),
        ({"搜一下", "查一下", "查查", "谷歌", "购物", "买东西"}, "baize-skills"),
        ({"提醒", "定时", "闹钟", "备忘", "日程", "待办", "记一下", "提醒我"}, "magic-reminder"),
        ({"笔记", "备忘录", "记下来", "记一下"}, "magic-notes"),
        ({"音量", "说慢", "说快", "语速", "大声", "小声"}, "magic-system"),
        ({"状态", "运行", "负载", "设备"}, "magic-system"),
        ({"新闻", "头条", "热点"}, "magic-info"),
        ({"时间", "几点", "日期", "星期"}, "magic-info"),
        ({"翻译", "翻成", "英语说", "怎么说"}, "magic-info"),
        ({"计算", "算一下", "换算", "等于多少", "等于几", "加", "减", "乘", "除"}, "magic-info"),
        ({"放歌", "放一首", "放个", "播放", "听歌", "放周杰伦", "放毛不易", "音乐", "歌单", "停止播放", "每日推荐", "随机", "来一首", "播一首", "点一首", "放首", "放点", "整首", "整点", "循环", "单曲", "来首", "点歌", "唱首歌", "放音乐"}, "magic-music"),
        ({"空调", "电视", "制冷", "制热", "风扇", "开灯", "关灯", "关闭空调", "关闭电视"}, "ac-control"),
        ({"文件", "读文件", "写文件", "笔记"}, "filesystem"),
        ({"外卖", "点餐", "吃饭", "购物", "商品", "查一下", "充电桩", "特斯拉", "出门"}, "magic-life"),
        ({"学习", "进化", "自进化", "优化", "自我优化", "自学习", "学习进度", "进化状态"}, "magic-evolution"),
        ({"淘宝", "京东", "比价", "商品", "价格对比", "买东西", "购物", "买"}, "magic-taobao"),
        ({"浏览器", "打开网页", "打开网站", "访问", "浏览", "爬取", "截图", "页面", "网页", "百度"}, "magic-browser"),
        ({"微信", "支付宝", "今日头条", "美团", "拼多多", "大众点评", "猫眼", "大麦", "咸鱼", "外卖", "酒店", "机票", "火车票", "高铁", "电影票", "餐厅", "日料", "火锅", "美食", "门票", "演出", "演唱会"}, "magic-apps"),
        ({"飞书", "飞书文档", "飞书消息", "飞书日历", "日历"}, "magic-feishu"),
        ({"抖音", "douyin", "抖音搜索", "抖音视频", "抖音热搜", "热门视频", "热搜"}, "magic-douyin"),
        ({"晚安", "睡觉", "好梦", "休息吧", "睡吧", "睡", "goodnight"}, "magic-scenes"),
        ({"早上好", "早安", "起床", "good morning", "上午好"}, "magic-scenes"),
        ({"电影", "看电影", "视频", "追剧", "观影", "movie"}, "magic-scenes"),
        ({"看看屏幕", "屏幕上有什么", "截图分析", "帮我看看屏幕", "截屏", "识别图片", "图上有什么", "看看这张图", "看看这张", "看看这个图", "屏幕上显示什么", "屏幕上有啥"}, "mimo-vision"),
    ]
    # 闲聊短句预判: ≤6字 + 不含任何领域关键词 → 直接none, 跳过LLM (最佳实践: Suki/Gladia分层路由)
    _ALL_DOMAIN_KEYWORDS = set()
    for kw_set, _ in _KEYWORD_MAP:
        _ALL_DOMAIN_KEYWORDS |= kw_set
    if len(text) <= 6 and not any(kw in text for kw in _ALL_DOMAIN_KEYWORDS):
        log.info(f"[intent] 闲聊短句跳过LLM: '{text}' → none")
        _intent_cache_set(text, "none")
        return "none"
    for keywords, mcp_name in _KEYWORD_MAP:
        if any(kw in text for kw in keywords):
            log.info(f"[intent] 关键词命中: '{text[:30]}' → {mcp_name}")
            _intent_cache_set(text, mcp_name)
            return mcp_name
    prompt = (
        "任务: 判断用户输入需要哪个工具, 只回一个词\n"
        "选项: none | amap-maps | baize-skills | filesystem | magic-music | magic-reminder | magic-notes | magic-system | magic-info | magic-life | ac-control\n"
        "规则:\n"
        "- none = 闲聊/问候/常识问答(你好/谢谢/讲个笑话)\n"
        "- amap-maps = 天气/地图/导航/我在哪/附近/路线/温度/几度\n"
        "- baize-skills = 搜索互联网/查资料/购物/买东西\n"
        "- magic-music = 播放/歌单/音乐/随机播放\n"
        "- magic-reminder = 提醒/日程/定时器/日历\n"
        "- magic-notes = 备忘录/笔记\n"
        "- magic-system = 音量/语速/设备状态\n"
        "- magic-info = 时间/新闻/翻译/计算\n"
        "- magic-life = 外卖/充电桩/特斯拉/出门\n"
        "- magic-scenes = 晚安/早安/电影/出门场景\n"
        "- magic-evolution = 学习/优化/自进化\n"
        "- magic-browser = 浏览器/打开网页/搜索/截图/百度\n"
        "- magic-apps = 微信/支付宝/抖音/淘宝/京东/美团/拼多多/飞书等App\n"
        "- magic-feishu = 飞书文档/消息/日历\n"
        "- magic-douyin = 抖音搜索/视频/热搜\n"
        "- magic-taobao = 淘宝京东搜索/比价\n"
        "- ac-control = 空调/电视/制冷/制热/风扇/灯光\n"
        "- filesystem = 文件/读写文件/笔记\n"
        "- mimo-vision = 截图/看看屏幕/识别图片/图中有什么/屏幕上有什么\n"
        "示例:\n"
        "  你好→none\n"
        "  今天天气怎么样→amap-maps\n"
        "  北京天气→amap-maps\n"
        "  搜一下薛之谦→baize-skills\n"
        "  帮我点杯咖啡→baize-skills\n"
        "  设提醒→magic-reminder\n"
        "  放歌→magic-music\n"
        "  晚安→magic-scenes\n"
        "  早上好→magic-scenes\n"
        "  看电影→magic-scenes\n"
        "  自进化→magic-evolution\n"
        "  打开百度/搜索百度→magic-browser\n"
        "  学习我→magic-evolution\n"
        "  打开空调→ac-control\n"
        "  音量调大→magic-system\n"
        "  现在几点→magic-info\n"
        f"用户输入: {text[:100]}\n"
        "工具: →")
    try:
        r = _session.post(f"{ARK_BASE}/chat/completions",
            json={"model": ARK_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False,
                  "max_tokens": 10, "temperature": 0,
                  "extra_body": {"enable_thinking": False}},
            headers={"Authorization": f"Bearer {ARK_KEY}"},
            timeout=(3, 10))
        raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip().lower()
        # 后处理: 模型可能回 "magic"/"baize" 等截断词, 统一映射
        if "amap" in raw or "map" in raw: mcp = "amap-maps"
        elif "baize" in raw or "search" in raw: mcp = "baize-skills"
        elif "music" in raw: mcp = "magic-music"
        elif "remind" in raw: mcp = "magic-reminder"
        elif "note" in raw: mcp = "magic-notes"
        elif "system" in raw: mcp = "magic-system"
        elif "info" in raw: mcp = "magic-info"
        elif "life" in raw: mcp = "magic-life"
        elif "scenes" in raw or "scene" in raw: mcp = "magic-scenes"
        elif "evolution" in raw or "learn" in raw: mcp = "magic-evolution"
        elif "browser" in raw: mcp = "magic-browser"
        elif "apps" in raw: mcp = "magic-apps"
        elif "feishu" in raw: mcp = "magic-feishu"
        elif "douyin" in raw: mcp = "magic-douyin"
        elif "taobao" in raw: mcp = "magic-taobao"
        elif "magic" in raw: mcp = "magic-music"
        elif "ac" in raw or "air" in raw or "control" in raw: mcp = "ac-control"
        elif "file" in raw or "fs" in raw: mcp = "filesystem"
        elif "vision" in raw or "mimo" in raw or "screen" in raw or "截图" in raw: mcp = "mimo-vision"
        else: mcp = "none"
        with _intent_cache_lock:
            _intent_failures = 0
            _intent_disabled_until = 0.0
        _intent_cache_set(text, mcp)
        log.info(f"[intent] '{text[:30]}' → {mcp} ({raw[:15]})")
        return mcp
    except Exception as e:
        with _intent_cache_lock:
            _intent_failures += 1
            if _intent_failures >= INTENT_FAILURE_THRESHOLD:
                _intent_disabled_until = now + INTENT_FAILURE_COOLDOWN
                log.warning(f"[intent] 连续失败{_intent_failures}次，暂停本地分类{INTENT_FAILURE_COOLDOWN:g}秒")
        log.warning(f"[intent] 本地分类失败,默认none: text='{text[:20]}' err={e}")
        # 尝试Ollama降级(本地模型, 无429风险)
        try:
            r = _session.post("http://localhost:11434/api/chat",
                json={"model": "qwen3.5:2b",
                      "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "think": False,
                      "options": {"num_predict": 10, "temperature": 0}},
                timeout=(3, 15))
            raw = r.json().get("message", {}).get("content", "").strip().lower()
            if "amap" in raw or "map" in raw: mcp = "amap-maps"
            elif "baize" in raw or "search" in raw: mcp = "baize-skills"
            elif "scenes" in raw or "scene" in raw: mcp = "magic-scenes"
            elif "evolution" in raw or "learn" in raw: mcp = "magic-evolution"
            elif "browser" in raw: mcp = "magic-browser"
            elif "apps" in raw: mcp = "magic-apps"
            elif "feishu" in raw: mcp = "magic-feishu"
            elif "douyin" in raw: mcp = "magic-douyin"
            elif "taobao" in raw: mcp = "magic-taobao"
            elif "magic" in raw or "remind" in raw: mcp = "magic-music"
            elif "ac" in raw or "air" in raw or "control" in raw: mcp = "ac-control"
            elif "file" in raw or "fs" in raw: mcp = "filesystem"
            elif "vision" in raw or "mimo" in raw or "screen" in raw or "截图" in raw: mcp = "mimo-vision"
            else: mcp = "none"
            with _intent_cache_lock:
                _intent_failures = 0
                _intent_disabled_until = 0.0
            _cache_set("__intent__:" + text, mcp)
            log.info(f"[intent] '{text[:30]}' → {mcp} ({raw[:15]}) [重试成功]")
            return mcp
        except Exception as e2:
            with _intent_cache_lock:
                _intent_failures += 1
                if _intent_failures >= INTENT_FAILURE_THRESHOLD:
                    _intent_disabled_until = now + INTENT_FAILURE_COOLDOWN
                    log.warning(f"[intent] 连续失败{_intent_failures}次，暂停本地分类{INTENT_FAILURE_COOLDOWN:g}秒")
            log.warning(f"[intent] 本地分类重试仍失败,默认none: text='{text[:20]}' err={e2}")
            return "none"

def _get_brain(mcp_set="none"):
    """获取或构建指定MCP组合的大脑(带缓存)。
    每次返回前刷新 system_message 中的时间/待办等动态信息。"""
    global _brain_build_time
    with _brain_lock:
        if mcp_set not in _brains:
            _brains[mcp_set] = _build_brain(mcp_set)
            if not _brain_build_time:
                _brain_build_time = time.time()
            log.info(f"[brain] 大脑构建完成: mcp={mcp_set}, 缓存总数={len(_brains)}")
        brain = _brains[mcp_set]
    brain.system_message = _build_system_msg(mcp_set)
    return brain

def _ensure_event_loop():
    """确保当前线程有event loop(Qwen-Agent MCP可能需要)"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

def _record_brain_failure(error: str = ""):
    """记录大脑失败, 连续失败超过阈值时自动重建"""
    global _brain_failures, _brain_total_failures, _brain_last_failure, _brains
    with _brain_lock:
        _brain_failures += 1
        _brain_total_failures += 1
        _brain_last_failure = time.time()
        is_429 = '429' in error or 'Too Many' in error or 'rate' in error.lower()
        log.error(f"[brain] 失败#{_brain_failures}(累计{_brain_total_failures}): {error}" +
                  (" [429限流, 不清缓存]" if is_429 else ""))
        if _brain_failures >= _MAX_BRAIN_FAILURES:
            log.warning(f"[brain] 连续失败{_brain_failures}次({'429' if is_429 else '非429'}), 清除所有缓存大脑...")
            for k, b in list(_brains.items()):
                _cleanup_brain_processes(b)
            _brains.clear()
            _brain_failures = 0
        elif is_429 and _brain_failures >= 10:
            log.warning(f"[brain] 429连续{ _brain_failures}次, 清除缓存大脑...")
            for k, b in list(_brains.items()):
                _cleanup_brain_processes(b)
            _brains.clear()
            _brain_failures = 0

def _record_brain_success():
    """记录大脑成功, 重置失败计数"""
    global _brain_failures, _brain_last_success
    with _brain_lock:
        _brain_failures = 0
        _brain_last_success = time.time()

def _cleanup_brain_processes(brain_instance):
    """清理大脑实例关联的MCP子进程(防止僵尸进程)"""
    if brain_instance is None:
        return
    try:
        # Qwen-Agent的MCP服务器通过subprocess管理
        # 清理function_list中的MCP server connections
        if hasattr(brain_instance, '_function_list'):
            for func in (brain_instance._function_list or []):
                if isinstance(func, dict) and 'mcpServers' in func:
                    for name, cfg in func['mcpServers'].items():
                        # 尝试关闭MCP client连接
                        try:
                            if hasattr(brain_instance, '_mcp_clients') and name in (brain_instance._mcp_clients or {}):
                                client = brain_instance._mcp_clients[name]
                                if hasattr(client, 'close'):
                                    client.close()
                                log.info(f"[brain] MCP客户端已关闭: {name}")
                        except Exception as e:
                            log.debug(f"[brain] 关闭MCP客户端 {name} 失败: {e}")
        # 杀掉残留的MCP子进程(防止僵尸进程)
        if hasattr(brain_instance, '_mcp_processes'):
            for name, proc in list((brain_instance._mcp_processes or {}).items()):
                try:
                    if hasattr(proc, 'terminate'):
                        proc.terminate()
                        proc.wait(timeout=5)
                    elif hasattr(proc, 'kill'):
                        proc.kill()
                    log.info(f"[brain] MCP子进程已终止: {name}")
                except Exception as e:
                    log.debug(f"[brain] 终止MCP子进程 {name} 失败: {e}")
                    try:
                        if hasattr(proc, 'kill'):
                            proc.kill()
                    except Exception:
                        pass
    except Exception as e:
        log.debug(f"[brain] MCP清理异常: {e}")

def restart_brain() -> str:
    """手动重启大脑(清除所有缓存大脑+MCP连接, 下次请求重建)"""
    global _brains, _brain_failures
    with _brain_lock:
        for k, b in list(_brains.items()):
            _cleanup_brain_processes(b)
        _brains.clear()
        _brain_failures = 0
    log.info("[brain] 手动重启, 所有缓存大脑已清除")
    return "大脑重启中, 下次请求将自动重建"

def set_current_user(user_id: str):
    """切换当前用户, 更新所有数据文件路径"""
    global _current_user_id
    _current_user_id = user_id or "default"
    os.environ["CHARLIE_USER_ID"] = _current_user_id
    log.info(f"[user] 切换到用户: {_current_user_id}")

def get_current_user() -> str:
    """获取当前用户ID"""
    return _current_user_id

def brain_status() -> dict:
    """获取大脑健康状态"""
    with _brain_lock:
        result = {
            "ready": len(_brains) > 0,
            "cached_brains": list(_brains.keys()),
            "consecutive_failures": _brain_failures,
            "total_failures": _brain_total_failures,
            "max_failures_before_rebuild": _MAX_BRAIN_FAILURES,
            "last_success": datetime.datetime.fromtimestamp(_brain_last_success).isoformat() if _brain_last_success else None,
            "last_failure": datetime.datetime.fromtimestamp(_brain_last_failure).isoformat() if _brain_last_failure else None,
            "uptime_since": datetime.datetime.fromtimestamp(_brain_build_time).isoformat() if _brain_build_time else None,
        }
    return result

def _ollama_fallback(text: str, messages: list) -> str:
    """Finna失败时, 用本地Ollama做离线降级对话。无MCP, 纯文本对话。"""
    try:
        import requests as _req
        # 用 qwen3.5:2b 做降级对话(和意图分类同一个模型, 已在Ollama上)
        # 取最近几轮历史 + 当前输入
        recent = messages[-6:] if len(messages) > 6 else messages
        ollama_msgs = [{"role": m["role"], "content": m["content"]} for m in recent]
        # 过滤掉 __MUSIC__ URL 格式的内容，避免为大模型带来困惑
        for m in ollama_msgs:
            if isinstance(m.get("content"), str) and m["content"].startswith("__MUSIC__"):
                m["content"] = "[音乐播放中]"
        # 确保 system message 存在
        if not ollama_msgs or ollama_msgs[0]["role"] != "system":
            ollama_msgs.insert(0, {"role": "system", "content": "你是Charlie，搭档级AI助理。直接、偶尔幽默、不废话。不知道就说不知道，不编造。别输出占用语，第一句就是答案。"})
        r = _req.post("http://localhost:11434/api/chat", json={
            "model": "qwen3.5:2b",
            "messages": ollama_msgs,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.7, "num_predict": 200}
        }, timeout=(5, 15))
        if r.status_code == 200:
            data = r.json()
            reply = data.get("message", {}).get("content", "").strip()
            if reply:
                return reply
        return ""
    except Exception as e:
        log.warning(f"[brain] Ollama降级也失败: {e}")
        return ""

_OLLAMA_SIMPLE_SYSTEM_MSG = (
    "你是Charlie，搭档级AI助理。直接、偶尔幽默、不废话。\n"
    "回复规则：\n"
    "1. 直接说答案，别输出占用语，第一句就是答案。\n"
    "2. 简洁但不冷漠——偶尔可以带一句人话。\n"
    "3. 不知道的就说不知道，别编造。\n"
    "4. 你觉得有问题就说出来，别盲目执行。\n"
    "当前时间：{time}。"
)


def _direct_vision_analyze(text: str) -> str:
    """视觉关键词命中时直接截图+调用 vision.py，绕过 LLM，避免 429 限流。
    返回视觉分析结果文本，或空字符串(失败时)。
    语音场景：用户说"看看屏幕上有什么"→截图→视觉API分析→返回描述。
    也支持分析用户图片路径（如果文本中包含路径）。"""
    import subprocess, tempfile, os as _os, re as _re
    VISION_SCRIPT = _os.path.join(PROJECT_DIR, "skills", "mimo-vision", "scripts", "vision.py")
    if not _os.path.isfile(VISION_SCRIPT):
        log.warning(f"[vision] vision.py 不存在: {VISION_SCRIPT}")
        return ""
    # 提取问题（去掉视觉关键词前缀）
    question = _re.sub(
        r'^(看看|帮我看|帮我看看|截图分析|识别|分析一下|描述|描述一下|'
        r'屏幕上|屏幕|截屏|截图|帮我识别|帮我描述|帮我分析|'
        r'看一下|看下|看看看|看看)',
        '', text
    ).strip().rstrip('。.,，！!？?')
    if not question or len(question) < 2:
        question = "描述这张图片的内容"
    try:
        # Step 1: 截图 (macOS screencapture)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            screenshot_path = tmp.name
        subprocess.run(
            ["screencapture", "-x", screenshot_path],
            timeout=5, check=True
        )
        # Step 2: 调用 vision.py 分析截图
        r = subprocess.run(
            [sys.executable, VISION_SCRIPT, screenshot_path, "-q", question],
            capture_output=True, text=True, timeout=60,
            cwd=PROJECT_DIR
        )
        # 清理截图
        try:
            _os.unlink(screenshot_path)
        except OSError:
            pass
        if r.returncode == 0 and r.stdout.strip():
            result = r.stdout.strip()
            log.info(f"[vision] 分析成功: {result[:60]}...")
            return result
        else:
            err = r.stderr.strip()[:200]
            log.warning(f"[vision] 分析失败: exit={r.returncode} err={err}")
            return ""
    except subprocess.TimeoutExpired:
        log.warning("[vision] 截图或分析超时")
        return ""
    except Exception as e:
        log.warning(f"[vision] 异常: {e}")
        return ""


def _direct_music_play(text: str) -> str:
    """音乐关键词命中时直接调用 ncm-cli，绕过 LLM，避免 429 限流和反复迭代。
    返回 __MUSIC__url__name__artist 或空字符串(失败时)。"""
    import subprocess, json as _json, random, re as _re
    NCM = os.path.expanduser("~/.local/bin/ncm")
    # PyInstaller 打包后优先从 _internal/bin/ncm 找
    if getattr(sys, 'frozen', False):
        _ncm_packed = os.path.join(os.path.dirname(sys.executable), '_internal', 'bin', 'ncm')
        if os.path.isfile(_ncm_packed):
            NCM = _ncm_packed
    try:
        # 判断是随机播放还是指定歌曲
        is_random = any(kw in text for kw in ('随机', '随便', '来一首', '来首歌', '随便来', '随便播'))
        
        if is_random:
            # 随机播放: 从每日推荐或播放记录中随机选
            r = subprocess.run([NCM, "recommend", "songs", "--json"], capture_output=True, text=True, timeout=10)
            songs = []
            if r.returncode == 0 and r.stdout.strip():
                data = _json.loads(r.stdout)
                songs = data.get("recommend", []) or data.get("data", [])
            if not songs:
                r = subprocess.run([NCM, "record", "--json", "--limit", "30"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    data = _json.loads(r.stdout)
                    songs = data.get("data", []) or data.get("record", [])
            if not songs:
                return ""
            song = random.choice(songs)
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))
        else:
            # 指定歌曲: 从文本提取歌名/歌手名
            # 用正则做前缀匹配，避免 str.replace 误删歌名中的字符
            keyword = re.sub(
                r'^(播放|放一首|放个|我要听|我想听|来一首|播一首|点一首|放|唱|听|来首|点歌|一首|放首|放点|整首|整点|循环|单曲)',
                '', text
            ).strip().rstrip('。.,，！!')
            if not keyword or len(keyword) < 1:
                return ""
            r = subprocess.run([NCM, "search", "song", keyword, "--limit", "1", "--json"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return ""
            data = _json.loads(r.stdout)
            songs = data.get("result", {}).get("songs", [])
            if not songs:
                return ""
            song_id = songs[0]["id"]
            song_name = songs[0]["name"]
            ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))
        
        # 获取播放地址
        r = subprocess.run([NCM, "url", str(song_id), "--level", "exhigh", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ""
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return ""
        # 强制 https
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        log.info(f"[music] ncm直连成功: {song_name} - {ars} ({len(url)}B URL)")
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        log.warning(f"[music] ncm直连异常: {e}")
        return ""

def _direct_weather_play(text: str) -> str:
    """天气关键词命中时直接调用高德接口，绕过 LLM，省掉 2 次 ARK 往返。
    返回自然语言天气摘要，或空字符串(失败/非天气问题时回退 brain)。"""
    AMAP = os.getenv("AMAP_KEY", "")
    if not AMAP:
        return ""
    try:
        r = requests.get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": "110000", "key": AMAP, "extensions": "all"},
            timeout=8,
        ).json()
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if not casts:
            return ""
        today = casts[0]
        day_w = today.get("dayweather", "")
        night_w = today.get("nightweather", "")
        day_t = today.get("daytemp", "")
        night_t = today.get("nighttemp", "")
        parts = []
        for w in (day_w, night_w):
            if w and w not in parts:
                parts.append(w)
        weather = "转".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
        if not weather:
            return ""
        reply = f"今天{weather}，{day_t}到{night_t}度。"
        log.info(f"[weather] 高德直连成功: {reply}")
        return reply
    except Exception as e:
        log.warning(f"[weather] 高德直连失败: {e}")
        return ""

def _direct_ac_control(text: str) -> str:
    """空调控制关键词命中时直接调用 Tuya 2B 红外云 API,绕过 LLM+MCP(2-4s→~0.2s)。
    解析: 开/关 + 模式(制冷/制热/除湿/送风/自动) + 温度(X度) + 风速(高/中/低)。
    返回自然语言回复,无明确指令或调用失败时返回空串(回退 brain)。

    2B 红外 API: POST /v2.0/infrareds/{infrared_id}/air-conditioners/{remote_id}/scenes/command
    infrared_id=TUYA_IR_DEVICE_ID(红外网关), remote_id=TUYA_AC_DEVICE_ID(空调遥控器)。
    需 TUYA_CLIENT_ID/TUYA_ACCESS_KEY(2B 凭证), 未配置则返回空回退 brain。
    """
    try:
        from tuya_api import TuyaCloudAPI
    except Exception as e:
        log.warning(f"[ac] tuya_api 导入失败: {e}")
        return ""
    infrared_id = os.getenv("TUYA_IR_DEVICE_ID", "")
    remote_id = os.getenv("TUYA_AC_DEVICE_ID", "")
    if not infrared_id or not remote_id:
        return ""
    try:
        api = TuyaCloudAPI()
    except ValueError as e:
        log.warning(f"[ac] 2B 凭证未配置: {e}")
        return ""
    import re as _re
    t = text.strip()

    # 关机: 关/关掉/关闭 空调(避免与"开"混淆)
    if (_re.search(r"关(?:闭|掉|上)?\s*空调|空调\s*关|把空调关|关了", t) or
            (("空调" in t or "电扇" in t) and "关" in t and "开" not in t)):
        try:
            api.ac_scenes_command(infrared_id, remote_id, power=0)
            log.info(f"[ac] 直连关机: {t[:20]}")
            return "好的，空调已关闭。"
        except Exception as e:
            log.warning(f"[ac] 关机失败: {e}")
            return ""

    power = False
    mode = None
    temp = None
    fan = None

    # mode(2B scenes: 0制冷/1制热/2自动/3送风/4除湿, 与2C mode映射不同)
    MODE_2B = {"cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4}
    if any(k in t for k in ("制冷", "制冷模式", "冷风", "降温")):
        mode = MODE_2B["cool"]
    elif any(k in t for k in ("制热", "制热模式", "暖风", "加热", "升温")):
        mode = MODE_2B["heat"]
    elif any(k in t for k in ("除湿", "抽湿")):
        mode = MODE_2B["dry"]
    elif any(k in t for k in ("送风", "自然风", "风机模式")):
        mode = MODE_2B["fan"]
    elif any(k in t for k in ("自动模式", "全自动")) or ("自动" in t and "模式" in t):
        mode = MODE_2B["auto"]
    # 风速
    if "高风" in t or "最大风" in t:
        fan = 3
    elif "中风" in t or "中等风" in t:
        fan = 2
    elif "低风" in t or "小风" in t or "微风" in t:
        fan = 1
    # 温度: "26度" / "调到26" / "设26度"
    m = _re.search(r"(\d{1,3})\s*(?:度|℃|°C|°C|摄氏度)", t)
    if not m:
        m = _re.search(r"(?:调到|设成|调到|调至|开?\s*到)\s*(\d{1,3})", t)
    if m:
        try:
            temp = int(m.group(1))
        except ValueError:
            temp = None

    # 开: 明确的打开/开机指令(避免把"关"里捞到"开")
    if any(k in t for k in ("开空调", "打开空调", "把空调", "开机制冷", "开机", "打开制冷", "开启空调")):
        power = True
    elif mode is not None or temp is not None or fan is not None:
        power = True
    elif any(k in t for k in ("开",)) and any(k in t for k in ("空调", "制冷", "制热", "除湿")):
        power = True
    else:
        power = False

    if mode is None and temp is None and fan is None and not power:
        # 无法解析出任何控制意图 → 让 brain 处理
        return ""

    if power:
        # 红外发码需完整状态: 未指定温度补26(残留值10不在码库范围发不出码)
        eff_temp = max(16, min(30, temp if temp is not None else 26))
        try:
            api.ac_scenes_command(infrared_id, remote_id, power=1, mode=mode, temp=eff_temp, wind=fan)
            parts = ["空调已打开"]
            if mode is not None:
                parts.append({0: "制冷", 1: "制热", 2: "自动", 3: "送风", 4: "除湿"}[mode])
            parts.append(f"{eff_temp}度")
            if fan is not None:
                parts.append({1: "低风", 2: "中风", 3: "高风"}.get(fan, ""))
            reply = "，".join(p for p in parts if p) + "。"
            log.info(f"[ac] 直连开机成功: {reply}")
            return reply
        except Exception as e:
            log.warning(f"[ac] 开机失败: {e}")
            return ""
    return ""


def _handle_smart_command(text: str) -> str | None:
    """智能语音快捷命令: 不走 brain, 直接执行系统操作
    支持: 停止/暂停、音量控制、静音、睡眠模式
    返回: 回复文本(已处理) 或 None(不匹配, 交给 brain)
    """
    import subprocess
    text_lower = text.strip().lower()
    # 系统音量控制
    if any(kw in text for kw in ['音量大', '大声点', '大一点', 'volume up', '音量加']):
        try:
            subprocess.run(['osascript', '-e', 'set volume output volume (output volume of (get volume settings) + 20)'], timeout=3)
            return '音量已调大。'
        except Exception:
            return None
    if any(kw in text for kw in ['音量小', '小声点', '小一点', 'volume down', '音量减']):
        try:
            subprocess.run(['osascript', '-e', 'set volume output volume (output volume of (get volume settings) - 20)'], timeout=3)
            return '音量已调小。'
        except Exception:
            return None
    if any(kw in text for kw in ['静音', 'mute', '消音']):
        try:
            subprocess.run(['osascript', '-e', 'set volume output muted true'], timeout=3)
            return '已静音。'
        except Exception:
            return None
    # 停止/暂停 — 当前无法中断 TTS, 但可以给用户反馈
    if text_lower in ('停止', '暂停', '停', 'stop', 'pause', '闭嘴'):
        try:
            subprocess.run(['osascript', '-e', 'set volume output muted true'], timeout=3)
            return '好的，我停。'
        except Exception:
            return None
    # 睡眠模式
    if any(kw in text for kw in ['睡眠', '休眠', 'sleep', '显示器关闭']):
        try:
            subprocess.run(['pmset', 'displaysleepnow'], timeout=3)
            return '已进入睡眠模式。'
        except Exception:
            return None
    return None


def brain(text: str, session_id: str = "default") -> str:
    """大脑推理: 意图=none走Ollama(本地,0.25s), ≠none走Finna+MCP(云,2s)"""
    # 智能语音快捷命令: 不需要走 brain LLM 的简单指令
    _cmd_reply = _handle_smart_command(text)
    if _cmd_reply is not None:
        _append_history(_get_history(session_id), text, _cmd_reply)
        return _cmd_reply
    # 决策反馈检测: 检查是否有待确认的决策等待用户回应
    try:
        import importlib.util as _iu
        _spec_dec = _iu.spec_from_file_location("magic_decisions", os.path.join(PROJECT_DIR, "magic-decisions.py"))
        if _spec_dec and _spec_dec.loader:
            _dec = _iu.module_from_spec(_spec_dec)
            _spec_dec.loader.exec_module(_dec)
            _pending = _dec.get_pending_confirmation()
            if _pending:
                rule_id = _pending.get("rule_id", "")
                # 检测用户是否在接受或拒绝
                text_lower = text.strip().lower()
                positive = ("好" in text_lower or "行" in text_lower or "可以" in text_lower
                            or "嗯" in text_lower or "要" in text_lower or "yes" in text_lower
                            or text_lower in ("对", "是", "ok", "okay", "好的", "好呀", "行啊", "可以啊"))
                negative = ("不用" in text_lower or "不要" in text_lower or "别" in text_lower
                            or "不需要" in text_lower or "算了" in text_lower or "no" in text_lower
                            or "不" in text_lower and len(text_lower) < 6)
                if positive:
                    _dec.record_feedback(rule_id, True)
                    _dec.clear_pending_confirmation()
                    log.info(f"[decision-feedback] 用户接受决策 {rule_id}")
                    # 找到并执行该决策动作
                    for _r in _dec.get_rules():
                        if _r["id"] == rule_id:
                            _action = _r.get("action", {})
                            if _action.get("type") == "tts":
                                reply = _action.get("text", "已执行")
                                _append_history(_get_history(session_id), text, reply)
                                return reply
                            break
                elif negative:
                    _dec.record_feedback(rule_id, False)
                    _dec.clear_pending_confirmation()
                    log.info(f"[decision-feedback] 用户拒绝决策 {rule_id}")
                    reply = "好的，那就不做这个了。"
                    _append_history(_get_history(session_id), text, reply)
                    return reply
    except Exception as _e:
        log.debug(f"[decision-feedback] 检测跳过: {_e}")
    # 时间类查询直接返回实时时间，绕过LLM(避免缓存/历史污染/限流)
    TIME_KEYWORDS = ('几点', '几点啦')
    if any(kw in text for kw in TIME_KEYWORDS):
        now = datetime.datetime.now()
        reply = f"现在{now.strftime('%H点%M分')}。"
        log.info(f"[time] 直接返回实时时间: {reply}")
        return reply
    # 空调类控制直接调用 Tuya 云 API，绕过LLM(原走 ac-control MCP 需2次ARK+1次API)
    AC_KEYWORDS = ('空调', '制冷', '制热', '除湿', '开空调', '调温度', '温度调',
                   '高风', '中风', '低风', '风速')
    if any(kw in text for kw in AC_KEYWORDS) and '天气' not in text:
        ac_reply = _direct_ac_control(text)
        if ac_reply:
            _append_history(_get_history(session_id), text, ac_reply)
            return ac_reply
        log.info("[ac] 空调直连未命中或失败，回退到brain")
    # 天气类查询直接调高德，绕过LLM(原走 amap-maps MCP 需2次ARK+1次外部API，共~5s)
    WEATHER_KEYWORDS = ('天气', '气温', '下雨', '下雪', '温度', '几度',
                        '穿什么', '今天冷', '今天热', '冷不冷', '热不热')
    if any(kw in text for kw in WEATHER_KEYWORDS):
        weather_reply = _direct_weather_play(text)
        if weather_reply:
            _append_history(_get_history(session_id), text, weather_reply)
            return weather_reply
        log.warning("[weather] 高德直连失败，回退到brain")
    # 音乐播放类查询直接路由到MCP，绕过LLM(避免反复迭代)
    MUSIC_KEYWORDS = ('播放音乐', '播放歌', '随机播放', '放歌', '放一首', '放个', '唱首歌', '放音乐', '来首歌', '来一首', '播一首', '听歌', '点歌', '点一首', '我想听', '我要听', '播放', '来首', '放首', '循环', '单曲')
    if any(kw in text for kw in MUSIC_KEYWORDS):
        log.info(f"[music] 音乐关键词命中，直接调用ncm绕过LLM: {text[:20]}")
        music_reply = _direct_music_play(text)
        if music_reply:
            _append_history(_get_history(session_id), text, music_reply)
            return music_reply
        log.warning(f"[music] ncm直连失败，回退到brain")
    # 视觉关键词命中直接截图+分析，绕过LLM(避免429限流)
    VISION_KEYWORDS = ('看看屏幕', '看看屏幕上', '截屏', '截图分析', '帮我看看屏幕', '屏幕上有什么', '屏幕上显示')
    if any(kw in text for kw in VISION_KEYWORDS):
        log.info(f"[vision] 视觉关键词命中，直接截图分析: {text[:20]}")
        vision_reply = _direct_vision_analyze(text)
        if vision_reply:
            _append_history(_get_history(session_id), text, vision_reply)
            return vision_reply
        log.warning(f"[vision] vision.py调用失败，回退到brain")
    # 缓存命中
    cached = _cache_get(text)
    if cached is not None:
        log.info(f"[cache] 命中: {text[:20]}")
        return cached
    # 意图路由: 判断需要哪些MCP，全部走 ARK + MCP
    mcp_set = _classify_intent(text)
    # 刷新当前用户输入, 供叙事性记忆注入
    global _current_user_input
    _current_user_input = text
    # Protocol 场景匹配: 如果触发词匹配, 直接执行场景, 不走 LLM
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_scenes", "magic-scenes.py")
        if _spec and _spec.loader:
            _mod = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _match_proto = _mod.match_protocol
            _exec_proto = _mod.execute_protocol
            proto_key = _match_proto(text)
        if proto_key:
            log.info(f"[scene] Protocol触发: {text[:20]} → {proto_key}")
            reply = _exec_proto(proto_key)
            _append_history(_get_history(session_id), text, reply)
            _cache_set(text, reply)
            return reply
    except Exception:
        pass
    _ensure_event_loop()
    try:
        brain_instance = _get_brain(mcp_set)
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        return "大脑启动失败，请稍后重试。"

    hist = _get_history(session_id)
    messages = [{'role': m['role'], 'content': m['content']} for m in hist] + [{'role': 'user', 'content': text}]
    try:
        final = None
        for rsp in brain_instance.run(messages):
            final = rsp
            # 检测到 __MUSIC__ 立即短路，避免 brain 反复迭代同一工具
            if isinstance(rsp, list):
                for m in rsp:
                    if isinstance(m, dict) and m.get('role') == 'function' and isinstance(m.get('content'), str) and m['content'].startswith('__MUSIC__'):
                        break
                else:
                    continue
                break
        _record_brain_success()
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        # 离线降级: Finna失败时 fallback 到本地 Ollama
        ollama_reply = _ollama_fallback(text, messages)
        if ollama_reply:
            log.info(f"[brain] Finna失败, Ollama降级成功: {ollama_reply[:30]}")
            _append_history(hist, text, ollama_reply)
            _cache_set(text, ollama_reply)
            return ollama_reply
        return "抱歉，我现在有点忙不过来，请稍等一下再试。"

    reply = "我没听明白"
    if final and isinstance(final, list):
        # 收集所有 function 消息的工具返回内容
        func_replies = []
        for m in final:
            if m.get('role') == 'function' and m.get('content'):
                c = str(m.get('content', ''))
                if c.strip():
                    func_replies.append(c)
                    if c.startswith('__MUSIC__'):
                        reply = c
                        break
        # 如果找到 __MUSIC__ 直接返回
        if reply.startswith('__MUSIC__'):
            pass
        # 没找到 __MUSIC__ 但有其他工具返回内容, 使用第一个非空内容
        elif reply == "我没听明白" and func_replies:
            reply = func_replies[0][:200]
        # 如果没找到工具返回, 找 assistant 文字回复
        if reply == "我没听明白":
            for m in reversed(final):
                if m.get('role') == 'assistant':
                    c = m.get('content')
                    if isinstance(c, str) and c.strip():
                        reply = c
                        break
                    elif isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and part.get('text'):
                                reply = part['text']
                                break
                        if reply != "我没听明白":
                            break
        # 如果assistant content为空但有function_call(工具调用了但没生成文字)
        # 生成一个默认回复
        if reply == "我没听明白":
            has_tool_call = any(m.get('function_call') for m in final if isinstance(m, dict))
            has_music = any('__MUSIC__' in str(m.get('content','')) for m in final if isinstance(m, dict))
            if has_music:
                reply = "__MUSIC_PLAYING__"
            elif has_tool_call:
                reply = "好的，已执行。"

    # 更新历史+缓存
    _append_history(hist, text, reply)
    _cache_set(text, reply)
    # 叙事性记忆: 异步提取关键事件
    try:
        import threading as _mem_thread
        _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
    except Exception:
        pass
    return reply


def _extract_assistant_text(rsp: Any) -> str:
    if not isinstance(rsp, list):
        return ""
    for m in reversed(rsp):
        if not isinstance(m, dict) or m.get("role") != "function":
            continue
        c = m.get("content", "")
        if isinstance(c, str) and c.startswith("__MUSIC__"):
            return c
    for m in reversed(rsp):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("text"):
                    return part["text"]
    return ""

def _interrupted_context_message(interrupted_reply: str) -> dict:
    reply = (interrupted_reply or "").strip()[:200]
    return {
        "role": "system",
        "content": (
            "上一条助手回复被用户打断，只播报或显示了片段："
            + reply +
            "。请基于已显示的内容继续新的对话，不要重复之前的回复。"
        ),
    }

def _remember_conversation_async(user_text: str, assistant_reply: str):
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_memory", os.path.join(PROJECT_DIR, "magic-memory.py"))
        if _spec and _spec.loader:
            _mem = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_mem)
            _mem.extract_memories(user_text, assistant_reply)
    except Exception as e:
        log.debug(f"[memory] 记忆提取跳过: {e}")

# ===== 流式大脑: 逐句产出，支持TTS流水线 =====
import queue as _queue
_SENTENCE_END = re.compile(r'[。！？；\n]')
_COMMA_SOFT = re.compile(r'[，,]')
_MIN_CHUNK = 15
_MAX_CHUNK = 80

def brain_stream_sentences(text: str, session_id: str = "default", interrupted_reply: str = "") -> Generator[Tuple[str, str], None, None]:
    """流式大脑: brain.run()增量产出token → 检测句子边界 → yield完整句。
    最后更新对话历史+缓存。
    yield: (sentence:str, full_reply:str)
    最后一次yield后，full_reply是完整回复。
    """
    # 占用语过滤: LLM可能先输出"让我想想"等废话，这些话会被流式TTS立即合成播放
    # 但最终回复里又不包含，导致用户听到了但历史里看不到。在yield前直接过滤掉。
    _FILLER_WORDS = {
        "让我想想", "稍等一下", "稍等", "让我思考一下", "想一想",
        "让我看看", "我看看", "好的", "好", "嗯嗯", "嗯",
        "让我想一下", "我想想", "等一下", "稍等下",
    }
    def _is_filler(s: str) -> bool:
        """检查句子是否是纯占用语（去掉标点后完全匹配）"""
        cleaned = re.sub(r'[，。！？\s]', '', s).strip()
        is_filler = bool(cleaned) and len(cleaned) <= 6 and cleaned in _FILLER_WORDS
        log.info(f"[filler] input='{s}' cleaned='{cleaned}' is_filler={is_filler}")
        return is_filler

    # 时间类查询直接返回实时时间，绕过LLM(避免缓存/历史污染/限流)
    TIME_KEYWORDS = ('几点', '几点啦')
    if any(kw in text for kw in TIME_KEYWORDS):
        now = datetime.datetime.now()
        reply = f"现在{now.strftime('%H点%M分')}。"
        log.info(f"[time] 直接返回实时时间: {reply}")
        yield (reply, reply)
        return
    # 空调类控制直接调用 Tuya 云 API，绕过LLM
    AC_KEYWORDS = ('空调', '制冷', '制热', '除湿', '开空调', '调温度', '温度调',
                   '高风', '中风', '低风', '风速')
    if any(kw in text for kw in AC_KEYWORDS) and '天气' not in text:
        ac_reply = _direct_ac_control(text)
        if ac_reply:
            yield (ac_reply, ac_reply)
            return
        log.info("[ac] 空调直连未命中或失败(流式)，回退到brain")
    # 天气类查询直接调高德，绕过LLM(原走 amap-maps MCP 需2次ARK+1次外部API，共~5s)
    WEATHER_KEYWORDS = ('天气', '气温', '下雨', '下雪', '温度', '几度',
                        '穿什么', '今天冷', '今天热', '冷不冷', '热不热')
    if any(kw in text for kw in WEATHER_KEYWORDS):
        weather_reply = _direct_weather_play(text)
        if weather_reply:
            yield (weather_reply, weather_reply)
            return
        log.info("[weather] 高德直连失败，回退到brain")
    # 音乐播放类查询，直接调用ncm绕过LLM
    MUSIC_KEYWORDS = ('播放音乐', '播放歌', '随机播放', '放歌', '放一首', '放个', '唱首歌', '放音乐', '来首歌', '来一首', '播一首', '听歌', '点歌', '点一首', '我想听', '我要听', '播放', '来首', '放首', '循环', '单曲')
    if any(kw in text for kw in MUSIC_KEYWORDS):
        log.info(f"[music] 音乐关键词命中(流式)，直接调用ncm绕过LLM: {text[:20]}")
        music_reply = _direct_music_play(text)
        if music_reply:
            hist = _get_history(session_id)
            _append_history(hist, text, music_reply)
            yield (music_reply, music_reply)
            return
        log.warning(f"[music] ncm直连失败(流式)，回退到brain")
    # 缓存命中
    cached = _cache_get_interrupted(text, interrupted_reply) if interrupted_reply.strip() else _cache_get(text)
    if cached is not None:
        yield (_clean_for_tts(cached), cached)
        return
    # 意图路由: 判断需要哪些MCP，全部走 ARK + MCP
    mcp_set = _classify_intent(text)
    # 刷新当前用户输入, 供叙事性记忆注入
    global _current_user_input
    _current_user_input = text
    # Protocol 场景匹配: 如果触发词匹配, 直接执行场景, 不走 LLM
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_scenes", "magic-scenes.py")
        if _spec and _spec.loader:
            _mod = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _match_proto = _mod.match_protocol
            _exec_proto = _mod.execute_protocol
            proto_key = _match_proto(text)
        if proto_key:
            log.info(f"[scene] Protocol触发: {text[:20]} → {proto_key}")
            reply = _exec_proto(proto_key)
            _append_history(_get_history(session_id), text, reply)
            _cache_set(text, reply)
            return reply
    except Exception:
        pass
    _ensure_event_loop()
    try:
        brain_instance = _get_brain(mcp_set)
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        message = "大脑启动失败，请稍后重试。"
        yield (message, message)
        return

    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist] + [{"role": "user", "content": text}]
    if interrupted_reply.strip():
        messages.insert(-1, _interrupted_context_message(interrupted_reply))
    sent_len = 0       # 已yield的字符数
    full_reply = ""

    try:
        for rsp in brain_instance.run(messages):
            # 检测到 __MUSIC__ 立即短路，避免反复迭代
            if isinstance(rsp, list):
                _has_music = False
                for _m in rsp:
                    if isinstance(_m, dict) and _m.get('role') == 'function' and isinstance(_m.get('content'), str) and _m['content'].startswith('__MUSIC__'):
                        _has_music = True
                        break
                if _has_music:
                    t = _extract_assistant_text(rsp)
                    if t:
                        full_reply = t
                    break
            t = _extract_assistant_text(rsp)
            if not t or len(t) <= sent_len:
                continue
            full_reply = t
            unsent = full_reply[sent_len:]
            while True:
                # 优先在句末标点处切割
                m = _SENTENCE_END.search(unsent)
                if m:
                    sentence = unsent[:m.end()].strip()
                    unsent = unsent[m.end():]
                    if sentence:
                        sent_len = len(full_reply) - len(unsent)
                        cleaned_sentence = _clean_for_tts(sentence)
                        if not _is_filler(cleaned_sentence):
                            yield (cleaned_sentence, full_reply)
                    continue
                # 句末无标点但已积累较长 → 在逗号处软切割
                if len(unsent) >= _MIN_CHUNK:
                    cm = _COMMA_SOFT.search(unsent)
                    if cm:
                        sentence = unsent[:cm.end()].strip()
                        unsent = unsent[cm.end():]
                        if sentence:
                            sent_len = len(full_reply) - len(unsent)
                            cleaned_sentence = _clean_for_tts(sentence)
                            if not _is_filler(cleaned_sentence):
                                yield (cleaned_sentence, full_reply)
                        continue
                break
            sent_len = len(full_reply) - len(unsent)
        _record_brain_success()
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        if not full_reply:
            full_reply = "抱歉，处理时出错了，请稍后再试。"

    # 剩余文本作为最后一句
    if full_reply and len(full_reply) > sent_len:
        remaining = full_reply[sent_len:].strip()
        if remaining:
            cleaned_remaining = _clean_for_tts(remaining)
            if not _is_filler(cleaned_remaining):
                yield (cleaned_remaining, full_reply)
    elif not full_reply:
        full_reply = "我没听明白"
        yield (full_reply, full_reply)

    # 更新历史+缓存
    _append_history(hist, text, full_reply)
    if session_id != "default":
        _trim_history_tokens(hist, session_id=session_id)
    _cache_set(text, full_reply, interrupted_reply)

def stream_voice_pipeline(text: str) -> Generator[Tuple[str, str, bytes], None, None]:
    """
    流式语音流水线生成器: 大脑逐句产出 → TTS → yield (type, sentence, mp3)。
    type: "sentence"(文字+音频), "error"(错误信息), "done"(完成)
    大脑流式产出减少了整体等待时间。
    服务器端可在此基础上实现更细粒度的并行(见 voice_server SSE端点)。
    """
    try:
        for sentence, full_reply in brain_stream_sentences(text):
            mp3 = _tts_cleaned_to_mp3(sentence)
            yield ("sentence", sentence, mp3)
    except Exception as e:
        log.error(f"流式流水线异常: {e}")
        yield ("error", str(e)[:60], b"")
    yield ("done", None, b"")



# ===== 完整语音闭环 =====
def voice_loop(audio_in: bytes, fmt: str = "mp3") -> Tuple[str, str, bytes]:
    """语音进 → ASR → 大脑(含MCP) → TTS → 语音出"""
    text = asr(audio_in, fmt)
    if not text:
        return EMPTY_ASR_TEXT, EMPTY_ASR_REPLY, b""
    if is_low_intent_asr(text):
        return text, LOW_INTENT_ASR_REPLY, b""
    reply = brain(text)
    try:
        audio_out = tts(reply)
    except Exception as e:
        log.warning(f"voice_loop TTS降级为文字: {e}")
        audio_out = b""
    return text, reply, audio_out


# 预热: 在模块加载时构建一次 none 大脑实例(避免首请求等待 976ms)
try:
    _get_brain("none")
    log.info("[warmup] brain(none) 预热完成")
except Exception as e:
    log.warning(f"[warmup] brain(none) 预热失败: {e}")

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    q = sys.argv[1] if len(sys.argv) > 1 else "帮我搜下北京附近的充电桩"
    print(f"① TTS生成输入语音: {q}")
    audio_in = tts(q)
    print(f"   输入音频 {len(audio_in)}字节")
    print("② ASR识别 + ③大脑推理(调MCP) + ④TTS合成回复...")
    text, reply, audio_out = voice_loop(audio_in)
    print(f"\n  ASR识别: {text}")
    print(f"  大脑回复: {reply}")
    print(f"  回复音频: {len(audio_out)}字节")
    output_path = runtime_audio_path("voice_reply.wav")
    write_audio_file(output_path, audio_out)
    print(f"  已保存 {output_path}")
