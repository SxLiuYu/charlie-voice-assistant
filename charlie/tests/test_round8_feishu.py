"""Round 8 feishu channel tests (F1-F5). All network/SDK calls are mocked."""
import os
import sys
import time
import types
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# Helpers
# ======================================================================

def _install_fake_lark_oapi():
    """Install a minimal fake lark_oapi package into sys.modules so feishu_ws
    can be imported without the real SDK."""
    fake = types.ModuleType("lark_oapi")
    fake_ws = types.ModuleType("lark_oapi.ws")
    fake_ws_client = types.ModuleType("lark_oapi.ws.client")
    fake_api = types.ModuleType("lark_oapi.api")
    fake_im = types.ModuleType("lark_oapi.api.im")
    fake_im_v1 = types.ModuleType("lark_oapi.api.im.v1")

    class FakeClient:
        def __init__(self, *a, **kw):
            pass
        def start(self):
            pass

    class FakeEventHandler:
        @classmethod
        def builder(cls, *a, **kw):
            return cls()
        def register_p2_im_message_receive_v1(self, handler):
            return self
        def build(self):
            return self

    class FakeLogLevel:
        INFO = "INFO"

    fake_ws_client.Client = FakeClient
    fake_ws.client = fake_ws_client
    fake_ws.client.Client = FakeClient
    fake.EventDispatcherHandler = FakeEventHandler
    fake.LogLevel = FakeLogLevel
    fake_api.im = fake_im
    fake_im.v1 = fake_im_v1
    fake_im_v1.P2ImMessageReceiveV1 = type("P2ImMessageReceiveV1", (), {})

    sys.modules["lark_oapi"] = fake
    sys.modules["lark_oapi.ws"] = fake_ws
    sys.modules["lark_oapi.ws.client"] = fake_ws_client
    sys.modules["lark_oapi.api"] = fake_api
    sys.modules["lark_oapi.api.im"] = fake_im
    sys.modules["lark_oapi.api.im.v1"] = fake_im_v1


@pytest.fixture(autouse=True)
def _fresh_feishu_state(monkeypatch):
    """Reset feishu_ws module-level state and token cache between tests."""
    monkeypatch.setenv("FEISHU_APP_ID", "test_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test_secret")
    _install_fake_lark_oapi()
    # Force re-import to pick up fresh module state
    for mod_name in list(sys.modules):
        if mod_name == "feishu_ws" or mod_name.startswith("feishu_ws."):
            del sys.modules[mod_name]
    import feishu_token
    with feishu_token._lock:
        feishu_token._cache["token"] = ""
        feishu_token._cache["at"] = 0.0
    import feishu_ws
    feishu_ws._started = False
    yield
    feishu_ws._started = False


# ======================================================================
# F1: feishu_token cache expiry > 7000s forces re-fetch
# ======================================================================

