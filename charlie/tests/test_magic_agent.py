"""magic-agent 开放任务域测试：Hermes agent CLI 后台执行 + 语音秒回 + 完成推送。

覆盖：
1. 路由：关键词命中/强标记消歧/_normalize_intent/prompt/decider DOMAINS 与
   _KEYWORD_MAP 的 magic-agent 条目联动
2. 命令构建：--query-file -（stdin，不经 shell）、--oneshot、预算/turns、--in
3. 输出抠取：box 边框剥离、无框退化为元数据剥离
4. 后台执行：超时 kill 进程组、非零退出/空产出/二进制缺失降级
5. dispatch_task：秒回 ACK 不阻塞、任务注册表、完成/失败推送（含截断）
6. brain_stream_sentences 早退分支：yield 一句 ack 即 return，不碰 brain；
   hermes 不可用 → 落回大脑路径
"""
import os
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent import magic_agent


@pytest.fixture
def clean_registry():
    magic_agent._tasks.clear()
    yield
    magic_agent._tasks.clear()


class _FakeProc:
    """subprocess.Popen 替身：可控返回码/输出/超时/异常。"""

    def __init__(self, rc=0, out="", err="", timeout_on_communicate=False,
                 exc_on_communicate=None):
        self.returncode = rc
        self._out, self._err = out, err
        self._timeout = timeout_on_communicate
        self._exc = exc_on_communicate
        self.pid = 424242
        self.killed = False
        self.communicate_input = None
        self.communicate_timeout = "unset"

    def communicate(self, input=None, timeout=None):
        self.communicate_input = input
        self.communicate_timeout = timeout
        if self._exc is not None:
            raise self._exc
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=1)
        return self._out, self._err

    def kill(self):
        self.killed = True


# ===== 路由 =====

class TestRouting:
    def test_keyword_hits_single_domain(self):
        from agent.llm import _classify_intent
        for t in ("清理磁盘", "磁盘满了", "释放点空间", "把系统备份一下",
                  "电脑很卡帮我看看", "排查网络"):
            assert _classify_intent(t) == "magic-agent", t

    def test_keyword_variants_verbal_particles(self):
        """中文动补变体（"清理一下磁盘"不含连续子串"清理磁盘"）必须命中关键词。"""
        from agent.llm import _classify_intent
        for t in ("帮我清理一下磁盘", "清理下磁盘", "磁盘清理",
                  "清理一下空间", "网速很慢排查一下", "把下载文件夹整理一下"):
            assert _classify_intent(t) == "magic-agent", t

    def test_collision_disambiguated_by_task_phrase(self):
        """共域子串误命中（"查一下"在"排查一下"里、"放点"在"释放点"里、
        magic-life 的"网络"类泛词）：magic-agent 词表全是任务型短语，命中即真
        意图，压过其他域的子串误命中。词表变体与共域消歧共用一份名单，不再有
        "强标记名单"漂移（"排查网络"挡不住"排查一下网络"踩过——实测漏判成多域
        降级走了 decider 360ms，filler 抢走首句）。"""
        from agent.llm import _classify_intent
        for t in ("网速很慢排查一下", "释放点空间", "排查一下网络"):
            assert _classify_intent(t) == "magic-agent", t

    def test_search_guard_not_stolen(self):
        """真搜索（"查一下"句式非排查、搜索动词）不能被 magic-agent 抢走——
        守卫只挡判定，落分类器；无端点测试环境下分类器不可用会回落 none，
        这里断言不是 magic-agent 即证明守卫生效。"""
        from agent.llm import _classify_intent
        assert _classify_intent("帮我查一下磁盘满了怎么办") != "magic-agent"

    def test_other_domains_not_stolen(self):
        """相近句式的其他域不能被 magic-agent 抢走。"""
        from agent.llm import _classify_intent
        assert _classify_intent("清理一下微信消息") == "magic-apps"
        assert _classify_intent("今天天气怎么样") == "amap-maps"
        assert _classify_intent("提醒我三点开会") == "magic-reminder"

    def test_normalize_intent_passthrough(self):
        """magic-agent 必须显式归一（不在 ALL_MCP，泛匹配会兜底成 magic-music）。"""
        from agent.llm import _normalize_intent
        assert _normalize_intent("magic-agent") == "magic-agent"
        assert _normalize_intent("agent") == "magic-agent"

    def test_prompt_contains_domain(self):
        import agent.llm as llm
        src = open(llm.__file__, encoding="utf-8").read()
        assert "magic-agent" in src
        # 选项行里有（源文件里 \n 是字面转义，不是真换行），规则行里也有
        assert "| magic-agent" in src
        assert "- magic-agent = 开放式系统任务" in src

    def test_decider_domains_include_magic_agent(self):
        from agent import decide_intent as di
        assert "magic-agent" in di.DOMAINS
        assert "magic-agent = 开放式系统任务" in di._DECIDE_CTX
        # 改说明必须换 ctx id（同 id = 同文本，服务端缓存）
        assert di._DECIDE_CTX_ID == "charlie-intent-v5"

    def test_decider_domains_match_prompt_set(self):
        """decider 域集合必须是 prompt 选项行的子集（防两处漂移）。

        2026-09-20 起 decider 停用，LLM 分类 prompt 是路由唯一真源，可以
        先于 decider 加新域（magic-roles）。方向性不变量：decider 选得出
        的每个域都必须在 prompt 里（否则命中即断链）；prompt 允许领先。
        恢复 decider 时同步 DOMAINS + _DECIDE_CTX + 换 ctx id，再改回相等。
        """
        import re
        import agent.llm as llm
        from agent import decide_intent as di
        src = open(llm.__file__, encoding="utf-8").read()
        m = re.search(r'"选项: (.*?)\\n"', src)
        assert m
        prompt_domains = set(m.group(1).split(" | "))
        missing = set(di.DOMAINS) - prompt_domains
        assert not missing, f"decider 有这些域而 prompt 没有（命中即断链）: {missing}"


