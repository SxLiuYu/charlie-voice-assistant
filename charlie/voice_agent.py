"""
Charlie - 语音Agent核心 (精简入口)

voice_agent.py 现在是公共 API 入口，实际逻辑在 agent/ 子模块:
  - agent/llm_state.py: 共享状态（连接池、大脑缓存、意图缓存等）
  - agent/llm.py:       LLM 调用链（意图分类、大脑构建、流式生成等）
  - agent/music.py:     音乐快路径
  - agent/weather.py:   天气快路径
  - agent/vision.py:    视觉快路径
  - agent/device_control.py: 设备控制快路径

语音闭环: ASR(百度) → 大脑(ARK+Qwen-Agent+MCP) → TTS(百度)
连接韧性: Session复用 + 自动重试 + 异常降级
对话记忆: 跨请求保留历史上下文，支持多轮连续对话，持久化到磁盘
"""
import os, sys, json, datetime, time, logging, re
from typing import Optional, Generator, Tuple, List, Dict, Any, Callable

try:
    from dotenv import load_dotenv
    _dotenv_path = os.path.join(os.path.dirname(sys.executable), ".env") if getattr(sys, "frozen", False) else None
    load_dotenv(_dotenv_path) if _dotenv_path else load_dotenv()
except ImportError:
    pass

if not getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", PROJECT_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# ===== 共享状态 + LLM 调用链 (from agent/ submodules) =====
import agent.llm_state as _st
from agent.llm_state import (
    FINNA, ARK_BASE, ARK_KEY, ARK_MODEL,
    EMPTY_ASR_TEXT, EMPTY_ASR_REPLY,
    INTENT_FAILURE_THRESHOLD, INTENT_FAILURE_COOLDOWN,
    session as _session, OLLAMA_SIMPLE_SYSTEM_MSG,
)
from agent.llm import (
    _demo_mode_active, _llm_has_any_key,
    _intent_cache_set, intent_classifier_status, _normalize_intent,
    _classify_intent, _wrap_openai_create_unknown_kwargs, _install_openai_compat,
    _build_brain, _get_brain, _ensure_event_loop,
    _record_brain_failure, _record_brain_success, _cleanup_brain_processes,
    restart_brain, reload_brain_config, set_current_user, get_current_user,
    brain_status, _extract_assistant_text,
    _interrupted_context_message, _remember_conversation_async,
    _brain_llm, brain_stream_sentences, stream_voice_pipeline,
)

# ===== 快路径函数已拆分到 agent/ 子模块 =====
from agent.music import direct_music_play as _direct_music_play
from agent.weather import direct_weather_play as _direct_weather_play
from agent.vision import direct_vision_analyze as _direct_vision_analyze
from agent.vision import direct_camera_vision_analyze as _direct_camera_vision_analyze
from agent.device_control import (
    direct_ac_control as _direct_ac_control,
    set_volume as _set_volume,
    set_volume_abs as _set_volume_abs,
    get_volume_pct as _get_volume_pct,
    mute_volume as _mute_volume,
    sleep_display as _sleep_display,
    handle_smart_command as _handle_smart_command,
)

# ===== 从其他 agent/ 子模块导入 =====
from app.llm_config import active_chat_endpoint
from agent.intent import LOW_INTENT_ASR_REPLY, is_low_intent_asr, is_garbled_asr
from agent.cache import _cache_get, _cache_set, _cache_get_interrupted, _cache_lock, _cache, _CACHE_TTL, _CACHE_MAX
from agent.history import (
    # 注意：不要在此快照 import history 的可变全局（_history/HISTORY_FILE 等），
    # 多用户切换后这些别名会变陈旧；需要时一律动态引用 agent.history 的函数
    _get_history_file, _history_file_sig, _locked_history_file, _read_history_file_locked, _read_history_file,
    _get_history, _history_snapshot, _searchable_history, _session_summaries,
    _save_history, _append_history, _load_history, _estimate_tokens, _estimate_msg_tokens, _trim_history_tokens,
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
    _asr_fallback_times, _asr_lock, TTSUnavailableError,
    _clean_for_tts, _tts_cache_get, _tts_cache_put,
    tts, tts_status, _tts_cleaned_to_mp3, tts_to_mp3,
    asr, asr_race, _vosk_asr, _baidu_get_token, _asr_baidu, _tts_baidu,
    runtime_audio_path, write_audio_file, runtime_temp_audio_path,
    _TTS_BOLD_RE, _TTS_HEADER_RE, _TTS_BLOCKQUOTE_RE, _TTS_TABLE_PIPE_RE,
    _TTS_CODE_BLOCK_RE, _TTS_INLINE_CODE_RE, _TTS_LIST_ITEM_RE, _TTS_MARKDOWN_LINK_RE, _TTS_WHITESPACE_RE,
)
from agent.state import update_user_state, get_user_state, _infer_user_state, _user_state, _user_state_lock
from agent.system_msg import _build_system_msg, _build_user_profile, invalidate_system_msg_cache, _build_tools_doc

# ===== 向后兼容: 共享状态别名 (tests 和外部代码引用 voice_agent._xxx) =====
# 可变对象 (dict, lock, OrderedDict) — 直接引用同一对象
_brains = _st.brains
_brain_lock = _st.brain_lock
_intent_cache = _st.intent_cache
_intent_cache_lock = _st.intent_cache_lock
_brain_build_time = _st.brain_build_time  # float, 读取 OK; 写入需通过 _st
_MAX_BRAIN_FAILURES = _st._MAX_BRAIN_FAILURES
_INTENT_CACHE_MAX = _st._INTENT_CACHE_MAX
_INTENT_CACHE_TTL = _st._INTENT_CACHE_TTL
_UNKNOWN_KWARG_RE = _st.UNKNOWN_KWARG_RE
_SENTENCE_END = _st.SENTENCE_END
_COMMA_SOFT = _st.COMMA_SOFT
_MIN_CHUNK = _st.MIN_CHUNK
_MAX_CHUNK = _st.MAX_CHUNK


def __getattr__(name: str):
    """Module-level __getattr__: delegate scalar state reads to agent.llm_state,
    and history shared-state reads to agent.history.

    Tests and code that READ voice_agent._brain_failures, voice_agent._intent_failures,
    voice_agent._intent_disabled_until etc. get the live value from llm_state.
    Tests that WRITE these scalars must set them on agent.llm_state directly.
    History 名字不在 import 期快照绑定（多用户切换后静态别名会陈旧），
    统一经此动态转发到 agent.history 的当前值。
    """
    _STATE_NAMES = {
        '_brain_failures', '_brain_total_failures', '_brain_last_failure',
        '_brain_last_success', '_current_user_id',
        '_intent_failures', '_intent_disabled_until',
    }
    _HISTORY_NAMES = {
        '_history', '_sessions', 'MAX_HISTORY', 'MAX_SESSIONS',
        '_history_lock', 'HISTORY_FILE', 'HISTORY_LOCK_FILE',
    }
    if name in _STATE_NAMES:
        # map voice_agent._xxx → agent.llm_state.xxx
        _mapped = name
        if name == '_current_user_id':
            _mapped = 'current_user_id'
        elif name.startswith('_brain_'):
            _mapped = 'brain' + name[6:]  # _brain_failures → brain_failures
        elif name.startswith('_intent_'):
            _mapped = 'intent' + name[7:]  # _intent_failures → intent_failures
        return getattr(_st, _mapped)
    if name in _HISTORY_NAMES:
        from agent import history as _hist_mod
        # 路径类名字必须实时重算：env 可能被测试/运维代码直接改写，
        # 而模块级导出仅在 set_current_user 时同步——静态读会得到陈旧路径
        if name == 'HISTORY_FILE':
            return _hist_mod._get_history_file()
        if name == 'HISTORY_LOCK_FILE':
            return _hist_mod._get_history_lock_file()
        return getattr(_hist_mod, name)
    raise AttributeError(f"module 'voice_agent' has no attribute {name!r}")


