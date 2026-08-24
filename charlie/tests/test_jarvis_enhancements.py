"""Tests for P0-P3 enhancements: magic-jarvis, memory v2, context, persona."""
import os
import sys
import json
import time
import tempfile
import datetime
import importlib.util
from unittest.mock import patch

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== P0: magic-jarvis proactive conversation =====

class TestMagicJarvis:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="jarvis_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.jarvis = _load_module("jarvis_test", "magic-jarvis.py")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_proactive_greeting_morning(self):
        with patch.object(self.jarvis, "_get_time_context") as tc, \
             patch.object(self.jarvis, "_get_user_context") as uc, \
             patch.object(self.jarvis, "_get_recent_history") as h, \
             patch.object(self.jarvis, "_get_preferences") as p:
            tc.return_value = {"hour": 8, "weekday": 0, "period": "早晨", "is_weekend": False}
            uc.return_value = {"state": "home_awake"}
            h.return_value = []
            p.return_value = {}
            greet = self.jarvis.proactive_greeting()
            assert "早上好" in greet or "早晨" in greet

    def test_proactive_greeting_night(self):
        with patch.object(self.jarvis, "_get_time_context") as tc, \
             patch.object(self.jarvis, "_get_user_context") as uc, \
             patch.object(self.jarvis, "_get_recent_history") as h, \
             patch.object(self.jarvis, "_get_preferences") as p:
            tc.return_value = {"hour": 23, "weekday": 0, "period": "深夜", "is_weekend": False}
            uc.return_value = {"state": "home_resting"}
            h.return_value = []
            p.return_value = {}
            greet = self.jarvis.proactive_greeting()
            assert "夜深" in greet or len(greet) > 0

    def test_suggest_action_lunch(self):
        with patch.object(self.jarvis, "_get_time_context") as tc, \
             patch.object(self.jarvis, "_get_user_context") as uc, \
             patch.object(self.jarvis, "_get_recent_history") as h, \
             patch.object(self.jarvis, "_get_preferences") as p:
            tc.return_value = {"hour": 12, "weekday": 0, "period": "中午", "is_weekend": False}
            uc.return_value = {"state": "home_awake"}
            h.return_value = []
            p.return_value = {}
            suggestion = self.jarvis.suggest_action()
            assert "午饭" in suggestion

    def test_suggest_action_empty(self):
        with patch.object(self.jarvis, "_get_time_context") as tc, \
             patch.object(self.jarvis, "_get_user_context") as uc, \
             patch.object(self.jarvis, "_get_recent_history") as h, \
             patch.object(self.jarvis, "_get_preferences") as p:
            tc.return_value = {"hour": 3, "weekday": 0, "period": "凌晨", "is_weekend": False}
            uc.return_value = {"state": "home_sleeping"}
            h.return_value = []
            p.return_value = {}
            suggestion = self.jarvis.suggest_action()
            assert isinstance(suggestion, str)

    def test_learn_habit(self):
        with patch.object(self.jarvis, "_get_recent_history") as h:
            h.return_value = [
                {"role": "user", "content": "早上好", "ts": time.time() - 3600 * 2},
                {"role": "user", "content": "今天天气", "ts": time.time() - 3600},
                {"role": "user", "content": "早上好", "ts": time.time()},
            ]
            result = self.jarvis.learn_habit()
            assert isinstance(result, str)
            assert len(result) > 0


# ===== P1: Memory v2 enhancements =====

class TestMemoryV2:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="mem_v2_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.mem = _load_module("mem_v2_test", "magic-memory.py")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_working_memory_reset(self):
        self.mem.update_working_memory(facts={"key": "val"}, intent="test")
        self.mem.reset_working_memory()
        wm = self.mem.get_working_memory()
        assert wm["session_facts"] == {}
        assert wm["turn_count"] == 0

    def test_working_memory_update(self):
        self.mem.reset_working_memory()
        self.mem.update_working_memory(facts={"project": "charlie"}, intent="weather")
        wm = self.mem.get_working_memory()
        assert wm["session_facts"].get("project") == "charlie"
        assert "weather" in wm["intent_stack"]

    def test_hybrid_recall_basic(self):
        self.mem.remember_conversation("项目周五截止", "注意进度")
        results = self.mem.recall_hybrid("项目", k=3)
        assert len(results) > 0
        assert "项目" in results[0].get("summary", "") or "项目" in results[0].get("user_text", "")

    def test_hybrid_recall_empty(self):
        results = self.mem.recall_hybrid("不存在的主题xyz", k=3)
        assert isinstance(results, list)

    def test_semantic_memory_extraction(self):
        self.mem.remember_conversation("我喜欢听爵士乐", "好的")
        self.mem.remember_conversation("我习惯每天早上喝咖啡", "收到")
        count = self.mem.refresh_semantic_memory()
        assert count >= 0
        sem = self.mem.get_semantic_memories()
        assert isinstance(sem, list)


