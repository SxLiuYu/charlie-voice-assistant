"""ASR/TTS: 语音识别与合成"""
import os
import json
import time
import base64
import threading
import logging
import re
import tempfile
from typing import Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from agent.intent import is_garbled_asr

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BAIDU_APP_ID = os.getenv("BAIDU_APP_ID", "")
BAIDU_API_KEY = os.getenv("BAIDU_API_KEY", "")
BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY", "")

# Baidu ASR hotwords (dev_pid=1537). Boosts recognition of the wake word and
# frequent command nouns so short / ambient-noise-y input is read correctly.
# 加载顺序：keywords/keywords.txt → ASR_HOTWORDS 环境变量
# 环境变量可覆盖文件默认值。
def _load_asr_hotwords() -> list[str]:
    """从 keywords/keywords.txt 和环境变量加载 ASR 热词"""
    words: list[str] = []
    # 1. 从 keywords/keywords.txt 加载
    kw_file = os.path.join(
        os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "..", "keywords", "keywords.txt"
    )
    kw_file = os.path.normpath(kw_file)
    try:
        if os.path.exists(kw_file):
            with open(kw_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("/"):
                        continue
                    # 支持逗号分隔的一行多词
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    words.extend(parts)
    except Exception as e:
        log.debug(f"[agent.asr_tts] error at line 44: {e}")
        pass
    # 2. 从环境变量加载（可覆盖文件）
    env_words = [w.strip() for w in
                 os.getenv("ASR_HOTWORDS", "").split(",") if w.strip()]
    if env_words:
        words = env_words
    # 去重保持顺序
    seen = set()
    result = []
    for w in words:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result

ASR_HOTWORDS = _load_asr_hotwords()
_baidu_token = {"token": "", "at": 0.0}
_baidu_token_lock = threading.Lock()
BAIDU_TOKEN_FILE = os.path.join(DATA_DIR, ".baidu_token.json")
if os.path.exists(BAIDU_TOKEN_FILE):
    try:
        with open(BAIDU_TOKEN_FILE, encoding="utf-8") as f:
            saved = json.load(f)
            if isinstance(saved, dict) and saved.get("token"):
                _baidu_token["token"] = saved["token"]
                _baidu_token["at"] = saved.get("at", 0.0)
    except Exception as e:
        log.debug(f"[agent.asr_tts] error at line 71: {e}")
        pass

TTS_CACHE_MAX_CHARS = int(os.getenv("TTS_CACHE_MAX_CHARS", "200"))
TTS_CACHE_TTL = 3600.0
TTS_CACHE_MAX = 200
TTS_VOICE = os.getenv("TTS_VOICE", "Ethan")
TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-flash")
TTS_FAILURE_COOLDOWN = float(os.getenv("TTS_FAILURE_COOLDOWN", "120"))
TTS_FAILURE_THRESHOLD = int(os.getenv("TTS_FAILURE_THRESHOLD", "3"))
_LOCAL_TTS_ENABLED = os.getenv("LOCAL_TTS_ENABLED", "0") == "1"
_tts_cache: dict[tuple, tuple] = {}
_tts_unavailable_until = 0.0
_tts_failures = 0
_tts_lock = threading.Lock()
_tts_speed = float(os.getenv("TTS_SPEED", "1.0"))  # e.g. 0.9 slow / 1.1 fast

# ===== 情感 TTS: 线程级情感上下文 =====
_emotion_ctx = threading.local()

_EMOTION_TO_BAIDU_PIT = {
    "happy": 8,    # 高音调，活泼
    "sad": 3,      # 低音调，沉稳
    "serious": 4,  # 中低音调，严肃
}


def set_current_emotion(emotion: str = "") -> None:
    """设置当前轮 TTS 情感（线程级，brain_stream 调用前设置）。"""
    _emotion_ctx.emotion = emotion or ""


def get_current_emotion() -> str:
    """获取当前轮 TTS 情感，空串=默认中性。"""
    return getattr(_emotion_ctx, "emotion", "")

ASR_PRIORITY = [p.strip() for p in os.getenv("ASR_PRIORITY", "sensevoice,baidu,stepfun,vosk").split(",") if p.strip()]
TTS_PRIORITY = [p.strip() for p in os.getenv("TTS_PRIORITY", "audio8,baidu,stepfun,finna").split(",") if p.strip()]

# ===== 规范音色 → 各 provider 音色映射（单一真相源）=====
# 角色 tts_voice 使用规范音色名，由 resolve_voice 翻译成各 provider 的具体音色ID。
# 注：百度 per 参数除 3=成熟男声(Ethan) 外，其余映射为近似值，未逐一实调验证。
_VOICE_MAP: dict[str, dict[str, Any]] = {
    "Ethan": {           # 成熟男声（charlie/jarvis/baize 默认）
        "finna": "Ethan",
        "baidu": 3,      # 百度 per 参数：3=成熟男声
        "stepfun": "cixingnansheng",  # StepFun: 磁性男声
        "audio8": "tingting",  # Audio8: 默认注册的 tingting 声音
        "cosyvoice": "中文男",  # CosyVoice: 男声
    },
    "Cherry": {          # 自然女声
        "finna": "Ethan", # Finna 只支持 Ethan，其余 provider 用近似值
        "baidu": 0,      # 百度 per 参数：0=度小美(女声)，近似
        "stepfun": "jingdiannvsheng",  # StepFun: 经典女声
        "audio8": "tingting",
        "cosyvoice": "中文女",
    },
    "Stella": {          # 温柔女声
        "finna": "Ethan",
        "baidu": 4,      # 百度 per 参数：4=度小鹿(女声)，近似
        "stepfun": "jingdiannvsheng",  # StepFun: 经典女声
        "audio8": "tingting",
        "cosyvoice": "中文女",
    },
    "Alex": {            # 沉稳男声
        "finna": "Ethan",
        "baidu": 1,      # 百度 per 参数：1=度小宇(男声)，近似
        "stepfun": "boyinnansheng",    # StepFun: 播音男声
        "audio8": "tingting",
        "cosyvoice": "中文男",
    },
    "Vega": {            # 活力女声
        "finna": "Ethan",
        "baidu": 5,      # 百度 per 参数：5=普通女声，近似
        "stepfun": "jingdiannvsheng",  # StepFun: 经典女声
        "audio8": "tingting",
        "cosyvoice": "中文女",
    },
    "Nova": {            # 甜美女声
        "finna": "Ethan",
        "baidu": 0,      # 百度 per 参数：0=度小美(女声)，近似
        "stepfun": "jingdiannvsheng",  # StepFun: 经典女声
        "audio8": "tingting",
        "cosyvoice": "中文女",
    },
    "Echo": {            # 中性声
        "finna": "Ethan",
        "baidu": 5,      # 百度 per 参数：5=普通女声，近似
        "stepfun": "cixingnansheng",   # StepFun: 磁性男声
        "audio8": "tingting",
        "cosyvoice": "中文女",
    },
    "Kid": {             # 童声（近似：百度基础API无精品童声，用女声+高pitch+慢速近似）
        "finna": "Ethan",
        "baidu": 0,      # 度小美女声，配合 emotion happy→pit=8 近似童声
        "stepfun": "jingdiannvsheng",
        "audio8": "tingting",
        "cosyvoice": "中文女",
    },
}
_DEFAULT_CANONICAL = "Ethan"


def resolve_voice(canonical: str, provider: str) -> Any:
    """规范音色 → 指定 provider 的音色ID；未知值回退 Ethan 映射。"""
    mapping = _VOICE_MAP.get(canonical or _DEFAULT_CANONICAL) or _VOICE_MAP[_DEFAULT_CANONICAL]
    return mapping.get(provider, mapping.get("finna", "Ethan"))


def reload() -> None:
    """配置热重载：从 os.environ 重新读取百度凭证 + TTS 参数。

    /welcome 或 /setup 保存 Key 后由 voice_server._reload_runtime_env() 调用，
    无需重启进程即可让新填入的百度 ASR/TTS Key 生效。

    同时：
    - 清掉内存里用旧（可能为空）凭证拿到/失败的百度 token
    - 删除落盘的 .baidu_token.json（避免旧 token 被复用）
    - 重置 TTS 熔断状态（否则即使 Key 正确也要等冷却结束）
    """
    global BAIDU_APP_ID, BAIDU_API_KEY, BAIDU_SECRET_KEY
    global TTS_VOICE, TTS_MODEL, TTS_FAILURE_COOLDOWN, TTS_FAILURE_THRESHOLD
    global TTS_CACHE_MAX_CHARS, TTS_CACHE_MAX
    global _tts_speed, _tts_unavailable_until, _tts_failures
    global ASR_PRIORITY, TTS_PRIORITY

    BAIDU_APP_ID = os.getenv("BAIDU_APP_ID", "")
    BAIDU_API_KEY = os.getenv("BAIDU_API_KEY", "")
    BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY", "")

    TTS_VOICE = os.getenv("TTS_VOICE", "Ethan")
    TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-flash")
    TTS_FAILURE_COOLDOWN = float(os.getenv("TTS_FAILURE_COOLDOWN", "120"))
    TTS_FAILURE_THRESHOLD = int(os.getenv("TTS_FAILURE_THRESHOLD", "3"))
    TTS_CACHE_MAX_CHARS = int(os.getenv("TTS_CACHE_MAX_CHARS", "200"))
    TTS_CACHE_MAX = int(os.getenv("TTS_CACHE_MAX", "200"))
    _tts_speed = float(os.getenv("TTS_SPEED", "1.0"))

    ASR_PRIORITY = [p.strip() for p in os.getenv("ASR_PRIORITY", "sensevoice,baidu,stepfun,vosk").split(",") if p.strip()]
    TTS_PRIORITY = [p.strip() for p in os.getenv("TTS_PRIORITY", "audio8,baidu,stepfun,finna").split(",") if p.strip()]

    # 清空旧 token（内存 + 落盘文件），下次用新凭证重新获取
    with _baidu_token_lock:
        _baidu_token["token"] = ""
        _baidu_token["at"] = 0.0
    try:
        if os.path.exists(BAIDU_TOKEN_FILE):
            os.remove(BAIDU_TOKEN_FILE)
    except Exception as e:
        log.debug(f"[agent.asr_tts] error at line 179: {e}")
        pass

    # 重置熔断，让新 Key 能立即尝试合成
    with _tts_lock:
        _tts_failures = 0
        _tts_unavailable_until = 0.0

    log.info(f"[asr_tts] 配置已热重载: 百度Key={'已配置' if BAIDU_API_KEY else '未配置'}, "
             f"voice={TTS_VOICE}, model={TTS_MODEL}")

_asr_fallback_times: list = []
_asr_lock = threading.Lock()

_TTS_BOLD_RE = re.compile(r'\*{1,3}')
_TTS_HEADER_RE = re.compile(r'^#{1,6}\s*')
_TTS_BLOCKQUOTE_RE = re.compile(r'^>\s*')
_TTS_TABLE_PIPE_RE = re.compile(r'\|')
_TTS_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```')
_TTS_INLINE_CODE_RE = re.compile(r'`[^`]*`')
_TTS_LIST_ITEM_RE = re.compile(r'^[-*+]\s+')
_TTS_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_TTS_EMOJI_RE = re.compile(
    r'[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2764\u2760]',
    re.UNICODE)
_TTS_PAREN_RE = re.compile(r'[（(][^）)]{0,40}[）)]?')
_TTS_WHITESPACE_RE = re.compile(r'\s{2,}')
_TTS_STRIKETHROUGH_RE = re.compile(r'~~([^~]+)~~')
_TTS_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\([^)]*\)')
_TTS_MULTI_PUNCT_RE = re.compile(r'([！？。，！？。，]){2,}')

_DIGIT_CN = "零一二三四五六七八九"
_UNIT_AFTER_DIGIT = ("点", "度", "分", "秒", "层", "楼", "块", "号", "岁")
_NORMALIZE_DIGITS_RE = re.compile(r'(\d{1,4}(?:\.\d+)?)(' + '|'.join(_UNIT_AFTER_DIGIT) + ')')


def _normalize_digits(text: str) -> str:
    """数字后跟单位词时转中文读法（处理1-4位数字及小数）"""
    _DC = _DIGIT_CN
    def _num_to_cn(n: str) -> str:
        if "." in n:
            int_part, dec_part = n.split(".", 1)
            int_cn = _num_to_cn(int_part) if int_part else ""
            dec_cn = "".join(_DC[int(d)] for d in dec_part)
            return f"{int_cn}点{dec_cn}" if int_cn else f"点{dec_cn}"
        if len(n) == 1:
            return _DC[int(n)]
        if len(n) == 2:
            if n[0] == "1":
                return "十" + (_DC[int(n[1])] if n[1] != "0" else "")
            return _DC[int(n[0])] + "十" + (_DC[int(n[1])] if n[1] != "0" else "")
        if len(n) == 3:
            t = _DC[int(n[0])] + "百"
            if n[1] == "0":
                if n[2] != "0":
                    t += "零" + _DC[int(n[2])]
                return t
            t += _DC[int(n[1])] + "十"
            if n[2] != "0":
                t += _DC[int(n[2])]
            return t
        if len(n) == 4:
            t = _DC[int(n[0])] + "千"
            if n[1] == "0":
                if n[2] == "0":
                    if n[3] != "0":
                        t += "零" + _DC[int(n[3])]
                else:
                    t += "零" + _DC[int(n[2])] + "十"
                    if n[3] != "0":
                        t += _DC[int(n[3])]
                return t
            t += _DC[int(n[1])] + "百"
            if n[2] == "0":
                if n[3] != "0":
                    t += "零" + _DC[int(n[3])]
                return t
            t += _DC[int(n[2])] + "十"
            if n[3] != "0":
                t += _DC[int(n[3])]
            return t
        return n
    def _replace(m):
        return _num_to_cn(m.group(1)) + m.group(2)
    return _NORMALIZE_DIGITS_RE.sub(_replace, text)


class TTSUnavailableError(Exception):
    pass

def _clean_for_tts(text: str) -> str:
    t = text
    t = _TTS_BOLD_RE.sub('', t)
    t = _TTS_HEADER_RE.sub('', t)
    t = _TTS_BLOCKQUOTE_RE.sub('', t)
    t = _TTS_TABLE_PIPE_RE.sub(' ', t)
    t = _TTS_CODE_BLOCK_RE.sub('', t)
    t = _TTS_INLINE_CODE_RE.sub('', t)
    t = _TTS_LIST_ITEM_RE.sub('', t)
    t = _TTS_MARKDOWN_LINK_RE.sub(r'\1', t)
    t = _TTS_STRIKETHROUGH_RE.sub('', t)
    t = _TTS_IMAGE_RE.sub('', t)
    # Sidebars/remark glosses and emoji don't add speech value.
    t = _TTS_EMOJI_RE.sub('', t)
    t = _TTS_PAREN_RE.sub('', t)
    t = _TTS_MULTI_PUNCT_RE.sub(r'\1', t)
    t = _TTS_WHITESPACE_RE.sub(' ', t)
    t = t.strip()
    t = _normalize_digits(t)
    return t

def get_effective_tts_config() -> tuple[str, float]:
    """获取当前生效的 TTS 配置（voice, speed），优先使用角色配置"""
    try:
        from agent.roles import get_current_role, get_role_tts_config
        role_id = get_current_role()
        role_config = get_role_tts_config(role_id)
        voice = role_config.get("voice", "") or TTS_VOICE
        speed = role_config.get("speed", _tts_speed)
        return voice, float(speed)
    except Exception:
        return TTS_VOICE, _tts_speed


def _tts_cache_get(text: str, voice: str = "", model: str = "", speed: float = 1.0) -> Optional[bytes]:
    emotion = get_current_emotion()
    key = (text, voice or TTS_VOICE, speed, model or TTS_MODEL, emotion)
    with _tts_lock:
        item = _tts_cache.get(key)
        if item is None:
            return None
        audio, ts = item
        if time.time() - ts > TTS_CACHE_TTL:
            _tts_cache.pop(key, None)
            return None
        return audio

def _tts_cache_put(text: str, audio: bytes, voice: str = "", model: str = "", speed: float = 1.0) -> None:
    if len(text) <= TTS_CACHE_MAX_CHARS and len(audio) > 100:
        emotion = get_current_emotion()
        with _tts_lock:
            _tts_cache[(text, voice or TTS_VOICE, speed, model or TTS_MODEL, emotion)] = (audio, time.time())
            if len(_tts_cache) > TTS_CACHE_MAX:
                _tts_cache.pop(next(iter(_tts_cache)), None)

def tts(text: str) -> bytes:
    from agent.retry import _retry
    global _tts_unavailable_until, _tts_failures
    cleaned = _clean_for_tts(text).strip()
    if not cleaned:
        return b""
    _voice, _speed = get_effective_tts_config()
    cached = _tts_cache_get(cleaned, _voice, TTS_MODEL, speed=_speed)
    if cached is not None:
        return cached
    now = time.time()
    with _tts_lock:
        if now < _tts_unavailable_until:
            raise TTSUnavailableError(f"TTS冷却熔断, 剩余{_tts_unavailable_until - now:.0f}s")
    last_exc: Exception | None = None
    for name in TTS_PRIORITY:
        provider_fn = _TTS_PROVIDERS.get(name)
        if provider_fn is None:
            # 惰性加载 (cosyvoice 等未默认导入的 provider)
            if name == "cosyvoice":
                try:
                    from agent.cosyvoice_clone import tts_cosyvoice
                    _TTS_PROVIDERS["cosyvoice"] = tts_cosyvoice
                    provider_fn = tts_cosyvoice
                except ImportError:
                    log.debug("[tts] cosyvoice_clone 不可用，跳过")
                    continue
            else:
                log.warning(f"[tts] 未知 provider '{name}'，跳过")
                continue
        if name == "stepfun" and not os.getenv("STEPFUN_KEY", ""):
            log.debug("[tts] StepFun 未配置，跳过")
            continue
        try:
            audio = _retry(lambda fn=provider_fn: fn(cleaned), name=f"TTS({name})")
        except TTSUnavailableError as e:
            # per-provider 熔断：记录失败但继续尝试下一个 provider
            last_exc = e
            log.warning(f"[tts] {name} 熔断/限流，尝试下一个 provider: {e}")
            continue
        except Exception as e:
            last_exc = e
            log.warning(f"[tts] {name} 失败: {e}")
            continue
        # 成功
        with _tts_lock:
            _tts_failures = 0
            _tts_unavailable_until = 0.0
        _tts_cache_put(cleaned, audio, _voice, TTS_MODEL, speed=_speed)
        return audio
    # 所有 provider 都失败
    with _tts_lock:
        _tts_failures += 1
        _tts_unavailable_until = now + TTS_FAILURE_COOLDOWN
    log.warning(f"TTS失败熔断{TTS_FAILURE_COOLDOWN:g}秒(连续失败{_tts_failures}次)")
    raise TTSUnavailableError(str(last_exc)) from last_exc

def tts_status() -> Dict[str, Any]:
    now = time.time()
    with _tts_lock:
        remaining = max(0.0, _tts_unavailable_until - now)
        consecutive_failures = _tts_failures
    return {
        "active": remaining > 0,
        "remaining_seconds": remaining,
        "cooldown_seconds": TTS_FAILURE_COOLDOWN,
        "consecutive_failures": consecutive_failures,
        "failure_threshold": TTS_FAILURE_THRESHOLD,
    }

def asr(audio_bytes: bytes, fmt: str = "mp3", session_id: str = "default") -> str:
    """ASR 入口：按 ASR_PRIORITY 遍历 provider，失败降级，成功后做文本纠错。

    新增 session_id 参数用于上下文感知纠错（working_memory）。
    """
    # 按 ASR_PRIORITY 顺序遍历 provider，失败降级到下一个
    for name in ASR_PRIORITY:
        provider_fn = _ASR_PROVIDERS.get(name)
        if provider_fn is None:
            log.warning(f"[asr] 未知 provider '{name}'，跳过")
            continue
        if name == "stepfun" and not os.getenv("STEPFUN_KEY", ""):
            log.debug("[asr] StepFun 未配置，跳过")
            continue
        try:
            text = provider_fn(audio_bytes, fmt)
            if text:
                # ASR 文本纠错（后处理）
                try:
                    from agent.asr_correction import correct_asr_text
                    text = correct_asr_text(text, session_id=session_id)
                except Exception as e:
                    log.debug(f"[agent.asr_tts] error at line 368: {e}")
                    pass
                return text
            log.warning(f"[asr] {name} 返回空结果")
        except Exception as e:
            log.warning(f"[asr] {name} 失败: {e}")
    # 所有 provider 都失败后的频率限制
    now = time.time()
    with _asr_lock:
        _asr_fallback_times.append(now)
        while _asr_fallback_times and _asr_fallback_times[0] < now - 30:
            _asr_fallback_times.pop(0)
        if len(_asr_fallback_times) > 3:
            log.error(f"ASR全部失败太频繁({len(_asr_fallback_times)}次/30s)，返回空")
            return ""
    return ""


_RACE_POOL: "ThreadPoolExecutor | None" = None
_RACE_POOL_LOCK = threading.Lock()


def _get_race_pool() -> ThreadPoolExecutor:
    """Lazy-initialized, process-wide thread pool for asr_race parallel providers."""
    global _RACE_POOL
    if _RACE_POOL is None:
        with _RACE_POOL_LOCK:
            if _RACE_POOL is None:
                _RACE_POOL = ThreadPoolExecutor(
                    max_workers=int(os.getenv("ASR_RACE_POOL_SIZE", "2")),
                    thread_name_prefix="asr-race",
                )
    return _RACE_POOL


def asr_race(audio_bytes: bytes, fmt: str = "wav", session_id: str = "default", deadline_ms: int | None = None) -> tuple[str, str]:
    """ASR 竞速择优：并行发起本地 SenseVoice 与云端 StepFun，按质量/延迟策略选择结果。

    返回 (text, provider)，provider 为 sensevoice / stepfun / legacy / none。
    任何异常降级到现有串行 asr()。
    """
    # 回退开关：ASR_RACE=0 时完全走旧路径
    if os.getenv("ASR_RACE", "1") != "1":
        return asr(audio_bytes, fmt, session_id), "legacy"

    stepfun_configured = bool(os.getenv("STEPFUN_KEY", ""))
    if not stepfun_configured:
        # 云端未配置，只跑本地
        t0 = time.perf_counter()
        try:
            text = _asr_sense_voice(audio_bytes, fmt)
            ms = (time.perf_counter() - t0) * 1000
            log.info(f"[asr-race] 本地={ms:.0f}ms 云端=N/A(未配置) 选择=local 原因=stepfun未配置")
            return text, "sensevoice"
        except Exception as e:
            log.warning(f"[asr-race] local failed: {e}")
            return asr(audio_bytes, fmt, session_id), "legacy"

    deadline_ms = deadline_ms if deadline_ms is not None else int(os.getenv("ASR_RACE_DEADLINE_MS", "1200"))
    grace_ms = int(os.getenv("ASR_RACE_GRACE_MS", "50"))  # 本地干净时不等云端（实测 StepFun 800ms+ 永远赶不上宽限）；乱码路径仍等满 deadline

    local_start = time.perf_counter()
    cloud_start = time.perf_counter()

    local_text = None
    local_ms = None
    cloud_text = None
    cloud_ms = None

    pool = _get_race_pool()
    local_future = pool.submit(_asr_sense_voice, audio_bytes, fmt)
    cloud_future = pool.submit(_asr_stepfun, audio_bytes, fmt)

    try:
        done, not_done = wait(
            [local_future, cloud_future],
            timeout=deadline_ms / 1000.0,
            return_when=FIRST_COMPLETED,
        )
    except Exception as e:
        log.warning(f"[asr-race] wait failed: {e}")
        return asr(audio_bytes, fmt, session_id), "legacy"

    if local_future in done:
        try:
            local_text = local_future.result()
        except Exception:
            pass
        local_ms = (time.perf_counter() - local_start) * 1000

    if cloud_future in done:
        try:
            cloud_text = cloud_future.result()
        except Exception:
            pass
        cloud_ms = (time.perf_counter() - cloud_start) * 1000

    cloud_done = cloud_future.done()
    local_done = local_future.done()

    # Case A: Cloud finished within deadline with valid text
    if cloud_done and cloud_text:
        reason = "云端先完成且≤deadline"
        if local_done and local_text and not is_garbled_asr(local_text):
            reason = "本地干净+云端已完成"
        log.info(f"[asr-race] 本地={local_ms or 'N/A'}ms 云端={cloud_ms:.0f}ms 选择=cloud 原因={reason}")
        try:
            from agent.asr_correction import correct_asr_text
            cloud_text = correct_asr_text(cloud_text, session_id=session_id)
        except Exception as e:
            log.debug(f"[agent.asr_tts] error at line ...: {e}")
        return cloud_text, "stepfun"

    # Case B: Cloud failed/empty, wait for local if still running
    if cloud_done and not cloud_text:
        if not local_done:
            remaining = max(0, deadline_ms - (time.perf_counter() - local_start) * 1000) / 1000.0
            if remaining > 0:
                try:
                    wait([local_future], timeout=remaining, return_when=FIRST_COMPLETED)
                except Exception:
                    pass
            if local_future.done():
                try:
                    local_text = local_future.result()
                    local_ms = (time.perf_counter() - local_start) * 1000
                except Exception:
                    pass

        if local_text is not None:
            log.info(f"[asr-race] 本地={local_ms or 'N/A'}ms 云端=err 选择=local 原因=云端异常/失败")
            try:
                from agent.asr_correction import correct_asr_text
                local_text = correct_asr_text(local_text, session_id=session_id)
            except Exception as e:
                log.debug(f"[agent.asr_tts] error at line ...: {e}")
            return local_text, "sensevoice"

    # Case C: Local finished first
    if local_done and local_text is not None:
        if local_text.strip() and not is_garbled_asr(local_text):
            # Local clean
            if cloud_done:
                if cloud_text:
                    log.info(f"[asr-race] 本地={local_ms:.0f}ms 云端={cloud_ms:.0f}ms 选择=cloud 原因=本地干净+云端已完成")
                    try:
                        from agent.asr_correction import correct_asr_text
                        cloud_text = correct_asr_text(cloud_text, session_id=session_id)
                    except Exception as e:
                        log.debug(f"[agent.asr_tts] error at line ...: {e}")
                    return cloud_text, "stepfun"
                else:
                    log.info(f"[asr-race] 本地={local_ms:.0f}ms 云端=err 选择=local 原因=本地干净+云端失败")
                    try:
                        from agent.asr_correction import correct_asr_text
                        local_text = correct_asr_text(local_text, session_id=session_id)
                    except Exception as e:
                        log.debug(f"[agent.asr_tts] error at line ...: {e}")
                    return local_text, "sensevoice"
            else:
                # Wait grace for cloud
                try:
                    cloud_done_futures, _ = wait([cloud_future], timeout=grace_ms / 1000.0, return_when=FIRST_COMPLETED)
                except Exception:
                    cloud_done_futures = set()

                if cloud_future in cloud_done_futures:
                    try:
                        cloud_text = cloud_future.result()
                        cloud_ms = (time.perf_counter() - cloud_start) * 1000
                        if cloud_text:
                            log.info(f"[asr-race] 本地={local_ms:.0f}ms 云端={cloud_ms:.0f}ms 选择=cloud 原因=本地干净+云端宽限期内到达")
                            try:
                                from agent.asr_correction import correct_asr_text
                                cloud_text = correct_asr_text(cloud_text, session_id=session_id)
                            except Exception as e:
                                log.debug(f"[agent.asr_tts] error at line ...: {e}")
                            return cloud_text, "stepfun"
                    except Exception:
                        pass

                log.info(f"[asr-race] 本地={local_ms:.0f}ms 云端=timeout 选择=local 原因=本地干净+云端超宽限")
                try:
                    from agent.asr_correction import correct_asr_text
                    local_text = correct_asr_text(local_text, session_id=session_id)
                except Exception as e:
                    log.debug(f"[agent.asr_tts] error at line ...: {e}")
                return local_text, "sensevoice"
        else:
            # Local empty or garbled
            if not cloud_done:
                remaining = max(0, deadline_ms - (time.perf_counter() - cloud_start) * 1000) / 1000.0
                if remaining > 0:
                    try:
                        wait([cloud_future], timeout=remaining, return_when=FIRST_COMPLETED)
                    except Exception:
                        pass

            if cloud_future.done():
                try:
                    cloud_text = cloud_future.result()
                    cloud_ms = (time.perf_counter() - cloud_start) * 1000
                except Exception:
                    pass

            if cloud_text:
                log.info(f"[asr-race] 本地={local_ms or 'N/A'}ms 云端={cloud_ms:.0f}ms 选择=cloud 原因=本地乱码/空+云端成功")
                try:
                    from agent.asr_correction import correct_asr_text
                    cloud_text = correct_asr_text(cloud_text, session_id=session_id)
                except Exception as e:
                    log.debug(f"[agent.asr_tts] error at line ...: {e}")
                return cloud_text, "stepfun"

            if local_text is not None:
                log.info(f"[asr-race] 本地={local_ms or 'N/A'}ms 云端={'err' if cloud_ms is None else f'{cloud_ms:.0f}ms'} 选择=local 原因=云端也失败")
                try:
                    from agent.asr_correction import correct_asr_text
                    local_text = correct_asr_text(local_text, session_id=session_id)
                except Exception as e:
                    log.debug(f"[agent.asr_tts] error at line ...: {e}")
                return local_text, "sensevoice"

    # Neither done or both failed: fallback to serial asr
    return asr(audio_bytes, fmt, session_id), "legacy"


# ── SenseVoice 本地 ASR (sherpa-onnx, 26ms, 无网络往返) ──
_sense_voice_recognizer = None
_sense_voice_lock = threading.Lock()

def _load_sense_voice():
    """懒加载 SenseVoice OfflineRecognizer (单例, 首次~528ms 后0)"""
    global _sense_voice_recognizer
    if _sense_voice_recognizer is not None:
        return _sense_voice_recognizer
    if os.getenv("SENSE_VOICE_DISABLED", "0") == "1":
        return None
    with _sense_voice_lock:
        if _sense_voice_recognizer is not None:
            return _sense_voice_recognizer
        model_dir = os.getenv("SENSE_VOICE_MODEL", os.path.join(PROJECT_DIR, "models", "sense-voice"))
        model_file = os.path.join(model_dir, "model.int8.onnx")
        tokens_file = os.path.join(model_dir, "tokens.txt")
        if not os.path.exists(model_file):
            log.warning(f"[asr] SenseVoice 模型不存在: {model_file}, 跳过本地ASR")
            return None
        try:
            import sherpa_onnx
            _sense_voice_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model_file, tokens=tokens_file,
                use_itn=True, num_threads=2, debug=False)
            log.info(f"[asr] SenseVoice 加载完成 (本地ASR, ~26ms)")
        except Exception as e:
            log.warning(f"[asr] SenseVoice 加载失败, 降级百度: {e}")
            _sense_voice_recognizer = None
        return _sense_voice_recognizer

