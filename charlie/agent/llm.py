"""LLM 调用链核心模块（7 个职责域，全部共享状态在 agent/llm_state.py）:

1. Intent 路由: _KEYWORD_MAP 关键词预匹配 + _classify_intent LLM 分类
2. qwen_agent 兼容 patch: _install_openai_compat（未知参数透传）
3. MCP 工具超时 patch: _install_mcp_timeout_patch（future.result 30s 超时，
   超时包装成带 timeout 关键字的异常以触发 provider 轮换）
4. Brain 构建/缓存/LRU: _build_brain, _get_brain, restart_brain, reload_brain_config
   （_cleanup_brain_processes 是 no-op——MCPManager 单例下进程全局共享，
   运行期清理会误杀其他 brain；彻底清理由 voice_server lifespan shutdown 负责）
5. Provider 轮换: _try_rotate_provider_on_failure（同一 provider 连续失败
   >=_PROVIDER_ROTATE_THRESHOLD 次轮换到下一个已配置 provider，锁保护，成功清零）
6. LLM 调用链: _brain_llm 非流式 / _chat_lite_stream 闲聊轻量通道 /
   brain_stream_sentences 流式（死链兜底 + 失败即时重建 + 非流式回退）
7. 工作记忆/填充词: _build_wm_anaphor_prompt, _FILLER_WORDS

共享状态经 import agent.llm_state as _st 模块对象访问，
标量变更（_st.brain_failures += 1）对所有持有者可见。

Public API (re-exported by voice_agent.py for backward compat):
  - brain_stream_sentences(), stream_voice_pipeline()
  - restart_brain(), reload_brain_config(), brain_status()
  - set_current_user(), get_current_user()
  - _classify_intent(), _get_brain(), _build_brain()
  - _record_brain_failure(), _record_brain_success()
  - _chat_lite_stream(), _try_rotate_provider_on_failure()
  - _install_openai_compat(), _install_mcp_timeout_patch()
  - _extract_assistant_text()
"""
import os, sys, json, copy, datetime, time, logging, asyncio, re, threading, queue
from typing import Generator, Tuple, Any, Dict
from collections import OrderedDict

try:
    from openai import RateLimitError as _RateLimitError
except ImportError:
    _RateLimitError = None

import agent.llm_state as _st
from agent.llm_state import (
    FINNA, ARK_BASE, ARK_KEY, ARK_MODEL,
    EMPTY_ASR_TEXT, EMPTY_ASR_REPLY,
    INTENT_FAILURE_THRESHOLD, INTENT_FAILURE_COOLDOWN,
    _MAX_BRAIN_FAILURES, _INTENT_CACHE_MAX, _INTENT_CACHE_TTL,
    session as _session, UNKNOWN_KWARG_RE as _UNKNOWN_KWARG_RE,
    SENTENCE_END as _SENTENCE_END, COMMA_SOFT as _COMMA_SOFT,
    MIN_CHUNK as _MIN_CHUNK, MAX_CHUNK as _MAX_CHUNK,
    OLLAMA_SIMPLE_SYSTEM_MSG,
)
from app.llm_config import (
    OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_OPENAI_BASE,
    demo_mode_active as _demo_mode_active_impl,
    ollama_online as _ollama_online_impl,
    active_chat_endpoint, LLM_PRIORITY,
    active_is_glm,
    is_agnes_configured,
    AGNES_BASE, AGNES_KEY, AGNES_MODEL,
)
from agent.cache import _cache_get, _cache_set, _cache_get_interrupted
from agent.history import (
    _get_history, _append_history, _save_history,
    _trim_history_tokens,
)
from agent.system_msg import _build_system_msg, _build_lite_system_msg
from agent.asr_tts import _clean_for_tts, _tts_cleaned_to_mp3
from agent.intent import LOW_INTENT_ASR_REPLY, is_low_intent_asr, is_garbled_asr
from agent.working_memory import (
    update as update_working_memory,
    get as get_working_memory,
    switch_session as wm_switch_session,
    increment_clarification,
)
from agent.context import invalidate_context_cache

log = logging.getLogger("magic")


def _is_429_error(err_str: str) -> bool:
    s = err_str.lower()
    return ('429' in s or 'too many' in s or 'rate' in s
            or '1305' in s or '频率' in s)


def _is_conn_error(err_str: str) -> bool:
    s = err_str.lower()
    return ('connection' in s or 'timeout' in s or 'broken pipe' in s
            or 'reset' in s or 'closed' in s or 'remotedisconnected' in s)


def _friendly_brain_error(err: str) -> str:
    s = err.lower()
    if 'timeout' in s or 'timed out' in s:
        return '抱歉，刚才的请求响应超时了，请稍后再试。'
    if 'mcp工具' in s or '暂时不可用' in s:
        return '抱歉，这个功能暂时不可用，请稍后重试或换个说法。'
    return '抱歉，处理时出错了，请稍后再试。'


_wm_local = threading.local()


def _get_last_wm_session() -> str:
    return getattr(_wm_local, "session", "default")


def _set_last_wm_session(sid: str) -> None:
    _wm_local.session = sid


def _get_max_sentences() -> int:
    """从 evolution_data 读取 max_sentences 适配状态（0 表示无限制）。"""
    try:
        import json as _json
        uid = os.environ.get("CHARLIE_USER_ID", "default")
        _data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _evolution_file = os.path.join(_data_dir, f"evolution_data_{uid}.json" if uid != "default" else "evolution_data.json")
        if not os.path.exists(_evolution_file):
            return 0
        with open(_evolution_file, 'r', encoding='utf-8') as _f:
            _ev_data = _json.load(_f)
        return int(_ev_data.get("adaptation_state", {}).get("max_sentences", 0))
    except Exception:
        return 0

_KEYWORD_MAP = [
    # 偏好记忆（最高优先级）："...，记住" 句式是用户设置事实记忆的固定模式
    # 必须排在 scenes/reminder 等域之前，否则"起床，记住"/"提醒我，记住"会被其他域抢走
    ({"，记住", ",记住", "，记一下", ",记一下",
      "我喜欢什么", "我喜欢的", "我喜欢吃", "我喜欢喝", "我的偏好", "我偏好",
      "我的口味", "口味偏好", "别再", "我习惯",
      "我最喜欢", "我最爱", "我叫什么名字，记住", "我叫", "记住我", "记住我喜欢",
      "我最喜欢的食物", "我最喜欢的颜色", "我最喜欢的歌", "我最喜欢的地方"}, "magic-preferences"),
    # 记忆查询：用户问"我叫什么"是在回忆之前告诉过助手的事实，属于 memory 域
    ({"我叫什么", "我的名字", "我之前说过", "你记得吗", "你记得我", "我跟你说过",
      "我记得我", "我上次说", "我刚才说", "我刚才告诉你",
      "我名字里有几个字", "我名字里有几个",
      "猫叫什么", "狗叫什么", "宠物叫"}, "magic-memory"),
    ({"切换到", "切换回", "切换", "我要切换", "切到", "换角色", "换到", "切回去"}, "magic-jarvis"),
    # 摄像头/视觉/看看（"看看"在屏幕语境下是mimo-vision，门口/院子是vision_camera类场景）
    ({"摄像头", "门口有", "门口没", "看看门口", "看看后门", "看看院子", "看看猫眼", "后门有人", "前门有人",
      "快递到了吗", "快递到没", "门口有快递", "帮我看摄像头"}, "mimo-vision"),
    # magic-apps：电商/票务 app，"猫眼"必须避免被 mimo-vision 的"看看猫眼"误命中
    ({"微信", "支付宝", "今日头条", "美团", "拼多多", "猫眼电影", "猫眼票房", "大麦", "咸鱼", "外卖",
      "酒店", "机票", "火车票", "高铁", "电影票", "餐厅", "日料", "火锅", "美食", "门票", "演出", "演唱会"}, "magic-apps"),
    # 电视控制(DLNA)——"电视音量/电视静音"需先于 ac-control 的泛"电视"命中
    ({"电视音量", "电视静音", "电视暂停", "电视停止", "投屏", "投到电视", "电视状态",
      "电视现在音量", "电视音量调"}, "magic-tv"),
    ({"天气", "气温", "下雨", "温度", "几度", "今天天气", "明天天气", "今天冷", "今天热", "定位"}, "amap-maps"),
    ({"地图", "导航", "附近", "我在哪", "路线", "怎么走", "到哪"}, "amap-maps"),
    ({"搜一下", "查一下", "查查", "谷歌", "购物", "买东西", "票房", "票房多少"}, "baize-skills"),
    ({"提醒", "定时", "闹钟", "备忘", "日程", "待办", "记一下", "提醒我"}, "magic-reminder"),
    ({"笔记", "备忘录", "记下来", "记一下"}, "magic-notes"),
    ({"音量", "说慢", "说快", "语速", "大声", "小声"}, "magic-system"),
    ({"状态", "运行", "负载", "设备"}, "magic-system"),
    ({"新闻", "头条", "热点"}, "magic-info"),
    ({"几点", "几点钟", "日期", "星期", "什么时候"}, "magic-info"),
    ({"翻译", "翻成", "英语说", "怎么说"}, "magic-info"),
    ({"计算", "算一下", "换算", "等于多少", "等于几", "加几", "减几", "加多少", "减多少", "乘以", "除以",
      "what time", "what time is it"}, "magic-info"),
    ({"放歌", "放一首", "放个", "播放", "听歌", "放周杰伦", "放毛不易", "音乐", "歌单", "停止播放", "每日推荐", "随机", "来一首", "播一首", "点一首", "放首", "放点", "整首", "整点", "循环", "单曲", "来首", "点歌", "唱首歌", "放音乐", "暂停", "继续播放", "继续", "下一首", "上一首", "换一首", "切歌", "停止", "停"}, "magic-music"),
    ({"空调", "电视", "制冷", "制热", "风扇", "开灯", "关灯", "关闭空调", "关闭电视"}, "ac-control"),
    ({"文件", "读文件", "写文件", "笔记"}, "filesystem"),
    ({"外卖", "点餐", "购物", "商品", "查一下", "充电桩", "特斯拉", "出门"}, "magic-life"),
    ({"穿什么", "穿搭", "今天穿", "明天穿", "出门穿", "约会穿", "搭配", "衣橱", "衣服", "加衣服", "外套", "冷了穿", "热了穿", "穿几件"}, "magic-wardrobe"),
    ({"做菜", "菜谱", "食谱", "做什么菜", "食材", "吃什么", "吃饭", "好吃", "想吃什么", "做饭", "怎么做", "做法", "怎么煮", "怎么炒", "今天吃啥", "今晚吃啥", "中午吃啥", "推荐个菜", "推荐一道菜", "凉菜", "热菜", "汤", "主食", "下饭", "买菜", "番茄炒蛋", "可乐鸡翅"}, "magic-recipe"),
    ({"学习", "进化", "自进化", "优化", "自我优化", "自学习", "学习进度", "进化状态"}, "magic-evolution"),
    ({"淘宝", "京东", "比价", "商品", "价格对比", "买东西", "购物", "买一个", "买个"}, "magic-taobao"),
    ({"浏览器", "打开网页", "打开网站", "访问", "浏览", "爬取", "截图", "页面", "网页", "百度"}, "magic-browser"),
    ({"飞书", "飞书文档", "飞书消息", "飞书日历", "日历"}, "magic-feishu"),
    ({"抖音", "douyin", "抖音搜索", "抖音视频", "抖音热搜", "热门视频", "热搜"}, "magic-douyin"),
    ({"改代码", "修改文件", "编辑文件", "读文件", "看代码", "改一下", "改个", "修bug", "修个", "开发模式", "代码修改"}, "magic-dev"),
    ({"晚安", "睡觉", "好梦", "休息吧", "睡吧", "睡", "goodnight"}, "magic-scenes"),
    ({"早上好", "早安", "起床", "good morning", "上午好"}, "magic-scenes"),
    ({"电影", "看电影", "视频", "追剧", "观影", "movie"}, "magic-scenes"),
    ({"看看屏幕", "屏幕上有什么", "截图分析", "帮我看看屏幕", "截屏", "识别图片", "图上有什么", "看看这张图", "看看这张", "看看这个图", "屏幕上显示什么", "屏幕上有啥"}, "mimo-vision"),
]
_ALL_DOMAIN_KEYWORDS = set()
for _kw_set, _ in _KEYWORD_MAP:
    _ALL_DOMAIN_KEYWORDS |= _kw_set