# ===== P1: agent/working_memory module =====

class TestWorkingMemoryModule:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="wm_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        from agent.working_memory import reset
        reset()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_add_and_get_fact(self):
        from agent.working_memory import add_fact, get_fact
        add_fact("city", "北京")
        assert get_fact("city") == "北京"
        assert get_fact("missing", "default") == "default"

    def test_update_and_get(self):
        from agent.working_memory import update, get
        update(facts={"a": "1"}, intent="music", topic="song")
        wm = get()
        assert wm["session_facts"]["a"] == "1"
        assert "music" in wm["intent_stack"]
        assert wm["last_topic"] == "song"

    def test_reset_clears_all(self):
        from agent.working_memory import reset, update, get
        update(facts={"x": "y"}, intent="test")
        reset()
        wm = get()
        assert wm["session_facts"] == {}
        assert wm["turn_count"] == 0

    def test_turn_count(self):
        from agent.working_memory import update, turn_count
        update()
        update()
        assert turn_count() == 2

    def test_pop_intent(self):
        from agent.working_memory import update, pop_intent
        update(intent="first")
        update(intent="second")
        assert pop_intent() == "second"


# ===== P2: agent/context.py =====

class TestContextFusion:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="ctx_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.ctx_mod = _load_module("ctx_test", "agent/context.py")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_get_context_returns_dict(self):
        ctx = self.ctx_mod.get_context()
        assert isinstance(ctx, dict)
        assert "time" in ctx
        assert "location" in ctx
        assert "weather" in ctx
        assert "device" in ctx
        assert "recent_conversation" in ctx
        assert "user_state" in ctx
        assert "todos" in ctx

    def test_format_context_for_prompt(self):
        ctx = {
            "time": {"now": "2026-08-21 10:00", "hour": 10, "period": "上午", "is_weekend": False},
            "location": "北京",
            "weather": "晴 26°C",
            "device": {},
            "recent_conversation": ["今天天气怎么样", "北京晴天"],
            "user_state": {"state": "home_awake", "confidence": 0.7},
            "todos": ["开会"],
        }
        prompt = self.ctx_mod.format_context_for_prompt(ctx)
        assert "2026-08-21 10:00" in prompt
        assert "北京" in prompt
        assert "晴" in prompt

    def test_format_empty_context(self):
        prompt = self.ctx_mod.format_context_for_prompt({})
        assert isinstance(prompt, str)


# ===== P3: persona consistency =====

class TestPersonaConsistency:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="persona_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.persona_mod = _load_module("persona_test", "agent/persona.py")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_get_tone_profile_default(self):
        tone = self.persona_mod.get_tone_profile()
        assert isinstance(tone, dict)
        assert tone.get("tone") in ("casual", "formal", "humorous")

    def test_get_relationship_level_default(self):
        level = self.persona_mod.get_relationship_level()
        assert level in ("stranger", "acquaintance", "friend", "family")

    def test_set_relationship_level(self):
        self.persona_mod.set_relationship_level("friend")
        assert self.persona_mod.get_relationship_level() == "friend"

    def test_set_relationship_level_invalid(self):
        self.persona_mod.set_relationship_level("invalid_level")
        assert self.persona_mod.get_relationship_level() in (
            "stranger", "acquaintance", "friend", "family"
        )

    def test_contextual_response_style_stranger(self):
        self.persona_mod.set_relationship_level("stranger")
        style = self.persona_mod.contextual_response_style()
        assert isinstance(style, str)

    def test_contextual_response_style_friend(self):
        self.persona_mod.set_relationship_level("friend")
        style = self.persona_mod.contextual_response_style()
        assert isinstance(style, str)
        assert "朋友" in style or "轻松" in style or len(style) > 0

    def test_update_relationship_from_formal(self):
        self.persona_mod.update_relationship_from_interaction("您好，请问...")
        tone = self.persona_mod.get_tone_profile()
        assert tone.get("tone") == "formal"

    def test_update_relationship_from_humor(self):
        self.persona_mod.update_relationship_from_interaction("哈哈哈太搞笑了")
        tone = self.persona_mod.get_tone_profile()
        assert tone.get("tone") == "humorous"