# ===== 输出抠取 =====

class TestExtractAnswer:
    def test_box_border_stripped(self):
        raw = ("Query: 清理磁盘\nInitializing agent...\n"
               "╭─ ☤ Hermes ───────────────╮\n"
               "已清理 12GB 缓存文件。\n"
               "╰──────────────────────────╯\n\n"
               "Resume this session with: hermes chat --resume abc\n"
               "Session: abc\nTitle: 清理磁盘\nDuration: 90s\nMessages: 4\n")
        assert magic_agent._extract_answer(raw) == "已清理 12GB 缓存文件。"

    def test_box_multiline_answer(self):
        raw = ("╭─ ☤ Hermes ─╮\n第一行\n第二行\n╰╯\n")
        assert magic_agent._extract_answer(raw) == "第一行\n第二行"

    def test_no_box_metadata_stripped(self):
        raw = "Query: x\nInitializing agent...\n答案正文\nSession: abc\n"
        assert magic_agent._extract_answer(raw) == "答案正文"

    def test_empty(self):
        assert magic_agent._extract_answer("") == ""
        assert magic_agent._extract_answer("Session: abc\nTitle: x\n") == ""


# ===== run_agent_task =====

class TestRunAgentTask:
    def test_command_building_and_stdin(self):
        """任务文本走 stdin（--query-file -）：不经 shell，任意中文/$() 安全。"""
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _FakeProc(out="╭─ ☤ Hermes ─╮\n完成\n╰╯\n")

        with patch("subprocess.Popen", _fake_popen):
            status, answer = magic_agent.run_agent_task("清理磁盘")
        assert status == "ok"
        assert answer == "完成"
        cmd = captured["cmd"]
        assert cmd[0] == magic_agent.AGENT_CLI_BIN == "hermes"
        assert cmd[1] == "chat"
        assert cmd[2:4] == ["--query-file", "-"]
        assert "--oneshot" in cmd and "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "text"
        assert cmd[cmd.index("--run-budget") + 1] == "600"
        assert cmd[cmd.index("--max-turns") + 1] == "12"
        assert "--in" not in cmd  # WORKDIR 空 → 不传
        assert captured["kwargs"]["start_new_session"] is True
        assert captured["kwargs"]["stdin"] == subprocess.PIPE

    def test_task_passed_via_stdin_not_argv(self):
        """任务文本只出现在 communicate(input=...)，绝不出现在 argv
        （argv 会经 hermes 侧 shell 解释？不——query-file - 是文档钦定的
        无 shell 解释通道；这里钉死防后人改回 -q 直传）。"""
        captured = {}
        proc = _FakeProc(out="╭x╮\nok\n╰x╯\n")
        captured["proc"] = proc

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return proc

        with patch("subprocess.Popen", _fake_popen):
            magic_agent.run_agent_task("rm -rf ~ && echo $(whoami) `id`")
        assert "rm -rf" not in " ".join(captured["cmd"])
        assert proc.communicate_input == "rm -rf ~ && echo $(whoami) `id`"
        assert proc.communicate_timeout == magic_agent.AGENT_CLI_TIMEOUT

    def test_workdir_passed_when_configured(self, monkeypatch):
        monkeypatch.setattr(magic_agent, "AGENT_CLI_WORKDIR", "/tmp")
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(out="╭x╮\nok\n╰x╯\n")

        with patch("subprocess.Popen", _fake_popen):
            magic_agent.run_agent_task("整理下载")
        cmd = captured["cmd"]
        assert cmd[cmd.index("--in") + 1] == "/tmp"

    def test_nonzero_exit_is_error(self):
        with patch("subprocess.Popen",
                   lambda cmd, **kw: _FakeProc(rc=1, err="boom")):
            status, msg = magic_agent.run_agent_task("x")
        assert status == "error"
        assert "boom" in msg

    def test_empty_answer(self):
        with patch("subprocess.Popen",
                   lambda cmd, **kw: _FakeProc(out="Session: abc\n")):
            status, msg = magic_agent.run_agent_task("x")
        assert status == "empty"

    def test_timeout_kills_process_group(self, monkeypatch):
        """硬超时 kill 整个进程组：hermes 的工具子进程一并收掉，不留僵尸。"""
        killed = {}
        monkeypatch.setattr(magic_agent.os, "killpg",
                            lambda pgid, sig: killed.update(pgid=pgid, sig=sig))
        monkeypatch.setattr(magic_agent.os, "getpgid", lambda pid: 424242)
        with patch("subprocess.Popen",
                   lambda cmd, **kw: _FakeProc(timeout_on_communicate=True)):
            status, msg = magic_agent.run_agent_task("x")
        assert status == "timeout"
        assert killed["pgid"] == 424242
        assert killed["sig"] == signal.SIGKILL
        assert "已终止" in msg

    def test_exception_is_error(self):
        with patch("subprocess.Popen",
                   lambda cmd, **kw: _FakeProc(exc_on_communicate=OSError("no such file"))):
            status, msg = magic_agent.run_agent_task("x")
        assert status == "error"
        assert "no such file" in msg

    def test_no_binary(self, monkeypatch):
        monkeypatch.setattr(magic_agent, "AGENT_CLI_BIN", "charlie-no-such-cli-xyz")
        status, msg = magic_agent.run_agent_task("x")
        assert status == "no_binary"