def _asr_sense_voice(audio_bytes: bytes, fmt: str = "mp3") -> str:
    """SenseVoice 本地中文 ASR (26ms, 无网络)。audio_bytes → 16k mono wav → 识别"""
    import subprocess
    recognizer = _load_sense_voice()
    if recognizer is None:
        raise RuntimeError("SenseVoice 未加载")
    # 转换为 16k mono wav pcm
    if fmt == "wav":
        wav_bytes = audio_bytes
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"],
            input=audio_bytes, capture_output=True, timeout=10)
        wav_bytes = r.stdout
    if not wav_bytes or len(wav_bytes) < 200:
        raise RuntimeError("无有效音频")
    import numpy as np
    samples = np.frombuffer(wav_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()
    if not text:
        raise RuntimeError("SenseVoice 识别为空")
    return text

def _asr_stepfun(audio_bytes: bytes, fmt: str = "mp3") -> str:
    """StepFun ASR (OpenAI-compatible /audio/transcriptions, ~0.8s)"""
    if not os.getenv("STEPFUN_KEY", ""):
        raise RuntimeError("StepFun Key 未配置")
    if fmt != "wav":
        import subprocess
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            input=audio_bytes, capture_output=True, timeout=10)
        if not r.stdout or len(r.stdout) < 100:
            raise RuntimeError("音频转换失败")
        audio_bytes = r.stdout
    # 短音频守卫：<0.7s 跳过 StepFun，省一次无意义的云调用（no speech found ~317ms）
    wav_len = len(audio_bytes)
    duration_s = wav_len / (16000 * 2)  # 16kHz mono 16bit PCM
    if duration_s < 0.7:
        raise RuntimeError("音频过短，跳过 StepFun")
    from voice_agent import _session
    r = _session.post(
        f"{os.getenv('STEPFUN_ASR_BASE', 'https://api.stepfun.com/v1')}/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.getenv('STEPFUN_KEY', '')}"},
        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        data={"model": os.getenv("STEPFUN_ASR_MODEL", "stepaudio-2.5-asr")},
        timeout=15)
    r.raise_for_status()
    text = r.json().get("text", "").strip()
    if not text:
        raise RuntimeError("StepFun 识别为空")
    return text

