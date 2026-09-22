"""agent/realtime_tts.py 的单元测试。

全部用假 WebSocket，不碰网络 —— conftest 的"默认拒绝"兜底也会拦住真连接。
重点验证三件实测得出的协议事实（官方文档没写清）：
1. 必须用 `response.create` 带 `response.instructions` 覆盖，模型才会逐字照读
2. 取消后 `response.done.status` 是 `incomplete`（不是 `cancelled`），
   且不发 `response.cancelled` 事件
3. 音频是 pcm16 Base64，采样率 24000Hz
"""
import base64
import json

import pytest

from agent import realtime_tts as rt


# ────────────────────────── 假 WebSocket ──────────────────────────

class FakeWS:
    """按脚本依次吐出服务端事件，并记录客户端发过什么。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.on_send = None

    def settimeout(self, t):
        self.timeouts.append(t)

    def send(self, payload):
        ev = json.loads(payload)
        self.sent.append(ev)
        if self.on_send:
            self.on_send(ev)

    def recv(self):
        if not self.script:
            return ""          # 代码里 `if not raw: break`
        return json.dumps(self.script.pop(0))

    def close(self):
        self.closed = True


def _delta(pcm: bytes) -> dict:
    return {"type": "response.audio.delta",
            "delta": base64.b64encode(pcm).decode()}


def _done(status="completed") -> dict:
    return {"type": "response.done", "response": {"status": status}}


@pytest.fixture
def ws_env(monkeypatch):
    """装好 STEPFUN_KEY，并返回一个可注入假连接的安装器。"""
    monkeypatch.setenv("STEPFUN_KEY", "test-stepfun-key")
    monkeypatch.delenv("STEPAUDIO3_RT_ENABLED", raising=False)
    holder = {}

    def install(script):
        fake = FakeWS(script)
        holder["ws"] = fake
        import websocket
        monkeypatch.setattr(websocket, "create_connection",
                            lambda *a, **k: fake)
        return fake

    return install, holder


def _sent_types(fake):
    return [e.get("type") for e in fake.sent]


# ────────────────────────── 协议要点 ──────────────────────────

class TestVerbatimProtocol:
    def test_sends_response_create_with_verbatim_instruction(self, ws_env):
        """必须带 response.instructions 覆盖，否则模型会改写文本。"""
        install, _ = ws_env
        fake = install([_done()])

        rt.synthesize_streaming("今天北京晴", lambda b: None)

        creates = [e for e in fake.sent if e.get("type") == "response.create"]
        assert len(creates) == 1, "应当且只发一次 response.create"
        instr = creates[0]["response"]["instructions"]
        assert "逐字朗读" in instr
        assert "不要改写" in instr
        assert "今天北京晴" in instr, "原文必须出现在指令里"

    def test_session_update_disables_server_vad_and_uses_pcm16(self, ws_env):
        """turn_detection 必须关掉 —— 打断判定归 Charlie 自己的 VAD 管。"""
        install, _ = ws_env
        fake = install([_done()])

        rt.synthesize_streaming("你好", lambda b: None)

        upd = [e for e in fake.sent if e.get("type") == "session.update"]
        assert len(upd) == 1
        s = upd[0]["session"]
        assert s["turn_detection"] is None
        assert s["input_audio_format"] == "pcm16"
        assert s["output_audio_format"] == "pcm16"
        assert s["modalities"] == ["text", "audio"]

    def test_user_item_carries_the_text(self, ws_env):
        install, _ = ws_env
        fake = install([_done()])

        rt.synthesize_streaming("念我", lambda b: None)

        items = [e for e in fake.sent if e.get("type") == "conversation.item.create"]
        assert len(items) == 1
        assert items[0]["item"]["content"][0]["text"] == "念我"
        assert items[0]["item"]["role"] == "user"

    def test_send_order_is_session_then_item_then_response(self, ws_env):
        install, _ = ws_env
        fake = install([_done()])

        rt.synthesize_streaming("你好", lambda b: None)

        assert _sent_types(fake) == [
            "session.update", "conversation.item.create", "response.create",
        ]


# ────────────────────────── 音频回调 ──────────────────────────

class TestAudioDelivery:
    def test_pcm_chunks_delivered_in_order(self, ws_env):
        install, _ = ws_env
        install([_delta(b"\x01\x02"), _delta(b"\x03\x04"), _done()])

        chunks = []
        stats = rt.synthesize_streaming("你好", chunks.append)

        assert chunks == [b"\x01\x02", b"\x03\x04"]
        assert stats["ok"] is True
        assert stats["bytes"] == 4
        assert stats["status"] == "completed"

    def test_empty_delta_is_skipped(self, ws_env):
        install, _ = ws_env
        install([_delta(b""), _delta(b"\x09\x09"), _done()])

        chunks = []
        rt.synthesize_streaming("你好", chunks.append)

        assert chunks == [b"\x09\x09"]

    def test_first_audio_ms_is_recorded(self, ws_env):
        install, _ = ws_env
        install([_delta(b"\x01\x02"), _done()])

        stats = rt.synthesize_streaming("你好", lambda b: None)

        assert stats["first_audio_ms"] is not None
        assert stats["first_audio_ms"] >= 0

    def test_no_audio_means_not_ok(self, ws_env):
        """一句音频都没有 → ok 为 False，调用方好降级到下一个 provider。"""
        install, _ = ws_env
        install([_done()])

        stats = rt.synthesize_streaming("你好", lambda b: None)

        assert stats["ok"] is False
        assert stats["bytes"] == 0

    def test_on_pcm_exception_stops_without_raising(self, ws_env):
        """下游（转码/下行）出错要停下来并记 error，不要把异常抛给调用方。"""
        install, _ = ws_env
        install([_delta(b"\x01\x02"), _delta(b"\x03\x04"), _done()])

        def boom(_b):
            raise ValueError("下游炸了")

        stats = rt.synthesize_streaming("你好", boom)

        assert stats["ok"] is False
        assert "下游炸了" in stats["error"]


# ────────────────────────── 取消语义 ──────────────────────────

class TestCancel:
    def test_cancel_sends_response_cancel(self, ws_env):
        install, holder = ws_env
        fake = install([_delta(b"\x01\x02"), _delta(b"\x03\x04"), _done("incomplete")])

        import threading
        evt = threading.Event()
        chunks = []

        def on_pcm(b):
            chunks.append(b)
            evt.set()          # 收到第一段就"用户开口"打断

        stats = rt.synthesize_streaming("你好", on_pcm, evt)

        assert "response.cancel" in _sent_types(fake)
        assert stats["cancelled"] is True
        # 取消后不再回调下游 —— 这正是"掐断"的关键
        assert chunks == [b"\x01\x02"]
        assert stats["bytes"] == 2

    def test_incomplete_status_is_reported_as_is(self, ws_env):
        """实测：取消时 status 是 incomplete，不是 cancelled。代码不得依赖后者。"""
        install, _ = ws_env
        install([_delta(b"\x01\x02"), _done("incomplete")])

        import threading
        evt = threading.Event()

        def on_pcm(_b):
            evt.set()

        stats = rt.synthesize_streaming("你好", on_pcm, evt)

        assert stats["status"] == "incomplete"

    def test_no_cancel_event_sent_when_not_interrupted(self, ws_env):
        install, _ = ws_env
        fake = install([_delta(b"\x01\x02"), _done()])

        rt.synthesize_streaming("你好", lambda b: None)

        assert "response.cancel" not in _sent_types(fake)

    def test_already_set_cancel_event_sends_cancel_once(self, ws_env):
        install, _ = ws_env
        fake = install([_delta(b"\x01\x02"), _done("incomplete")])

        import threading
        evt = threading.Event()
        evt.set()              # 进函数前就已经被 set

        stats = rt.synthesize_streaming("你好", lambda b: None, evt)

        assert _sent_types(fake).count("response.cancel") == 1
        assert stats["bytes"] == 0, "已取消时不应再回调音频"


# ────────────────────────── 异常与配置 ──────────────────────────

class TestRobustness:
    def test_missing_key_returns_error_without_connecting(self, monkeypatch):
        monkeypatch.delenv("STEPFUN_KEY", raising=False)
        called = []
        import websocket
        monkeypatch.setattr(websocket, "create_connection",
                            lambda *a, **k: called.append(1) or FakeWS([]))

        stats = rt.synthesize_streaming("你好", lambda b: None)

        assert stats["ok"] is False
        assert "STEPFUN_KEY" in stats["error"]
        assert called == [], "没 key 就不该建连"

    def test_server_error_event_is_reported(self, ws_env):
        install, _ = ws_env
        install([{"type": "error", "code": "bad_voice", "message": "voice 无效"}])

        stats = rt.synthesize_streaming("你好", lambda b: None)

        assert stats["ok"] is False
        assert "bad_voice" in stats["error"]

    def test_connect_failure_does_not_raise(self, ws_env, monkeypatch):
        install, _ = ws_env
        install([])
        import websocket

        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(websocket, "create_connection", boom)

        stats = rt.synthesize_streaming("你好", lambda b: None)

        assert stats["ok"] is False
        assert "connection refused" in stats["error"]

    def test_ws_is_closed_on_success(self, ws_env):
        install, holder = ws_env
        install([_delta(b"\x01\x02"), _done()])

        rt.synthesize_streaming("你好", lambda b: None)

        assert holder["ws"].closed is True

    def test_ws_is_closed_on_failure(self, ws_env, monkeypatch):
        install, holder = ws_env
        fake = install([_delta(b"\x01\x02")])
        import websocket

        def boom_recv():
            raise OSError("read timeout")

        monkeypatch.setattr(fake, "recv", boom_recv)
        monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: fake)

        rt.synthesize_streaming("你好", lambda b: None)

        assert fake.closed is True


class TestConfig:
    def test_sample_rate_defaults_to_24000(self, monkeypatch):
        monkeypatch.delenv("STEPAUDIO3_SAMPLE_RATE", raising=False)
        assert rt.sample_rate() == 24000

    def test_sample_rate_env_override(self, monkeypatch):
        monkeypatch.setenv("STEPAUDIO3_SAMPLE_RATE", "16000")
        assert rt.sample_rate() == 16000

    def test_sample_rate_bad_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("STEPAUDIO3_SAMPLE_RATE", "not-a-number")
        assert rt.sample_rate() == 24000

    def test_pcm_duration_matches_24k(self):
        # 24kHz 单声道 16bit → 每秒 48000 字节
        assert rt.pcm_duration_seconds(48000) == pytest.approx(1.0)
        assert rt.pcm_duration_seconds(0) == 0.0

    def test_is_configured_requires_flag_and_key(self, monkeypatch):
        monkeypatch.setenv("STEPFUN_KEY", "k")
        monkeypatch.delenv("STEPAUDIO3_RT_ENABLED", raising=False)
        assert rt.is_configured() is False, "默认关闭：计费模型，保持 opt-in 控制成本"

        monkeypatch.setenv("STEPAUDIO3_RT_ENABLED", "1")
        assert rt.is_configured() is True

        monkeypatch.delenv("STEPFUN_KEY", raising=False)
        assert rt.is_configured() is False

    def test_status_shape(self, monkeypatch):
        monkeypatch.setenv("STEPFUN_KEY", "k")
        st = rt.status()
        assert st["sample_rate"] == 24000
        assert "stepaudio-2.5-realtime" in st["model"]
        assert st["endpoint"].startswith("wss://")

    def test_defaults_point_at_step_plan_2_5(self, monkeypatch):
        """钉住 2026-09-19 的切换：默认端点/模型必须落在 step_plan 路径的 2.5。

        上一代默认是 v1 路径的 stepaudio-3-realtime-preview（限免预览版）。
        换回旧值意味着（a）丢了订阅计费通道的一致性，（b）拿随时会到期的
        预览模型当生产默认。这两个常量走的是模块默认值，env 没设时才生效，
        改了不报错、只在真机对话时表现为 404/旧音色——必须有测试钉住。
        """
        monkeypatch.delenv("STEPAUDIO3_RT_URL", raising=False)
        monkeypatch.delenv("STEPAUDIO3_RT_MODEL", raising=False)
        assert rt.DEFAULT_MODEL == "stepaudio-2.5-realtime"
        assert rt.DEFAULT_URL == "wss://api.stepfun.com/step_plan/v1/realtime"
        # _url() 把 model 拼成 query param
        assert rt._url() == "wss://api.stepfun.com/step_plan/v1/realtime?model=stepaudio-2.5-realtime"

    def test_url_env_override(self, monkeypatch):
        """env 覆盖仍然优先于默认（回退通道必须可用）。"""
        monkeypatch.setenv("STEPAUDIO3_RT_URL", "wss://example.test/v1/realtime")
        monkeypatch.setenv("STEPAUDIO3_RT_MODEL", "other-model")
        assert rt._url() == "wss://example.test/v1/realtime?model=other-model"
