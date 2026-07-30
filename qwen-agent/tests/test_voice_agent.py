"""
魔幻手机 - 语音Agent核心单元测试
使用mock测试: 缓存/对话历史/流式大脑生成/Markdown清理
"""
import os, json, tempfile
from unittest.mock import patch, MagicMock

import voice_agent


class TestCache:
    """测试响应缓存"""

    def setup_method(self):
        """每个测试前清空缓存"""
        voice_agent._cache.clear()

    def test_cache_miss(self):
        """未缓存的查询返回None"""
        assert voice_agent._cache_get("hello") is None

    def test_cache_set_get(self):
        """设置缓存后可获取"""
        voice_agent._cache_set("你好", "你好啊")
        assert voice_agent._cache_get("你好") == "你好啊"

    def test_cache_case_insensitive(self):
        """缓存大小写不敏感"""
        voice_agent._cache_set("Hello", "Hi")
        assert voice_agent._cache_get("hello") == "Hi"

    def test_cache_strips_whitespace(self):
        """缓存自动去除空白"""
        voice_agent._cache_set("  test  ", "result")
        assert voice_agent._cache_get("test") == "result"

    def test_cache_expiry(self):
        """缓存过期后返回None"""
        voice_agent._cache_set("temp", "reply")
        # 手动设置过期时间戳
        key = "temp"
        voice_agent._cache[key] = ("reply", 0)  # epoch=0, 必定过期
        assert voice_agent._cache_get("temp") is None

    def test_cache_max_size(self):
        """缓存达到上限时移除最旧的"""
        voice_agent._cache.clear()
        voice_agent._CACHE_MAX = 3  # 临时设小
        voice_agent._cache_set("a", "1")
        voice_agent._cache_set("b", "2")
        voice_agent._cache_set("c", "3")
        voice_agent._cache_set("d", "4")  # 应该移除"a"
        assert voice_agent._cache_get("a") is None
        assert voice_agent._cache_get("d") == "4"


class TestHistory:
    """测试对话历史管理"""

    def test_reset_history(self):
        """重置历史后为空"""
        voice_agent._history.append({"role": "user", "content": "test"})
        voice_agent.reset_history()
        assert len(voice_agent._history) == 0

    def test_history_persistence(self):
        """历史持久化到文件"""
        voice_agent.reset_history()
        voice_agent._history.append({"role": "user", "content": "测试持久化"})
        voice_agent._history.append({"role": "assistant", "content": "已保存"})
        voice_agent._save_history()
        # 重新加载
        voice_agent._history = []
        voice_agent._load_history()
        assert len(voice_agent._history) == 2
        assert voice_agent._history[0]["content"] == "测试持久化"
        voice_agent.reset_history()


class TestCleanForTTS:
    """测试Markdown清理(用于TTS)"""

    def test_removes_bold(self):
        """移除粗体标记"""
        result = voice_agent._clean_for_tts("**重要**内容")
        assert "**" not in result
        assert "重要" in result

    def test_removes_headers(self):
        """移除标题标记"""
        result = voice_agent._clean_for_tts("## 标题\n正文")
        assert "##" not in result
        assert "正文" in result

    def test_removes_table(self):
        """移除表格标记"""
        result = voice_agent._clean_for_tts("| 列1 | 列2 |\n|---|---|")
        assert "|" not in result

    def test_removes_blockquote(self):
        """移除引用标记"""
        result = voice_agent._clean_for_tts("> 引用内容")
        assert ">" not in result or result.strip() == "引用内容"

    def test_preserves_plain_text(self):
        """纯文本保持不变"""
        result = voice_agent._clean_for_tts("你好世界")
        assert result == "你好世界"


class TestBuildSystemMsg:
    """测试系统提示词构建"""

    def test_contains_time(self):
        """包含当前时间"""
        msg = voice_agent._build_system_msg()
        assert "当前时间" in msg

    def test_contains_role(self):
        """包含角色定义"""
        msg = voice_agent._build_system_msg()
        assert "魔幻手机" in msg
        assert "贾维斯" in msg

    def test_contains_tools(self):
        """包含工具列表"""
        msg = voice_agent._build_system_msg()
        assert "MCP" in msg or "地图" in msg


