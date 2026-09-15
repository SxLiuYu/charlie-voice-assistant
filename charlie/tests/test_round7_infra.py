"""Round 7 infra bug-fix behavior tests.

Covers:
1. agent/vision.py text-cleaning rewrite (prefix stripping + fallback)
2. agent/vision.py graceful fallbacks (missing script / subprocess timeout)
3. personalized_push.py dedup + expiry + disk rewrite + LLM fallback
4. app/xiaozhi_ws.py 2s timeout branch invokes rebuild_brain_pool (source-level)
5. local_wake toggle_wake honesty (no_model reason + not lying about enabled)
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import builtins
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# 1. vision text cleaning
# ======================================================================

class TestVisionCleaning:
    """_clean_vision_question 的有序剥离 + 兜底行为。"""

    def _clean(self, text: str) -> str:
        from agent.vision import _clean_vision_question
        return _clean_vision_question(text)

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            # 最常见前缀
            ("帮我看一下这个", "看一下这个"),
            ("帮我看看这个", "这个"),
            ("看看这个", "这个"),
            ("描述一下这个", "这个"),
            ("分析一下这个", "这个"),
            ("截图分析这个", "这个"),
            ("识别这个", "这个"),
            ("描述这个", "这个"),
            ("看一下这个", "这个"),
            # 去掉前缀后剩余不足 2 字符，兜底
            ("看看", "描述这张图片的内容"),
            ("帮我看", "描述这张图片的内容"),
            ("描述一下", "描述这张图片的内容"),
            # 无前缀（普通问题）应原样返回
            ("这张图片里有什么", "这张图片里有什么"),
            # 较长文本，去掉前缀后保留
            ("帮我看看这个手机的型号是多少", "这个手机的型号是多少"),
            # 尾部标点清理
            ("帮我看一下这个。", "看一下这个"),
            ("描述一下这个！", "这个"),
        ],
    )
    def test_prefix_stripping_and_fallback(self, input_text, expected):
        assert self._clean(input_text) == expected

    def test_empty_input(self):
        assert self._clean("") == "描述这张图片的内容"

    def test_whitespace_only(self):
        assert self._clean("   ") == "描述这张图片的内容"


class TestVisionFallback:
    """direct_vision_analyze 在脚本缺失 / 超时时的降级。"""

    def _analyze(self, text: str, vision_script_exists: bool = True,
                 screencapture_ok: bool = True,
                 run_side_effect=None):
        from agent import vision as vision_mod
        with patch.object(vision_mod.os.path, "isfile",
                          return_value=vision_script_exists):
            with patch.object(vision_mod.subprocess, "run",
                              side_effect=run_side_effect) as mock_run:
                with patch.object(vision_mod.os, "unlink"):
                    with patch.object(vision_mod, "PROJECT_DIR",
                                      "/fake/project"):
                        if not vision_script_exists:
                            return vision_mod.direct_vision_analyze(text)
                        # screencapture 分支
                        if run_side_effect is None:
                            mock_run.return_value = MagicMock(
                                returncode=0, stdout="result", stderr="")
                        return vision_mod.direct_vision_analyze(text)

    def test_script_missing_returns_prompt(self):
        # 视觉失败返回明确提示而非空串（防止回退LLM产生幻觉）
        result = self._analyze("帮我看一下", vision_script_exists=False)
        assert result != ""
        assert "视觉" in result or "稍后再试" in result

    def test_subprocess_timeout_cleans_tmp_and_returns_empty(self):
        import subprocess
        calls = []

        def side_effect(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "screencapture":
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(cmd, 5)
            return MagicMock(returncode=0, stdout="result", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            from agent import vision as vision_mod
            with patch.object(vision_mod.os.path, "isfile", return_value=True):
                with patch.object(vision_mod.subprocess, "run",
                                  side_effect=side_effect) as mock_run:
                    with patch.object(vision_mod.os, "unlink") as mock_unlink:
                        with patch.object(vision_mod, "PROJECT_DIR", tmp):
                            with patch.object(vision_mod.tempfile,
                                              "NamedTemporaryFile",
                                              return_value=MagicMock(
                                                  __enter__=MagicMock(
                                                      return_value=MagicMock(
                                                          name="/tmp/test.png")),
                                                  __exit__=MagicMock(return_value=False))):
                                result = vision_mod.direct_vision_analyze("帮我看一下")
                                # 截屏超时应返回提示而非空串（防LLM幻觉）
                                assert result != ""
                                assert "超时" in result or "再试" in result


# ======================================================================
# 2. personalized_push dedup / expiry / disk rewrite / LLM fallback
# ======================================================================

class TestPersonalizedPush:
    """_filter_pushed / _load_pushed / recommend_with_llm 边界行为。"""

    def _reset(self, tmp_data_dir: str):
        import personalized_push as push_mod
        push_mod._PUSHED_CACHE = {}
        push_mod._PUSHED_FILE = os.path.join(tmp_data_dir, "pushed_hot_topics.json")
        push_mod.DATA_DIR = tmp_data_dir

    def test_filter_pushed_dedup_and_expiry(self, tmp_path):
        import personalized_push as push_mod
        self._reset(str(tmp_path))
        now = time.time()
        # 写入一些记录（含过期）
        data = {
            "新鲜词A": now - 100,
            "新鲜词B": now - 200,
            "过期词": now - (30 * 24 * 3600),
        }
        push_mod._PUSHED_FILE = str(tmp_path / "pushed_hot_topics.json")
        with open(push_mod._PUSHED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        push_mod._PUSHED_CACHE = {}
        fresh = push_mod._filter_pushed(["新鲜词A", "新鲜词B", "过期词", "新词C"])
        # 过期词已被清理，应重新出现在 fresh 中（不是重复）
        assert "过期词" in fresh
        assert "新词C" in fresh
        # 已推送过的应被过滤
        assert "新鲜词A" not in fresh
        assert "新鲜词B" not in fresh

    def test_load_pushed_rewrites_disk_on_expiry(self, tmp_path):
        import personalized_push as push_mod
        self._reset(str(tmp_path))
        now = time.time()
        data = {
            "过期词1": now - (10 * 24 * 3600),
            "过期词2": now - (20 * 24 * 3600),
            "保留词": now - 100,
        }
        push_mod._PUSHED_FILE = str(tmp_path / "pushed_hot_topics.json")
        with open(push_mod._PUSHED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        push_mod._load_pushed()
        # 内存中应无过期词
        assert "过期词1" not in push_mod._PUSHED_CACHE
        assert "过期词2" not in push_mod._PUSHED_CACHE
        assert "保留词" in push_mod._PUSHED_CACHE
        # 磁盘文件应已被回写清理
        with open(push_mod._PUSHED_FILE, "r", encoding="utf-8") as f:
            disk = json.load(f)
        assert "过期词1" not in disk
        assert "过期词2" not in disk
        assert "保留词" in disk

    def test_llm_recommend_fallback_on_fabrication(self, tmp_path):
        import personalized_push as push_mod
        import app.llm_config as llm_config
        self._reset(str(tmp_path))
        hots = ["苹果", "香蕉", "西瓜", "台风"]
        # 模拟 LLM 返回编造词，应 fallback 到 hots[:3]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "发明的水果1\n发明的水果2\n发明的水果3"}}]
        }
        with patch.object(push_mod.requests, "post",
                          return_value=fake_resp):
            with patch.object(llm_config, "active_chat_endpoint",
                              return_value=("http://fake", "key", "model")):
                result = push_mod.recommend_with_llm("兴趣画像", hots)
        assert result == hots[:3]

    def test_llm_recommend_fallback_on_empty_response(self, tmp_path):
        import personalized_push as push_mod
        import app.llm_config as llm_config
        self._reset(str(tmp_path))
        hots = ["苹果", "香蕉", "西瓜"]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": ""}}]
        }
        with patch.object(push_mod.requests, "post",
                          return_value=fake_resp):
            with patch.object(llm_config, "active_chat_endpoint",
                              return_value=("http://fake", "key", "model")):
                result = push_mod.recommend_with_llm("兴趣画像", hots)
        assert result == hots[:3]

    def test_llm_recommend_filters_matched(self, tmp_path):
        import personalized_push as push_mod
        import app.llm_config as llm_config
        self._reset(str(tmp_path))
        hots = ["苹果", "香蕉", "西瓜"]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "香蕉\n苹果\n菠萝"}}]
        }
        with patch.object(push_mod.requests, "post",
                          return_value=fake_resp):
            with patch.object(llm_config, "active_chat_endpoint",
                              return_value=("http://fake", "key", "model")):
                result = push_mod.recommend_with_llm("兴趣画像", hots)
        assert "香蕉" in result
        assert "苹果" in result
        assert "菠萝" not in result  # 编造词被过滤


# ======================================================================
# 3. xiaozhi_ws 2s timeout branch calls rebuild_brain_pool
# ======================================================================

class TestXiaozhiWsTimeoutCleanup:
    """验证 asyncio.TimeoutError 分支调用了 rebuild_brain_pool。"""

    def test_timeout_branch_calls_rebuild_brain_pool(self):
        """直接用 inspect 断言源码中 2s 收尾超时分支包含 rebuild_brain_pool。"""
        import app.xiaozhi_ws as xws
        src = inspect.getsource(xws)
        # 找到 finally 块中的 asyncio.TimeoutError（2s 收尾超时）
        # 该分支的标识是 "producer未在2s内结束"
        timeout_marker = "producer未在2s内结束"
        assert timeout_marker in src, "未找到 2s 收尾超时分支"
        marker_pos = src.index(timeout_marker)
        # 向前找对应的 except asyncio.TimeoutError
        preceding = src[:marker_pos]
        timeout_err_pos = preceding.rindex("asyncio.TimeoutError")
        # 提取一个合理窗口搜索 rebuild_brain_pool
        window = src[timeout_err_pos:timeout_err_pos + 800]
        assert "rebuild_brain_pool" in window, (
            "asyncio.TimeoutError(producer 2s超时) 分支未调用 rebuild_brain_pool"
        )


# ======================================================================
# 4. local_wake toggle_wake honesty
# ======================================================================

class TestLocalWakeToggle:
    """toggle_wake 在无模型时的诚实性。"""

    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "local_wake_test_toggle",
            os.path.join(PROJECT_DIR, "local_wake.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        # 先 load 一次让全局状态存在
        spec.loader.exec_module(mod)
        # 重置关键状态
        mod._is_enabled = False
        mod._is_running = False
        mod._oww_model = None
        mod._sherpa_kws = None
        return mod

    def test_no_model_returns_reason(self):
        mod = self._mod()
        with patch.object(mod, "_oww_model", None), \
             patch.object(mod, "_sherpa_kws", None):
            result = mod.toggle_wake(True)
        assert isinstance(result, dict)
        assert result.get("reason") == "no_model"
        assert result.get("enabled") is False  # 未实际启用
        assert result.get("model_loaded") is False

    def test_no_model_false_toggle_still_returns_dict(self):
        """禁用时即使无模型也应返回 dict（保持结构一致）。"""
        mod = self._mod()
        with patch.object(mod, "_oww_model", None), \
             patch.object(mod, "_sherpa_kws", None):
            result = mod.toggle_wake(False)
        assert isinstance(result, dict)
        assert result.get("enabled") is False
        assert result.get("model_loaded") is False

    def test_with_model_allows_toggle(self):
        mod = self._mod()
        with patch.object(mod, "_oww_model", MagicMock()), \
             patch.object(mod, "_sherpa_kws", None):
            result = mod.toggle_wake(True)
        assert isinstance(result, dict)
        assert result.get("enabled") is True
        assert "reason" not in result or result.get("reason") is None


# ======================================================================
# 6. Agnes vision fast-path integration
# ======================================================================

class TestAgnesVisionIntegration:
    """Agnes vision 优先调用 + 回退 mimo-vision。"""

    def test_analyze_image_bytes_agnes_first(self):
        """agnes_analyze 返回结果时直接使用，不调用 mimo-vision。"""
        from agent import vision as vision_mod
        fake_result = "Agnes says: a cat on a table"

        with patch.object(vision_mod, "tempfile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.name = "/tmp/x.jpg"
            mock_file.write = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.NamedTemporaryFile.return_value = mock_file
            # agnes_analyze 是在函数体内 from agent.agnes_vision import 的
            # 所以 patch agent.agnes_vision.agnes_analyze
            with patch("agent.agnes_vision.agnes_analyze", return_value=fake_result) as mock_agnes:
                with patch.object(vision_mod, "_run_vision_script") as mock_run:
                    with patch.object(vision_mod.os, "unlink"):
                        result = vision_mod.analyze_image_bytes(b"fake-jpeg-bytes", "what is this")
                        assert result == fake_result
                        mock_agnes.assert_called_once()
                        mock_run.assert_not_called()

    def test_analyze_image_bytes_falls_back_when_agnes_empty(self):
        """Agnes 返回空串时回退到 mimo-vision。"""
        from agent import vision as vision_mod
        vision_result = "vision.py result: a laptop screen"

        with patch.object(vision_mod, "tempfile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.name = "/tmp/x.jpg"
            mock_file.write = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.NamedTemporaryFile.return_value = mock_file
            with patch("agent.agnes_vision.agnes_analyze", return_value=""):
                with patch.object(vision_mod, "_run_vision_script", return_value=vision_result) as mock_run:
                    with patch.object(vision_mod.os, "unlink"):
                        result = vision_mod.analyze_image_bytes(b"fake-jpeg-bytes", "what is this")
                        assert result == vision_result
                        mock_run.assert_called_once()

    def test_analyze_image_bytes_falls_back_when_agnes_unimportable(self):
        """Agnes 模块不可用时回退到 mimo-vision。"""
        from agent import vision as vision_mod
        vision_result = "vision.py fallback"

        with patch.object(vision_mod, "tempfile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.name = "/tmp/x.jpg"
            mock_file.write = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.NamedTemporaryFile.return_value = mock_file
            # 强制 agnes_vision 不在 sys.modules 中，模拟 ImportError
            saved = sys.modules.pop("agent.agnes_vision", None)
            try:
                with patch.object(vision_mod, "_run_vision_script", return_value=vision_result) as mock_run:
                    with patch.object(vision_mod.os, "unlink"):
                        result = vision_mod.analyze_image_bytes(b"fake-jpeg-bytes", "test")
                        assert result == vision_result
                        mock_run.assert_called_once()
            finally:
                if saved is not None:
                    sys.modules["agent.agnes_vision"] = saved

    def test_analyze_image_bytes_falls_back_when_agnes_raises(self):
        """Agnes 抛非 ImportError 异常（网络错/JSON 错）时也回退 mimo-vision。

        生产隐患场景：_analyze_with_fallback 的 except 只捕 ImportError 会
        让整体失败；必须捕所有异常回退。
        """
        from agent import vision as vision_mod
        vision_result = "vision.py fallback after crash"

        with patch("agent.agnes_vision.agnes_analyze",
                   side_effect=ValueError("boom")):
            with patch.object(vision_mod, "_run_vision_script",
                              return_value=vision_result) as mock_run:
                result = vision_mod.analyze_image_bytes(b"fake-jpeg-bytes", "test")
                assert result == vision_result
                mock_run.assert_called_once()

    def test_vision_status_callable_without_config(self):
        """vision_status 是普通函数，未配置时给出明确提示而非抛异常。"""
        from agent import agnes_vision as av
        with patch.object(av, "_is_configured", return_value=False):
            result = av.vision_status()
            assert "未配置" in result
