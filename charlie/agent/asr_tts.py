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

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BAIDU_APP_ID = os.getenv("BAIDU_APP_ID", "")
BAIDU_API_KEY = os.getenv("BAIDU_API_KEY", "")
BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY", "")

# Baidu ASR hotwords (dev_pid=1537). Boosts recognition of the wake word and
# frequent command nouns so short / ambient-noise-y input is read correctly.
# Configure ASR_HOTWORDS env as a CSV to override.
ASR_HOTWORDS = [w.strip() for w in
                os.getenv("ASR_HOTWORDS", "charlie,您好,查里,小智,时间,几点了,音乐,提醒,天气,播放,打开,空调,电视,记得,帮我,设置,搜索,写,读,记事本")
                .split(",") if w.strip()]
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
    except Exception:
        pass

TTS_CACHE_MAX_CHARS = int(os.getenv("TTS_CACHE_MAX_CHARS", "20"))
TTS_CACHE_TTL = 3600.0
TTS_CACHE_MAX = 50
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
    global _tts_speed, _tts_unavailable_until, _tts_failures

    BAIDU_APP_ID = os.getenv("BAIDU_APP_ID", "")
    BAIDU_API_KEY = os.getenv("BAIDU_API_KEY", "")
    BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY", "")

    TTS_VOICE = os.getenv("TTS_VOICE", "Ethan")
    TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-flash")
    TTS_FAILURE_COOLDOWN = float(os.getenv("TTS_FAILURE_COOLDOWN", "120"))
    TTS_FAILURE_THRESHOLD = int(os.getenv("TTS_FAILURE_THRESHOLD", "3"))
    _tts_speed = float(os.getenv("TTS_SPEED", "1.0"))

    # 清空旧 token（内存 + 落盘文件），下次用新凭证重新获取
    with _baidu_token_lock:
        _baidu_token["token"] = ""
        _baidu_token["at"] = 0.0
    try:
        if os.path.exists(BAIDU_TOKEN_FILE):
            os.remove(BAIDU_TOKEN_FILE)
    except Exception:
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
    # Sidebars/remark glosses and emoji don't add speech value.
    t = _TTS_EMOJI_RE.sub('', t)
    t = _TTS_PAREN_RE.sub('', t)
    t = _TTS_WHITESPACE_RE.sub(' ', t)
    return t.strip()

def _tts_cache_get(text: str, voice: str = "", model: str = "") -> Optional[bytes]:
    key = (text, voice or TTS_VOICE, model or TTS_MODEL)
    with _tts_lock:
        item = _tts_cache.get(key)
        if item is None:
            return None
        audio, ts = item
        if time.time() - ts > TTS_CACHE_TTL:
            _tts_cache.pop(key, None)
            return None
        return audio

def _tts_cache_put(text: str, audio: bytes, voice: str = "", model: str = "") -> None:
    if len(text) <= TTS_CACHE_MAX_CHARS and len(audio) > 100:
        with _tts_lock:
            _tts_cache[(text, voice or TTS_VOICE, model or TTS_MODEL)] = (audio, time.time())
            if len(_tts_cache) > TTS_CACHE_MAX:
                _tts_cache.pop(next(iter(_tts_cache)), None)

def tts(text: str) -> bytes:
    from agent.retry import _retry
    global _tts_unavailable_until, _tts_failures
    cleaned = _clean_for_tts(text).strip()
    if not cleaned:
        return b""
    cached = _tts_cache_get(cleaned)
    if cached is not None:
        return cached
    now = time.time()
    with _tts_lock:
        if now < _tts_unavailable_until:
            raise TTSUnavailableError(f"TTS冷却熔断, 剩余{_tts_unavailable_until - now:.0f}s")
    try:
        # 百度TTS主用(~120ms vs Finna~1085ms, 快9倍), 失败降级Finna
        audio = _retry(lambda: _tts_baidu(cleaned), name="TTS(百度)")
    except TTSUnavailableError:
        raise
    except Exception as e:
        log.warning(f"百度TTS失败, 降级Finna: {e}")
        try:
            audio = _retry(lambda: _tts_finna(cleaned), name="TTS(Finna降级)")
        except Exception as e2:
            log.warning(f"Finna降级也失败: {e2}")
            with _tts_lock:
                _tts_failures += 1
                _tts_unavailable_until = now + TTS_FAILURE_COOLDOWN
            log.warning(f"TTS失败熔断{TTS_FAILURE_COOLDOWN:g}秒(连续失败{_tts_failures}次)")
            raise TTSUnavailableError(str(e2)) from e2
    with _tts_lock:
        _tts_failures = 0
        _tts_unavailable_until = 0.0
    _tts_cache_put(cleaned, audio)
    return audio

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

