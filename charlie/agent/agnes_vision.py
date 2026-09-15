"""Agnes 视觉模型接入

调用 Agnes ARK API 的 vision 能力分析图片 (base64 编码发送)。
兼容 OpenAI vision 格式: content = [{"type": "text", ...}, {"type": "image_url", ...}]

配置环境变量:
  AGNES_KEY   - API key (必填)
  AGNES_BASE  - API base URL (默认 https://apihub.agnes-ai.com/v1)
  AGNES_MODEL - 模型名 (默认 agnes-2.0-flash, 需支持 vision)

集成方式:
  - agent/vision.py 的 analyze_image_bytes() 优先调用 agnes_analyze()
  - 失败时回退到 _run_vision_script() (mimo-vision)
  - direct_camera_vision_analyze() 同理
"""
import os
import base64
import logging
import time
import requests

log = logging.getLogger("magic")

#  Agnes API 配置
_AGNES_BASE = os.getenv("AGNES_BASE", "https://apihub.agnes-ai.com/v1").rstrip("/")
_AGNES_KEY = os.getenv("AGNES_KEY", "")
_AGNES_MODEL = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
# S-Agnes (新加坡节点，有时更快)
_SAGNES_BASE = os.getenv("SAGNES_BASE", "https://api-sg.agnes-ai.com/v1").rstrip("/")
_SAGNES_KEY = os.getenv("SAGNES_KEY", "")
_SAGNES_MODEL = os.getenv("SAGNES_MODEL", "agnes-2.0-flash")

# 请求超时 (vision 通常需要 10-30s)
_REQUEST_TIMEOUT = int(os.getenv("AGNES_VISION_TIMEOUT", "30"))

# 降级图片尺寸 (太大可能导致 API 超时或报错)
_MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4MB
_RESIZE_IF_LARGER = (1024, 1024)     # 超过此尺寸尝试压缩


def _is_configured() -> bool:
    """检查是否有可用的 Agnes API key。"""
    return bool(_AGNES_KEY) or bool(_SAGNES_KEY)


def _get_provider() -> tuple[str, str, str]:
    """选择 provider (优先 AGNES, 回退 SAGNES)。返回 (base, key, model)。"""
    if _AGNES_KEY:
        return _AGNES_BASE, _AGNES_KEY, _AGNES_MODEL
    if _SAGNES_KEY:
        return _SAGNES_BASE, _SAGNES_KEY, _SAGNES_MODEL
    return "", "", ""


def _maybe_resize(image_bytes: bytes) -> bytes:
    """图片过大时压缩 (使用 PIL/Pillow)。失败返回原图。"""
    if len(image_bytes) <= _MAX_IMAGE_BYTES:
        return image_bytes
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail(_RESIZE_IF_LARGER, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        fmt = img.format or "JPEG"
        img.save(buf, format=fmt, quality=85)
        return buf.getvalue()
    except Exception as e:
        log.debug(f"[agnes_vision] 压缩失败，使用原图: {e}")
        return image_bytes


def agnes_analyze(image_bytes: bytes, question: str) -> str:
    """调用 Agnes vision API 分析图片。

    参数:
      image_bytes: 图片原始字节 (JPEG/PNG)
      question:    用户问题 (发给 vision model 的 prompt)

    返回:
      分析结果文本字符串, 失败返回空串。

    失败原因:
      - AGNES_KEY 未配置 → 空串 (调用方应回退)
      - API 超时 → 空串
      - API 返回 4xx/5xx → 空串
    """
    if not _is_configured():
        log.debug(f"[agnes_vision] 未配置 AGNES_KEY，跳过 vision 分析")
        return ""

    base_url, api_key, model = _get_provider()
    if not base_url or not api_key:
        return ""

    # 压缩过大图片
    img_bytes = _maybe_resize(image_bytes)

    # Base64 编码
    try:
        b64 = base64.b64encode(img_bytes).decode("utf-8")
    except Exception as e:
        log.warning(f"[agnes_vision] base64 编码失败: {e}")
        return ""

    # 检测 MIME 类型
    mime = "image/jpeg"
    if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif img_bytes[:4] == b"GIF8":
        mime = "image/gif"
    elif img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        mime = "image/webp"

    data_url = f"data:{mime};base64,{b64}"

    # 构建 OpenAI 兼容请求
    system_prompt = os.getenv(
        "AGNES_VISION_SYSTEM_PROMPT",
        "你是 Charlie 视觉助手。分析用户提供的图片，用中文简洁回答。"
        "如果用户问的是具体问题（比如这是什么、几点了），直接回答；"
        "如果是开放问题，描述图片的关键内容。"
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "max_tokens": int(os.getenv("AGNES_VISION_MAX_TOKENS", "1000")),
        "temperature": float(os.getenv("AGNES_VISION_TEMPERATURE", "0.3")),
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = f"{base_url}/chat/completions"

    try:
        from voice_agent import _session  # 仓库惯例: 复用连接池
        t0 = time.time()
        resp = _session.post(url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT)
        try:
            elapsed = time.time() - t0

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    text = choices[0].get("message", {}).get("content", "").strip()
                    usage = data.get("usage", {})
                    log.info(f"[agnes_vision] 分析成功 {elapsed:.1f}s "
                             f"tokens={usage.get('total_tokens', '?')}: {text[:60]}...")
                    return text
                log.warning(f"[agnes_vision] 响应无 choices: {resp.text[:200]}")
                return ""
            else:
                log.warning(f"[agnes_vision] API 错误 {resp.status_code}: {resp.text[:300]}")
                return ""
        finally:
            resp.close()
    except requests.exceptions.Timeout:
        log.warning(f"[agnes_vision] 请求超时 ({_REQUEST_TIMEOUT}s)")
        return ""
    except requests.exceptions.ConnectionError as e:
        log.warning(f"[agnes_vision] 连接失败: {e}")
        return ""
    except Exception as e:
        log.warning(f"[agnes_vision] 未知错误: {e}")
        return ""


# ===== 状态查询（供 magic-jarvis.py 注册为 MCP 工具）=====

def vision_status() -> str:
    """检查 Agnes vision 配置状态。"""
    if not _is_configured():
        return "Agnes vision 未配置 (缺少 AGNES_KEY 环境变量)。"
    base, key, model = _get_provider()
    return (f"Agnes vision 已配置:\n"
            f"  Base: {base}\n"
            f"  Model: {model}\n"
            f"  Timeout: {_REQUEST_TIMEOUT}s\n"
            f"  状态: 就绪")

# 说明: 图片分析不提供 MCP 工具——LLM 无法产生数 MB 的图片 base64 参数，
# 视觉入口是语音关键词/摄像头快路径（agent/vision.py），不走 LLM 工具调用。
