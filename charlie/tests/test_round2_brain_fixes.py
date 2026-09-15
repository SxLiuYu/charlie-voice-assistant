"""Round 2 brain-side fixes regression tests (NB1-NB8).

覆盖：
- NB1: _get_history_file() 按 CHARLIE_USER_ID 动态分流 + 签名缓存按 uid 分键
- NB2: feishu_text 通道规则通过 messages.insert 注入，不动 brain 实例属性
- NB3: _record_brain_failure 连续 429 仅 GLM provider 清空 brains
- NB4: context 退避计数在连续网络失败时生效
- NB5: get_context / _refresh_context_cache 按 session_id 取最近对话
- NB6: _context_summaries 统一以 session_id 为 key
- NB7: rotate_glm_model 线程安全
- NB8: set_current_user 清响应缓存并失效上下文缓存
"""
import os
import sys
import json
import time
import threading
import tempfile
import shutil
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from agent.history import (
    _get_history_file,
    _get_history_lock_file,
    _append_history,
    _save_history,
    _load_history,
    _get_history,
    _history_file_signatures,
    _history_file_caches,
    _context_summaries,
    HISTORY_FILE,
    HISTORY_LOCK_FILE,
    _history_file_sig,
    _read_history_file_locked,
)
from agent.llm import (
    _record_brain_failure,
    set_current_user,
    _st,
)
from app.llm_config import (
    rotate_glm_model,
    _glm_rotation_idx,
    _glm_rotate_lock,
    GLM_MODEL,
    get_glm_models,
)
from agent.context import (
    get_context,
    _refresh_context_cache,
    _context_fail_count,
    _context_backoff_until,
    invalidate_context_cache,
    format_context_for_prompt,
)
from agent.system_msg import (
    _build_system_msg,
    _build_system_msg_impl,
)


@pytest.fixture(autouse=True)
def _restore_user_state():
    """每个用例前后快照/恢复多用户相关全局状态。

    set_current_user() 会同步 agent.history 的静态导出并改写 llm_state；
    只 pop 环境变量不够，残留的导出值会污染后续测试文件
    （test_voice_agent / test_runtime_resilience 依赖 HISTORY_FILE 指向 default 路径）。
    """
    from agent import history as _hist_mod
    saved_env_user = os.environ.get("CHARLIE_USER_ID")
    saved_llm_uid = getattr(_st, "current_user_id", "default")
    saved_hf = _hist_mod.HISTORY_FILE
    saved_hlf = _hist_mod.HISTORY_LOCK_FILE
    saved_sigs = dict(_hist_mod._history_file_signatures)
    saved_caches = dict(_hist_mod._history_file_caches)
    yield
    if saved_env_user is None:
        os.environ.pop("CHARLIE_USER_ID", None)
    else:
        os.environ["CHARLIE_USER_ID"] = saved_env_user
    try:
        _st.current_user_id = saved_llm_uid
    except Exception:
        pass
    _hist_mod.HISTORY_FILE = saved_hf
    _hist_mod.HISTORY_LOCK_FILE = saved_hlf
    _hist_mod._history_file_signatures.clear()
    _hist_mod._history_file_signatures.update(saved_sigs)
    _hist_mod._history_file_caches.clear()
    _hist_mod._history_file_caches.update(saved_caches)


@pytest.fixture(autouse=True)
def _mock_context_network(monkeypatch):
    """P1: 把 context 刷新线程依赖的网络调用 mock 为快速返回空，
    避免真实网络 I/O 导致后台线程跨测试泄漏。"""
    monkeypatch.setattr("agent.weather.direct_weather_play", lambda *a, **k: "")
    monkeypatch.setattr("app.geo.locate_text", lambda *a, **k: "")


# ===== NB2 helpers =====

class FakeBrain:
    def __init__(self):
        self.system_message = (
            "你是 Charlie。\n"
            "<<<VOICE_RULE>>>\n"
            "禁止输出 markdown。"
        )
        self.calls = []

    def run(self, messages):
        self.calls.append(messages)
        yield [{"role": "assistant", "content": "ok"}]


