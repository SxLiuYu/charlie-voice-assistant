"""Round 16: 新闻格式+晚安场景+env 文档化回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestNewsFormatVoice:
    def test_no_url_in_news_output(self):
        """新闻 MCP 工具输出不应包含裸 URL。"""
        # magic-info.py 文件名含连字符，用 importlib 加载
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "magic_info", os.path.join(PROJECT_DIR, "magic-info.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = inspect.getsource(mod)
        # get_news 函数源码中不应将 link 放入返回文本
        news_fn_idx = src.find("def get_news")
        assert news_fn_idx > 0
        news_fn_src = src[news_fn_idx:news_fn_idx+1500]
        # 不应在 return 语句中包含 {link}
        assert 'f"• {title}\\n  {link}"' not in news_fn_src, \
            "新闻输出不应包含裸 URL（语音会朗读字母）"
        assert "•" not in news_fn_src or "•" not in news_fn_src.split("return")[-1], \
            "新闻输出不应包含项目符号"

    def test_news_uses_semicolon_separator(self):
        """新闻输出应用分号分隔（TTS 朗读友好）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "magic_info", os.path.join(PROJECT_DIR, "magic-info.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = inspect.getsource(mod)
        news_fn_idx = src.find("def get_news")
        news_fn_src = src[news_fn_idx:news_fn_idx+1500]
        assert "；" in news_fn_src, \
            "新闻输出应用分号分隔（适合 TTS 朗读停顿）"


class TestGoodnightNoAutoExecute:
    def test_require_confirm_does_not_execute(self):
        """require_confirm=True 的场景不应自动执行设备操作。"""
        from app import schedulers
        src = inspect.getsource(schedulers)
        # 找到 require_confirm 处理块
        confirm_idx = src.find("require_confirm")
        assert confirm_idx > 0
        after_confirm = src[confirm_idx:confirm_idx+1000]
        # 应有 continue 跳过 execute_protocol
        assert "continue" in after_confirm, \
            "require_confirm=True 时应 continue 跳过自动执行"
        # continue 应在 execute_protocol 之前（确保不执行设备操作）
        cont_idx = after_confirm.find("continue")
        exec_idx = after_confirm.find("execute_protocol")
        assert cont_idx < exec_idx or exec_idx == -1, \
            "require_confirm=True 时 continue 应在 execute_protocol 之前"


class TestEnvExampleNewVars:
    def test_push_quiet_hours_documented(self):
        """PERSONALIZED_PUSH_QUIET_START/END 应在 .env.example 中文档化。"""
        env_path = os.path.join(PROJECT_DIR, ".env.example")
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
        assert "PERSONALIZED_PUSH_QUIET_START" in content, \
            ".env.example 应包含 PERSONALIZED_PUSH_QUIET_START"
        assert "PERSONALIZED_PUSH_QUIET_END" in content, \
            ".env.example 应包含 PERSONALIZED_PUSH_QUIET_END"
