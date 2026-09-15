"""Round 6 覆盖补齐: barge-in守卫 / _mp3_to_pcm auto-probe / exchange_rate / on_this_day / music"""
import json
import os
import subprocess
import sys
import socket
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# Reuse xiaozhi WS test helpers（不要导入 xiaozhi_server fixture：
# 跨文件导入 session fixture 会产生第二个定义实例，与本文件各自绑定
# 同一端口互相冲突。这里用本文件独立的 module 级 server，端口错开。）
from tests.test_xiaozhi_ws import (  # noqa: E402
    _hello, _drain_json, _send_utterance_burst,
    pcm_to_opus_packets, generate_tone_pcm, collect_responses,
)
from websockets.sync.client import connect as ws_connect  # noqa: E402

_HOST = "127.0.0.1"
_PORT = int(os.environ.get("XIAOZHI_TEST_PORT_R6", "8011"))


@pytest.fixture(scope="module")
def xiaozhi_server():
    import uvicorn
    import voice_server
    APP = voice_server.app
    config = uvicorn.Config(APP, host=_HOST, port=_PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            with socket.create_connection((_HOST, _PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError(f"r6 server did not start on {_HOST}:{_PORT}")
    time.sleep(0.3)
    try:
        yield f"ws://{_HOST}:{_PORT}/ws/xiaozhi"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ======================================================================
# Gap 2: _mp3_to_pcm auto-probe
# ======================================================================

class TestMp3ToPcm:
    def test_auto_probe_real_mp3(self, tmp_path):
        """ffmpeg auto-probe（无 -f mp3）将真实 MP3 转为非空 s16le PCM，
        字节数 ≈ 时长×sample_rate×2，允许 ±10%。"""
        mp3_path = tmp_path / "tone.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.5",
             "-b:a", "32k", "-ac", "1", "-ar", "24000", "-f", "mp3",
             str(mp3_path)],
            capture_output=True, timeout=10,
        )
        assert r.returncode == 0, f"ffmpeg generate failed: {r.stderr.decode()[:300]}"
        mp3_data = mp3_path.read_bytes()
        assert len(mp3_data) > 100, "generated MP3 is suspiciously small"

        from app.xiaozhi_codec import _mp3_to_pcm
        pcm = _mp3_to_pcm(mp3_data, sample_rate=24000)

        assert len(pcm) > 0, "PCM output is empty"
        expected = int(0.5 * 24000 * 2)
        assert expected * 0.9 <= len(pcm) <= expected * 1.1, (
            f"PCM size {len(pcm)} out of expected range ~{expected}")

    def test_volume_boost(self, tmp_path):
        """volume_db=6 应成功转换，不抛异常。"""
        mp3_path = tmp_path / "tone.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.5",
             "-b:a", "32k", "-ac", "1", "-ar", "24000", "-f", "mp3",
             str(mp3_path)],
            capture_output=True, timeout=10,
        )
        assert r.returncode == 0
        mp3_data = mp3_path.read_bytes()

        from app.xiaozhi_codec import _mp3_to_pcm
        pcm = _mp3_to_pcm(mp3_data, sample_rate=24000, volume_db=6.0)
        assert len(pcm) > 0, "volume-boosted PCM is empty"

    def test_too_small_raises_runtime_error(self):
        """<10B 输入应抛 RuntimeError。"""
        from app.xiaozhi_codec import _mp3_to_pcm
        with pytest.raises(RuntimeError, match="input too small"):
            _mp3_to_pcm(b"ab", sample_rate=24000)


# ======================================================================
# Gap 4: exchange_rate / on_this_day
# ======================================================================

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class TestExchangeRate:
    @patch("app.exchange_rate.requests.get")
    def test_get_rates_success(self, mock_get):
        """正常返回汇率表且缓存生效。"""
        from app.exchange_rate import get_rates

        mock_get.return_value = FakeResp({
            "rates": {"CNY": 7.2, "EUR": 0.92, "JPY": 150.0}
        })

        rates = get_rates("USD")
        assert rates["CNY"] == 7.2
        assert rates["EUR"] == 0.92
        assert len(rates) == 3
        # 第二次走缓存，不触发 requests
        rates2 = get_rates("USD")
        assert rates2 == rates
        mock_get.assert_called_once()

    @patch("app.exchange_rate.requests.get", side_effect=Exception("network down"))
    def test_get_rates_network_failure(self, mock_get):
        """网络异常 → 降级返回空字典，不抛异常。"""
        from app import exchange_rate

        exchange_rate._cache.clear()  # 缓存 TTL 短路在 requests 之前，必须先清
        rates = exchange_rate.get_rates("USD")
        assert rates == {}

    @patch("app.exchange_rate.requests.get")
    def test_convert_success(self, mock_get):
        """汇率换算正确。"""
        from app.exchange_rate import convert

        mock_get.return_value = FakeResp({
            "rates": {"EUR": 0.92, "CNY": 7.2}
        })

        assert convert(100, "USD", "EUR") == 92.0
        assert convert(100, "USD", "CNY") == 720.0

    @patch("app.exchange_rate.requests.get")
    def test_convert_same_currency(self, mock_get):
        """同币种换算直接返回原值，不请求网络。"""
        from app.exchange_rate import convert

        assert convert(100, "USD", "USD") == 100.0
        mock_get.assert_not_called()

    @patch("app.exchange_rate.requests.get")
    def test_convert_unknown_rate(self, mock_get):
        """找不到目标汇率 → 返回 None。"""
        from app.exchange_rate import convert

        mock_get.return_value = FakeResp({"rates": {}})

        assert convert(100, "USD", "XYZ") is None