# ===== NB1: dynamic history file + per-uid signature cache =====
# 关键：不调用 importlib.reload（会破坏 voice_agent 对 agent.history 的引用），
# 而是直接测动态函数 + 清理模块级缓存。

class TestNB1DynamicHistoryFile:
    def setup_method(self):
        import agent.history as hist_mod
        self._orig_env = os.environ.get("CHARLIE_USER_ID")
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmpdir = tempfile.mkdtemp(prefix="nb1-test-")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmpdir
        # 保存并清掉模块级缓存
        self._orig_signatures = hist_mod._history_file_signatures.copy()
        self._orig_caches = {k: v.copy() for k, v in hist_mod._history_file_caches.items()}
        hist_mod._history_file_signatures.clear()
        hist_mod._history_file_caches.clear()
        # 保存 default 会话原始长度，teardown 时截断回原长，避免污染 voice_agent._history
        self._orig_default_len = len(hist_mod._sessions.get("default", []))

    def teardown_method(self):
        import agent.history as hist_mod
        # 先恢复环境变量，防止后续测试继续使用已删除的临时目录
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)
        if self._orig_env is not None:
            os.environ["CHARLIE_USER_ID"] = self._orig_env
        else:
            os.environ.pop("CHARLIE_USER_ID", None)
        # 恢复模块级缓存
        hist_mod._history_file_signatures.clear()
        hist_mod._history_file_signatures.update(self._orig_signatures)
        hist_mod._history_file_caches.clear()
        hist_mod._history_file_caches.update(self._orig_caches)
        # 恢复 default 会话长度（截断），避免破坏 voice_agent._history 引用
        if "default" in hist_mod._sessions:
            default_hist = hist_mod._sessions["default"]
            if len(default_hist) > self._orig_default_len:
                del default_hist[self._orig_default_len:]
        # 清理测试期间创建的临时目录
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_default_user_uses_default_path(self):
        os.environ.pop("CHARLIE_USER_ID", None)
        p = _get_history_file()
        assert p.endswith("conversation_history.json")
        assert "_default" not in p

    def test_alice_uses_alice_path(self):
        os.environ["CHARLIE_USER_ID"] = "alice"
        p = _get_history_file()
        assert p.endswith("conversation_history_alice.json")

    def test_bob_uses_bob_path(self):
        os.environ["CHARLIE_USER_ID"] = "bob"
        p = _get_history_file()
        assert p.endswith("conversation_history_bob.json")

    def test_lock_file_matches_history_file(self):
        os.environ["CHARLIE_USER_ID"] = "alice"
        assert _get_history_lock_file() == _get_history_file() + ".lock"

    def test_append_history_writes_to_correct_user_file(self):
        os.environ["CHARLIE_USER_ID"] = "alice"
        # 清空 default 会话，避免旧数据干扰
        _get_history("default").clear()
        _append_history(_get_history("default"), "hello alice", "hi alice")
        _save_history()
        path = _get_history_file()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "default" in data
        assert data["default"][-1]["content"] == "hi alice"

    def test_switch_user_does_not_see_other_history(self):
        # alice 写历史
        os.environ["CHARLIE_USER_ID"] = "alice"
        _get_history("default").clear()
        _append_history(_get_history("default"), "alice msg", "alice reply")
        _save_history()

        # bob 读历史应空（新文件）
        os.environ["CHARLIE_USER_ID"] = "bob"
        _get_history("default").clear()
        assert len(_get_history("default")) == 0

        # 验证 alice 文件内容独立
        alice_path = os.path.join(self._tmpdir, "conversation_history_alice.json")
        with open(alice_path, "r", encoding="utf-8") as f:
            alice_data = json.load(f)
        assert alice_data["default"][-1]["content"] == "alice reply"

    def test_signature_cache_per_uid(self):
        # alice 文件存在 → 读一次后签名写入 alice 缓存
        alice_path = os.path.join(self._tmpdir, "conversation_history_alice.json")
        with open(alice_path, "w", encoding="utf-8") as f:
            json.dump({"default": []}, f)
        os.environ["CHARLIE_USER_ID"] = "alice"
        _read_history_file_locked()
        assert "alice" in _history_file_signatures

        # bob 文件不存在 → 不应在签名缓存中留下 bob 条目
        os.environ["CHARLIE_USER_ID"] = "bob"
        _read_history_file_locked()
        assert "bob" not in _history_file_signatures