# 高歧义词：如果出现在感叹/评价语境中，不应触发工具查询
_EXCLAMATION_SUFFIXES = ("不错", "不错吧", "真好", "挺好的", "挺不错", "还行", "很差", "太差", "好差",
                        "太热", "太冷", "好热", "好冷", "好闷", "舒服", "不舒服", "不好", "棒", "糟糕", "还行吧")
_AMBIGUOUS_KWS = ("天气", "温度", "下雨")

# 复合查询指示词：出现这些词时可能涉及多个工具域或需要 LLM 理解上下文，
# 不应仅凭关键词短路分类。
_COMPOUND_INDICATORS = ("和", "同时", "然后", "再", "接着", "之后", "另外", "还有",
                        "以及", "并且", "适合", "需要", "应该", "还是", "不如",
                        "顺便", "帮我还", "加上", "搭配")


def _is_compound_query(text: str) -> bool:
    """检测是否为复合/复杂查询（可能需要多个工具或 LLM 理解上下文）。

    长句（>15字）或包含复合指示词的短句不走关键词短路，交由 LLM 分类。
    """
    if len(text) > 15:
        return True
    if any(w in text for w in _COMPOUND_INDICATORS):
        return True
    return False


def _is_exclamatory_weather(text: str) -> bool:
    """如果文本包含天气类歧义词且以感叹/评价后缀结尾，判定为闲聊而非查询。"""
    if any(kw in text for kw in _AMBIGUOUS_KWS) and any(text.endswith(suf) for suf in _EXCLAMATION_SUFFIXES):
        return True
    return False


_ENTITY_PATTERNS = [
    re.compile(r'([\u4e00-\u9fa5]{2,4}(?:市|省))'),           # 城市（带后缀）
    re.compile(r'(空调|电视|灯|音响|窗帘|扫地机|冰箱|洗衣机|净化器|加湿器|投影仪|风扇|新风机)'),  # 设备
    re.compile(r'(明天|后天|今天|下周|这周|大后天|前天|昨晚|一会儿|马上)'),   # 时间
    re.compile(r'(?:放|播放|来一首|来首|点一首|点首)([\u4e00-\u9fa5]{2,5})的?(?:歌|音乐|曲)'),  # 歌手名
    re.compile(r'([\u4e00-\u9fa5]{2,6})怎么做'),                 # 菜名
]

_TIME_WORDS = ("明天", "后天", "今天", "下周", "这周", "现在", "前天", "昨晚", "一会儿", "马上")


def _extract_entities(text: str) -> str:
    """从用户文本中提取实体（城市、设备、时间词等），返回所有匹配到的实体用逗号拼接。"""
    entities = []
    for p in _ENTITY_PATTERNS:
        entities.extend(p.findall(text))
    # 去重保持顺序
    seen = set()
    unique = [e for e in entities if not (e in seen or seen.add(e))]
    return "、".join(unique) if unique else ""


def _extract_facts(text: str) -> dict:
    """从用户文本中提取会话事实，用于 working_memory.session_facts。"""
    facts = {}
    entity = _extract_entities(text)
    if entity:
        facts["last_entity"] = entity
    # 时间词：取文本中最后出现的（最接近当前指代）
    hits = [tw for tw in _TIME_WORDS if tw in text]
    if hits:
        facts["time_ref"] = hits[-1]
    return facts


def _demo_mode_active() -> bool:
    return _demo_mode_active_impl()

def _ollama_online() -> bool:
    return _ollama_online_impl()

def _llm_has_any_key() -> bool:
    from app import env_catalog as _ec
    return (_ec.is_configured("AGNES_KEY")
            or _ec.is_configured("SAGNES_KEY")
            or _ec.is_configured("STEPFUN_KEY")
            or _ec.is_configured("GLM_KEY")
            or _ec.is_configured("ARK_KEY"))


# ===== Intent cache helpers =====
_INTENT_SUFFIX_STRIP = ("怎么样", "呢", "啊", "吧", "吗", "呀", "了", "的")

def _intent_cache_key(text: str) -> str:
    """归一化 intent cache key：去标点空格、转小写、去常见语气后缀"""
    t = re.sub(r'[，。！？\s]', '', text.strip().lower())
    # 只在剩余长度足够时才去语气后缀，避免过度归一化
    for suf in _INTENT_SUFFIX_STRIP:
        if t.endswith(suf) and len(t) - len(suf) > 5:
            t = t[:-len(suf)]
            break
    return t

def _intent_cache_set(text: str, intent: str):
    key = _intent_cache_key(text)
    if len(key) <= 5:
        return  # 短文本不缓存
    with _st.intent_cache_lock:
        _st.intent_cache[key] = (intent, time.time())
        _st.intent_cache.move_to_end(key)
        if len(_st.intent_cache) > _INTENT_CACHE_MAX:
            _st.intent_cache.popitem(last=False)

def intent_classifier_status() -> Dict[str, Any]:
    now = time.time()
    with _st.intent_cache_lock:
        remaining = max(0.0, _st.intent_disabled_until - now)
        consecutive_failures = _st.intent_failures
    return {
        "circuit_open": remaining > 0,
        "remaining_seconds": round(remaining, 1),
        "consecutive_failures": consecutive_failures,
        "failure_threshold": INTENT_FAILURE_THRESHOLD,
        "cooldown_seconds": INTENT_FAILURE_COOLDOWN,
    }