class TestOnThisDay:
    @patch("app.on_this_day.requests.get")
    def test_get_events_success(self, mock_get):
        """正常解析历史事件，按年份降序。"""
        from app.on_this_day import get_events

        mock_get.return_value = FakeResp({
            "events": [
                {"year": "1969", "description": "阿波罗11号登月", "wikipedia": []},
                {"year": "2001", "description": "九一一事件", "wikipedia": []},
            ]
        })

        events = get_events(8, 8)
        assert len(events) == 2
        assert events[0]["year"] == "2001"
        assert events[1]["year"] == "1969"

    @patch("app.on_this_day.requests.get", side_effect=Exception("network down"))
    def test_get_events_network_failure(self, mock_get):
        """网络异常 → 降级返回空列表，不抛异常。"""
        from app import on_this_day

        on_this_day._cache.clear()  # 缓存短路在 requests 之前，必须先清
        events = on_this_day.get_events(8, 8)
        assert events == []

    @patch("app.on_this_day.requests.get")
    def test_get_events_text_format(self, mock_get):
        """get_events_text 正常格式化输出。"""
        from app import on_this_day

        on_this_day._cache.clear()  # 清掉 success 用例的缓存（缓存短路在 requests 之前）
        mock_get.return_value = FakeResp({
            "events": [
                {"year": "1969", "description": "阿波罗11号登月", "wikipedia": []},
            ]
        })
        text = on_this_day.get_events_text(8, 8, count=1)
        assert "1969年" in text
        assert "阿波罗11号登月" in text


# ======================================================================
# Gap 5: agent/music.py
# ======================================================================

class FakeSubprocessResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestMusicDirectPlay:
    @patch("agent.music.subprocess.run")
    def test_random_play_success(self, mock_run):
        """随机播放命中关键词，成功返回 __MUSIC__ 标记。"""
        from agent.music import direct_music_play

        mock_run.side_effect = [
            FakeSubprocessResult(0, json.dumps({
                "recommend": [{"id": 123, "name": "随机歌曲",
                                "ar": [{"name": "歌手"}]}]
            })),
            FakeSubprocessResult(0, json.dumps({
                "data": [{"url": "https://example.com/song.mp3"}]
            })),
        ]

        result = direct_music_play("随机来一首歌")
        assert result == "__MUSIC__https://example.com/song.mp3__随机歌曲__歌手"

    @patch("agent.music.subprocess.run")
    def test_keyword_play_success(self, mock_run):
        """关键词播放命中，成功返回。"""
        from agent.music import direct_music_play

        mock_run.side_effect = [
            FakeSubprocessResult(0, json.dumps({
                "result": {
                    "songs": [{"id": 456, "name": "海阔天空",
                               "ar": [{"name": "Beyond"}]}]
                }
            })),
            FakeSubprocessResult(0, json.dumps({
                "data": [{"url": "https://example.com/haitian.mp3"}]
            })),
        ]

        result = direct_music_play("播放海阔天空")
        assert result == "__MUSIC__https://example.com/haitian.mp3__海阔天空__Beyond"

    @patch("agent.music.subprocess.run", side_effect=Exception("ncm not found"))
    def test_ncm_unavailable_graceful(self, mock_run):
        """ncm 不可用时安全降级，不抛未捕获异常。"""
        from agent.music import direct_music_play

        result = direct_music_play("播放歌曲")
        assert result == ""

    @patch("agent.music.subprocess.run")
    def test_search_no_results_returns_empty(self, mock_run):
        """搜索无结果时返回空字符串。"""
        from agent.music import direct_music_play

        mock_run.return_value = FakeSubprocessResult(0, json.dumps({
            "result": {"songs": []}
        }))

        result = direct_music_play("播放不存在的歌")
        assert result == ""

    @patch("agent.music.subprocess.run")
    def test_url_empty_returns_empty(self, mock_run):
        """拿到 song_id 但 url 为空时返回空字符串。"""
        from agent.music import direct_music_play

        mock_run.side_effect = [
            FakeSubprocessResult(0, json.dumps({
                "result": {
                    "songs": [{"id": 999, "name": "无名", "ar": []}]
                }
            })),
            FakeSubprocessResult(0, json.dumps({"data": [{"url": ""}]})),
        ]

        result = direct_music_play("播放无名")
        assert result == ""
