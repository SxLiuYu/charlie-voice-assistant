"""
Charlie - API服务端单元测试
使用FastAPI TestClient + mock测试所有API端点
"""
import json, base64, os, sys, subprocess, datetime, logging, warnings, asyncio, threading, time
import io
import wave
from collections import deque
from unittest.mock import patch, MagicMock, AsyncMock
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import reminders as app_reminders
from app.state import Metrics, _interrupt_telemetry, _poll_telemetry, _rate_buckets, _session_buckets, register_sse_client, unregister_sse_client, snapshot_sse_clients, sse_client_count
import voice_server
import app.schedulers as _sched
import app.notifications as _notif
import app.cors as _cors
import app.http_helpers as _http
import app.reminders as _rem
import app.routes.reminders as _rem_route
import app.routes.conversation as _conv_route
import app.routes.websocket as _ws_route
import agent.history as _history
import agent.asr_tts as _asr_tts
import agent.preferences as _prefs_mod
import app.routes.manage as _manage_route
import agent.llm_state as _llm_state


def _silence_wav(seconds=5, sample_rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * seconds * sample_rate)
    return buf.getvalue()


def _tone_wav(seconds=5, amplitude=12000, sample_rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(
            amplitude.to_bytes(2, "little", signed=True) * (seconds * sample_rate)
        )
    return buf.getvalue()


def _parse_sse_events(response):
    events = []
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip():
            events.append(json.loads(payload))
    return events


@pytest.fixture(scope="module")
def client():
    """创建测试客户端(不触发lifespan, 避免后台线程冲突)"""
    import os
    os.environ["SKIP_BACKGROUND"] = "1"
    os.environ.setdefault("AGNES_KEY", "test")
    os.environ.setdefault("AGNES_KEY", "test")
    os.environ.setdefault("AGNES_BASE", "https://apihub.agnes-ai.com/v1")
    os.environ.setdefault("AGNES_MODEL", "agnes-2.5-flash")
    os.environ.setdefault("TTS_KEY", "test")
    os.environ.setdefault("ASR_KEY", "test")
    os.environ.setdefault("AMAP_KEY", "test")
    import voice_server
    # 不使用'with'语句 → 不触发lifespan → 不启动后台线程 → 不hang
    c = TestClient(voice_server.app)
    yield c


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """每个用例独立统计限流，避免 module 级 TestClient 累计触发 429。"""
    _rate_buckets.clear()
    _session_buckets.clear()
    yield
    _rate_buckets.clear()
    _session_buckets.clear()


class TestHealthAndStatus:
    """健康检查和系统状态"""

    @pytest.fixture(autouse=True)
    def reset_lan_access_cache(self):
        from app.routes import system as system_routes

        system_routes._invalidate_lan_access_cache()
        system_routes._host_metadata_cache = None
        yield
        system_routes._invalidate_lan_access_cache()
        system_routes._host_metadata_cache = None

    @pytest.fixture(autouse=True)
    def reset_poll_telemetry(self):
        _poll_telemetry.reset()
        yield
        _poll_telemetry.reset()

    def test_home_supports_head(self, client):
        r = client.head("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.content == b""

    def test_successful_request_logs_completion_without_info_start_line(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="magic"):
            r = client.get("/health")

        assert r.status_code == 200
        messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
        assert any("GET /health → 200" in message for message in messages)
        assert not any(message.endswith("GET /health") for message in messages)

    def test_home_declares_mobile_icons(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert 'rel="icon"' in r.text
        assert 'rel="apple-touch-icon"' in r.text
        assert "/favicon.ico" in r.text
        assert "/apple-touch-icon.png" in r.text

    @pytest.mark.parametrize("path", ["/", "/test"])
    def test_html_routes_reuse_cached_file_contents(self, client, monkeypatch, path):
        monkeypatch.setattr(_http, "_text_file_cache", {}, raising=False)
        opened_paths = []
        real_open = open

        def tracking_open(file, *args, **kwargs):
            opened_paths.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(_http, "_open_text_file", tracking_open)

        first = client.get(path)
        second = client.get(path)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.text == second.text
        assert len(opened_paths) == 1
        assert opened_paths[0].endswith("voice.html" if path == "/" else "voice_test.html")

    @pytest.mark.parametrize("path", ["/", "/test"])
    def test_html_routes_support_etag_304_without_reading_file(self, client, monkeypatch, path):
        monkeypatch.setattr(_http, "_text_file_cache", {}, raising=False)

        first = client.get(path)
        etag = first.headers.get("etag")

        assert first.status_code == 200
        assert etag
        assert first.headers["cache-control"] == "no-cache"

        opened_paths = []
        real_open = open

        def tracking_open(file, *args, **kwargs):
            opened_paths.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(_http, "_open_text_file", tracking_open)
        second = client.get(path, headers={"If-None-Match": etag})

        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert opened_paths == []

    def test_cached_text_file_rereads_when_file_token_changes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_http, "_text_file_cache", {}, raising=False)
        path = tmp_path / "page.html"
        path.write_text("first version", encoding="utf-8")

        opened_paths = []
        real_open = open

        def tracking_open(file, *args, **kwargs):
            opened_paths.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(_http, "_open_text_file", tracking_open)

        assert voice_server._read_cached_text(str(path)) == "first version"
        assert voice_server._read_cached_text(str(path)) == "first version"

        path.write_text("second version!", encoding="utf-8")
        future_mtime = time.time() + 10
        os.utime(path, (future_mtime, future_mtime))

        assert voice_server._read_cached_text(str(path)) == "second version!"
        assert opened_paths == [str(path), str(path)]

    def test_home_pauses_polling_when_hidden(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "visibilitychange" in r.text
        assert "schedulePoll" in r.text
        assert "setInterval(loadRems" not in r.text.replace(" ", "")
        assert "setInterval(loadPrefs" not in r.text.replace(" ", "")
        assert "setInterval(loadTunnelURL" not in r.text.replace(" ", "")

    def test_home_uses_conditional_gets_for_polling(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "If-None-Match" in r.text
        assert "pollEtags" in r.text
        assert "fetchPollJSON('/api/reminders','reminders'" in r.text.replace(" ", "")

    def test_home_visibility_polling_resumes_with_refresh(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")
        assert "if(!job.paused){job.paused=true;reportPollingTelemetry('paused',job.name);}" in compact
        assert "document.addEventListener('visibilitychange'" in compact
        assert "reportPollingTelemetry('resumed')" in compact
        assert "pollJobs.forEach(job=>{job.run().catch(()=>{});schedulePoll(job);})" in compact

    def test_home_reports_polling_pause_resume_and_backoff(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "functionreportPollingTelemetry" in compact
        assert "reportPollingTelemetry('paused',job.name)" in compact
        assert "reportPollingTelemetry('resumed')" in compact
        assert "reportPollingTelemetry('error',job.name)" in compact

    def test_home_manual_interrupt_reports_last_reply(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "functionbuildInterruptPayload" in compact
        assert "wsConn.send(JSON.stringify(buildInterruptPayload()))" in compact

    def test_home_manual_interrupt_aborts_sse_fallback(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")
        helper_start = compact.index("functionabortActiveStream()")
        helper_end = compact.index("functioncaptureInterruptedReply", helper_start)
        helper = compact[helper_start:helper_end]
        start = compact.index("functioninterruptTTS()")
        end = compact.index("//自动打断(barge-in)", start)
        body = compact[start:end]

        assert "if(abortCtrl){try{abortCtrl.abort();}catch(e){}abortCtrl=null;}" in helper
        assert "abortActiveStream();" in body

    def test_home_automatic_barge_in_uses_stricter_threshold_than_wake_word(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "constSOUND_THRESHOLD=0.2" in compact
        assert "constBARGE_THRESHOLD=0.45" in compact
        assert "BARGE_DELAY=650" in compact
        assert "BARGE_START_GUARD_MS=900" in compact
        barge_start = compact.index("if(autoListen&&state==='speaking'&&isPlaying)")
        barge_end = compact.index("//静默停止", barge_start)
        barge_logic = compact[barge_start:barge_end]
        assert "if(v>BARGE_THRESHOLD&&Date.now()-speakingStartedAt>=BARGE_START_GUARD_MS)" in barge_logic
        assert "setTimeout(()=>{if(state==='speaking'&&isPlaying)bargeIn();},BARGE_DELAY)" in barge_logic

        manual_start = compact.index("functioninterruptTTS()")
        manual_end = compact.index("//自动打断(barge-in)", manual_start)
        manual_body = compact[manual_start:manual_end]
        assert "setTimeout" not in manual_body
        assert "abortActiveStream();" in manual_body

    def test_home_barge_in_guard_starts_when_playback_session_begins(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "abortCtrl=null,speakingStartedAt=0" in compact
        play_start = compact.index("functionplayNextAudio()")
        play_end = compact.index("functionhtmlAudioFallback", play_start)
        play_body = compact[play_start:play_end]
        assert "if(!isPlaying)speakingStartedAt=Date.now();" in play_body
        assert "isPlaying=true;" in play_body
        assert play_body.index("if(!isPlaying)speakingStartedAt=Date.now();") < play_body.index("isPlaying=true;")
        assert play_body.count("speakingStartedAt=Date.now()") == 1

        stop_audio_start = compact.index("functionstopAllAudio()")
        stop_audio_end = compact.index("functionabortActiveStream", stop_audio_start)
        stop_audio_body = compact[stop_audio_start:stop_audio_end]
        assert "speakingStartedAt=0" in stop_audio_body
        assert "clearTimeout(bargeTimer);bargeTimer=null;" in stop_audio_body

    def test_home_stale_webaudio_decode_callback_does_not_start_after_interrupt(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "currentHtmlAudio=null,audioPlaybackGeneration=0" in compact
        play_start = compact.index("functionplayNextAudio()")
        play_end = compact.index("functionhtmlAudioFallback", play_start)
        play_body = compact[play_start:play_end]
        assert "constplaybackGeneration=++audioPlaybackGeneration" in play_body
        assert "if(playbackGeneration!==audioPlaybackGeneration)return;" in play_body

        stop_audio_start = compact.index("functionstopAllAudio()")
        stop_audio_end = compact.index("functionabortActiveStream", stop_audio_start)
        stop_audio_body = compact[stop_audio_start:stop_audio_end]
        assert "audioPlaybackGeneration++" in stop_audio_body

    def test_home_stale_decode_error_does_not_fallback_after_interrupt(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        play_start = compact.index("functionplayNextAudio()")
        play_end = compact.index("functionhtmlAudioFallback", play_start)
        play_body = compact[play_start:play_end]
        assert "function(){if(playbackGeneration!==audioPlaybackGeneration)return;htmlAudioFallback(b64);}" in play_body

    def test_home_html_audio_fallback_releases_object_url(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        fallback_start = compact.index("functionhtmlAudioFallback(b64){")
        fallback_end = compact.index("//=====SSE流解析", fallback_start)
        fallback_body = compact[fallback_start:fallback_end]

        assert "constaudioUrl=URL.createObjectURL" in fallback_body
        assert "newAudio(audioUrl)" in fallback_body
        assert "functionreleaseHtmlAudioUrl(){if(released)return;released=true;try{URL.revokeObjectURL(audioUrl);}catch(e){}}" in fallback_body
        assert fallback_body.count("releaseHtmlAudioUrl();") >= 3

        stop_start = compact.index("functionstopAllAudio()")
        stop_end = compact.index("functionabortActiveStream", stop_start)
        stop_body = compact[stop_start:stop_end]
        assert "if(audio._releaseHtmlAudioUrl)audio._releaseHtmlAudioUrl();" in stop_body

    def test_home_shows_asr_ack_before_thinking(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "case'ack'" in compact
        assert "嗯，让我想想" in compact
        assert "setState('thinking','👂','嗯，让我想想…')" in compact

    def test_home_ws_reconnect_sse_init_has_explicit_guard(self, client):
        r = client.get("/")
        compact = r.text.replace(" ", "")

        assert "if(!evtSource||evtSource.readyState===EventSource.CLOSED)initSSE();" in compact

    def test_home_provides_conversation_export_controls(self, client):
        r = client.get("/")

        assert r.status_code == 200
        assert "对话导出" in r.text
        assert 'id="exportfrom"' in r.text.replace(" ", "")
        assert 'id="exportto"' in r.text.replace(" ", "")
        assert 'id="exportformat"' in r.text.replace(" ", "")
        assert "downloadConversationExport()" in r.text.replace(" ", "")
        assert "exportSessionId" in r.text
        assert "from_date" in r.text
        assert "to_date" in r.text

    def test_mobile_icons_are_png_and_cacheable(self, client):
        for path in (
            "/favicon.ico",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
        ):
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"] == "image/png", path
            assert r.headers["cache-control"] == "public, max-age=86400", path
            assert r.content.startswith(b"\x89PNG\r\n\x1a\n"), path

    def test_mobile_icons_support_head(self, client):
        for path in (
            "/favicon.ico",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
        ):
            r = client.head(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"] == "image/png", path
            assert r.headers["cache-control"] == "public, max-age=86400", path
            assert r.headers["content-length"] == "241", path
            assert r.content == b"", path

    @pytest.mark.parametrize(
        "path",
        [
            "/favicon.ico",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
        ],
    )
    def test_mobile_icons_support_etag_304(self, client, path):
        first = client.get(path)
        etag = first.headers.get("etag")

        assert first.status_code == 200
        assert etag
        assert first.headers["cache-control"] == "public, max-age=86400"

        second = client.get(path, headers={"If-None-Match": etag})

        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert second.headers["cache-control"] == "public, max-age=86400"

    def test_manifest_reuses_cached_json_and_supports_head_304(self, client, monkeypatch):
        monkeypatch.setattr(_http, "_MANIFEST_BODY", None, raising=False)

        first = client.get("/manifest.json")
        etag = first.headers.get("etag")

        assert first.status_code == 200
        assert first.headers["content-type"].startswith("application/json")
        assert first.headers["cache-control"] == "no-cache"
        assert etag
        assert first.json()["name"] == "Charlie"

        builds = 0
        original_build = voice_server._build_manifest_payload

        def tracking_build():
            nonlocal builds
            builds += 1
            return original_build()

        monkeypatch.setattr(_http, "build_manifest_payload", tracking_build)

        head = client.head("/manifest.json")
        cached = client.get("/manifest.json", headers={"If-None-Match": etag})

        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["etag"] == etag
        assert cached.status_code == 304
        assert cached.content == b""
        assert cached.headers["etag"] == etag
        assert builds == 0

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "service" in data
        assert "version" in data
        assert "uptime_seconds" in data
        assert "brain_ready" in data
        assert "websocket_clients" in data

    def test_status(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert "device" in data
        assert "cpu_percent" in data
        assert "memory_percent" in data

    def test_status_and_dashboard_use_nonblocking_cpu_sample(self, client, monkeypatch):
        import psutil
        from app.routes import system as system_routes

        calls = []

        def fake_cpu_percent(interval=None):
            calls.append(interval)
            return 12.5

        monkeypatch.setattr(psutil, "cpu_percent", fake_cpu_percent)

        status = client.get("/api/status")
        dashboard = client.get("/dashboard")

        assert status.status_code == 200
        assert dashboard.status_code == 200
        assert calls
        assert all(call is None for call in calls)

    def test_status_exposes_lan_access(self, client, monkeypatch):
        import psutil
        import socket as socket_module
        from app.routes import system as system_routes

        class FakeAddress:
            family = socket_module.AF_INET
            address = "192.168.1.4"

        monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
            "lo0": [type("Loopback", (), {"family": socket_module.AF_INET, "address": "127.0.0.1"})()],
            "en1": [FakeAddress()],
        })
        monkeypatch.setattr(socket_module, "gethostname", lambda: "kid.local")

        r = client.get("/api/status")

        assert r.status_code == 200
        network = r.json()["network"]
        assert network["hostname"] == "kid.local"
        assert network["interface"] == "en1"
        assert network["lan_ip"] == "192.168.1.4"
        assert network["http_url"] == "http://192.168.1.4:8000"
        assert network["https_url"] == "https://192.168.1.4:8443"
        assert "同一" in network["access_hint"]
        assert isinstance(network["auth_required"], bool)

    def test_dashboard_shows_lan_access(self, client, monkeypatch):
        import psutil
        import socket as socket_module

        class FakeAddress:
            family = socket_module.AF_INET
            address = "192.168.1.4"

        monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"en1": [FakeAddress()]})
        monkeypatch.setattr(socket_module, "gethostname", lambda: "kid.local")

        r = client.get("/dashboard")

        assert r.status_code == 200
        assert "http://192.168.1.4:8000" in r.text
        assert "https://192.168.1.4:8443" in r.text

    def test_lan_access_uses_configured_ports(self, monkeypatch):
        import psutil
        import socket as socket_module
        from app.routes import system as system_routes

        class FakeAddress:
            family = socket_module.AF_INET
            address = "10.0.0.8"

        monkeypatch.setenv("ASSISTANT_KID_HTTP_PORT", "18000")
        monkeypatch.setenv("ASSISTANT_KID_HTTPS_PORT", "18443")
        monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"en0": [FakeAddress()]})

        network = system_routes._lan_access()

        assert network["http_url"] == "http://10.0.0.8:18000"
        assert network["https_url"] == "https://10.0.0.8:18443"

    def test_lan_access_caches_network_enumeration_for_a_short_ttl(self, monkeypatch):
        import psutil
        import socket as socket_module
        from app.routes import system as system_routes

        class FakeAddress:
            family = socket_module.AF_INET
            address = "192.168.1.4"

        enumerations = []

        def fake_net_if_addrs():
            enumerations.append(1)
            return {"en1": [FakeAddress()]}

        now = [1000.0]
        monkeypatch.setattr(system_routes.time, "time", lambda: now[0])
        monkeypatch.setattr(psutil, "net_if_addrs", fake_net_if_addrs)

        first = system_routes._lan_access()
        second = system_routes._lan_access()
        now[0] += system_routes._LAN_ACCESS_TTL_SECONDS
        third = system_routes._lan_access()

        assert first["lan_ip"] == second["lan_ip"] == "192.168.1.4"
        assert third["lan_ip"] == "192.168.1.4"
        assert len(enumerations) == 2

    def test_tunnel_cors_origin_reloads_when_tunnel_file_changes(self, tmp_path):
        tunnel_file = tmp_path / "tunnel_url.txt"
        tunnel_file.write_text("https://first.example.com", encoding="utf-8")
        old_origins = list(_cors._cors_origins)
        try:
            with patch.object(_cors, "TUNNEL_FILE", str(tunnel_file)):
                assert voice_server._reload_cors_origins() == ["https://first.example.com"]
                assert "https://first.example.com" in voice_server._cors_origins

                tunnel_file.write_text("https://second.example.com", encoding="utf-8")
                assert voice_server._reload_cors_origins() == ["https://second.example.com"]
                assert "https://first.example.com" not in voice_server._cors_origins
                assert "https://second.example.com" in voice_server._cors_origins

                tunnel_file.unlink()
                assert voice_server._reload_cors_origins() == []
                assert "https://second.example.com" not in voice_server._cors_origins
        finally:
            _cors._cors_origins[:] = old_origins

    def test_tunnel_endpoint_reloads_cors_origin(self, client, tmp_path):
        tunnel_file = tmp_path / "tunnel_url.txt"
        tunnel_file.write_text("https://new.example.com", encoding="utf-8")
        old_origins = list(_cors._cors_origins)
        try:
            with patch.object(_cors, "TUNNEL_FILE", str(tunnel_file)), \
                 patch.object(_manage_route, "TUNNEL_FILE", str(tunnel_file)):
                response = client.get("/api/tunnel")

            assert response.status_code == 200
            assert response.json()["url"] == "https://new.example.com"
            assert "https://new.example.com" in voice_server._cors_origins
        finally:
            _cors._cors_origins[:] = old_origins

    def test_dynamic_cors_middleware_reloads_origin_flags(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        current_origins = ["http://localhost:8000"]
        app = Starlette(routes=[Route("/ok", lambda request: JSONResponse({"ok": True}))])
        app.add_middleware(
            voice_server.DynamicCORSMiddleware,
            allow_origins=lambda: list(current_origins),
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["Content-Type"],
            allow_credentials=True,
        )
        client = TestClient(app)
        preflight_headers = {
            "Origin": "https://updated-tunnel.example.com",
            "Access-Control-Request-Method": "GET",
        }

        rejected = client.options("/ok", headers=preflight_headers)
        current_origins[:] = ["http://localhost:8000", "https://updated-tunnel.example.com"]
        allowed = client.options("/ok", headers=preflight_headers)

        assert rejected.status_code == 400
        assert allowed.status_code == 200
        assert allowed.headers["Access-Control-Allow-Origin"] == "https://updated-tunnel.example.com"

    def test_cors_defaults_to_localhost_and_lan_without_wildcard(self, client, monkeypatch):
        monkeypatch.delenv("ASSISTANT_KID_CORS_ORIGINS", raising=False)
        monkeypatch.setattr(
            _cors,
            "lan_origins",
            lambda: [
                "http://192.168.1.4:8000",
                "https://192.168.1.4:8443",
            ],
        )

        old_origins = list(_cors._cors_origins)
        try:
            with patch.object(_cors, "tunnel_origins", lambda: []):
                voice_server._reload_cors_origins()

                assert "*" not in voice_server._cors_origins
                assert "http://localhost:8000" in voice_server._cors_origins
                assert "http://192.168.1.4:8000" in voice_server._cors_origins
                assert "https://192.168.1.4:8443" in voice_server._cors_origins

                lan_preflight = client.options(
                    "/health",
                    headers={
                        "Origin": "http://192.168.1.4:8000",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                unknown_preflight = client.options(
                    "/health",
                    headers={
                        "Origin": "https://evil.example",
                        "Access-Control-Request-Method": "GET",
                    },
                )

            assert lan_preflight.status_code == 200
            assert lan_preflight.headers["Access-Control-Allow-Origin"] == "http://192.168.1.4:8000"
            assert lan_preflight.headers["Access-Control-Allow-Credentials"] == "true"
            assert unknown_preflight.status_code == 400
            assert "Access-Control-Allow-Origin" not in unknown_preflight.headers
        finally:
            _cors._cors_origins[:] = old_origins

    def test_extra_cors_origins_are_explicitly_allowlisted(self, monkeypatch, tmp_path):
        from app import config

        monkeypatch.setenv(
            "ASSISTANT_KID_CORS_ORIGINS",
            " https://phone.example:8443, not-a-url, *, http://192.168.1.4:8000 ",
        )
        monkeypatch.setattr(config, "lan_origins", lambda: ["http://192.168.1.4:8000", "https://192.168.1.4:8443"])

        tunnel_file = tmp_path / "tunnel_url.txt"
        old_origins = list(_cors._cors_origins)
        try:
            with patch.object(_manage_route, "TUNNEL_FILE", str(tunnel_file)):
                voice_server._reload_cors_origins()

                assert "https://phone.example:8443" in voice_server._cors_origins
                assert "http://192.168.1.4:8000" in voice_server._cors_origins
                assert "*" not in voice_server._cors_origins
                assert "not-a-url" not in voice_server._cors_origins
        finally:
            _cors._cors_origins[:] = old_origins

    def test_cors_refreshes_lan_origins_after_ttl_without_tunnel_endpoint(self, monkeypatch):
        from app import config

        now = [1000.0]
        monkeypatch.setattr(voice_server.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(_cors, "tunnel_origins", lambda: [])
        monkeypatch.setattr(_cors, "configured_cors_origins", lambda: [])
        monkeypatch.setattr(config, "configured_cors_origins", lambda: [])
        monkeypatch.setattr(_cors, "lan_origins", lambda: ["http://192.168.1.4:8000"])
        old_origins = list(_cors._cors_origins)
        old_loaded_at = voice_server._cors_origins_loaded_at
        try:
            voice_server._refresh_cors_origins(force=True)
            assert "http://192.168.1.4:8000" in voice_server._cors_origins

            monkeypatch.setattr(_cors, "lan_origins", lambda: ["http://10.0.0.5:8000"])
            voice_server._refresh_cors_origins()
            assert "http://10.0.0.5:8000" not in voice_server._cors_origins

            now[0] += voice_server._CORS_ORIGIN_TTL_SECONDS
            voice_server._refresh_cors_origins()
            assert "http://10.0.0.5:8000" in voice_server._cors_origins
            assert "http://192.168.1.4:8000" not in voice_server._cors_origins
        finally:
            _cors._cors_origins[:] = old_origins
            voice_server._cors_origins_loaded_at = old_loaded_at

    def test_status_exposes_runtime_health(self, client, tmp_path, monkeypatch):
        import time
        import voice_agent

        lock_file = tmp_path / "reminders.json.scheduler.lock"
        proactive_lock_file = tmp_path / "suggestions_state.json.runner.lock"
        monkeypatch.setattr(app_reminders, "SCHEDULER_LOCK_FILE", str(lock_file))
        monkeypatch.setattr(app_reminders, "PROACTIVE_LOCK_FILE", str(proactive_lock_file))
        scheduler_handle = app_reminders.acquire_scheduler_lock()
        proactive_handle = app_reminders.acquire_proactive_lock()
        assert scheduler_handle is not None
        assert proactive_handle is not None

        now = time.time()
        monkeypatch.setattr(_asr_tts, "_tts_unavailable_until", now + 12)
        monkeypatch.setattr(_llm_state, "intent_failures", 2)
        monkeypatch.setattr(_llm_state, "intent_disabled_until", now + 25)

        try:
            r = client.get("/api/status")
            assert r.status_code == 200
            data = r.json()

            assert data["tts"]["active"] is True
            assert 10 <= data["tts"]["remaining_seconds"] <= 12
            assert data["tts"]["cooldown_seconds"] == voice_agent.TTS_FAILURE_COOLDOWN

            assert data["intent_classifier"]["circuit_open"] is True
            assert 23 <= data["intent_classifier"]["remaining_seconds"] <= 25
            assert data["intent_classifier"]["consecutive_failures"] == 2
            assert data["intent_classifier"]["failure_threshold"] == voice_agent.INTENT_FAILURE_THRESHOLD
            assert data["intent_classifier"]["cooldown_seconds"] == voice_agent.INTENT_FAILURE_COOLDOWN

            assert data["scheduler"]["locked"] is True
            assert data["scheduler"]["held_by_this_process"] is True
            assert data["scheduler"]["owner_pid"] == os.getpid()
            assert data["scheduler"]["lock_file"] == str(lock_file)

            assert data["proactive_suggestions"]["locked"] is True
            assert data["proactive_suggestions"]["held_by_this_process"] is True
            assert data["proactive_suggestions"]["owner_pid"] == os.getpid()
            assert data["proactive_suggestions"]["lock_file"] == str(proactive_lock_file)
        finally:
            scheduler_handle.close()
            proactive_handle.close()

    def test_status_reuses_one_brain_status_snapshot(self, client, monkeypatch):
        from app.routes import system as system_routes

        calls = 0
        original_brain_status = system_routes.voice_agent.brain_status

        def counting_brain_status():
            nonlocal calls
            calls += 1
            return original_brain_status()

        monkeypatch.setattr(
            system_routes.voice_agent,
            "brain_status",
            counting_brain_status,
        )

        r = client.get("/api/status")

        assert r.status_code == 200
        data = r.json()
        assert calls == 1
        assert data["brain_health"] == data["brain_status"]
        assert data["brain_ready"] == data["brain_status"]["ready"]
        assert "total_failures" in data["brain_status"]

    def test_dashboard_exposes_runtime_health(self, client, monkeypatch):
        from app.routes import system as system_routes

        brain_calls = 0

        def counting_brain_status():
            nonlocal brain_calls
            brain_calls += 1
            return {"total_failures": 7, "ready": False}

        monkeypatch.setattr(system_routes.voice_agent, "brain_status", counting_brain_status)
        monkeypatch.setattr(system_routes.voice_agent, "tts_status", lambda: {
            "active": True,
            "remaining_seconds": 12.0,
            "cooldown_seconds": system_routes.voice_agent.TTS_FAILURE_COOLDOWN,
        })
        monkeypatch.setattr(system_routes.voice_agent, "intent_classifier_status", lambda: {
            "circuit_open": True,
            "remaining_seconds": 25.0,
            "consecutive_failures": 2,
            "failure_threshold": system_routes.voice_agent.INTENT_FAILURE_THRESHOLD,
            "cooldown_seconds": system_routes.voice_agent.INTENT_FAILURE_COOLDOWN,
        })
        monkeypatch.setattr(system_routes, "scheduler_lock_status", lambda: {
            "locked": True,
            "held_by_this_process": True,
            "owner_pid": os.getpid(),
            "lock_file": "/tmp/charlie-reminders.lock",
        })
        monkeypatch.setattr(system_routes, "proactive_lock_status", lambda: {
            "locked": True,
            "held_by_this_process": True,
            "owner_pid": os.getpid(),
            "lock_file": "/tmp/charlie-proactive.lock",
        })

        r = client.get("/dashboard")

        assert r.status_code == 200
        assert "运行健康" in r.text
        assert "TTS 冷却中" in r.text
        assert "12 秒" in r.text
        assert "意图分类熔断中" in r.text
        assert "2/2" in r.text
        assert "调度器锁已占用" in r.text
        assert "主动建议锁已占用" in r.text
        assert str(os.getpid()) in r.text
        assert "/tmp/charlie-proactive.lock" in r.text
        assert "7" in r.text
        assert brain_calls == 1

    def test_dashboard_shows_conditional_request_metrics(self, client):
        summary = {
            "total_requests": 10,
            "total_errors": 0,
            "cache_hits": 4,
            "conditional_requests": 8,
            "not_modified": 4,
            "not_modified_rate": 50.0,
            "avg_response_ms": 12.3,
            "p95_response_ms": 33.3,
            "endpoints": {},
        }
        from app.routes import system as system_routes

        with patch.object(system_routes._metrics, "summary", return_value=summary):
            r = client.get("/dashboard")

        assert r.status_code == 200
        assert "304命中" in r.text
        assert "4/8" in r.text
        assert "50.0%" in r.text

    def test_dashboard_uses_preference_count_without_snapshot(self, client, monkeypatch):
        import voice_agent
        from app.routes import system as system_routes

        def reject_preference_snapshot():
            raise AssertionError("dashboard must not copy preferences for count")

        monkeypatch.setattr(voice_agent, "preference_count", lambda: 7, raising=False)
        monkeypatch.setattr(voice_agent, "list_preferences", reject_preference_snapshot)
        monkeypatch.setattr(system_routes.voice_agent, "list_preferences", reject_preference_snapshot)

        r = client.get("/dashboard")

        assert r.status_code == 200
        assert '用户偏好</span><span class="val">7 项' in r.text

    def test_status_and_dashboard_ignore_malformed_reminders(self, client, tmp_path):
        malformed_reminders = [
            None,
            {"text": None, "due": "", "done": False},
            {"text": "缺少时间", "due": None, "done": False},
            {"text": "正常提醒", "due": "2026-08-01T10:00:00", "done": False},
        ]
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps(malformed_reminders, ensure_ascii=False), encoding="utf-8")
        old_reminders_file = app_reminders.REMINDERS_FILE
        old_lock_file = app_reminders.REMINDERS_LOCK_FILE
        app_reminders.REMINDERS_FILE = str(reminders_file)
        app_reminders.REMINDERS_LOCK_FILE = str(reminders_file) + ".lock"

        try:
            status = client.get("/api/status")
            dashboard = client.get("/dashboard")
        finally:
            app_reminders.REMINDERS_FILE = old_reminders_file
            app_reminders.REMINDERS_LOCK_FILE = old_lock_file

        assert status.status_code == 200
        assert status.json()["reminders_pending"] == 2
        assert dashboard.status_code == 200
        assert "正常提醒" in dashboard.text
        assert "缺少时间" in dashboard.text
        assert ">None<" not in dashboard.text

    def test_status_and_dashboard_expose_reminder_delivery_counts(self, client, tmp_path):
        reminders = [
            {
                "id": 1,
                "text": "重试中的提醒",
                "due": "2026-08-01T09:00:00",
                "done": False,
                "delivery_state": "retry",
                "attempt_count": 1,
            },
            {
                "id": 2,
                "text": "播报中的提醒",
                "due": "2026-08-01T09:01:00",
                "done": False,
                "delivery_state": "delivering",
                "attempt_count": 2,
            },
            {
                "id": 3,
                "text": "最终失败的提醒",
                "due": "2026-08-01T09:02:00",
                "done": True,
                "delivery_state": "failed",
                "attempt_count": 3,
            },
            {
                "id": 4,
                "text": "普通待办",
                "due": "2026-08-01T20:00:00",
                "done": False,
            },
        ]
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps(reminders, ensure_ascii=False), encoding="utf-8")
        old_reminders_file = app_reminders.REMINDERS_FILE
        old_lock_file = app_reminders.REMINDERS_LOCK_FILE
        app_reminders.REMINDERS_FILE = str(reminders_file)
        app_reminders.REMINDERS_LOCK_FILE = str(reminders_file) + ".lock"

        try:
            status = client.get("/api/status")
            dashboard = client.get("/dashboard")
        finally:
            app_reminders.REMINDERS_FILE = old_reminders_file
            app_reminders.REMINDERS_LOCK_FILE = old_lock_file

        assert status.status_code == 200
        delivery = status.json()["reminder_delivery"]
        assert delivery == {
            "active": 2,
            "delivering": 1,
            "retry": 1,
            "failed": 1,
        }
        assert dashboard.status_code == 200
        assert "重试中 1" in dashboard.text
        assert "失败 1" in dashboard.text

    def test_status_and_dashboard_scan_reminders_once(self, client, monkeypatch):
        from app.routes import system as system_routes

        class TrackingList(list):
            def __init__(self):
                super().__init__()
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        reminders = TrackingList()
        reminders.extend([
            {"text": "待办一", "due": "2026-08-01T20:00:00", "done": False},
            {
                "text": "重试提醒",
                "due": "2026-08-01T09:00:00",
                "done": False,
                "delivery_state": "retry",
            },
            {"text": "已完成", "due": "2026-08-01T08:00:00", "done": True},
        ])
        load_calls = {"count": 0}

        def counting_load_reminders():
            load_calls["count"] += 1
            return reminders

        monkeypatch.setattr(system_routes, "_load_reminders", counting_load_reminders)

        status = client.get("/api/status")
        assert status.status_code == 200
        assert load_calls["count"] == 1
        assert reminders.iterations == 1
        assert status.json()["reminders_pending"] == 2
        assert status.json()["reminder_delivery"]["retry"] == 1

        load_calls["count"] = 0
        reminders.iterations = 0
        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert load_calls["count"] == 1
        assert reminders.iterations == 1
        assert "提醒 (2 待办)" in dashboard.text
        assert "重试中 1" in dashboard.text

    def test_status_and_dashboard_cache_host_metadata(self, client, monkeypatch):
        import platform
        import socket as socket_module
        from app.routes import system as system_routes

        system_routes._host_metadata_cache = None
        calls = {"hostname": 0, "system": 0, "release": 0}

        def counting_hostname():
            calls["hostname"] += 1
            return "cached-host"

        def counting_system():
            calls["system"] += 1
            return "TestOS"

        def counting_release():
            calls["release"] += 1
            return "1.2.3"

        monkeypatch.setattr(socket_module, "gethostname", counting_hostname)
        monkeypatch.setattr(platform, "system", counting_system)
        monkeypatch.setattr(platform, "release", counting_release)

        monkeypatch.setattr(socket_module, "gethostname", counting_hostname)

        first = client.get("/api/status")
        second = client.get("/api/status")
        dashboard = client.get("/dashboard")

        assert first.status_code == 200
        assert second.status_code == 200
        assert dashboard.status_code == 200
        assert first.json()["device"] == "cached-host"
        assert second.json()["device"] == "cached-host"
        assert first.json()["os"] == "TestOS 1.2.3"
        assert "cached-host" in dashboard.text
        assert "TestOS 1.2.3" in dashboard.text
        assert calls["hostname"] <= 2
        assert calls["system"] == 1
        assert calls["release"] == 1

    def test_status_records_polling_telemetry(self, client):
        before = client.get("/api/status").json()["polling"]
        assert before["totals"] == {"paused": 0, "resumed": 0, "backoff": 0, "errors": 0}

        paused = client.post("/api/polling-telemetry", json={"event": "paused", "job": "reminders"})
        resumed = client.post("/api/polling-telemetry", json={"event": "resumed"})
        error = client.post("/api/polling-telemetry", json={"event": "error", "job": "preferences"})

        assert paused.status_code == 202
        assert resumed.status_code == 202
        assert error.status_code == 202

        data = client.get("/api/status").json()["polling"]
        assert data["totals"] == {"paused": 1, "resumed": 1, "backoff": 1, "errors": 1}
        assert data["jobs"]["reminders"]["paused"] == 1
        assert data["jobs"]["preferences"]["errors"] == 1
        assert data["jobs"]["preferences"]["backoff"] == 1
        assert data["last_event"]["event"] == "errors"
        assert data["last_event"]["job"] == "preferences"
        assert isinstance(data["last_event"]["at"], float)

    def test_polling_telemetry_rejects_unknown_values(self, client):
        missing_job = client.post("/api/polling-telemetry", json={"event": "paused"})
        synthetic_backoff = client.post("/api/polling-telemetry", json={"event": "backoff", "job": "reminders"})
        bad_event = client.post("/api/polling-telemetry", json={"event": "frozen", "job": "reminders"})
        bad_job = client.post("/api/polling-telemetry", json={"event": "paused", "job": "weather"})

        assert missing_job.status_code == 422
        assert synthetic_backoff.status_code == 422
        assert bad_event.status_code == 422
        assert bad_job.status_code == 422
        assert client.get("/api/status").json()["polling"]["totals"] == {
            "paused": 0,
            "resumed": 0,
            "backoff": 0,
            "errors": 0,
        }

    def test_dashboard_exposes_polling_telemetry(self, client):
        client.post("/api/polling-telemetry", json={"event": "paused", "job": "reminders"})
        client.post("/api/polling-telemetry", json={"event": "paused", "job": "tunnel"})
        client.post("/api/polling-telemetry", json={"event": "resumed"})
        client.post("/api/polling-telemetry", json={"event": "error", "job": "preferences"})

        r = client.get("/dashboard")

        assert r.status_code == 200
        assert "轮询暂停" in r.text
        assert "暂停 2" in r.text
        assert "恢复 1" in r.text
        assert "退避 1" in r.text
        assert "失败 1" in r.text

    def test_version(self, client):
        r = client.get("/api/version")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert data["version"] == "3.2.0"
        # brain 字段动态化：只要是非空字符串且包含 MCP 数量信息即可
        brain = data.get("brain", "")
        assert brain
        assert "MCP" in brain


class TestMetrics:
    def test_summary_can_skip_endpoint_breakdown(self):
        metrics = Metrics()
        metrics.record("GET /test", 12.0)

        summary = metrics.summary(include_endpoints=False)

        assert summary["total_requests"] == 1
        assert summary["avg_response_ms"] == 12.0
        assert "endpoints" not in summary

    def test_dashboard_metrics_summary_skips_endpoint_breakdown(self, client, monkeypatch):
        from app.routes import system as system_routes

        observed = {}
        original_summary = system_routes._metrics.summary

        def spy_summary(*args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return original_summary(*args, **kwargs)

        monkeypatch.setattr(system_routes._metrics, "summary", spy_summary)

        r = client.get("/dashboard")

        assert r.status_code == 200
        assert observed["kwargs"].get("include_endpoints") is False

    def test_summary_preserves_latency_window_stats(self):
        metrics = Metrics()
        for duration_ms in range(1, 101):
            metrics.record("GET /test", float(duration_ms))

        summary = metrics.summary()

        assert summary["avg_response_ms"] == 50.5
        assert summary["p95_response_ms"] == 96.0

    def test_summary_caches_latency_stats_until_new_sample_arrives(self, monkeypatch):
        metrics = Metrics()
        for duration_ms in range(1, 101):
            metrics.record("GET /test", float(duration_ms))
        sort_calls = 0
        original_sorted = sorted

        def spy_sorted(*args, **kwargs):
            nonlocal sort_calls
            sort_calls += 1
            return original_sorted(*args, **kwargs)

        monkeypatch.setattr("app.state.sorted", spy_sorted, raising=False)

        first = metrics.summary()
        second = metrics.summary()
        metrics.record("GET /test", 101.0)
        third = metrics.summary()

        assert first["p95_response_ms"] == 96.0
        assert second["p95_response_ms"] == 96.0
        assert third["p95_response_ms"] == 97.0
        assert sort_calls == 2

    def test_metrics(self, client):
        r = client.get("/api/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "total_requests" in data

    def test_metrics_track_conditional_get_304_hits(self, client):
        first = client.get("/api/reminders")
        assert first.status_code == 200
        before = client.get("/api/metrics").json()

        second = client.get(
            "/api/reminders",
            headers={"If-None-Match": first.headers["etag"]},
        )

        assert second.status_code == 304
        after = client.get("/api/metrics").json()
        assert after["conditional_requests"] >= before.get("conditional_requests", 0) + 1
        assert after["not_modified"] >= before.get("not_modified", 0) + 1
        assert after["cache_hits"] == after["not_modified"]
        assert after["endpoints"]["/api/reminders"]["not_modified"] >= 1
        assert 0 <= after["not_modified_rate"] <= 100

    def test_metrics_supports_etag_304(self, client):
        first = client.get("/api/metrics")
        assert first.status_code == 200
        assert first.headers.get("etag")

        second = client.get("/api/metrics", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 304
        # Metrics must not count its own polling; otherwise its body changes on
        # every request and conditional GET can never return a stable ETag.
        assert "/api/metrics" not in first.json().get("endpoints", {})

    def test_metrics_304_skips_summary_build(self, client, monkeypatch):
        import voice_server

        first = client.get("/api/metrics")
        assert first.status_code == 200

        def forbid_summary(*args, **kwargs):
            raise AssertionError("304 metrics response must not rebuild summary")

        monkeypatch.setattr(voice_server._metrics, "summary", forbid_summary)

        second = client.get("/api/metrics", headers={"If-None-Match": first.headers["etag"]})

        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == first.headers["etag"]

    def test_status_metrics_exclude_metrics_endpoint(self, client):
        client.get("/api/metrics")

        status = client.get("/api/status")
        dashboard = client.get("/dashboard")

        assert status.status_code == 200
        assert "/api/metrics" not in status.json()["metrics"]["endpoints"]
        assert dashboard.status_code == 200


class TestReminders:
    """提醒管理API"""

    @pytest.fixture(autouse=True)
    def clear_notifications(self):
        voice_server._drain_notifications()
        yield
        voice_server._drain_notifications()

    def test_sse_client_registry_tracks_queues_with_snapshot_copy(self):
        first_queue = asyncio.Queue()
        second_queue = asyncio.Queue()

        register_sse_client(first_queue)
        register_sse_client(second_queue)
        try:
            assert sse_client_count() == 2

            snapshot = snapshot_sse_clients()
            assert len(snapshot) == 2
            assert first_queue in snapshot
            assert second_queue in snapshot
        finally:
            unregister_sse_client(first_queue)
            unregister_sse_client(second_queue)


class TestWebSocketConcurrentSafety:
    """验证 _ws_clients / _ws_session_groups / _ws_client_locations 的锁保护。"""

    def test_concurrent_register_cleanup_no_corruption(self):
        """并发注册/注销不应引发 RuntimeError/KeyError。"""
        from app.routes.websocket import (
            _ws_join_session, _ws_cleanup_after_disconnect,
            _ws_broadcast_to_session, _ws_cancel_stream,
        )
        from app.state import _ws_clients, _ws_session_groups, _ws_client_locations

        # 构造若干假 WebSocket 对象（只需 id 唯一）
        class FakeWS:
            def __init__(self, ws_id):
                self.id = ws_id

        errors = []

        def worker(ws_id):
            try:
                ws = FakeWS(ws_id)
                # 模拟 register_xiaozhi_client 的写入路径
                with _ws_route._ws_clients_lock:
                    _ws_clients[ws_id] = {
                        "ws": ws,
                        "interrupt": False,
                        "last_active": time.time(),
                        "stream_task": None,
                    }
                _ws_join_session(ws_id, "session-1")
                _ws_cancel_stream(ws_id)
                # 模拟清理
                _ws_cleanup_after_disconnect(ws_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发访问引发异常: {errors}"
        # 清理后应无残留
        assert len(_ws_clients) == 0
        assert len(_ws_session_groups) == 0
        assert len(_ws_client_locations) == 0