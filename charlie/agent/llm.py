"""LLM call chain: brain build, intent classification, brain run, streaming.

Extracted from voice_agent.py. All shared state lives in agent/llm_state.py;
this module accesses it through the module object (import agent.llm_state as _st)
so that scalar mutations (_st.brain_failures += 1) are visible to everyone.

Public API (re-exported by voice_agent.py for backward compat):
  - brain_stream_sentences(), stream_voice_pipeline()
  - restart_brain(), reload_brain_config(), brain_status()
  - set_current_user(), get_current_user()
  - _classify_intent(), _get_brain(), _build_brain()
  - _record_brain_failure(), _record_brain_success()
  - _cleanup_brain_processes(), _extract_assistant_text()
"""
import os, sys, json, copy, datetime, time, logging, asyncio, re
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
    active_chat_endpoint,
)
from agent.cache import _cache_get, _cache_set, _cache_get_interrupted
from agent.history import (
    _get_history, _append_history, _save_history,
    _trim_history_tokens,
)
from agent.system_msg import _build_system_msg
from agent.asr_tts import _clean_for_tts, _tts_cleaned_to_mp3
from agent.intent import LOW_INTENT_ASR_REPLY, is_low_intent_asr, is_garbled_asr

log = logging.getLogger("magic")


def _demo_mode_active() -> bool:
    return _demo_mode_active_impl()

def _ollama_online() -> bool:
    return _ollama_online_impl()

def _llm_has_any_key() -> bool:
    from app import env_catalog as _ec
    return _ec.is_configured("GLM_KEY") or _ec.is_configured("ARK_KEY")


# ===== Intent cache helpers =====
def _intent_cache_set(text: str, intent: str):
    with _st.intent_cache_lock:
        _st.intent_cache[text] = (intent, time.time())
        _st.intent_cache.move_to_end(text)
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
    elif "magic" in raw: return "magic-music"
    elif "ac" in raw or "air" in raw or "control" in raw: return "ac-control"
    elif "file" in raw or "fs" in raw: return "filesystem"
    elif "vision" in raw or "mimo" in raw or "screen" in raw or "截图" in raw: return "mimo-vision"
    else: return "none"