def _vosk_asr(audio_bytes, fmt="mp3"):
    try:
        from vosk import Model, KaldiRecognizer
        import json as _json, wave, io
        from app.audio import to_wav
        wav = to_wav(audio_bytes, fmt)
        model_path = os.path.join(PROJECT_DIR, "web", "vosk", "vosk-model-small-en-us-0.15")
        if not os.path.exists(model_path):
            return ""
        model = Model(model_path)
        rec = KaldiRecognizer(model, 16000)
        wf = wave.open(io.BytesIO(wav), 'rb')
        while True:
            frames = wf.readframes(4000)
            if not frames:
                break
            rec.AcceptWaveform(frames)
        final = _json.loads(rec.FinalResult())
        return final.get("text", "").strip()
    except Exception:
        return ""

# 鉴权类错误码：token 无效/过期/获取失败 —— 继续用缓存只会失败到缓存期满
_BAIDU_TOKEN_ERR_NOS = {110, 111, 113}


def _baidu_invalidate_token() -> None:
    """清空内存中的百度 token，下次调用重新换取。

    凭证被吊销/换 Key 后旧 token 会持续失败最长 28 天（缓存 TTL），
    ASR/TTS 整段时间降级；命中鉴权类错误码时必须立即失效。
    """
    with _baidu_token_lock:
        _baidu_token["token"] = ""
        _baidu_token["at"] = 0.0


