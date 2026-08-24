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
)
from agent.cache import _cache_get, _cache_set, _cache_get_interrupted
from agent.history import (
    _get_history, _append_history, _save_history,
    _trim_history_tokens,
)
from agent.system_msg import _build_system_msg
from agent.asr_tts import _clean_for_tts, _tts_cleaned_to_mp3
from agent.intent import LOW_INTENT_ASR_REPLY, is_low_intent_asr, is_garbled_asr
from agent.working_memory import (
    update as update_working_memory,
    reset as wm_reset,
    get as get_working_memory,
    get_all as get_working_memory_all,
    restore as restore_working_memory,
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


_last_wm_session_id = "default"

_KEYWORD_MAP = [
    ({"天气", "气温", "下雨", "温度", "几度", "今天天气", "明天天气", "今天冷", "今天热", "定位"}, "amap-maps"),
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
    ({"放歌", "放一首", "放个", "播放", "听歌", "放周杰伦", "放毛不易", "音乐", "歌单", "停止播放", "每日推荐", "随机", "来一首", "播一首", "点一首", "放首", "放点", "整首", "整点", "循环", "单曲", "来首", "点歌", "唱首歌", "放音乐", "暂停", "继续播放", "继续", "下一首", "上一首", "换一首", "切歌", "停止", "停"}, "magic-music"),
    ({"空调", "电视", "制冷", "制热", "风扇", "开灯", "关灯", "关闭空调", "关闭电视"}, "ac-control"),
    ({"文件", "读文件", "写文件", "笔记"}, "filesystem"),
    ({"外卖", "点餐", "购物", "商品", "查一下", "充电桩", "特斯拉", "出门"}, "magic-life"),
    ({"穿什么", "穿搭", "今天穿", "明天穿", "出门穿", "约会穿", "搭配", "衣橱", "衣服", "加衣服", "外套", "冷了穿", "热了穿", "穿几件"}, "magic-wardrobe"),
    ({"做菜", "菜谱", "食谱", "做什么菜", "食材", "吃什么", "吃饭", "好吃", "想吃什么", "做饭", "怎么做", "做法", "怎么煮", "怎么炒", "今天吃啥", "今晚吃啥", "中午吃啥", "推荐个菜", "推荐一道菜", "凉菜", "热菜", "汤", "主食", "下饭", "买菜", "番茄炒蛋", "可乐鸡翅"}, "magic-recipe"),
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
_ALL_DOMAIN_KEYWORDS = set()
for _kw_set, _ in _KEYWORD_MAP:
    _ALL_DOMAIN_KEYWORDS |= _kw_set

# 高歧义词：如果出现在感叹/评价语境中，不应触发工具查询
_EXCLAMATION_SUFFIXES = ("不错", "不错吧", "真好", "挺好的", "挺不错", "还行", "很差", "太差", "好差",
                        "太热", "太冷", "好热", "好冷", "好闷", "舒服", "不舒服", "不好", "棒", "糟糕", "还行吧")
_AMBIGUOUS_KWS = ("天气", "温度", "下雨")


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
    elif "magic" in raw: return "magic-music"
    elif "ac" in raw or "air" in raw or "control" in raw: return "ac-control"
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
    if len(text) <= 15 and not any(kw in text for kw in _ALL_DOMAIN_KEYWORDS):
        log.info(f"[intent] 闲聊短句跳过LLM: '{text}' → none")
        _intent_cache_set(text, "none")
        return "none"
    # 关键词匹配（含消歧）— 即使在冷却期也执行
    matched_domains = set()
    for keywords, mcp_name in _KEYWORD_MAP:
        if any(kw in text for kw in keywords):
            matched_domains.add(mcp_name)
    if matched_domains:
        if _is_exclamatory_weather(text):
            log.info(f"[intent] 消歧: 感叹语境，降级为 none: '{text[:30]}'")
            _intent_cache_set(text, "none")
            return "none"
        try:
            from app.audit_log import audit_log
            audit_log("intent", input_data=text, output_data=mcp_name,
                      action="keyword_match", session_id="intent")
        except Exception:
            pass
        if len(matched_domains) == 1:
            result = matched_domains.pop()
            log.info(f"[intent] 关键词命中: '{text[:30]}' → {result}")
            _intent_cache_set(text, result)
            return result
        log.info(f"[intent] 多域关键词命中 {matched_domains}，降级到 LLM 分类: '{text[:30]}'")
        # 多个域命中 → 继续走 LLM 分类
    # disabled 检查 — 仅影响 LLM 分类路径
    with _st.intent_cache_lock:
        if now < _st.intent_disabled_until:
            log.info(f"[intent] 分类冷却中，{_st.intent_disabled_until - now:.0f}秒内默认none")
            return "none"
    prompt = (
        "任务: 判断用户输入需要哪个工具, 只回一个词\n"
        "选项: none | amap-maps | baize-skills | filesystem | magic-music | magic-reminder | magic-notes | magic-system | magic-info | magic-life | ac-control | magic-recipe | magic-preferences\n"
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
        "- magic-preferences = 设置/查询/删除用户偏好(我喜欢/以后/别再/我习惯/记住)\n"
        "示例:\n"
        "  你好→none\n  今天天气怎么样→amap-maps\n  北京天气→amap-maps\n"
        "  搜一下薛之谦→baize-skills\n  帮我点杯咖啡→baize-skills\n"
        "  设提醒→magic-reminder\n  放歌→magic-music\n  晚安→magic-scenes\n"
        "  早上好→magic-scenes\n  看电影→magic-scenes\n  自进化→magic-evolution\n"
        "  打开百度/搜索百度→magic-browser\n  学习我→magic-evolution\n"
        "  打开空调→ac-control\n  番茄炒蛋怎么做→magic-recipe\n"
        "  今天吃什么→magic-recipe\n  音量调大→magic-system\n  现在几点→magic-info\n"
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
        #   (2, 5) = 连接 2s / 读取 5s（原为 2/8）。
        # 失败时直接回退 none（不走重试），等待 INTENT_FAILURE_COOLDOWN 冷却后再尝试。
        r = _session.post(f"{_ep_base}/chat/completions",
            json=_payload,
            headers={"Authorization": f"Bearer {_ep_key}"},
            timeout=(2, 5))
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
        except Exception:
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
                        return future.result(timeout=timeout)
                    except _cf.TimeoutError as e:
                        future.cancel()
                        # 包装成带 timeout 关键字的异常：_record_brain_failure 的
                        # 错误特征匹配依赖 message 内容触发 provider 轮换与失败计数
                        raise TimeoutError(
                            f"MCP tool '{tool_name}' timeout after {timeout:.0f}s") from e
                    except Exception as e:
                        log.info(f'[mcp-patch] MCP工具调用失败: {e}')
                        raise e

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


# ===== Brain build / cache =====
def _build_brain(mcp_set="all"):
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
    from app.mcp_registry import resolve as resolve_mcps
    llm_cfg = resolve_llm()
    mcp_servers = resolve_mcps(mcp_set)
    # magic-preferences 始终加载（偏好记忆对闲聊也有价值且不触发主动工具调用）
    _mcp_registry = __import__("app.mcp_registry", fromlist=["ALL_MCP"])
    if "magic-preferences" in _mcp_registry.ALL_MCP:
        mcp_servers.setdefault("magic-preferences", _mcp_registry.ALL_MCP["magic-preferences"])
    # magic-jarvis 仅在非 none brain 时注入（避免闲聊 brain 带 11 个工具导致 LLM 主动调用 get_air_quality 等，造成 10-40s 延迟）
    if mcp_set != "none" and "magic-jarvis" in _mcp_registry.ALL_MCP:
        mcp_servers.setdefault("magic-jarvis", _mcp_registry.ALL_MCP["magic-jarvis"])
    log.info(f"[brain] 构建大脑 mcp_set={mcp_set}, 启用{len(mcp_servers)}个MCP: {list(mcp_servers.keys())}")
    tools = [{"mcpServers": mcp_servers}] if mcp_servers else []
    brain = Assistant(llm=llm_cfg, name='Charlie',
        system_message=_build_system_msg(mcp_set),
        function_list=tools)
    _install_openai_compat(brain)
    _install_mcp_timeout_patch()
    return brain

_BRAIN_CACHE_MAX = int(os.getenv("BRAIN_CACHE_MAX", "12"))
_BRAIN_RUN_TIMEOUT = float(os.getenv("BRAIN_RUN_TIMEOUT", "35"))


def _brain_run_iter(brain_instance, messages, total_timeout: float = None):
    """包装 brain.run() 生成器，用独立线程+队列实现 per-iteration 超时保护。

    防止 brain.run 的 next() 永久阻塞导致线程池 worker 耗尽。
    超时后主线程抛 TimeoutError，孤儿线程自然结束（Python 无法 kill 线程）。
    """
    if total_timeout is None:
        total_timeout = _BRAIN_RUN_TIMEOUT
    _q: queue.Queue = queue.Queue()
    _start = time.time()

    def _worker():
        try:
            _ensure_event_loop()
            for rsp in brain_instance.run(messages):
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

def _get_brain(mcp_set="none"):
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
            # 锁外构建（不阻塞其他 mcp_set 的 _get_brain 调用）
            new_brain = _build_brain(mcp_set)
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
    brain.system_message = _build_system_msg(mcp_set)
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
        elif is_429 and _st.brain_failures >= 10:
            log.warning(f"[brain] 429连续{_st.brain_failures}次, 清除缓存大脑...")
            for k, b in list(_st.brains.items()):
                _cleanup_brain_processes(b)
            _st.brains.clear()
            _st.brain_failures = 0
    # provider 轮换在 brain_lock 之外执行，避免与 _try_rotate_provider_on_failure 内部
    # 获取 brain_lock 形成不可重入死锁
    if is_conn:
        _note_provider_failure()
        _try_rotate_provider_on_failure()

def _record_brain_success():
    _reset_provider_failures()
    with _st.brain_lock:
        _st.brain_failures = 0
        _st.brain_last_success = time.time()

def _cleanup_brain_processes(brain_instance):
    """No-op: MCPManager 是单例，clients/processes 全局共享。

    运行期 brain 淘汰/清理不再杀 MCP 子进程，否则会误杀其他正在使用的 brain 的进程。
    MCP 子进程随 MCPManager 单例存活，彻底清理由 voice_server lifespan shutdown 的
    MCPManager().shutdown() 负责（同步方法，内部正确处理 async cleanup + processes terminate）。
    """
    # 保留调用点但函数体不做任何操作，避免误杀其他 brain 的 MCP 子进程
    return

def restart_brain() -> str:
    with _st.brain_lock:
        for k, b in list(_st.brains.items()):
            _cleanup_brain_processes(b)
        _st.brains.clear()
        _st.brain_failures = 0
    log.info("[brain] 手动重启, 所有缓存大脑已清除")
    return "大脑重启中, 下次请求将自动重建"

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
    except Exception:
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


def _build_wm_anaphor_prompt() -> str | None:
    """从 working_memory 提取指代消解信息，生成 LLM 可理解的上下文提示。

    当对话轮数 > 1 且意图栈非空时，向 messages 注入一个 system 消息，
    帮助 LLM 理解"它/这个/那个"等指代词指向的上文实体。
    """
    try:
        wm = get_working_memory()
    except Exception:
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
    except Exception:
        pass
    try:
        from agent.persona import update_relationship_from_interaction as _update_rel
        _update_rel(text)
    except Exception:
        pass
    mcp_set = _classify_intent(text)
    # 会话切换时重置工作记忆并清除上下文缓存
    global _last_wm_session_id
    if session_id != _last_wm_session_id:
        wm_reset()
        invalidate_context_cache()
        _last_wm_session_id = session_id
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
    if mcp_set == "none":
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
            _record_brain_failure(str(e)[:60])
            log.error(f"[brain] 构建失败: {e}", exc_info=True)
            if not _llm_has_any_key():
                return ("我还没配置 AI 大脑。注册智谱 GLM 免费 Key 即可解锁完整能力"
                        "（注册即送，永久免费）：打开 /welcome 按引导操作")
            return "大脑启动失败，请稍后重试。"
        hist = _get_history(session_id)
        messages = [{'role': m['role'], 'content': m['content']} for m in hist] + [{'role': 'user', 'content': text}]
        _wm_prompt = _build_wm_anaphor_prompt()
        if _wm_prompt:
            messages.insert(0, {'role': 'system', 'content': _wm_prompt})
        _brain_start = time.time()
        try:
            for rsp in _brain_run_iter(brain_instance, messages, _BRAIN_RUN_TIMEOUT):
                final = rsp
                if isinstance(rsp, list):
                    for m in rsp:
                        if isinstance(m, dict) and m.get('role') == 'function' and isinstance(m.get('content'), str) and m['content'].startswith('__MUSIC__'):
                            break
                    else:
                        continue
                    break
            _record_brain_success()
            break
        except Exception as e:
            _err = str(e)
            is_429 = (_RateLimitError is not None and isinstance(e, _RateLimitError)) or \
                     '429' in _err or 'Too Many' in _err or '1305' in _err or \
                     'rate' in _err.lower() or '频率' in _err
            if is_429 and _attempt < _glm_tries - 1:
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
    _cache_set(text, reply)
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
_LITE_MAX_TOKENS = 200
_LITE_TIMEOUT = (2, 15)   # 连接2s；chunk间最大等待15s（超时抛异常→回退重brain）
_LITE_TOTAL_TIMEOUT = float(os.getenv("LITE_TOTAL_TIMEOUT", "10"))
_LITE_HISTORY_TURNS = int(os.getenv("LITE_HISTORY_TURNS", "12"))


def _chat_lite_stream(text: str, session_id: str = "default", channel: str = "voice"):
    """闲聊快速通道：yield (sentence, full_reply)；失败时不 yield 任何内容并返回。

    成功结束时自行写入 history/cache/记忆提取；调用方据此判断是否回退。

    参数:
      channel: "voice" → 对输出做 _clean_for_tts 清洗（语音场景）
               "feishu_text" → 跳过清洗，保留 markdown（飞书文本场景）
    """
    try:
        base, key, model = active_chat_endpoint()
    except Exception as e:
        log.debug(f"[lite] 无可用端点: {e}")
        return
    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist[-_LITE_HISTORY_TURNS:]]
    messages.append({"role": "user", "content": text})
    try:
        _wm_prompt = _build_wm_anaphor_prompt()
        if _wm_prompt:
            messages.insert(0, {'role': 'system', 'content': _wm_prompt})
        messages.insert(0, {"role": "system", "content": _build_system_msg("none")})
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
    payload = {
        "model": model, "messages": messages,
        "stream": True, "max_tokens": _LITE_MAX_TOKENS,
    }
    # thinking 参数仅 GLM（智谱）支持，其他 provider 会 400
    if "glm" in base.lower() or "zhipu" in base.lower():
        payload["thinking"] = {"type": "disabled"}
    if "stepfun" in base.lower():
        payload["reasoning_effort"] = "low"
    t0 = time.time()
    try:
        r = _session.post(f"{base}/chat/completions",
                          json=payload,
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=_LITE_TIMEOUT, stream=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"[lite] 快速通道请求失败，回退大脑: {e}")
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
                    log.warning(f"[lite] 流式总时长超时(>{_LITE_TOTAL_TIMEOUT}s)，中断流")
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
            log.warning(f"[lite] 流中断({len(full)}字): {e}")
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
        except Exception:
            pass
    if full.strip():
        _append_history(hist, text, full)
        _cache_set(text, full)
        try:
            import threading as _mem_thread
            _mem_thread.Thread(target=_remember_conversation_async, args=(text, full), daemon=True).start()
        except Exception as e:
            log.debug(f"[lite] 记忆提取线程启动失败: {e}")


def brain_stream_sentences(text: str, session_id: str = "default", interrupted_reply: str = "", channel: str = "voice") -> Generator[Tuple[str, str], None, None]:
    """流式 brain：yield (已清洗句子, 完整回复)。

    跨 session 隔离逻辑：
    1. 入口保存 working_memory 快照
    2. session_id 变化时 wm_reset() + 清除上下文缓存
    3. finally 恢复快照，确保 A 用户的工作记忆不被 B 用户覆盖

    参数:
      channel: "voice"（默认，语音场景，输出经 _clean_for_tts 清洗 + 短句限制）
               "feishu_text"（飞书文本场景，保留 markdown 格式，不经过 _clean_for_tts）
    """
    try:
        from agent.persona import update as _persona_update
        _persona_update(text)
    except Exception:
        pass
    try:
        from agent.persona import update_relationship_from_interaction as _update_rel
        _update_rel(text)
    except Exception:
        pass

    # 跨 session 隔离：入口保存工作记忆快照，finally 时恢复
    _wm_snapshot = get_working_memory_all()

    def _maybe_clean(sentence: str) -> str:
        """根据 channel 决定是否对句子做 TTS 清洗。

        voice 渠道：保留 _clean_for_tts 去除 markdown/符号，适合 TTS 播放。
        feishu_text 渠道：跳过清洗，保留 markdown 格式供飞书文本展示。
        """
        if channel == "feishu_text":
            return sentence
        return _clean_for_tts(sentence)

    # 会话切换时重置工作记忆并清除上下文缓存
    global _last_wm_session_id
    if session_id != _last_wm_session_id:
        wm_reset()
        invalidate_context_cache()
        _last_wm_session_id = session_id

    # 快路径链 — lazy import to avoid circular dependency
    from voice_agent import FAST_PATHS
    reply = None
    for path in FAST_PATHS:
        if reply is not None:
            _append_history(_get_history(session_id), text, reply)
            _cache_set(text, reply)
            try:
                import threading as _mem_thread
                _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
            except Exception as e:
                log.debug(f"[brain_stream] fast_path记忆提取线程启动失败: {e}")
            yield (reply, reply)
            return
    cached = _cache_get_interrupted(text, interrupted_reply) if interrupted_reply.strip() else _cache_get(text)
    if cached is not None:
        yield (_maybe_clean(cached), cached)
        return
    # 乐观并发：无关键词命中的文本先走 lite stream，不等 LLM 分类
    _keyword_hit = any(kw in text for kw in _ALL_DOMAIN_KEYWORDS)
    if not _keyword_hit and not interrupted_reply.strip() and not _demo_mode_active():
        # 先更新工作记忆（实体/事实），供 lite stream 的指代消解使用
        update_working_memory(intent="none", topic=text[:20], entity=_extract_entities(text), facts=_extract_facts(text))
        _lite_yielded = False
        for _sent, _full in _chat_lite_stream(text, session_id, channel=channel):
            _lite_yielded = True
            yield (_sent, _full)
        if _lite_yielded:
            return
        log.info("[lite] 乐观并发无产出，回退分类+完整大脑")
        mcp_set = _classify_intent(text)
    else:
        mcp_set = _classify_intent(text)
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
                _cache_set(text, reply)
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
    if mcp_set == "none" and not interrupted_reply.strip():
        _lite_yielded = False
        for _sent, _full in _chat_lite_stream(text, session_id, channel=channel):
            _lite_yielded = True
            yield (_sent, _full)
        if _lite_yielded:
            return
        log.info("[lite] 快速通道无产出，回退完整大脑")
    _orig_sys_msg = None  # feishu_text 模式下会临时替换 system_message，finally 时恢复
    try:
        brain_instance = _get_brain(mcp_set)
    except Exception as e:
        _record_brain_failure(str(e)[:60])
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
    # Channel-aware system message: replace (not append) base voice constraints
    _orig_sys_msg = brain_instance.system_message
    if channel == "feishu_text":
        try:
            from agent.system_msg import _replace_system_msg_for_channel
            brain_instance.system_message = _replace_system_msg_for_channel(_orig_sys_msg, channel)
        except Exception:
            pass

    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist] + [{"role": "user", "content": text}]
    _wm_prompt = _build_wm_anaphor_prompt()
    if _wm_prompt:
        messages.insert(0, {'role': 'system', 'content': _wm_prompt})
    if interrupted_reply.strip():
        messages.insert(-1, _interrupted_context_message(interrupted_reply))
    sent_len = 0
    full_reply = ""
    _brain_start = time.time()
    try:
        for rsp in _brain_run_iter(brain_instance, messages, _BRAIN_RUN_TIMEOUT):
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
                m = _SENTENCE_END.search(unsent)
                if m:
                    sentence = unsent[:m.end()].strip()
                    unsent = unsent[m.end():]
                    if sentence:
                        sent_len = len(full_reply) - len(unsent)
                        cleaned_sentence = _maybe_clean(sentence)
                        if not _is_filler_word(cleaned_sentence):
                            yield (cleaned_sentence, full_reply)
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
                        continue
                break
            sent_len = len(full_reply) - len(unsent)
        _record_brain_success()
    except Exception as e:
        _err = str(e)
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
            full_reply = "抱歉，处理时出错了，请稍后再试。"
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
    _cache_set(text, full_reply, interrupted_reply)
    try:
        import threading as _mem_thread
        _mem_thread.Thread(target=_remember_conversation_async, args=(text, full_reply), daemon=True).start()
    except Exception as e:
        log.debug(f"[brain_stream] 记忆提取线程启动失败: {e}")
    finally:
        # 恢复工作记忆快照：确保当前 session 的修改不污染其他 session
        restore_working_memory(_wm_snapshot)
        # 恢复 brain 的 system_message（feishu_text 模式下已临时替换）
        if channel == "feishu_text" and _orig_sys_msg is not None:
            try:
                brain_instance.system_message = _orig_sys_msg
            except Exception:
                pass

def stream_voice_pipeline(text: str) -> Generator[Tuple[str, str, bytes], None, None]:
    try:
        for sentence, full_reply in brain_stream_sentences(text):
            mp3 = _tts_cleaned_to_mp3(sentence)
            yield ("sentence", sentence, mp3)
    except Exception as e:
        log.error(f"流式流水线异常: {e}")
        yield ("error", str(e)[:60], b"")
    yield ("done", None, b"")
