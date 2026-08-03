"""
Charlie - 语音Agent核心单元测试
使用mock测试: 缓存/对话历史/流式大脑生成/Markdown清理
"""
import os, sys, json, tempfile, time, types
from unittest.mock import patch, MagicMock

import pytest
import requests

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
        key = "text\x00temp"
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

    def test_save_history_does_not_hold_history_lock_during_file_write(self, monkeypatch):
        """落盘 I/O 不应阻塞历史读取和追加。"""
        voice_agent.reset_history()
        voice_agent._history.append({"role": "user", "content": "落盘不持锁"})
        original_open = open

        def assert_lock_released(path, *args, **kwargs):
            if str(path) == voice_agent.HISTORY_FILE:
                assert not voice_agent._history_lock.locked()
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", assert_lock_released)
        voice_agent._save_history()
        voice_agent.reset_history()

    def test_stale_history_save_does_not_replace_newer_snapshot(self, monkeypatch):
        """先开始的旧快照不能在新快照排队后覆盖文件。"""
        import threading

        voice_agent.reset_history()
        hist = voice_agent._history
        hist.append({"role": "user", "content": "旧快照"})
        initial_seq = voice_agent._history_save_seq
        first_dump_started = threading.Event()
        second_save_snapshotted = threading.Event()
        replace_calls = []
        original_dump = voice_agent.json.dump
        original_replace = voice_agent.os.replace

        def counting_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        def block_first_dump(obj, f, **kwargs):
            result = original_dump(obj, f, **kwargs)
            if not first_dump_started.is_set():
                first_dump_started.set()
                if not second_save_snapshotted.wait(1):
                    raise TimeoutError("second save did not snapshot")
                time.sleep(0.02)
            return result

        monkeypatch.setattr(voice_agent.json, "dump", block_first_dump)
        monkeypatch.setattr(voice_agent.os, "replace", counting_replace)

        old_thread = threading.Thread(target=voice_agent._save_history)
        old_thread.start()
        assert first_dump_started.wait(1)

        with voice_agent._history_lock:
            hist.append({"role": "assistant", "content": "新快照"})
        new_thread = threading.Thread(target=voice_agent._save_history)
        new_thread.start()

        deadline = time.time() + 1
        while time.time() < deadline and voice_agent._history_save_seq < initial_seq + 2:
            time.sleep(0.01)
        assert voice_agent._history_save_seq >= initial_seq + 2
        second_save_snapshotted.set()

        old_thread.join(1)
        new_thread.join(1)
        assert not old_thread.is_alive()
        assert not new_thread.is_alive()
        assert len(replace_calls) == 1

        voice_agent._history_file_signature = None
        voice_agent._history_file_cache = None
        voice_agent._load_history()
        assert any(message.get("content") == "新快照" for message in voice_agent._history)
        voice_agent.reset_history()

    def test_save_history_waits_for_shared_history_file_lock(self):
        """保存历史应等待 conversation_history.json.lock，和后台截断共用事务边界。"""
        import subprocess
        import sys

        voice_agent.reset_history()
        voice_agent._history.append({"role": "user", "content": "文件锁保存"})
        lock_file = voice_agent.HISTORY_FILE + ".lock"
        script = f"""
import fcntl, time
with open({lock_file!r}, "a+", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    time.sleep(0.5)
"""
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            time.sleep(0.1)
            started = time.monotonic()
            voice_agent._save_history()
            elapsed = time.monotonic() - started
        finally:
            proc.wait(timeout=5)

        assert elapsed >= 0.35
        assert os.path.exists(voice_agent.HISTORY_FILE)
        voice_agent.reset_history()

    def test_searchable_history_reads_file_without_holding_history_lock(self, monkeypatch):
        """搜索补齐外部历史时，文件读取不应阻塞内存历史的并发访问。"""
        import json

        session_id = "search_file_read_unlocked"
        voice_agent.reset_history(session_id)
        with open(voice_agent.HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({session_id: [{"role": "user", "content": "外部历史"}]}, f, ensure_ascii=False)
        voice_agent._history_file_signature = None
        voice_agent._history_file_cache = None
        original_open = open
        lock_state_when_opened = None

        def track_open(path, *args, **kwargs):
            if str(path) == voice_agent.HISTORY_FILE:
                nonlocal lock_state_when_opened
                lock_state_when_opened = voice_agent._history_lock.locked()
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", track_open)
        hist = voice_agent._searchable_history(session_id)

        assert lock_state_when_opened is False
        assert hist[0]["content"] == "外部历史"
        voice_agent.reset_history(session_id)

    def test_history_snapshot_is_stable_after_append(self):
        voice_agent.reset_history()
        hist = voice_agent._get_history("snapshot_session")
        hist.append({"role": "user", "content": "快照前"})

        snapshot = voice_agent._history_snapshot("snapshot_session")
        hist.append({"role": "assistant", "content": "快照后追加"})

        assert len(snapshot) == 1
        assert snapshot[0]["content"] == "快照前"
        voice_agent.reset_history("snapshot_session")

    def test_session_summaries_are_copied_under_lock(self):
        session_id = "summary_session"
        hist = voice_agent._get_history(session_id)
        hist.append({"role": "user", "content": "摘要消息"})

        summaries = voice_agent._session_summaries()
        voice_agent.reset_history(session_id)

        summary = next(item for item in summaries if item["session_id"] == session_id)
        assert summary["message_count"] == 1
        assert summary["last_message"] == "摘要消息"


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

    def test_clean_for_tts_reuses_compiled_patterns(self, monkeypatch):
        """TTS热路径复用预编译正则，避免每个片段重复编译。"""
        import re

        compiled_patterns = [
            value
            for name, value in vars(voice_agent).items()
            if name.startswith("_TTS_") and name.endswith("_RE") and isinstance(value, re.Pattern)
        ]
        assert len(compiled_patterns) >= 9

        def reject_re_sub(*args, **kwargs):
            raise AssertionError("voice_agent._clean_for_tts must reuse compiled Pattern.sub")

        monkeypatch.setattr(re, "sub", reject_re_sub)

        result = voice_agent._clean_for_tts(
            "## 标题 **粗体** |列| ```code``` `inline` [链接](http://x)  空格"
        )

        assert result == "标题 粗体 列 链接 空格"


class TestBuildSystemMsg:
    """测试系统提示词构建"""

    def test_contains_time(self):
        """包含当前时间"""
        msg = voice_agent._build_system_msg()
        assert "当前时间" in msg

    def test_contains_role(self):
        """包含角色定义"""
        msg = voice_agent._build_system_msg()
        assert "Charlie" in msg
        assert "私人AI助理" in msg

    def test_contains_tools(self):
        """包含工具列表"""
        msg = voice_agent._build_system_msg()
        assert "MCP" in msg or "地图" in msg

    def test_today_reminders_use_locked_reminder_loader(self, monkeypatch):
        """系统提示词复用提醒锁加载器，避免直接读文件绕过畸形过滤和共享锁。"""
        from app import reminders as app_reminders

        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        calls = {"load": 0, "open": 0}

        def fake_load_reminders():
            calls["load"] += 1
            return [
                {"text": "系统提示待办", "due": f"{today}T10:00:00", "done": False},
                {"text": "已完成待办", "due": f"{today}T11:00:00", "done": True},
            ]

        def reject_direct_open(path, *args, **kwargs):
            if str(path) == voice_agent.REMINDERS_FILE or str(path) == app_reminders.REMINDERS_FILE:
                calls["open"] += 1
                raise AssertionError("system prompt must load reminders through app.reminders")
            return original_open(path, *args, **kwargs)

        original_open = open
        monkeypatch.setattr(app_reminders, "_load_reminders", fake_load_reminders)
        monkeypatch.setattr("builtins.open", reject_direct_open)
        voice_agent.invalidate_system_msg_cache()

        msg = voice_agent._build_system_msg()

        assert calls == {"load": 1, "open": 0}
        assert "今日有1项待办" in msg


class TestBrainStreamSentences:
    """测试流式大脑句子切分(使用mock)"""

    def setup_method(self):
        voice_agent.reset_history()
        voice_agent._cache.clear()

    def test_stream_with_mock_brain(self):
        """使用mock大脑测试流式句子切分"""
        mock_brain = MagicMock()
        # brain.run()返回的是消息列表(list of dicts), _extract_assistant_text从中提取assistant文本
        mock_brain.run = MagicMock(return_value=[
            [{"role": "user", "content": "你好"},
             {"role": "assistant", "content": "你好。我是Charlie，很高兴为你服务！"}]
        ])

        with patch.object(voice_agent, '_classify_intent', return_value="none"), \
             patch.object(voice_agent, '_get_brain', return_value=mock_brain):
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
        with patch.object(voice_agent, '_classify_intent', return_value="none"), \
             patch.object(voice_agent, '_get_brain', side_effect=Exception("mock build failed")):
                sentences = list(voice_agent.brain_stream_sentences("test"))
                assert len(sentences) >= 1
                assert "失败" in sentences[0][0] or "未" in sentences[0][0]

    def test_interrupted_reply_is_added_to_prompt_but_not_history(self):
        mock_brain = MagicMock()
        mock_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "我会接着刚才被打断的内容说明。"}
        ]])

        with patch.object(voice_agent, '_classify_intent', return_value="none"), \
             patch.object(voice_agent, '_get_brain', return_value=mock_brain):
            list(voice_agent.brain_stream_sentences(
                "那明天呢？",
                interrupted_reply="我正准备说明明天的天气和出门建议。"
            ))

        messages = mock_brain.run.call_args.args[0]
        assert any(
            msg["role"] == "system" and "上一条助手回复被用户打断" in msg["content"]
            and "我正准备说明明天的天气和出门建议。" in msg["content"]
            for msg in messages
        )
        assert messages[-1] == {"role": "user", "content": "那明天呢？"}
        hist = voice_agent._get_history("default")
        assert len(hist) == 2
        assert not any("上一条助手回复被用户打断" in msg["content"] for msg in hist)

    def test_interrupted_reply_uses_separate_response_cache(self):
        replies = [
            [[{"role": "assistant", "content": "明天有雨，出门带伞。"}]],
            [[{"role": "assistant", "content": "明天日程是上午十点开会。"}]],
        ]
        mock_brain = MagicMock()
        mock_brain.run = MagicMock(side_effect=replies)

        with patch.object(voice_agent, '_classify_intent', return_value="none"), \
             patch.object(voice_agent, '_get_brain', return_value=mock_brain):
            first = list(voice_agent.brain_stream_sentences(
                "那明天呢？",
                interrupted_reply="我正准备说明明天的天气。",
            ))
            second = list(voice_agent.brain_stream_sentences(
                "那明天呢？",
                interrupted_reply="我正准备播报明天的日程。",
            ))

        assert first[-1][1] == "明天有雨，出门带伞。"
        assert second[-1][1] == "明天日程是上午十点开会。"
        assert mock_brain.run.call_count == 2


