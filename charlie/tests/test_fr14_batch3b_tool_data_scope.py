"""FR-14 批次 3B 回归：**工具层与数据层的 uid 归属**。

方案依据：``docs/PLAN_FR14_MULTI_USER.md`` §3B

**背景**：批次 3A 把"用户/会话相关内容"从共享的 ``brain.system_message`` 移出，
但**工具层**（``magic-*.py`` 的 MCP 工具函数）与**管理 API**（``app/routes/manage.py``）
仍然按**进程级**身份找数据文件：

- ``magic-memory.py`` / ``magic-evolution.py`` / ``magic-summary.py`` /
  ``magic-habits.py`` 全都直接读 ``os.environ["CHARLIE_USER_ID"]`` 或干脆硬编码
  ``episodic_memories.json``；
- ``MEMORY_FILE`` / ``EVOLUTION_FILE`` / ``HABITS_FILE`` 是**导入期快照**，
  一旦定义就把"谁在用"冻结成进程启动那一刻的身份；
- ``magic-memory.py`` 的 semantic 记忆路径缓存写成 ``_SEMANTIC_FILE = None``
  **单例**，第一次调用（进程早期、uid 恒为 ``default``）就把路径固化，
  之后**所有用户**读写同一个文件 —— 比"uid 解析读错"更隐蔽；
- ``app/routes/manage.py`` 的数据目录写成 ``os.path.join(PROJECT_DIR, "data", ...)``，
  而 ``PROJECT_DIR`` 是 ``charlie/`` **子目录**，真实数据在仓库根的
  ``ASSISTANT_KID_DATA_DIR`` ⇒ ``/api/habits`` 永远返回"暂无习惯数据"，
  ``/api/behaviors`` 与 ``/api/decisions/config`` 的反馈读写**全部落空**。

改造后：uid 统一走 ``agent.user_context.resolve_user()``（ContextVar → session
注册表 → env 兜底）；文件按 uid 分片且 ``default`` 保留原名（**老数据零迁移**）；
动态属性改 PEP 562 ``__getattr__``；semantic 缓存按 uid 分键；管理 API 走
``ASSISTANT_KID_DATA_DIR``。

⚠️ **本文件里的源码级断言一律用 AST 而不是 ``str.index``**。本项目的注释/文档
字符串特别爱把被断言的符号名原样写出来（例如 ``manage._data_dir`` 的 docstring 里
就写着 ``os.path.join(PROJECT_DIR, "data", ...)``），用 ``str.index`` 会命中注释
或 docstring，断言变成**永真** —— ``test_lite_cancel_watchdog.py`` 已经踩过一次。
AST 完全看不到注释，字符串字面量也能精确比对，没有这个隐患。
"""
import ast
import asyncio
import importlib.util
import json
import os
import sys
from itertools import count

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from agent import user_context as uc

LAOYU = "laoyu"
JIAREN = "jiaren1"


# ======================================================================
# 助手
# ======================================================================

_LOAD_SEQ = count()


def _load(filename: str):
    """从文件路径加载 ``magic-*.py``（文件名带 ``-``，不能普通 import）。

    ⚠️ 每次用**唯一模块名**加载，不缓存。这些模块的 ``DATA_DIR`` 是**导入期**从
    ``os.environ`` 读的模块常量，缓存复用会把上一个用例的临时目录泄漏进来。
    加载后由 fixture 统一 ``monkeypatch.setattr(mod, "DATA_DIR", tmp)``。
    """
    name = f"_fr14b3b_{os.path.splitext(filename)[0].replace('-', '_')}_{next(_LOAD_SEQ)}"
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        pass
    return mod


