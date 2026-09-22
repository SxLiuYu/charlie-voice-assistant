"""StepFun 多 Key 本地轮询路由（一个路由器，全链路共用）.

2026-09-19 决策：StepFun 是 TTS 云端单点（TTS_PRIORITY=stepfun），实测单 key
有持续配额限制（15s 间隔照样 429）。用户持有 10 把 key，STEPFUN_KEY 从此支持
逗号分隔多把，全部 StepFun 调用（TTS 批量/流式、ASR、LLM、realtime WS）共用
这一个本地路由器：

    STEPFUN_KEY=sk-aaa,sk-bbb,sk-ccc   # 多把轮询
    STEPFUN_KEY=sk-aaa                 # 单把（老写法，行为不变）

路由策略：
- **轮询**：每次取 key 前进一格，把请求摊到所有 key 上，单 key 配额 × N；
- **429 冷却**：某把 key 撞 429 后冷却 `STEPFUN_KEY_COOLDOWN` 秒（默认 60），
  轮询跳过它，配额窗口过了自动回归；
- **401/403 隔离**：鉴权失败按 `STEPFUN_KEY_AUTH_COOLDOWN` 秒（默认 1800）
  隔离，不反复撞墙（key 欠费/被禁时快速滑过它）；
- **全冷却兜底**：所有 key 都在冷却时，直接返回轮询指针当前那把——不阻塞、
  不吞错，让上游 429/401 照常抛出，per-provider 熔断（TTS_FAILURE_THRESHOLD）
  和 ASR/LLM 降级链照常工作。

线程安全：TTS 流式跑在 producer 线程、ASR race 在线程池、realtime 在 asyncio
executor，全部走同一把锁。
"""
import os
import threading
import time

_lock = threading.Lock()
_idx = 0                    # 轮询指针（对 keys 全长的模，冷却 key 也会经过）
_cooldown: dict[str, float] = {}   # key → 冷却截止时间戳
_last_key = ""              # 最近一次 next_key() 发出的 key（近似打标用）


def _cooldown_seconds() -> float:
    try:
        return float(os.getenv("STEPFUN_KEY_COOLDOWN", "60"))
    except ValueError:
        return 60.0


def _auth_cooldown_seconds() -> float:
    try:
        return float(os.getenv("STEPFUN_KEY_AUTH_COOLDOWN", "1800"))
    except ValueError:
        return 1800.0


def stepfun_keys() -> list[str]:
    """解析 STEPFUN_KEY（逗号分隔多把），去空、去重、保序。"""
    raw = os.getenv("STEPFUN_KEY", "")
    keys, seen = [], set()
    for part in raw.replace(";", ",").split(","):
        k = part.strip()
        if k and k not in seen:
            keys.append(k)
            seen.add(k)
    return keys


def has_keys() -> bool:
    return bool(stepfun_keys())


def next_key() -> str:
    """轮询取下一把可用 key（跳过冷却中的；全冷却时回退当前指针那把）。"""
    global _idx, _last_key
    keys = stepfun_keys()
    if not keys:
        return ""
    now = time.monotonic()
    with _lock:
        _idx %= len(keys)
        # 从指针开始扫一圈，找第一把不在冷却的
        for off in range(len(keys)):
            k = keys[(_idx + off) % len(keys)]
            if _cooldown.get(k, 0.0) <= now:
                _idx = (_idx + off + 1) % len(keys)
                _last_key = k
                return k
        # 全在冷却：不阻塞，用指针当前那把硬上（错误让上游照常报）
        k = keys[_idx % len(keys)]
        _idx = (_idx + 1) % len(keys)
        _last_key = k
        return k


def mark_rate_limited(key: str) -> None:
    """429：该 key 进入短冷却（配额窗口），轮询自动滑到下一把。"""
    if not key:
        return
    with _lock:
        _cooldown[key] = time.monotonic() + _cooldown_seconds()


def mark_auth_failed(key: str) -> None:
    """401/403：该 key 长时间隔离（欠费/被禁），避免反复撞墙。"""
    if not key:
        return
    with _lock:
        _cooldown[key] = time.monotonic() + _auth_cooldown_seconds()


def keys_status() -> list[dict]:
    """运行时状态（调试/管理页展示用）：不回传 key 明文，只给尾 4 位。"""
    keys = stepfun_keys()
    now = time.monotonic()
    with _lock:
        return [
            {
                "key_tail": k[-4:],
                "cooling": _cooldown.get(k, 0.0) > now,
                "cooldown_left_s": max(0.0, round(_cooldown.get(k, 0.0) - now, 1)),
            }
            for k in keys
        ]


def _reset_for_tests() -> None:
    global _idx, _last_key
    with _lock:
        _idx = 0
        _last_key = ""
        _cooldown.clear()


def mark_from_status(key: str, status_code: int) -> None:
    """按 HTTP 状态码打标（各调用点共用的判定，避免每处各写一份 if）。"""
    if status_code == 429:
        mark_rate_limited(key)
    elif status_code in (401, 403):
        mark_auth_failed(key)


def mark_last_rate_limited() -> None:
    """429 的近似打标：标记最近一次 next_key() 发出的那把。

    给拿不到"本次到底用了哪把 key"的调用点用（LLM 大脑客户端在建脑时
    就固化了 key）。低 QPS 下近似足够；偶尔标错也只是让一把健康 key
    冷却 60s，无害。
    """
    with _lock:
        key = _last_key
    if key:
        mark_rate_limited(key)