# ===== 角色切换快路径 =====
# 模式在匹配前统一 lower()，英文名（charlie/jarvis）靠此命中
_ROLE_SWITCH_PATTERNS = (
    (("切换到贾维斯", "变成贾维斯", "切换贾维斯", "我要贾维斯", "换成贾维斯",
      "切换回贾维斯", "切回贾维斯", "变回贾维斯", "回到贾维斯", "切换到jarvis", "切换回jarvis",
      "切到jarvis", "换成jarvis"), "jarvis", "贾维斯"),
    (("切换到查理", "变成查理", "切换查理", "回到查理", "回到默认", "切换到默认",
      "切换回查理", "切回查理", "变回查理", "切换到charlie", "切换回charlie",
      "切到charlie", "换成charlie", "恢复默认", "切换回默认"), "charlie", "Charlie"),
    (("切换到白泽", "变成白泽", "切换白泽", "我要白泽", "换成白泽",
      "切换回白泽", "切回白泽", "变回白泽"), "baize", "白泽"),
)


def _role_switch_handler(text: str) -> str | None:
    """角色切换关键词直连 switch_role，不进 LLM（轻量通道无工具，必须在此拦截）。"""
    text_lower = text.lower()
    for patterns, role_id, role_label in _ROLE_SWITCH_PATTERNS:
        if any(p in text_lower for p in patterns):
            try:
                from agent.roles import switch_role
                switch_role(role_id)
                return f"好的，已切换为{role_label}。"
            except Exception as e:
                log.warning(f"[role_switch] 切换失败: {e}")
                return "角色切换失败了，请稍后再试。"
    return None


# ===== 社交礼貌快路径 =====
_SOCIAL_REPLIES = {
    "谢谢": "不客气。",
    "多谢": "不客气。",
    "辛苦了": "应该的。",
    "再见": "再见，随时叫我。",
    "拜拜": "再见，随时叫我。",
    "你好": "嗯，我在。",
    "您好": "嗯，我在。",
    "在吗": "我在。",
    "在不在": "我在。",
    "你在吗": "我在。",
    "在家吗": "我在。",
}


def _social_reply_handler(text: str) -> str | None:
    """社交礼貌语直返固定短句，不进 LLM。

    仅限纯社交短句：去掉标点后 >6 字、或含任何领域关键词（天气/提醒等）
    的复合语句一律返回 None 回退，避免"谢谢，今天天气怎么样"被吞掉真实意图。
    """
    import re as _re
    try:
        from agent.llm import _ALL_DOMAIN_KEYWORDS
        if any(kw in text for kw in _ALL_DOMAIN_KEYWORDS):
            return None
    except Exception as e:
        log.debug(f"[voice_agent] error at line 187: {e}")
        pass
    cleaned = _re.sub(r'[，。！？!?,.\s]', '', text)
    if len(cleaned) > 6:
        return None
    for keyword, reply in _SOCIAL_REPLIES.items():
        if keyword in cleaned:
            return reply
    return None


# ===== 决策反馈检测 =====
def _decision_feedback_handler(text: str, session_id: str = "default") -> str | None:
    """检测用户对上轮决策的反馈（确认/取消/修改），匹配则执行并返回回复"""
    try:
        from app import load_magic_module
        _dec = load_magic_module("magic_decisions")
        if _dec:
            return _dec.check_feedback(text, session_id)
    except Exception as e:
        log.debug(f"[decision] 反馈检测跳过: {e}")
    return None


# ===== 用户画像捕获（USER_PROFILE_ENABLED / AUTOSTORE 门控，默认不触发、零行为变更）=====
def _capture_profile(text: str, session_id: str, assistant_reply: str) -> None:
    """把本轮对话里的用户画像信号落库，喂给 user_profile 门面的读路径。

    默认全不触发（两个开关都默认 0）。只有在明确开启后才会有副作用：
      - USER_PROFILE_ENABLED=1：解析用户显式画像指令（我喜欢X/我住在X/说简短点/
        记住X）→ record_explicit 立即路由落库（写的是既有库，非新增存储）。
      - USER_PROFILE_AUTOSTORE=1：每轮后台抽取沉淀 → apply_extracted（含敏感闸）。

    纯后台动作，绝不影响返回值与延迟；任何异常都静默吞掉。
    """
    if os.getenv("USER_PROFILE_ENABLED") != "1" and os.getenv("USER_PROFILE_AUTOSTORE") != "1":
        return
    try:
        import threading as _pt
        import contextvars as _cv
        _ctx = _cv.copy_context()
        from agent.user_profile import record_explicit, apply_extracted

        def _run() -> None:
            try:
                if os.getenv("USER_PROFILE_ENABLED") == "1":
                    record_explicit(session_id, text)
                if os.getenv("USER_PROFILE_AUTOSTORE") == "1":
                    apply_extracted(session_id, text, assistant_reply)
            except Exception as _e:
                log.debug(f"[user_profile] 捕获落库跳过: {_e}")

        _pt.Thread(target=lambda: _ctx.run(_run), daemon=True).start()
    except Exception as _e:
        log.debug(f"[user_profile] 捕获线程启动跳过: {_e}")


# ===== Laya + Needle3 工具快路径 =====
def _laya_tool_fastpath(text: str, session_id: str = "default") -> str | None:
    """Laya 中文意图路由 + Needle3 英文工具执行的端侧快路径（LAYA_TOOL_ENABLED=1）。

    在 LLM 之前拦截中文**只读/可逆**工具意图，由 Laya+Needle3 双引擎直接解析
    并执行 charlie 真实工具，省一次云端 LLM fncall 循环。写操作（add_reminder/
    play_music）默认只规划不执行；不确定/澄清/闲聊都返回 None 落回 _brain_llm。

    默认关闭（不进生产路径）；这是 decider-first 精神在工具侧的延伸。所有异常
    静默兜底为 None——它在语音热路径上，绝不能因为它崩了而让用户没话。
    """
    if os.getenv("LAYA_TOOL_ENABLED") != "1":
        return None
    try:
        from scripts.laya_needle_bridge import LayaNeedleBridge
        bridge = LayaNeedleBridge()
        plan = bridge.execute(text, allow_writes=False)
        action = plan.get("action")
        # 只有真实执行成功（只读/可逆工具）才采信；其余一律落回 LLM
        if action == "call" and plan.get("executed") is True:
            result = plan.get("result") or ""
            log.info(f"[laya_tool] 端侧工具命中 {plan.get('name')} "
                     f"(intent={plan.get('intent')}, {plan.get('latency_ms')}ms)")
            return str(result)
        if action == "clarify":
            # 槽位缺失：回澄清问句，让 LLM 后续轮接住
            return plan.get("clarify_question")
        # action == "chitchat" / 未执行：静默落回 LLM
        return None
    except Exception as e:
        log.warning(f"[laya_tool] 快路径异常，落回 LLM: {e}")
        return None