class TestFeishuTokenCache:
    """F1: token cache expires after _REFRESH_AFTER (7000s) and re-fetches."""

    def test_token_refreshes_after_expiry(self, monkeypatch):
        import feishu_token
        import requests as _req

        call_count = [0]

        def fake_post(url, *args, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            m.json.return_value = {"tenant_access_token": f"tok-{call_count[0]}"}
            return m

        monkeypatch.setattr(_req, "post", fake_post)

        # First call: cache miss → fetch
        tok1 = feishu_token.get_tenant_access_token()
        assert tok1 == "tok-1"
        assert call_count[0] == 1

        # Second call within TTL: cached
        tok2 = feishu_token.get_tenant_access_token()
        assert tok2 == "tok-1"
        assert call_count[0] == 1

        # Expire the cache by moving _at back >7000s
        with feishu_token._lock:
            feishu_token._cache["at"] = time.time() - 7001.0

        tok3 = feishu_token.get_tenant_access_token()
        assert tok3 == "tok-2"
        assert call_count[0] == 2


# ======================================================================
# F2: _send_text_reply 400 → invalidate → retry success
# ======================================================================

class TestSendTextReply:
    """F2: _send_text_reply retry on 400 with invalidate."""

    def test_400_then_invalidate_then_200(self, monkeypatch):
        import feishu_ws
        import requests as _req

        msg_send_count = [0]

        def fake_post(url, *args, **kwargs):
            m = MagicMock()
            if "tenant_access_token/internal" in str(url):
                # Token endpoint always returns a fresh token
                m.json.return_value = {"tenant_access_token": "tok-fresh"}
                return m
            # Message send endpoint
            msg_send_count[0] += 1
            if msg_send_count[0] == 1:
                m.status_code = 400
                m.text = '{"code":400}'
            else:
                m.status_code = 200
                m.json.return_value = {"code": 0, "data": {"message_id": "om_test_123"}}
            return m

        invalidate_count = [0]

        def fake_invalidate():
            invalidate_count[0] += 1
            import feishu_token
            with feishu_token._lock:
                feishu_token._cache["token"] = ""
                feishu_token._cache["at"] = 0.0

        # Pre-seed a token so first attempt doesn't bail early
        import feishu_token
        with feishu_token._lock:
            feishu_token._cache["token"] = "stale-tok"
            feishu_token._cache["at"] = time.time()

        monkeypatch.setattr(_req, "post", fake_post)
        monkeypatch.setattr(feishu_ws, "_invalidate_token", fake_invalidate)

        result = feishu_ws._send_text_reply("chat123", "hello")
        assert result is True
        assert msg_send_count[0] == 2
        assert invalidate_count[0] == 1

    def test_two_400s_returns_false(self, monkeypatch):
        import feishu_ws
        import requests as _req

        msg_send_count = [0]

        def fake_post(url, *args, **kwargs):
            m = MagicMock()
            if "tenant_access_token/internal" in str(url):
                m.json.return_value = {"tenant_access_token": "tok-fresh"}
                return m
            # Message send endpoint: always 400
            msg_send_count[0] += 1
            m.status_code = 400
            m.text = '{"code":400}'
            return m

        invalidate_count = [0]

        def fake_invalidate():
            invalidate_count[0] += 1
            import feishu_token
            with feishu_token._lock:
                feishu_token._cache["token"] = ""
                feishu_token._cache["at"] = 0.0

        import feishu_token
        with feishu_token._lock:
            feishu_token._cache["token"] = "stale-tok"
            feishu_token._cache["at"] = time.time()

        monkeypatch.setattr(_req, "post", fake_post)
        monkeypatch.setattr(feishu_ws, "_invalidate_token", fake_invalidate)

        result = feishu_ws._send_text_reply("chat123", "hello")
        assert result is False
        assert msg_send_count[0] == 2
        assert invalidate_count[0] == 1


# ======================================================================
# F3: _is_duplicate_message LRU + TTL
# ======================================================================

class TestDuplicateMessage:
    """F3: dedup cache: TTL=300s, max=200, LRU eviction."""

    def test_same_id_short_term_duplicate(self, monkeypatch):
        import feishu_ws
        # Reset the cache
        feishu_ws._processed_messages.clear()
        assert feishu_ws._is_duplicate_message("msg-1") is False
        assert feishu_ws._is_duplicate_message("msg-1") is True

    def test_different_id_not_duplicate(self, monkeypatch):
        import feishu_ws
        feishu_ws._processed_messages.clear()
        assert feishu_ws._is_duplicate_message("msg-a") is False
        assert feishu_ws._is_duplicate_message("msg-b") is False

    def test_expired_after_ttl(self, monkeypatch):
        import feishu_ws
        feishu_ws._processed_messages.clear()
        feishu_ws._is_duplicate_message("msg-old")
        # Move time forward >300s using a fixed offset captured at call time
        base = time.time()
        with monkeypatch.context() as m:
            m.setattr(time, "time", lambda: base + 301)
            assert feishu_ws._is_duplicate_message("msg-old") is False

    def test_lru_eviction_over_max(self, monkeypatch):
        import feishu_ws
        feishu_ws._processed_messages.clear()
        for i in range(210):
            feishu_ws._is_duplicate_message(f"msg-{i}")
        # The first 10 should have been evicted
        assert feishu_ws._is_duplicate_message("msg-0") is False
        assert len(feishu_ws._processed_messages) <= 200


# ======================================================================
# F4: _on_message_receive catches all exceptions, no re-raise
# ======================================================================

class TestOnMessageReceive:
    """F4: _on_message_receive swallows exceptions (no re-raise)."""

    def test_exception_in_handler_is_swallowed(self, monkeypatch):
        import feishu_ws

        class BadData:
            @property
            def event(self):
                raise RuntimeError("boom")

        # Should not raise
        feishu_ws._on_message_receive(BadData())

    def test_missing_event_attribute_is_swallowed(self, monkeypatch):
        import feishu_ws

        class NoEvent:
            pass

        feishu_ws._on_message_receive(NoEvent())


# ======================================================================
# F5: start() reentrancy guard + skip when FEISHU_APP_ID not configured
# ======================================================================

class TestFeishuStart:
    """F5: start() guards against re-entry and skips when APP_ID missing."""

    def test_start_skips_when_no_app_id(self, monkeypatch):
        import feishu_ws
        feishu_ws._started = False
        monkeypatch.setenv("FEISHU_APP_ID", "")
        monkeypatch.setenv("FEISHU_APP_SECRET", "")
        # Reload to pick up new env
        del sys.modules["feishu_ws"]
        import feishu_ws as fw
        fw.start()
        assert fw._started is False
        assert fw._ws_thread is None

    def test_start_skips_when_no_app_secret(self, monkeypatch):
        import feishu_ws
        feishu_ws._started = False
        monkeypatch.setenv("FEISHU_APP_ID", "test_app")
        monkeypatch.setenv("FEISHU_APP_SECRET", "")
        del sys.modules["feishu_ws"]
        import feishu_ws as fw
        fw.start()
        assert fw._started is False
        assert fw._ws_thread is None

    def test_start_is_idempotent(self, monkeypatch):
        import feishu_ws
        feishu_ws._started = False
        monkeypatch.setenv("FEISHU_APP_ID", "test_app")
        monkeypatch.setenv("FEISHU_APP_SECRET", "test_secret")

        class FakeThread:
            def __init__(self, *a, **kw):
                pass
            def start(self):
                pass

        monkeypatch.setattr(feishu_ws.threading, "Thread", FakeThread)

        feishu_ws.start()
        assert feishu_ws._started is True

        # Second call should be a no-op
        feishu_ws.start()
        assert feishu_ws._started is True