def _classify_intent(text: str) -> str:
    """意图分类: 关键词预判 → ARK API, 返回MCP组合名"""
    now = time.time()
    with _st.intent_cache_lock:
        cached = _st.intent_cache.get(text)
        if cached and now - cached[1] < _INTENT_CACHE_TTL:
            _st.intent_cache.move_to_end(text)
            cached_intent = cached[0]
            log.info(f"[intent] 缓存命中: {text[:30]} → {cached_intent}")
            return cached_intent
        if now < _st.intent_disabled_until:
            log.info(f"[intent] 分类冷却中，{_st.intent_disabled_until - now:.0f}秒内默认none")
            return "none"
    _KEYWORD_MAP = [
        ({"天气", "气温", "下雨", "温度", "几度", "今天天气", "明天天气", "今天冷", "今天热"}, "amap-maps"),
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
        ({"外卖", "点餐", "购物", "商品", "查一下", "充电桩", "特斯拉", "出门"}, "magic-life"),
        ({"穿什么", "穿搭", "今天穿", "明天穿", "出门穿", "约会穿", "搭配", "衣橱", "衣服", "加衣服", "外套", "冷了穿", "热了穿", "穿几件"}, "magic-wardrobe"),
        ({"做菜", "菜谱", "食谱", "做什么菜", "食材", "吃什么", "做饭", "怎么做", "做法", "怎么煮", "怎么炒", "今天吃啥", "今晚吃啥", "中午吃啥", "推荐个菜", "推荐一道菜", "凉菜", "热菜", "汤", "主食", "下饭", "买菜", "番茄炒蛋", "可乐鸡翅"}, "magic-recipe"),
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
    for kw_set, _ in _KEYWORD_MAP:
        _ALL_DOMAIN_KEYWORDS |= kw_set
    if len(text) <= 6 and not any(kw in text for kw in _ALL_DOMAIN_KEYWORDS):
        log.info(f"[intent] 闲聊短句跳过LLM: '{text}' → none")
        _intent_cache_set(text, "none")
        return "none"
    for keywords, mcp_name in _KEYWORD_MAP:
        if any(kw in text for kw in keywords):
            log.info(f"[intent] 关键词命中: '{text[:30]}' → {mcp_name}")
            try:
                from app.audit_log import audit_log
                audit_log("intent", input_data=text, output_data=mcp_name,
                          action="keyword_match", session_id="intent")
            except Exception:
                pass
            _intent_cache_set(text, mcp_name)
            return mcp_name
    prompt = (
        "任务: 判断用户输入需要哪个工具, 只回一个词\n"
        "选项: none | amap-maps | baize-skills | filesystem | magic-music | magic-reminder | magic-notes | magic-system | magic-info | magic-life | ac-control | magic-recipe\n"
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
    try:
        _ep_base, _ep_key, _ep_model = active_chat_endpoint()
        r = _session.post(f"{_ep_base}/chat/completions",
            json={"model": _ep_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False,
                  "max_tokens": 10, "temperature": 0},
            headers={"Authorization": f"Bearer {_ep_key}"},
            timeout=(2, 5))
        raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip().lower()
        mcp = _normalize_intent(raw)
        with _st.intent_cache_lock:
            _st.intent_failures = 0
            _st.intent_disabled_until = 0.0
        _intent_cache_set(text, mcp)
        log.info(f"[intent] '{text[:30]}' → {mcp} ({raw[:15]})")
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
        try:
            r = _session.post(f"{OLLAMA_HOST}/api/chat",
                json={"model": OLLAMA_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "think": False,
                      "options": {"num_predict": 10, "temperature": 0}},
                timeout=(3, 15))
            raw = r.json().get("message", {}).get("content", "").strip().lower()
            mcp = _normalize_intent(raw)
            with _st.intent_cache_lock:
                _st.intent_failures = 0
                _st.intent_disabled_until = 0.0
            _intent_cache_set(text, mcp)
            log.info(f"[intent] '{text[:30]}' → {mcp} ({raw[:15]}) [重试成功]")
            return mcp
        except Exception as e2:
            with _st.intent_cache_lock:
                _st.intent_failures += 1
                if _st.intent_failures >= INTENT_FAILURE_THRESHOLD:
                    _st.intent_disabled_until = now + INTENT_FAILURE_COOLDOWN
                    log.warning(f"[intent] 连续失败{_st.intent_failures}次，暂停本地分类{INTENT_FAILURE_COOLDOWN:g}秒")
            log.warning(f"[intent] 本地分类重试仍失败,默认none: text='{text[:20]}' err={e2}")
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
    log.info(f"[brain] 构建大脑 mcp_set={mcp_set}, 启用{len(mcp_servers)}个MCP: {list(mcp_servers.keys())}")
    tools = [{"mcpServers": mcp_servers}] if mcp_servers else []
    brain = Assistant(llm=llm_cfg, name='Charlie',
        system_message=_build_system_msg(mcp_set),
        function_list=tools)
    _install_openai_compat(brain)
    return brain

def _get_brain(mcp_set="none"):
    with _st.brain_lock:
        if mcp_set not in _st.brains:
            _st.brains[mcp_set] = _build_brain(mcp_set)
            if not _st.brain_build_time:
                _st.brain_build_time = time.time()
            log.info(f"[brain] 大脑构建完成: mcp={mcp_set}, 缓存总数={len(_st.brains)}")
        brain = _st.brains[mcp_set]
    brain.system_message = _build_system_msg(mcp_set)
    return brain

def _ensure_event_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

def _record_brain_failure(error: str = ""):
    with _st.brain_lock:
        _st.brain_failures += 1
        _st.brain_total_failures += 1
        _st.brain_last_failure = time.time()
        is_429 = '429' in error or 'Too Many' in error or 'rate' in error.lower()
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

def _record_brain_success():
    with _st.brain_lock:
        _st.brain_failures = 0
        _st.brain_last_success = time.time()

def _cleanup_brain_processes(brain_instance):
    if brain_instance is None:
        return
    try:
        if hasattr(brain_instance, '_function_list'):
            for func in (brain_instance._function_list or []):
                if isinstance(func, dict) and 'mcpServers' in func:
                    for name, cfg in func['mcpServers'].items():
                        try:
                            if hasattr(brain_instance, '_mcp_clients') and name in (brain_instance._mcp_clients or {}):
                                client = brain_instance._mcp_clients[name]
                                if hasattr(client, 'close'):
                                    client.close()
                                log.info(f"[brain] MCP客户端已关闭: {name}")
                        except Exception as e:
                            log.debug(f"[brain] 关闭MCP客户端 {name} 失败: {e}")
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
                    except Exception as e:
                        log.debug(f"[brain] kill fallback 也失败: {name}: {e}")
    except Exception as e:
        log.debug(f"[brain] MCP清理异常: {e}")

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

def _ollama_fallback(text: str, messages: list) -> str:
    try:
        import requests as _req
        recent = messages[-6:] if len(messages) > 6 else messages
        ollama_msgs = [{"role": m["role"], "content": m["content"]} for m in recent]
        for m in ollama_msgs:
            if isinstance(m.get("content"), str) and m["content"].startswith("__MUSIC__"):
                m["content"] = "[音乐播放中]"
        if not ollama_msgs or ollama_msgs[0]["role"] != "system":
            ollama_msgs.insert(0, {"role": "system", "content": "你是Charlie，搭档级AI助理。直接、偶尔幽默、不废话。不知道就说不知道，不编造。别输出占用语，第一句就是答案。"})
        r = _req.post(f"{OLLAMA_HOST}/api/chat", json={
            "model": OLLAMA_MODEL,
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
            _mem.extract_memories(user_text, assistant_reply)
    except Exception as e:
        log.debug(f"[memory] 记忆提取跳过: {e}")


# ===== LLM path (non-streaming) =====
def _brain_llm(text: str, session_id: str = "default") -> str:
    mcp_set = _classify_intent(text)
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
    _ensure_event_loop()
    from app import llm_config as _llm_cfg
    _glm_tries = len(getattr(_llm_cfg, "GLM_MODELS", []) or [None]) if _llm_cfg.is_glm_configured() and not _llm_cfg.is_ark_configured() else 1
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
            return f"大脑启动失败：{str(e)[:80]}。请稍后重试。"
        hist = _get_history(session_id)
        messages = [{'role': m['role'], 'content': m['content']} for m in hist] + [{'role': 'user', 'content': text}]
        try:
            for rsp in brain_instance.run(messages):
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
            err = str(e)
            is_429 = (_RateLimitError is not None and isinstance(e, _RateLimitError)) or \
                     '429' in err or 'Too Many' in err or '1305' in err or \
                     'rate' in err.lower() or '频率' in err
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
            _record_brain_failure(err[:60])
            ollama_reply = _ollama_fallback(text, messages)
            if ollama_reply:
                log.info(f"[brain] Finna失败, Ollama降级成功: {ollama_reply[:30]}")
                _append_history(hist, text, ollama_reply)
                _cache_set(text, ollama_reply)
                return ollama_reply
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
    try:
        import threading as _mem_thread
        _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
    except Exception as e:
        log.debug(f"[brain_llm] 记忆提取线程启动失败: {e}")
    return reply


# ===== Streaming brain =====
def brain_stream_sentences(text: str, session_id: str = "default", interrupted_reply: str = "") -> Generator[Tuple[str, str], None, None]:
    _FILLER_WORDS = {
        "让我想想", "稍等一下", "稍等", "让我思考一下", "想一想",
        "让我看看", "我看看", "好的", "好", "嗯嗯", "嗯",
        "让我想一下", "我想想", "等一下", "稍等下",
    }
    def _is_filler(s: str) -> bool:
        cleaned = re.sub(r'[，。！？\s]', '', s).strip()
        is_filler = bool(cleaned) and len(cleaned) <= 6 and cleaned in _FILLER_WORDS
        return is_filler

    # 快路径链 — lazy import to avoid circular dependency
    from voice_agent import FAST_PATHS
    for path in FAST_PATHS:
        reply = path.run(text, session_id)
        if reply is not None:
            yield (reply, reply)
            return
    cached = _cache_get_interrupted(text, interrupted_reply) if interrupted_reply.strip() else _cache_get(text)
    if cached is not None:
        yield (_clean_for_tts(cached), cached)
        return
    mcp_set = _classify_intent(text)
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
        yield (message, message)
        return
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
            message = f"大脑启动失败：{str(e)[:80]}。请稍后重试。"
        yield (message, message)
        return
    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist] + [{"role": "user", "content": text}]
    if interrupted_reply.strip():
        messages.insert(-1, _interrupted_context_message(interrupted_reply))
    sent_len = 0
    full_reply = ""
    try:
        for rsp in brain_instance.run(messages):
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
                        cleaned_sentence = _clean_for_tts(sentence)
                        if not _is_filler(cleaned_sentence):
                            yield (cleaned_sentence, full_reply)
                    continue
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
        _err = str(e)
        is_429 = '429' in _err or 'Too Many' in _err or '1305' in _err or 'rate' in _err.lower()
        is_conn = 'Connection' in _err or 'closed' in _err or 'RemoteDisconnected' in _err or 'reset' in _err.lower()
        _record_brain_failure(_err[:60])
        if (is_429 or is_conn) and not full_reply:
            from app import llm_config as _llm_cfg
            if _llm_cfg.is_glm_configured() and not _llm_cfg.is_ark_configured():
                log.warning(f"[brain] 流式失败({_err[:30]})，回退非流式 + 模型轮换")
                if is_429:
                    _llm_cfg.rotate_glm_model()
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
            cleaned_remaining = _clean_for_tts(remaining)
            if not _is_filler(cleaned_remaining):
                yield (cleaned_remaining, full_reply)
    elif not full_reply:
        full_reply = "我没听明白"
        yield (full_reply, full_reply)
    _append_history(hist, text, full_reply)
    if session_id != "default":
        _trim_history_tokens(hist, session_id=session_id)
    _cache_set(text, full_reply, interrupted_reply)

def stream_voice_pipeline(text: str) -> Generator[Tuple[str, str, bytes], None, None]:
    try:
        for sentence, full_reply in brain_stream_sentences(text):
            mp3 = _tts_cleaned_to_mp3(sentence)
            yield ("sentence", sentence, mp3)
    except Exception as e:
        log.error(f"流式流水线异常: {e}")
        yield ("error", str(e)[:60], b"")
    yield ("done", None, b"")