class TestOpenAICompat:
    """OpenAI SDK 与上游模型私有参数的兼容层。"""

    def test_unknown_create_kwarg_moves_into_extra_body(self):
        calls = []

        def fake_create(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TypeError("Completions.create() got an unexpected keyword argument 'enable_thinking'")
            return "ok"

        wrapped = voice_agent._wrap_openai_create_unknown_kwargs(fake_create)

        assert wrapped(model="deepseek", messages=[], enable_thinking=False) == "ok"
        assert calls[0].get("enable_thinking") is False
        assert "enable_thinking" not in calls[1]
        assert calls[1]["extra_body"] == {"enable_thinking": False}

    def test_unrelated_type_error_is_not_swallowed(self):
        def fake_create(*args, **kwargs):
            raise TypeError("something else broke")

        wrapped = voice_agent._wrap_openai_create_unknown_kwargs(fake_create)

        with pytest.raises(TypeError, match="something else broke"):
            wrapped(model="deepseek", messages=[])

    def test_build_brain_installs_compat_wrapper(self):
        class FakeCreate:
            def __init__(self):
                self.calls = []

            def __call__(self, *args, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    raise TypeError("Completions.create() got an unexpected keyword argument 'enable_thinking'")
                return []

        class FakeLlm:
            def __init__(self):
                self.create = FakeCreate()
                self._chat_complete_create = self.create

        class FakeMem:
            def __init__(self, llm):
                self.llm = llm

        class FakeAssistant:
            def __init__(self, **kwargs):
                self.llm = FakeLlm()
                self.mem = FakeMem(self.llm)

        class FakeMemory:
            percent = 50
            total = 17179869184
            available = 8589934592

        fake_psutil = type("FakePsutil", (), {"virtual_memory": staticmethod(lambda: FakeMemory())})

        fake_qwen_agent = types.ModuleType("qwen_agent")
        fake_qwen_agents = types.ModuleType("qwen_agent.agents")
        fake_qwen_agents.Assistant = FakeAssistant
        fake_modules = {
            "qwen_agent": fake_qwen_agent,
            "qwen_agent.agents": fake_qwen_agents,
        }

        with patch.dict(sys.modules, fake_modules), \
             patch.dict(sys.modules, {"psutil": fake_psutil}):
            brain = voice_agent._build_brain("none")

        assert brain.llm._chat_complete_create is not brain.llm.create
        assert brain.mem.llm._chat_complete_create is brain.llm._chat_complete_create
        assert brain.llm._chat_complete_create(model="deepseek", messages=[], enable_thinking=False) == []
        assert brain.llm.create.calls[1]["extra_body"] == {"enable_thinking": False}


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

    def test_retry_logs_exception_type_when_message_is_empty(self, caplog):
        """空字符串异常仍要记录异常类型，避免日志变成空错误。"""

        class EmptyRuntimeError(RuntimeError):
            def __str__(self):
                return ""

        def fn():
            raise EmptyRuntimeError()

        caplog.clear()
        with patch.object(voice_agent, "RETRY_BACKOFF", [0, 0, 0]), pytest.raises(
            Exception, match="test失败: EmptyRuntimeError"
        ):
            voice_agent._retry(fn, "test")

        assert "test第1次异常: EmptyRuntimeError" in caplog.text


class TestIntentClassification:
    """本地意图分类在 Ollama 不稳定时要快速降级并自动恢复。"""

    def setup_method(self):
        voice_agent._intent_failures = 0
        voice_agent._intent_disabled_until = 0.0

    def test_consecutive_local_intent_failures_trip_short_circuit(self):
        with patch.object(voice_agent, "INTENT_FAILURE_THRESHOLD", 2), \
             patch.object(voice_agent, "INTENT_FAILURE_COOLDOWN", 30), \
             patch.object(voice_agent.time, "time", return_value=100.0), \
             patch.object(voice_agent._session, "post", side_effect=requests.exceptions.Timeout("slow")) as mock_post:
            assert voice_agent._classify_intent("第一条") == "none"
            assert voice_agent._classify_intent("第二条") == "none"
            assert voice_agent._classify_intent("第三条") == "none"

        assert mock_post.call_count == 4
        assert voice_agent._intent_disabled_until == 130.0

    def test_successful_intent_classification_resets_failure_state(self):
        voice_agent._intent_failures = 1
        response = MagicMock()
        response.json.return_value = {"message": {"content": "amap-maps"}}

        with patch.object(voice_agent._session, "post", return_value=response):
            assert voice_agent._classify_intent("天气") == "amap-maps"

        assert voice_agent._intent_failures == 0


class TestVoiceLoop:
    """完整语音闭环边界。"""

    @pytest.mark.parametrize("text", ["嗯。", "啊啊啊", "Hmm.", "hmm", " ，。！ "])
    def test_low_intent_asr_short_circuits_brain_and_tts(self, text):
        """明确无任务的语气词只给本地确认，不写历史、不调用大脑或 TTS。"""
        with patch.object(voice_agent, "asr", return_value=text), \
             patch.object(voice_agent, "brain") as mock_brain, \
             patch.object(voice_agent, "tts") as mock_tts:
            recognized, reply, audio = voice_agent.voice_loop(b"fake-audio", "wav")

        assert voice_agent.is_low_intent_asr(text)
        assert recognized == text
        assert reply == voice_agent.LOW_INTENT_ASR_REPLY
        assert audio == b""
        mock_brain.assert_not_called()
        mock_tts.assert_not_called()

    @pytest.mark.parametrize("text", ["几点了", "讲个冷笑话", "今天天气怎么样", "对。", "好啊。", "home"])
    def test_short_real_questions_are_not_low_intent_asr(self, text):
        """短但有明确意图的问题必须继续进入大脑。"""
        assert not voice_agent.is_low_intent_asr(text)

    def test_runtime_audio_path_uses_configured_data_dir(self, tmp_path, monkeypatch):
        """命令行 demo 输出必须遵守数据目录隔离，不能固定写入 /tmp。"""
        monkeypatch.setattr(voice_agent, "DATA_DIR", str(tmp_path))

        assert voice_agent.runtime_audio_path("voice_reply.wav") == str(
            tmp_path / "voice_reply.wav"
        )

    def test_write_audio_file_keeps_existing_file_when_replace_fails(self, tmp_path, monkeypatch):
        """音频输出必须先写临时文件再替换，不能在落盘中断时截断旧文件。"""
        target = tmp_path / "voice_reply.wav"
        target.write_bytes(b"old audio")

        def fail_replace(src, dst):
            raise OSError("replace failed")

        monkeypatch.setattr(voice_agent.os, "replace", fail_replace)

        with pytest.raises(OSError):
            voice_agent.write_audio_file(str(target), b"new audio")

        assert target.read_bytes() == b"old audio"
        assert list(tmp_path.glob(".voice_reply*.tmp")) == []

    def test_empty_asr_short_circuits_brain_and_tts(self):
        """空 ASR 不进入大脑、不合成 TTS，避免污染历史和浪费调用。"""
        with patch.object(voice_agent, "asr", return_value=""), \
             patch.object(voice_agent, "brain") as mock_brain, \
             patch.object(voice_agent, "tts") as mock_tts:
            text, reply, audio = voice_agent.voice_loop(b"fake-audio", "wav")

        assert text == "(未识别到语音)"
        assert reply == "抱歉，我没听清，请再说一遍。"
        assert audio == b""
        mock_brain.assert_not_called()
        mock_tts.assert_not_called()

    def test_tts_unavailable_keeps_text_reply(self):
        """TTS 限流冷却时仍返回 ASR 文本和大脑回复，避免整轮对话失败。"""
        with patch.object(voice_agent, "asr", return_value="今天天气怎么样"), \
             patch.object(voice_agent, "brain", return_value="今天晴天。"), \
             patch.object(voice_agent, "tts", side_effect=voice_agent.TTSUnavailableError("TTSHTTP异常: 429")):
            text, reply, audio = voice_agent.voice_loop(b"fake-audio", "wav")

        assert text == "今天天气怎么样"
        assert reply == "今天晴天。"
        assert audio == b""


class TestTTSCache:
    """TTS 音频缓存只复用成功的短音频，并按音色/模型隔离。"""

    def setup_method(self):
        voice_agent._tts_cache.clear()
        voice_agent._tts_unavailable_until = 0.0

    def test_repeated_short_tts_calls_network_and_transcoder_once(self):
        from subprocess import CompletedProcess

        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=CompletedProcess([], 0, stdout=mp3)) as mock_ffmpeg, \
             patch.object(voice_agent, "_LOCAL_TTS_ENABLED", False):
            first = voice_agent.tts_to_mp3("提醒时间到了")
            second = voice_agent.tts_to_mp3(" 提醒时间到了 ")

        assert first == mp3
        assert second == mp3
        mock_tts.assert_called_once_with("提醒时间到了")
        mock_ffmpeg.assert_called_once()

    def test_public_tts_to_mp3_cleans_markdown_before_synthesis(self):
        from subprocess import CompletedProcess

        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=CompletedProcess([], 0, stdout=mp3)):
            result = voice_agent.tts_to_mp3("## 标题 **粗体**")

        assert result == mp3
        mock_tts.assert_called_once_with("标题 粗体")

    def test_tts_cache_path_does_not_reclean_already_cleaned_text(self, monkeypatch):
        from subprocess import CompletedProcess

        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        original_clean = voice_agent._clean_for_tts
        calls = []

        def counting_clean(text):
            calls.append(text)
            return original_clean(text)

        monkeypatch.setattr(voice_agent, "_clean_for_tts", counting_clean)
        with patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=CompletedProcess([], 0, stdout=mp3)) as mock_ffmpeg, \
             patch.object(voice_agent, "_LOCAL_TTS_ENABLED", False):
            voice_agent.tts_to_mp3("提醒时间到了")
            voice_agent.tts_to_mp3("提醒时间到了")

        assert calls == ["提醒时间到了", "提醒时间到了"]
        mock_tts.assert_called_once_with("提醒时间到了")
        mock_ffmpeg.assert_called_once()

    def test_failed_tts_is_not_cached(self):
        from subprocess import CompletedProcess

        mp3 = b"mp3-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "tts", side_effect=[
                    voice_agent.TTSUnavailableError("TTSHTTP异常: 429"),
                    b"wav-two" + b"x" * 120,
                ]) as mock_tts, \
             patch("subprocess.run", return_value=CompletedProcess([], 0, stdout=mp3)) as mock_ffmpeg:
            with pytest.raises(voice_agent.TTSUnavailableError):
                voice_agent.tts_to_mp3("重试这段")
            second = voice_agent.tts_to_mp3("重试这段")

        assert second.startswith(b"mp3-audio-bytes")
        assert mock_tts.call_count == 2
        mock_ffmpeg.assert_called_once()

    def test_long_tts_is_not_cached(self):
        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        long_text = "很" * (voice_agent.TTS_CACHE_MAX_CHARS + 1)
        with patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=MagicMock(stdout=mp3)):
            voice_agent.tts_to_mp3(long_text)
            voice_agent.tts_to_mp3(long_text)

        assert mock_tts.call_count == 2
        assert voice_agent._tts_cache == {}

    def test_cache_key_includes_voice_and_model(self):
        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=MagicMock(stdout=mp3)):
            voice_agent.tts_to_mp3("同一句话")
            with patch.object(voice_agent, "TTS_VOICE", "Serena"):
                voice_agent.tts_to_mp3("同一句话")
            with patch.object(voice_agent, "TTS_MODEL", "another-tts-model"):
                voice_agent.tts_to_mp3("同一句话")

        assert mock_tts.call_count == 3

    def test_cache_respects_ttl(self):
        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "TTS_CACHE_TTL", 0.01), \
             patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=MagicMock(stdout=mp3)):
            voice_agent.tts_to_mp3("第一句")
            assert voice_agent._tts_cache_get("第一句") == mp3
            time.sleep(0.02)
            assert voice_agent._tts_cache_get("第一句") is None
            voice_agent.tts_to_mp3("第一句")

        assert mock_tts.call_count == 2

    def test_cache_respects_max_size(self):
        wav = b"wav-audio-bytes" + b"x" * 120
        mp3 = b"mp3-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "TTS_CACHE_MAX", 2), \
             patch.object(voice_agent, "tts", return_value=wav) as mock_tts, \
             patch("subprocess.run", return_value=MagicMock(stdout=mp3)):
            voice_agent.tts_to_mp3("第一句")
            voice_agent.tts_to_mp3("第二句")
            voice_agent.tts_to_mp3("第三句")
            voice_agent.tts_to_mp3("第三句")

        assert mock_tts.call_count == 3
        assert voice_agent._tts_cache_get("第一句") is None
        assert voice_agent._tts_cache_get("第三句", "Cherry", voice_agent.TTS_MODEL) == mp3


