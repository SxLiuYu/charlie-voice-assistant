"""HTTP response helpers: ETag, conditional GET, cached text, JSON/HTML responses, SSE event serialization.

Extracted from voice_server.py to enable route modules to share response utilities
without importing the monolith.
"""
import os, json, hashlib, threading
from collections.abc import Callable
from fastapi import Request, Response
from fastapi.responses import HTMLResponse

# ===== ETag / conditional GET =====

def weak_etag(token: str) -> str:
    """Build a compact weak ETag from an opaque cache token."""
    return 'W/"' + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] + '"'

def etag_headers(etag: str) -> dict:
    return {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}

def if_none_matches(request: Request, etag: str) -> bool:
    """Match If-None-Match, tolerating comma-separated weak/strong ETags."""
    cached_etags = []
    for value in request.headers.get("if-none-match", "").split(","):
        value = value.strip()
        if not value:
            continue
        cached_etags.append(value)
        if value.startswith("W/"):
            cached_etags.append(value[2:])
        else:
            cached_etags.append("W/" + value)
    return etag in cached_etags

def not_modified_response(etag: str) -> Response:
    return Response(status_code=304, headers=etag_headers(etag))

def file_etag_token(path: str, prefix: str) -> str:
    """Return a stable file token without opening or reading the file."""
    try:
        stat = os.stat(path)
        return f"{prefix}:{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"
    except FileNotFoundError:
        return f"{prefix}:missing"
    except OSError as exc:
        return f"{prefix}:error:{exc.__class__.__name__}"

def file_not_modified_response(request: Request, path: str, prefix: str) -> Response | None:
    """Return 304 only if both the request ETag and current file metadata still match."""
    etag = weak_etag(file_etag_token(path, prefix))
    if not if_none_matches(request, etag):
        return None
    if weak_etag(file_etag_token(path, prefix)) != etag:
        return None
    return not_modified_response(etag)

# ===== Cached text reader =====

_open_text_file = open
_text_file_cache: dict[str, tuple[tuple[int, int, int], str]] = {}
_text_file_cache_lock = threading.Lock()

def read_cached_text(path: str, return_token: bool = False):
    """Read a small static text file, reusing contents while file metadata is unchanged."""
    for _ in range(2):
        stat = os.stat(path)
        token = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        with _text_file_cache_lock:
            cached = _text_file_cache.get(path)
        if cached is not None and cached[0] == token:
            return (cached[1], token) if return_token else cached[1]

        with _open_text_file(path, encoding="utf-8") as f:
            text = f.read()

        after_stat = os.stat(path)
        after_token = (after_stat.st_mtime_ns, after_stat.st_size, after_stat.st_ino)
        if token == after_token:
            with _text_file_cache_lock:
                cached = _text_file_cache.get(path)
                if cached is None or cached[0] != token:
                    _text_file_cache[path] = (token, text)
            return (text, token) if return_token else text
    return (text, token) if return_token else text

def html_response(request: Request, path: str, prefix: str) -> Response:
    """Return HTML with a file-based weak ETag and no-store validation headers."""
    cached = file_not_modified_response(request, path, prefix)
    if cached is not None:
        return cached
    text, token = read_cached_text(path, return_token=True)
    etag = weak_etag(f"{prefix}:{token[0]}:{token[1]}:{token[2]}")
    if if_none_matches(request, etag):
        return not_modified_response(etag)
    body = text.encode("utf-8")
    headers = etag_headers(etag)
    headers["Content-Length"] = str(len(body))
    if request.method == "HEAD":
        body = b""
    return Response(content=body, media_type="text/html; charset=utf-8", headers=headers)

def json_response(
    request: Request,
    payload: dict | Callable[[], dict],
    etag_token: str | None = None,
) -> Response:
    """Return compact JSON with a weak ETag for polling-heavy GET endpoints."""
    if etag_token is not None:
        etag = weak_etag(etag_token)
        if if_none_matches(request, etag):
            return not_modified_response(etag)
        resolved_payload = payload() if callable(payload) else payload
        body = json.dumps(resolved_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        resolved_payload = payload() if callable(payload) else payload
        body = json.dumps(resolved_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = 'W/"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if if_none_matches(request, etag):
            return not_modified_response(etag)
    return Response(content=body, media_type="application/json", headers=etag_headers(etag))

# ===== Manifest =====

_manifest_lock = threading.Lock()
_MANIFEST_BODY: tuple[bytes, str] | None = None

def build_manifest_payload() -> dict:
    return {
        "name": "Charlie",
        "short_name": "Charlie",
        "description": "中国版贾维斯 - AI语音助理，全屋智能家居控制",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f0c29",
        "theme_color": "#e94560",
        "icons": [
            {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml"},
            {"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml"}
        ],
        "categories": ["productivity", "lifestyle", "utilities"],
        "lang": "zh-CN",
        "dir": "ltr"
    }

def manifest_response(request: Request) -> Response:
    """Serve the immutable PWA manifest once, supporting HEAD and conditional GET."""
    global _MANIFEST_BODY
    with _manifest_lock:
        if _MANIFEST_BODY is None:
            body = json.dumps(
                build_manifest_payload(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            etag = 'W/"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            _MANIFEST_BODY = (body, etag)
        body, etag = _MANIFEST_BODY

    if if_none_matches(request, etag):
        return not_modified_response(etag)

    headers = etag_headers(etag)
    headers["Content-Length"] = str(len(body))
    content = b"" if request.method == "HEAD" else body
    return Response(content=content, media_type="application/json", headers=headers)

# ===== SSE event serialization =====

def sse_event(event: dict) -> str:
    """Serialize one compact SSE data frame."""
    return f'data: {json.dumps(event, ensure_ascii=False, separators=(",", ":"))}\n\n'

SSE_DONE_FRAME = 'data: {"type":"done"}\n\n'
SSE_HEARTBEAT_FRAME = ': heartbeat\n\n'
SSE_EVENT_HEARTBEAT_FRAME = sse_event({"type": "heartbeat", "text": "", "time": ""})
