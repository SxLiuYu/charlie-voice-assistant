"""CosyVoice 语音克隆接入

调用 CosyVoice Web API 进行零样本语音克隆合成。
需要预先运行 CosyVoice 服务 (本地或远程)。

配置环境变量:
  COSYVOICE_BASE  - CosyVoice API 地址 (默认 http://127.0.0.1:8080)
  COSYVOICE_SPEAKER - 默认说话人ID (默认: 中文女)

使用流程:
  1. 上传参考音频 → create_voice_profile(name, audio_path)
  2. 合成语音 → synthesize(text, voice_profile_name)
  3. 管理声音 → list_voices() / delete_voice(name)

集成到 TTS:
  - 在 asr_tts.py 的 _TTS_PROVIDERS 注册 "cosyvoice"
  - 在 _VOICE_MAP 添加 cosyvoice 映射
"""
import os
import json
import shutil
import logging
import hashlib
import threading
from datetime import datetime, timezone

log = logging.getLogger("magic")

# CosyVoice API 地址
_COSYVOICE_BASE = os.getenv("COSYVOICE_BASE", "http://127.0.0.1:8080").rstrip("/")
_DEFAULT_SPEAKER = os.getenv("COSYVOICE_SPEAKER", "中文女")
_REQUEST_TIMEOUT = int(os.getenv("COSYVOICE_TIMEOUT", "60"))

# 声音配置存储目录
_VOICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "cosyvoice_voices")
_VOICE_DIR_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _ensure_voice_dir():
    """确保声音配置目录存在。"""
    os.makedirs(_VOICE_DIR, exist_ok=True)


def _voice_path(name: str) -> str:
    """返回声音配置 JSON 文件路径。"""
    safe = hashlib.sha256(name.encode()).hexdigest()[:16]
    return os.path.join(_VOICE_DIR, f"{safe}.json")


def _voice_audio_path(name: str) -> str:
    """返回声音参考音频文件路径。"""
    safe = hashlib.sha256(name.encode()).hexdigest()[:16]
    return os.path.join(_VOICE_DIR, f"{safe}_ref.wav")


# ===== 声音管理 =====