class TestTTSFailureCooldown:
    """TTS 最终失败后短时间冷却，避免在限流窗口继续打上游。"""

    def setup_method(self):
        voice_agent._tts_cache.clear()
        voice_agent._tts_unavailable_until = 0.0

    def test_final_failure_sets_short_circuit_cooldown(self):
        with patch.object(voice_agent, "TTS_FAILURE_COOLDOWN", 0.02), \
             patch.object(voice_agent, "_retry", side_effect=Exception("TTSHTTP异常: 429")) as mock_retry:
            for _ in range(2):
                with pytest.raises(voice_agent.TTSUnavailableError):
                    voice_agent.tts("第一句")
                with pytest.raises(voice_agent.TTSUnavailableError):
                    voice_agent.tts("第二句")

        mock_retry.assert_called_once()

    def test_success_resets_cooldown(self):
        wav = b"wav-audio-bytes" + b"x" * 120
        with patch.object(voice_agent, "TTS_FAILURE_COOLDOWN", 0.02), \
             patch.object(voice_agent, "_retry", side_effect=[Exception("TTSHTTP异常: 429"), wav]) as mock_retry:
            with pytest.raises(voice_agent.TTSUnavailableError):
                voice_agent.tts("失败")
            time.sleep(0.03)
            assert voice_agent.tts("恢复") == wav

        assert mock_retry.call_count == 2
        assert voice_agent._tts_unavailable_until == 0.0


