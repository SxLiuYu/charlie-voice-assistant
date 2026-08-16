import os, sys, time, types, signal, subprocess
from pathlib import Path

import pytest
import requests


def test_voice_server_import_uses_configured_log_dir(tmp_path):
    """直接导入 voice_server 时也必须遵守 ASSISTANT_KID_LOG_DIR，不能把测试请求写回真实日志。"""
    import sys
    import subprocess

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = tmp_path / "logs"
    script = (
        "import os; "
        "os.environ['SKIP_BACKGROUND']='1'; "
        "os.environ['GLM_KEY']='test'; "
        "os.environ['TTS_KEY']='test'; "
        "os.environ['ASR_KEY']='test'; "
        "os.environ['AMAP_KEY']='test'; "
        "import voice_server; "
        "print(voice_server._LOG_DIR)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_dir,
        env={**os.environ, "ASSISTANT_KID_LOG_DIR": str(log_dir)},
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip().endswith(str(log_dir))
    assert (log_dir / "app.log").exists()
    project_log = Path(project_dir) / "logs" / "app.log"
    assert f"ASSISTANT_KID_LOG_DIR={log_dir}" not in project_log.read_text(encoding="utf-8", errors="ignore")


def test_runtime_files_use_configured_data_dir():
    """测试运行时历史、偏好、提醒和日志不能写回项目根目录。"""
    data_dir = os.environ["ASSISTANT_KID_DATA_DIR"]
    log_dir = os.environ["ASSISTANT_KID_LOG_DIR"]

    import app.reminders
    import voice_agent
    import voice_server

    assert voice_agent.HISTORY_FILE == os.path.join(data_dir, "conversation_history.json")
    assert voice_agent.PREFS_FILE == os.path.join(data_dir, "preferences.json")
    assert voice_agent.REMINDERS_FILE == os.path.join(data_dir, "reminders.json")
    assert app.reminders.REMINDERS_FILE == os.path.join(data_dir, "reminders.json")
    assert app.reminders.SCHEDULER_LOCK_FILE == os.path.join(data_dir, "reminders.json.scheduler.lock")
    assert app.reminders.SUGGESTIONS_STATE_FILE == os.path.join(data_dir, "suggestions_state.json")
    assert app.reminders.PROACTIVE_LOCK_FILE == os.path.join(data_dir, "suggestions_state.json.runner.lock")
    assert voice_server.SUGGEST_STATE_FILE == os.path.join(data_dir, "suggestions_state.json")
    assert voice_server._LOG_DIR == str(log_dir)

    voice_agent.set_preference("test_key", "test_value")
    app.reminders._save_reminders([{"id": 1, "text": "test", "done": False}])
    voice_agent._save_history()

    assert os.path.exists(os.path.join(data_dir, "preferences.json"))
    assert os.path.exists(os.path.join(data_dir, "reminders.json"))
    assert os.path.exists(os.path.join(data_dir, "conversation_history.json"))


@pytest.mark.skip(reason="baize_skills_mcp 已拆分，提醒/偏好工具移到 app/reminders + voice_agent")
def test_baize_mcp_files_use_configured_data_dir():
    """独立 MCP 子进程必须复用同一份提醒和偏好数据目录。"""
    data_dir = os.environ["ASSISTANT_KID_DATA_DIR"]

    import baize_skills_mcp

    assert baize_skills_mcp.STORE == os.path.join(data_dir, "reminders.json")
    assert baize_skills_mcp.PREFS_FILE == os.path.join(data_dir, "preferences.json")

    result = baize_skills_mcp.add_reminder("MCP测试提醒")
    assert "MCP测试提醒" in result
    assert baize_skills_mcp.set_preference("mcp_key", "mcp_value") == "已记住您的偏好：mcp_key = mcp_value"

    with open(os.path.join(data_dir, "reminders.json"), encoding="utf-8") as f:
        reminders = __import__("json").load(f)
    with open(os.path.join(data_dir, "preferences.json"), encoding="utf-8") as f:
        prefs = __import__("json").load(f)

    assert any(item["text"] == "MCP测试提醒" for item in reminders)
    assert prefs["mcp_key"] == "mcp_value"


@pytest.mark.skip(reason="system_status 已移到 mcp_server.py(magic-phone MCP)")
def test_mcp_system_status_uses_nonblocking_cpu_sample(monkeypatch):
    """MCP 系统状态不能每次固定阻塞 1 秒采样 CPU。"""
    import baize_skills_mcp

    cpu_intervals = []

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.cpu_percent = lambda interval=None: cpu_intervals.append(interval) or 12.3
    fake_psutil.cpu_count = lambda logical=True: 8

    class FakeVirtualMemory:
        total = 16 * 1024 ** 3
        available = 8 * 1024 ** 3
        percent = 50.0

    class FakeDiskUsage:
        total = 100 * 1024 ** 3
        used = 40 * 1024 ** 3
        percent = 40.0

    fake_psutil.virtual_memory = lambda: FakeVirtualMemory()
    fake_psutil.disk_usage = lambda path: FakeDiskUsage()
    fake_psutil.boot_time = lambda: time.time() - 3661
    monkeypatch.setattr(baize_skills_mcp, "psutil", fake_psutil)

    result = baize_skills_mcp.system_status()

    assert "CPU使用率:12.3%" in result
    assert "运行:0天1时1分" in result
    assert cpu_intervals == [None]


@pytest.mark.skip(reason="list_reminders 已从 baize_skills_mcp 删除")
def test_baize_mcp_ignores_malformed_reminders(tmp_path, monkeypatch):
    """MCP 提醒列表必须和主服务一样，忽略 None、非 dict 和空文本记录。"""
    import json
    import baize_skills_mcp

    reminders_file = tmp_path / "reminders.json"
    reminders_file.write_text(
        json.dumps([
            None,
            {"text": "", "done": False},
            {"text": "MCP正常提醒", "done": False},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(baize_skills_mcp, "STORE", str(reminders_file))

    assert "MCP正常提醒" in baize_skills_mcp.list_reminders()


@pytest.mark.skip(reason="add_reminder 已从 baize_skills_mcp 删除")
def test_baize_mcp_add_reminder_uses_shared_reminder_lock(tmp_path, monkeypatch):
    """MCP 子进程写提醒时必须和主服务共用 reminders.json.lock，避免读改写丢更新。"""
    import fcntl
    import json
    import threading
    import time
    import baize_skills_mcp

    reminders_file = tmp_path / "reminders.json"
    lock_file = tmp_path / "reminders.json.lock"
    reminders_file.write_text(
        json.dumps([{"id": 1, "text": "已有提醒", "done": False}], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(baize_skills_mcp, "STORE", str(reminders_file))
    monkeypatch.setattr(baize_skills_mcp, "STORE_LOCK_FILE", str(lock_file), raising=False)

    lock_handle = open(lock_file, "a+", encoding="utf-8")
    fcntl.flock(lock_handle, fcntl.LOCK_EX)
    try:
        thread = threading.Thread(target=baize_skills_mcp.add_reminder, args=("MCP锁内提醒",))
        thread.start()
        time.sleep(0.2)

        assert thread.is_alive(), "MCP reminder write must wait for the shared reminder lock"
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()

    thread.join(timeout=2)
    assert not thread.is_alive()
    stored = json.loads(reminders_file.read_text(encoding="utf-8"))
    assert [item["text"] for item in stored] == ["已有提醒", "MCP锁内提醒"]


@pytest.mark.skip(reason="add_reminder 已从 baize_skills_mcp 删除")
def test_baize_mcp_reminder_write_is_atomic(tmp_path, monkeypatch):
    """MCP 提醒保存必须写临时文件后原子替换，不能直接截断 reminders.json。"""
    import json
    import baize_skills_mcp

    reminders_file = tmp_path / "reminders.json"
    lock_file = tmp_path / "reminders.json.lock"
    reminders_file.write_text(
        json.dumps([{"id": 1, "text": "旧提醒", "done": False}], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(baize_skills_mcp, "STORE", str(reminders_file))
    monkeypatch.setattr(baize_skills_mcp, "STORE_LOCK_FILE", str(lock_file), raising=False)

    direct_writes = []
    original_open = open

    def reject_direct_target_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(path) == str(reminders_file) and any(flag in mode for flag in ("w", "a", "x")):
            direct_writes.append(mode)
            raise AssertionError("MCP reminder writes must use a temporary file and os.replace")
        return original_open(path, *args, **kwargs)

    replace_calls = []
    monkeypatch.setattr("builtins.open", reject_direct_target_open)
    monkeypatch.setattr(baize_skills_mcp.os, "replace", lambda src, dst: replace_calls.append((src, dst)))

    result = baize_skills_mcp.add_reminder("MCP原子提醒")

    assert "MCP原子提醒" in result
    assert direct_writes == []
    assert len(replace_calls) == 1


@pytest.mark.skip(reason="add_reminder/_locked_store 已从 baize_skills_mcp 删除")
def test_baize_mcp_add_reminder_holds_exclusive_lock_for_read_modify_write(tmp_path, monkeypatch):
    """MCP 添加提醒必须在同一个排他锁内完成读改写，避免并发添加丢更新。"""
    import json
    from contextlib import contextmanager
    import baize_skills_mcp

    reminders_file = tmp_path / "reminders.json"
    lock_file = tmp_path / "reminders.json.lock"
    reminders_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(baize_skills_mcp, "STORE", str(reminders_file))
    monkeypatch.setattr(baize_skills_mcp, "STORE_LOCK_FILE", str(lock_file), raising=False)

    lock_modes = []
    original_locked_store = baize_skills_mcp._locked_store

    @contextmanager
    def tracking_locked_store(shared=False):
        lock_modes.append(shared)
        with original_locked_store(shared=shared):
            yield

    monkeypatch.setattr(baize_skills_mcp, "_locked_store", tracking_locked_store)

    assert "MCP事务提醒" in baize_skills_mcp.add_reminder("MCP事务提醒")
    assert lock_modes == [False]
    stored = json.loads(reminders_file.read_text(encoding="utf-8"))
    assert [item["text"] for item in stored] == ["MCP事务提醒"]


@pytest.mark.skip(reason="set_preference 已从 baize_skills_mcp 删除")
def test_baize_mcp_preference_write_is_atomic(tmp_path, monkeypatch):
    """MCP 偏好保存必须写临时文件后原子替换，不能直接截断 preferences.json。"""
    import json
    import baize_skills_mcp

    prefs_file = tmp_path / "preferences.json"
    lock_file = tmp_path / "preferences.json.lock"
    prefs_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(baize_skills_mcp, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(baize_skills_mcp, "PREFS_LOCK_FILE", str(lock_file), raising=False)

    direct_writes = []
    original_open = open

    def reject_direct_target_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(path) == str(prefs_file) and any(flag in mode for flag in ("w", "a", "x")):
            direct_writes.append(mode)
            raise AssertionError("MCP preference writes must use a temporary file and os.replace")
        return original_open(path, *args, **kwargs)

    replace_calls = []
    monkeypatch.setattr("builtins.open", reject_direct_target_open)
    monkeypatch.setattr(baize_skills_mcp.os, "replace", lambda src, dst: replace_calls.append((src, dst)))

    result = baize_skills_mcp.set_preference("atomic_pref", "atomic_value")

    assert "atomic_pref" in result
    assert direct_writes == []
    assert len(replace_calls) == 1


@pytest.mark.skip(reason="set_preference/_locked_prefs 已从 baize_skills_mcp 删除")
def test_baize_mcp_set_preference_holds_exclusive_lock_for_read_modify_write(tmp_path, monkeypatch):
    """MCP 设置偏好必须在同一个排他锁内完成读改写，避免并发写丢 key。"""
    import json
    from contextlib import contextmanager
    import baize_skills_mcp

    prefs_file = tmp_path / "preferences.json"
    lock_file = tmp_path / "preferences.json.lock"
    prefs_file.write_text(json.dumps({"old_pref": "old_value"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(baize_skills_mcp, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(baize_skills_mcp, "PREFS_LOCK_FILE", str(lock_file), raising=False)

    lock_modes = []
    original_locked_prefs = baize_skills_mcp._locked_prefs

    @contextmanager
    def tracking_locked_prefs(shared=False):
        lock_modes.append(shared)
        with original_locked_prefs(shared=shared):
            yield

    monkeypatch.setattr(baize_skills_mcp, "_locked_prefs", tracking_locked_prefs)

    result = baize_skills_mcp.set_preference("mcp_pref", "mcp_value")

    assert "mcp_pref" in result
    assert lock_modes == [False]
    stored = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert stored == {"old_pref": "old_value", "mcp_pref": "mcp_value"}


@pytest.mark.skip(reason="set_preference 已从 baize_skills_mcp 删除")
def test_baize_mcp_set_preference_waits_for_shared_preference_lock(tmp_path, monkeypatch):
    """MCP 偏好写事务必须等待共享 preferences.json.lock。"""
    import fcntl
    import json
    import threading
    import time
    import baize_skills_mcp

    prefs_file = tmp_path / "preferences.json"
    lock_file = tmp_path / "preferences.json.lock"
    prefs_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(baize_skills_mcp, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(baize_skills_mcp, "PREFS_LOCK_FILE", str(lock_file), raising=False)

    lock_handle = open(lock_file, "a+", encoding="utf-8")
    fcntl.flock(lock_handle, fcntl.LOCK_EX)
    try:
        thread = threading.Thread(
            target=baize_skills_mcp.set_preference,
            args=("locked_mcp_pref", "locked_value"),
        )
        thread.start()
        time.sleep(0.2)

        assert thread.is_alive(), "MCP preference write must wait for the shared preference lock"
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()

    thread.join(timeout=2)
    assert not thread.is_alive()
    stored = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert stored["locked_mcp_pref"] == "locked_value"


def test_voice_agent_set_preference_waits_for_shared_preference_lock(tmp_path, monkeypatch):
    """主进程偏好写事务必须使用同一把 preferences.json.lock，并在拿到锁后再修改内存。"""
    import fcntl
    import json
    import threading
    import time
    import voice_agent
    import agent.preferences

    prefs_file = tmp_path / "preferences.json"
    lock_file = tmp_path / "preferences.json.lock"
    prefs_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(voice_agent, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(voice_agent, "PREFS_LOCK_FILE", str(lock_file), raising=False)
    monkeypatch.setattr(agent.preferences, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(agent.preferences, "PREFS_LOCK_FILE", str(lock_file))
    voice_agent._preferences.clear()
    voice_agent._preferences_revision = 0
    voice_agent._preferences_save_seq = 0
    if hasattr(voice_agent, "_preferences_file_signature"):
        voice_agent._preferences_file_signature = None
    voice_agent._load_preferences()

    lock_handle = open(lock_file, "a+", encoding="utf-8")
    fcntl.flock(lock_handle, fcntl.LOCK_EX)
    try:
        thread = threading.Thread(
            target=voice_agent.set_preference,
            args=("locked_voice_pref", "locked_value"),
        )
        thread.start()
        time.sleep(0.2)

        assert thread.is_alive(), "voice_agent preference write must wait for the shared preference lock"
        with voice_agent._prefs_lock:
            assert "locked_voice_pref" not in voice_agent._preferences
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()

    thread.join(timeout=2)
    assert not thread.is_alive()
    stored = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert stored["locked_voice_pref"] == "locked_value"

    voice_agent._preferences.clear()
    voice_agent._preferences_revision = 0
    voice_agent._preferences_save_seq = 0


def test_magic_phone_cli_audio_paths_use_configured_data_dir(tmp_path, monkeypatch):
    """交互式 CLI 的录音和回复音频必须遵守数据目录隔离，不能固定写入 /tmp。"""
    import magic_phone_cli

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))

    assert magic_phone_cli.cli_audio_path("cli_mic.wav") == str(tmp_path / "cli_mic.wav")
    assert magic_phone_cli.cli_audio_path("cli_reply.wav") == str(tmp_path / "cli_reply.wav")


def test_magic_phone_cli_record_default_output_uses_cli_audio_path(tmp_path, monkeypatch):
    """CLI 录音函数不能在导入时把默认输出固定到旧数据目录或 /tmp。"""
    import magic_phone_cli

    fake_proc = types.SimpleNamespace()
    opened = {}
    target = tmp_path / "cli_mic.wav"

    def fake_popen(cmd, stdout, stderr):
        opened["cmd"] = cmd
        return fake_proc

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(magic_phone_cli.subprocess, "Popen", fake_popen)

    proc = magic_phone_cli.record("3")

    assert proc is fake_proc
    assert opened["cmd"][-1] != str(target)
    assert proc._cli_target_path == str(target)
    assert Path(opened["cmd"][-1]).parent == tmp_path
    assert Path(opened["cmd"][-1]).name.startswith(".cli_mic.")
    assert Path(opened["cmd"][-1]).name.endswith(".recording.wav")


def test_magic_phone_cli_recording_commits_temporary_wav_atomically(tmp_path, monkeypatch):
    """CLI 录音进行中只能写临时文件，录制成功后再原子替换正式 WAV。"""
    import magic_phone_cli

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))
    target = tmp_path / "cli_mic.wav"
    target.write_bytes(b"old recording")
    expected = b"new recording" * 200
    class FakeProc:
        def __init__(self, cmd, stdout, stderr):
            self.cmd = cmd
            self.terminated = False
            self.waited = False
            self.returncode = None
            Path(cmd[-1]).write_bytes(expected)

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(magic_phone_cli.subprocess, "Popen", FakeProc)

    proc = magic_phone_cli.record("3")
    assert target.read_bytes() == b"old recording"
    temp_path = Path(proc.cmd[-1])
    assert temp_path.parent == tmp_path
    assert temp_path.name.startswith(".cli_mic.")
    assert temp_path.name.endswith(".recording.wav")

    committed = magic_phone_cli.commit_recording(proc)

    assert committed == str(target)
    assert target.read_bytes() == expected
    assert not temp_path.exists()
    assert proc.terminated
    assert proc.waited


def test_magic_phone_cli_short_recording_keeps_existing_wav(tmp_path, monkeypatch):
    """录音过短或 ffmpeg 异常时不能用半截临时文件覆盖上一条有效录音。"""
    import magic_phone_cli

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))
    target = tmp_path / "cli_mic.wav"
    target.write_bytes(b"old recording")
    class FakeProc:
        def __init__(self, cmd, stdout, stderr):
            self.cmd = cmd
            self.returncode = None
            Path(cmd[-1]).write_bytes(b"x")

        def terminate(self):
            pass

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(magic_phone_cli.subprocess, "Popen", FakeProc)

    proc = magic_phone_cli.record("3")
    temp_path = Path(proc.cmd[-1])
    assert magic_phone_cli.commit_recording(proc) is None
    assert target.read_bytes() == b"old recording"
    assert not temp_path.exists()


def test_magic_phone_cli_commit_replace_failure_keeps_existing_wav(tmp_path, monkeypatch):
    """录音临时文件替换失败时不能截断上一条有效录音。"""
    import magic_phone_cli

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))
    target = tmp_path / "cli_mic.wav"
    target.write_bytes(b"old recording")

    class FakeProc:
        def __init__(self, cmd, stdout, stderr):
            self.cmd = cmd
            self.returncode = None
            Path(cmd[-1]).write_bytes(b"x" * 3000)

        def terminate(self):
            pass

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(magic_phone_cli.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(magic_phone_cli.os, "replace", fail_replace)

    proc = magic_phone_cli.record("3")
    temp_path = Path(proc.cmd[-1])
    with pytest.raises(OSError):
        magic_phone_cli.commit_recording(proc)

    assert target.read_bytes() == b"old recording"
    assert not temp_path.exists()


def test_magic_phone_cli_discard_recording_removes_temporary_wav(tmp_path, monkeypatch):
    """录音中退出时必须停止 ffmpeg 并删除临时文件，不能留下半截录音。"""
    import magic_phone_cli

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))

    class FakeProc:
        def __init__(self, cmd, stdout, stderr):
            self.cmd = cmd
            self.stopped = False
            Path(cmd[-1]).write_bytes(b"partial")

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(magic_phone_cli.subprocess, "Popen", FakeProc)

    proc = magic_phone_cli.record("3")
    temp_path = Path(proc.cmd[-1])
    assert temp_path.exists()

    magic_phone_cli.discard_recording(proc)

    assert proc.stopped
    assert not temp_path.exists()


def test_magic_phone_cli_commit_failure_prints_retry_message(monkeypatch, capsys):
    """录音发布失败时 CLI 应给中文提示并继续下一轮，而不是直接崩溃。"""
    import magic_phone_cli

    inputs = iter(["", "", EOFError()])
    procs = [
        types.SimpleNamespace(poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0),
        types.SimpleNamespace(poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0),
    ]

    monkeypatch.setattr(magic_phone_cli, "find_airpods", lambda: ("3", "Fake Mic"))
    monkeypatch.setattr(magic_phone_cli, "record", lambda device: procs.pop(0))
    commit_results = iter([OSError("replace failed"), None])

    def fake_commit(proc_arg):
        result = next(commit_results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(magic_phone_cli, "commit_recording", fake_commit)
    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr("builtins.input", fake_input)

    magic_phone_cli.main()

    assert "保存录音失败" in capsys.readouterr().out


def test_magic_phone_cli_reply_write_failure_keeps_text_reply(tmp_path, monkeypatch, capsys):
    """回复音频落盘失败时 CLI 应保留文字回复并继续，而不是直接崩溃。"""
    import magic_phone_cli

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))
    proc = object()
    inputs = iter(["", ""])
    mic_path = tmp_path / "cli_mic.wav"

    monkeypatch.setattr(magic_phone_cli, "find_airpods", lambda: ("3", "Fake Mic"))
    monkeypatch.setattr(magic_phone_cli, "record", lambda device: proc)

    def fake_commit(proc_arg):
        mic_path.parent.mkdir(parents=True, exist_ok=True)
        mic_path.write_bytes(b"fake wav")
        return str(mic_path)

    monkeypatch.setattr(magic_phone_cli, "commit_recording", fake_commit)
    monkeypatch.setattr(
        magic_phone_cli,
        "voice_loop",
        lambda audio_data, fmt: ("你好", "你好，我在。", b"audio" * 30),
    )
    def fail_write_audio_file(path, audio):
        raise OSError("disk full")

    monkeypatch.setattr(magic_phone_cli, "write_audio_file", fail_write_audio_file)
    play_calls = []
    monkeypatch.setattr(magic_phone_cli, "play", lambda path: play_calls.append(path))

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr("builtins.input", fake_input)

    magic_phone_cli.main()

    output = capsys.readouterr().out
    assert "你好，我在。" in output
    assert "保存回复音频失败" in output
    assert play_calls == []


def test_magic_phone_cli_reply_write_failure_keeps_existing_audio(tmp_path, monkeypatch):
    """CLI 回复音频必须写临时文件后替换，替换失败不能截断已有回复文件。"""
    import magic_phone_cli
    import voice_agent

    monkeypatch.setattr(magic_phone_cli, "DATA_DIR", str(tmp_path))
    target = tmp_path / "cli_reply.wav"
    target.write_bytes(b"old reply")

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(voice_agent.os, "replace", fail_replace)

    with pytest.raises(OSError):
        magic_phone_cli.write_audio_file(str(target), b"new reply")

    assert target.read_bytes() == b"old reply"
    assert list(tmp_path.glob(".cli_reply*.tmp")) == []


@pytest.mark.skip(reason="baize_skills_mcp.set_preference 已删除，跨进程偏好测试不再适用")
def test_preference_writers_from_separate_processes_do_not_lose_keys(tmp_path):
    """主进程和 MCP 子进程并发写偏好时，共用文件锁必须保住双方写入的 key。"""
    import json
    import subprocess
    import sys

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    start_file = tmp_path / "start"
    data_dir.mkdir()
    log_dir.mkdir()
    prefs_file = data_dir / "preferences.json"
    prefs_file.write_text("{}", encoding="utf-8")

    base_env = {
        **os.environ,
        "ASSISTANT_KID_DATA_DIR": str(data_dir),
        "ASSISTANT_KID_LOG_DIR": str(log_dir),
        "SKIP_BACKGROUND": "1",
        "START_FILE": str(start_file),
        "PREF_WRITES": "15",
    }
    voice_env = {**base_env, "WORKER_ID": "voice"}
    mcp_env = {**base_env, "WORKER_ID": "mcp"}
    voice_script = (
        "import os,time,voice_agent\n"
        "start=os.environ['START_FILE']\n"
        "writes=int(os.environ['PREF_WRITES'])\n"
        "worker=os.environ['WORKER_ID']\n"
        "while not os.path.exists(start): time.sleep(0.005)\n"
        "for i in range(writes):\n"
        "    voice_agent.set_preference(f'{worker}_{i}', 'ok')\n"
    )
    mcp_script = (
        "import os,time,baize_skills_mcp\n"
        "start=os.environ['START_FILE']\n"
        "writes=int(os.environ['PREF_WRITES'])\n"
        "worker=os.environ['WORKER_ID']\n"
        "while not os.path.exists(start): time.sleep(0.005)\n"
        "for i in range(writes):\n"
        "    baize_skills_mcp.set_preference(f'{worker}_{i}', 'ok')\n"
    )

    voice_proc = subprocess.Popen(
        [sys.executable, "-c", voice_script],
        cwd=project_dir,
        env=voice_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    mcp_proc = subprocess.Popen(
        [sys.executable, "-c", mcp_script],
        cwd=project_dir,
        env=mcp_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.1)
    start_file.write_text("start", encoding="utf-8")

    voice_stdout, voice_stderr = voice_proc.communicate(timeout=15)
    mcp_stdout, mcp_stderr = mcp_proc.communicate(timeout=15)

    assert voice_proc.returncode == 0, voice_stderr or voice_stdout
    assert mcp_proc.returncode == 0, mcp_stderr or mcp_stdout
    stored = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert len(stored) == 30
    assert all(stored.get(f"voice_{i}") == "ok" for i in range(15))
    assert all(stored.get(f"mcp_{i}") == "ok" for i in range(15))


def test_suggest_state_write_is_atomic(tmp_path, monkeypatch):
    """主动建议状态必须写临时文件后原子替换，不能直接截断 suggestions_state.json。"""
    import voice_server

    state_file = tmp_path / "suggestions_state.json"
    lock_file = tmp_path / "suggestions_state.json.lock"
    state_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(voice_server, "SUGGEST_STATE_FILE", str(state_file))
    monkeypatch.setattr(voice_server, "SUGGEST_STATE_LOCK_FILE", str(lock_file), raising=False)
    voice_server.SUGGESTIONS_STATE.clear()
    voice_server.SUGGESTIONS_STATE.update({"last_rain_suggest": "2026-08-01"})

    direct_writes = []
    original_open = open

    def reject_direct_target_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(path) == str(state_file) and any(flag in mode for flag in ("w", "a", "x")):
            direct_writes.append(mode)
            raise AssertionError("suggestion state writes must use a temporary file and os.replace")
        return original_open(path, *args, **kwargs)

    replace_calls = []
    monkeypatch.setattr("builtins.open", reject_direct_target_open)
    monkeypatch.setattr(voice_server.os, "replace", lambda src, dst: replace_calls.append((src, dst)))

    voice_server._save_suggest_state()

    assert direct_writes == []
    assert len(replace_calls) == 1


def test_update_suggest_state_holds_exclusive_lock_for_read_modify_write(tmp_path, monkeypatch):
    """主动建议状态更新必须在同一个排他锁内重读磁盘、合并更新并写入。"""
    import json
    from contextlib import contextmanager
    import app.schedulers as _sched

    state_file = tmp_path / "suggestions_state.json"
    lock_file = tmp_path / "suggestions_state.json.lock"
    state_file.write_text(
        json.dumps({"last_rain_suggest": "old-day"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(_sched, "SUGGEST_STATE_FILE", str(state_file))
    monkeypatch.setattr(_sched, "SUGGEST_STATE_LOCK_FILE", str(lock_file), raising=False)
    _sched.SUGGESTIONS_STATE.clear()

    lock_modes = []
    original_locked_state = _sched._locked_suggest_state

    @contextmanager
    def tracking_locked_state(shared=False):
        lock_modes.append(shared)
        with original_locked_state(shared=shared):
            yield

    monkeypatch.setattr(_sched, "_locked_suggest_state", tracking_locked_state)

    _sched._update_suggest_state({"last_time_suggest": "2026-08-01_morning"})

    assert lock_modes == [False]
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert stored == {
        "last_weather_check": 0,
        "last_rain_suggest": "old-day",
        "last_time_suggest": "2026-08-01_morning",
        "last_health_alert": "",
    }


def test_suggest_state_writers_from_separate_processes_do_not_lose_keys(tmp_path):
    """多个 HTTP/语音服务进程同时记录建议状态时，不能互相覆盖对方的 key。"""
    import json
    import subprocess
    import sys

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    start_file = tmp_path / "start"
    data_dir.mkdir()
    log_dir.mkdir()
    state_file = data_dir / "suggestions_state.json"
    state_file.write_text("{}", encoding="utf-8")

    base_env = {
        **os.environ,
        "ASSISTANT_KID_DATA_DIR": str(data_dir),
        "ASSISTANT_KID_LOG_DIR": str(log_dir),
        "SKIP_BACKGROUND": "1",
        "START_FILE": str(start_file),
        "STATE_WRITES": "15",
    }
    scripts = []
    procs = []
    for worker in ("alpha", "beta"):
        script = (
            "import os,time,voice_server\n"
            "start=os.environ['START_FILE']\n"
            "writes=int(os.environ['STATE_WRITES'])\n"
            "worker=os.environ['WORKER_ID']\n"
            "while not os.path.exists(start): time.sleep(0.005)\n"
            "for i in range(writes):\n"
            "    voice_server._update_suggest_state({f'{worker}_{i}': 'ok'})\n"
        )
        scripts.append(script)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=project_dir,
            env={**base_env, "WORKER_ID": worker},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ))
    time.sleep(0.1)
    start_file.write_text("start", encoding="utf-8")

    outputs = [proc.communicate(timeout=15) for proc in procs]
    for proc, (stdout, stderr) in zip(procs, outputs):
        assert proc.returncode == 0, stderr or stdout
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert all(stored.get(f"alpha_{i}") == "ok" for i in range(15))
    assert all(stored.get(f"beta_{i}") == "ok" for i in range(15))


def test_retry_honors_retry_after_and_does_not_retry_permanent_4xx(monkeypatch):
    """429 读取 Retry-After；普通 4xx 立即失败，避免无意义重试。"""
    import voice_agent

    sleep_calls = []
    monkeypatch.setattr(voice_agent.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    permanent_response = requests.Response()
    permanent_response.status_code = 400
    permanent_calls = 0

    def permanent_failure():
        nonlocal permanent_calls
        permanent_calls += 1
        raise requests.exceptions.HTTPError(response=permanent_response)

    try:
        voice_agent._retry(permanent_failure, "HTTP")
    except Exception as exc:
        assert "400" in str(exc)
    else:
        raise AssertionError("permanent 4xx should fail")

    assert permanent_calls == 1
    assert sleep_calls == []

    rate_limited_response = requests.Response()
    rate_limited_response.status_code = 429
    rate_limited_response.headers["Retry-After"] = "0"
    rate_limited_calls = 0

    def rate_limited_then_ok():
        nonlocal rate_limited_calls
        rate_limited_calls += 1
        if rate_limited_calls == 1:
            raise requests.exceptions.HTTPError(response=rate_limited_response)
        return "ok"

    assert voice_agent._retry(rate_limited_then_ok, "HTTP") == "ok"
    assert rate_limited_calls == 2
    assert sleep_calls == [0]


def test_reminder_scheduler_lock_is_cross_process_exclusive(tmp_path):
    """提醒调度锁应在同一数据目录下跨进程互斥，持有者退出后自动释放。"""
    import fcntl
    import app.reminders

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reminders_file = data_dir / "reminders.json"
    reminders_file.write_text("[]", encoding="utf-8")

    old_reminders_file = app.reminders.REMINDERS_FILE
    old_scheduler_lock_file = app.reminders.SCHEDULER_LOCK_FILE
    app.reminders.REMINDERS_FILE = str(reminders_file)
    app.reminders.SCHEDULER_LOCK_FILE = str(reminders_file) + ".scheduler.lock"
    try:
        first = app.reminders.acquire_scheduler_lock()
        assert first is not None
        second = app.reminders.acquire_scheduler_lock()
        assert second is None

        fcntl.flock(first, fcntl.LOCK_UN)
        first.close()

        third = app.reminders.acquire_scheduler_lock()
        assert third is not None
        third.close()
    finally:
        app.reminders.REMINDERS_FILE = old_reminders_file
        app.reminders.SCHEDULER_LOCK_FILE = old_scheduler_lock_file


def test_proactive_runner_lock_is_cross_process_exclusive(tmp_path):
    """主动建议运行锁应在同一数据目录下跨进程互斥，持有者退出后自动释放。"""
    import fcntl
    import app.reminders

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    suggestions_file = data_dir / "suggestions_state.json"
    suggestions_file.write_text("{}", encoding="utf-8")

    old_lock_file = app.reminders.PROACTIVE_LOCK_FILE
    app.reminders.PROACTIVE_LOCK_FILE = str(suggestions_file) + ".runner.lock"
    try:
        first = app.reminders.acquire_proactive_lock()
        assert first is not None
        second = app.reminders.acquire_proactive_lock()
        assert second is None

        fcntl.flock(first, fcntl.LOCK_UN)
        first.close()

        third = app.reminders.acquire_proactive_lock()
        assert third is not None
        third.close()
    finally:
        app.reminders.PROACTIVE_LOCK_FILE = old_lock_file


def test_load_reminders_uses_shared_lock_so_reads_do_not_block_each_other(tmp_path):
    """只读提醒加载应使用共享锁，允许状态页和列表请求并发读取。"""
    import fcntl
    import app.reminders

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reminders_file = data_dir / "reminders.json"
    reminders_file.write_text('[{"id":1,"text":"共享锁提醒","done":false}]', encoding="utf-8")
    lock_file = data_dir / "reminders.json.lock"

    old_reminders_file = app.reminders.REMINDERS_FILE
    old_lock_file = app.reminders.REMINDERS_LOCK_FILE
    app.reminders.REMINDERS_FILE = str(reminders_file)
    app.reminders.REMINDERS_LOCK_FILE = str(lock_file)
    first = None
    try:
        first = open(lock_file, "a+", encoding="utf-8")
        fcntl.flock(first, fcntl.LOCK_SH | fcntl.LOCK_NB)

        reminders = app.reminders._load_reminders()

        assert [item["text"] for item in reminders] == ["共享锁提醒"]
    finally:
        if first is not None:
            fcntl.flock(first, fcntl.LOCK_UN)
            first.close()
        app.reminders.REMINDERS_FILE = old_reminders_file
        app.reminders.REMINDERS_LOCK_FILE = old_lock_file


def test_reminder_writes_are_atomic(tmp_path, monkeypatch):
    """提醒保存、申领、完成和失败重试都必须原子替换，不能直接截断目标 JSON。"""
    import json
    from app import reminders

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reminders_file = data_dir / "reminders.json"
    due = __import__("datetime").datetime.now().isoformat()
    reminders_file.write_text(
        json.dumps([
            {"id": 1, "text": "原子保存提醒", "due": due, "done": False},
            {"id": 2, "text": "待完成提醒", "due": due, "done": False, "delivery_state": "delivering", "claim_started_at": due, "attempt_count": 1},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(reminders, "REMINDERS_FILE", str(reminders_file))
    monkeypatch.setattr(reminders, "REMINDERS_LOCK_FILE", str(reminders_file) + ".lock")

    direct_writes = []
    original_open = open

    def write_file(path, contents):
        with original_open(path, "w", encoding="utf-8") as f:
            f.write(contents)

    def reject_direct_target_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(path) == str(reminders_file) and any(flag in mode for flag in ("w", "a", "x")):
            direct_writes.append(mode)
            raise AssertionError("reminder writes must use a temporary file and os.replace")
        return original_open(path, *args, **kwargs)

    replace_calls = []
    def count_replace(src, dst):
        replace_calls.append((src, dst))

    monkeypatch.setattr("builtins.open", reject_direct_target_open)
    monkeypatch.setattr(reminders.os, "replace", count_replace)

    reminders._save_reminders([{"id": 3, "text": "显式保存", "done": False}])
    write_file(
        reminders_file,
        json.dumps([{"id": 1, "text": "到期提醒", "due": due, "done": False}], ensure_ascii=False),
    )
    assert reminders.claim_due_reminders()
    reminders.complete_reminder_delivery(1)
    write_file(
        reminders_file,
        json.dumps([{"id": 4, "text": "失败提醒", "due": due, "done": False, "delivery_state": "delivering", "claim_started_at": due, "attempt_count": 1}], ensure_ascii=False),
    )
    reminders.release_failed_reminder(4, due, "播报失败")

    assert direct_writes == []
    assert len(replace_calls) == 4


def test_reminder_write_failure_keeps_existing_file_intact(tmp_path, monkeypatch):
    """序列化临时文件失败时，不能截断或替换原有提醒文件。"""
    from app import reminders

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reminders_file = data_dir / "reminders.json"
    original_contents = '[{"id":1,"text":"旧提醒","done":false}]'
    reminders_file.write_text(original_contents, encoding="utf-8")
    monkeypatch.setattr(reminders, "REMINDERS_FILE", str(reminders_file))
    monkeypatch.setattr(reminders, "REMINDERS_LOCK_FILE", str(reminders_file) + ".lock")

    def fail_dump(*args, **kwargs):
        raise RuntimeError("serialization failed")

    replace_calls = []
    monkeypatch.setattr(reminders.json, "dump", fail_dump)
    monkeypatch.setattr(reminders.os, "replace", lambda *args: replace_calls.append(args))

    try:
        reminders._save_reminders([{"id": 2, "text": "新提醒", "done": False}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("serialization failure should propagate")

    assert reminders_file.read_text(encoding="utf-8") == original_contents


def test_append_reminder_holds_single_exclusive_transaction(tmp_path, monkeypatch):
    """HTTP/MCP 添加提醒必须在同一把排他锁内完成读改写，避免并发添加丢更新。"""
    import json
    from contextlib import contextmanager
    from app import reminders

    reminders_file = tmp_path / "reminders.json"
    reminders_file.write_text(
        json.dumps([{"id": 7, "text": "旧提醒", "done": False}], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(reminders, "REMINDERS_FILE", str(reminders_file))
    monkeypatch.setattr(reminders, "REMINDERS_LOCK_FILE", str(reminders_file) + ".lock")

    lock_modes = []
    original_locked = reminders._locked_reminders

    @contextmanager
    def tracking_locked(shared=False):
        lock_modes.append(shared)
        with original_locked(shared=shared):
            yield

    monkeypatch.setattr(reminders, "_locked_reminders", tracking_locked)

    created = reminders.append_reminder("事务新增提醒", "", None)

    assert created["text"] == "事务新增提醒"
    assert created["id"] > 7
    assert lock_modes == [False]
    stored = json.loads(reminders_file.read_text(encoding="utf-8"))
    assert [item["text"] for item in stored] == ["旧提醒", "事务新增提醒"]


def test_append_reminder_is_cross_process_safe(tmp_path):
    """多个独立进程同时新增提醒时，文件锁必须保护读改写和 ID 分配。"""
    import json
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reminders_file = data_dir / "reminders.json"
    reminders_file.write_text(
        json.dumps([{"id": 100, "text": "种子提醒", "done": False}], ensure_ascii=False),
        encoding="utf-8",
    )

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env["SKIP_BACKGROUND"] = "1"
    env["ASSISTANT_KID_DATA_DIR"] = str(data_dir)

    def append_once(index):
        child_code = (
            "import sys; "
            f"sys.path.insert(0, {project_dir!r}); "
            "from app.reminders import append_reminder; "
            f"item = append_reminder(f'并发提醒#{index}', '', None); "
            "print(item['id'])"
        )
        return subprocess.run(
            [sys.executable, "-c", child_code],
            cwd=project_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append_once, range(8)))

    ids = []
    for result in results:
        assert result.returncode == 0, result.stderr
        ids.append(int(result.stdout.strip()))

    stored = json.loads(reminders_file.read_text(encoding="utf-8"))
    assert len(stored) == 9
    assert len({item["id"] for item in stored}) == 9
    assert {item["text"] for item in stored} == {f"并发提醒#{i}" for i in range(8)} | {"种子提醒"}
    assert sorted(ids) == sorted(item["id"] for item in stored if item["text"] != "种子提醒")


def test_complete_reminder_missing_does_not_write(tmp_path, monkeypatch):
    """删除/完成不存在的提醒不能触发无意义写入，更不能覆盖并发新增的数据。"""
    import json
    from app import reminders

    reminders_file = tmp_path / "reminders.json"
    original_contents = json.dumps([{"id": 9, "text": "保留提醒", "done": False}], ensure_ascii=False)
    reminders_file.write_text(original_contents, encoding="utf-8")
    monkeypatch.setattr(reminders, "REMINDERS_FILE", str(reminders_file))
    monkeypatch.setattr(reminders, "REMINDERS_LOCK_FILE", str(reminders_file) + ".lock")

    def reject_write(_data):
        raise AssertionError("completing a missing reminder must not write reminders.json")

    monkeypatch.setattr(reminders, "_write_locked_reminders", reject_write)

    assert reminders.complete_reminder(404) is False
    assert reminders_file.read_text(encoding="utf-8") == original_contents


def test_scheduler_lock_status_does_not_report_stale_owner(tmp_path):
    """锁文件中的旧 pid 不能在锁已释放后继续冒充当前 owner。"""
    import app.reminders

    lock_file = tmp_path / "reminders.json.scheduler.lock"
    old_scheduler_lock_file = app.reminders.SCHEDULER_LOCK_FILE
    app.reminders.SCHEDULER_LOCK_FILE = str(lock_file)
    try:
        handle = app.reminders.acquire_scheduler_lock()
        assert handle is not None
        handle.close()

        status = app.reminders.scheduler_lock_status()
        assert status["locked"] is False
        assert status["held_by_this_process"] is False
        assert status["owner_pid"] is None
    finally:
        app.reminders.SCHEDULER_LOCK_FILE = old_scheduler_lock_file


def test_scheduler_lock_status_missing_file_does_not_create_runtime_file(tmp_path):
    """只读状态探测不能因为查看状态而创建调度器锁文件。"""
    import app.reminders

    lock_file = tmp_path / "reminders.json.scheduler.lock"
    old_scheduler_lock_file = app.reminders.SCHEDULER_LOCK_FILE
    app.reminders.SCHEDULER_LOCK_FILE = str(lock_file)
    try:
        status = app.reminders.scheduler_lock_status()
        assert status == {
            "locked": False,
            "held_by_this_process": False,
            "owner_pid": None,
            "lock_file": str(lock_file),
        }
        assert not lock_file.exists()
    finally:
        app.reminders.SCHEDULER_LOCK_FILE = old_scheduler_lock_file


def test_scheduler_lock_status_does_not_release_existing_lock(tmp_path):
    """只读状态探测不能抢走或释放当前进程已持有的调度锁。"""
    import app.reminders

    lock_file = tmp_path / "reminders.json.scheduler.lock"
    old_scheduler_lock_file = app.reminders.SCHEDULER_LOCK_FILE
    app.reminders.SCHEDULER_LOCK_FILE = str(lock_file)
    handle = None
    try:
        handle = app.reminders.acquire_scheduler_lock()
        assert handle is not None

        status = app.reminders.scheduler_lock_status()
        assert status["locked"] is True
        assert status["held_by_this_process"] is True
        assert status["owner_pid"] == os.getpid()

        assert app.reminders.acquire_scheduler_lock() is None
    finally:
        if handle is not None:
            handle.close()
        app.reminders.SCHEDULER_LOCK_FILE = old_scheduler_lock_file


def test_scheduler_lock_status_reports_other_process_owner(tmp_path):
    """状态接口应能识别其他进程持有的调度锁。"""
    import subprocess
    import sys
    import time
    import app.reminders

    lock_file = tmp_path / "reminders.json.scheduler.lock"
    ready_file = tmp_path / "ready"
    old_scheduler_lock_file = app.reminders.SCHEDULER_LOCK_FILE
    app.reminders.SCHEDULER_LOCK_FILE = str(lock_file)
    child = None
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, time; "
                    "import os; "
                    f"f = open({str(lock_file)!r}, 'a+', encoding='utf-8'); "
                    "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); "
                    "f.seek(0); f.truncate(); f.write('pid=' + str(os.getpid()) + '\\n'); f.flush(); "
                    f"open({str(ready_file)!r}, 'w').close(); "
                    "time.sleep(2)"
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        deadline = time.time() + 5
        while not ready_file.exists() and time.time() < deadline:
            if child.poll() is not None:
                raise AssertionError("lock holder exited early")
            time.sleep(0.05)

        status = app.reminders.scheduler_lock_status()
        assert status["locked"] is True
        assert status["held_by_this_process"] is False
        assert status["owner_pid"] == child.pid
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=5)
        app.reminders.SCHEDULER_LOCK_FILE = old_scheduler_lock_file


def test_start_tunnel_uses_configured_log_and_url_files(tmp_path):
    """Cloudflare 隧道脚本必须遵守日志目录和可覆盖的 URL 文件，不固定写 /tmp。"""
    project_dir = Path(__file__).resolve().parents[1]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    url_file = tmp_path / "tunnel_url.txt"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    command_log = tmp_path / "commands.log"

    (fakebin / "pkill").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (fakebin / "cloudflared").write_text(
        "#!/bin/bash\necho \"$@\" >> \"$FAKE_COMMAND_LOG\"\necho 'https://test-tunnel.trycloudflare.com'\nsleep 5\n",
        encoding="utf-8",
    )
    for path in (fakebin / "pkill", fakebin / "cloudflared"):
        path.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "CF_BIN": str(fakebin / "cloudflared"),
        "TUNNEL_URL_FILE": str(url_file),
        "ASSISTANT_KID_LOG_DIR": str(log_dir),
        "ASSISTANT_KID_HTTP_PORT": "18080",
        "FAKE_COMMAND_LOG": str(command_log),
    }
    result = subprocess.run(
        ["bash", str(project_dir / "start_tunnel.sh")],
        cwd=project_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert url_file.read_text(encoding="utf-8").strip() == "https://test-tunnel.trycloudflare.com"
    assert (log_dir / "cloudflared.log").exists()
    assert "http://localhost:18080" in command_log.read_text(encoding="utf-8")


def test_watchdog_uses_configured_log_dir(tmp_path):
    """看门狗日志必须写入 ASSISTANT_KID_LOG_DIR，不能固定写 /tmp。"""
    project_dir = Path(__file__).resolve().parents[1]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    command_log = tmp_path / "commands.log"

    for name, body in {
        "curl": "#!/bin/bash\necho \"$@\" >> \"$FAKE_COMMAND_LOG\"\necho 200\n",
        "pkill": "#!/bin/bash\nexit 0\n",
        "pgrep": "#!/bin/bash\nexit 1\n",
        "screen": "#!/bin/bash\nexit 0\n",
    }.items():
        path = fakebin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "ASSISTANT_KID_LOG_DIR": str(log_dir),
        "ASSISTANT_KID_HTTP_PORT": "18080",
        "ASSISTANT_KID_HTTPS_PORT": "18443",
        "FAKE_COMMAND_LOG": str(command_log),
    }
    process = subprocess.Popen(
        ["bash", str(project_dir / "watchdog.sh")],
        cwd=project_dir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        time.sleep(1.5)
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert (log_dir / "watchdog.log").exists()
    assert "看门狗v2启动" in (log_dir / "watchdog.log").read_text(encoding="utf-8")
    commands = command_log.read_text(encoding="utf-8")
    assert "http://localhost:18080/api/status" in commands
    assert "https://localhost:18443/api/status" in commands
