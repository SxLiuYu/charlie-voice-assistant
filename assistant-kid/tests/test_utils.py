"""
Charlie - 工具模块单元测试
测试: parse_time_str / sanitize_error / cleanup_temp_files / truncate_history_file
"""
import os, json, tempfile, datetime, subprocess, sys, time
from utils import parse_time_str, sanitize_error, cleanup_temp_files, truncate_history_file


class TestParseTimeStr:
    """测试中文时间解析"""

    def test_minutes_later(self):
        """N分钟后"""
        result = parse_time_str("30分钟后")
        assert result is not None
        now = datetime.datetime.now()
        tg = datetime.datetime.fromisoformat(result)
        delta = tg - now
        assert 29 * 60 <= delta.total_seconds() <= 31 * 60  # 约30分钟(1800秒)

    def test_hours_later(self):
        """N小时后"""
        result = parse_time_str("2小时后")
        assert result is not None
        now = datetime.datetime.now()
        tg = datetime.datetime.fromisoformat(result)
        delta = tg - now
        assert 119 * 60 <= delta.total_seconds() <= 121 * 60  # 约2小时(7200秒)

    def test_days_later(self):
        """N天后"""
        result = parse_time_str("3天后")
        assert result is not None
        now = datetime.datetime.now()
        tg = datetime.datetime.fromisoformat(result)
        delta = (tg.date() - now.date()).days
        assert delta == 3

    def test_tomorrow(self):
        """明天"""
        result = parse_time_str("明天")
        assert result is not None
        now = datetime.datetime.now()
        tg = datetime.datetime.fromisoformat(result)
        delta = (tg.date() - now.date()).days
        assert delta == 1

    def test_day_after_tomorrow(self):
        """后天"""
        result = parse_time_str("后天")
        assert result is not None
        now = datetime.datetime.now()
        tg = datetime.datetime.fromisoformat(result)
        delta = (tg.date() - now.date()).days
        assert delta == 2

    def test_specific_time_pm(self):
        """下午3点"""
        result = parse_time_str("下午3点")
        assert result is not None
        tg = datetime.datetime.fromisoformat(result)
        assert tg.hour == 15
        assert tg.minute == 0

    def test_specific_time_evening(self):
        """晚上9点半"""
        result = parse_time_str("晚上9点半")
        assert result is not None
        tg = datetime.datetime.fromisoformat(result)
        assert tg.hour == 21
        assert tg.minute == 30

    def test_empty_string(self):
        """空字符串"""
        assert parse_time_str("") is None
        assert parse_time_str(None) is None

    def test_unparseable(self):
        """无法解析的字符串"""
        assert parse_time_str("随便什么时候") is None

    def test_combined_date_time(self):
        """明天9点(日期+时间组合)"""
        result = parse_time_str("明天9点")
        assert result is not None
        now = datetime.datetime.now()
        tg = datetime.datetime.fromisoformat(result)
        assert tg.hour == 9
        assert tg.minute == 0
        assert (tg.date() - now.date()).days == 1


class TestSanitizeError:
    """测试错误脱敏"""

    def test_removes_file_paths(self):
        """移除文件路径"""
        msg = "Error in /home/user/project/voice_server.py line 42"
        result = sanitize_error(msg)
        assert "voice_server.py" not in result
        assert "[文件]" in result

    def test_removes_api_keys(self):
        """移除API密钥"""
        msg = "Auth failed with key sk-abc123def456"
        result = sanitize_error(msg)
        assert "sk-abc123def456" not in result
        assert "[密钥]" in result

    def test_removes_app_keys(self):
        """移除app-开头的密钥"""
        msg = "Invalid key app-Egtyx0Fytauhxkr6rWBLZyZl"
        result = sanitize_error(msg)
        assert "app-Egtyx0Fytauhxkr6rWBLZyZl" not in result
        assert "[密钥]" in result

    def test_removes_ip_addresses(self):
        """移除IP地址"""
        msg = "Connection refused from 192.168.1.100:8080"
        result = sanitize_error(msg)
        assert "192.168.1.100" not in result
        assert "[IP]" in result

    def test_truncates_long_errors(self):
        """截断过长错误信息"""
        msg = "x" * 200
        result = sanitize_error(msg)
        assert len(result) <= 103  # 100 + "..."

    def test_preserves_short_errors(self):
        """短错误信息保持原样"""
        msg = "timeout"
        result = sanitize_error(msg)
        assert result == "timeout"


class TestCleanupTempFiles:
    """测试临时文件清理"""

    def test_cleanup_removes_matching_files(self):
        """清理匹配的临时文件"""
        # 创建测试文件
        f1 = tempfile.NamedTemporaryFile(suffix="_reply.wav", delete=False, dir="/tmp")
        f1.write(b"test")
        f1.close()
        assert os.path.exists(f1.name)
        removed = cleanup_temp_files()
        assert removed >= 1
        assert not os.path.exists(f1.name)

    def test_cleanup_returns_count(self):
        """返回清理的文件数"""
        # 创建2个测试文件
        for suffix in ["_reply.wav", "_reply2.wav"]:
            f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir="/tmp")
            f.write(b"x")
            f.close()
        removed = cleanup_temp_files("/tmp/*_reply*.wav")
        assert removed >= 2

    def test_cleanup_removes_runtime_audio_files_from_data_dir(self, tmp_path):
        """启动清理也必须移除数据目录中的运行时音频，避免异常退出后残留。"""
        reply = tmp_path / "voice_reply.wav"
        reminder = tmp_path / "tmpabc123.mp3"
        reply.write_bytes(b"wav")
        reminder.write_bytes(b"mp3")

        removed = cleanup_temp_files(extra_dirs=[str(tmp_path)])

        assert removed >= 2
        assert not reply.exists()
        assert not reminder.exists()

    def test_cleanup_does_not_remove_unrelated_tmp_mp3_without_runtime_dir(self, tmp_path):
        """默认清理不能扩大范围删除系统临时目录中的任意 MP3。"""
        unrelated = tmp_path / "tmpunrelated.mp3"
        unrelated.write_bytes(b"mp3")

        cleanup_temp_files(pattern=str(tmp_path / "*_reply.wav"))

        assert unrelated.exists()


