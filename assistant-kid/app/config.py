"""Runtime configuration shared by server entry points and status routes."""
import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urlparse, urlunparse

import psutil


_VIRTUAL_INTERFACE_PREFIXES = (
    "lo", "utun", "bridge", "awdl", "llw", "anpi", "ap", "gif", "stf"
)
_PREFERRED_INTERFACE_PREFIXES = ("en", "eth", "wlan")
LAN_ORIGINS_TTL_SECONDS = 2.0
_lan_origins_cache = {"at": 0.0, "key": None, "value": []}
_lan_origins_lock = threading.Lock()


def invalidate_lan_origins_cache():
    with _lan_origins_lock:
        _lan_origins_cache["at"] = 0.0
        _lan_origins_cache["key"] = None
        _lan_origins_cache["value"] = []


def _env_port(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        return default
    if not 1 <= port <= 65535:
        return default
    return port


def http_port() -> int:
    return _env_port("ASSISTANT_KID_HTTP_PORT", 8000)


def https_port() -> int:
    return _env_port("ASSISTANT_KID_HTTPS_PORT", 8443)


def localhost_origins() -> list[str]:
    return [
        f"http://localhost:{http_port()}",
        f"http://localhost:{https_port()}",
        f"https://localhost:{https_port()}",
    ]


def lan_origins() -> list[str]:
    """Return private LAN origins for the configured HTTP and HTTPS ports."""
    now = time.time()
    cache_key = (http_port(), https_port())
    with _lan_origins_lock:
        cached = _lan_origins_cache["value"]
        if (
            _lan_origins_cache["key"] == cache_key
            and now - _lan_origins_cache["at"] < LAN_ORIGINS_TTL_SECONDS
        ):
            return list(cached)

    candidates = []
    for interface, addresses in psutil.net_if_addrs().items():
        name = interface.lower()
        if name.startswith(_VIRTUAL_INTERFACE_PREFIXES):
            continue
        for addr in addresses:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address.split("%", 1)[0]
            try:
                parsed = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if (
                parsed.is_loopback or parsed.is_link_local or parsed.is_multicast
                or parsed.is_reserved or parsed.is_unspecified or not parsed.is_private
            ):
                continue
            preferred = 0 if name.startswith(_PREFERRED_INTERFACE_PREFIXES) else 1
            candidates.append((preferred, interface, ip))

    origins = []
    seen = set()
    for _, _, ip in sorted(candidates, key=lambda item: (item[0], item[1])):
        for origin in (
            f"http://{ip}:{http_port()}",
            f"https://{ip}:{https_port()}",
        ):
            if origin not in seen:
                seen.add(origin)
                origins.append(origin)

    with _lan_origins_lock:
        _lan_origins_cache["at"] = now
        _lan_origins_cache["key"] = cache_key
        _lan_origins_cache["value"] = list(origins)
    return origins


def configured_cors_origins() -> list[str]:
    """Parse explicitly allowlisted CORS origins from the environment."""
    raw = os.getenv("ASSISTANT_KID_CORS_ORIGINS", "")
    origins = []
    seen = set()
    for value in raw.split(","):
        value = value.strip()
        if not value or value == "*":
            continue
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        if origin in seen:
            continue
        seen.add(origin)
        origins.append(origin)
    return origins