def _baidu_get_token() -> str:
    now = time.time()
    with _baidu_token_lock:
        if _baidu_token["token"] and now - _baidu_token["at"] < 86400 * 28:
            return _baidu_token["token"]
    from voice_agent import _session
    url = "https://aip.baidubce.com/oauth/2.0/token"
    r = _session.post(url, params={
        "grant_type": "client_credentials",
        "client_id": BAIDU_API_KEY,
        "client_secret": BAIDU_SECRET_KEY,
    }, timeout=10)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token", "")
    with _baidu_token_lock:
        _baidu_token["token"] = token
        _baidu_token["at"] = now
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=DATA_DIR, prefix=".baidu_token_tmp", suffix=".json", delete=False
        ) as f:
            tmp_name = f.name
            json.dump({"token": token, "at": now}, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, BAIDU_TOKEN_FILE)
    except Exception as e:
        log.debug(f"[agent.asr_tts] error at line 515: {e}")
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    return token


def verify_baidu_key(app_id: str = "", api_key: str = "", secret_key: str = "") -> tuple[bool, str]:
    """验证百度语音三件套是否有效（实时请求百度 OAuth 接口）。

    返回 (ok, message)。用 OAuth client_credentials 换 token：
    - 200 + access_token → 凭证有效
    - 401/400 → 凭证错误（展示百度返回的错误描述）
    - 网络异常 → 提示检查网络，但不阻断保存
    """
    app_id = (app_id or BAIDU_APP_ID).strip()
    api_key = (api_key or BAIDU_API_KEY).strip()
    secret_key = (secret_key or BAIDU_SECRET_KEY).strip()
    if not app_id or not api_key or not secret_key:
        return False, "请填全 App ID、API Key、Secret Key 三项"
    try:
        import requests as _req
        r = _req.post("https://aip.baidubce.com/oauth/2.0/token", params={
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }, timeout=8)
        if r.status_code == 200 and r.json().get("access_token"):
            return True, "百度语音 Key 验证通过"
        try:
            err = r.json()
            desc = err.get("error_description") or err.get("error") or r.text[:120]
        except Exception:
            desc = r.text[:120]
        return False, f"百度 Key 无效：{desc}"
    except Exception as e:
        return False, f"无法连接百度验证服务器（{e}），Key 已保存，可稍后重试"


