"""CosyVoice 语音克隆模块测试 (21 cases)

覆盖：
1. 声音配置 CRUD (create/list/delete/get_audio)
2. 合成函数 (synthesize, tts_cosyvoice provider)
3. 可用性检查 (is_available)
4. 边界: 空名称/缺失文件/服务错误
5. MCP 工具注册 (FastMCP availability)

所有用例的声音目录都重定向到 pytest tmp 目录，绝不触碰真实 data/cosyvoice_voices/。
"""
from __future__ import annotations

import os
import sys
import tempfile
import asyncio
from unittest.mock import MagicMock, patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


@pytest.fixture(autouse=True)
def _isolated_voice_dir(tmp_path):
    """把 cosyvoice_clone 的声音目录重定向到本次测试的 tmp 目录。

    autouse：每个测试都跑在隔离目录里，防止误删真实用户声音配置。
    """
    import agent.cosyvoice_clone as cv
    real_dir = cv._VOICE_DIR
    cv._VOICE_DIR = str(tmp_path)
    cv._ensure_voice_dir()
    yield tmp_path
    cv._VOICE_DIR = real_dir


class TestVoiceProfileCRUD:
    """声音配置的增删查。"""

    def _make_profile(self, name="测试声音"):
        from agent.cosyvoice_clone import create_voice_profile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"RIFF" + b"\x00" * 100)  # 伪 WAV
            path = tmp.name
        try:
            return create_voice_profile(name, path)
        finally:
            os.unlink(path)

    def test_create_voice_profile_ok(self):
        r = self._make_profile("小明")
        assert r["ok"] is True
        assert r["name"] == "小明"

    def test_create_duplicate_updates(self):
        self._make_profile("小明")
        r = self._make_profile("小明")
        assert r["ok"] is True  # 覆盖更新

    def test_create_empty_name_fails(self):
        from agent.cosyvoice_clone import create_voice_profile
        r = create_voice_profile("", "/tmp/nonexistent.wav")
        assert r["ok"] is False

    def test_create_missing_audio_fails(self):
        from agent.cosyvoice_clone import create_voice_profile
        r = create_voice_profile("小明", "/nonexistent/path.wav")
        assert r["ok"] is False

    def test_list_voices_empty(self):
        from agent.cosyvoice_clone import list_voices
        assert list_voices() == []

    def test_list_voices_after_create(self):
        self._make_profile("小明")
        self._make_profile("小红")
        from agent.cosyvoice_clone import list_voices
        voices = list_voices()
        names = [v["name"] for v in voices]
        assert "小明" in names
        assert "小红" in names

    def test_delete_voice_ok(self):
        self._make_profile("小明")
        from agent.cosyvoice_clone import delete_voice
        r = delete_voice("小明")
        assert r["ok"] is True

    def test_delete_nonexistent_is_ok(self):
        from agent.cosyvoice_clone import delete_voice
        r = delete_voice("不存在的声")
        assert r["ok"] is True  # 删除不存在的 = 成功 (幂等)

    def test_get_voice_audio_returns_path(self):
        self._make_profile("小明")
        from agent.cosyvoice_clone import get_voice_audio
        path = get_voice_audio("小明")
        assert path is not None
        assert os.path.isfile(path)

    def test_get_voice_audio_missing_returns_none(self):
        from agent.cosyvoice_clone import get_voice_audio
        assert get_voice_audio("不存在的声") is None

    def test_profile_has_required_fields(self):
        r = self._make_profile("小明")
        assert r["name"] == "小明"
        assert "speaker_id" in r
        assert "created_at" in r


class TestSynthesize:
    """合成函数的行为。"""

    def test_synthesize_empty_text_returns_none(self):
        from agent.cosyvoice_clone import synthesize
        assert synthesize("") is None
        assert synthesize("   ") is None

    @patch("requests.post")
    @patch("agent.cosyvoice_clone._upload_audio")
    def test_synthesize_success_returns_mp3(self, mock_upload, mock_post):
        """服务返回音频时转成 MP3。"""
        from agent.cosyvoice_clone import create_voice_profile, synthesize
        mock_upload.return_value = ""  # 跳过实际上传
        # 创建声音配置
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"RIFF" + b"\x00" * 100)
            path = tmp.name
        try:
            create_voice_profile("试音", path)
        finally:
            os.unlink(path)

        # mock requests: 返回 WAV 内容
        mock_wav = b"RIFF" + b"\x00" * 1000
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "audio/wav"}
        mock_resp.content = mock_wav
        mock_post.return_value = mock_resp

        # mock ffmpeg: stdout = "mp3" (must be > 100 bytes to pass length check)
        mp3_data = b"MP3DATA" * 20  # 140 bytes
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=mp3_data, returncode=0)
            result = synthesize("你好世界", voice="试音")
            assert result == mp3_data

    @patch("requests.post")
    @patch("agent.cosyvoice_clone._upload_audio")
    def test_synthesize_http_error_returns_none(self, mock_upload, mock_post):
        from agent.cosyvoice_clone import synthesize
        mock_upload.return_value = ""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp
        assert synthesize("你好") is None

    @patch("requests.post")
    @patch("agent.cosyvoice_clone._upload_audio")
    def test_synthesize_connection_error_returns_none(self, mock_upload, mock_post):
        from agent.cosyvoice_clone import synthesize
        mock_upload.return_value = ""
        mock_post.side_effect = ConnectionError("连接失败")
        assert synthesize("你好") is None