# ===== brain() 公共入口 =====
def brain(text: str, session_id: str = "default") -> str:
    """大脑推理: 系统控制快路径(角色切换/社交) → LLM 意图分类 → MCP 工具调用。"""
    import time as _t
    _start = _t.time()
    log.debug(f"[brain] 收到请求: {text[:50]}")
    try:
        # 多轮指代消解（MULTITURN_COREF_ENABLED=1）：把当前句的"它/那个/那首歌"
        # 等就近还原成字面实体，喂给后续所有路径（含 Laya 快路径、LLM）。默认关，
        # 关时 text 原样透传、零行为变更。
        if os.getenv("MULTITURN_COREF_ENABLED") == "1":
            try:
                from agent.coreference import resolve_for_session
                _resolved, _changed, _map = resolve_for_session(text, session_id)
                if _changed:
                    log.info(f"[coref] 消解: {text!r} → {_resolved!r} {_map}")
                    text = _resolved
            except Exception as _e:
                log.debug(f"[coref] 消解跳过，原句透传: {_e}")
        _cmd_reply = _handle_smart_command(text)
        if _cmd_reply is not None:
            _append_history(_get_history(session_id), text, _cmd_reply)
            from app.audit_log import audit_log
            audit_log("brain", input_data=text, output_data=_cmd_reply,
                      action="smart_command", session_id=session_id,
                      duration_ms=(_t.time()-_start)*1000)
            return _cmd_reply
        reply = _decision_feedback_handler(text, session_id)
        if reply is not None:
            from app.audit_log import audit_log
            audit_log("brain", input_data=text, output_data=reply,
                      action="decision_feedback", session_id=session_id,
                      duration_ms=(_t.time()-_start)*1000)
            return reply
        for path in FAST_PATHS:
            reply = path.run(text, session_id)
            if reply is not None:
                from app.audit_log import audit_log
                audit_log("brain", input_data=text, output_data=reply,
                          action=f"fast_path:{path.name}", session_id=session_id,
                          duration_ms=(_t.time()-_start)*1000)
                try:
                    from agent.llm import _remember_conversation_async
                    import threading as _mem_thread
                    _mem_thread.Thread(target=_remember_conversation_async, args=(text, reply), daemon=True).start()
                except Exception as e:
                    log.debug(f"[brain] fast_path记忆提取线程启动失败: {e}")
                return reply
        cached = _cache_get(text, session_id)
        if cached is not None:
            log.info(f"[cache] 命中: {text[:20]}")
            from app.audit_log import audit_log
            audit_log("brain", input_data=text, output_data=cached,
                      action="cache_hit", session_id=session_id,
                      duration_ms=(_t.time()-_start)*1000)
            return cached
        # 摄像头源名兜底：文本包含配置的源名（如"帮我看看院子"命中"院子"）时
        # 直接抓帧分析，避免掉进 LLM 被历史上下文带偏
        try:
            from agent.vision import try_camera_by_source_name
            _cam_reply = try_camera_by_source_name(text, session_id)
            if _cam_reply:
                _append_history(_get_history(session_id), text, _cam_reply)
                return _cam_reply
        except Exception as e:
            log.debug(f"[brain] 摄像头源名匹配跳过: {e}")
        # 乱码/纯符号输入直接兜底（置于快路径之后：2字场景词"晚安/早安"会被
        # garbled 判定误杀，而快路径是明确关键词匹配，乱码匹配不上自然落到这里）
        if is_garbled_asr(text):
            return LOW_INTENT_ASR_REPLY
        # Laya + Needle3 工具快路径（LAYA_TOOL_ENABLED=1）：中文工具意图在 LLM
        # 之前由端侧双引擎直接解析并执行**只读/可逆**工具，省一次云端 LLM fncall
        # 循环；写操作/不确定/澄清则落回 _brain_llm（LLM 的 fncall 或澄清）。
        # 默认关闭，不进入生产路径；启用即 decider-first 精神的工具侧延伸。
        _lt = _laya_tool_fastpath(text, session_id)
        if _lt is not None:
            return _lt
        # 用户画像只读观测（USER_PROFILE_ENABLED=1，P1）：把聚合后的统一画像
        # 记进审计日志，**只读不改行为**，用于灰度前看画像覆盖率/质量。默认关。
        if os.getenv("USER_PROFILE_ENABLED") == "1":
            try:
                from agent.user_profile import get_user_profile
                from app.audit_log import audit_log
                _prof = get_user_profile(session_id)
                audit_log("user_profile", input_data=text, output_data=_prof,
                          action="profile_view", session_id=session_id,
                          duration_ms=(_t.time()-_start)*1000)
            except Exception as _e:
                log.debug(f"[user_profile] 画像观测跳过: {_e}")
        result = _brain_llm(text, session_id)
        from app.audit_log import audit_log
        audit_log("brain", input_data=text, output_data=result,
                  action="llm", session_id=session_id,
                  duration_ms=(_t.time()-_start)*1000)
        # 用户画像捕获（开关门控，默认不触发）：本轮显式/自动信号落库，喂读路径。
        _capture_profile(text, session_id, result)
        return result
    except Exception as e:
        from app.audit_log import audit_log
        audit_log("brain", input_data=text, success=False, error=str(e),
                  action="error", session_id=session_id,
                  duration_ms=(_t.time()-_start)*1000)
        raise


def _load_magic_module(name: str, filename: str = None):
    """加载 magic-*.py 模块 — 委托到 app.load_magic_module"""
    from app import load_magic_module
    return load_magic_module(name, filename)


class FastPath:
    """快路径：关键词命中 → 直连 handler → 返回或回退 None"""
    def __init__(self, name: str, keywords: tuple, handler_name: str, exclude: tuple = (),
                 case_insensitive: bool = False):
        self.name = name
        self.keywords = keywords
        self.handler_name = handler_name
        self.exclude = exclude
        self.case_insensitive = case_insensitive

    def match(self, text: str) -> bool:
        if self.case_insensitive:
            text = text.lower()
            keywords = [kw.lower() for kw in self.keywords]
            exclude = [ex.lower() for ex in self.exclude]
        else:
            keywords = self.keywords
            exclude = self.exclude
        if not any(kw in text for kw in keywords):
            return False
        if self.exclude and any(ex in text for ex in exclude):
            return False
        return True

    def run(self, text: str, session_id: str = "default") -> str | None:
        if not self.match(text):
            return None
        import time as _t
        _start = _t.time()
        log.info(f"[{self.name}] 关键词命中: {text[:20]}")
        try:
            _module = sys.modules[__name__]
            handler = getattr(_module, self.handler_name)
            try:
                # 新式 handler 接收 session_id（多轮上下文，如摄像头追问沿用上次源）
                reply = handler(text, session_id)
            except TypeError:
                reply = handler(text)
        except AttributeError:
            log.warning(f"[{self.name}] handler '{self.handler_name}' 不存在")
            return None
        except Exception as e:
            log.warning(f"[{self.name}] handler 异常: {e}")
            from app.audit_log import audit_log
            audit_log(f"fast_path:{self.name}", input_data=text, success=False,
                      error=str(e), action=self.handler_name, session_id=session_id,
                      duration_ms=(_t.time()-_start)*1000)
            return None
        from app.audit_log import audit_log
        audit_log(f"fast_path:{self.name}", input_data=text, output_data=reply,
                  action=self.handler_name, session_id=session_id,
                  duration_ms=(_t.time()-_start)*1000)
        if reply:
            _append_history(_get_history(session_id), text, reply)
            _capture_profile(text, session_id, reply)
            return reply
        log.info(f"[{self.name}] 未命中或失败，回退")
        return None