def _asr_baidu(audio_bytes: bytes, fmt: str = "mp3") -> str:
    if fmt == "wav":
        if len(audio_bytes) > 44:
            audio_bytes = audio_bytes[44:]
        fmt = "pcm"
    else:
        try:
            import subprocess
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1",
                 "-f", "wav", "pipe:1"],
                input=audio_bytes, capture_output=True, timeout=10)
            if r.stdout and len(r.stdout) > 100:
                wav = r.stdout
                audio_bytes = wav[44:] if len(wav) > 44 else wav
                fmt = "pcm"
        except Exception as e:
            log.debug(f"[agent.asr_tts] error at line 568: {e}")
            pass
    token = _baidu_get_token()
    speech_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    speech_len = len(audio_bytes)
    from voice_agent import _session
    payload = {
        "format": fmt, "rate": 16000, "channel": 1,
        "token": str(token),
        "cuid": "charlie",
        "dev_pid": 1537,
        "speech": speech_b64, "len": speech_len,
    }
    if ASR_HOTWORDS:
        payload["hotword"] = ",".join(ASR_HOTWORDS)
    r = _session.post("https://vop.baidu.com/server_api",
         json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    err_no = data.get("err_no", -1)
    if err_no != 0:
        if err_no in _BAIDU_TOKEN_ERR_NOS:
            _baidu_invalidate_token()
        raise RuntimeError(f"百度ASR错误 {err_no}: {data.get('err_msg', '')}")
    return data.get("result", [""])[0]

def _tts_finna(text: str) -> bytes:
    from voice_agent import _session
    api_key = os.getenv("FINNA_API_KEY", "") or os.getenv("ALIYUN_API_KEY", "")
    base = os.getenv("FINNA_BASE", "https://www.finna.com.cn/v1")
    voice, speed = get_effective_tts_config()
    voice = resolve_voice(voice, "finna")
    r = _session.post(
        f"{base}/audio/speech",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "apikey": api_key,
        },
        json={
            "model": TTS_MODEL or "tts-flash",
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:200]}")
    return r.content

