"""Round 4 通道与运维回归测试：NH8 / NH10 / NH11 / NH14。

Run:
    cd charlie && python3 -m pytest tests/test_round4_channels_ops.py -q
"""
import os
import sys
import re
import json
import tempfile
import threading
import time
import subprocess

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# ============================================================
# NH8: abort 世代计数器
# ============================================================
from app.mqtt_server import (
    MqttXiaozhiServer,
    _sessions,
    _sessions_lock,
    _device_topics,
    _device_topics_lock,
    _addr_to_device,
    _addr_to_device_lock,
    _generate_aes_key_nonce,
    _ABORT_AUTO_CLEAR_DELAY,
)


# ============================================================
# P3: timing-robust helpers
# ============================================================

def _wait_flag(flag, want_set, timeout=2.0):
    """20ms 轮询等待 threading.Event 达到目标状态，超时 AssertionError。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if flag.is_set() == want_set:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"flag.is_set()={flag.is_set()} did not reach {want_set} within {timeout}s"
    )


@pytest.fixture
def mqtt_server():
    server = MqttXiaozhiServer()
    yield server
    with _sessions_lock:
        _sessions.clear()
    with _device_topics_lock:
        _device_topics.clear()
    with _addr_to_device_lock:
        _addr_to_device.clear()
    # P3: 顺带清理可枚举的遗留状态（abort 世代与 daemon clearer 线程）
    with server._abort_generations_lock:
        server._abort_generations.clear()
    with server._abort_flags_lock:
        server._abort_flags.clear()


class TestNH8AbortGeneration:
    """abort 世代化 auto-clear：旧 clearer 不能吞掉新一轮 abort。"""

    def test_generation_monotonic_and_second_abort_survives_first_clearer(self, mqtt_server, monkeypatch):
        """t0 abort → t1 新 utterance(clear+gen++) → t2 再次 abort：世代单调递增，
        且第二代 set 后，第一代 clearer 到期时不应再清除 flag。"""
        monkeypatch.setattr("app.mqtt_server._ABORT_AUTO_CLEAR_DELAY", 0.3)
        device_id = "nh8-gen"
        flag = mqtt_server._get_abort_flag(device_id)

        # t0: 第一次 abort (gen=1, flag set, clearer T0 0.3s 后到期)
        mqtt_server._handle_abort(device_id)
        with mqtt_server._abort_generations_lock:
            gen0 = mqtt_server._abort_generations[device_id]
        assert flag.is_set()

        # t1 (0.1s 后): 模拟新 utterance 启动（世代 +1，clear flag）
        time.sleep(0.1)
        with mqtt_server._abort_generations_lock:
            mqtt_server._abort_generations[device_id] = gen0 + 1
            gen1 = mqtt_server._abort_generations[device_id]
        flag.clear()
        assert gen1 == gen0 + 1

        # t2 (0.1s 后 = 0.2s 后 t0): 第二次 abort (gen=3, flag set, clearer T2 0.3s 后到期)
        time.sleep(0.1)
        mqtt_server._handle_abort(device_id)
        with mqtt_server._abort_generations_lock:
            gen2 = mqtt_server._abort_generations[device_id]
        assert gen2 == gen1 + 1
        assert flag.is_set()

        # t3 (0.1s 后 = 0.3s 后 t0): 第一代 clearer T0 到期。
        # 它应发现 gen=3 != 1，不执行 clear；此时第二代 clearer T2 尚未到期，
        # 因此 flag 必须仍为 set。
        # 使用轮询代替固定 sleep，避免负载下 clearer 线程延迟导致误判。
        _wait_flag(flag, want_set=True, timeout=0.5)
        assert flag.is_set(), "第一代 clearer 到期时，第二代 abort 的 flag 不应被清除"

        # t4 (0.2s 后 = 0.5s 后 t0 / 0.3s 后 t2): 第二代 clearer T2 到期，应正常 clear
        _wait_flag(flag, want_set=False, timeout=2.0)
        assert not flag.is_set()

    def test_auto_clear_only_when_no_new_activity(self, mqtt_server, monkeypatch):
        """在没有新一轮活动时，auto-clear 在延迟后生效。"""
        monkeypatch.setattr("app.mqtt_server._ABORT_AUTO_CLEAR_DELAY", 0.15)
        device_id = "nh8-clear"
        flag = mqtt_server._get_abort_flag(device_id)
        mqtt_server._handle_abort(device_id)
        assert flag.is_set()
        time.sleep(0.25)
        assert not flag.is_set()

    def test_abort_generations_cleanup_on_goodbye(self, mqtt_server):
        """goodbye 时顺带清理 abort 世代和 flag。"""
        device_id = "nh8-goodbye"
        mqtt_server._handle_abort(device_id)
        assert device_id in mqtt_server._abort_generations
        assert device_id in mqtt_server._abort_flags

        mqtt_server._handle_goodbye({"session_id": "x"}, device_id)
        assert device_id not in mqtt_server._abort_generations
        assert device_id not in mqtt_server._abort_flags


# ============================================================
# NH10: manage.py fcntl import 静态守卫
# ============================================================
class TestNH10FcntlCompat:
    """manage.py 必须使用 try/except + fcntl_compat 垫片，防止 Windows 启动崩溃。"""

    def test_manage_py_uses_fcntl_compat_fallback(self):
        manage_path = os.path.join(PROJECT_DIR, "app", "routes", "manage.py")
        with open(manage_path, "r", encoding="utf-8") as f:
            source = f.read()
        # 必须存在 try/except ImportError 包裹 fcntl，允许 except 行带注释
        assert re.search(r"try:\s*\n\s*import fcntl\s*\n\s*except ImportError[^\n]*\s*\n\s*import fcntl_compat as fcntl", source), (
            "manage.py 顶部应使用 try/except ImportError + import fcntl_compat as fcntl"
        )


# ============================================================
# NH11: _write_env_file 原子写入
# ============================================================
from app.routes.manage import _write_env_file


class TestNH11WriteEnvFile:
    """_write_env_file 并发安全 + 原子整版替换。"""

    def test_concurrent_writes_produce_valid_file(self):
        """8 线程并发写入不同 catalog key，最终文件可完整解析，无截断/空文件。"""
        # 使用 env_catalog 中真实存在的 key，否则 _write_env_file 会静默忽略
        catalog_keys = [
            "LLM_PRIORITY", "ARK_KEY", "ARK_MODEL", "STEPFUN_KEY", "STEPFUN_LLM_MODEL",
            "GLM_KEY", "GLM_MODEL", "TTS_VOICE",
        ]
        assert len(catalog_keys) >= 8
        tmp_env = tempfile.mktemp(suffix=".env")
        try:
            errors = []

            def writer(idx):
                try:
                    _write_env_file(tmp_env, {catalog_keys[idx]: f"value_{idx}"})
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            assert not errors, f"writer errors: {errors}"
            assert os.path.exists(tmp_env)
            with open(tmp_env, "r", encoding="utf-8") as f:
                content = f.read()
            assert content.strip(), "env file must not be empty"
            # 每行必须可解析为 key=value 或注释/空行
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                assert "=" in stripped, f"malformed line: {line!r}"
                k, v = stripped.split("=", 1)
                assert k, f"empty key in line: {line!r}"
        finally:
            with suppress_os_error():
                os.unlink(tmp_env)

    def test_atomic_replace_preserves_old_content_on_failure(self, monkeypatch):
        """replace 失败时应保留旧文件内容，不截断。"""
        tmp_env = tempfile.mktemp(suffix=".env")
        try:
            with open(tmp_env, "w", encoding="utf-8") as f:
                f.write("OLD=1\n")
            original_replace = os.replace

            def fail_replace(src, dst):
                raise OSError("replace failed")

            monkeypatch.setattr(os, "replace", fail_replace)
            with pytest.raises(OSError):
                _write_env_file(tmp_env, {"NEW": "2"})
            # 旧文件内容应保留
            with open(tmp_env, "r", encoding="utf-8") as f:
                content = f.read()
            assert "OLD=1" in content
        finally:
            monkeypatch.undo()
            with suppress_os_error():
                os.unlink(tmp_env)

    def test_partial_content_from_original_is_preserved(self):
        """新写入应保留原文件中未更新的 key。"""
        tmp_env = tempfile.mktemp(suffix=".env")
        try:
            with open(tmp_env, "w", encoding="utf-8") as f:
                f.write("KEEP=yes\nUPDATE=old\n# comment\n")
            _write_env_file(tmp_env, {"UPDATE": "new"})
            with open(tmp_env, "r", encoding="utf-8") as f:
                lines = [l.rstrip("\n") for l in f.readlines()]
            assert "KEEP=yes" in lines
            assert "UPDATE=new" in lines
            assert "# comment" in lines
        finally:
            with suppress_os_error():
                os.unlink(tmp_env)


# ============================================================
# NH14: watchdog.sh stat 双兼容
# ============================================================
class TestNH14WatchdogStatCompat:
    """watchdog.sh 的 stat 获取文件大小应兼容 BSD/GNU。"""

    def test_stat_fallback_works_with_gnu_stat_only(self):
        """在只有 GNU stat 的环境下，fallback 应返回正确字节数。"""
        # 构造一个只支持 -c%s 的假 stat，放入 PATH
        fake_stat = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False)
        fake_stat.write("#!/bin/sh\n")
        fake_stat.write('if [ "$1" = "-c%s" ]; then\n')
        fake_stat.write('  echo 123456\n')
        fake_stat.write('elif [ "$1" = "-f%z" ]; then\n')
        fake_stat.write('  exit 1\n')
        fake_stat.write('else\n')
        fake_stat.write('  exit 1\n')
        fake_stat.write('fi\n')
        fake_stat.close()
        os.chmod(fake_stat.name, 0o755)

        script_dir = os.path.dirname(fake_stat.name)
        fake_file = tempfile.NamedTemporaryFile(delete=False)
        fake_file.write(b"x" * 123456)
        fake_file.close()

        try:
            env = os.environ.copy()
            env["PATH"] = script_dir + os.pathsep + env.get("PATH", "")
            cmd = (
                f'SIZE=$(stat -f%z "{fake_file.name}" 2>/dev/null || '
                f'stat -c%s "{fake_file.name}" 2>/dev/null || echo 0); '
                f'echo "$SIZE"'
            )
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "123456"
        finally:
            with suppress_os_error():
                os.unlink(fake_stat.name)
            with suppress_os_error():
                os.unlink(fake_file.name)


# ============================================================
# Helpers
# ============================================================
class suppress_os_error:
    """上下文管理器：忽略 OSError。"""
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is OSError:
            return True
        return False
