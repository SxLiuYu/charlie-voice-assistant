"""认证与输入清洗: API令牌校验 + 客户端IP + XSS防护

AUTH_TOKEN 为空时放行所有请求(本地开发); 非空时保护公网访问.
"""
import hmac
import os, re
from fastapi import Request

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
TRUST_PROXY_HEADERS = os.getenv("ASSISTANT_KID_TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")
LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1", ""))

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_JAVASCRIPT_SCHEME_RE = re.compile(r'javascript:', re.IGNORECASE)
_EVENT_HANDLER_RE = re.compile(r'on\w+\s*=', re.IGNORECASE)
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

def _peer_ip(request: Request) -> str:
    """直连对端 IP；这是唯一无需信任代理配置即可确认的地址。"""
    return request.client.host if request.client else "unknown"

def _client_ip(request: Request) -> str:
    """获取客户端 IP；只有显式信任代理时才读取转发头。"""
    if TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    return _peer_ip(request)

def _has_proxy_headers(request: Request) -> bool:
    return bool(request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"))

def _is_local_request(request):
    # 公网请求可任意伪造 X-Forwarded-For/X-Real-IP。本地免 token 只看真实 TCP 对端，
    # 且一旦请求带代理头，即使对端是本机也要求令牌，避免代理头混淆边界。
    if _has_proxy_headers(request):
        return False
    return _peer_ip(request) in LOCAL_HOSTS

def _check_auth(request):
    if not AUTH_TOKEN:
        return True
    if _is_local_request(request):
        return True
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return hmac.compare_digest(token, AUTH_TOKEN)

def _sanitize_text(text: str, max_len: int = 500) -> str:
    """清洗用户输入: 去除HTML标签、脚本、控制字符"""
    if not text:
        return ""
    # 截断超长输入
    text = text[:max_len]
    # 去除HTML标签
    text = _HTML_TAG_RE.sub('', text)
    # 去除脚本相关内容
    text = _JAVASCRIPT_SCHEME_RE.sub('', text)
    text = _EVENT_HANDLER_RE.sub('', text)
    # 去除控制字符(保留换行和制表符)
    text = _CONTROL_CHAR_RE.sub('', text)
    # 去除首尾空白
    return text.strip()
