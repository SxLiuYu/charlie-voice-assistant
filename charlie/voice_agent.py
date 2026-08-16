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
    _demo_mode_active, _ollama_online, _llm_has_any_key,
    _intent_cache_set, intent_classifier_status, _normalize_intent,
    _classify_intent, _wrap_openai_create_unknown_kwargs, _install_openai_compat,
    _build_brain, _get_brain, _ensure_event_loop,
    _record_brain_failure, _record_brain_success, _cleanup_brain_processes,
    restart_brain, reload_brain_config, set_current_user, get_current_user,
    brain_status, _ollama_fallback, _extract_assistant_text,
    _interrupted_context_message, _remember_conversation_async,
    _brain_llm, brain_stream_sentences, stream_voice_pipeline,
)

# ===== 快路径函数已拆分到 agent/ 子模块 =====
from agent.music import direct_music_play as _direct_music_play
from agent.weather import direct_weather_play as _direct_weather_play
from agent.vision import direct_vision_analyze as _direct_vision_analyze
from agent.device_control import (
    direct_ac_control as _direct_ac_control,
    set_volume as _set_volume,
    mute_volume as _mute_volume,
    sleep_display as _sleep_display,
    handle_smart_command as _handle_smart_command,
)

# ===== 从其他 agent/ 子模块导入 =====
from app.llm_config import (
    OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_OPENAI_BASE, active_chat_endpoint,
)
from agent.intent import LOW_INTENT_ASR_REPLY, is_low_intent_asr, is_garbled_asr
from agent.cache import _cache_get, _cache_set, _cache_get_interrupted, _cache_lock, _cache, _CACHE_TTL, _CACHE_MAX
from agent.history import (
    _history, _sessions, MAX_HISTORY, MAX_SESSIONS, _history_lock, HISTORY_FILE, HISTORY_LOCK_FILE,
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
    asr, _vosk_asr, _baidu_get_token, _asr_baidu, _tts_baidu,
    runtime_audio_path, write_audio_file, runtime_temp_audio_path,
    _TTS_BOLD_RE, _TTS_HEADER_RE, _TTS_BLOCKQUOTE_RE, _TTS_TABLE_PIPE_RE,
    _TTS_CODE_BLOCK_RE, _TTS_INLINE_CODE_RE, _TTS_LIST_ITEM_RE, _TTS_MARKDOWN_LINK_RE, _TTS_WHITESPACE_RE,
)
from agent.state import update_user_state, get_user_state, _infer_user_state, _user_state, _user_state_lock
from agent.system_msg import _build_system_msg, _build_user_profile, invalidate_system_msg_cache, _MCP_SYSTEM_PROMPTS

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
    """Module-level __getattr__: delegate scalar state reads to agent.llm_state.

    Tests and code that READ voice_agent._brain_failures, voice_agent._intent_failures,
    voice_agent._intent_disabled_until etc. get the live value from llm_state.
    Tests that WRITE these scalars must set them on agent.llm_state directly.
    """
    _STATE_NAMES = {
        '_brain_failures', '_brain_total_failures', '_brain_last_failure',
        '_brain_last_success', '_current_user_id',
        '_intent_failures', '_intent_disabled_until',
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
    raise AttributeError(f"module 'voice_agent' has no attribute {name!r}")


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


# ===== brain() 公共入口 =====
def brain(text: str, session_id: str = "default") -> str:
    """大脑推理: 先走快路径（关键词命中直连），未命中走 LLM。"""
    log.debug(f"[brain] 收到请求: {text[:50]}")
    _cmd_reply = _handle_smart_command(text)
    if _cmd_reply is not None:
        _append_history(_get_history(session_id), text, _cmd_reply)
        return _cmd_reply
    reply = _decision_feedback_handler(text, session_id)
    if reply is not None:
        return reply
    for path in FAST_PATHS:
        reply = path.run(text, session_id)
        if reply is not None:
            return reply
    cached = _cache_get(text)
    if cached is not None:
        log.info(f"[cache] 命中: {text[:20]}")
        return cached
    return _brain_llm(text, session_id)


def _load_magic_module(name: str, filename: str = None):
    """加载 magic-*.py 模块 — 委托到 app.load_magic_module"""
    from app import load_magic_module
    return load_magic_module(name, filename)


class FastPath:
    """快路径：关键词命中 → 直连 handler → 返回或回退 None"""
    def __init__(self, name: str, keywords: tuple, handler_name: str, exclude: tuple = ()):
        self.name = name
        self.keywords = keywords
        self.handler_name = handler_name
        self.exclude = exclude

    def match(self, text: str) -> bool:
        if not any(kw in text for kw in self.keywords):
            return False
        if self.exclude and any(ex in text for ex in self.exclude):
            return False
        return True

    def run(self, text: str, session_id: str = "default") -> str | None:
        if not self.match(text):
            return None
        log.info(f"[{self.name}] 关键词命中: {text[:20]}")
        try:
            _module = sys.modules[__name__]
            handler = getattr(_module, self.handler_name)
            reply = handler(text)
        except AttributeError:
            log.warning(f"[{self.name}] handler '{self.handler_name}' 不存在")
            return None
        except Exception as e:
            log.warning(f"[{self.name}] handler 异常: {e}")
            return None
        if reply:
            _append_history(_get_history(session_id), text, reply)
            return reply
        log.info(f"[{self.name}] 未命中或失败，回退")
        return None


def _time_handler(text: str) -> str:
    now = datetime.datetime.now()
    return f"现在{now.strftime('%H点%M分')}。"


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


# 快路径链：顺序执行，先命中先返回
FAST_PATHS = [
    FastPath("time", ('几点', '几点啦'), "_time_handler"),
    FastPath("ac", ('空调', '制冷', '制热', '除湿', '开空调', '调温度', '温度调', '高风', '中风', '低风', '风速'),
             "_direct_ac_control", exclude=('天气',)),
    FastPath("weather", ('天气', '气温', '下雨', '下雪', '温度', '几度', '今天冷', '今天热', '冷不冷', '热不热'),
             "_direct_weather_play"),
    FastPath("music", ('播放音乐', '播放歌', '随机播', '来一首', '来首歌', '播一首', '点一首', '放一首', '放歌', '听歌', '放音乐', '放点音乐', '随机来', '来首歌', '放首', '放点', '整首', '整点', '播首歌', '单曲循环', '随机播放', '每日推荐', '停止播放'),
             "_direct_music_play", exclude=('停止播放',)),
    FastPath("vision", ('看看屏幕', '屏幕上有什么', '截图分析', '帮我看看屏幕', '截屏', '识别图片', '图上有什么', '看看这张图', '看看这张', '看看这个图', '屏幕上显示什么', '屏幕上有啥'),
             "_direct_vision_analyze"),
    FastPath("scene", ('晚安', '睡觉', '好梦', '早上好', '早安', '看电影', '出门'),
             "_scene_protocol_handler"),
]


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


# 预热: 在模块加载时构建一次 none 大脑实例(避免首请求等待)
try:
    _get_brain("none")
    log.info("[warmup] brain(none) 预热完成")
except Exception as e:
    log.warning(f"[warmup] brain(none) 预热失败: {e}")


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