def _time_handler(text: str) -> str:
    """本地时间/日期/星期应答（零 LLM）。

    只接**纯时间问句**。复合句（"现在几点了顺便放首歌"）不在匹配表，
    仍走意图分类。节日/农历/生肖不本地算（"春节是几号"≠今天日期）。
    """
    if len(text) >= 16:
        return None
    # 农历/阴历/生肖不是 datetime.now() 能答的
    if any(k in text for k in ("农历", "阴历", "属相", "生肖", "黄历")):
        return None
    # 外国城市几点 ≠ 本地时间（伦敦现在几点 ≠ 北京时间）
    _foreign = ("伦敦", "纽约", "美国", "日本", "东京", "巴黎", "英国", "韩国",
                "首尔", "莫斯科", "悉尼", "加州", "洛杉矶", "柏林", "泰国", "曼谷")
    if any(k in text for k in _foreign) and "北京" not in text and "中国" not in text:
        return None
    # Demo 模式（未配置任何 LLM key）下不本地应答，落回 demo 引导提示 ——
    # demo 的语义是"引导完成配置"，此时给真实时间会掩盖"服务未就绪"。
    from agent.llm import _demo_mode_active
    if _demo_mode_active():
        return None
    now = datetime.datetime.now()
    weekday_names = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    # 上午/下午/早上/晚上
    if any(k in text for k in ("上午还是下午", "早上还是晚上", "白天还是晚上", "上午还是晚上")):
        h = now.hour
        if 5 <= h < 8:
            return "现在是早上。"
        if 8 <= h < 11:
            return "现在是上午。"
        if 11 <= h < 13:
            return "现在是中午。"
        if 13 <= h < 18:
            return "现在是下午。"
        if 18 <= h < 23:
            return "现在是晚上。"
        return "现在是凌晨。"
    # 年份（生肖/农历不接）
    if any(k in text for k in ("哪一年", "今年年份", "今年是多少年", "现在是哪一年", "今年是哪年")):
        if any(k in text for k in ("属", "农历", "生肖")):
            return None
        return f"今年是{now.year}年。"
    # 星期（今天/明天/昨天/后天）
    if "星期" in text or "礼拜" in text:
        delta = 0
        if "明天" in text:
            delta = 1
        elif "昨天" in text:
            delta = -1
        elif "后天" in text:
            delta = 2
        wd = weekday_names[(now.weekday() + delta) % 7]
        if delta == 0:
            return f"今天是{wd}。"
        if delta == 1:
            return f"明天是{wd}。"
        if delta == -1:
            return f"昨天是{wd}。"
        return f"后天是{wd}。"
    # 日期 —— 只接今天/现在，"春节是几号"不走这里
    if any(k in text for k in ("几号", "多少号", "日期", "几月几号", "几月几日")):
        if "今天" in text or "现在" in text:
            return f"今天是{now.strftime('%Y年%m月%d日')}。"
        return None
    # 默认：几点
    return f"现在{now.strftime('%H点%M分')}。"


def _math_fast(text: str, session_id: str = "default") -> str | None:
    """纯算式本地求值（X加Y / X+Y / 乘 / 除），复合算式落回 LLM。"""
    t = (text or "").strip().rstrip("？?。!！ ")
    if not t or any(k in t for k in ("帮我", "为什么", "英语", "歌", "天气", "几点")):
        return None
    for _p in ("口算", "算一下", "帮我算", "计算"):
        if t.startswith(_p):
            t = t[len(_p):].strip()
            break
    # 中文数字口算：三十七乘以八十九 → 37*89（ASR 常出中文数字）
    if re.search(r"[零一二两三四五六七八九十百千万]", t):
        _CN = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
               "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}

        def _cn_num(m: "re.Match") -> str:
            s = m.group(0)
            if s in _CN:
                return _CN[s]
            total, cur = 0, 0
            for c in s:
                if c in _CN:
                    cur = int(_CN[c])
                elif c == "十":
                    total += (cur or 1) * 10
                    cur = 0
                elif c == "百":
                    total += (cur or 1) * 100
                    cur = 0
                elif c == "千":
                    total += (cur or 1) * 1000
                    cur = 0
                elif c == "万":
                    total = (total + cur) * 10000
                    cur = 0
            return str(total + cur)

        t = re.sub(r"[零一二两三四五六七八九十百千万]+", _cn_num, t)
    m = re.match(
        r'^(-?\d+)\s*([+\-*/x×÷]|加|减|乘以|乘|除以)\s*(-?\d+)\s*(?:等于多少|等于几|是多少|等于)?\s*$', t)
    if not m:
        return None
    try:
        a = int(m.group(1))
        b = int(m.group(3))
        op = m.group(2)
        if op in ("+", "加"):
            return f"{a}加{b}等于{a + b}。"
        if op in ("-", "减"):
            return f"{a}减{b}等于{a - b}。"
        if op in ("*", "x", "×", "乘", "乘以"):
            return f"{a}乘{b}等于{a * b}。"
        if op in ("/", "÷", "除以"):
            if b == 0:
                return None
            if a % b == 0:
                return f"{a}除以{b}等于{a // b}。"
            val = round(a / b, 4)
            return f"{a}除以{b}等于{val}。"
    except Exception:
        return None
    return None


def _cn_int(text: str) -> int | None:
    """中文数字 0-100 → int。"五十"→50，"一百"→100。失败 None。"""
    t = (text or "").strip()
    if t.isdigit():
        return int(t) if 0 <= int(t) <= 100 else None
    d = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
         "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if t == "十":
        return 10
    if t == "一百" or t == "百":
        return 100
    total, cur = 0, 0
    for c in t:
        if c in d:
            cur = d[c]
        elif c == "十":
            total += (cur or 1) * 10
            cur = 0
        elif c == "百":
            total += (cur or 1) * 100
            cur = 0
        else:
            return None
    v = total + cur
    return v if 0 <= v <= 100 else None