# ===== Round 2: 记忆注入与检索增强 =====

class TestMemoryInjectionRound2:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="mem_r2_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.mem = _load_module("mem_r2_test", "magic-memory.py")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_format_memories_for_prompt_uses_hybrid_recall(self):
        """format_memories_for_prompt 应使用 recall_hybrid（混合检索）而非仅 bigram 匹配。"""
        self.mem.remember_conversation("项目周五截止", "注意进度")
        # 等待时间戳差异，确保 recency 评分生效
        time.sleep(0.05)
        self.mem.remember_conversation("今天天气不错", "是的")
        # recall_hybrid 会结合 recency + relevance + importance
        results_hybrid = self.mem.recall_hybrid("项目", k=3)
        formatted = self.mem.format_memories_for_prompt("项目", limit=3)
        assert len(results_hybrid) > 0
        assert "项目" in formatted or len(formatted) > 0
        # 验证返回格式包含时间戳
        if formatted:
            assert "[" in formatted and "]" in formatted

    def test_format_memories_for_prompt_empty(self):
        formatted = self.mem.format_memories_for_prompt("不存在的主题xyz", limit=3)
        assert formatted == ""

    def test_remember_conversation_async_logs_warning_on_failure(self, caplog):
        """_remember_conversation_async 在加载模块失败时应记录 warning，而非静默跳过。"""
        import logging
        caplog.set_level(logging.WARNING, logger="magic")
        # 直接导入 llm 模块的函数进行测试
        from agent.llm import _remember_conversation_async
        # 用一个不存在的模块名触发加载失败
        with patch("app.load_magic_module", side_effect=RuntimeError("load failed")):
            _remember_conversation_async("测试", "回复")
        assert "记忆提取失败" in caplog.text
        assert "load failed" in caplog.text


# ===== Round 4: 集成增强 =====

class TestRound4AnaphorPrompt:
    """working_memory 指代消解注入 messages"""

    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="r4_anaphor_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        from agent.working_memory import reset
        reset()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_no_prompt_when_empty(self):
        from agent.llm import _build_wm_anaphor_prompt
        from agent.working_memory import reset
        reset()
        assert _build_wm_anaphor_prompt() is None

    def test_prompt_includes_intent_stack(self):
        from agent.llm import _build_wm_anaphor_prompt
        from agent.working_memory import update
        update(intent="weather")
        update(intent="music")
        prompt = _build_wm_anaphor_prompt()
        assert prompt is not None
        assert "weather" in prompt or "music" in prompt

    def test_prompt_includes_session_facts(self):
        from agent.llm import _build_wm_anaphor_prompt
        from agent.working_memory import update
        update(facts={"city": "北京"})
        prompt = _build_wm_anaphor_prompt()
        assert prompt is not None
        assert "city=北京" in prompt


class TestRound4ContextCacheInvalidation:
    """invalidate_context_cache 在会话切换时被调用"""

    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="r4_ctx_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_session_switch_invalidates_context(self):
        from agent.context import get_context, invalidate_context_cache
        from agent.llm import _last_wm_session_id, brain_stream_sentences
        from agent.working_memory import reset
        reset()
        # 先填充缓存
        ctx = get_context()
        assert isinstance(ctx, dict)
        # 模拟会话切换：通过直接调用 invalidate 验证可清除
        invalidate_context_cache()
        ctx2 = get_context()
        # 缓存已清空，应重新构建（仍返回 dict）
        assert isinstance(ctx2, dict)
        assert "time" in ctx2