class TestIsAvailable:
    """服务可用性检查。"""

    @patch("requests.get")
    def test_available_when_health_200(self, mock_get):
        from agent.cosyvoice_clone import is_available
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        assert is_available() is True

    @patch("requests.get")
    def test_not_available_when_timeout(self, mock_get):
        from agent.cosyvoice_clone import is_available
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout()
        assert is_available() is False

    @patch("requests.get")
    def test_not_available_when_connection_error(self, mock_get):
        from agent.cosyvoice_clone import is_available
        mock_get.side_effect = ConnectionError()
        assert is_available() is False


class TestTtsCosyvoiceProvider:
    """TTS provider 函数 (适配 asr_tts.py calling convention)。"""

    @patch("agent.cosyvoice_clone.synthesize")
    def test_provider_calls_synthesize(self, mock_syn):
        from agent.cosyvoice_clone import tts_cosyvoice
        mock_syn.return_value = b"MP3"
        result = tts_cosyvoice("Hello")
        assert result == b"MP3"
        mock_syn.assert_called_once()

    @patch("agent.cosyvoice_clone.synthesize")
    def test_provider_raises_on_failure(self, mock_syn):
        from agent.cosyvoice_clone import tts_cosyvoice
        mock_syn.return_value = None
        with pytest.raises(RuntimeError, match="合成失败"):
            tts_cosyvoice("Hello")


class TestSynthesizeContract:
    """synthesize 的返回值契约：只返回 MP3，绝不返回原始 WAV。"""

    @patch("requests.post")
    @patch("agent.cosyvoice_clone._upload_audio")
    def test_ffmpeg_failure_returns_none_not_wav(self, mock_upload, mock_post):
        """ffmpeg 转换失败必须返回 None（走 fallback 链），不能返回 WAV 假成功。"""
        from agent.cosyvoice_clone import synthesize
        mock_upload.return_value = ""
        wav = b"RIFF" + b"\x00" * 1000
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "audio/wav"}
        mock_resp.content = wav
        mock_post.return_value = mock_resp

        with patch("subprocess.run") as mock_run:
            # ffmpeg 失败：stdout 为空
            mock_run.return_value = MagicMock(stdout=b"", returncode=1)
            result = synthesize("你好")
            assert result is None  # 不是 wav


class TestMCPTools:
    """MCP 工具：函数可直接调用 + 已注册进 magic-jarvis 常驻链。"""

    def test_tool_functions_callable(self):
        """4 个工具函数是模块级普通函数，可被 FastMCP 直接注册。"""
        import inspect
        from agent import cosyvoice_clone as cv
        for fname in ("create_voice", "list_voice_profiles",
                      "delete_voice_profile", "voice_status"):
            fn = getattr(cv, fname, None)
            assert callable(fn), f"{fname} 不存在或不可调用"
            # FastMCP @tool 需要带 docstring 的函数
            assert (fn.__doc__ or "").strip(), f"{fname} 缺少 docstring"
            # 参数必须可被 inspect（FastMCP 用 signature 生成 schema）
            inspect.signature(fn)

    def test_registered_in_magic_jarvis(self):
        """工具应注册进 magic-jarvis.py（进程内常驻加载）。"""
        try:
            from mcp.server.fastmcp import FastMCP
            import importlib.util
            jarvis_path = os.path.join(PROJECT_DIR, "magic-jarvis.py")
            spec = importlib.util.spec_from_file_location("_jarvis_mcp_test", jarvis_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tool_names = {t.name for t in asyncio.run(mod.mcp.list_tools())}
            expected = {"vision_status", "voice_status",
                        "list_voice_profiles", "create_voice", "delete_voice_profile"}
            missing = expected - tool_names
            assert not missing, f"magic-jarvis 缺少工具: {missing}"
        except ImportError:
            pytest.skip("FastMCP 未安装")