def _volume_fast(text: str, session_id: str = "default") -> str | None:
    """音量本地控制：天猫级即时。优先控 xiaozhi 音箱（IoT SetVolume），
    设备不在线再控 PC 音量。复合句（"放歌然后调大音量"）不接。"""
    t = (text or "").strip().rstrip("。？！?!")
    if not any(k in t for k in ("音量", "声音", "静音", "大声", "小声")):
        return None
    if any(k in t for k in ("放", "播", "歌", "唱", "音乐")):
        return None

    def _dev_vol(n: int) -> bool:
        try:
            from app.notifications import push_iot_volume
            return push_iot_volume(n)
        except Exception:
            return False

    def _dev_mute() -> bool:
        try:
            from app.notifications import push_iot_mute
            return push_iot_mute(True)
        except Exception:
            return False

    if any(k in t for k in ("静音", "闭麦", "不出声")):
        if _dev_mute():
            return "已静音。"
        return "已静音。" if _mute_volume() else "静音失败。"
    if any(k in t for k in ("取消静音", "解除静音", "恢复声音")):
        if _dev_vol(1) or _set_volume(1):
            return "已取消静音。"
        return "没解开。"
    if any(k in t for k in ("多少", "当前", "现在音量", "多大")) and "调" not in t and "设" not in t:
        v = _get_volume_pct()
        return f"当前音量{v}%。" if v is not None else "查不到音量。"
    m = re.search(r'(?:调到|设到|设为|调成|到|至)\s*([\d一二两三四五六七八九十百]+)', t)
    if m:
        n = _cn_int(m.group(1))
        if n is not None:
            if _dev_vol(n) or _set_volume_abs(n):
                return f"音量已调到{n}%。"
            return "音量没调成。"
    n_only = re.search(r'([\d一二两三四五六七八九十百]+)\s*(?:%|％)?', t)
    if n_only and any(k in t for k in ("调", "设", "到")):
        n = _cn_int(n_only.group(1))
        if n is not None:
            if _dev_vol(n) or _set_volume_abs(n):
                return f"音量已调到{n}%。"
            return "音量没调成。"
    if any(k in t for k in ("最大", "最响")):
        if _dev_vol(100) or _set_volume_abs(100):
            return "音量已到100%。"
        return "没调成。"
    if any(k in t for k in ("大", "高", "加", "响")):
        v0 = _get_volume_pct() or 40
        n = min(100, v0 + 15)
        if _dev_vol(n) or _set_volume(15):
            return f"音量调大到{n}%。"
        return "音量没调成。"
    if any(k in t for k in ("小", "低", "减", "降", "轻")):
        v0 = _get_volume_pct() or 40
        n = max(0, v0 - 15)
        if _dev_vol(n) or _set_volume(-15):
            return f"音量调小到{n}%。"
        return "音量没调成。"
    return None


_JOKES = (
    "小明问爸爸：为什么我每次考试你都说下次努力？爸爸说：因为我怕你骄傲。",
    "医生说我缺乏运动。我说不可能，我每天都把手机从左手换到右手。",
    "我不是胖，我只是瘦得不明显。",
    "老板说要给我画大饼，我说不用画了，我减肥。",
    "蚊子咬我一口，我拍了它一巴掌，我们算扯平了。",
    "我朋友的记忆力特别好，好到能把昨天忘得一干二净。",
    "我减了三天肥，瘦了两斤，然后吃顿火锅又回来了。它是不是在等我。",
    "闹钟的作用是：提醒我还能再睡五分钟。",
    "我以为我是潜力股，后来发现我是 ST。",
    "别人熬夜是奋斗，我熬夜是舍不得睡。",
)


def _joke_fast(text: str, session_id: str = "default") -> str | None:
    """笑话/段子本地库：天猫是秒回，不走 2s LLM。故事不接（留给 LLM 发挥）。"""
    t = (text or "").replace(" ", "")
    if not any(k in t for k in (
            "讲个笑话", "说个笑话", "来个笑话", "讲笑话", "说笑话",
            "讲个段子", "说个段子", "来个段子", "逗我", "讲个冷笑话",
            "来点笑话", "有什么笑话")):
        return None
    if any(k in t for k in ("故事", "诗", "歌", "脑筋急转弯")):
        return None
    import random as _r
    # 同会话尽量不连讲同一条
    hist = _get_history(session_id)
    last = ""
    for m in reversed(hist):
        if isinstance(m, dict) and m.get("role") == "assistant":
            last = m.get("content") or ""
            break
    pool = [j for j in _JOKES if j != last] or _JOKES
    return _r.choice(pool)


def _reminder_list_fast(text: str, session_id: str = "default") -> str | None:
    """"我有哪些提醒" 本地直答：不进 LLM（实测 LLM 把工具结果改写成
    "最近一条是。"然后被单句模式截断）。只报还没响的，首句=最近一条。"""
    t = (text or "").replace(" ", "")
    if not any(k in t for k in (
            "哪些提醒", "提醒列表", "我的提醒", "提醒有哪些",
            "有什么提醒", "闹钟列表", "查看提醒", "查一下提醒")):
        return None
    if any(k in t for k in ("设", "加", "定一个", "取消", "删除", "关掉", "别提醒")):
        return None
    import datetime as _dt
    try:
        from app.reminders import list_reminders
        now = _dt.datetime.now()
        upcoming = []
        for r in list_reminders(include_completed=False):
            due = r.get("due") or ""
            if not due:
                continue
            try:
                dd = _dt.datetime.fromisoformat(due)
            except Exception:
                continue
            if dd >= now - _dt.timedelta(hours=2):
                upcoming.append((dd, r))
        upcoming.sort(key=lambda x: x[0])
        if not upcoming:
            return "没有待响的提醒。"
        n = len(upcoming)
        dd0, r0 = upcoming[0]
        txt0 = (r0.get("text") or "").strip() or "提醒"
        rep0 = {"daily": "，每天", "weekly": "，每周",
                "weekdays": "，工作日"}.get(r0.get("repeat", ""), "")
        when0 = dd0.strftime("%H点%M分")
        if dd0.date() != now.date():
            when0 = dd0.strftime("%m月%d日%H点%M分")
        head = f"最近一条{when0}，{txt0}{rep0}。"
        if n > 1:
            head += f"另外还有{n - 1}条。"
        return head
    except Exception as e:
        log.warning(f"[reminder] 列表快路径失败: {e}")
        return None


# 60秒读懂世界（中文日报，免 key）。keep-hot 每 4 分钟预热；
# 事件循环里只读缓存，绝不现拉（60s 冷启 1-3s，OkSurf 9-12s 都会冻住 WS）。
_NEWS60_CACHE: dict = {"t": 0.0, "val": ""}


def prefetch_news60() -> bool:
    """拉 60s 中文日报进缓存，供 _news_fast 秒答。keep-hot 专用。"""
    import requests as _rq
    for u in ("https://60s-api.viki.moe/v2/60s",
              "https://60s.viki.moe/v2/60s"):
        try:
            r = _rq.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
            items = (r.json().get("data") or {}).get("news") or []
            items = [str(x).strip() for x in items if x and len(str(x)) > 8]
            if items:
                # 5 条足够；太长 nmcloud 合成 3s+，首问 TTFA 破 2s
                val = "今天新闻：" + "；".join(items[:5]) + "。"
                _NEWS60_CACHE.update(t=time.time(), val=val)
                return True
        except Exception:
            continue
    return False