class TestBrainStreamSentences:
    """测试流式大脑句子切分(使用mock)"""

    def test_stream_with_mock_brain(self):
        """使用mock大脑测试流式句子切分"""
        mock_brain = MagicMock()
        # brain.run()返回的是消息列表(list of dicts), _extract_assistant_text从中提取assistant文本
        mock_brain.run = MagicMock(return_value=[
            [{"role": "user", "content": "你好"},
             {"role": "assistant", "content": "你好。我是魔幻手机，很高兴为你服务！"}]
        ])

        with patch.object(voice_agent, '_brain', mock_brain):
            voice_agent.reset_history()
            sentences = list(voice_agent.brain_stream_sentences("你好"))

        assert len(sentences) >= 1
        # 第一个元素是(sentence, full_reply)
        first_sentence, full_reply = sentences[0]
        assert isinstance(first_sentence, str)
        assert len(first_sentence) > 0
        assert "你好" in full_reply

    def test_stream_brain_not_built(self):
        """大脑未构建且无法构建时返回错误"""
        voice_agent.reset_history()
        with patch.object(voice_agent, '_brain', None):
            with patch.object(voice_agent, '_build_brain', side_effect=Exception("mock build failed")):
                sentences = list(voice_agent.brain_stream_sentences("test"))
                assert len(sentences) >= 1
                assert "失败" in sentences[0][0] or "未" in sentences[0][0]


class TestRetry:
    """测试重试逻辑"""

    def test_retry_succeeds_first_try(self):
        """第一次成功"""
        call_count = [0]
        def fn():
            call_count[0] += 1
            return "ok"
        result = voice_agent._retry(fn, "test")
        assert result == "ok"
        assert call_count[0] == 1

    def test_retry_succeeds_after_failures(self):
        """失败后重试成功"""
        call_count = [0]
        def fn():
            call_count[0] += 1
            if call_count[0] < 2:
                import requests
                raise requests.exceptions.Timeout("timeout")
            return "ok"
        # 用0延迟重试
        with patch.object(voice_agent, 'RETRY_BACKOFF', [0, 0, 0]):
            result = voice_agent._retry(fn, "test")
        assert result == "ok"
        assert call_count[0] == 2

    def test_retry_all_failures(self):
        """全部失败抛出异常"""
        import pytest as _pytest
        def fn():
            import requests
            raise requests.exceptions.Timeout("timeout")
        with patch.object(voice_agent, 'RETRY_BACKOFF', [0, 0, 0]):
            with _pytest.raises(Exception, match="test超时"):
                voice_agent._retry(fn, "test")


class TestMultiUserSessions:
    """多用户会话隔离测试"""

    def test_session_isolation(self):
        """不同session_id的对话历史相互隔离"""
        voice_agent.reset_history("default")
        voice_agent.reset_history("user2")
        
        # default session
        hist1 = voice_agent._get_history("default")
        hist1.append({"role": "user", "content": "hello from user1"})
        
        # user2 session
        hist2 = voice_agent._get_history("user2")
        hist2.append({"role": "user", "content": "hello from user2"})
        
        assert len(hist1) == 1
        assert len(hist2) == 1
        assert hist1[0]["content"] == "hello from user1"
        assert hist2[0]["content"] == "hello from user2"
        assert hist1 is not hist2  # 不同列表对象
        
        # 清理
        voice_agent.reset_history("default")
        voice_agent.reset_history("user2")

    def test_default_session_backward_compat(self):
        """default会话向后兼容(_history仍指向同一列表)"""
        voice_agent.reset_history()
        hist = voice_agent._get_history("default")
        hist.append({"role": "user", "content": "compat test"})
        # _history应该指向同一个列表
        assert len(voice_agent._history) == 1
        assert voice_agent._history[0]["content"] == "compat test"
        voice_agent.reset_history()

    def test_reset_specific_session(self):
        """重置特定会话不影响其他会话"""
        voice_agent.reset_history("default")
        voice_agent.reset_history("user3")
        
        hist1 = voice_agent._get_history("default")
        hist1.append({"role": "user", "content": "user1 msg"})
        
        hist2 = voice_agent._get_history("user3")
        hist2.append({"role": "user", "content": "user3 msg"})
        
        # 重置default不影响user3
        voice_agent.reset_history("default")
        assert len(voice_agent._get_history("default")) == 0
        assert len(voice_agent._get_history("user3")) == 1
        
        voice_agent.reset_history("user3")

    def test_max_sessions_limit(self):
        """超过最大会话数时自动清理"""
        # 创建MAX_SESSIONS+1个会话
        for i in range(voice_agent.MAX_SESSIONS + 2):
            voice_agent._get_history(f"test_session_{i}")
        # 应该不超过MAX_SESSIONS+1(default + MAX_SESSIONS-1 others)
        # (因为每次创建新的非default会话时, 如果超限会删一个旧的)
        assert len(voice_agent._sessions) <= voice_agent.MAX_SESSIONS + 1
        # 清理测试会话
        for k in list(voice_agent._sessions.keys()):
            if k.startswith("test_session_"):
                del voice_agent._sessions[k]