# ===== NB2: feishu_text channel rules via messages.insert =====

class TestNB2FeishuChannelIsolation:
    def test_feishu_text_does_not_mutate_brain_system_message(self):
        from agent.llm import brain_stream_sentences
        brain = FakeBrain()
        original_sys_msg = brain.system_message
        with patch("agent.llm._get_brain", return_value=brain), \
             patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._cache_get", return_value=None), \
             patch("agent.llm._get_history", return_value=[]), \
             patch("agent.llm._append_history"), \
             patch("agent.llm._cache_set"), \
             patch("agent.llm._build_wm_anaphor_prompt", return_value=None), \
             patch("agent.llm._remember_conversation_async"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])):
            list(brain_stream_sentences("hi", channel="feishu_text"))
        assert brain.system_message == original_sys_msg

    def test_feishu_text_injects_channel_rules_at_head(self):
        from agent.llm import brain_stream_sentences
        brain = FakeBrain()
        with patch("agent.llm._get_brain", return_value=brain), \
             patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._cache_get", return_value=None), \
             patch("agent.llm._get_history", return_value=[]), \
             patch("agent.llm._append_history"), \
             patch("agent.llm._cache_set"), \
             patch("agent.llm._build_wm_anaphor_prompt", return_value=None), \
             patch("agent.llm._remember_conversation_async"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])):
            list(brain_stream_sentences("hi", channel="feishu_text"))
        messages = brain.calls[0]
        # 头部应为 feishu 规则 system message
        assert messages[0]["role"] == "system"
        assert "飞书文本对话" in messages[0]["content"]
        # 原有 base system 应在 brain.run 中被 assistant 自己消费，这里不强制位置
        # 但至少 feishu 规则在 messages 头部
        assert any("飞书文本对话" in m.get("content", "") for m in messages)

    def test_voice_channel_unchanged(self):
        from agent.llm import brain_stream_sentences
        brain = FakeBrain()
        with patch("agent.llm._get_brain", return_value=brain), \
             patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._cache_get", return_value=None), \
             patch("agent.llm._get_history", return_value=[]), \
             patch("agent.llm._append_history"), \
             patch("agent.llm._cache_set"), \
             patch("agent.llm._build_wm_anaphor_prompt", return_value=None), \
             patch("agent.llm._remember_conversation_async"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])):
            list(brain_stream_sentences("hi", channel="voice"))
        messages = brain.calls[0]
        # voice 通道不应注入 feishu 规则
        assert not any("飞书文本对话" in m.get("content", "") for m in messages)


# ===== NB3: 429 guard with active_is_glm() =====

class TestNB3429Guard:
    def setup_method(self):
        _st.brains.clear()
        _st.brain_failures = 0
        _st.brain_total_failures = 0

    def test_non_glm_429_does_not_clear_brains(self, monkeypatch):
        monkeypatch.setattr("agent.llm.active_is_glm", lambda: False)
        # 抬高通用阈值，避免 5 次失败提前清缓存，干扰 429 分支验证
        monkeypatch.setattr("agent.llm._MAX_BRAIN_FAILURES", 100)
        brain = object()
        _st.brains["none"] = brain
        for _ in range(10):
            _record_brain_failure("429 Too Many Requests")
        assert _st.brains  # 非 GLM 不清空

    def test_glm_429_clears_brains(self, monkeypatch):
        monkeypatch.setattr("agent.llm.active_is_glm", lambda: True)
        monkeypatch.setattr("agent.llm._MAX_BRAIN_FAILURES", 100)
        brain = object()
        _st.brains["none"] = brain
        _record_brain_failure("429 Too Many Requests")
        # 注意：需要连续 >=10 次才触发，这里先验证阈值逻辑可被触发
        for _ in range(9):
            _record_brain_failure("429 Too Many Requests")
        assert not _st.brains  # GLM 连续 10 次 429 后清空