def _news_fast(text: str, session_id: str = "default") -> str | None:
    """新闻快路径：短问句读 60s 中文日报缓存，不走 LLM fncall。

    实测 LLM 会把"今天有什么新闻"带偏成"社会词条释义/不是实时新闻"；
    OkSurf 是英文头条。60s 是中文短句，天猫感。
    """
    t = (text or "").strip()
    if not t or len(t) > 18:
        return None
    if not any(k in t for k in ("新闻", "头条", "资讯")):
        return None
    if any(k in t for k in ("是什么", "什么是", "怎么", "为什么", "联播", "软件",
                            "App", "app", "来源", "谁创办", "历史", "英语")):
        return None
    val = _NEWS60_CACHE.get("val") or ""
    # 缓存 8 分钟内可用；keep-hot 4 分钟一刷。过期/空 → 落 LLM，别在循环里现拉
    if val and time.time() - _NEWS60_CACHE.get("t", 0) < 480:
        return val
    return None


_STORIES = {
    "白雪公主": "白雪公主因美貌遭王后妒忌，逃进森林遇到七个小矮人。王后扮成老太婆骗她咬下毒苹果，王子路过救醒了她，从此过上幸福生活。",
    "小红帽": "小红帽去看外婆，路上遇到大灰狼。狼先跑到外婆家把外婆吞了，又扮成外婆等小红帽。猎人路过剪开狼肚子救出她们，装上石头把狼沉了河。",
    "三只小猪": "三只小猪盖房子，老大用草，老二用木头，老三用砖头。大灰狼吹倒了草房和木房，兄弟俩躲进砖房。狼从烟囱爬下来，掉进热水锅，再也不敢来了。",
    "龟兔赛跑": "兔子嘲笑乌龟爬得慢，要和乌龟赛跑。兔子一路领先，中途睡了一觉。乌龟一步一步坚持爬，等兔子醒来，乌龟已经到了终点。",
    "丑小鸭": "丑小鸭因为长得和别人不一样，被欺负，离开了家。冬天差点冻死，春天醒来发现自己变成了一只漂亮的白天鹅。",
}


def _story_fast(text: str, session_id: str = "default") -> str | None:
    """经典童话本地讲（LLM 2.5s）；没收录的落 LLM。"""
    t = (text or "").strip()
    if not t or len(t) > 24:
        return None
    if not any(k in t for k in ("故事", "讲个", "讲一个", "讲一个")):
        return None
    if any(k in t for k in ("谁的", "作者", "英语", "多少字", "续写")):
        return None
    for k, v in _STORIES.items():
        if k in t:
            return v
    return None


def _iot_fast(text: str, session_id: str = "default") -> str | None:
    """扫描/连接蓝牙与 WiFi 设备。扫描在后台线程，不冻 WS；
    绝不让 PC 去连别的 AP（断网）。"""
    t = (text or "").strip()
    if not t:
        return None
    if any(k in t for k in ("扫描设备", "扫描蓝牙", "扫描WiFi", "扫描wifi", "扫描wi-fi",
                            "搜设备", "发现设备", "附近蓝牙", "附近WiFi", "附近wifi",
                            "家里有哪些设备", "有哪些智能设备", "搜索智能设备",
                            "蓝牙扫描", "wifi扫描")):
        try:
            from agent.local_iot import push_scan_result
            push_scan_result()
            return "好的，正在扫描附近的蓝牙和WiFi设备，结果马上告诉你。"
        except Exception as e:
            log.debug(f"[iot] 扫描启动失败: {e}")
            return None
    if t.startswith(("连接", "配对")):
        # 硬件控制暂不开放（用户 2026-11-01：还不成熟）。只留扫描。
        return None
    return None


def _who_fast(text: str, session_id: str = "default") -> str | None:
    """你是谁 → 本地身份，不进 LLM（实测 2.1s 才出"我是贾维斯"）。"""
    t = (text or "").strip().rstrip("？?。!！ ")
    if t not in ("你是谁", "你是谁啊", "你叫什么", "你叫什么名字", "介绍一下自己",
                 "介绍下自己", "你是哪个", "who are you", "你是干嘛的", "你是做什么的"):
        return None
    try:
        from agent.roles import get_current_role, get_role
        rid = get_current_role()
        name = (get_role(rid) or {}).get("name") or "查理"
    except Exception:
        name = "查理"
    if "J.A.R.V.I.S" in name or "jarvis" in name.lower() or "贾维斯" in name:
        return "我是贾维斯，你的语音助手。"
    if "白泽" in name:
        return "我是白泽，你的语音助手。"
    return f"我是{name}，你的语音助手。"


_TRANSLATE_TARGET = {
    "英语": "英文", "英文": "英文", "中文": "中文", "汉语": "中文",
    "日语": "日文", "日文": "日文", "韩语": "韩文", "韩文": "韩文",
    "法语": "法语", "德语": "德语",
}


def _translate_fast(text: str, session_id: str = "default") -> str | None:
    """翻译快路径：直调 magic-info.translate（1.6s），不走闲聊 LLM（实测 5s）。"""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return None
    if not any(k in t for k in ("翻译", "英语", "英文", "日语", "韩语", "法语", "德语")):
        return None
    if any(k in t for k in ("歌", "音乐", "故事", "诗")):
        return None
    src = target = ""
    m = re.search(r'(?:把)?(.+?)翻译成(英语|英文|中文|汉语|日语|日文|韩语|韩文|法语|德语)', t)
    if m:
        src, target = m.group(1), _TRANSLATE_TARGET.get(m.group(2), "")
    else:
        m = re.search(r'(.+?)用(英语|英文|中文|汉语|日语|日文|韩语|韩文|法语|德语)怎么说', t)
        if m:
            src, target = m.group(1), _TRANSLATE_TARGET.get(m.group(2), "")
    src = (src or "").strip(" ，。！？!?,.")
    if not src or not target or len(src) > 30:
        return None
    try:
        from agent.inproc_tools import _load_mcp_module
        mcp = _load_mcp_module("magic-info.py")
        tm = getattr(mcp, "_tool_manager", None)
        tool = (getattr(tm, "_tools", {}) or {}).get("translate") if tm else None
        fn = getattr(tool, "fn", None)
        if not fn:
            return None
        val = (fn(src, target) or "").strip().rstrip("。.")
        if val and len(val) < 100 and "失败" not in val:
            return val + "。"
    except Exception as e:
        log.debug(f"[translate] 快路径失败: {e}")
    return None


_IDIOMS = {
    "画蛇添足": "画蛇添足：做多余的事反而坏事。",
    "守株待兔": "守株待兔：不主动努力，妄想靠运气得到意外收获。",
    "亡羊补牢": "亡羊补牢：出了问题及时补救，还不算晚。",
    "掩耳盗铃": "掩耳盗铃：自己骗自己。",
    "画龙点睛": "画龙点睛：关键处一笔让整体活起来。",
    "对牛弹琴": "对牛弹琴：对不懂的人讲道理，白费口舌。",
    "狐假虎威": "狐假虎威：借别人的势力吓唬人。",
    "刻舟求剑": "刻舟求剑：不懂事物已变化，还按老办法办。",
    "南辕北辙": "南辕北辙：行动和目的相反。",
    "井底之蛙": "井底之蛙：见识狭窄的人。",
    "望梅止渴": "望梅止渴：用空想安慰自己。",
    "塞翁失马": "塞翁失马：坏事在一定条件下可能变成好事。",
    "叶公好龙": "叶公好龙：表面爱好，实际并不真懂真爱。",
    "滥竽充数": "滥竽充数：没本事的人混在行家里凑数。",
    "自相矛盾": "自相矛盾：说话做事前后抵触。",
    "买椟还珠": "买椟还珠：取舍不当，舍本逐末。",
    "东施效颦": "东施效颦：盲目模仿，效果更糟。",
    "班门弄斧": "班门弄斧：在行家面前卖弄本领。",
    "囫囵吞枣": "囫囵吞枣：学习时不加分析，笼统接受。",
    "拔苗助长": "拔苗助长：急于求成，反而坏事。",
}


