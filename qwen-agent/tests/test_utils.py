"""
魔幻手机 - 工具模块单元测试
测试: parse_time_str / sanitize_error / cleanup_temp_files / truncate_history_file
"""
import os, json, tempfile, datetime
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