class TestTruncateHistoryFile:
    """测试对话历史文件截断"""

    def test_truncate_long_history(self):
        """截断过长的历史文件"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            data = [{"role": "user", "content": f"msg{i}"} for i in range(150)]
            json.dump(data, f)
            path = f.name
        try:
            truncate_history_file(path, max_entries=50)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert len(data) == 50
            assert data[-1]["content"] == "msg149"  # 保留最近的
        finally:
            os.unlink(path)

    def test_keep_short_history(self):
        """短历史不截断"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            data = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
            json.dump(data, f)
            path = f.name
        try:
            truncate_history_file(path, max_entries=100)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert len(data) == 10  # 未截断
        finally:
            os.unlink(path)

    def test_nonexistent_file(self):
        """不存在的文件不报错"""
        truncate_history_file("/tmp/nonexistent_test_file.json", max_entries=100)
        # 不应该抛出异常

    def test_truncate_dict_history_per_session(self):
        """多会话 dict 历史按每个会话截断，保留最近消息"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            data = {
                "alpha": [{"role": "user", "content": f"alpha-{i}"} for i in range(7)],
                "beta": [{"role": "user", "content": f"beta-{i}"} for i in range(4)],
                "gamma": [{"role": "user", "content": "gamma-0"}],
                "meta": {"version": 1},
            }
            json.dump(data, f)
            path = f.name
        try:
            truncate_history_file(path, max_entries=3)
            with open(path, "r", encoding="utf-8") as f:
                result = json.load(f)
            assert len(result["alpha"]) == 3
            assert result["alpha"][0]["content"] == "alpha-4"
            assert result["alpha"][-1]["content"] == "alpha-6"
            assert len(result["beta"]) == 3
            assert result["beta"][-1]["content"] == "beta-3"
            assert result["gamma"] == data["gamma"]
            assert result["meta"] == data["meta"]
        finally:
            os.unlink(path)

    def test_truncate_uses_atomic_replace_without_truncating_target(self, monkeypatch, tmp_path):
        """截断写入使用临时文件和 replace，不直接以写模式截断目标文件"""
        path = tmp_path / "history.json"
        original = [{"role": "user", "content": f"msg{i}"} for i in range(6)]
        path.write_text(json.dumps(original), encoding="utf-8")

        direct_writes = []
        replaced = []
        real_open = open
        real_replace = os.replace

        def tracking_open(file, *args, **kwargs):
            mode = kwargs.get("mode", args[0] if args else "r")
            if os.path.abspath(os.fspath(file)) == os.fspath(path) and "w" in mode:
                direct_writes.append(mode)
            return real_open(file, *args, **kwargs)

        def tracking_replace(src, dst):
            assert os.path.abspath(os.fspath(dst)) == os.fspath(path)
            replaced.append(os.fspath(src))
            return real_replace(src, dst)

        monkeypatch.setattr("builtins.open", tracking_open)
        monkeypatch.setattr("utils.os.replace", tracking_replace)

        truncate_history_file(os.fspath(path), max_entries=2)

        assert direct_writes == []
        assert len(replaced) == 1
        assert not os.path.exists(replaced[0])
        result = json.loads(path.read_text(encoding="utf-8"))
        assert [item["content"] for item in result] == ["msg4", "msg5"]

    def test_write_failure_keeps_existing_history_intact(self, monkeypatch, tmp_path):
        """写入失败时保留原历史文件，不留下截断后的空文件"""
        path = tmp_path / "history.json"
        original_text = json.dumps([{"role": "user", "content": f"msg{i}"} for i in range(3)])
        path.write_text(original_text, encoding="utf-8")

        def fail_dump(*args, **kwargs):
            raise OSError("disk full")

        import json as json_module
        monkeypatch.setattr(json_module, "dump", fail_dump)

        truncate_history_file(os.fspath(path), max_entries=1)

        assert path.read_text(encoding="utf-8") == original_text

    def test_truncate_waits_for_exclusive_history_lock(self, tmp_path):
        """截断读改写等待历史文件排他锁，避免跨进程覆盖"""
        path = tmp_path / "conversation_history.json"
        lock_path = path.with_name(path.name + ".lock")
        path.write_text(json.dumps([{"role": "user", "content": f"msg{i}"} for i in range(5)]), encoding="utf-8")

        script = f"""
import fcntl, time
with open({str(lock_path)!r}, "a+", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    time.sleep(0.5)
"""
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            time.sleep(0.1)
            started = time.monotonic()
            truncate_history_file(os.fspath(path), max_entries=2)
            elapsed = time.monotonic() - started
        finally:
            proc.wait(timeout=5)

        assert elapsed >= 0.35
        result = json.loads(path.read_text(encoding="utf-8"))
        assert [item["content"] for item in result] == ["msg3", "msg4"]

    def test_malformed_or_unsupported_history_does_not_raise(self, tmp_path):
        """畸形 JSON 或不支持的 JSON 类型只记录失败，不影响服务"""
        cases = [
            b"{not json",
            b"123",
            b"null",
        ]
        for index, content in enumerate(cases):
            path = tmp_path / f"history-{index}.json"
            path.write_bytes(content)
            truncate_history_file(os.fspath(path), max_entries=1)
            assert path.read_bytes() == content