def _normalize_intent(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if "amap" in raw or "map" in raw: return "amap-maps"
    elif "baize" in raw or "search" in raw: return "baize-skills"
    elif "music" in raw: return "magic-music"
    elif "remind" in raw: return "magic-reminder"
    elif "note" in raw: return "magic-notes"
    elif "system" in raw: return "magic-system"
    elif "info" in raw: return "magic-info"
    elif "life" in raw: return "magic-life"
    elif "scenes" in raw or "scene" in raw: return "magic-scenes"
    elif "evolution" in raw or "learn" in raw: return "magic-evolution"
    elif "browser" in raw: return "magic-browser"
    elif "apps" in raw: return "magic-apps"
    elif "feishu" in raw: return "magic-feishu"
    elif "douyin" in raw: return "magic-douyin"
    elif "taobao" in raw: return "magic-taobao"
    elif "recipe" in raw or "cook" in raw or "菜" in raw or "食谱" in raw: return "magic-recipe"
    elif "wardrobe" in raw or "clothes" in raw or "穿搭" in raw or "穿什么" in raw or "今天穿" in raw: return "magic-wardrobe"
    elif "preference" in raw or "偏好" in raw or "记住" in raw or "设置偏好" in raw or "magic-preferences" in raw: return "magic-preferences"
    elif "memory" in raw or "记忆" in raw or "记得" in raw: return "magic-memory"
    elif "magic" in raw:
        # 如果 raw 已经是已知的 MCP 集名，直接返回
        from app.mcp_registry import ALL_MCP
        if raw in ALL_MCP:
            return raw
        return "magic-music"
    elif "ac-control" in raw or "air" in raw or "空调" in raw or "制冷" in raw or "制热" in raw: return "ac-control"
    elif "file" in raw or "fs" in raw: return "filesystem"
    elif "vision" in raw or "mimo" in raw or "screen" in raw or "截图" in raw: return "mimo-vision"
    else: return "none"


def _classify_intent(text: str) -> str:
    """意图分类: 关键词预判 → ARK API, 返回MCP组合名"""
    now = time.time()
    _cache_key = _intent_cache_key(text)
    with _st.intent_cache_lock:
        cached = _st.intent_cache.get(_cache_key)
        if cached and now - cached[1] < _INTENT_CACHE_TTL:
            _st.intent_cache.move_to_end(_cache_key)
            cached_intent = cached[0]
            log.info(f"[intent] 缓存命中: {text[:30]} → {cached_intent}")
            return cached_intent
    # 关键词匹配（含消歧）— 即使在冷却期也执行
    matched_domains = set()
    for keywords, mcp_name in _KEYWORD_MAP:
        if any(kw in text for kw in keywords):
            matched_domains.add(mcp_name)
    # 算术前置匹配：纯算式（"25乘4"/"3加5"/"100减37"）无算式关键词时用正则补判
    if not matched_domains and re.search(r'\d+\s*[加减乘除]\s*\d+', text):
        matched_domains.add("magic-info")
    if matched_domains:
        if _is_exclamatory_weather(text):
            log.info(f"[intent] 消歧: 感叹语境，降级为 none: '{text[:30]}'")
            _intent_cache_set(text, "none")
            return "none"
        try:
            from app.audit_log import audit_log
            # 审计记实际命中的域集合（而非单个域名），
            # 否则多域命中时下游意图分析被污染
            audit_log("intent", input_data=text,
                      output_data=",".join(sorted(matched_domains)),
                      action="keyword_match", session_id="intent")
        except Exception as e:
            log.debug(f"[agent.llm] error at line 285: {e}")
            pass
        if len(matched_domains) == 1 and not _is_compound_query(text):
            result = matched_domains.pop()
            log.info(f"[intent] 关键词命中: '{text[:30]}' → {result}")
            _intent_cache_set(text, result)
            return result
        if len(matched_domains) == 1 and _is_compound_query(text):
            log.info(f"[intent] 单域命中但句子复杂，降级到 LLM 分类: '{text[:30]}'")
        # 确定性消歧: "我叫xxx"（无"什么"）→ 偏好设置；"我叫什么/名字"（有"什么"）→ 记忆查询
        if "magic-preferences" in matched_domains and "magic-memory" in matched_domains:
            if "什么" not in text and ("我叫" in text or "我名字" in text):
                log.info(f"[intent] 消歧: '我叫xxx'命名句式 → magic-preferences: '{text[:30]}'")
                _intent_cache_set(text, "magic-preferences")
                return "magic-preferences"
        # 确定性消歧: magic-tv 专属词("电视音量/静音/暂停/停止/投屏")精确度
        # 高于 ac-control 的泛"电视"，LLM 分类(无magic-tv上下文)会误选ac-control
        if "magic-tv" in matched_domains:
            log.info(f"[intent] 消歧: 电视专属词命中 → magic-tv: '{text[:30]}'")
            _intent_cache_set(text, "magic-tv")
            return "magic-tv"
        # 确定性消歧: magic-preferences 的"，记住"句式是用户存储事实的固定模式
        # 优先于 scenes(起床/早安) / reminder(提醒) 等域
        if "magic-preferences" in matched_domains and (
            "，记住" in text or ",记住" in text or "，记一下" in text or ",记一下" in text
        ):
            log.info(f"[intent] 消歧: '记住'句式 → magic-preferences: '{text[:30]}'")
            _intent_cache_set(text, "magic-preferences")
            return "magic-preferences"
        # 确定性消歧: magic-reminder 的"提醒"关键词比 baize-skills 的"查一下"更专属
        if "magic-reminder" in matched_domains and "提醒" in text:
            log.info(f"[intent] 消歧: '提醒'关键词优先 → magic-reminder: '{text[:30]}'")
            _intent_cache_set(text, "magic-reminder")
            return "magic-reminder"
        # 确定性消歧: magic-browser 的"访问"关键词比 magic-system 的泛"状态"更专属
        if "magic-browser" in matched_domains and ("访问" in text or "http" in text.lower()):
            log.info(f"[intent] 消歧: '访问/http'关键词优先 → magic-browser: '{text[:30]}'")
            _intent_cache_set(text, "magic-browser")
            return "magic-browser"
        # 确定性消歧: 偏好设置"我喜欢..."句式应优先于 reminder/notes/apps 的"记一下"
        if "magic-preferences" in matched_domains and re.search(r'我喜[欢爱]', text):
            log.info(f"[intent] 消歧: '我喜欢'优先 → magic-preferences: '{text[:30]}'")
            _intent_cache_set(text, "magic-preferences")
            return "magic-preferences"
        log.info(f"[intent] 多域关键词命中 {matched_domains}，降级到 LLM 分类: '{text[:30]}'")
        # 多个域命中 → 继续走 LLM 分类
    # disabled 检查 — 仅影响 LLM 分类路径
    with _st.intent_cache_lock:
        if now < _st.intent_disabled_until:
            log.info(f"[intent] 分类冷却中，{_st.intent_disabled_until - now:.0f}秒内默认none")
            return "none"
    prompt = (
        "任务: 判断用户输入需要哪个工具, 只回一个词\n"
        "选项: none | amap-maps | baize-skills | filesystem | magic-music | magic-reminder | magic-notes | magic-system | magic-info | magic-life | ac-control | magic-recipe | magic-preferences | magic-dev | magic-scenes | magic-evolution | magic-browser | magic-apps | magic-feishu | magic-douyin | magic-taobao | magic-wardrobe | magic-jarvis | magic-memory | magic-habits | magic-summary | mimo-vision | magic-tv\n"
        "规则:\n"
        "- none = 闲聊/问候/常识问答(你好/谢谢/讲个笑话)\n"
        "- amap-maps = 天气/地图/导航/我在哪/附近/路线/温度/几度\n"
        "- baize-skills = 搜索互联网/查资料/购物/买东西/票房\n"
        "- magic-music = 播放/歌单/音乐/随机播放\n"
        "- magic-reminder = 提醒/日程/定时器/日历\n"
        "- magic-notes = 备忘录/笔记\n"
        "- magic-system = 音量/语速/设备状态\n"
        "- magic-info = 时间/新闻/翻译/计算\n"
        "- magic-memory = 回忆/查询过去对话记住的事(我的猫叫什么/我之前说过什么/你记得吗)\n"
        "- magic-preferences = 设置/查询/删除用户偏好(我喜欢/以后/别再/我习惯/记住/我最喜欢)\n"
        "- magic-life = 外卖/充电桩/特斯拉/出门\n"
        "- magic-recipe = 做菜/菜谱/食谱/食材/吃什么/怎么做/今日推荐菜\n"
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
        "- magic-tv = 电视DLNA控制(电视音量调到X/电视静音/电视暂停/电视状态/投屏到电视)\n"
        "- magic-dev = 改代码/修改文件/编辑文件/读文件/看代码/改一下/修bug/代码修改（需 DEV_MODE_ENABLED=1）\n"
        "示例:\n"
        "  你好→none\n  今天天气怎么样→amap-maps\n  北京天气→amap-maps\n"
        "  搜一下薛之谦→baize-skills\n  帮我点杯咖啡→baize-skills\n  流浪地球票房多少→baize-skills\n"
        "  设提醒→magic-reminder\n  放歌→magic-music\n  晚安→magic-scenes\n"
        "  早上好→magic-scenes\n  看电影→magic-scenes\n  自进化→magic-evolution\n"
        "  打开百度/搜索百度→magic-browser\n  学习我→magic-evolution\n"
        "  打开空调→ac-control\n  番茄炒蛋怎么做→magic-recipe\n"
        "  今天吃什么→magic-recipe\n  音量调大→magic-system\n  现在几点→magic-info\n"
        "  我的猫叫什么→magic-memory\n  你记得我之前说过什么→magic-memory\n"
        "  我喜欢吃什么→magic-preferences\n  我最喜欢的数字是多少→magic-preferences\n"
        "  帮我算25乘4→magic-info\n  25乘以4等于多少→magic-info\n"
        f"用户输入: {text[:100]}\n"
        "工具: →")
    t0 = time.time()
    log.info(f"[intent] LLM分类开始: text='{text[:30]}'")
    try:
        _ep_base, _ep_key, _ep_model = active_chat_endpoint()
        _max_tokens = 2048
        _is_stepfun = "stepfun" in _ep_base.lower()
        _payload = {"model": _ep_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False,
                  "max_tokens": 4, "temperature": 0}
        if _is_stepfun:
            _payload["reasoning_effort"] = "low"
        # GLM_KEY / ARK_KEY 均未配置（.env 无对应条目），专用端点方案不可用。
        # 使用当前 active_chat_endpoint，收紧超时防止卡住整条流水线：
        #   (2, 10) = 连接 2s / 读取 10s。
        # Agnes 延迟波动大（0.3s~8s），5s 误杀频繁；10s 给足余量。
        # 失败时直接回退 none（不走重试），等待 INTENT_FAILURE_COOLDOWN 冷却后再尝试。
        r = _session.post(f"{_ep_base}/chat/completions",
            json=_payload,
            headers={"Authorization": f"Bearer {_ep_key}"},
            timeout=(2, 10))
        raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip().lower()
        mcp = _normalize_intent(raw)
        with _st.intent_cache_lock:
            _st.intent_failures = 0
            _st.intent_disabled_until = 0.0
        _intent_cache_set(text, mcp)
        t1 = time.time()
        log.info(f"[intent] LLM分类完成: {t1-t0:.3f}s | '{text[:30]}' → {mcp} ({raw[:15]})")
        try:
            from app.audit_log import audit_log
            audit_log("intent", input_data=text, output_data=mcp,
                      action="llm_classify", session_id="intent",
                      duration_ms=(time.time()-now)*1000)
        except Exception as e:
            log.debug(f"[agent.llm] error at line 369: {e}")
            pass
        return mcp
    except Exception as e:
        with _st.intent_cache_lock:
            _st.intent_failures += 1
            if _st.intent_failures >= INTENT_FAILURE_THRESHOLD:
                _st.intent_disabled_until = now + INTENT_FAILURE_COOLDOWN
                log.warning(f"[intent] 连续失败{_st.intent_failures}次，暂停本地分类{INTENT_FAILURE_COOLDOWN:g}秒")
        log.warning(f"[intent] 本地分类失败,默认none: text='{text[:20]}' err={e}")
        return "none"


# ===== OpenAI compat wrapper =====
def _wrap_openai_create_unknown_kwargs(create_fn):
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


_MCP_TIMEOUT_PATCH_INSTALLED = False
_MCP_TIMEOUT_LOCK = threading.Lock()
# Separate lock for serializing brain construction to prevent concurrent MCPManager
# init_config_async() calls (which spawn stdio subprocesses) from racing on the
# same singleton event loop. DO NOT use _st.brain_lock here — that lock is used
# for brain_status/failure recording and would cause deadlocks.
_brain_build_lock = threading.Lock()


def _install_mcp_timeout_patch(timeout: float = 30.0) -> None:
    """给 qwen_agent MCP 工具调用加超时，防止 MCP server 卡死导致 brain 永久阻塞。

    包装 MCPManager.create_tool_class 生成的 ToolClass.call，使 future.result()
    在指定秒数后抛 TimeoutError，从而让 brain.run 异常退出 → _record_brain_failure
    计数增加 → watchdog/pool 重建机制可检测并接管。

    - 幂等：多次安装安全（通过 _MCP_TIMEOUT_PATCH_INSTALLED 标记）
    - 安全降级：qwen_agent 升级导致结构变化时，记录警告日志但服务继续运行
    """
    global _MCP_TIMEOUT_PATCH_INSTALLED
    if _MCP_TIMEOUT_PATCH_INSTALLED:
        return
    with _MCP_TIMEOUT_LOCK:
        if _MCP_TIMEOUT_PATCH_INSTALLED:
            return
        try:
            from qwen_agent.tools import mcp_manager as _mm
            # 保存原始工厂方法（只 patch 一次）
            _orig_factory = _mm.MCPManager.create_tool_class

            def _patched_factory(self, register_name, register_client_id, tool_name,
                                 tool_desc, tool_parameters):
                original_tool_class = _orig_factory(
                    self, register_name, register_client_id, tool_name,
                    tool_desc, tool_parameters)

                def _call_with_timeout(self, params, **kwargs):
                    import concurrent.futures as _cf
                    tool_args = json.loads(params)
                    manager = _mm.MCPManager()
                    client = manager.clients[self.client_id]
                    future = asyncio.run_coroutine_threadsafe(
                        client.execute_function(tool_name, tool_args), manager.loop)
                    try:
                        result = future.result(timeout=timeout)
                    except _cf.TimeoutError as e:
                        future.cancel()
                        # 包装成带 timeout 关键字的异常：_record_brain_failure 的
                        # 错误特征匹配依赖 message 内容触发 provider 轮换与失败计数
                        raise TimeoutError(
                            f"MCP tool '{tool_name}' timeout after {timeout:.0f}s") from e
                    except Exception as e:
                        log.info(f'[mcp-patch] MCP工具调用失败: {e}')
                        raise e
                    # qwen_agent 重连失败时返回字符串而非抛异常，导致内部错误文本泄露给用户
                    if isinstance(result, str) and 'reconnect' in result.lower() and 'exception' in result.lower():
                        log.warning(f"[mcp-patch] MCP重连失败({tool_name}): {result[:100]}")
                        raise RuntimeError(f"MCP工具 '{tool_name}' 暂时不可用，请稍后重试")
                    return result

                # 绑定为实例方法，保证 self 是 ToolClass 实例
                import types
                original_tool_class.call = types.MethodType(
                    _call_with_timeout, original_tool_class)
                return original_tool_class

            _mm.MCPManager.create_tool_class = _patched_factory
            _MCP_TIMEOUT_PATCH_INSTALLED = True
            log.info(f"[mcp-patch] 已安装 MCP 工具调用超时({timeout:.0f}s)")
        except Exception as e:
            log.warning(f"[mcp-patch] 安装失败(服务继续运行): {e}")


def _install_mcp_fault_tolerance_patch() -> None:
    """给 MCPManager.init_config_async 加容错：单 server 失败不阻断其余 server。

    qwen_agent 原实现循环 for server_name in mcp_servers: await client.connection_server(...)
    若任一 server 连接失败则整个循环 abort，所有 MCP 工具不可用。
    此 patch 把每次 connection_server 包装在 try/except 中，失败则记录警告并继续。
    """
    try:
        from qwen_agent.tools import mcp_manager as _mm
        if getattr(_mm.MCPManager, '_charlie_fault_tolerance_patched', False):
            return
        _orig_init = _mm.MCPManager.init_config_async

        async def _fault_tolerant_init(self, config):
            tools = []
            mcp_servers = config.get('mcpServers', {})
            for server_name in mcp_servers:
                client = _mm.MCPClient()
                server = mcp_servers[server_name]
                try:
                    await client.connection_server(
                        mcp_server_name=server_name, mcp_server=server)
                except Exception as _conn_err:
                    err_str = str(_conn_err)
                    # 预热阶段常见子进程退出噪音：MCP server 进程退出但连接已关闭，
                    # 实际不影响运行（真实请求时 MCP 会正常初始化），降级到 debug 避免刷屏。
                    if "Connection closed" in err_str or "EOF" in err_str or "pipe" in err_str.lower():
                        log.debug(f"[mcp-patch] MCP server '{server_name}' 连接关闭(预热常见)，跳过")
                    else:
                        log.warning(
                            f"[mcp-patch] MCP server '{server_name}' 连接失败，跳过: {_conn_err}")
                    continue
                client_id = server_name + '_' + str(__import__('uuid').uuid4())
                client.client_id = client_id
                self.clients[client_id] = client
                for tool in client.tools:
                    parameters = tool.inputSchema
                    if 'required' not in parameters:
                        parameters['required'] = []
                    required_fields = {'type', 'properties', 'required'}
                    missing_fields = required_fields - parameters.keys()
                    if missing_fields:
                        log.warning(
                            f"[mcp-patch] MCP tool '{server_name}/{tool.name}' "
                            f"schema missing fields {missing_fields}，跳过")
                        continue
                    cleaned_parameters = {
                        'type': parameters['type'],
                        'properties': parameters['properties'],
                        'required': parameters['required'],
                    }
                    register_name = server_name + '-' + tool.name
                    agent_tool = self.create_tool_class(
                        register_name=register_name,
                        register_client_id=client_id,
                        tool_name=tool.name,
                        tool_desc=tool.description,
                        tool_parameters=cleaned_parameters,
                    )
                    tools.append(agent_tool)
                if client.resources:
                    list_resources_tool_name = server_name + '-list_resources'
                    list_resources_params = {'type': 'object', 'properties': {}, 'required': []}
                    list_resources_agent_tool = self.create_tool_class(
                        register_name=list_resources_tool_name,
                        register_client_id=client_id,
                        tool_name='list_resources',
                        tool_desc='Servers expose a list of resources.',
                        tool_parameters=list_resources_params,
                    )
                    tools.append(list_resources_agent_tool)
                    try:
                        list_resource_templates = await client.session.list_resource_templates()
                        if list_resource_templates.resourceTemplates:
                            resources_template_str = '\n'.join(
                                str(t) for t in list_resource_templates.resourceTemplates)
                            read_resource_tool_name = server_name + '-read_resource'
                            read_resource_params = {
                                'type': 'object',
                                'properties': {
                                    'uri': {
                                        'type': 'string',
                                        'description': 'The URI identifying the specific resource to access'
                                    }
                                },
                                'required': ['uri']
                            }
                            read_resource_agent_tool = self.create_tool_class(
                                register_name=read_resource_tool_name,
                                register_client_id=client_id,
                                tool_name='read_resource',
                                tool_desc='Request to access a resource provided by a connected MCP server. '
                                          'Resources represent data sources that can be used as context.\n'
                                          f'Resource Templates:\n{resources_template_str}',
                                tool_parameters=read_resource_params,
                            )
                            tools.append(read_resource_agent_tool)
                    except Exception:
                        pass
            self.tools = tools
            return tools

        _mm.MCPManager.init_config_async = _fault_tolerant_init
        _mm.MCPManager._charlie_fault_tolerance_patched = True
        log.info("[mcp-patch] 已安装 MCP 单 server 失败容错")
    except Exception as e:
        log.warning(f"[mcp-patch] 容错 patch 安装失败(服务继续运行): {e}")


# ===== Brain build / cache =====
_BRAIN_CACHE_MAX = int(os.getenv("BRAIN_CACHE_MAX", "12"))
_BRAIN_RUN_TIMEOUT = float(os.getenv("BRAIN_RUN_TIMEOUT", "35"))
_BRAIN_BUILD_TIMEOUT = float(os.getenv("BRAIN_BUILD_TIMEOUT", "60"))

# 多步 Agent: 语音场景工具调用步数上限（防止长时间静默）
_MAX_TOOL_STEPS = int(os.getenv("MAX_TOOL_STEPS", "5"))

# 工具名 → 中文进度提示
_TOOL_PROGRESS_LABELS = {
    "get_current_time": "查时间",
    "get_detailed_weather": "查天气",
    "add_reminder": "设提醒",
    "search_music": "搜音乐",
    "play_music": "播放音乐",
    "search_recipe": "查菜谱",
    "get_news": "查新闻",
    "translate": "翻译",
    "calculate": "计算",
    "search_web": "搜索网页",
    "browse_page": "打开网页",
    "get_calendar_today": "查日程",
    "outfit_recommend": "查穿搭",
    "get_stock": "查股票",
    "get_air_quality": "查空气质量",
}


def _count_function_msgs(rsp: Any) -> int:
    """统计 rsp 中 function 角色消息数量。"""
    if not isinstance(rsp, list):
        return 0
    return sum(1 for m in rsp if isinstance(m, dict) and m.get("role") == "function")


def _tool_progress_text(tool_name: str) -> str:
    """生成工具调用进度提示文本。"""
    label = _TOOL_PROGRESS_LABELS.get(tool_name)
    if label:
        return f"正在{label}..."
    return f"正在处理..."


def _build_brain(mcp_set="all", session_id="default"):
    """构建大脑，带超时保护防止 MCP server 启动卡死导致永久阻塞。

    Assistant() 构造函数内部调用 MCPManager().initConfig() → future.result() 无超时，
    若某个 MCP server 启动卡死会永久阻塞 brain 构建 → 线程池 worker 耗尽。
    此函数用独立线程 + join(timeout) 包装，超时抛 TimeoutError。
    """
    _result: list = []
    _error: list = []

    def _do_build():
        try:
            _result.append(_build_brain_inner(mcp_set, session_id=session_id))
        except Exception as e:
            _error.append(e)

    _t = threading.Thread(target=_do_build, daemon=True)
    _t.start()
    _t.join(timeout=_BRAIN_BUILD_TIMEOUT)
    if _t.is_alive():
        log.error(f"[brain] 构建超时({_BRAIN_BUILD_TIMEOUT}s)，MCP server 可能卡死")
        raise TimeoutError(f"brain build timeout {_BRAIN_BUILD_TIMEOUT}s")
    if _error:
        raise _error[0]
    if not _result:
        raise RuntimeError("brain build failed without error")
    return _result[0]


def _build_brain_inner(mcp_set="all", session_id="default"):
    try:
        import psutil
        mem = psutil.virtual_memory()
        avail_gb = mem.available / 1073741824
        if avail_gb < 0.3:
            log.error(f"[brain] 内存不足(可用 {avail_gb:.1f}GB / {mem.percent}%占用)，拒绝构建大脑防OOM")
            raise RuntimeError(f"内存不足(可用 {avail_gb:.1f}GB / {mem.percent}%占用), 拒绝构建大脑防OOM")
        log.info(f"[brain] 内存检查通过: {mem.percent}% ({avail_gb:.1f}GB可用)")
    except ImportError:
        log.debug("[brain] psutil 不可用，跳过内存检查")
    from qwen_agent.agents import Assistant
    from app.llm_config import resolve as resolve_llm
    llm_cfg = resolve_llm()

    # 进程内工具模式：直接 import 工具函数，不走 stdio 子进程
    # 环境变量 MCP_INPROC=0 可回退到原 stdio 模式
    _use_inproc = os.getenv("MCP_INPROC", "1") != "0"

    if _use_inproc:
        try:
            from agent.inproc_tools import resolve_inproc_tools
            tools = resolve_inproc_tools(mcp_set)
            log.info(f"[brain] 构建大脑(inproc) mcp_set={mcp_set}, {len(tools)}个工具")
            brain = Assistant(llm=llm_cfg, name='Charlie',
                system_message=_build_system_msg(mcp_set, session_id=session_id),
                function_list=tools)
            _install_openai_compat(brain)
            return brain
        except Exception as e:
            log.warning(f"[brain] 进程内工具加载失败，回退stdio: {e}")
            # 回退到 stdio 模式

    # 原 stdio MCP 模式（回退路径）
    from app.mcp_registry import resolve as resolve_mcps
    mcp_servers = resolve_mcps(mcp_set)
    # magic-preferences 始终加载（偏好记忆对闲聊也有价值且不触发主动工具调用）
    _mcp_registry = __import__("app.mcp_registry", fromlist=["ALL_MCP"])
    if "magic-preferences" in _mcp_registry.ALL_MCP:
        mcp_servers.setdefault("magic-preferences", _mcp_registry.ALL_MCP["magic-preferences"])
    # magic-jarvis 仅在非 none brain 时注入（避免闲聊 brain 带 11 个工具导致 LLM 主动调用 get_air_quality 等，造成 10-40s 延迟）
    if mcp_set != "none" and "magic-jarvis" in _mcp_registry.ALL_MCP:
        mcp_servers.setdefault("magic-jarvis", _mcp_registry.ALL_MCP["magic-jarvis"])
    log.info(f"[brain] 构建大脑(stdio) mcp_set={mcp_set}, 启用{len(mcp_servers)}个MCP: {list(mcp_servers.keys())}")
    tools = [{"mcpServers": mcp_servers}] if mcp_servers else []
    brain = Assistant(llm=llm_cfg, name='Charlie',
        system_message=_build_system_msg(mcp_set, session_id=session_id),
        function_list=tools)
    _install_openai_compat(brain)
    _install_mcp_timeout_patch()
    _install_mcp_fault_tolerance_patch()
    return brain


def _brain_run_iter(brain_instance, messages, total_timeout: float = None, cancel_event=None):
    """包装 brain.run() 生成器，用独立线程+队列实现 per-iteration 超时保护。

    防止 brain.run 的 next() 永久阻塞导致线程池 worker 耗尽。
    超时后主线程抛 TimeoutError，孤儿线程自然结束（Python 无法 kill 线程）。

    cancel_event: threading.Event，set 时 worker 尽快退出并投递结束标记。
    """
    if total_timeout is None:
        total_timeout = _BRAIN_RUN_TIMEOUT
    _q: queue.Queue = queue.Queue()
    _start = time.time()

    def _worker():
        try:
            _ensure_event_loop()
            for rsp in brain_instance.run(messages):
                if cancel_event is not None and cancel_event.is_set():
                    break
                _q.put(("item", rsp))
        except Exception as e:
            _q.put(("error", e))
        finally:
            _q.put(("done", None))

    _t = threading.Thread(target=_worker, daemon=True)
    _t.start()

    while True:
        remaining = total_timeout - (time.time() - _start)
        if remaining <= 0:
            raise TimeoutError(f"brain.run total timeout {total_timeout}s")
        try:
            kind, val = _q.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError(f"brain.run total timeout {total_timeout}s")
        if kind == "item":
            yield val
        elif kind == "error":
            raise val
        elif kind == "done":
            break

def _get_brain(mcp_set="none", session_id="default"):
    # 快路径：无锁缓存命中（dict.get 在 GIL 下原子安全）
    brain = _st.brains.get(mcp_set)
    if brain is None:
        # 慢路径：加锁双重检查
        with _st.brain_lock:
            brain = _st.brains.get(mcp_set)
            if brain is None:
                # 淘汰在锁内（快），构建在锁外
                while len(_st.brains) >= _BRAIN_CACHE_MAX:
                    oldest_key = next(iter(_st.brains))
                    old_brain = _st.brains.pop(oldest_key)
                    _cleanup_brain_processes(old_brain)
                    log.info(f"[brain] 缓存淘汰: mcp={oldest_key}, 当前缓存={len(_st.brains)}")
        if brain is None:
            # 锁外构建，但用 _brain_build_lock 序列化（防止并发 MCPManager init_config_async 竞争）
            with _brain_build_lock:
                # 双重检查：等待锁期间其他线程可能已完成构建并缓存
                brain = _st.brains.get(mcp_set)
                if brain is None:
                    new_brain = _build_brain(mcp_set, session_id=session_id)
                    with _st.brain_lock:
                        brain = _st.brains.get(mcp_set)
                        if brain is None:
                            _st.brains[mcp_set] = new_brain
                            if not _st.brain_build_time:
                                _st.brain_build_time = time.time()
                            brain = new_brain
                            log.info(f"[brain] 大脑构建完成: mcp={mcp_set}, 缓存总数={len(_st.brains)}")
    # system_message 刷新在锁外（cache miss 时构建耗时 50-200ms，
    # 持锁会阻塞失败计数/watchdog；实例属性赋值 GIL 下原子安全）
    brain.system_message = _build_system_msg(mcp_set, session_id=session_id)
    return brain

_thread_loop_local = threading.local()

def _ensure_event_loop():
    """确保当前线程有可用 event loop。

    关键：按线程复用已创建的 loop（threading.local），不能每次都 new——
    旧实现 get_running_loop() 在 worker 线程必然失败，每次调用都创建新 loop，
    泄漏一个 kqueue/epoll FD，高负载下耗尽 FD 上限导致服务假死。
    """
    loop = getattr(_thread_loop_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_loop_local.loop = loop
    return loop

def _fmt_exc(e: BaseException) -> str:
    """异常诊断字符串：str(e) 为空时至少保留异常类名，多行错误压缩为单行。"""
    s = str(e).strip()
    if not s:
        return type(e).__name__
    # Collapse multiline errors (e.g. "Error code: 400.\nError message: ...") to
    # a single line so log entries don't span multiple lines with empty first lines.
    s = " ".join(s.split())
    return f"{type(e).__name__}: {s}"


def _record_brain_failure(error: str = ""):
    is_429 = _is_429_error(error)
    is_conn = _is_conn_error(error)
    with _st.brain_lock:
        _st.brain_failures += 1
        _st.brain_total_failures += 1
        _st.brain_last_failure = time.time()
        log.error(f"[brain] 失败#{_st.brain_failures}(累计{_st.brain_total_failures}): {error}" +
                  (" [429限流, 不清缓存]" if is_429 else ""))
        if _st.brain_failures >= _MAX_BRAIN_FAILURES:
            log.warning(f"[brain] 连续失败{_st.brain_failures}次({'429' if is_429 else '非429'}), 清除所有缓存大脑...")
            for k, b in list(_st.brains.items()):
                _cleanup_brain_processes(b)
            _st.brains.clear()
            _st.brain_failures = 0
        elif is_429 and _st.brain_failures >= 10 and active_is_glm():
            # 仅 GLM provider 的连续 429 才清空 brains，其他 provider 保留轮换逻辑
            log.warning(f"[brain] 429连续{_st.brain_failures}次 (GLM), 清除缓存大脑...")
            for k, b in list(_st.brains.items()):
                _cleanup_brain_processes(b)
            _st.brains.clear()
            _st.brain_failures = 0
    # provider 轮换在 brain_lock 之外执行，避免与 _try_rotate_provider_on_failure 内部
    # 获取 brain_lock 形成不可重入死锁
    if is_conn:
        s = error.lower()
        if not (s.startswith("mcp tool") or "mcp tool '" in s):
            _note_provider_failure()
            _try_rotate_provider_on_failure()

def _record_brain_success():
    _reset_provider_failures()
    with _st.brain_lock:
        _st.brain_failures = 0
        _st.brain_last_success = time.time()

def _cleanup_brain_processes(brain_instance):
    """清理 MCP 子进程：淘汰时终止孤儿进程，保留活跃 client 的进程。

    策略：
    1. brain_instance 为 None（brain 构建失败路径）或该 brain 自身的 MCP client
       已不在 MCPManager.clients 中时，扫描 MCPManager().processes，
       对没有对应 client 的进程调用 terminate()。
    2. 不终止仍有 client 在服务的进程（避免误杀其他 brain 的 MCP 子进程）。
    """
    try:
        from qwen_agent.tools import mcp_manager as _mm
        manager = _mm.MCPManager()
        if not hasattr(manager, 'processes') or not hasattr(manager, 'clients'):
            return
        clients = manager.clients or {}
        for pid, proc in list(manager.processes.items()):
            try:
                # 该进程没有对应 client → 孤儿进程（连接失败后遗留）
                if pid not in clients:
                    if proc.poll() is None:
                        log.info(f"[brain] 终止孤儿 MCP 进程 pid={pid}")
                        proc.terminate()
            except (ProcessLookupError, OSError):
                pass  # 进程已自行退出
    except Exception as e:
        log.debug(f"[brain] 清理 MCP 进程失败: {e}")

def restart_brain() -> str:
    with _st.brain_lock:
        for k, b in list(_st.brains.items()):
            _cleanup_brain_processes(b)
        _st.brains.clear()
        _st.brain_failures = 0
    log.info("[brain] 手动重启, 所有缓存大脑已清除")
    # 立即预热默认大脑：否则 ready=len(brains)>0 在下一个需要 MCP 的请求前
    # 一直为 False，健康检查/watchdog 会误判服务不可用（lite 通道不重建缓存）
    try:
        _get_brain("none")
        msg = "大脑已重启并预热完成"
    except Exception as e:
        log.warning(f"[brain] 重启预热失败(请求时将重建): {e}")
        msg = "大脑重启中, 下次请求将自动重建"
    return msg

def reload_brain_config() -> str:
    try:
        from app import llm_config as _llm_cfg
        _llm_cfg.reload()
    except Exception as e:
        log.warning(f"[brain] llm_config.reload 失败: {e}")
    with _st.brain_lock:
        for k, b in list(_st.brains.items()):
            _cleanup_brain_processes(b)
        _st.brains.clear()
        _st.brain_failures = 0
    with _provider_rotate_lock:
        _provider_fail_counts.clear()
    log.info("[brain] 配置已热重载, 缓存大脑已清除, 下次请求用新配置重建")
    return "配置已生效，大脑将在下次对话时使用新配置重建"

def set_current_user(user_id: str):
    _st.current_user_id = user_id or "default"
    os.environ["CHARLIE_USER_ID"] = _st.current_user_id
    # 切换用户后清响应缓存并失效上下文缓存，避免跨用户串台
    try:
        from agent import cache as _cache_mod
        with _cache_mod._cache_lock:
            _cache_mod._cache.clear()
    except Exception as e:
        log.debug(f"[user] 清响应缓存失败: {e}")
    try:
        invalidate_context_cache()
    except Exception as e:
        log.debug(f"[user] 失效上下文缓存失败: {e}")
    # 同步更新 agent.history 的 HISTORY_FILE / HISTORY_LOCK_FILE，
    # 避免 env 切换后静态导出与动态 _get_history_file() 分叉
    try:
        from agent import history as _hist_mod
        _hist_mod.HISTORY_FILE = _hist_mod._get_history_file()
        _hist_mod.HISTORY_LOCK_FILE = _hist_mod.HISTORY_FILE + ".lock"
    except Exception as e:
        log.debug(f"[user] 更新 HISTORY_FILE 失败: {e}")
    log.info(f"[user] 切换到用户: {_st.current_user_id}")

def get_current_user() -> str:
    return _st.current_user_id

def brain_status() -> dict:
    with _st.brain_lock:
        result = {
            "ready": len(_st.brains) > 0,
            "cached_brains": list(_st.brains.keys()),
            "consecutive_failures": _st.brain_failures,
            "total_failures": _st.brain_total_failures,
            "max_failures_before_rebuild": _MAX_BRAIN_FAILURES,
            "last_success": datetime.datetime.fromtimestamp(_st.brain_last_success).isoformat() if _st.brain_last_success else None,
            "last_failure": datetime.datetime.fromtimestamp(_st.brain_last_failure).isoformat() if _st.brain_last_failure else None,
            "uptime_since": datetime.datetime.fromtimestamp(_st.brain_build_time).isoformat() if _st.brain_build_time else None,
        }
    return result


_PROVIDER_ROTATE_THRESHOLD = 2  # 同一 provider 连续失败达到此次数才轮换，防偶发 429 抖动
_provider_rotate_lock = threading.Lock()
_provider_fail_counts: dict[str, int] = {}


def _current_provider_name() -> str:
    try:
        from app import llm_config as _llm_cfg
        cur_base, _, _ = _llm_cfg.active_chat_endpoint()
        for name in _llm_cfg._get_priority_list():
            if _llm_cfg._get_provider_cfg(name)["base"] == cur_base:
                return name
    except Exception as e:
        log.debug(f"[agent.llm] error at line 719: {e}")
        pass
    return ""


def _note_provider_failure() -> None:
    name = _current_provider_name()
    if not name:
        return
    with _provider_rotate_lock:
        _provider_fail_counts[name] = _provider_fail_counts.get(name, 0) + 1


def _reset_provider_failures() -> None:
    name = _current_provider_name()
    if not name:
        return
    with _provider_rotate_lock:
        _provider_fail_counts.pop(name, None)


def _try_rotate_provider_on_failure() -> bool:
    """Streaming 失败后，尝试跳过当前 provider 用 LLM_PRIORITY 中下一个已配置的 provider。

    仅当同一 provider 连续失败 >= _PROVIDER_ROTATE_THRESHOLD 次才轮换（防偶发 429 抖动）。
    成功调用会经 _reset_provider_failures 清零计数。
    返回 True 表示已轮换（修改了 LLM_PRIORITY env），调用方应重建 brain 重试。
    """
    try:
        from app import llm_config as _llm_cfg
        _need_clear_brains = False
        _rotated_to = ""
        with _provider_rotate_lock:
            priority = _llm_cfg._get_priority_list()
            if not priority:
                return False
            cur_base, _, _ = _llm_cfg.active_chat_endpoint()
            if not cur_base:
                return False
            cur_name = None
            for name in priority:
                cfg = _llm_cfg._get_provider_cfg(name)
                if cfg["base"] == cur_base:
                    cur_name = name
                    break
            if not cur_name:
                return False
            if _provider_fail_counts.get(cur_name, 0) < _PROVIDER_ROTATE_THRESHOLD:
                log.info(f"[brain] provider {cur_name} 连续失败 "
                         f"{_provider_fail_counts.get(cur_name, 0)}/"
                         f"{_PROVIDER_ROTATE_THRESHOLD}，未达轮换阈值")
                return False
            idx = priority.index(cur_name)
            # 环形查找：当前 provider 是最后一个时也要能回绕到前面已配置的
            for offset in range(1, len(priority) + 1):
                next_name = priority[(idx + offset) % len(priority)]
                if next_name == cur_name:
                    break
                if _llm_cfg._is_provider_configured(next_name):
                    new_priority = priority[idx + 1:] + priority[:idx + 1]
                    os.environ["LLM_PRIORITY"] = ",".join(new_priority)
                    _llm_cfg.LLM_PRIORITY = new_priority
                    _provider_fail_counts.pop(cur_name, None)
                    _need_clear_brains = True
                    _rotated_to = next_name
                    break
        # brain_lock 在 _provider_rotate_lock 释放后获取，避免嵌套锁
        if _need_clear_brains:
            with _st.brain_lock:
                _st.brains.clear()
            log.warning(f"[brain] provider轮换: {cur_name} → {_rotated_to} "
                        f"(连续失败{_PROVIDER_ROTATE_THRESHOLD}次)")
            return True
    except Exception as e:
        log.debug(f"[brain] provider轮换异常: {e}")
    return False


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
        from app import load_magic_module
        _mem = load_magic_module("magic_memory", "magic-memory.py")
        if _mem:
            _mem.remember_conversation(user_text, assistant_reply)
    except Exception as e:
        log.warning(f"[memory] 记忆提取失败: {e}")


def _recall_memory_prompt(text: str) -> str | None:
    """从 episodic 记忆召回与当前输入相关的长期记忆，生成 system 提示。

    无记忆/召回为空返回 None。轻量异常兜底，绝不阻塞主链路。
    """
    try:
        from app import load_magic_module
        _mem = load_magic_module("magic_memory", "magic-memory.py")
        if not _mem:
            return None
        mems = _mem.get_relevant_memories(text, limit=3)
        if not mems:
            return None
        lines = []
        for m in mems:
            dt_str = m.get("datetime", "")
            info = m.get("summary") or m.get("user_text", "")
            lines.append(f"- [{dt_str}] {info}")
        return "以下是过去对话中记住的长期记忆，若与当前问题相关可参考：\n" + "\n".join(lines)
    except Exception as e:
        log.debug(f"[memory] 记忆召回失败: {e}")
        return None


def _build_wm_anaphor_prompt() -> str | None:
    """从 working_memory 提取指代消解信息，生成 LLM 可理解的上下文提示。

    当对话轮数 > 1 且意图栈非空时，向 messages 注入一个 system 消息，
    帮助 LLM 理解"它/这个/那个"等指代词指向的上文实体。
    """
    try:
        wm = get_working_memory()
    except Exception as e:
        log.debug(f"[agent.llm] error at line 847: {e}")
        return None
    turn_count = wm.get("turn_count", 0)
    intent_stack = wm.get("intent_stack", [])
    last_entity = wm.get("last_entity", "")
    last_topic = wm.get("last_topic", "")
    session_facts = wm.get("session_facts", {})
    if turn_count <= 1 and not intent_stack and not session_facts:
        return None
    parts = []
    if intent_stack:
        recent_intents = " → ".join(intent_stack[-3:])
        parts.append(f"本轮对话已涉及：{recent_intents}")
    if last_entity:
        parts.append(f"最后提到的实体：{last_entity}")
    if last_topic:
        parts.append(f"当前话题：{last_topic}")
    if session_facts:
        facts_str = "；".join(f"{k}={v}" for k, v in list(session_facts.items())[:3])
        parts.append(f"已确认事实：{facts_str}")
    if not parts:
        return None
    return "【指代消解上下文】" + "。".join(parts) + "。用户说'它''这个''那个''他''她'时，请参考以上上下文理解指代对象。"


# ===== LLM path (non-streaming) =====
def _brain_llm(text: str, session_id: str = "default") -> str:
    try:
        from agent.persona import update as _persona_update
        _persona_update(text)
    except Exception as e:
        log.debug(f"[agent.llm] error at line 877: {e}")
        pass
    try:
        from agent.persona import update_relationship_from_interaction as _update_rel
        _update_rel(text)
    except Exception as e:
        log.debug(f"[agent.llm] error at line 882: {e}")
        pass
    # 情感 TTS：根据用户情绪设置当前轮 TTS 情感（线程级）
    try:
        from agent.persona import get_current_emotion as _get_emotion
        from agent.asr_tts import set_current_emotion as _set_emotion
        _set_emotion(_get_emotion())
    except Exception:
        pass
    mcp_set = _classify_intent(text)
    # 会话切换时切换工作记忆槽位并清除上下文缓存（thread-local，跨通道并发安全）
    if session_id != _get_last_wm_session():
        wm_switch_session(session_id)
        invalidate_context_cache()
        _set_last_wm_session(session_id)
    update_working_memory(intent=mcp_set, topic=text[:20], entity=_extract_entities(text), facts=_extract_facts(text))
    if _demo_mode_active() and not (
        os.getenv("OLLAMA_ENABLED", "0") == "1" and _ollama_online()
    ):
        _port = 8000
        try:
            from app.config import http_port as _hp
            _port = _hp()
        except Exception as e:
            log.debug(f"[brain_llm] http_port() 不可用，使用默认8000: {e}")
        reply = (f"我还没配置 AI 大脑。注册智谱 GLM 免费 Key 即可解锁完整能力（注册即送，永久免费）：\n"
                 f"  打开 http://localhost:{_port}/welcome 按引导操作")
        _append_history(_get_history(session_id), text, reply)
        return reply
    # 闲聊快速通道：无工具直连流式，避免 fncall 大 payload 拖慢首包
    if mcp_set == "none" and not is_garbled_asr(text):
        try:
            _lite_reply = "".join(s for s, _ in _chat_lite_stream(text, session_id))
            if _lite_reply.strip():
                return _lite_reply
            log.info("[lite] 快速通道无产出，回退完整大脑")
        except Exception as e:
            log.warning(f"[lite] 快速通道异常，回退完整大脑: {e}")
    _ensure_event_loop()
    from app import llm_config as _llm_cfg
    _glm_tries = len(_llm_cfg.get_glm_models() or [None]) if _llm_cfg.is_glm_configured() and not _llm_cfg.is_ark_configured() else 1
    final = None
    for _attempt in range(_glm_tries):
        try:
            brain_instance = _get_brain(mcp_set)
        except Exception as e:
            _record_brain_failure(_fmt_exc(e)[:400])
            log.error(f"[brain] 构建失败: {e}", exc_info=True)
            if not _llm_has_any_key():
                return ("我还没配置 AI 大脑。注册智谱 GLM 免费 Key 即可解锁完整能力"
                        "（注册即送，永久免费）：打开 /welcome 按引导操作")
            return "大脑启动失败，请稍后重试。"
        hist = _get_history(session_id)
        messages = [{'role': m['role'], 'content': m['content']} for m in hist] + [{'role': 'user', 'content': text}]
        _wm_prompt = _build_wm_anaphor_prompt()
        _mem_prompt = _recall_memory_prompt(text)
        _sys_combined = "\n\n".join(p for p in (_wm_prompt, _mem_prompt) if p)
        if _sys_combined:
            messages.insert(0, {'role': 'system', 'content': _sys_combined})
        _brain_start = time.time()
        _last_fn_count = 0
        _tool_steps = 0
        try:
            for rsp in _brain_run_iter(brain_instance, messages, _BRAIN_RUN_TIMEOUT):
                final = rsp
                if isinstance(rsp, list):
                    for m in rsp:
                        if isinstance(m, dict) and m.get('role') == 'function' and isinstance(m.get('content'), str) and m['content'].startswith('__MUSIC__'):
                            break
                    else:
                        # 多步 Agent：步数上限检查
                        _fn_count = _count_function_msgs(rsp)
                        if _fn_count > _last_fn_count:
                            _last_fn_count = _fn_count
                            _tool_steps += 1
                            if _tool_steps > _MAX_TOOL_STEPS:
                                log.warning(f"[brain] 非流式工具步数达上限 {_MAX_TOOL_STEPS}，停止")
                                break
                        continue
                    break
            _record_brain_success()
            break
        except Exception as e:
            _err = _fmt_exc(e)
            is_429 = (_RateLimitError is not None and isinstance(e, _RateLimitError)) or \
                     '429' in _err or 'Too Many' in _err or '1305' in _err or \
                     'rate' in _err.lower() or '频率' in _err
            # GLM 轮换只对 GLM 端点有意义：stepfun/agnes 等 429 时
            # 轮换 GLM 模型是空操作，清空全部 brains 反而引发全量重建
            if is_429 and _llm_cfg.active_is_glm() and _attempt < _glm_tries - 1:
                time.sleep(1)
                _new_model = _llm_cfg.rotate_glm_model()
                log.warning(f"[brain] GLM 429 限流，轮换到 {_new_model} 重试（第{_attempt+2}个模型）")
                with _st.brain_lock:
                    for _k, _b in list(_st.brains.items()):
                        _cleanup_brain_processes(_b)
                    _st.brains.clear()
                    _st.brain_failures = 0
                continue
            _record_brain_failure(_err[:60])
            # 超时/连接错误时清掉当前 brain 缓存，避免下次复用状态不一致的
            # 实例再次超时（与 brain_stream_sentences 的处理保持一致）
            if 'timeout' in _err.lower() or _is_conn_error(_err):
                with _st.brain_lock:
                    _st.brains.pop(mcp_set, None)
                log.warning("[brain] 超时/连接错误后清除 brain 缓存: mcp=%s", mcp_set)
            return "抱歉，我现在有点忙不过来，请稍等一下再试。"
    else:
        return "抱歉，我现在有点忙不过来，请稍等一下再试。"
    reply = _extract_assistant_text(final) if final and isinstance(final, list) else "我没听明白"
    if not reply:
        reply = "我没听明白"
    if reply == "我没听明白" and final and isinstance(final, list):
        has_tool_call = any(m.get('function_call') for m in final if isinstance(m, dict))
        has_music = any('__MUSIC__' in str(m.get('content', '')) for m in final if isinstance(m, dict))
        if has_music:
            reply = "__MUSIC_PLAYING__"
        elif has_tool_call:
            reply = "好的，已执行。"
    _append_history(hist, text, reply)
    _cache_set(text, reply, session_id=session_id)
    invalidate_context_cache()
    try:
        import threading as _mem_thread
        _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
    except Exception as e:
        log.debug(f"[brain_llm] 记忆提取线程启动失败: {e}")
    return reply


# ===== Streaming brain =====
_FILLER_WORDS = {
    "让我想想", "稍等一下", "稍等", "让我思考一下", "想一想",
    "让我看看", "我看看", "好的", "好", "嗯嗯", "嗯",
    "让我想一下", "我想想", "等一下", "稍等下",
}


def _is_filler_word(s: str) -> bool:
    cleaned = re.sub(r'[，。！？\s]', '', s).strip()
    return bool(cleaned) and len(cleaned) <= 6 and cleaned in _FILLER_WORDS


# ===== 闲聊轻量通道（无工具直连流式）=====
# intent=none 的闲聊不需要 MCP 工具；带全套工具定义进 fncall 模式会让
# prompt 膨胀、Agnes 首包从 ~1s 恶化到 13s+（实测 2026-08-22 事故 75s）。
# 此通道直连 chat/completions 流式接口，payload 只含 system+history。
_LITE_MAX_TOKENS = 300
_LITE_TIMEOUT = (2, 15)   # 连接2s；chunk间最大等待15s（超时抛异常→回退重brain）
_LITE_TOTAL_TIMEOUT = float(os.getenv("LITE_TOTAL_TIMEOUT", "12"))
_LITE_HISTORY_TURNS = int(os.getenv("LITE_HISTORY_TURNS", "6"))
_LITE_FIRST_CONTENT_TIMEOUT = 8.0  # 首 content chunk 最大等待（秒）；Agnes 尾部抖动最高 7.9s，8s 几乎不误杀
# delta_stream 门控：qwen_agent 0.0.34 不支持 brain 路径 delta_stream=True，
# 保留此 env 作未来版本兼容开关（当前恒为 False）。
BRAIN_DELTA_STREAM = os.getenv("BRAIN_DELTA_STREAM", "0")  # no-op 前瞻门：qwen_agent 0.0.34 不支持 delta_stream（base.py:222 / function_calling.py:129），默认关


def _lite_endpoint():
    """闲聊轻量通道专用端点：优先 Agnes（非 reasoning 模型）。

    Agnes 已配置则返回它，避免 stepfun reasoning_effort 吃光配额；
    未配置时兜底 active_chat_endpoint()。
    """
    if is_agnes_configured():
        return AGNES_BASE, AGNES_KEY, AGNES_MODEL
    return active_chat_endpoint()


def _chat_lite_stream(text: str, session_id: str = "default", channel: str = "voice"):
    """闲聊快速通道：yield (sentence, full_reply)；失败时不 yield 任何内容并返回。

    成功结束时自行写入 history/cache/记忆提取；调用方据此判断是否回退。

    参数:
      channel: "voice" → 对输出做 _clean_for_tts 清洗（语音场景）
               "feishu_text" → 跳过清洗，保留 markdown（飞书文本场景）
    """
    try:
        base, key, model = _lite_endpoint()
    except Exception as e:
        log.debug(f"[lite] 无可用端点: {e}")
        return
    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist[-_LITE_HISTORY_TURNS:]]
    try:
        _wm_prompt = _build_wm_anaphor_prompt()
        _mem_prompt = _recall_memory_prompt(text)
        _sys_combined = "\n\n".join(p for p in (_wm_prompt, _mem_prompt) if p)
        if _sys_combined:
            messages.insert(0, {'role': 'system', 'content': _sys_combined})
        _lite_sys = _build_lite_system_msg("none", session_id=session_id)
        if channel in ("voice", "feishu_text"):
            try:
                from agent.system_msg import _replace_system_msg_for_channel
                _lite_sys = _replace_system_msg_for_channel(_lite_sys, channel)
            except Exception:
                pass
        messages.insert(0, {"role": "system", "content": _lite_sys})
    except Exception as e:
        log.debug(f"[lite] system消息构建降级: {e}")
        # 兜底：至少保留人格设定，避免模型自报 provider 身份（"我是 Agnes"）
        if not any(m.get("role") == "system" for m in messages):
            try:
                from agent.roles import get_role, get_current_role
                fallback = (get_role(get_current_role()) or {}).get(
                    "system_prompt", "你是 Charlie，一个智能语音助手。")
            except Exception:
                fallback = "你是 Charlie，一个智能语音助手。"
            messages.insert(0, {"role": "system", "content": fallback})
    # 粗略估算 token 数：system + 历史；超限时裁剪早期消息
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars > 4000:  # 中文约 2000 tokens
        messages = messages[-4:]  # 保留最近 2 轮
    messages.append({"role": "user", "content": text})
    payload = {
        "model": model, "messages": messages,
        "stream": True, "max_tokens": _LITE_MAX_TOKENS,
    }
    # thinking 参数仅 GLM（智谱）支持，其他 provider 会 400
    if "glm" in base.lower() or "zhipu" in base.lower():
        payload["thinking"] = {"type": "disabled"}
    t0 = time.time()
    try:
        r = _session.post(f"{base}/chat/completions",
                          json=payload,
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=_LITE_TIMEOUT, stream=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"[lite] 快速通道请求失败，回退大脑: {e}")
        # 连接类失败（超时/断连）计入 provider 失败计数，让轮换机制
        # 在节点风暴时切到备节点。真实事故：agnes 节点 40 分钟内 19 次
        # Read timeout，lite 15s 超时 + 回退大脑再撞 35s 超时 = 用户等 50s，
        # 而 lite 路径不计失败导致 sagnes 备节点一直没被启用。
        try:
            import requests as _rq
            if isinstance(e, (TimeoutError, _rq.exceptions.Timeout,
                              _rq.exceptions.ConnectionError)):
                _note_provider_failure()
        except Exception:
            pass
        return
    full = ""
    sent_len = 0
    first_at = None
    deadline = time.time() + _LITE_TOTAL_TIMEOUT
    timer = None
    try:
        # 方案 B：用 Timer 强制关闭底层 socket，解决 iter_lines 在 C 层阻塞时 deadline 不检查的问题
        timer = threading.Timer(_LITE_TOTAL_TIMEOUT, r.close)
        timer.start()
        try:
            for line in r.iter_lines():
                if time.time() > deadline:
                    _elapsed = time.time() - t0
                    log.warning(f"[lite] 流式总时长超时(>{_LITE_TOTAL_TIMEOUT}s, elapsed={_elapsed:.1f}s, model={model})，中断流")
                    if not full:
                        return
                    break
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    obj = json.loads(body)
                except json.JSONDecodeError:
                    continue
                # 末尾 usage chunk 的 choices 可能为空列表
                choices = obj.get("choices") or [{}]
                delta = (choices[0].get("delta") or {}).get("content") or ""
                # 首 content 快速失败：1.5s 内从未收到 content 且当前 delta 仍空 → 放弃
                # 注意：放 delta 解析之后，避免把"第1个 content chunk 恰好 >1.5s 到达"
                # 的正常情况误杀（Agnes 完整 prompt 首包约 2s）。
                if not delta and first_at is None and time.time() - t0 > _LITE_FIRST_CONTENT_TIMEOUT:
                    _elapsed = time.time() - t0
                    log.warning(f"[lite] 首content超时(>{_LITE_FIRST_CONTENT_TIMEOUT}s, elapsed={_elapsed:.1f}s, model={model})无content，中断流")
                    return
                if not delta:
                    continue
                if first_at is None:
                    first_at = time.time()
                    log.info(f"[lite] 首token={first_at - t0:.2f}s")
                full += delta
                unsent = full[sent_len:]
                while True:
                    m = _SENTENCE_END.search(unsent)
                    if not m:
                        break
                    sentence = unsent[:m.end()].strip()
                    unsent = unsent[m.end():]
                    sent_len = len(full) - len(unsent)
                    if sentence:
                        cleaned = _clean_for_tts(sentence) if channel == "voice" else sentence
                        if cleaned and not _is_filler_word(cleaned):
                            yield (cleaned, full)
            if full and len(full) > sent_len:
                remaining = (_clean_for_tts(full[sent_len:].strip())
                             if channel == "voice" else full[sent_len:].strip())
                if remaining and not _is_filler_word(remaining):
                    yield (remaining, full)
        except Exception as e:
            # 中途断流：已产出的句子有效，剩余部分放弃
            _elapsed = time.time() - t0
            log.warning(f"[lite] 流中断(elapsed={_elapsed:.1f}s, model={model}, {len(full)}字): {e}")
            if full and len(full) > sent_len:
                remaining = (_clean_for_tts(full[sent_len:].strip())
                             if channel == "voice" else full[sent_len:].strip())
                if remaining and not _is_filler_word(remaining):
                    yield (remaining, full)
    finally:
        if timer is not None:
            timer.cancel()
        try:
            r.close()
        except Exception as e:
            log.debug(f"[agent.llm] error at line 1139: {e}")
            pass
    if full.strip():
        _append_history(hist, text, full)
        _cache_set(text, full, session_id=session_id)
        try:
            import threading as _mem_thread
            _mem_thread.Thread(target=_remember_conversation_async, args=(text, full), daemon=True).start()
        except Exception as e:
            log.debug(f"[lite] 记忆提取线程启动失败: {e}")


def brain_stream_sentences(text: str, session_id: str = "default", interrupted_reply: str = "", channel: str = "voice", cancel_event=None) -> Generator[Tuple[str, str], None, None]:
    """流式 brain：yield (已清洗句子, 完整回复)。

    跨 session 隔离逻辑：
    session_id 变化时 wm_switch_session() 保存当前会话 WM 并载入
    目标会话的上次状态（各设备会话事实互不覆盖、各自保留）。

    参数:
      channel: "voice"（默认，语音场景，输出经 _clean_for_tts 清洗 + 短句限制）
               "feishu_text"（飞书文本场景，保留 markdown 格式，不经过 _clean_for_tts）
    """
    try:
        from agent.persona import update as _persona_update
        _persona_update(text)
    except Exception as e:
        log.debug(f"[agent.llm] error at line 1166: {e}")
        pass
    try:
        from agent.persona import update_relationship_from_interaction as _update_rel
        _update_rel(text)
    except Exception as e:
        log.debug(f"[agent.llm] error at line 1171: {e}")
        pass
    # 情感 TTS：根据用户情绪设置当前轮 TTS 情感（线程级）
    try:
        from agent.persona import get_current_emotion as _get_emotion
        from agent.asr_tts import set_current_emotion as _set_emotion
        _set_emotion(_get_emotion())
    except Exception:
        pass

    def _maybe_clean(sentence: str) -> str:
        """根据 channel 决定是否对句子做 TTS 清洗。

        voice 渠道：保留 _clean_for_tts 去除 markdown/符号，适合 TTS 播放。
        feishu_text 渠道：跳过清洗，保留 markdown 格式供飞书文本展示。
        """
        if channel == "feishu_text":
            return sentence
        return _clean_for_tts(sentence)

    # 会话切换时切换工作记忆槽位并清除上下文缓存（thread-local，跨通道并发安全）
    if session_id != _get_last_wm_session():
        wm_switch_session(session_id)
        invalidate_context_cache()
        _set_last_wm_session(session_id)

    # 快路径链 — lazy import to avoid circular dependency
    from voice_agent import FAST_PATHS
    reply = None
    for path in FAST_PATHS:
        reply = path.run(text, session_id)
        if reply is not None:
            _append_history(_get_history(session_id), text, reply)
            _cache_set(text, reply, session_id=session_id)
            try:
                import threading as _mem_thread
                _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
            except Exception as e:
                log.debug(f"[brain_stream] fast_path记忆提取线程启动失败: {e}")
            yield (reply, reply)
            return
    cached = (_cache_get_interrupted(text, interrupted_reply, session_id=session_id)
              if interrupted_reply.strip() else _cache_get(text, session_id))
    if cached is not None:
        # 命中也要入历史：否则下一轮"为什么？"指代的上文缺失
        _append_history(_get_history(session_id), text, cached)
        yield (_maybe_clean(cached), cached)
        return
    # 先分类，再根据意图决定走 lite stream 还是完整 brain
    # 这样需要工具的查询（票房、搜索、记忆等）不会被闲聊通道吞掉
    _keyword_hit = any(kw in text for kw in _ALL_DOMAIN_KEYWORDS)
    mcp_set = _classify_intent(text)
    _lite_already_failed = False

    # intent=none 的纯闲聊走 lite stream（无工具直连流式）
    if mcp_set == "none" and not interrupted_reply.strip() and not _demo_mode_active() and not is_garbled_asr(text):
        _lite_yielded = False
        _lite_full = ""
        for _sent, _full in _chat_lite_stream(text, session_id, channel=channel):
            _lite_yielded = True
            _lite_full = _full
            yield (_sent, _full)
        if _lite_yielded:
            update_working_memory(intent=mcp_set, topic=text[:20],
                                  entity=_extract_entities(text), facts=_extract_facts(text))
            return
        log.info("[lite] 闲聊通道无产出，回退完整大脑")
        _lite_already_failed = True
    # 回退路径已在上文 increment过 turn_count，此处不重复递增
    _already_incremented = not _keyword_hit and not interrupted_reply.strip() and not _demo_mode_active()
    update_working_memory(intent=mcp_set, topic=text[:20], entity=_extract_entities(text), facts=_extract_facts(text), increment_turn=not _already_incremented)
    try:
        from app import load_magic_module
        _mod = load_magic_module("magic_scenes")
        if _mod:
            proto_key = _mod.match_protocol(text)
            if proto_key:
                log.info(f"[scene] Protocol触发: {text[:20]} → {proto_key}")
                reply = _mod.execute_protocol(proto_key)
                _append_history(_get_history(session_id), text, reply)
                _cache_set(text, reply, session_id=session_id)
                try:
                    import threading as _mem_thread
                    _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
                except Exception as e:
                    log.debug(f"[brain_stream] 场景记忆提取线程启动失败: {e}")
                yield (reply, reply)
                return
    except Exception as e:
        log.debug(f"[brain_stream] 场景匹配跳过: {e}")
    _ensure_event_loop()
    if _demo_mode_active() and not (
        os.getenv("OLLAMA_ENABLED", "0") == "1" and _ollama_online()
    ):
        _port = 8000
        try:
            from app.config import http_port as _hp
            _port = _hp()
        except Exception as e:
            log.debug(f"[brain_stream] http_port() 不可用: {e}")
        message = (f"我还没配置 AI 大脑。注册智谱 GLM 免费 Key 即可解锁完整能力（注册即送，永久免费）：\n"
                   f"  打开 http://localhost:{_port}/welcome 按引导操作")
        _append_history(_get_history(session_id), text, message)
        try:
            import threading as _mem_thread
            _mem_thread.Thread(target=_remember_conversation_async, args=(text, message), daemon=True).start()
        except Exception as e:
            log.debug(f"[brain_stream] demo模式记忆提取线程启动失败: {e}")
        yield (message, message)
        return
    # 闲聊快速通道（关键词命中路径或乐观回退后 mcp_set == none）
    if mcp_set == "none" and not interrupted_reply.strip() and not _lite_already_failed and not is_garbled_asr(text):
        _lite_yielded = False
        for _sent, _full in _chat_lite_stream(text, session_id, channel=channel):
            _lite_yielded = True
            yield (_sent, _full)
        if _lite_yielded:
            return
        log.info("[lite] 快速通道无产出，回退完整大脑")
    try:
        brain_instance = _get_brain(mcp_set, session_id=session_id)
    except Exception as e:
        _record_brain_failure(_fmt_exc(e)[:400])
        if not _llm_has_any_key():
            _port = 8000
            try:
                from app.config import http_port as _hp
                _port = _hp()
            except Exception as e:
                log.debug(f"[brain_stream] http_port() 不可用: {e}")
            message = (f"我还没配置 AI 大脑。注册智谱 GLM 免费 Key 即可解锁：\n"
                       f"  打开 http://localhost:{_port}/welcome 按引导操作")
        else:
            log.error(f"[brain] 构建失败: {e}", exc_info=True)
            message = "大脑启动失败，请稍后重试。"
        try:
            import threading as _mem_thread
            _mem_thread.Thread(target=_remember_conversation_async, args=(text, message), daemon=True).start()
        except Exception as e:
            log.debug(f"[brain_stream] 构建失败记忆提取线程启动失败: {e}")
        yield (message, message)
        return

    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist] + [{"role": "user", "content": text}]
    # Channel-aware system message: prepend channel rules without modifying brain instance
    # 仅允许一条且位于首位的 system——qwen_agent 截断路径遇双 system 直接抛 400
    # ModelServiceError（上下文超限时必现），故 channel 规则与工作记忆合并为一条
    _sys_parts = []
    if channel in ("voice", "feishu_text"):
        try:
            from agent.system_msg import _replace_system_msg_for_channel
            _sys_parts.append(_replace_system_msg_for_channel(brain_instance.system_message, channel))
        except Exception as e:
            log.debug(f"[agent.llm] error at line 1311: {e}")
            pass
    else:
        _sys_parts.append(brain_instance.system_message)
    _wm_prompt = _build_wm_anaphor_prompt()
    if _wm_prompt:
        _sys_parts.append(_wm_prompt)
    _mem_prompt = _recall_memory_prompt(text)
    if _mem_prompt:
        _sys_parts.append(_mem_prompt)
    if _sys_parts:
        # 仅拼接真实字符串（测试中 system_message 可能是 MagicMock 对象）
        _sys_content = "\n\n".join(p for p in _sys_parts if isinstance(p, str) and p)
        if _sys_content:
            messages.insert(0, {"role": "system", "content": _sys_content})
    if interrupted_reply.strip():
        messages.insert(-1, _interrupted_context_message(interrupted_reply))
    sent_len = 0
    full_reply = ""
    _yielded_count = 0
    _max_sentences = _get_max_sentences()
    _max_reached = False
    _brain_start = time.time()
    _last_fn_count = 0
    _tool_steps = 0
    try:
        for rsp in _brain_run_iter(brain_instance, messages, _BRAIN_RUN_TIMEOUT, cancel_event=cancel_event):
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
                # 多步 Agent：检测新的工具调用，播报进度
                _fn_count = _count_function_msgs(rsp)
                if _fn_count > _last_fn_count:
                    _last_fn_count = _fn_count
                    _tool_steps += 1
                    if _tool_steps > _MAX_TOOL_STEPS:
                        log.warning(f"[brain] 工具调用步数达上限 {_MAX_TOOL_STEPS}，停止")
                        break
                    # 提取最新工具名播报进度
                    for _m in reversed(rsp):
                        if isinstance(_m, dict) and _m.get("role") == "function":
                            _tname = _m.get("name", "")
                            _progress = _tool_progress_text(_tname)
                            if _progress and channel == "voice":
                                yield (_progress, full_reply)
                            break
            t = _extract_assistant_text(rsp)
            if not t or len(t) <= sent_len:
                continue
            full_reply = t
            unsent = full_reply[sent_len:]
            while True:
                m = _SENTENCE_END.search(unsent)
                if m:
                    sentence = unsent[:m.end()].strip()
                    unsent = unsent[m.end():]
                    if sentence:
                        sent_len = len(full_reply) - len(unsent)
                        cleaned_sentence = _maybe_clean(sentence)
                        if not _is_filler_word(cleaned_sentence):
                            yield (cleaned_sentence, full_reply)
                            _yielded_count += 1
                            if _max_sentences and _yielded_count >= _max_sentences:
                                _max_reached = True
                                break
                    continue
                if len(unsent) >= _MIN_CHUNK:
                    cm = _COMMA_SOFT.search(unsent)
                    if cm:
                        sentence = unsent[:cm.end()].strip()
                        unsent = unsent[cm.end():]
                        if sentence:
                            sent_len = len(full_reply) - len(unsent)
                            cleaned_sentence = _maybe_clean(sentence)
                            if not _is_filler_word(cleaned_sentence):
                                yield (cleaned_sentence, full_reply)
                                _yielded_count += 1
                                if _max_sentences and _yielded_count >= _max_sentences:
                                    _max_reached = True
                                    break
                        continue
                # 硬上限：向后搜索自然停顿点再截断
                if len(unsent) >= _MAX_CHUNK:
                    _cut = _MAX_CHUNK
                    for _sep in ['，', '。', '；', '、', ',', '！', '？']:
                        _pos = unsent.rfind(_sep, max(0, _MAX_CHUNK - 15), _MAX_CHUNK + 5)
                        if _pos > _MAX_CHUNK - 15:
                            _cut = _pos + 1
                            break
                    sentence = unsent[:_cut]
                    unsent = unsent[_cut:]
                    sent_len = len(full_reply) - len(unsent)
                    cleaned_sentence = _maybe_clean(sentence)
                    if not _is_filler_word(cleaned_sentence):
                        yield (cleaned_sentence, full_reply)
                        _yielded_count += 1
                        if _max_sentences and _yielded_count >= _max_sentences:
                            _max_reached = True
                            break
                    continue
                break
            if _max_reached:
                break
            sent_len = len(full_reply) - len(unsent)
        _record_brain_success()
    except Exception as e:
        _err = _fmt_exc(e)
        is_429 = _is_429_error(_err)
        is_conn = _is_conn_error(_err)
        _record_brain_failure(_err[:60])
        # 超时/连接错误时主动清除当前 brain 缓存，避免复用状态不一致的实例
        if is_conn or 'timeout' in _err.lower():
            with _st.brain_lock:
                if mcp_set in _st.brains:
                    _cleanup_brain_processes(_st.brains[mcp_set])
                    del _st.brains[mcp_set]
                    log.info(f"[brain] 超时/连接错误，清除 brain 缓存: mcp={mcp_set}")
        from app import llm_config as _llm_cfg
        _do_retry = _llm_cfg.is_glm_configured() and not _llm_cfg.is_ark_configured()
        if not full_reply and _do_retry:
            with _st.brain_lock:
                for _k, _b in list(_st.brains.items()):
                    _cleanup_brain_processes(_b)
                _st.brains.clear()
                _st.brain_failures = 0
            try:
                fb = _brain_llm(text, session_id)
                if fb and "忙不过来" not in fb and "处理时出错" not in fb:
                    yield (fb, fb)
                    return
            except Exception as fb_err:
                log.debug(f"[brain_stream] 非流式回退也失败: {fb_err}")
        if not full_reply:
            full_reply = _friendly_brain_error(_err)
    if full_reply and len(full_reply) > sent_len:
        remaining = full_reply[sent_len:].strip()
        if remaining:
            cleaned_remaining = _maybe_clean(remaining)
            if not _is_filler_word(cleaned_remaining):
                yield (cleaned_remaining, full_reply)
    elif not full_reply:
        full_reply = "我没听明白"
        yield (full_reply, full_reply)
    _append_history(hist, text, full_reply)
    if session_id != "default":
        _trim_history_tokens(hist, session_id=session_id)
    _cache_set(text, full_reply, interrupted_reply, session_id=session_id)
    try:
        import threading as _mem_thread
        _mem_thread.Thread(target=_remember_conversation_async, args=(text, full_reply), daemon=True).start()
    except Exception as e:
        log.debug(f"[brain_stream] 记忆提取线程启动失败: {e}")

def stream_voice_pipeline(text: str) -> Generator[Tuple[str, str, bytes], None, None]:
    try:
        for sentence, full_reply in brain_stream_sentences(text):
            mp3 = _tts_cleaned_to_mp3(sentence)
            yield ("sentence", sentence, mp3)
    except Exception as e:
        log.error(f"流式流水线异常: {e}")
        yield ("error", _friendly_brain_error(str(e)), b"")
    yield ("done", None, b"")
