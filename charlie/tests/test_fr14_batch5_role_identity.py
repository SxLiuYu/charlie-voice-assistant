"""FR-14 批次 5 回归：**角色/音色按 uid 隔离** + 线程池 ContextVar 传播 + 会话级切换兼容层。

方案依据：``docs/PLAN_FR14_MULTI_USER.md`` §4 第 4/5 步、决策 D-1

**背景（本轮实测出来的真 bug，与批次 3B 同族）**：

``agent/roles.py`` 把"当前角色"缓存成**进程级单例** ``_current_role``，而
``preferences_{uid}.json`` 自批次 2 起**已按 uid 分片** —— 进程级缓存把分片又抹平了。
实测（2026-09-17）两个方向的串台：

1. 老于把角色设成 ``jarvis`` 后，妈妈（``preferences_jiaren1.json`` 根本不存在）
   拿到的也是 ``jarvis`` —— 老于的设置泄漏给了全家人；
2. 进程重启后**妈妈先说话** → 缓存固化成 ``charlie`` → 老于之后一直拿到 ``charlie``，
   **他自己的 ``jarvis`` 设置失效**。谁先说话谁定全家的角色。

同时 ``set_role_tts_voice()`` 直接改**共享**的 ``_ROLES[role_id]["tts_voice"]``
（进程级唯一的一份内置角色表），而 ``_load_persisted_voice_overrides()`` 在
**模块加载时**被调用一次 —— 那一刻 uid 恒为 env/default，把某个用户的音色覆盖
烤进了进程状态。

另一条独立的线：``loop.run_in_executor`` 的工作线程**不继承** ``ContextVar``，
而 TTS 合成链（``_synth_mp3`` → ``asr_tts.get_effective_tts_config()`` →
``roles.get_current_role()``）依赖它解析"当前用户"的角色与音色 ⇒ 不拷上下文就
静默回落到 env/default，**妈妈听到老于的音色**。

⚠️ 本文件的源码级断言一律用 **AST** 或**结构不变量**，不用 ``str.index`` ——
本项目注释/文档字符串爱把被断言的符号名原样写出来，``index()`` 会命中注释让断言永真。
"""
import ast
import asyncio
import datetime
import json
import os
import sys
import time

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.history as hist_mod
import agent.llm as llm_mod
import agent.persona as persona_mod
import agent.preferences as pref_mod
import agent.roles as roles_mod
import agent.system_msg as sysmsg_mod
from agent import user_context as uc

LAOYU = "laoyu"
JIAREN = "jiaren1"

# 合法的"无参数"JSON Schema。⚠️ 不能传 `{}`，也不能只写 `type`+`properties` ——
# `qwen_agent.tools.base.is_tool_schema` 要求键集合**恰好**是
# `{type, properties, required}` 且 `required` 是 list（`base.py:93-97`），
# 否则实例化时直接抛 `ValueError: ... must confirm to a valid openai-compatible
# JSON schema.`（实测 2026-09-17，两轮才试对）。
_EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": []}

# preferences 的模块级状态（default 走标量，其余走分片字典）
_PREF_DICT_KEYS = ("_preferences", "_uid_prefs", "_uid_signatures",
                   "_uid_revisions", "_uid_save_seqs")
_PREF_SCALAR_KEYS = ("_preferences_file_signature", "_preferences_revision",
                     "_preferences_save_seq")


def _src(rel: str) -> str:
    with open(os.path.join(PROJECT_DIR, rel), "r", encoding="utf-8") as f:
        return f.read()


