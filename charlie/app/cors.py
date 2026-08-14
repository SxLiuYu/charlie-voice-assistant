"""Dynamic CORS origin management + rate limiting helpers.

Extracted from voice_server.py.
"""
import os, time, logging, threading
from collections.abc import Callable

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import (
    configured_cors_origins, invalidate_lan_origins_cache, lan_origins, localhost_origins,
)
from app.state import _rate_buckets, _session_buckets, _RATE_GENERAL, _RATE_VOICE, _RATE_WINDOW, _RATE_LOCK

log = logging.getLogger("magic")

def tunnel_origins() -> list[str]:
    TUNNEL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tunnel_url.txt")
    try:
        with open(TUNNEL_FILE, encoding="utf-8") as f:
            tunnel = f.read().strip()
        return [tunnel] if tunnel else []
    except OSError:
        return []

def read_tunnel_url() -> str | None:
    TUNNEL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tunnel_url.txt")
    try:
        with open(TUNNEL_FILE, encoding="utf-8") as f:
            url = f.read().strip()
        return url if url.startswith("https://") else None
    except OSError:
        return None

def get_lan_ip() -> str | None:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None

_CORS_ORIGIN_TTL_SECONDS = 2.0
_cors_origins_loaded_at = 0.0
_cors_origins = [
    *localhost_origins(),
    *lan_origins(),
    *tunnel_origins(),
    *configured_cors_origins(),
]

def refresh_cors_origins(force: bool = False) -> list[str]:
    global _cors_origins_loaded_at
    now = time.monotonic()
    if not force and _cors_origins and now - _cors_origins_loaded_at < _CORS_ORIGIN_TTL_SECONDS:
        return []
    invalidate_lan_origins_cache()
    tunnel = tunnel_origins()
    origins = [*localhost_origins(), *lan_origins(), *tunnel, *configured_cors_origins()]
    _cors_origins[:] = list(dict.fromkeys(origins))
    _cors_origins_loaded_at = now
    return tunnel

def reload_cors_origins() -> list[str]:
    return refresh_cors_origins(force=True)

def get_cors_origins() -> list[str]:
    return list(_cors_origins)

class DynamicCORSMiddleware(CORSMiddleware):
    def __init__(self, app, allow_origins=(), **kwargs):
        if callable(allow_origins):
            self._origin_provider = allow_origins
        else:
            self._origin_provider = lambda: allow_origins
        super().__init__(app, allow_origins=list(self._origin_provider()), **kwargs)

    def _refresh_origins(self):
        origins = list(self._origin_provider())
        allow_all = "*" in origins
        self.allow_origins = origins
        self.allow_all_origins = allow_all
        self.preflight_explicit_allow_origin = not allow_all or self.allow_credentials
        if allow_all:
            self.simple_headers["Access-Control-Allow-Origin"] = "*"
        else:
            self.simple_headers.pop("Access-Control-Allow-Origin", None)
        if self.preflight_explicit_allow_origin:
            self.preflight_headers["Vary"] = "Origin"
            self.preflight_headers.pop("Access-Control-Allow-Origin", None)
        else:
            self.preflight_headers["Access-Control-Allow-Origin"] = "*"
            self.preflight_headers.pop("Vary", None)

    async def __call__(self, scope, receive, send):
        refresh_cors_origins()
        self._refresh_origins()
        return await super().__call__(scope, receive, send)

# ===== Rate limiting =====
def check_rate(ip: str, bucket_type: str, limit: int) -> tuple:
    now = time.time()
    with _RATE_LOCK:
        if len(_rate_buckets) > 1000:
            expired = [k for k, v in _rate_buckets.items()
                       if all(not v.get(bt) or now - v[bt][-1] > _RATE_WINDOW
                              for bt in ("voice", "general"))]
            for k in expired:
                del _rate_buckets[k]
        if ip not in _rate_buckets:
            _rate_buckets[ip] = {}
        bucket = _rate_buckets[ip].setdefault(bucket_type, [])
        bucket[:] = [ts for ts in bucket if now - ts < _RATE_WINDOW]
        if len(bucket) >= limit:
            retry_after = int(_RATE_WINDOW - (now - bucket[0])) + 1
            return False, 0, retry_after
        bucket.append(now)
        return True, limit - len(bucket), 0