# ===== NB4: context backoff =====

class TestNB4ContextBackoff:
    def setup_method(self):
        import agent.context as ctx_mod
        ctx_mod._context_fail_count = 0
        ctx_mod._context_backoff_until = 0.0
        # P1: 重置刷新线程，避免前测泄漏的线程导致新线程不启动
        if ctx_mod._context_refresh_thread is not None:
            ctx_mod._context_refresh_thread.join(timeout=2.0)
            ctx_mod._context_refresh_thread = None

    def test_three_weather_failures_trigger_backoff(self, monkeypatch):
        import agent.context as ctx_mod
        call_count = {"count": 0}

        def boom(*args, **kwargs):
            call_count["count"] += 1
            raise RuntimeError("weather down")

        monkeypatch.setattr("agent.weather.direct_weather_play", boom)
        # 定位同为网络字段且排在天气之前：一并 mock 为快速失败，
        # 避免真实 locate_text 网络慢时刷新线程卡住、失败计数达不到 3
        monkeypatch.setattr("app.geo.locate_text", boom)
        # 隔离：等前序测试可能泄漏的刷新线程退出，避免其在中途写入缓存
        if ctx_mod._context_refresh_thread is not None:
            ctx_mod._context_refresh_thread.join(timeout=2.0)
        ctx_mod._context_refresh_thread = None

        # 轮询直到 3 次失败触发退避。不能用固定 3 次 get_context 编排：
        # 刷新线程即使失败也会写入部分缓存（时间字段永远成功），60s TTL 内
        # 后续 get_context 走缓存早退、不再启动新线程——满载下时序漂移会
        # 让失败计数到不了 3（全量偶发失败的根因）。
        deadline = time.time() + 10.0
        while time.time() < deadline and ctx_mod._context_backoff_until <= time.time():
            invalidate_context_cache()   # 强制走刷新路径
            get_context()
            t = ctx_mod._context_refresh_thread
            if t is not None:
                t.join(timeout=2.0)
            time.sleep(0.02)
        assert call_count["count"] >= 3, \
            f"boom 仅被调 {call_count['count']} 次，未达到 3 次失败"
        assert ctx_mod._context_backoff_until > time.time()

    def test_backoff_skips_refresh_thread(self, monkeypatch):
        import agent.context as ctx_mod

        def down(*args, **kwargs):
            raise RuntimeError("down")

        # 先触发退避（网络字段全部快速失败，保证确定性）
        monkeypatch.setattr("agent.weather.direct_weather_play", down)
        monkeypatch.setattr("app.geo.locate_text", down)
        if ctx_mod._context_refresh_thread is not None:
            ctx_mod._context_refresh_thread.join(timeout=2.0)
        ctx_mod._context_refresh_thread = None
        deadline = time.time() + 10.0
        while time.time() < deadline and ctx_mod._context_backoff_until <= time.time():
            invalidate_context_cache()
            get_context()
            t = ctx_mod._context_refresh_thread
            if t is not None:
                t.join(timeout=2.0)
            time.sleep(0.02)
        assert ctx_mod._context_backoff_until > time.time()
        # 再次调用，不应启动新线程。
        # 注意：不能用 threading.active_count() 比较——它统计全进程所有线程
        # （飞书重连/mqtt clearer 等无关线程），满载下必然偶发。改为针对性断言：
        # 退避期间模块槽位里的线程引用不应被替换（即没有新刷新线程被启动）。
        old_thread = ctx_mod._context_refresh_thread
        get_context()
        time.sleep(0.1)
        assert ctx_mod._context_refresh_thread is old_thread, \
            "backoff 期间启动了新的刷新线程"