def _idiom_fast(text: str, session_id: str = "default") -> str | None:
    """常见成语本地释义（LLM 2.4s）；没收录的落 LLM。"""
    t = (text or "").strip().rstrip("？?。!！ ")
    if not t or len(t) > 20:
        return None
    if not any(k in t for k in ("什么意思", "是什么意思", "意思", "成语", "解释")):
        return None
    for k, v in _IDIOMS.items():
        if k in t:
            return v
    return None


def _weather_follow_fast(text: str, session_id: str = "default") -> str | None:
    """多轮天气追问："那上海呢"/"明天呢" —— 上一轮必须是天气，否则 None 落回 LLM。"""
    t = (text or "").strip()
    if not t.endswith("呢") or len(t) > 12 or "天气" in t:
        return None
    try:
        hist = _get_history(session_id)
        last_a = ""
        for m in reversed(hist):
            if isinstance(m, dict) and m.get("role") == "assistant":
                last_a = m.get("content") or ""
                break
        if not last_a or not any(k in last_a for k in ("天气", "度", "雨", "晴", "阴", "雪", "雾")):
            return None
        from agent.weather import direct_weather_play as _dwp
        from agent.weather import _KNOWN_CITIES
        import re as _re
        city = next((c for c in _KNOWN_CITIES if c in t), None)
        if city:
            return _dwp(f"{city}天气")
        if any(k in t for k in ("明天", "后天", "大后天", "周末", "这周")):
            lm = _re.search(r'([\u4e00-\u9fa5]{2,4})(?:今天|明天|这周|天气)', last_a)
            city = lm.group(1) if lm else ""
            if not city:
                return None
            if "后天" in t:
                return _dwp(f"{city}后天天气")
            if "明天" in t:
                return _dwp(f"{city}明天天气")
            return _dwp(f"{city}这周天气")
    except Exception as e:
        log.debug(f"[weather] 追问快路径异常: {e}")
    return None


def _weather_fast(text: str, session_id: str = "default") -> str | None:
    """天气快路径：感叹/音乐/原理类不走天气 API。"""
    from agent.llm import _is_exclamatory_weather
    if _is_exclamatory_weather(text):
        return None
    if any(k in text for k in ("唱", "首歌", "歌曲", "歌单", "是怎么形成", "是怎么来的",
                               "是谁发明", "的起源", "的原理", "为什么不准", "用英语")):
        return None
    return _direct_weather_play(text)


_MUSIC_PLAY_PREFIX = (
    "播放", "放一首", "放首歌", "放个", "放首", "放点", "放音乐", "放歌",
    "放摇滚", "放轻音乐", "放钢琴", "放儿歌", "放古典", "放白噪", "放催眠",
    "来一首", "来首歌", "点一首", "点歌", "我要听", "我想听",
    "换一首", "换首歌", "切歌", "下一首", "上一首", "随机播放",
    "每日推荐", "热歌榜", "热歌", "有什么新歌", "推荐几首",
                       "唱一首", "唱首歌", "唱个歌", "唱一个", "我要听歌",
                       "继续播放", "暂停播放", "停止播放", "来个歌单",
)
_MUSIC_KNOWLEDGE = (
    "是谁", "发明", "原理", "历史", "起源", "的歌词", "用英语", "的区别",
    "怎么整理", "播放器", "赏析", "创作者", "哪年发行", "是谁唱", "写一首",
    "评论", "为什么", "诗", "故事", "笑话", "绕口令",
)


def _music_fast(text: str, session_id: str = "default") -> str | None:
    """音乐快路径：只接"动词开头"的播放指令，知识问句/复合句落回 LLM。

    之前音乐不进 FAST_PATHS 是怕误路由；这里用**句首动词**+知识排除双闸，
    "音乐是谁发明的"/"视频播放器推荐" 不会进来，"放一首歌"/"换一首" 不再
    依赖 LLM fncall（ncm 缺失时也能 HTTP 兜底出 __MUSIC__）。
    """
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith(("停止播放", "暂停播放", "停止音乐", "别放了", "关掉音乐", "停一下音乐")):
        return "__MUSIC_STOP__"
    if not t.startswith(_MUSIC_PLAY_PREFIX):
        return None
    if any(k in t for k in _MUSIC_KNOWLEDGE):
        return None
    reply = _direct_music_play(t)
    return reply or None


def _scene_protocol_handler(text: str) -> str | None:
    try:
        _mod = _load_magic_module("magic_scenes")
        if _mod:
            proto_key = _mod.match_protocol(text)
            if proto_key:
                return _mod.execute_protocol(proto_key)
    except Exception as e:
        log.debug(f"[scene] Protocol 匹配跳过: {e}")
    return None