def _src(filename: str) -> str:
    with open(os.path.join(PROJECT_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


def _tree(filename: str) -> ast.Module:
    return ast.parse(_src(filename))


def _string_constants(tree: ast.AST) -> set:
    """模块里出现过的全部**字符串字面量**（注释不在 AST 里，docstring 是整段长串）。"""
    return {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _calls_named(tree: ast.AST, func_name: str) -> list:
    """收集所有形如 ``func_name(...)`` 的调用节点。"""
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == func_name:
            out.append(n)
        elif isinstance(f, ast.Attribute) and f.attr == func_name:
            out.append(n)
    return out


def _find_project_dir_data_joins(filename: str) -> list:
    """AST 扫描 ``os.path.join(PROJECT_DIR, "data", ...)`` 调用，返回行号列表。

    这是批次 3B 修掉的那个**真 bug** 的守卫：注释与 docstring 里都会出现这个
    写法（用来解释"为什么不能这么写"），所以只能靠 AST 判定。
    """
    hits = []
    for n in ast.walk(_tree(filename)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == "join"):
            continue
        if len(n.args) < 2:
            continue
        a0, a1 = n.args[0], n.args[1]
        if (isinstance(a0, ast.Name) and a0.id == "PROJECT_DIR"
                and isinstance(a1, ast.Constant) and a1.value == "data"):
            hits.append(n.lineno)
    return hits


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _today() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d")


# ======================================================================
# fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """数据目录指向临时目录 + 清 session 注册表 + 清 env 身份。

    ⚠️ ``monkeypatch.delenv("CHARLIE_USER_ID")`` 是必须的：本机/CI 上这个变量
    可能被别的用例设过，留着会让"未绑定 → default"的断言随机失败。
    """
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
    uc.clear_all_session_users()
    yield
    uc.clear_all_session_users()


@pytest.fixture
def mem_mod(tmp_path, monkeypatch):
    mod = _load("magic-memory.py")
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return mod


@pytest.fixture
def evo_mod(tmp_path, monkeypatch):
    mod = _load("magic-evolution.py")
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return mod


@pytest.fixture
def hab_mod(tmp_path, monkeypatch):
    mod = _load("magic-habits.py")
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return mod


@pytest.fixture
def sum_mod(tmp_path, monkeypatch):
    mod = _load("magic-summary.py")
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return mod


@pytest.fixture
def manage(tmp_path, monkeypatch):
    from app.routes import manage as _m
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(tmp_path))
    return _m


# ======================================================================
# 1. 路径层：按 uid 分片，default 保留原名（老数据零迁移）
# ======================================================================

class TestPathSharding:
    def test_memory_episodic_path_shards_by_uid(self, mem_mod, tmp_path):
        """uid → 文件名后缀；不同 uid 路径互不相同、同一 uid 稳定。"""
        d = str(tmp_path)
        assert mem_mod._user_file("episodic_memories") == os.path.join(d, "episodic_memories.json")
        with uc.bind_user(LAOYU):
            p1 = mem_mod._user_file("episodic_memories")
            p2 = mem_mod._user_file("episodic_memories")
        with uc.bind_user(JIAREN):
            p3 = mem_mod._user_file("episodic_memories")
        assert p1 == os.path.join(d, "episodic_memories_laoyu.json")
        assert p3 == os.path.join(d, "episodic_memories_jiaren1.json")
        assert p1 == p2, "同一 uid 两次解析必须一致"
        assert p1 != p3, "不同 uid 必须落到不同文件"

    def test_evolution_path_shards_by_uid(self, evo_mod, tmp_path):
        d = str(tmp_path)
        assert evo_mod._get_evolution_file() == os.path.join(d, "evolution_data.json")
        with uc.bind_user(LAOYU):
            assert evo_mod._get_evolution_file() == os.path.join(d, "evolution_data_laoyu.json")

    def test_habits_path_shards_by_uid(self, hab_mod, tmp_path):
        d = str(tmp_path)
        assert hab_mod._get_habits_file() == os.path.join(d, "habits.json")
        with uc.bind_user(LAOYU):
            assert hab_mod._get_habits_file() == os.path.join(d, "habits_laoyu.json")

    def test_summary_user_file_shards_by_uid(self, sum_mod, tmp_path):
        d = str(tmp_path)
        assert sum_mod._user_file("episodic_memories") == os.path.join(d, "episodic_memories.json")
        with uc.bind_user(LAOYU):
            assert sum_mod._user_file("episodic_memories") == \
                os.path.join(d, "episodic_memories_laoyu.json")

    def test_default_never_gets_suffix(self, mem_mod, evo_mod, hab_mod, sum_mod):
        """``default`` 必须保留改造前的**原文件名** —— 老数据不用迁移。

        回归对象：若有人把规则写成 ``f"{basename}_{uid}.json"`` 无差别加后缀，
        线上所有老数据会"凭空消失"（表现为"我的记忆全没了"），这是最贵的错误。
        """
        with uc.bind_user("default"):
            paths = [
                mem_mod._user_file("episodic_memories"),
                evo_mod._get_evolution_file(),
                hab_mod._get_habits_file(),
                sum_mod._user_file("episodic_memories"),
            ]
        for p in paths:
            assert "_default" not in os.path.basename(p), \
                f"default 被加了后缀（老数据会凭空消失）: {p}"

    def test_empty_uid_is_treated_as_default(self, mem_mod):
        """``bind_user(None)`` / 空串 ⇒ default（不是 ``_`` 后缀的怪文件名）。"""
        with uc.bind_user(None):
            p = mem_mod._user_file("episodic_memories")
        assert p.endswith("episodic_memories.json")
        with uc.bind_user(""):
            p2 = mem_mod._user_file("episodic_memories")
        assert p2.endswith("episodic_memories.json")


# ======================================================================
# 2. PEP 562 动态属性：不能退回导入期快照
# ======================================================================

class TestDynamicModuleAttrs:
    def test_memory_file_is_not_in_module_dict(self, mem_mod):
        """``MEMORY_FILE`` 必须由 ``__getattr__`` 提供，**不在** ``__dict__`` 里。

        这是"导入期快照"最干净、最不脆弱的判据：一旦有人改回
        ``MEMORY_FILE = _get_memory_file()``，这个名字就会出现在模块 ``__dict__``，
        本断言立刻失败；而它又完全不依赖注释排版（不需要剥注释）。
        """
        assert "MEMORY_FILE" not in vars(mem_mod)
        assert mem_mod.MEMORY_FILE.endswith("episodic_memories.json")

    def test_evolution_file_is_not_in_module_dict(self, evo_mod):
        assert "EVOLUTION_FILE" not in vars(evo_mod)
        assert evo_mod.EVOLUTION_FILE.endswith("evolution_data.json")

    def test_habits_file_is_not_in_module_dict(self, hab_mod):
        assert "HABITS_FILE" not in vars(hab_mod)
        assert hab_mod.HABITS_FILE.endswith("habits.json")

    def test_memory_file_follows_binding(self, mem_mod):
        """同一进程内切换身份，``MEMORY_FILE`` 必须跟着变（快照则不变）。"""
        with uc.bind_user(LAOYU):
            a = mem_mod.MEMORY_FILE
        with uc.bind_user(JIAREN):
            b = mem_mod.MEMORY_FILE
        assert a.endswith("episodic_memories_laoyu.json")
        assert b.endswith("episodic_memories_jiaren1.json")
        assert a != b

    def test_evolution_file_follows_binding(self, evo_mod):
        with uc.bind_user(LAOYU):
            a = evo_mod.EVOLUTION_FILE
        with uc.bind_user(JIAREN):
            b = evo_mod.EVOLUTION_FILE
        assert a != b
        assert "evolution_data_laoyu.json" in a and "evolution_data_jiaren1.json" in b

    def test_habits_file_follows_binding(self, hab_mod):
        with uc.bind_user(LAOYU):
            a = hab_mod.HABITS_FILE
        with uc.bind_user(JIAREN):
            b = hab_mod.HABITS_FILE
        assert a != b
        assert "habits_laoyu.json" in a and "habits_jiaren1.json" in b

    def test_unknown_attr_raises_attribute_error(self, mem_mod, evo_mod, hab_mod):
        """``__getattr__`` 兜底必须抛 ``AttributeError``（不是 ``NameError``）。

        ⚠️ 一旦定义了模块级 ``__getattr__``，模块内**只读不写**的全局名会走它，
        拼错的名字会变成 ``AttributeError`` 而不是 ``NameError`` —— 这里把行为钉住，
        顺带保证拼写错误能被 ``hasattr`` / ``getattr(..., default)`` 正确识别。
        """
        for mod in (mem_mod, evo_mod, hab_mod):
            with pytest.raises(AttributeError) as ei:
                _ = mod.THIS_NAME_DOES_NOT_EXIST
            assert mod.__name__ in str(ei.value)
            assert not hasattr(mod, "THIS_NAME_DOES_NOT_EXIST")


# ======================================================================
# 3. 身份解析优先级（ContextVar → session → env）
# ======================================================================

class TestIdentityResolution:
    def test_contextvar_beats_env(self, mem_mod, monkeypatch):
        monkeypatch.setenv("CHARLIE_USER_ID", "from_env")
        with uc.bind_user(LAOYU):
            assert mem_mod._resolve_uid() == LAOYU
            assert "laoyu" in mem_mod._user_file("episodic_memories")
        # 退出绑定后回落到 env
        assert mem_mod._resolve_uid() == "from_env"

    def test_env_is_fallback_only(self, mem_mod, monkeypatch):
        """env 只作兜底（独立以 stdio 启动模块时用）。"""
        monkeypatch.setenv("CHARLIE_USER_ID", "from_env")
        assert mem_mod._resolve_uid() == "from_env"
        assert mem_mod._user_file("episodic_memories").endswith(
            "episodic_memories_from_env.json")

    def test_unset_env_falls_back_to_default(self, mem_mod):
        assert mem_mod._resolve_uid() == "default"

    def test_nested_bind_restores_previous(self, mem_mod):
        """嵌套 ``bind_user`` 必须逐层恢复（ContextVar token 语义）。"""
        with uc.bind_user(LAOYU):
            assert mem_mod._resolve_uid() == LAOYU
            with uc.bind_user(JIAREN):
                assert mem_mod._resolve_uid() == JIAREN
            assert mem_mod._resolve_uid() == LAOYU, "退出内层后必须回到外层身份"
        assert mem_mod._resolve_uid() == "default"

    def test_session_registry_alone_does_not_affect_data_layer(self, mem_mod):
        """**已知限制（有意钉住）**：数据层拿不到 ``session_id``。

        ``set_session_user("s1", "laoyu")`` 只影响 ``resolve_user("s1")``；
        ``magic-*`` 里的 ``_resolve_uid()`` 调的是 ``resolve_user()``（无参），
        所以**看不到** session 注册表，仍回落到 env/default。

        这不是 bug，是批次 4/5 的接口约束：要按 session 解析，必须由调用方把
        ``resolve_user(session_id)`` 的结果显式传下来（或先在入口 ``bind_user``）。
        本用例把现状钉住 —— 将来有人改了这里，必须是有意为之并同步更新文档。
        """
        uc.set_session_user("s1", LAOYU)
        assert uc.resolve_user("s1") == LAOYU
        assert mem_mod._resolve_uid() == "default"
        # 但入口层先 bind 之后就能拿到（标准写法）
        with uc.bind_session_user("s1"):
            assert mem_mod._resolve_uid() == LAOYU

    def test_bind_session_user_uses_registry(self, mem_mod):
        uc.set_session_user("dev-kitchen", JIAREN)
        with uc.bind_session_user("dev-kitchen"):
            assert mem_mod._user_file("episodic_memories").endswith(
                "episodic_memories_jiaren1.json")


# ======================================================================
# 4. 往返隔离：写进去的别人读不到
# ======================================================================

class TestRoundTripIsolation:
    def test_episodic_write_read_isolation(self, mem_mod):
        """以 laoyu 写入 → laoyu 读得到、jiaren1 读不到、default 读不到。"""
        with uc.bind_user(LAOYU):
            mem_mod._save_memories([{"summary": "LAOYU_SECRET",
                                     "datetime": "2026-09-17T10:00:00"}])
            got = mem_mod._load_memories()
            shard = mem_mod._user_file("episodic_memories")
        assert any(m.get("summary") == "LAOYU_SECRET" for m in got)
        assert shard.endswith("episodic_memories_laoyu.json")
        assert os.path.exists(shard), "必须真的落盘到 uid 分片"
        with uc.bind_user(JIAREN):
            assert not any(m.get("summary") == "LAOYU_SECRET" for m in mem_mod._load_memories())
        assert not any(m.get("summary") == "LAOYU_SECRET" for m in mem_mod._load_memories())

    def test_habits_write_read_isolation(self, hab_mod):
        with uc.bind_user(LAOYU):
            hab_mod.log_habit("喝水")
            assert "喝水" in hab_mod._load_habits()
        with uc.bind_user(JIAREN):
            assert "喝水" not in hab_mod._load_habits()
        assert "喝水" not in hab_mod._load_habits()

    def test_evolution_write_read_isolation(self, evo_mod):
        """进化数据按 uid 分片：老于的"常用功能"不能进妈妈的画像。"""
        with uc.bind_user(LAOYU):
            evo_mod._save_evolution_data({"learned_preferences": {"music": 7}})
            assert evo_mod._load_evolution_data().get("learned_preferences") == {"music": 7}
        with uc.bind_user(JIAREN):
            assert evo_mod._load_evolution_data().get("learned_preferences", {}) != {"music": 7}


# ======================================================================
# 5. semantic 记忆路径缓存：按 uid 分键，不是单例
# ======================================================================

class TestSemanticCachePerUid:
    def test_cache_is_a_dict_not_a_singleton(self, mem_mod):
        """原实现 ``_SEMANTIC_FILE = None`` + ``if _SEMANTIC_FILE is None`` 是单例。

        改法的关键是"容器必须能装多个 uid"，所以断言容器类型是 ``dict``。
        """
        assert isinstance(mem_mod._SEMANTIC_FILES, dict)
        assert not hasattr(mem_mod, "_SEMANTIC_FILE"), \
            "旧的单例变量必须彻底删掉，留着容易被人误用回去"

    def test_path_differs_per_uid(self, mem_mod, tmp_path):
        with uc.bind_user(LAOYU):
            a = mem_mod._get_semantic_file()
        with uc.bind_user(JIAREN):
            b = mem_mod._get_semantic_file()
        assert a.endswith("semantic_memories_laoyu.json")
        assert b.endswith("semantic_memories_jiaren1.json")
        assert a != b

    def test_cache_keys_by_uid_and_is_stable(self, mem_mod):
        with uc.bind_user(LAOYU):
            a1 = mem_mod._get_semantic_file()
        with uc.bind_user(JIAREN):
            mem_mod._get_semantic_file()
        with uc.bind_user(LAOYU):
            a2 = mem_mod._get_semantic_file()
        assert a1 == a2, "同一 uid 第二次必须命中缓存且返回同一路径"
        assert {LAOYU, JIAREN} <= set(mem_mod._SEMANTIC_FILES), \
            "缓存必须以 uid 为键，且两个 uid 各占一项"

    def test_semantic_roundtrip_isolation(self, mem_mod):
        with uc.bind_user(LAOYU):
            mem_mod._save_semantic_memories([{"fact": "LAOYU_FACT"}])
            assert any(m.get("fact") == "LAOYU_FACT" for m in mem_mod._load_semantic_memories())
        with uc.bind_user(JIAREN):
            assert not any(m.get("fact") == "LAOYU_FACT" for m in mem_mod._load_semantic_memories())

    def test_semantic_path_lock_is_distinct_from_semantic_lock(self, mem_mod):
        """⚠️ 死锁守卫：``_load/_save_semantic_memories`` 都在**持有**
        ``_semantic_lock`` 时调 ``_get_semantic_file()``。若那里复用同一把
        非重入 ``Lock``，一调用就自锁死 —— 服务会整个卡住（比"串台"严重得多）。
        """
        assert mem_mod._semantic_path_lock is not mem_mod._semantic_lock

    def test_no_deadlock_on_first_semantic_access(self, mem_mod):
        """行为级验证：在超时保护下真的能返回（不能只靠"两把锁不是同一个对象"）。"""
        import threading
        done = threading.Event()

        def _run():
            mem_mod._get_semantic_file()
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert done.wait(timeout=5.0), "_get_semantic_file 疑似死锁（5s 未返回）"


# ======================================================================
# 6. 工具层接线：summary 不再硬编码 / llm 读分片
# ======================================================================

class TestToolLayerWiring:
    def test_summary_has_no_hardcoded_episodic_filename(self):
        """``magic-summary.py`` 里不能再用字面量 ``"episodic_memories.json"``。

        ⚠️ 用 AST 而不是 ``str.index``：本模块的说明性注释里**故意**写着
        ``episodic_memories.json`` 来解释历史 bug，``str.index`` 会命中注释。
        """
        lits = _string_constants(_tree("magic-summary.py"))
        assert "episodic_memories.json" not in lits, \
            "硬编码文件名回归 —— 多用户下任何用户都会读到 default 的记忆"

    def test_summary_calls_user_file_for_episodic(self):
        calls = _calls_named(_tree("magic-summary.py"), "_user_file")
        assert any(
            c.args and isinstance(c.args[0], ast.Constant)
            and c.args[0].value == "episodic_memories"
            for c in calls
        ), "magic-summary 必须通过 _user_file('episodic_memories') 取路径"

    def test_summary_daily_memories_respects_uid(self, sum_mod, monkeypatch, tmp_path):
        """行为级：``_get_daily_memories()`` 按 uid 取不同分片。

        为了让 JSON 回退分支生效，把 SQLite 优先路径打成空。
        """
        import agent.memory_store as ms
        monkeypatch.setattr(ms, "get_events", lambda **kw: [])

        today = _today()
        _write_json(os.path.join(str(tmp_path), "episodic_memories.json"),
                    [{"datetime": f"{today}T10:00:00", "summary": "DEFAULT_MEM"}])
        _write_json(os.path.join(str(tmp_path), "episodic_memories_laoyu.json"),
                    [{"datetime": f"{today}T11:00:00", "summary": "LAOYU_MEM"}])

        with uc.bind_user(LAOYU):
            got = sum_mod._get_daily_memories()
        assert "LAOYU_MEM" in got
        assert "DEFAULT_MEM" not in got

        got_default = sum_mod._get_daily_memories()
        assert "DEFAULT_MEM" in got_default
        assert "LAOYU_MEM" not in got_default

    def test_llm_max_sentences_reads_uid_shard(self, monkeypatch, tmp_path):
        """``agent.llm._get_max_sentences()`` 必须读当前 uid 的 evolution 分片。

        旧写法读 ``os.environ``：妈妈设了简洁模式，老于的回复也一起变短。
        """
        import agent.llm as llm_mod

        monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(tmp_path))
        _write_json(os.path.join(str(tmp_path), "evolution_data.json"),
                    {"adaptation_state": {"max_sentences": 9}})
        _write_json(os.path.join(str(tmp_path), "evolution_data_laoyu.json"),
                    {"adaptation_state": {"max_sentences": 3}})

        assert llm_mod._get_max_sentences() == 9
        with uc.bind_user(LAOYU):
            assert llm_mod._get_max_sentences() == 3

    def test_llm_max_sentences_does_not_read_env_identity(self):
        """源码级守卫：``_get_max_sentences`` 体内不得出现 ``CHARLIE_USER_ID``。"""
        src = _src("agent/llm.py")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_get_max_sentences")
        seg = ast.get_source_segment(src, fn) or ""
        # 注释不在 AST 里，但 get_source_segment 会带出注释 → 逐行剥掉行首注释
        code = "\n".join(
            ln for ln in seg.splitlines()
            if not ln.strip().startswith("#")
        )
        assert "CHARLIE_USER_ID" not in code
        assert "resolve_user" in code


# ======================================================================
# 7. 管理 API 的数据目录解析（真 bug 回归）
# ======================================================================

class TestManageDataDir:
    def test_data_dir_follows_env(self, manage, tmp_path, monkeypatch):
        assert manage._data_dir() == str(tmp_path)
        monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", "/tmp/other-dir")
        assert manage._data_dir() == "/tmp/other-dir"

    def test_data_dir_is_not_project_dir_data(self, manage, tmp_path):
        """**真 bug 回归**：``PROJECT_DIR`` 是 ``charlie/`` 子目录，不是仓库根。

        实测 ``charlie/data/`` 下一个 JSON 都没有，所以旧写法让
        ``/api/habits`` 永远"暂无习惯数据"、决策反馈读写全部落空。
        """
        assert manage._data_dir() != os.path.join(manage.PROJECT_DIR, "data")
        assert manage.PROJECT_DIR.endswith(os.sep + "charlie")

    def test_no_project_dir_data_join_left(self):
        """AST 级守卫：``manage.py`` 里不能残留 ``os.path.join(PROJECT_DIR, "data", ...)``。

        ⚠️ docstring 里**故意**保留了这个写法作反面教材，所以只能靠 AST 判定。
        """
        hits = _find_project_dir_data_joins("app/routes/manage.py")
        assert hits == [], f"manage.py 第 {hits} 行仍有 PROJECT_DIR/data 硬编码"

    def test_data_file_joins_data_dir(self, manage, tmp_path):
        assert manage._data_file("x.json") == os.path.join(str(tmp_path), "x.json")

    def test_decision_files_use_data_file_helper(self):
        """``/api/behaviors`` 与 ``/api/decisions/config`` 的反馈读写走 ``_data_file``。"""
        calls = _calls_named(_tree("app/routes/manage.py"), "_data_file")
        names = {
            c.args[0].value for c in calls
            if c.args and isinstance(c.args[0], ast.Constant)
        }
        assert "decision_feedback.json" in names
        assert "decision_history.json" in names

    def test_habits_file_for_naming(self, manage, tmp_path):
        d = str(tmp_path)
        assert manage._habits_file_for("") == os.path.join(d, "habits.json")
        assert manage._habits_file_for("default") == os.path.join(d, "habits.json")
        assert manage._habits_file_for(LAOYU) == os.path.join(d, "habits_laoyu.json")

    def test_habits_naming_aligned_with_magic_module(self, manage, hab_mod, tmp_path):
        """管理台与语音工具**必须**对同一 uid 得出同一文件名。

        两边不一致 = "语音记的习惯管理台看不到"（静默少数据，最难查）。
        """
        for uid in ("default", LAOYU, JIAREN):
            with uc.bind_user(uid):
                a = os.path.basename(manage._habits_file_for(uid))
                b = os.path.basename(hab_mod._get_habits_file())
            assert a == b, f"uid={uid} 命名不一致: manage={a} magic-habits={b}"


class TestHabitsShards:
    def test_shards_enumerates_and_sorts_default_first(self, manage, tmp_path):
        d = str(tmp_path)
        _write_json(os.path.join(d, "habits.json"), {})
        _write_json(os.path.join(d, "habits_laoyu.json"), {})
        _write_json(os.path.join(d, "habits_jiaren1.json"), {})
        # ⚠️ 必须放一个**字典序在 "default" 之前**的 uid（`aaa`）：否则
        # `sorted(key=lambda t: t[0])` 恰好也得到 default 在最前，排序规则
        # 无法被区分（变异测试实测该变异会存活）。
        _write_json(os.path.join(d, "habits_aaa.json"), {})
        _write_json(os.path.join(d, "reminders.json"), {})       # 无关文件
        with open(os.path.join(d, "habits_notes.txt"), "w") as f:
            f.write("x")                                          # 后缀不对
        _write_json(os.path.join(d, "habits_.json"), {})          # 空 uid → 不算

        shards = manage._habits_shards()
        assert [u for u, _ in shards] == ["default", "aaa", "jiaren1", "laoyu"], \
            "default 必须排最前，其余按 uid 字典序"

    def test_shards_missing_dir_returns_empty(self, manage, monkeypatch, tmp_path):
        monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", os.path.join(str(tmp_path), "nope"))
        assert manage._habits_shards() == []


class TestHabitsEndpoint:
    def _call(self, manage, user=""):
        return asyncio.run(manage.habits_status(user=user))

    def test_no_data_returns_empty_with_scope(self, manage):
        r = self._call(manage)
        assert r["total"] == 0 and r["habits"] == {}
        assert r["scope"] == "all"
        assert "暂无习惯数据" in r["insight"]

    def test_single_shard_keeps_plain_key(self, manage, tmp_path):
        """**老行为不变**：只有一个分片时键名不带 ``owner·`` 前缀。"""
        _write_json(os.path.join(str(tmp_path), "habits.json"),
                    {"喝水": {"logs": [_today()], "created": "2026-01-01"}})
        r = self._call(manage)
        assert r["total"] == 1
        assert list(r["habits"]) == ["喝水"]
        assert r["habits"]["喝水"]["owner"] == "default"
        assert r["scope"] == "all"

    def test_global_merge_prefixes_owner_on_collision(self, manage, tmp_path):
        """多分片时**同名**习惯（老于和妈妈都记了"喝水"）不能互相覆盖。

        ⚠️ 前缀只加在**真的冲突**的名字上：`跑步` 只有 laoyu 记过，保持原名。
        若按"分片数 > 1"无差别加前缀，用户会看到 `laoyu·跑步` 却找不到
        `default·跑步`，像是数据丢了。
        """
        d = str(tmp_path)
        _write_json(os.path.join(d, "habits.json"),
                    {"喝水": {"logs": [_today()]}})
        _write_json(os.path.join(d, "habits_laoyu.json"),
                    {"喝水": {"logs": [_today()]}, "跑步": {"logs": []}})
        r = self._call(manage)
        assert r["total"] == 3, "3 条（default·喝水 / laoyu·喝水 / 跑步）"
        assert set(r["habits"]) == {"default·喝水", "laoyu·喝水", "跑步"}
        assert r["habits"]["default·喝水"]["owner"] == "default"
        assert r["habits"]["laoyu·喝水"]["owner"] == "laoyu"
        assert r["habits"]["跑步"]["owner"] == "laoyu"
        assert r["scope"] == "all"

    def test_empty_extra_shard_does_not_add_prefix(self, manage, tmp_path):
        """存在一个**空**分片（`{}`）也不该让所有键名突然带前缀。"""
        d = str(tmp_path)
        _write_json(os.path.join(d, "habits.json"), {"喝水": {"logs": [_today()]}})
        _write_json(os.path.join(d, "habits_laoyu.json"), {})
        r = self._call(manage)
        assert list(r["habits"]) == ["喝水"], "没有真实冲突就不该加前缀"

    def test_user_filter_returns_only_that_user(self, manage, tmp_path):
        d = str(tmp_path)
        _write_json(os.path.join(d, "habits.json"), {"喝水": {"logs": [_today()]}})
        _write_json(os.path.join(d, "habits_laoyu.json"), {"跑步": {"logs": [_today()]}})
        r = self._call(manage, user=LAOYU)
        assert r["total"] == 1
        assert list(r["habits"]) == ["跑步"], "单分片视图不应加 owner· 前缀"
        assert r["scope"] == LAOYU

    def test_unknown_user_returns_empty_not_everything(self, manage, tmp_path):
        """查一个不存在的用户必须返回空，**不能**退化成全局视图（否则管理台会误读）。"""
        _write_json(os.path.join(str(tmp_path), "habits.json"),
                    {"喝水": {"logs": [_today()]}})
        r = self._call(manage, user="nobody")
        assert r["total"] == 0 and r["habits"] == {}
        assert r["scope"] == "nobody"

    def test_corrupt_shard_does_not_break_others(self, manage, tmp_path):
        """单个分片损坏（写坏 / 被截断）不能拖垮整个概览。"""
        d = str(tmp_path)
        with open(os.path.join(d, "habits.json"), "w") as f:
            f.write("{ this is not json")
        _write_json(os.path.join(d, "habits_laoyu.json"),
                    {"跑步": {"logs": [_today()]}})
        r = self._call(manage)
        assert r["total"] == 1
        assert list(r["habits"]) == ["跑步"]

    def test_non_dict_shard_is_skipped(self, manage, tmp_path):
        """分片内容类型不对（历史遗留的 list）也不能抛异常。"""
        d = str(tmp_path)
        _write_json(os.path.join(d, "habits.json"), ["不是 dict"])
        _write_json(os.path.join(d, "habits_laoyu.json"),
                    {"跑步": {"logs": [_today()]}})
        r = self._call(manage)
        assert r["total"] == 1 and list(r["habits"]) == ["跑步"]

    def test_streak_and_completed_today(self, manage, tmp_path):
        import datetime
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        _write_json(os.path.join(str(tmp_path), "habits.json"), {
            "喝水": {"logs": [yesterday.isoformat(), today.isoformat()]},
        })
        r = self._call(manage)
        h = r["habits"]["喝水"]
        assert h["total_days"] == 2
        assert h["streak"] == 2
        assert h["completed_today"] is True
        assert h["last_log"] == today.isoformat()
        assert h["week_days"] >= 1

    def test_insight_sums_total_days(self, manage, tmp_path):
        import datetime
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write_json(os.path.join(str(tmp_path), "habits.json"), {
            "喝水": {"logs": [_today()]},
            "跑步": {"logs": [y, _today()]},
        })
        r = self._call(manage)
        assert r["habits"]["跑步"]["total_days"] == 2
        assert "3次" in r["insight"]

    def test_bad_log_entries_do_not_crash(self, manage, tmp_path):
        """``logs`` 里混入非字符串 / 非法日期时不能 500，也不能把连续记录清零。

        旧实现在 ``week_days`` 里裸调 ``_dt.date.fromisoformat(d)``，一条脏数据
        就直接抛 ``ValueError`` → 整个接口 500（管理台习惯页白屏）。
        """
        _write_json(os.path.join(str(tmp_path), "habits.json"), {
            "喝水": {"logs": [123, None, "not-a-date", _today()]},
        })
        r = self._call(manage)
        assert isinstance(r, dict), "不应返回 JSONResponse(500)"
        h = r["habits"]["喝水"]
        assert h["total_days"] == 1, "只统计合法 ISO 日期"
        assert h["streak"] == 1, "一条脏数据不能把连续记录清零"
        assert h["completed_today"] is True
        assert h["last_log"] == _today()

    def test_duplicate_logs_do_not_inflate_totals(self, manage, tmp_path):
        """``total_days`` / ``week_days`` 必须按**去重后的天数**算。

        `log_habit` 本身按天去重，所以真实数据看不出差别；但手工编辑或历史脏
        数据里出现重复日期时，"总天数"不该翻倍。
        """
        _write_json(os.path.join(str(tmp_path), "habits.json"), {
            "喝水": {"logs": [_today(), _today(), _today()]},
        })
        h = self._call(manage)["habits"]["喝水"]
        assert h["total_days"] == 1
        assert h["week_days"] == 1
        assert h["streak"] == 1
