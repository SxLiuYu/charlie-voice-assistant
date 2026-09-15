"""飞书 tenant_access_token 统一管理 — 全局唯一缓存，避免多模块竞争获取 token。

飞书 API 允许最多 10 个并发有效 token，第 11 个请求会使最老的失效。
本模块确保所有 Feishu 调用共享同一个 token，避免 400/401 错误。
"""
import os
import threading
import time
import logging

log = logging.getLogger("magic")

_lock = threading.Lock()
_cache: dict[str, float | str] = {"token": "", "at": 0.0}

# token 有效期 7200 秒（2 小时），提前 200 秒刷新避免边界过期
_REFRESH_AFTER = 7000.0


def get_tenant_access_token() -> str:
    """获取飞书 tenant_access_token（带全局缓存，线程安全）。

    所有飞书 API 调用应使用此函数，不要各自独立获取 token。
    """
    with _lock:
        now = time.time()
        cached = _cache["token"]
        if cached and (now - _cache["at"] < _REFRESH_AFTER):
            return cached  # type: ignore[return-value]
        try:
            import requests as _req

            app_id = os.getenv("FEISHU_APP_ID", "")
            app_secret = os.getenv("FEISHU_APP_SECRET", "")
            if not app_id or not app_secret:
                log.warning("[feishu-token] FEISHU_APP_ID 或 FEISHU_APP_SECRET 未配置")
                return ""
            r = _req.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            token = r.json().get("tenant_access_token", "")
            if token:
                _cache["token"] = token
                _cache["at"] = now
                log.debug("[feishu-token] token 刷新成功")
                return token
            log.error("[feishu-token] 响应无 token: %s", r.text[:200])
        except Exception as e:
            log.error("[feishu-token] 获取 token 失败: %s", e)
        return ""


def invalidate() -> None:
    """强制失效缓存中的 token，下次 get 时重新获取。

    在收到 400/401 错误时调用，确保下次请求使用新 token。
    """
    with _lock:
        _cache["token"] = ""
        _cache["at"] = 0.0
        log.debug("[feishu-token] token 已失效，下次请求将重新获取")