# 快路径链：零延迟系统控制（角色切换、社交问候）+ 本地时间应答。
# 时间/日期/星期是纯本地事实（datetime.now() 就能答），问句形态高度固定；
# 走 LLM 意图分类 → magic-info brain 是两次云端往返（实测 2-7s）。
# 其余动作类意图（天气/音乐/空调/视觉/场景）仍统一走 LLM 意图分类 →
# MCP 工具调用，确保复合请求和上下文引用正确路由。
FAST_PATHS = [
    FastPath("role_switch", ("切换到贾维斯", "变成贾维斯", "切换贾维斯", "我要贾维斯", "换成贾维斯",
                             "切换回贾维斯", "切回贾维斯", "变回贾维斯", "回到贾维斯",
                             "切换到jarvis", "切换回jarvis", "切到jarvis", "换成jarvis",
                             "切换到查理", "变成查理", "切换查理", "回到查理",
                             "切换回查理", "切回查理", "变回查理",
                             "切换到charlie", "切换回charlie", "切到charlie", "换成charlie",
                             "回到默认", "恢复默认", "切换到默认", "切换回默认",
                             "切换到白泽", "变成白泽", "切换白泽", "我要白泽", "换成白泽",
                             "切换回白泽", "变回白泽"),
             "_role_switch_handler", case_insensitive=True),
    FastPath("social", ("谢谢", "多谢", "辛苦了", "再见", "拜拜",
                        "你好", "您好", "在吗", "在不在", "你在吗", "在家吗"),
             "_social_reply_handler"),
    FastPath("who", ("你是谁", "你叫什么", "介绍一下自己", "介绍下自己",
                     "你是哪个", "你是干嘛的", "你是做什么的"),
             "_who_fast"),
    FastPath("translate", ("翻译", "英语", "英文", "日语", "韩语", "法语", "德语"),
             "_translate_fast",
             exclude=("歌", "音乐", "故事", "诗", "成语")),
    FastPath("idiom", ("什么意思", "是什么意思", "成语", "解释一下"),
             "_idiom_fast"),
    FastPath("time", ("现在几点", "几点了", "几点啦", "现在时间", "现在多少点",
                      "几点钟", "在几点", "什么时间", "多少点了", "几点钟了",
                      "今天几号", "今天多少号", "几月几号", "今天日期", "现在日期",
                      "今天星期几", "今天礼拜几", "星期几了", "礼拜几了",
                      "明天星期几", "明天礼拜几", "昨天星期几", "昨天礼拜几",
                      "后天星期几", "后天礼拜几", "今年是哪一年", "现在是哪一年",
                      "今年年份", "今年是多少年", "上午还是下午", "早上还是晚上",
                      "白天还是晚上", "现在是几月", "今天几月", "现在是什么时间",
                      "今天几月几日", "今天是几月几日"),
             "_time_handler"),
    FastPath("weather", ("天气", "气温", "下雨", "温度", "几度", "今天冷", "今天热",
                         "带伞", "湿度", "雾霾", "空气质量", "下雪", "紫外线", "降雨",
                         "降水", "洗车", "晾被子", "晾衣服", "适合跑步", "适合钓鱼",
                         "适合爬山", "穿外套", "风大", "有雾", "冷吗", "热吗",
                         "热不热", "冷不冷", "适合晾", "适合洗车"),
             "_weather_fast",
             exclude=("不错", "真好", "真烦", "刚好", "聊天", "首歌", "歌曲",
                      "歌单", "唱", "预报是怎么", "是怎么形成", "是怎么来的",
                      "是谁发明", "的起源", "的原理", "用英语", "歌名")),
    FastPath("weather_follow", ("呢",),
             "_weather_follow_fast",
             exclude=("为什么", "怎么", "是谁", "是什么", "干嘛", "干啥", "音乐", "歌",
                      "放", "点", "唱", "诗", "故事")),
    FastPath("music", ("播放", "放歌", "放一首", "放首歌", "放个", "放首", "放点",
                       "放音乐", "放摇滚", "放轻音乐", "放钢琴", "放儿歌", "放古典",
                       "放白噪", "放催眠", "来一首", "来首歌", "点一首", "点歌",
                       "听歌", "我要听", "我想听", "换一首", "切歌", "下一首",
                       "上一首", "随机播放", "每日推荐", "热歌榜", "有什么新歌",
                       "唱一首", "唱首歌", "唱个歌", "唱一个", "我要听歌",
                       "继续播放", "暂停播放", "停止播放", "来个歌单"),
             "_music_fast"),
    FastPath("math", ("加", "减", "乘", "除以", "+", "-", "*", "÷"),
             "_math_fast"),
    FastPath("volume", ("音量", "声音大", "声音小", "静音", "大声点", "小声点",
                        "调大", "调小", "最响"),
             "_volume_fast",
             exclude=("歌", "音乐", "播放", "放", "唱")),
    FastPath("joke", ("讲个笑话", "说个笑话", "来个笑话", "讲笑话", "说笑话",
                      "讲个段子", "说个段子", "来个段子", "逗我", "冷笑话"),
             "_joke_fast"),
    FastPath("story", ("故事", "讲个", "讲一个"),
             "_story_fast",
             exclude=("笑话", "段子", "英语", "作者", "续写")),
    FastPath("reminder_list", ("哪些提醒", "提醒列表", "我的提醒", "提醒有哪些",
                               "有什么提醒", "闹钟列表", "查看提醒"),
             "_reminder_list_fast"),
    FastPath("news", ("新闻", "头条", "资讯"),
             "_news_fast",
             exclude=("是什么", "怎么", "为什么", "联播", "软件",
                      "App", "app", "来源", "谁创办", "英语")),
    FastPath("iot", ("扫描设备", "扫描蓝牙", "扫描WiFi", "扫描wifi", "搜设备",
                     "发现设备", "附近蓝牙", "附近WiFi", "附近wifi",
                     "家里有哪些设备", "有哪些智能设备", "搜索智能设备",
                     "蓝牙扫描"),
             "_iot_fast",
             exclude=("二维码", "唱歌", "音乐", "服务器", "热点")),
]


# ===== 完整语音闭环 =====
def voice_loop(audio_in: bytes, fmt: str = "mp3") -> Tuple[str, str, bytes]:
    """语音进 → ASR → 大脑(含MCP) → TTS → 语音出"""
    import time as _t
    _start = _t.time()
    from app.audit_log import audit_log
    audit_log("voice_loop", input_data=f"audio={len(audio_in)}bytes fmt={fmt}",
              action="start", session_id="voice")
    text = asr(audio_in, fmt)
    audit_log("asr", input_data=f"{len(audio_in)}bytes", output_data=text,
              action="recognize", session_id="voice",
              duration_ms=(_t.time()-_start)*1000)
    if not text:
        audit_log("voice_loop", input_data="empty_asr", output_data="empty",
                  action="empty", session_id="voice",
                  duration_ms=(_t.time()-_start)*1000)
        return EMPTY_ASR_TEXT, EMPTY_ASR_REPLY, b""
    if is_low_intent_asr(text):
        audit_log("voice_loop", input_data=text, output_data=LOW_INTENT_ASR_REPLY,
                  action="low_intent", session_id="voice",
                  duration_ms=(_t.time()-_start)*1000)
        return text, LOW_INTENT_ASR_REPLY, b""
    reply = brain(text)
    try:
        _tts_start = _t.time()
        audio_out = tts(reply)
        audit_log("tts", input_data=reply[:100], output_data=f"{len(audio_out)}bytes",
                  action="synthesize", session_id="voice",
                  duration_ms=(_t.time()-_tts_start)*1000)
    except Exception as e:
        log.warning(f"voice_loop TTS降级为文字: {e}")
        audit_log("tts", input_data=reply[:100], success=False, error=str(e),
                  action="synthesize_failed", session_id="voice")
        audio_out = b""
    audit_log("voice_loop", input_data=text, output_data=reply[:100],
              action="complete", session_id="voice",
              duration_ms=(_t.time()-_start)*1000)
    return text, reply, audio_out


# 预热: 后台线程构建一次 none 大脑实例(避免首请求等待)。
# 不在 import 期同步执行——_build_brain 含 MCP 子进程启动，
# 任一 server 启动慢会让所有 import voice_agent 的进程卡最长 60s。
def _warmup_brain():
    try:
        # 语音对话实际用 ("none", "voice") 键的 brain（见 _get_brain 的
        # (mcp_set, channel) 分键）。预热成 default 键等于白热，首次语音
        # 对话照样冷启动（MCP 子进程启动是秒级）。
        _get_brain("none", channel="voice")
        log.info("[warmup] brain(none,voice) 预热完成")
    except Exception as e:
        log.warning(f"[warmup] brain(none) 预热失败: {e}")


import threading as _threading
if os.getenv("SKIP_BACKGROUND", "") != "1":
    _threading.Thread(target=_warmup_brain, daemon=True, name="brain-warmup").start()
else:
    log.info("[warmup] SKIP_BACKGROUND=1，跳过 brain 预热")


if __name__ == "__main__":
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