# ======================================================================
# fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """独立数据目录 + 清空全部按 uid 的内存缓存 + 复位进程级覆盖。

    ⚠️ `agent.preferences.DATA_DIR` / `PREFS_FILE` 是**导入期**从 env 读的模块常量，
    只 patch env 不够，必须直接改属性（同 `test_fr14_batch2_uid_sharding.py`）。
    """
    d = str(tmp_path)
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", d)
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
    monkeypatch.setattr(pref_mod, "DATA_DIR", d)
    monkeypatch.setattr(pref_mod, "PREFS_FILE", os.path.join(d, "preferences.json"))
    monkeypatch.setattr(pref_mod, "PREFS_LOCK_FILE",
                        os.path.join(d, "preferences.json.lock"))
    # `agent.persona.DATA_DIR` 也是**导入期**常量（`persona.py:12`），同 pref。
    # `_states` 是按 uid 的内存缓存，跨用例必须清 —— 否则上一条用例的 tmp 目录
    # 里的 persona 会被下一条读到（表现为"单独跑过、全量跑失败"）。
    monkeypatch.setattr(persona_mod, "DATA_DIR", d)
    with persona_mod._STATE_LOCK:
        persona_mod._states.clear()
    sysmsg_mod.invalidate_system_msg_cache()

    saved = {}
    for name in _PREF_DICT_KEYS:
        val = getattr(pref_mod, name, None)
        if isinstance(val, dict):
            saved[name] = dict(val)
            val.clear()
    for name in _PREF_SCALAR_KEYS:
        saved[name] = getattr(pref_mod, name, None)
        setattr(pref_mod, name, None)

    uc.clear_all_session_users()
    # 角色模块：清空按 uid 缓存与进程级覆盖
    monkeypatch.setattr(roles_mod, "_current_role", None)
    roles_mod._current_role_by_uid.clear()
    roles_mod._voice_overrides.clear()
    _role_voice_snapshot = {r: c.get("tts_voice") for r, c in roles_mod._ROLES.items()}

    _cur_user_before = llm_mod._st.current_user_id
    _hist_before = (hist_mod.HISTORY_FILE, hist_mod.HISTORY_LOCK_FILE)

    yield

    uc.clear_all_session_users()
    with persona_mod._STATE_LOCK:
        persona_mod._states.clear()
    sysmsg_mod.invalidate_system_msg_cache()
    for name in _PREF_DICT_KEYS:
        cur = getattr(pref_mod, name, None)
        if isinstance(cur, dict):
            cur.clear()
            cur.update(saved.get(name, {}))
    for name in _PREF_SCALAR_KEYS:
        setattr(pref_mod, name, saved.get(name))
    llm_mod._st.current_user_id = _cur_user_before
    hist_mod.HISTORY_FILE, hist_mod.HISTORY_LOCK_FILE = _hist_before
    # `_ROLES` 是内置角色表，任何用例都不该改它；这里断言式复原 + 兜底
    for r, v in _role_voice_snapshot.items():
        roles_mod._ROLES[r]["tts_voice"] = v


# ======================================================================
# 1. 「当前角色」必须按 uid 解析，不能是进程级单例
# ======================================================================

class TestCurrentRolePerUid:
    def test_cache_is_a_dict_not_a_singleton(self):
        """结构不变量：按 uid 的缓存必须是容器（原实现是 `_current_role` 单例）。"""
        assert isinstance(roles_mod._current_role_by_uid, dict)

    def test_role_differs_per_uid(self):
        with uc.bind_user(LAOYU):
            pref_mod.set_preference("current_role", "jarvis")
            assert roles_mod.get_current_role() == "jarvis"
        with uc.bind_user(JIAREN):
            assert roles_mod.get_current_role() == "charlie", \
                "老于的角色泄漏给了家人（`preferences_jiaren1.json` 根本不存在）"

    def test_no_role_set_falls_back_to_charlie(self):
        with uc.bind_user(LAOYU):
            assert roles_mod.get_current_role() == "charlie"

    def test_first_speaker_does_not_freeze_everyone(self):
        """**核心回归**：进程重启后"妈妈先说话"不能把全家的角色固化成 charlie。

        原实现 `_current_role` 是进程级单例 —— 谁先说话谁定全家的角色，
        而且"后说话的人自己的设置失效"，用户侧表现为"我设的角色怎么又变回去了"。
        """
        with uc.bind_user(LAOYU):
            pref_mod.set_preference("current_role", "jarvis")
        # 模拟进程重启：内存缓存清空，只剩磁盘上的分片偏好
        roles_mod._current_role = None
        roles_mod._current_role_by_uid.clear()

        with uc.bind_user(JIAREN):
            assert roles_mod.get_current_role() == "charlie"
        with uc.bind_user(LAOYU):
            assert roles_mod.get_current_role() == "jarvis", \
                "妈妈先说话把老于的角色吃掉了"

    def test_same_uid_is_stable(self):
        with uc.bind_user(LAOYU):
            pref_mod.set_preference("current_role", "baize")
            assert roles_mod.get_current_role() == "baize"
            assert roles_mod.get_current_role() == "baize"

    def test_switch_role_only_affects_current_uid(self):
        with uc.bind_user(LAOYU):
            ok, msg = roles_mod.switch_role("jarvis")
            assert ok and "J.A.R.V.I.S." in msg
            assert roles_mod.get_current_role() == "jarvis"
        assert roles_mod._current_role is None, \
            "switch_role 写了进程级 `_current_role` ⇒ 老于切角色会改到全家人"
        with uc.bind_user(JIAREN):
            assert roles_mod.get_current_role() == "charlie"
        with uc.bind_user(LAOYU):
            assert roles_mod.get_current_role() == "jarvis"

    def test_switch_role_persists_per_uid(self):
        """切换要落到**自己**的分片文件，别人的分片不受影响。"""
        with uc.bind_user(LAOYU):
            roles_mod.switch_role("baize")
        assert os.path.exists(os.path.join(str(pref_mod.DATA_DIR),
                                           "preferences_laoyu.json"))
        with uc.bind_user(JIAREN):
            roles_mod.switch_role("kid")
        assert os.path.exists(os.path.join(str(pref_mod.DATA_DIR),
                                           "preferences_jiaren1.json"))
        roles_mod._current_role_by_uid.clear()
        with uc.bind_user(LAOYU):
            assert roles_mod.get_current_role() == "baize"
        with uc.bind_user(JIAREN):
            assert roles_mod.get_current_role() == "kid"

    def test_invalid_role_rejected(self):
        ok, msg = roles_mod.switch_role("不存在")
        assert not ok and "未知角色" in msg

    def test_explicit_global_override_still_wins(self):
        """`_current_role` 保留为**显式进程级覆盖**（管理台强制切换 / 测试后门）。

        这是刻意保留的兼容点，不是缓存 —— 所以它非 None 时优先于按 uid 解析。
        """
        roles_mod._current_role = "kid"
        with uc.bind_user(LAOYU):
            assert roles_mod.get_current_role() == "kid"
        with uc.bind_user(JIAREN):
            assert roles_mod.get_current_role() == "kid"

    def test_get_current_role_does_not_write_global(self):
        """行为级：解析过程不得把结果写回进程级 `_current_role`（否则又变回单例）。"""
        with uc.bind_user(LAOYU):
            pref_mod.set_preference("current_role", "jarvis")
            roles_mod.get_current_role()
            assert roles_mod._current_role is None, \
                "解析结果被写回进程级变量 ⇒ 又变成跨用户单例"


# ======================================================================
# 2. 角色音色覆盖按 uid 隔离，且不污染内置角色表
# ======================================================================

class TestRoleVoicePerUid:
    def test_voice_override_is_per_uid(self):
        with uc.bind_user(LAOYU):
            roles_mod.set_role_tts_voice("charlie", "Nova")
            assert roles_mod.get_role_tts_config("charlie")["voice"] == "Nova"
        with uc.bind_user(JIAREN):
            assert roles_mod.get_role_tts_config("charlie")["voice"] == "Ethan", \
                "老于改的音色影响到了家人"

    def test_roles_table_not_polluted_by_user_actions(self):
        """**结构不变量**：`_ROLES` 是进程级唯一的内置角色表，用户操作不能写它。"""
        baseline = {r: c["tts_voice"] for r, c in roles_mod._ROLES.items()}
        with uc.bind_user(LAOYU):
            roles_mod.set_role_tts_voice("charlie", "Nova")
            roles_mod.set_role_tts_voice("jarvis", "Vega")
            roles_mod._load_persisted_voice_overrides()
        assert {r: c["tts_voice"] for r, c in roles_mod._ROLES.items()} == baseline, \
            "_ROLES 被用户覆盖污染 —— 一个人改音色会改到所有人"

    def test_override_survives_cache_reset_per_uid(self):
        """"新进程"（内存缓存清空）后按 uid 从各自分片恢复音色。"""
        with uc.bind_user(LAOYU):
            roles_mod.set_role_tts_voice("charlie", "Nova")
        with uc.bind_user(JIAREN):
            roles_mod.set_role_tts_voice("charlie", "Vega")
        roles_mod._voice_overrides.clear()
        with uc.bind_user(LAOYU):
            assert roles_mod.get_role_tts_config("charlie")["voice"] == "Nova"
        with uc.bind_user(JIAREN):
            assert roles_mod.get_role_tts_config("charlie")["voice"] == "Vega"

    def test_invalid_persisted_voice_rejected(self):
        with uc.bind_user(LAOYU):
            pref_mod.set_preference("tts_voice:charlie", "NOT_A_VOICE")
            roles_mod._voice_overrides.clear()
            assert roles_mod.get_role_tts_config("charlie")["voice"] == "Ethan"

    def test_load_persisted_voice_overrides_respects_uid(self):
        with uc.bind_user(LAOYU):
            pref_mod.set_preference("tts_voice:jarvis", "Nova")
            roles_mod._voice_overrides.clear()
            roles_mod._load_persisted_voice_overrides()
            assert roles_mod.get_role_tts_config("jarvis")["voice"] == "Nova"
        with uc.bind_user(JIAREN):
            roles_mod._voice_overrides.clear()
            roles_mod._load_persisted_voice_overrides()
            assert roles_mod.get_role_tts_config("jarvis")["voice"] == "Alex", \
                "预热把老于的音色烤进了家人的进程状态"

    def test_no_import_time_load_call(self):
        """AST 级：模块体里不得**顶层调用** `_load_persisted_voice_overrides()`。

        导入期的 uid 恒为 env/default，在那里读偏好 = 把某个用户的音色覆盖
        固化进进程状态（原实现就是这么写的）。注释/docstring 不算。
        """
        tree = ast.parse(_src("agent/roles.py"))
        top_level_calls = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                f = stmt.value.func
                top_level_calls.append(getattr(f, "id", getattr(f, "attr", "")))
        assert "_load_persisted_voice_overrides" not in top_level_calls, \
            "模块加载时预热会把 default 用户的音色烤进进程状态"

    def test_speed_is_role_intrinsic_not_per_user(self):
        """speed 是角色固有属性（不按用户分）—— 钉住这个设计，避免被"顺手也分了"。"""
        with uc.bind_user(LAOYU):
            laoyu_speed = roles_mod.get_role_tts_config("jarvis")["speed"]
        with uc.bind_user(JIAREN):
            jiaren_speed = roles_mod.get_role_tts_config("jarvis")["speed"]
        assert laoyu_speed == jiaren_speed == 0.92

    def test_unknown_role_returns_default_config(self):
        assert roles_mod.get_role_tts_config("不存在") == {"voice": "Ethan", "speed": 1.0}

    def test_resolve_uid_uses_contextvar_not_env(self, monkeypatch):
        monkeypatch.setenv("CHARLIE_USER_ID", "from_env")
        with uc.bind_user(LAOYU):
            assert roles_mod._resolve_uid() == LAOYU
        assert roles_mod._resolve_uid() == "from_env"


# ======================================================================
# 3. 兼容层：set_current_user(user_id, session_id=None)
# ======================================================================

class TestSetCurrentUserCompat:
    def test_signature_has_optional_session_id(self):
        import inspect
        sig = inspect.signature(llm_mod.set_current_user)
        assert "session_id" in sig.parameters
        assert sig.parameters["session_id"].default is None

    def test_session_path_binds_session_only(self, monkeypatch):
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
        before = llm_mod._st.current_user_id
        llm_mod.set_current_user(LAOYU, session_id="dev-kitchen")
        assert uc.get_session_user("dev-kitchen") == LAOYU
        assert uc.resolve_user("dev-kitchen") == LAOYU
        assert "CHARLIE_USER_ID" not in os.environ, \
            "会话级切换不该写进程级 env"
        assert llm_mod._st.current_user_id == before, \
            "会话级切换不该改进程级 current_user_id"

    def test_session_path_does_not_clear_other_sessions_cache(self):
        """会话级切换不能清全局响应缓存 —— 那是误伤其他用户。"""
        from agent import cache as cache_mod
        cache_mod._cache_set("你好", "别人的回复", session_id="dev-other")
        llm_mod.set_current_user(LAOYU, session_id="dev-kitchen")
        assert cache_mod._cache_get("你好", session_id="dev-other") == "别人的回复"
        with cache_mod._cache_lock:
            cache_mod._cache.clear()

    def test_session_path_is_scoped(self, monkeypatch):
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
        llm_mod.set_current_user(LAOYU, session_id="s-A")
        assert uc.resolve_user("s-A") == LAOYU
        assert uc.resolve_user("s-B") == "default", "绑定泄漏到了别的会话"

    def test_empty_session_id_falls_back_to_process_path(self, monkeypatch):
        """`session_id=""` 必须等价于"没传"，走旧路径（API 默认值就是空串）。"""
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
        llm_mod.set_current_user(LAOYU, session_id="")
        assert os.environ.get("CHARLIE_USER_ID") == LAOYU
        assert llm_mod.get_current_user() == LAOYU

    def test_process_path_keeps_legacy_behavior(self, monkeypatch):
        """不传 `session_id` 时行为不变：写 env + 更新 llm_state + 同步 history 静态导出。"""
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
        llm_mod.set_current_user(LAOYU)
        assert os.environ["CHARLIE_USER_ID"] == LAOYU
        assert llm_mod._st.current_user_id == LAOYU
        assert llm_mod.get_current_user() == LAOYU
        # history 的静态导出必须与动态解析一致（否则两处路径分叉）
        assert hist_mod.HISTORY_FILE == hist_mod._get_history_file()

    def test_session_binding_then_data_layer_sees_it(self):
        """兼容层的落点：绑定后数据层（roles / preferences）真的按新用户走。"""
        llm_mod.set_current_user(LAOYU, session_id="dev-kitchen")
        with uc.bind_session_user("dev-kitchen"):
            pref_mod.set_preference("current_role", "jarvis")
            assert roles_mod.get_current_role() == "jarvis"
        assert roles_mod.get_current_role() == "charlie"


# ======================================================================
# 4. API：/api/user/switch 的可选 session_id
# ======================================================================

class TestUserSwitchApi:
    def test_session_switch_marks_scope_session(self, monkeypatch):
        from app.routes import manage
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
        r = asyncio.run(manage.switch_user_api(user_id=LAOYU, session_id="s-1"))
        assert r["scope"] == "session"
        assert r["session_id"] == "s-1"
        assert "warning" not in r
        assert uc.resolve_user("s-1") == LAOYU
        assert "CHARLIE_USER_ID" not in os.environ

    def test_process_switch_marks_scope_process_and_warns(self, monkeypatch):
        from app.routes import manage
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
        r = asyncio.run(manage.switch_user_api(user_id=LAOYU))
        assert r["scope"] == "process"
        assert "warning" in r and "会话" in r["warning"]
        assert os.environ.get("CHARLIE_USER_ID") == LAOYU

    def test_default_session_id_is_empty_string(self):
        """路由签名默认值必须是空串（等价于"没传"），否则会误走会话分支。"""
        import inspect
        from app.routes import manage
        sig = inspect.signature(manage.switch_user_api)
        assert sig.parameters["session_id"].default == ""


# ======================================================================
# 5. ContextVar 跨线程池传播（批次 5 的风险最高项）
# ======================================================================

class TestContextVarPropagation:
    def test_asyncio_to_thread_propagates(self):
        """钉住前提假设：`asyncio.to_thread` **会**拷上下文（CPython ≥3.9 内部实现）。

        `app/routes/conversation.py` / `websocket.py` / `feishu.py` 大量使用它，
        它们能拿到正确身份全靠这一条。若哪天这个前提变了，本用例会红。
        """
        async def main():
            with uc.bind_user(LAOYU):
                return await asyncio.to_thread(uc.resolve_user)
        assert asyncio.run(main()) == LAOYU

    def test_run_in_executor_does_not_propagate(self, monkeypatch):
        """钉住陷阱：裸 `loop.run_in_executor` **不**继承 ContextVar。"""
        monkeypatch.delenv("CHARLIE_USER_ID", raising=False)

        async def main():
            loop = asyncio.get_running_loop()
            with uc.bind_user(LAOYU):
                return await loop.run_in_executor(None, uc.resolve_user)
        assert asyncio.run(main()) != LAOYU, \
            "前提变了：run_in_executor 现在会传播上下文？"

    def test_ctx_run_in_executor_propagates(self):
        from app.xiaozhi_ws import _ctx_run_in_executor

        async def main():
            loop = asyncio.get_running_loop()
            with uc.bind_user(LAOYU):
                return await _ctx_run_in_executor(loop, None, uc.resolve_user)
        assert asyncio.run(main()) == LAOYU

    def test_ctx_run_in_executor_passes_args(self):
        from app.xiaozhi_ws import _ctx_run_in_executor

        async def main():
            loop = asyncio.get_running_loop()
            return await _ctx_run_in_executor(loop, None, lambda a, b: a + b, 2, 3)
        assert asyncio.run(main()) == 5

    def test_ctx_run_in_executor_isolates_per_call(self):
        """每次调用拷一份新上下文 —— 两个用户并发不能互相污染。"""
        from app.xiaozhi_ws import _ctx_run_in_executor

        async def main():
            loop = asyncio.get_running_loop()

            async def one(uid):
                with uc.bind_user(uid):
                    return await _ctx_run_in_executor(loop, None, uc.resolve_user)

            return await asyncio.gather(one(LAOYU), one(JIAREN))
        assert asyncio.run(main()) == [LAOYU, JIAREN]

    def test_tts_voice_resolves_per_uid_inside_ctx_executor(self):
        """端到端：TTS 合成线程里能解析出**当前 uid** 的角色音色。

        这条覆盖的就是"妈妈听到老于的音色"那个症状：`_synth_mp3` →
        `get_effective_tts_config()` → `roles.get_role_tts_config()` 全在
        线程池里跑。
        """
        from app.xiaozhi_ws import _ctx_run_in_executor

        with uc.bind_user(LAOYU):
            pref_mod.set_preference("current_role", "jarvis")
            roles_mod.set_role_tts_voice("jarvis", "Nova")
        roles_mod._current_role_by_uid.clear()
        roles_mod._voice_overrides.clear()

        async def main():
            loop = asyncio.get_running_loop()

            async def voice_of(uid):
                with uc.bind_user(uid):
                    return await _ctx_run_in_executor(
                        loop, None, lambda: roles_mod.get_role_tts_config()["voice"])

            return await asyncio.gather(voice_of(LAOYU), voice_of(JIAREN))
        assert asyncio.run(main()) == ["Nova", "Ethan"]

    def test_consumer_side_synth_never_uses_bare_run_in_executor(self):
        """AST 级守卫：消费侧调 `_synth_mp3` 的地方必须走 `_ctx_run_in_executor`。

        主回复路径由 `_collect_ctx.run(collect)` 覆盖（producer 线程内合成），
        但兜底语/罐头语（"抱歉，反应慢了"、"抱歉，音乐加载失败"）在**消费侧**
        合成 —— 用裸 `run_in_executor` 就会丢掉用户身份。
        """
        tree = ast.parse(_src("app/xiaozhi_ws.py"))
        bad = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if not (isinstance(f, ast.Attribute) and f.attr == "run_in_executor"):
                continue
            names = {a.id for a in ast.walk(n) if isinstance(a, ast.Name)}
            if "_synth_mp3" in names:
                bad.append(n.lineno)
        assert bad == [], \
            f"app/xiaozhi_ws.py 第 {bad} 行的 _synth_mp3 仍在裸 run_in_executor 里（丢用户身份）"

    def test_async_inproc_tool_keeps_identity(self):
        """`is_async` 的进程内工具经 ThreadPoolExecutor 也必须保住用户身份。

        ⚠️ 当前**没有任何** magic-* 工具是 `async def`，所以这条分支现在是死路 ——
        但它是**潜伏陷阱**：谁哪天把一个读用户数据的工具改成 async，就会静默
        落到别人头上（不报错，只是串台）。本用例把它封死。
        """
        from agent import inproc_tools
        seen = {}

        async def _probe() -> str:
            seen["uid"] = uc.resolve_user()
            return "ok"

        cls = inproc_tools._make_tool_class(
            "probe_tool", "d", _EMPTY_SCHEMA, _probe, True)
        inst = cls(cfg={})

        async def main():
            with uc.bind_user(LAOYU):
                return inst.call({})

        assert asyncio.run(main()) == "ok"
        assert seen.get("uid") == LAOYU, \
            "异步工具丢了用户身份 ⇒ 数据会落到别人头上"

    def test_sync_inproc_tool_keeps_identity(self):
        """对照：同步工具在同线程内调用，天然继承（确认基线没被改坏）。"""
        from agent import inproc_tools
        seen = {}

        def _probe() -> str:
            seen["uid"] = uc.resolve_user()
            return "ok"

        cls = inproc_tools._make_tool_class(
            "probe_sync", "d", _EMPTY_SCHEMA, _probe, False)
        inst = cls(cfg={})
        with uc.bind_user(LAOYU):
            assert inst.call({}) == "ok"
        assert seen.get("uid") == LAOYU

    def test_inproc_async_branch_has_copy_context(self):
        """AST 级守卫：`_make_tool_class` 的 async 分支必须显式拷上下文。"""
        tree = ast.parse(_src("agent/inproc_tools.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_make_tool_class")
        calls = {getattr(n.func, "attr", None)
                 for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "copy_context" in calls, \
            "inproc_tools 的 async 分支没有 copy_context ⇒ 异步工具丢用户身份"


# ======================================================================
# 6. persona 人格段必须按 uid 隔离（不能进 static 段 / 共享 brain）
# ======================================================================

class TestPersonaScopeInSystemMsg:
    """`persona_state_{uid}.json` 里的 mood / relationship_level 是**用户态**。

    ⚠️ 这批用例封的是一个**真 bug**（本会话新发现，与角色/音色串台同族）：

    persona 段原本拼在 `_build_system_msg_static()` 末尾，而 static 段：
    1. 被 `(mcp_set, role)` 缓存（`_SYSTEM_MSG_TTL = 300`）—— 键里**不含 uid**；
    2. 被写进 `brain.system_message`（`agent/llm.py:952`），而 brain 本身按
       mcp_set 缓存（`_st.brains[mcp_set]`）。

    于是"**谁先说话谁的人格被烤进共享 brain**"：5 分钟 TTL 内妈妈会拿到老于的
    心情与亲密度。这也违反了批次 3A 自己立的"static 段与 user 无关"不变量。

    修法：把 persona 段移到 `_build_system_msg_dynamic(uid)` —— 那里 uid 按会话
    正确解析，且每轮实时拼（顺带修掉 persona 变化后最长 5 分钟不生效的陈旧问题）。
    """

    @staticmethod
    def _calls_in(fn_name: str) -> set:
        """某个函数体内**直接出现**的被调用名集合（AST，不命中注释）。"""
        tree = ast.parse(_src("agent/system_msg.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        out = set()
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        return out

    _PERSONA_FNS = {"get_persona_prompt", "contextual_response_style"}

    def test_static_segment_does_not_call_persona(self):
        bad = self._calls_in("_build_system_msg_static") & self._PERSONA_FNS
        assert not bad, (
            f"static 段仍在拼 persona（{sorted(bad)}）⇒ 它会被 (mcp_set, role) 缓存 "
            "并烤进共享 brain，变成'谁先说话谁的人格'"
        )

    def test_dynamic_segment_calls_persona(self):
        calls = self._calls_in("_build_system_msg_dynamic")
        assert self._PERSONA_FNS <= calls, (
            "persona 段既不在 static 也不在 dynamic ⇒ 人格注入整体丢失"
        )

    def test_static_segment_identical_across_uids(self):
        """不变量：static 段与 user 无关 —— 两个 uid 构建结果必须逐字节相同。

        ⚠️ 每次构建前必须 `invalidate_system_msg_cache()`。不清缓存的话第二次会
        直接命中缓存返回第一次的结果，**带 bug 的代码也会通过**（变异实测）。
        """
        with uc.bind_user(LAOYU):
            persona_mod.set_relationship_level("family")
            persona_mod.update("太开心了！")
        sysmsg_mod.invalidate_system_msg_cache()
        with uc.bind_user(LAOYU):
            a = sysmsg_mod._build_system_msg_static("none")
        sysmsg_mod.invalidate_system_msg_cache()
        with uc.bind_user(JIAREN):
            b = sysmsg_mod._build_system_msg_static("none")
        assert a == b, "static 段随用户变了 ⇒ 它不该进 (mcp_set, role) 缓存"

    def test_dynamic_persona_differs_per_uid(self):
        with uc.bind_user(LAOYU):
            persona_mod.set_relationship_level("family")
        with uc.bind_user(JIAREN):
            persona_mod.set_relationship_level("stranger")
        with uc.bind_user(LAOYU):
            dyn_l = sysmsg_mod._build_system_msg_dynamic("s-laoyu", LAOYU)
        with uc.bind_user(JIAREN):
            dyn_j = sysmsg_mod._build_system_msg_dynamic("s-jiaren1", JIAREN)
        assert "像家人一样" in dyn_l, dyn_l[:200]
        assert "像家人一样" not in dyn_j, "老于的亲密度泄漏给了家人"
        assert "保持礼貌和距离感" in dyn_j, dyn_j[:200]

    def test_dynamic_persona_honours_explicit_uid(self):
        """显式传 uid 时不得回落到"当前会话"的身份。"""
        with uc.bind_user(LAOYU):
            persona_mod.set_relationship_level("family")
        # 当前身份是家人，但显式要老于的 persona
        with uc.bind_user(JIAREN):
            dyn = sysmsg_mod._build_system_msg_dynamic("s-x", LAOYU)
        assert "像家人一样" in dyn, dyn[:200]

    def test_persona_absent_on_cache_hit_path(self):
        """缓存命中路径也不能冒出 persona（static 段第二次构建走缓存）。"""
        sysmsg_mod.invalidate_system_msg_cache()
        with uc.bind_user(LAOYU):
            persona_mod.set_relationship_level("family")
            first = sysmsg_mod._build_system_msg_static("none")    # 填缓存
            second = sysmsg_mod._build_system_msg_static("none")   # 走缓存
        assert first == second
        assert "像家人一样" not in second


# ======================================================================
# 7. 上下文里的「今日待办」必须按 uid 过滤（不能走共享缓存）
# ======================================================================

class TestContextTodosPerUid:
    """`reminders.json` 是**单一共享文件**（FR-14 有意不分片），隔离靠读取时按 owner 过滤。

    ⚠️ 这批用例封的是一个**真 bug**：`agent/context.py` 的 60 秒共享上下文缓存
    （`_context_cache`）由**后台线程** `_refresh_context_cache()` 填充，而它调的是
    **不过滤**的 `_load_reminders()` —— 算出来的 `todos` 是"某一个人"的今日待办，
    再被注入到**所有**会话的 system message（妈妈会看到老于的今日待办）。

    修法：`todos` 不进共享缓存，由 `_override_todos()` 在读取时按会话 uid 现算
    （与既有的 `_override_recent()` 同一套路）。

    ⚠️ `location` / `weather` **刻意保持共享**：一家人同一个房子、同一份天气，
    按会话重算意味着每会话一次网络请求。这是取舍，不是遗漏。
    """

    @staticmethod
    def _write_reminders(monkeypatch, tmp_path, items):
        """把提醒写到临时 reminders.json 并把 `app.reminders` 的路径常量指过去。

        ⚠️ `app.reminders.REMINDERS_FILE` 是**导入期**常量（`reminders.py:26`），
        只设 `ASSISTANT_KID_DATA_DIR` 不生效 —— `_read_locked_reminders()` 直接用它。
        """
        from app import reminders as rm
        d = str(tmp_path)
        os.makedirs(d, exist_ok=True)
        rfile = os.path.join(d, "reminders.json")
        with open(rfile, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        monkeypatch.setattr(rm, "REMINDERS_FILE", rfile)
        monkeypatch.setattr(rm, "REMINDERS_LOCK_FILE", rfile + ".lock")
        monkeypatch.setattr(rm, "SCHEDULER_LOCK_FILE", rfile + ".scheduler.lock")
        return rfile

    @staticmethod
    def _no_network(monkeypatch):
        """后台刷新线程会打网络（定位 + 天气），测试里掐掉。"""
        monkeypatch.setattr("agent.weather.direct_weather_play", lambda *a, **k: "")
        monkeypatch.setattr("app.geo.locate_text", lambda *a, **k: "")

    def test_get_context_todos_are_filtered_by_uid(self, monkeypatch, tmp_path):
        import agent.context as ctx_mod
        self._no_network(monkeypatch)
        today = datetime.date.today().isoformat()
        self._write_reminders(monkeypatch, tmp_path, [
            {"text": "老于的会", "due": f"{today}T10:00:00", "done": False, "owner": LAOYU},
            {"text": "家人的事", "due": f"{today}T11:00:00", "done": False, "owner": JIAREN},
            {"text": "没主的旧提醒", "due": f"{today}T12:00:00", "done": False},
        ])
        ctx_mod.invalidate_context_cache()
        c_l = ctx_mod.get_context("s-l", LAOYU)
        c_j = ctx_mod.get_context("s-j", JIAREN)
        assert c_l["todos"] == ["老于的会"], c_l["todos"]
        assert c_j["todos"] == ["家人的事"], c_j["todos"]

    def test_cached_todos_are_overridden_on_read(self, monkeypatch, tmp_path):
        """**确定性**地覆盖缓存命中路径：直接往共享缓存里塞别人的待办。

        不用"先 get_context 再 get_context"来撞缓存 —— 后台刷新线程是 daemon，
        时序不确定，那样写会变成偶发失败。
        """
        import agent.context as ctx_mod
        today = datetime.date.today().isoformat()
        self._write_reminders(monkeypatch, tmp_path, [
            {"text": "家人的事", "due": f"{today}T11:00:00", "done": False, "owner": JIAREN},
        ])
        with ctx_mod._context_cache_lock:
            ctx_mod._context_cache = {"todos": ["老于的会"], "recent_conversation": [],
                                      "time": {}, "location": "", "weather": ""}
            ctx_mod._context_cache_ts = time.time()
        c = ctx_mod.get_context("s-j", JIAREN)
        assert "老于的会" not in c["todos"], "共享缓存里的别人的待办被原样返回了"
        assert c["todos"] == ["家人的事"]

    def test_refresh_cache_never_computes_todos(self):
        """后台刷新线程不得算 todos —— 它没有用户上下文，算出来必然是别人的。"""
        tree = ast.parse(_src("agent/context.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_refresh_context_cache")
        names = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                 for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "_load_reminders" not in names, \
            "刷新线程又用全局视图 _load_reminders() 算 todos ⇒ 会注入给所有会话"
        assert "list_reminders_for" not in names, \
            "刷新线程不得算 todos（它没有用户上下文，结果会被共享缓存固化）"

    def test_override_todos_uses_user_facing_reader(self):
        tree = ast.parse(_src("agent/context.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_override_todos")
        names = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                 for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "list_reminders_for" in names, \
            "_override_todos 必须走用户面读取入口 list_reminders_for(uid)"

    def test_format_context_for_prompt_forwards_uid(self):
        tree = ast.parse(_src("agent/context.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "format_context_for_prompt")
        kw = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "get_context":
                kw |= {k.arg for k in n.keywords}
        assert "uid" in kw, \
            "format_context_for_prompt 没把 uid 透给 get_context ⇒ 待办仍按 env 身份算"

    def test_system_msg_dynamic_passes_uid_to_context(self):
        """调用链最后一跳：`_build_system_msg_dynamic(uid)` 必须把 uid 传下去。"""
        tree = ast.parse(_src("agent/system_msg.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_build_system_msg_dynamic")
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "format_context_for_prompt"):
                kw = {k.arg for k in n.keywords}
                assert "uid" in kw, \
                    "没把 uid 传给 format_context_for_prompt ⇒ 上下文按 env 身份算"
                return
        pytest.fail("_build_system_msg_dynamic 里找不到 format_context_for_prompt 调用")