def create_voice_profile(name: str, audio_path: str, description: str = "") -> dict:
    """创建或更新语音克隆配置。

    参数:
      name: 声音名称 (用户自定义, 如 "小明", "助手女声")
      audio_path: 参考音频文件路径 (WAV/MP3, 3-10秒)
      description: 声音描述 (可选)

    返回:
      {"ok": True, "name": name, "speaker_id": "..."} 或错误信息
    """
    if not name or not name.strip():
        return {"ok": False, "error": "声音名称不能为空"}
    if not audio_path or not os.path.isfile(audio_path):
        return {"ok": False, "error": f"参考音频不存在: {audio_path}"}

    name = name.strip()

    # 复制参考音频到配置目录（_upload_audio 涉及网络请求，放在锁外避免长持锁）
    _ensure_voice_dir()
    dest_audio = _voice_audio_path(name)
    try:
        shutil.copy2(audio_path, dest_audio)
    except Exception as e:
        return {"ok": False, "error": f"复制音频失败: {e}"}

    # 构建声音配置
    # 如果 CosyVoice 服务在线，先上传获取 speaker_id
    speaker_id = _upload_audio(dest_audio, name)
    if not speaker_id:
        # 上传失败没关系，本地记录 speaker_id 为空，合成时再尝试
        speaker_id = ""

    profile = {
        "name": name,
        "speaker_id": speaker_id,
        "audio_path": dest_audio,
        "description": description,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    vpath = _voice_path(name)
    try:
        with _VOICE_DIR_LOCK:
            with open(vpath, "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)
        log.info(f"[cosyvoice] 创建声音配置: {name} → {vpath}")
        return {
            "ok": True,
            "name": name,
            "speaker_id": speaker_id,
            "description": description,
            "created_at": profile["created_at"],
        }
    except Exception as e:
        return {"ok": False, "error": f"写入配置失败: {e}"}


def list_voices() -> list[dict]:
    """列出所有已配置的语音克隆声音。

    返回:
      [{"name": "小明", "description": "...", "speaker_id": "...", ...}, ...]
    """
    _ensure_voice_dir()
    voices = []
    with _VOICE_DIR_LOCK:
        for f in os.listdir(_VOICE_DIR):
            if f.endswith(".json"):
                fpath = os.path.join(_VOICE_DIR, f)
                try:
                    with open(fpath, "r", encoding="utf-8") as fh:
                        voices.append(json.load(fh))
                except Exception:
                    pass
    return sorted(voices, key=lambda v: v.get("name", ""))


def delete_voice(name: str) -> dict:
    """删除一个声音配置及参考音频。

    返回: {"ok": True/False, "error": "..."}
    """
    vpath = _voice_path(name)
    apath = _voice_audio_path(name)
    ok = True
    with _VOICE_DIR_LOCK:
        for p in [vpath, apath]:
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception as e:
                    log.warning(f"[cosyvoice] 删除失败 {p}: {e}")
                    ok = False
    if ok:
        log.info(f"[cosyvoice] 删除声音: {name}")
    return {"ok": ok}


def get_voice_audio(name: str) -> str | None:
    """获取指定声音的参考音频路径。

    返回: 音频文件路径, 不存在返回 None。
    """
    apath = _voice_audio_path(name)
    return apath if os.path.isfile(apath) else None


# ===== CosyVoice API 调用 =====

def _upload_audio(audio_path: str, name: str) -> str:
    """上传参考音频到 CosyVoice 服务获取 speaker_id。

    返回: speaker_id 字符串, 失败返回空串。
    """
    try:
        import requests
    except ImportError:
        log.warning("[cosyvoice] requests 未安装，跳过上传")
        return ""

    url = f"{_COSYVOICE_BASE}/upload"
    try:
        with open(audio_path, "rb") as f:
            files = {"audio": (os.path.basename(audio_path), f, "audio/wav")}
            data = {"name": name}
            resp = requests.post(url, files=files, data=data, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 200:
            result = resp.json()
            sid = result.get("speaker_id", result.get("id", ""))
            log.info(f"[cosyvoice] 上传成功: {name} → speaker_id={sid}")
            return sid
        log.warning(f"[cosyvoice] 上传失败 {resp.status_code}: {resp.text[:200]}")
        return ""
    except Exception as e:
        log.warning(f"[cosyvoice] 上传异常: {e}")
        return ""


def synthesize(text: str, voice: str = "", speed: float = 1.0) -> bytes | None:
    """使用 CosyVoice 语音克隆合成音频。

    参数:
      text: 要合成的文本
      voice: 声音名称（canonical 音色名如 "中文女"，或用户克隆声名如 "小明"；
             空串使用默认说话人）
      speed: 语速 (0.5-2.0)

    返回:
      MP3 音频字节, 失败返回 None。
    """
    if not text or not text.strip():
        return None

    voice_name = voice.strip() if voice else _DEFAULT_SPEAKER

    # 用户克隆声音优先于内置说话人：同名时取克隆配置的 speaker_id + 参考音频
    profiles = {v["name"]: v for v in list_voices()}
    speaker_id = ""
    ref_audio = ""
    if voice_name in profiles:
        speaker_id = profiles[voice_name].get("speaker_id", "")
        ref_audio = profiles[voice_name].get("audio_path", "")

    # 构建请求
    payload = {
        "text": text,
        "speaker": voice_name,
        "speaker_id": speaker_id,
        "speed": max(0.5, min(2.0, speed)),
    }
    if ref_audio and not speaker_id:
        # 没有 speaker_id 但有参考音频：先上传再合成
        payload["reference_audio"] = ref_audio

    try:
        import requests
        url = f"{_COSYVOICE_BASE}/api/tts"
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("audio"):
            wav_bytes = resp.content
        elif resp.status_code == 200:
            data = resp.json()
            # 有些实现返回 base64 音频
            b64 = data.get("audio", data.get("data", ""))
            if b64:
                import base64
                wav_bytes = base64.b64decode(b64)
            else:
                log.warning(f"[cosyvoice] 合成返回无音频数据: {resp.text[:200]}")
                return None
        else:
            log.warning(f"[cosyvoice] TTS 错误 {resp.status_code}: {resp.text[:200]}")
            return None

        if not wav_bytes or len(wav_bytes) < 100:
            return None

        # WAV → MP3
        import subprocess
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-f", "mp3", "-ab", "128k", "pipe:1"],
            input=wav_bytes, capture_output=True, timeout=15,
        )
        if proc.stdout and len(proc.stdout) > 100:
            return proc.stdout
        # 转换失败不能返回原始 WAV：tts() 链及下游播放器假定 MP3，
        # 返回 WAV 会让本 provider 假成功、跳过 fallback 链
        log.warning(f"[cosyvoice] WAV→MP3 转换失败: {proc.stderr.decode(errors='ignore')[:200]}")
        return None
    except Exception as e:
        log.warning(f"[cosyvoice] 合成异常: {e}")
        return None


def is_available() -> bool:
    """检查 CosyVoice 服务是否在线。"""
    try:
        import requests
        url = f"{_COSYVOICE_BASE}/health"
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ===== TTS Provider =====

def tts_cosyvoice(text: str) -> bytes:
    """CosyVoice TTS provider — 语音克隆合成。

    适配 asr_tts.py 的 _TTS_PROVIDERS 调用约定 (text → mp3 bytes)。

    优先使用用户配置的自定义声音, 回退到默认说话人。
    失败时抛出异常（由 asr_tts.py 的 fallback chain 捕获）。
    """
    voice, speed = _get_effective_config_for_provider("cosyvoice")
    result = synthesize(text, voice=voice, speed=speed)
    if result is None:
        raise RuntimeError("CosyVoice 合成失败")
    return result


def _get_effective_config_for_provider(provider: str) -> tuple[str, float]:
    """获取当前有效的声音和语速配置。"""
    # 复用现有 canonical voice 系统
    try:
        from agent.asr_tts import get_effective_tts_config, resolve_voice
        canonical, speed = get_effective_tts_config()
        voice_name = resolve_voice(canonical, provider)
        return voice_name, speed
    except Exception:
        return _DEFAULT_SPEAKER, 1.0


# ===== MCP 工具 =====

# ===== LLM 可调用的工具函数（供 magic-jarvis.py 注册为 @mcp.tool）=====
# 注意 create_voice 的 audio_path 参数：LLM 只能传服务器上已存在的文件路径
# （如用户先通过管理页/REST 上传录音再让助手建档），无法凭空生成音频。

def create_voice(name: str, audio_path: str, description: str = "") -> str:
    """上传参考音频创建语音克隆配置。

    参数:
      name: 声音名称 (如 "小明", "客服女声")
      audio_path: 服务器上参考音频文件路径 (3-10秒 WAV/MP3)
      description: 声音描述 (可选)

    例: create_voice("小明", "/tmp/reference.wav", "年轻男声")
    """
    if not os.path.isfile(audio_path):
        return f"参考音频文件不存在: {audio_path}"
    result = create_voice_profile(name, audio_path, description)
    if result.get("ok"):
        return f"声音配置已创建: {name}\n参考音频: {audio_path}"
    return f"创建失败: {result.get('error')}"


def list_voice_profiles() -> str:
    """列出所有已配置的语音克隆声音。"""
    voices = list_voices()
    if not voices:
        return "暂无语音克隆配置。使用 create_voice 创建。"
    lines = [f"共 {len(voices)} 个声音配置："]
    for v in voices:
        desc = v.get("description", "") or "-"
        lines.append(f"  - {v['name']}: {desc}")
    return "\n".join(lines)


def delete_voice_profile(name: str) -> str:
    """删除一个语音克隆配置。

    例: delete_voice_profile("小明")
    """
    result = delete_voice(name)
    if result.get("ok"):
        return f"已删除声音配置: {name}"
    return f"删除失败: {result.get('error')}"


def voice_status() -> str:
    """检查 CosyVoice 服务状态。"""
    if is_available():
        voices = list_voices()
        return (f"CosyVoice 服务在线\n"
                f"  地址: {_COSYVOICE_BASE}\n"
                f"  默认说话人: {_DEFAULT_SPEAKER}\n"
                f"  已配置声音: {len(voices)} 个")
    return (f"CosyVoice 服务离线\n"
            f"  地址: {_COSYVOICE_BASE}\n"
            f"  请先启动 CosyVoice wrapper 服务 (docs/cosyvoice-wrapper.md)")