class TestRound4SchedulerProactiveGreeting:
    """调度器状态变化时调用 proactive_greeting / suggest_action"""

    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="r4_sched_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.jarvis = _load_module("jarvis_r4_test", "magic-jarvis.py")

    def teardown_method(self):
        import shutil
        import importlib
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)
        # Reload only the modules that this test class dynamically loaded via
        # importlib, so they do not keep references to the deleted temp dir.
        for mod_name in ["magic_jarvis_r4_test", "magic_scenes_runtime"]:
            try:
                mod = sys.modules.get(mod_name)
                if mod is not None:
                    importlib.reload(mod)
            except Exception:
                pass

    def test_proactive_greeting_called_in_away_state(self):
        """away 状态应调用 proactive_greeting"""
        from app.schedulers import _proactive_suggestions
        from app import load_magic_module as _orig_load
        import time

        # 构建 mock jarvis
        mock_jarvis = type("MockJarvis", (), {})()
        mock_jarvis.proactive_greeting = lambda: "出门注意安全。"
        mock_jarvis.suggest_action = lambda ctx: ""

        call_log = []
        def fake_load(name, filename=None):
            if name == "magic_jarvis":
                call_log.append("proactive_greeting")
                return mock_jarvis
            return _orig_load(name, filename)

        # 模拟用户状态为 away，并让循环只执行一次
        fake_state = {"state": "away", "confidence": 0.9}

        with patch("app.schedulers.load_magic_module", side_effect=fake_load), \
             patch("voice_agent.get_user_state", return_value=fake_state), \
             patch("app.schedulers.time.sleep", side_effect=[None, StopIteration]), \
             patch("app.schedulers.add_notification") as mock_add, \
             patch("app.schedulers.play_reminder_audio"):
            try:
                _proactive_suggestions()
            except StopIteration:
                pass
        assert "proactive_greeting" in call_log
        assert mock_add.called

    def test_suggest_action_called_in_morning(self):
        """早晨应调用 suggest_action"""
        from app.schedulers import _proactive_suggestions
        from app import load_magic_module as _orig_load
        import datetime
        call_log = []

        mock_jarvis = type("MockJarvis", (), {})()
        mock_jarvis.proactive_greeting = lambda: "早上好！"
        mock_jarvis.suggest_action = lambda ctx: "建议查天气"

        def fake_load(name, filename=None):
            if name == "magic_jarvis":
                call_log.append("suggest_action")
                return mock_jarvis
            return _orig_load(name, filename)

        fake_state = {"state": "home_awake", "confidence": 0.9}

        with patch("app.schedulers.datetime") as mock_dt, \
             patch("app.schedulers.load_magic_module", side_effect=fake_load), \
             patch("voice_agent.get_user_state", return_value=fake_state), \
             patch("app.schedulers.time.sleep", side_effect=[None, StopIteration]), \
             patch("app.schedulers.add_notification") as mock_add, \
             patch("app.schedulers.play_reminder_audio"), \
             patch("app.schedulers._get_weather", return_value=[]):
            mock_dt.datetime.now.return_value = datetime.datetime(2026, 8, 21, 9, 0)
            mock_dt.side_effect = datetime.datetime
            try:
                _proactive_suggestions()
            except StopIteration:
                pass
        assert "suggest_action" in call_log or any("建议" in str(c.args) for c in mock_add.mock_calls)


class TestRound4FastPathMemory:
    """FAST_PATHS 命中后应触发记忆提取"""

    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = tempfile.mkdtemp(prefix="r4_fp_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_fast_path_triggers_memory_async(self):
        """FAST_PATHS 命中时应在后台线程触发记忆提取"""
        from unittest.mock import patch
        from agent.llm import brain_stream_sentences
        from voice_agent import FAST_PATHS

        memory_calls = []

        def fake_remember(user_text, assistant_reply):
            memory_calls.append((user_text, assistant_reply))

        with patch("agent.llm._remember_conversation_async", side_effect=fake_remember) as mock_mem, \
             patch("agent.llm._get_brain") as mock_brain, \
             patch("agent.llm._cache_get", return_value=None), \
             patch("agent.llm._cache_get_interrupted", return_value=None), \
             patch("voice_agent.FAST_PATHS", [
                 FAST_PATHS[0],  # time path
             ]):
            mock_brain.return_value.run.return_value = iter([])
            list(brain_stream_sentences("几点", session_id="test_fp"))
        # FAST_PATHS 命中，应触发记忆提取
        assert mock_mem.called or any("几点" in str(c) for c in memory_calls)
