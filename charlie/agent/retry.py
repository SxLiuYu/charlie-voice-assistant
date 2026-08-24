"""带重试的请求封装"""
import time
import random
import logging
from typing import Any
import requests

log = logging.getLogger("magic")

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 3, 10]
RETRY_AFTER_CAP = float(__import__("os").environ.get("ASSISTANT_KID_RETRY_AFTER_CAP") or "60")

def _http_error_message(exc: requests.exceptions.HTTPError, name: str) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        return f"{name}HTTP异常: {_exception_message(exc)}"
    text = ""
    try:
        text = (response.text or "").strip().replace("\n", " ")[:200]
    except Exception:
        text = ""
    return f"{name}HTTP异常: {status}" + (f" - {text}" if text else "")

def _exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__

def _is_retryable_http_status(status: int) -> bool:
    return status == 408 or status == 429 or status >= 500

def _retry_after_delay(response, attempt: int) -> float:
    raw = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        seconds = float(raw)
        if seconds >= 0:
            return min(seconds, RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        pass
    return RETRY_BACKOFF[attempt]

def _retry(fn, name: str = "请求") -> Any:
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            response = getattr(e, "response", None)
            status = getattr(response, "status_code", None)
            message = _http_error_message(e, name)
            if status is not None and not _is_retryable_http_status(status):
                log.warning(f"{name}第{attempt+1}次失败: {message}，不重试")
                raise Exception(message) from e
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                delay = _retry_after_delay(response, attempt)
                delay += random.uniform(0, delay * 0.3)
                log.warning(f"{name}第{attempt+1}次失败: {message}，{delay:.0f}秒后重试...")
                time.sleep(delay)
            else:
                log.warning(f"{name}第{attempt+1}次失败: {message}，放弃")
        except requests.exceptions.Timeout as e:
            last_exc = e
            message = _exception_message(e)
            log.warning(f"{name}第{attempt+1}次超时: {message}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            message = _exception_message(e)
            log.warning(f"{name}第{attempt+1}次连接失败: {message}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        except Exception as e:
            # TTSUnavailableError is a terminal signal (cooldown/rate-limit);
            # pass it through unchanged so callers can handle it directly.
            try:
                from agent.asr_tts import TTSUnavailableError
                if isinstance(e, TTSUnavailableError):
                    raise
            except ImportError:
                pass
            last_exc = e
            message = _exception_message(e)
            log.warning(f"{name}第{attempt+1}次异常: {message}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        if attempt < MAX_RETRIES - 1:
            continue
    if isinstance(last_exc, requests.exceptions.HTTPError):
        raise Exception(_http_error_message(last_exc, name)) from last_exc
    if isinstance(last_exc, requests.exceptions.Timeout):
        raise Exception(f"{name}超时: {_exception_message(last_exc)}") from last_exc
    if isinstance(last_exc, requests.exceptions.ConnectionError):
        raise Exception(f"{name}连接失败: {_exception_message(last_exc)}") from last_exc
    # TTSUnavailableError is terminal — re-raise as-is
    try:
        from agent.asr_tts import TTSUnavailableError
        if isinstance(last_exc, TTSUnavailableError):
            raise
    except ImportError:
        pass
    raise Exception(f"{name}失败: {_exception_message(last_exc)}") from last_exc