# ===== dispatch_task =====

class TestDispatchTask:
    def test_dispatch_returns_ack_immediately(self, monkeypatch, clean_registry):
        """秒回：hermes 任务（实测分钟级）不能阻塞语音链路。"""
        gate = threading.Event()
        captured = {}

        def _fake_run(task):
            gate.wait(timeout=5)
            captured["finished"] = True
            return "ok", "完成"

        monkeypatch.setattr(magic_agent, "run_agent_task", _fake_run)
        t0 = time.time()
        ack = magic_agent.dispatch_task("清理磁盘", "sess-1")
        assert ack == magic_agent.ACK_TEXT
        assert time.time() - t0 < 0.5
        # 不阻塞：任务还在跑（gate 未开），注册表为 running
        assert len(magic_agent._tasks) == 1
        (tid, info), = magic_agent._tasks.items()
        assert info["status"] == "running"
        assert info["task"] == "清理磁盘"
        assert tid.startswith("task-")
        gate.set()
        for _ in range(100):
            if magic_agent._tasks[tid]["status"] == "ok":
                break
            time.sleep(0.05)
        assert magic_agent._tasks[tid]["status"] == "ok"
        assert magic_agent._tasks[tid]["answer"] == "完成"
        assert magic_agent.agent_task_status(tid)["finished"] is not None

    def test_dispatch_unavailable_returns_empty(self, monkeypatch, clean_registry):
        """hermes 不可用 → 返回 ""，调用方据此落回大脑路径。"""
        monkeypatch.setattr(magic_agent, "AGENT_CLI_BIN", "charlie-no-such-cli-xyz")
        assert magic_agent.dispatch_task("清理磁盘") == ""
        assert magic_agent._tasks == {}

    def test_dispatch_disabled_returns_empty(self, monkeypatch, clean_registry):
        monkeypatch.setattr(magic_agent, "AGENT_CLI_ENABLED", False)
        assert magic_agent.dispatch_task("清理磁盘") == ""
        assert magic_agent._tasks == {}

    def test_push_on_success(self, monkeypatch, clean_registry):
        pushed = []
        monkeypatch.setattr("app.notifications.push_feishu_async",
                            lambda text, **k: pushed.append(text))
        monkeypatch.setattr(magic_agent, "run_agent_task",
                            lambda task: ("ok", "已清理 12GB"))
        monkeypatch.setattr(magic_agent, "agent_cli_available", lambda: True)
        monkeypatch.setattr(magic_agent, "_refresh_ack_cache", lambda: None)
        tid = f"task-{int(time.time())}-abc123"
        magic_agent._register(tid, "清理磁盘")
        magic_agent._run_and_push(tid, "清理磁盘")
        assert len(pushed) == 1
        assert "任务完成：清理磁盘" in pushed[0]
        assert "已清理 12GB" in pushed[0]

    def test_push_on_failure(self, monkeypatch, clean_registry):
        pushed = []
        monkeypatch.setattr("app.notifications.push_feishu_async",
                            lambda text, **k: pushed.append(text))
        monkeypatch.setattr(magic_agent, "run_agent_task",
                            lambda task: ("timeout", "超过硬超时 900s，已终止"))
        monkeypatch.setattr(magic_agent, "agent_cli_available", lambda: True)
        monkeypatch.setattr(magic_agent, "_refresh_ack_cache", lambda: None)
        tid = "task-x-fail"
        magic_agent._register(tid, "排查网络")
        magic_agent._run_and_push(tid, "排查网络")
        assert len(pushed) == 1
        assert "任务未完成：排查网络" in pushed[0]
        assert "已终止" in pushed[0]
        assert magic_agent.agent_task_status(tid)["status"] == "timeout"

    def test_push_truncates_long_answer(self, monkeypatch, clean_registry):
        pushed = []
        monkeypatch.setattr("app.notifications.push_feishu_async",
                            lambda text, **k: pushed.append(text))
        monkeypatch.setattr(magic_agent, "run_agent_task",
                            lambda task: ("ok", "详" * 2000))
        monkeypatch.setattr(magic_agent, "agent_cli_available", lambda: True)
        monkeypatch.setattr(magic_agent, "AGENT_CLI_PUSH_MAX_CHARS", 100)
        monkeypatch.setattr(magic_agent, "_refresh_ack_cache", lambda: None)
        tid = "task-x-long"
        magic_agent._register(tid, "备份")
        magic_agent._run_and_push(tid, "备份")
        assert "已截断" in pushed[0]
        assert "完整 2000 字" in pushed[0]

    def test_push_failure_does_not_raise(self, monkeypatch, clean_registry):
        """守护线程不能因推送失败而崩。"""
        def _boom(*a, **k):
            raise RuntimeError("feishu down")
        monkeypatch.setattr("app.notifications.push_feishu_async", _boom)
        monkeypatch.setattr(magic_agent, "run_agent_task", lambda task: ("ok", "ok"))
        monkeypatch.setattr(magic_agent, "agent_cli_available", lambda: True)
        monkeypatch.setattr(magic_agent, "_refresh_ack_cache", lambda: None)
        magic_agent._register("task-x-pushfail", "t")
        magic_agent._run_and_push("task-x-pushfail", "t")  # 不应抛
        assert magic_agent.agent_task_status("task-x-pushfail")["status"] == "ok"

    def test_registry_cap_evicts_oldest(self, clean_registry):
        for i in range(magic_agent._TASK_REGISTRY_MAX + 10):
            magic_agent._register(f"task-{i}", f"任务{i}")
        assert len(magic_agent._tasks) == magic_agent._TASK_REGISTRY_MAX
        assert "task-0" not in magic_agent._tasks  # 最旧的被丢
        assert f"task-{magic_agent._TASK_REGISTRY_MAX + 9}" in magic_agent._tasks

    def test_unknown_task_status_empty(self, clean_registry):
        assert magic_agent.agent_task_status("task-nonexistent") == {}

    def test_refresh_ack_cache_pays_next_budget(self, monkeypatch):
        """保温：先付下一次 ACK 的 TTS 合成预算（进缓存后即时 ACK ~50ms 出声，
        不保温则空闲 >1h 缓存过期后重新走网络 ~1.9s，filler 抢走首句）。"""
        calls = []
        monkeypatch.setattr("app.xiaozhi_ws._synth_mp3",
                            lambda s, **k: calls.append(s) or b"x" * 200)
        magic_agent._refresh_ack_cache()
        assert calls == [magic_agent.ACK_TEXT]
        # 合成失败静默：保温只是优化
        def _boom(s, **k):
            raise RuntimeError("no network")
        monkeypatch.setattr("app.xiaozhi_ws._synth_mp3", _boom)
        magic_agent._refresh_ack_cache()  # 不抛即过

    def test_run_and_push_refreshes_before_task(self, monkeypatch, clean_registry):
        """_run_and_push 先保温再跑任务（顺序对：保温不能挤占任务启动时间）。"""
        order = []
        monkeypatch.setattr(magic_agent, "_refresh_ack_cache",
                            lambda: order.append("refresh"))
        monkeypatch.setattr(magic_agent, "run_agent_task",
                            lambda task: (order.append("run"), ("ok", "done"))[1])
        magic_agent._register("task-x-order", "t")
        magic_agent._run_and_push("task-x-order", "t")
        assert order == ["refresh", "run"]


