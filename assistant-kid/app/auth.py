"""认证与输入清洗: API令牌校验 + 客户端IP + XSS防护

AUTH_TOKEN 为空时放行所有请求(本地开发); 非空时保护公网访问.
"""
import os, re
from fastapi import Request

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")

def _client_ip(request: Request) -> str:
    """获取客户端IP(支持代理转发)"""
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
           request.headers.get("x-real-ip", "") or \
           (request.client.host if request.client else "unknown")

def _is_local_request(request):
    ip = _client_ip(request)
    return ip in ("127.0.0.1", "localhost", "::1", "")

def _check_auth(request):
    if not AUTH_TOKEN:
        return True
    if _is_local_request(request):
        return True
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return token == AUTH_TOKEN

def _sanitize_text(text: str, max_len: int = 500) -> str:
    """清洗用户输入: 去除HTML标签、脚本、控制字符"""
    if not text:
        return ""
    # 截断超长输入
    text = text[:max_len]
    # 去除HTML标签
    text = re.sub(r'<[^>]+>', '', text)
    # 去除脚本相关内容
    text = re.sub(r'javascript:', '', text, flags=re.IGNORECASE)
    text = re.sub(r'on\w+\s*=', '', text, flags=re.IGNORECASE)
    # 去除控制字符(保留换行和制表符)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # 去除首尾空白
    return text.strip()