class TestContextSummarization:
    """对话上下文摘要测试"""

    def setup_method(self):
        voice_agent._context_summaries.clear()

    def test_summary_stored_on_trim(self):
        """截断后存储上下文摘要"""
        # 创建超长历史触发截断
        hist = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"测试消息{i}" + "x" * 80} for i in range(30)]
        voice_agent._trim_history_tokens(hist, max_tokens=200, session_id="test_summary")
        # 摘要应该被存储
        assert "test_summary" in voice_agent._context_summaries
        assert len(voice_agent._context_summaries["test_summary"]) > 0

    def test_summary_preserves_topics(self):
        """摘要保留了对话话题"""
        hist = [
            {"role": "user", "content": "帮我查天气"},
            {"role": "assistant", "content": "今天晴天"},
            {"role": "user", "content": "帮我叫车"},
            {"role": "assistant", "content": "已叫车"},
            {"role": "user", "content": "帮我订餐"},
            {"role": "assistant", "content": "已订餐"},
            {"role": "user", "content": "最新消息"},
            {"role": "assistant", "content": "回复"},
        ]
        voice_agent._trim_history_tokens(hist, max_tokens=50, session_id="test_topics")
        summary = voice_agent._context_summaries.get("test_topics", "")
        # 摘要应该包含一些被移除的话题关键词
        assert len(summary) > 0

    def test_no_summary_on_short_history(self):
        """短历史不触发摘要"""
        hist = [{"role": "user", "content": "hi"}]
        voice_agent._trim_history_tokens(hist, max_tokens=4000, session_id="test_short")
        assert "test_short" not in voice_agent._context_summaries

    def test_summary_max_length(self):
        """摘要不超过最大长度"""
        # 创建大量历史
        hist = [{"role": "user", "content": f"话题{i}" + "y" * 100} for i in range(50)]
        voice_agent._trim_history_tokens(hist, max_tokens=100, session_id="test_maxlen")
        summary = voice_agent._context_summaries.get("test_maxlen", "")
        assert len(summary) <= voice_agent.MAX_SUMMARY_LEN

    def test_summary_accumulates(self):
        """多次截断摘要累积"""
        sid = "test_accumulate"
        # 第一次截断
        hist1 = [{"role": "user", "content": "话题A" + "z" * 100}] * 10
        voice_agent._trim_history_tokens(hist1, max_tokens=100, session_id=sid)
        first_summary = voice_agent._context_summaries.get(sid, "")
        # 第二次截断
        hist2 = [{"role": "user", "content": "话题B" + "z" * 100}] * 10
        voice_agent._trim_history_tokens(hist2, max_tokens=100, session_id=sid)
        second_summary = voice_agent._context_summaries.get(sid, "")
        # 第二次应该包含之前的内容
        assert len(second_summary) >= len(first_summary)

    def test_summary_in_system_prompt(self):
        """摘要出现在系统提示词中"""
        voice_agent._context_summaries["default"] = "之前聊过天气和订餐"
        voice_agent.invalidate_system_msg_cache()
        msg = voice_agent._build_system_msg()
        assert "天气" in msg or "订餐" in msg
        voice_agent._context_summaries.clear()