# ===== brain_stream_sentences 早退分支 =====

class TestEarlyExitBranch:
    def test_magic_agent_yields_ack_without_brain(self, monkeypatch):
        """分类到 magic-agent → 秒回 ack 即 return，大脑一次都不构建。"""
        import agent.llm as llm
        monkeypatch.setattr(llm, "_classify_intent", lambda t: "magic-agent")
        monkeypatch.setattr(llm.magic_agent, "dispatch_task",
                            lambda task, sid="default": llm.magic_agent.ACK_TEXT)
        monkeypatch.setattr(
            llm, "_get_brain",
            lambda mcp_set, session_id="default": (_ for _ in ()).throw(
                AssertionError("大脑不该被构建")))
        outs = list(llm.brain_stream_sentences("清理磁盘", session_id="sess-e1"))
        assert len(outs) == 1
        assert outs[0] == (magic_agent.ACK_TEXT, magic_agent.ACK_TEXT)

    def test_unavailable_falls_through_to_brain(self, monkeypatch):
        """hermes 不可用（dispatch 返回 ""）→ 落回大脑路径。"""
        import agent.llm as llm
        monkeypatch.setattr(llm, "_classify_intent", lambda t: "magic-agent")
        monkeypatch.setattr(llm.magic_agent, "dispatch_task", lambda task, sid="default": "")
        monkeypatch.setattr(
            llm, "_get_brain",
            lambda mcp_set, session_id="default": (_ for _ in ()).throw(
                RuntimeError("__BRAIN_REACHED__")))
        outs = list(llm.brain_stream_sentences("清理磁盘", session_id="sess-e2"))
        # 大脑构建异常 → 兜底话术（LLM key 测试环境已配 → "大脑启动失败"）
        assert len(outs) == 1
        assert "大脑启动失败" in outs[0][0]

    def test_ack_not_cached(self, monkeypatch):
        """ack 不入意图缓存：重复请求要再派发任务，不能被缓存静默吞掉。"""
        import agent.llm as llm
        llm._st.intent_cache.clear()
        monkeypatch.setattr(llm, "_classify_intent", lambda t: "magic-agent")
        monkeypatch.setattr(llm.magic_agent, "dispatch_task",
                            lambda task, sid="default": llm.magic_agent.ACK_TEXT)
        monkeypatch.setattr(
            llm, "_get_brain",
            lambda mcp_set, session_id="default": (_ for _ in ()).throw(
                AssertionError("大脑不该被构建")))
        list(llm.brain_stream_sentences("清理磁盘", session_id="sess-e3"))
        from agent.cache import _cache_get
        assert _cache_get("清理磁盘", "sess-e3") is None