def asr(audio_bytes: bytes, fmt: str = "mp3") -> str:
    # 本地 SenseVoice 优先(26ms vs 百度327ms, 无网络往返), 失败降级百度→Vosk
    try:
        return _asr_sense_voice(audio_bytes, fmt)
    except Exception as e:
        log.warning(f"SenseVoice ASR失败, 降级百度: {e}")
    try:
        return _asr_baidu(audio_bytes, fmt)
    except Exception as e:
        log.warning(f"百度ASR失败，降级到Vosk: {e}")
    now = time.time()
    with _asr_lock:
        _asr_fallback_times.append(now)
        while _asr_fallback_times and _asr_fallback_times[0] < now - 30:
            _asr_fallback_times.pop(0)
        if len(_asr_fallback_times) > 3:
            log.error(f"ASR降级太频繁({len(_asr_fallback_times)}次/30s)，跳过云知声，返回空")
            return ""
    try:
        text = _vosk_asr(audio_bytes, fmt)
        if text:
            log.info(f"[asr] Vosk fallback: {text[:40]}")
            return text
    except Exception as e:
        log.warning(f"[asr] Vosk failed: {e}")
    return ""

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
            json.dump({"token": token, "at": now}, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, BAIDU_TOKEN_FILE)
    except Exception:
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
        except Exception:
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
        raise RuntimeError(f"百度ASR错误 {err_no}: {data.get('err_msg', '')}")
    return data.get("result", [""])[0]

def _tts_finna(text: str) -> bytes:
    from voice_agent import _session
    api_key = os.getenv("FINNA_API_KEY", "") or os.getenv("ALIYUN_API_KEY", "")
    base = os.getenv("FINNA_BASE", "https://www.finna.com.cn/v1")
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
            "voice": TTS_VOICE or "Ethan",
            "response_format": "mp3",
            "speed": _tts_speed,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:200]}")
    return r.content

def _tts_baidu(text: str) -> bytes:
    token = _baidu_get_token()
    from voice_agent import _session
    url = "https://tsn.baidu.com/text2audio"
    r = _session.post(url, data={
        "tex": text,
        "tok": token,
        "cuid": "charlie",
        "ctp": 1,
        "lan": "zh",
        "spd": 5,
        "pit": 5,
        "vol": 9,
        "per": 3,
        "aue": 6,
    }, timeout=15)
    content_type = r.headers.get("Content-Type", "")
    if "json" in content_type:
        err = r.json()
        raise RuntimeError(f"百度TTS错误 {err.get('err_no', -1)}: {err.get('err_msg', '')}")
    r.raise_for_status()
    return r.content

def _tts_cleaned_to_mp3(cleaned: str) -> bytes:
    if not cleaned:
        return b""
    cached = _tts_cache_get(cleaned)
    if cached is not None:
        return cached
    audio = tts(cleaned)
    if not audio or len(audio) < 100:
        raise TTSUnavailableError("TTS合成无有效音频")
    import subprocess
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-b:a", "32k", "-ac", "1", "-f", "mp3", "pipe:1"],
            input=audio, capture_output=True, timeout=10)
        result = r.stdout if r.stdout and len(r.stdout) > 100 else audio
    except Exception:
        result = audio
    _tts_cache_put(cleaned, result)
    return result

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