class TestPreferences:
    """用户偏好系统测试"""

    class _NoCopyDict(dict):
        def copy(self):
            raise AssertionError("preference_count must not copy preferences")

        def __iter__(self):
            raise AssertionError("preference_count must not iterate preferences")

        def items(self):
            raise AssertionError("preference_count must not iterate preferences")

        def keys(self):
            raise AssertionError("preference_count must not iterate preferences")

        def values(self):
            raise AssertionError("preference_count must not iterate preferences")

    def setup_method(self):
        """每个测试前清空偏好"""
        voice_agent._preferences.clear()

    def teardown_method(self):
        """每个测试后恢复偏好状态，避免污染其他用例。"""
        voice_agent._preferences.clear()
        voice_agent._preferences_revision = 0
        if hasattr(voice_agent, "_preferences_save_seq"):
            voice_agent._preferences_save_seq = 0

    def test_set_and_get_preference(self):
        """设置和获取偏好"""
        voice_agent.set_preference("喜欢的食物", "意大利菜")
        assert voice_agent.get_preference("喜欢的食物") == "意大利菜"

    def test_list_preferences(self):
        """列出所有偏好"""
        voice_agent.set_preference("key1", "val1")
        voice_agent.set_preference("key2", "val2")
        prefs = voice_agent.list_preferences()
        assert "key1" in prefs
        assert "key2" in prefs
        assert prefs["key1"] == "val1"

    def test_preference_count_without_copying(self):
        """只统计偏好数量，不复制或遍历整份偏好"""
        original = voice_agent._preferences
        voice_agent._preferences = self._NoCopyDict()
        try:
            assert voice_agent.preference_count() == 0
            voice_agent._preferences["key1"] = "val1"
            voice_agent._preferences["key2"] = "val2"
            assert voice_agent.preference_count() == 2
        finally:
            voice_agent._preferences = original

    def test_del_preference(self):
        """删除偏好"""
        voice_agent.set_preference("temp", "val")
        result = voice_agent.del_preference("temp")
        assert "已忘记" in result
        assert voice_agent.get_preference("temp") == ""

    def test_del_nonexistent(self):
        """删除不存在的偏好"""
        result = voice_agent.del_preference("nonexistent")
        assert "未找到" in result

    def test_preferences_in_system_prompt(self):
        """偏好出现在系统提示词中"""
        voice_agent.set_preference("下班时间", "18:00")
        voice_agent.invalidate_system_msg_cache()
        msg = voice_agent._build_system_msg()
        assert "18:00" in msg
        assert "用户偏好" in msg or "下班时间" in msg

    def test_no_preferences_no_crash(self):
        """无偏好时不崩溃"""
        voice_agent._preferences.clear()
        msg = voice_agent._build_system_msg()
        assert "Charlie" in msg  # 系统提示词仍然正常

    def test_save_preferences_does_not_hold_prefs_lock_during_file_write(self, monkeypatch):
        """偏好落盘 I/O 不应阻塞内存偏好的并发访问。"""
        import threading

        voice_agent._preferences["落盘锁探针"] = "value"
        write_started = threading.Event()
        release_probe = threading.Event()
        original_dump = voice_agent.json.dump

        def block_during_dump(obj, f, **kwargs):
            write_started.set()
            release_probe.wait(1)
            return original_dump(obj, f, **kwargs)

        monkeypatch.setattr(voice_agent.json, "dump", block_during_dump)
        save_thread = threading.Thread(target=voice_agent._save_preferences)
        save_thread.start()
        assert write_started.wait(1)

        lock_available = voice_agent._prefs_lock.acquire(timeout=0.2)
        if lock_available:
            voice_agent._prefs_lock.release()

        release_probe.set()
        save_thread.join(1)
        assert not save_thread.is_alive()
        assert lock_available

    def test_stale_preferences_save_does_not_replace_newer_snapshot(self, monkeypatch):
        """先开始的旧偏好快照不能在新快照排队后覆盖文件。"""
        import threading

        voice_agent._preferences["旧偏好"] = "旧值"
        initial_seq = getattr(voice_agent, "_preferences_save_seq", 0)
        first_dump_started = threading.Event()
        second_save_snapshotted = threading.Event()
        replace_calls = []
        original_dump = voice_agent.json.dump
        original_replace = voice_agent.os.replace

        def counting_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        def block_first_dump(obj, f, **kwargs):
            if not first_dump_started.is_set():
                first_dump_started.set()
                if not second_save_snapshotted.wait(1):
                    raise TimeoutError("second preferences save did not snapshot")
                time.sleep(0.02)
            result = original_dump(obj, f, **kwargs)
            return result

        monkeypatch.setattr(voice_agent.json, "dump", block_first_dump)
        monkeypatch.setattr(voice_agent.os, "replace", counting_replace)

        old_thread = threading.Thread(target=voice_agent._save_preferences)
        old_thread.start()
        assert first_dump_started.wait(1)

        with voice_agent._prefs_lock:
            voice_agent._preferences["新偏好"] = "新值"
        new_thread = threading.Thread(target=voice_agent._save_preferences)
        new_thread.start()

        deadline = time.time() + 1
        while time.time() < deadline and getattr(voice_agent, "_preferences_save_seq", 0) < initial_seq + 2:
            time.sleep(0.01)
        assert getattr(voice_agent, "_preferences_save_seq", 0) >= initial_seq + 2
        second_save_snapshotted.set()

        old_thread.join(1)
        new_thread.join(1)
        assert not old_thread.is_alive()
        assert not new_thread.is_alive()
        assert len(replace_calls) == 1

        with open(voice_agent.PREFS_FILE, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        assert persisted.get("新偏好") == "新值"

    def test_refresh_preferences_reloads_external_changes_and_bumps_etag(self, tmp_path, monkeypatch):
        """主进程必须检测其他进程写入的 preferences.json，并刷新内存快照和 ETag。"""
        prefs_file = tmp_path / "preferences.json"
        lock_file = tmp_path / "preferences.json.lock"
        prefs_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(voice_agent, "PREFS_FILE", str(prefs_file))
        monkeypatch.setattr(voice_agent, "PREFS_LOCK_FILE", str(lock_file), raising=False)
        voice_agent._preferences.clear()
        voice_agent._preferences_revision = 0
        if hasattr(voice_agent, "_preferences_save_seq"):
            voice_agent._preferences_save_seq = 0
        if hasattr(voice_agent, "_preferences_file_signature"):
            voice_agent._preferences_file_signature = None
        voice_agent._load_preferences()

        assert voice_agent.list_preferences() == {}
        prefs_file.write_text(
            json.dumps({"external_pref": "external_value"}, ensure_ascii=False),
            encoding="utf-8",
        )

        changed = voice_agent._refresh_preferences_if_changed()

        assert changed is True
        assert voice_agent.get_preference("external_pref") == "external_value"
        assert voice_agent.preferences_etag_token() != "preferences:0:0"

        changed_again = voice_agent._refresh_preferences_if_changed()
        assert changed_again is False

class TestTimestamps:
    """测试对话时间戳"""

    def test_history_has_timestamp(self):
        """brain()添加的消息有时间戳"""
        from unittest.mock import patch, MagicMock
        mock_brain = MagicMock()
        mock_brain.run = MagicMock(return_value=[
            [{"role": "user", "content": "你好"},
             {"role": "assistant", "content": "你好！"}]
        ])
        voice_agent.reset_history()
        voice_agent._cache.clear()  # 清除缓存, 确保走真实brain路径
        with patch.object(voice_agent, '_classify_intent', return_value="none"), \
             patch.object(voice_agent, '_get_brain', return_value=mock_brain):
            voice_agent.brain("你好")
        hist = voice_agent._get_history("default")
        assert len(hist) >= 2
        for m in hist:
            assert "ts" in m  # 每条消息都有时间戳
            assert m["ts"]  # 时间戳非空
        voice_agent.reset_history()

    def test_timestamp_stripped_from_brain_input(self):
        """时间戳不传递给大脑"""
        voice_agent.reset_history()
        # 手动添加带时间戳的历史
        voice_agent._get_history("default").append(
            {"role": "user", "content": "old msg", "ts": "2026-01-01T00:00:00"})
        
        mock_brain = MagicMock()
        mock_brain.run = MagicMock(return_value=[
            [{"role": "user", "content": "old msg"},
             {"role": "user", "content": "test"},
             {"role": "assistant", "content": "reply"}]
        ])
        with patch.object(voice_agent, '_classify_intent', return_value="none"), \
             patch.object(voice_agent, '_get_brain', return_value=mock_brain):
            voice_agent.brain("test")
        
        # 验证brain.run收到的消息没有ts字段
        called_messages = mock_brain.run.call_args[0][0]
        for m in called_messages:
            assert "ts" not in m  # ts不应该传递给大脑
        voice_agent.reset_history()


class TestApiKeyFailover:
    """GLM密钥故障转移测试"""

    def test_single_key(self):
        """单密钥场景"""
        voice_agent._glm_keys = ["test-key-1"]
        voice_agent._glm_key_idx = 0
        assert voice_agent._get_glm_key() == "test-key-1"

    def test_rotate_single_key_fails(self):
        """单密钥无法轮换"""
        voice_agent._glm_keys = ["test-key-1"]
        voice_agent._glm_key_idx = 0
        result = voice_agent._rotate_glm_key()
        assert result is False

    def test_rotate_multiple_keys(self):
        """多密钥可轮换"""
        voice_agent._glm_keys = ["key-a", "key-b", "key-c"]
        voice_agent._glm_key_idx = 0
        assert voice_agent._get_glm_key() == "key-a"
        assert voice_agent._rotate_glm_key() is True
        assert voice_agent._get_glm_key() == "key-b"
        assert voice_agent._rotate_glm_key() is True
        assert voice_agent._get_glm_key() == "key-c"
        # 循环回到第一个
        assert voice_agent._rotate_glm_key() is True
        assert voice_agent._get_glm_key() == "key-a"

    def test_rotate_wraps_around(self):
        """密钥轮换循环"""
        voice_agent._glm_keys = ["key-1", "key-2"]
        voice_agent._glm_key_idx = 0
        voice_agent._rotate_glm_key()  # -> 1
        assert voice_agent._glm_key_idx == 1
        voice_agent._rotate_glm_key()  # -> 0 (wrap)
        assert voice_agent._glm_key_idx == 0


class TestContextManagement:
    """测试对话上下文管理(token感知截断)"""

    def test_estimate_tokens_reuses_compiled_patterns_once(self, monkeypatch):
        import re

        cn_pattern = getattr(voice_agent, "_TOKEN_CHINESE_RE", None)
        en_pattern = getattr(voice_agent, "_TOKEN_ENGLISH_RE", None)
        assert isinstance(cn_pattern, re.Pattern)
        assert isinstance(en_pattern, re.Pattern)

        calls = []

        class TrackedPattern:
            def __init__(self, pattern, label):
                self.pattern = pattern
                self.label = label

            def findall(self, text):
                calls.append(self.label)
                return self.pattern.findall(text)

        monkeypatch.setattr(voice_agent, "_TOKEN_CHINESE_RE", TrackedPattern(cn_pattern, "cn"))
        monkeypatch.setattr(voice_agent, "_TOKEN_ENGLISH_RE", TrackedPattern(en_pattern, "en"))

        def reject_re_findall(*args, **kwargs):
            raise AssertionError("token estimation must reuse compiled Pattern.findall")

        monkeypatch.setattr(re, "findall", reject_re_findall)

        assert voice_agent._estimate_tokens("你好 hello world!") == 7
        assert calls == ["cn", "en"]

    def test_estimate_tokens_chinese(self):
        """中文token估算"""
        tokens = voice_agent._estimate_tokens("你好世界")
        assert tokens > 0
        assert 5 <= tokens <= 7  # 4 Chinese chars * ~1.5

    def test_estimate_tokens_english(self):
        """英文token估算"""
        tokens = voice_agent._estimate_tokens("hello world")
        assert tokens > 0
        assert 2 <= tokens <= 4  # 2 words + space

    def test_estimate_tokens_empty(self):
        """空文本token为0"""
        assert voice_agent._estimate_tokens("") == 0
        assert voice_agent._estimate_tokens(None) == 0

    def test_estimate_msg_tokens(self):
        """消息token估算(含role开销)"""
        msg = {"role": "user", "content": "你好"}
        tokens = voice_agent._estimate_msg_tokens(msg)
        assert tokens > 4  # content tokens + 4 for role

    def test_trim_short_history(self):
        """短历史不截断"""
        hist = [{"role": "user", "content": "hi"}]
        voice_agent._trim_history_tokens(hist, 4000)
        assert len(hist) == 1

    def test_trim_long_history(self):
        """长历史截断到token预算内"""
        # 创建超长历史(每条100字, 50条)
        hist = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": "测" * 100} for i in range(50)]
        original_len = len(hist)
        voice_agent._trim_history_tokens(hist, 400)
        assert len(hist) < original_len  # 应该被截断
        assert len(hist) >= 4  # 至少保留4条(2轮)

    def test_trim_preserves_recent(self):
        """截断后保留最近的消息"""
        hist = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"消息{i}" + "测" * 50} for i in range(20)]
        voice_agent._trim_history_tokens(hist, 200)
        # 保留的最后一条应该是原来的最后一条
        assert "消息19" in hist[-1]["content"]

    def test_trim_estimates_each_message_once(self):
        """长历史截断复用首轮 token 估算，不对删除消息重复扫描。"""
        hist = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "主题" + str(i) + "测" * 10}
            for i in range(8)
        ]
        message_count = len(hist)
        original = voice_agent._estimate_msg_tokens
        seen = []

        def counting_estimate(msg):
            seen.append(msg["content"])
            return original(msg)

        with patch.object(voice_agent, "_estimate_msg_tokens", side_effect=counting_estimate):
            voice_agent._trim_history_tokens(hist, 100)

        assert len(seen) == message_count
        assert 4 <= len(hist) < 8
        assert "主题7" in hist[-1]["content"]

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