def _tts_baidu(text: str) -> bytes:
    token = _baidu_get_token()
    from voice_agent import _session
    voice, speed = get_effective_tts_config()
    per = resolve_voice(voice, "baidu")
    url = "https://tsn.baidu.com/text2audio"
    r = _session.post(url, data={
        "tex": text,
        "tok": token,
        "cuid": "charlie",
        "ctp": 1,
        "lan": "zh",
        "spd": int(speed * 5),  # 0-15，默认5
        "pit": _EMOTION_TO_BAIDU_PIT.get(get_current_emotion(), 5),
        "vol": 9,
        "per": per,
        "aue": 6,
    }, timeout=15)
    try:
        content_type = r.headers.get("Content-Type", "")
        if "json" in content_type:
            err = r.json()
            if err.get("err_no") in _BAIDU_TOKEN_ERR_NOS:
                _baidu_invalidate_token()
            raise RuntimeError(f"百度TTS错误 {err.get('err_no', -1)}: {err.get('err_msg', '')}")
        r.raise_for_status()
        return r.content
    finally:
        r.close()

def _tts_stepfun(text: str) -> bytes:
    """StepFun TTS (step-tts-2 + 系统预设音色, OpenAI兼容 /audio/speech, ~1.5s)"""
    if not os.getenv("STEPFUN_KEY", ""):
        raise RuntimeError("StepFun Key 未配置")
    from voice_agent import _session
    voice, speed = get_effective_tts_config()
    voice = resolve_voice(voice, "stepfun")
    r = _session.post(
        f"{os.getenv('STEPFUN_TTS_BASE', 'https://api.stepfun.com/v1')}/audio/speech",
        headers={
            "Authorization": f"Bearer {os.getenv('STEPFUN_KEY', '')}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("STEPFUN_TTS_MODEL", "step-tts-2"),
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"StepFun TTS HTTP {r.status_code}: {r.text[:200]}")
    return r.content

def _tts_edge(text: str) -> bytes:
    """Edge TTS (免费微软语音, zh-CN-YunxiNeural 男声, ~0.5s)"""
    import asyncio
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts 未安装，请运行: pip install edge-tts")
    voice = os.getenv("EDGE_TTS_VOICE", "zh-CN-YunxiNeural")

    async def _synthesize():
        communicate = edge_tts.Communicate(text, voice)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    try:
        audio = asyncio.run(asyncio.wait_for(_synthesize(), timeout=10))
    except asyncio.TimeoutError:
        raise RuntimeError("Edge TTS 合成超时(10s)")
    if not audio or len(audio) < 100:
        raise RuntimeError("Edge TTS 返回空音频")
    return audio


def _tts_audio8(text: str) -> bytes:
    """Audio8 TTS (本地 ONNX Runtime, 零样本音色克隆, ~7s/句)

    调用本地 Audio8 ONNX HTTP 服务（默认 127.0.0.1:8024），
    返回 MP3 格式音频（内部从 WAV 转换）。
    需要先启动 Audio8 ONNX 服务并注册至少一个声音。
    """
    base = os.getenv("AUDIO8_BASE", "http://127.0.0.1:8024")
    voice, speed = get_effective_tts_config()
    voice_name = resolve_voice(voice, "audio8")
    max_tokens = int(os.getenv("AUDIO8_MAX_TOKENS", "256"))

    from voice_agent import _session
    r = _session.post(
        f"{base}/api/tts",
        json={
            "text": text,
            "voice_name": voice_name,
            "max_new_tokens": max_tokens,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Audio8 TTS HTTP {r.status_code}: {r.text[:200]}")
    wav_bytes = r.content
    if not wav_bytes or len(wav_bytes) < 100:
        raise RuntimeError("Audio8 TTS 返回空音频")
    # Audio8 返回 WAV，转 MP3 供下游管道使用
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-f", "mp3", "-ab", "128k", "pipe:1"],
        input=wav_bytes, capture_output=True, timeout=10,
    )
    if not proc.stdout or len(proc.stdout) < 100:
        raise RuntimeError("Audio8 WAV→MP3 转换失败")
    return proc.stdout


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 44100, channels: int = 1, sample_width: int = 2) -> bytes:
    """裸 PCM s16le → 带 WAV header 的字节流（供前端 decodeAudioData 播放）。"""
    import struct
    data_len = len(pcm)
    fmt_chunk = struct.pack(b"<4sIHHIIHH", b"fmt ", 16, 1, channels, sample_rate,
                            sample_rate * channels * sample_width, channels * sample_width, sample_width * 8)
    data_chunk = struct.pack(b"<4sI", b"data", data_len) + pcm
    riff = struct.pack(b"<4sI4s", b"RIFF", 4 + len(fmt_chunk) + len(data_chunk), b"WAVE")
    return riff + fmt_chunk + data_chunk


def _tts_audio8_stream(text: str, chunk_cb=None):
    """Audio8 流式 TTS — 逐块 yield WAV base64 字符串。

    调用 Audio8 ONNX /api/tts/stream 接口，每收到一个 PCM chunk 就
    包成 WAV → base64 → yield。首次 yield 约 0.7s，后续持续输出。

    Args:
        text: 要合成的文本
        chunk_cb: 可选回调(chunk_index, elapsed) 用于日志/监控

    Yields:
        str: base64 编码的 WAV 音频片段
    """
    import requests as _req, base64 as _b64, json as _json, time as _time

    base = os.getenv("AUDIO8_BASE", "http://127.0.0.1:8024")
    voice, _speed = get_effective_tts_config()
    voice_name = resolve_voice(voice, "audio8")
    max_tokens = int(os.getenv("AUDIO8_MAX_TOKENS", "256"))
    sample_rate = 44100

    t0 = _time.time()
    resp = _req.post(
        f"{base}/api/tts/stream",
        json={
            "text": text,
            "voice_name": voice_name,
            "max_new_tokens": max_tokens,
        },
        stream=True,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Audio8 stream HTTP {resp.status_code}: {resp.text[:200]}")

    chunk_idx = 0
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        event = obj.get("event", "")
        if event == "audio_chunk":
            pcm_b64 = obj.get("pcm_b64", "")
            if not pcm_b64:
                continue
            pcm_bytes = _b64.b64decode(pcm_b64)
            wav_bytes = _pcm_to_wav_bytes(pcm_bytes, sample_rate)
            wav_b64 = _b64.b64encode(wav_bytes).decode()
            if chunk_cb:
                chunk_cb(chunk_idx, _time.time() - t0)
            chunk_idx += 1
            yield wav_b64
        elif event == "complete":
            break

# ── Provider 注册表（供 ASR_PRIORITY / TTS_PRIORITY 按需遍历） ──────────────
_ASR_PROVIDERS: dict[str, Any] = {
    "sensevoice": _asr_sense_voice,
    "baidu": _asr_baidu,
    "stepfun": _asr_stepfun,
    "vosk": _vosk_asr,
}

_TTS_PROVIDERS: dict[str, Any] = {
    "baidu": _tts_baidu,
    "finna": _tts_finna,
    "stepfun": _tts_stepfun,
    "edge": _tts_edge,
    "audio8": _tts_audio8,
    "cosyvoice": None,  # lazy import: from agent.cosyvoice_clone import tts_cosyvoice
}

def _tts_cleaned_to_mp3(cleaned: str) -> bytes:
    if not cleaned:
        return b""
    _voice, _speed = get_effective_tts_config()
    cached = _tts_cache_get(cleaned, _voice, speed=_speed)
    if cached is not None:
        return cached
    audio = tts(cleaned)
    if not audio or len(audio) < 100:
        raise TTSUnavailableError("TTS合成无有效音频")
    # TTS provider 已返回 mp3，跳过冗余 ffmpeg mp3→mp3 重编码
    # opus 编码器在 mp3_to_opus_packets 中先解码到 PCM 再编码，能处理任意码率
    _tts_cache_put(cleaned, audio, _voice, speed=_speed)
    return audio

def tts_to_mp3(text: str) -> bytes:
    cleaned = _clean_for_tts(text).strip()
    if not cleaned or len(cleaned) < 2:
        return b""
    return _tts_cleaned_to_mp3(cleaned)

def runtime_audio_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)

def write_audio_file(path: str, audio: bytes) -> str:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return path

def runtime_temp_audio_path() -> str:
    return DATA_DIR