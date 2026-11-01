"""Round 5 回归：ASR 竞速择优 + lite 单跑防重入 + FAST_PATHS 收紧 + eval CER"""
import os
import sys
import time
import re
from unittest.mock import patch, MagicMock
import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# ASR race
# ======================================================================

class TestASRRace:
    @staticmethod
    def _pin_stepfun(monkeypatch):
        """ASR_PRIORITY 是导入期读的常量，patch.dict 改 env 无效，
        必须直接改模块变量，否则实际跑的是 nmcloud。"""
        from agent import asr_tts
        monkeypatch.setattr(asr_tts, "ASR_PRIORITY", ["stepfun"])

    def test_local_clean_never_calls_cloud(self, monkeypatch):
        """本地干净 → 直接返回本地，云端一次都不发。

        旧实现是并行竞速，云端 nmcloud 常态 600ms+ 永远输给本地，却仍然
        每句占一个 worker；连续对话时把 2-worker 池占满，本地任务排队超
        deadline 掉 legacy（实测 15:37-15:43 连续 11 次 581-1387ms）。
        """
        from agent import asr_tts
        self._pin_stepfun(monkeypatch)

        calls = {"cloud": 0}

        def counting_cloud(*a, **k):
            calls["cloud"] += 1
            return "云端结果"

        monkeypatch.setattr(asr_tts, "_asr_sense_voice", lambda *a, **k: "本地结果")
        monkeypatch.setattr(asr_tts, "_asr_stepfun", counting_cloud)

        with patch.dict(os.environ, {"STEPFUN_KEY": "k", "NM_CLOUD_KEY": "k", "ASR_RACE": "1", "ASR_RACE_DEADLINE_MS": "1200"}):
            text, provider = asr_tts.asr_race(b"audio", "wav")
        assert provider == "sensevoice"
        assert text == "本地结果"
        assert calls["cloud"] == 0, "本地干净时不该发云端请求"

    def test_local_empty_falls_back_to_cloud(self, monkeypatch):
        """本地空 → 才问云端"""
        from agent import asr_tts
        self._pin_stepfun(monkeypatch)

        monkeypatch.setattr(asr_tts, "_asr_sense_voice", lambda *a, **k: "")
        monkeypatch.setattr(asr_tts, "_asr_stepfun", lambda *a, **k: "云端补位")

        with patch.dict(os.environ, {"STEPFUN_KEY": "k", "NM_CLOUD_KEY": "k", "ASR_RACE": "1", "ASR_RACE_DEADLINE_MS": "1200"}):
            text, provider = asr_tts.asr_race(b"audio", "wav")
        assert provider == "stepfun"
        assert text == "云端补位"

    def test_local_failure_uses_cloud(self, monkeypatch):
        """本地抛异常 → 云端补位"""
        from agent import asr_tts
        self._pin_stepfun(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("sensevoice down")

        monkeypatch.setattr(asr_tts, "_asr_sense_voice", boom)
        monkeypatch.setattr(asr_tts, "_asr_stepfun", lambda *a, **k: "云端救场")

        with patch.dict(os.environ, {"STEPFUN_KEY": "k", "NM_CLOUD_KEY": "k", "ASR_RACE": "1", "ASR_RACE_DEADLINE_MS": "1200"}):
            text, provider = asr_tts.asr_race(b"audio", "wav")
        assert provider == "stepfun"

    def test_local_garbled_falls_back_to_cloud(self, monkeypatch):
        """本地乱码 → 走云端（patch 检测器，确定性驱动该分支）"""
        from agent import asr_tts
        self._pin_stepfun(monkeypatch)

        def garbled_sv(*a, **k):
            time.sleep(0.05)
            return "呢就系有钱阿噶追近"

        def slow_cloud(*a, **k):
            time.sleep(0.3)
            return "云端结果"

        monkeypatch.setattr(asr_tts, "_asr_sense_voice", garbled_sv)
        monkeypatch.setattr(asr_tts, "_asr_stepfun", slow_cloud)
        # 真实 is_garbled_asr 对错字类乱码拦截率为 0（已知局限），此处
        # patch 为 True 以确定性驱动"本地乱码→问云端"分支
        monkeypatch.setattr(asr_tts, "is_garbled_asr", lambda t: True)

        with patch.dict(os.environ, {"STEPFUN_KEY": "k", "NM_CLOUD_KEY": "k", "ASR_RACE": "1", "ASR_RACE_DEADLINE_MS": "1200"}):
            text, provider = asr_tts.asr_race(b"audio", "wav")
        assert provider == "stepfun"

    def test_cloud_exception_falls_back_to_local(self, monkeypatch):
        """云端异常 → 回退本地（本地乱码时仍保留本地结果）"""
        from agent import asr_tts
        self._pin_stepfun(monkeypatch)

        monkeypatch.setattr(asr_tts, "_asr_sense_voice", lambda *a, **k: "本地兜底")
        monkeypatch.setattr(asr_tts, "_asr_stepfun", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(asr_tts, "is_garbled_asr", lambda t: True)

        with patch.dict(os.environ, {"STEPFUN_KEY": "k", "NM_CLOUD_KEY": "k", "ASR_RACE": "1", "ASR_RACE_DEADLINE_MS": "1200"}):
            text, provider = asr_tts.asr_race(b"audio", "wav")
        assert provider == "sensevoice"

    def test_asr_race_zero_uses_legacy(self, monkeypatch):
        """ASR_RACE=0 → 走旧 asr()"""
        from agent import asr_tts
        self._pin_stepfun(monkeypatch)

        with patch.object(asr_tts, "asr", return_value="legacy") as mock_asr:
            with patch.dict(os.environ, {"STEPFUN_KEY": "k", "ASR_RACE": "0"}):
                text, provider = asr_tts.asr_race(b"audio", "wav")
        assert provider == "legacy"
        mock_asr.assert_called_once()


# ======================================================================
# Lite 单跑防重入
# ======================================================================

class TestLiteNoDoubleInvoke:
    @patch("agent.llm._classify_intent", return_value="none")
    @patch("agent.llm._chat_lite_stream")
    @patch("agent.llm._get_brain")
    def test_lite_not_called_after_optimistic_failure(self, mock_get_brain, mock_lite, mock_classify):
        """乐观 lite 失败后，第二次 lite 不再被调用"""
        from agent.llm import brain_stream_sentences

        mock_lite.return_value = iter([])  # 无产出
        mock_brain = MagicMock()
        mock_brain.system_message = ""
        mock_get_brain.return_value = mock_brain

        with patch.object(sys.modules["agent.llm"], "_ALL_DOMAIN_KEYWORDS", set()), \
             patch.object(sys.modules["agent.llm"], "_demo_mode_active", return_value=False), \
             patch.object(sys.modules["agent.llm"], "update_working_memory"), \
             patch.object(sys.modules["agent.llm"], "_append_history"), \
             patch.object(sys.modules["agent.llm"], "_cache_set"), \
             patch.object(sys.modules["agent.llm"], "_remember_conversation_async"), \
             patch.object(sys.modules["agent.llm"], "_brain_run_iter", return_value=iter([("完整大脑", "完整大脑")])):
            results = list(brain_stream_sentences("测试文本", session_id="t"))
        assert mock_lite.call_count == 1


# ======================================================================
# FAST_PATHS 已精简：仅保留角色切换和社交，动作类已移除走 LLM
# ======================================================================

class TestFastPaths:
    def test_action_paths_removed(self):
        """time/weather/ac/scene/music/vision 等动作类快路径已移除。"""
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        for removed in ("time", "weather", "ac", "scene", "music",
                        "vision", "vision_camera"):
            assert removed not in names, f"{removed} should be removed from FAST_PATHS"

    def test_role_switch_and_social_kept(self):
        """角色切换和社交问候仍保留为快路径。"""
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "role_switch" in names
        assert "social" in names

    def test_role_switch_still_matches(self):
        """'切换到贾维斯' 仍命中 role_switch。"""
        from voice_agent import FAST_PATHS
        path = next(p for p in FAST_PATHS if p.name == "role_switch")
        assert path.match("切换到贾维斯")

    def test_social_still_matches(self):
        """'谢谢' 仍命中 social。"""
        from voice_agent import FAST_PATHS
        path = next(p for p in FAST_PATHS if p.name == "social")
        assert path.match("谢谢")


# ======================================================================
# eval_asr CER
# ======================================================================

class TestEvalAsrCer:
    def test_cer_identical(self):
        from scripts.eval_asr import _cer
        assert _cer("你好世界", "你好世界") == 0.0

    def test_cer_substitution(self):
        from scripts.eval_asr import _cer
        # "你好世界"→"你好大地"：2 个替换 / 4 字参考长度 = 0.5
        assert _cer("你好世界", "你好大地") == pytest.approx(1 / 2)

    def test_cer_empty_reference(self):
        from scripts.eval_asr import _cer
        assert _cer("", "abc") == 1.0
        assert _cer("", "") == 0.0

    def test_cer_insertion(self):
        from scripts.eval_asr import _cer
        assert _cer("abc", "abcd") == pytest.approx(1 / 3)