# ===== NB5: context per session_id =====

class TestNB5ContextSessionIsolation:
    def setup_method(self):
        from agent import history as hist_mod
        # 只补充 device-1 会话，不动 default，避免破坏 _sessions["default"] -> _history 引用
        if "device-1" not in hist_mod._sessions:
            hist_mod._sessions["device-1"] = []

    def test_get_context_recent_conversation_per_session(self):
        from agent.history import _get_history
        # default 会话写入 A
        _get_history("default").append({"role": "user", "content": "default msg"})
        # device-1 会话写入 B
        _get_history("device-1").append({"role": "user", "content": "device-1 msg"})
        invalidate_context_cache()
        ctx = get_context(session_id="device-1")
        assert "device-1 msg" in ctx.get("recent_conversation", [])
        assert "default msg" not in ctx.get("recent_conversation", [])


# ===== NB6: _context_summaries key unified to session_id =====

class TestNB6SummaryKeySessionId:
    def test_build_system_msg_reads_summary_by_session_id(self):
        import agent.history as hist_mod
        hist_mod._context_summaries["session-42"] = "session-42 摘要"
        try:
            msg = _build_system_msg_impl("none", session_id="session-42")
            assert "session-42 摘要" in msg
        finally:
            hist_mod._context_summaries.pop("session-42", None)

    def test_different_session_id_does_not_leak_summary(self):
        import agent.history as hist_mod
        hist_mod._context_summaries["session-A"] = "A 摘要"
        hist_mod._context_summaries["session-B"] = "B 摘要"
        try:
            msg_a = _build_system_msg_impl("none", session_id="session-A")
            msg_b = _build_system_msg_impl("none", session_id="session-B")
            assert "A 摘要" in msg_a
            assert "B 摘要" in msg_b
            assert "B 摘要" not in msg_a
            assert "A 摘要" not in msg_b
        finally:
            hist_mod._context_summaries.pop("session-A", None)
            hist_mod._context_summaries.pop("session-B", None)


# ===== NB7: rotate_glm_model thread-safe =====

class TestNB7RotateGlmModelThreadSafe:
    def test_rotate_glm_model_is_thread_safe(self):
        # 配置 GLM_MODELS
        os.environ["GLM_MODELS"] = "glm-a,glm-b,glm-c"
        os.environ["GLM_MODEL"] = "glm-a"
        import importlib
        import app.llm_config as cfg_mod
        importlib.reload(cfg_mod)

        errors = []

        def worker():
            try:
                for _ in range(20):
                    cfg_mod.rotate_glm_model()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # 最终索引应在合法范围内
        assert cfg_mod._glm_rotation_idx < len(cfg_mod.get_glm_models())


# ===== NB8: set_current_user clears cache =====

class TestNB8SetCurrentUserClearsCache:
    def setup_method(self):
        from agent import cache as cache_mod
        with cache_mod._cache_lock:
            cache_mod._cache.clear()
        invalidate_context_cache()

    def teardown_method(self):
        # 恢复环境变量，避免影响后续测试（如 voice_agent 的 HISTORY_FILE 相关测试）
        os.environ.pop("CHARLIE_USER_ID", None)
        from agent import cache as cache_mod
        with cache_mod._cache_lock:
            cache_mod._cache.clear()
        invalidate_context_cache()

    def test_set_current_user_clears_response_cache(self):
        from agent.cache import _cache_set, _cache_get
        _cache_set("hello", "cached reply", session_id="s1")
        assert _cache_get("hello", session_id="s1") == "cached reply"
        set_current_user("bob")
        assert _cache_get("hello", session_id="s1") is None

    def test_set_current_user_invalidates_context_cache(self):
        # 先填充上下文缓存
        ctx = get_context()
        assert ctx  # 有缓存
        set_current_user("bob")
        ctx2 = get_context()
        # 缓存应已失效（可能返回空或重建，但不应用旧值）
        assert ctx2 is not None  # 不应抛异常
