"""Round 28 回归测试（日志驱动优化第二轮）。

1. ASR 审计 duration 计算修复（原 bug: t_asr - t_decode = t0 = 系统开机时长，
   生产日志出现 45 条 >1 分钟、最长 33.9 小时的荒谬 ASR 时长）
2. brain 失败日志可诊断性（原 bug: str(e) 为空 → "失败#3(累计3): " 空消息，
   watchdog 却基于此重启了服务）
"""
import re
from unittest.mock import patch


class TestASRAuditDuration:
    def test_duration_from_worker_not_external_t0(self):
        """duration 应来自 worker 内部计时，而非外部 t0（避免队列等待膨胀）"""
        src = open('app/xiaozhi_ws.py').read()
        # process_utterance 中不应再用外部 t0 计算 duration
        idx = src.find("def process_utterance")
        body = src[idx:idx + 2000]
        assert "t0 = time.monotonic()" not in body.split("async def")[0]
        # worker 应返回 duration_ms
        assert "duration_ms" in src

    def test_decode_check_asr_returns_duration(self):
        """_decode_check_asr 应返回 duration_ms（4 元素 tuple）"""
        from app.xiaozhi_ws import _decode_check_asr

        fake_wav = b"RIFF" + b"\x00" * 100
        with patch("app.xiaozhi_ws.opus_decode_to_wav", return_value=fake_wav), \
             patch("app.xiaozhi_ws.likely_empty_audio", return_value=False), \
             patch("voice_agent.asr_race", return_value=("测试", "sensevoice")):
            result = _decode_check_asr([b"frame1"])
        assert len(result) == 4
        wav, text, provider, duration_ms = result
        assert wav is fake_wav
        assert text == "测试"
        assert provider == "sensevoice"
        assert duration_ms > 0

    def test_no_uptime_scale_regression(self):
        """duration_ms 应来自 worker 内 perf_counter，量级为秒级而非开机时长"""
        src = open('app/xiaozhi_ws.py').read()
        # 不再使用 t_decode = time.monotonic() - t0 这种可能受队列延迟影响的模式
        assert "t_decode = time.monotonic() - t0" not in src
        # worker 内部应使用 perf_counter
        idx = src.find("def _decode_check_asr")
        block = src[idx:idx + 800]
        assert "time.perf_counter()" in block


class TestBrainFailureDiagnosability:
    def test_fmt_exc_exists(self):
        from agent.llm import _fmt_exc
        assert callable(_fmt_exc)

    def test_fmt_exc_empty_message(self):
        """空消息异常至少保留类名"""
        from agent.llm import _fmt_exc
        assert _fmt_exc(TimeoutError()) == "TimeoutError"

    def test_fmt_exc_with_message(self):
        from agent.llm import _fmt_exc
        result = _fmt_exc(ValueError("bad input"))
        assert result == "ValueError: bad input"

    def test_all_failure_sites_use_fmt_exc(self):
        """所有 _record_brain_failure(str(e)) 调用点应改为 _fmt_exc"""
        src = open('agent/llm.py').read()
        assert "_record_brain_failure(str(e)[:60])" not in src
        # 至少 2 处调用点使用 _fmt_exc
        # 截断宽度放宽到400以保证400类API错误可诊断（原60会截掉错误正文）
        assert src.count("_record_brain_failure(_fmt_exc(e)[:400])") >= 2

    def test_error_feature_matching_unaffected(self):
        """_fmt_exc 前缀不破坏错误特征匹配（timeout/mcp tool 包含判断）"""
        from agent.llm import _fmt_exc, _is_conn_error
        err = _fmt_exc(TimeoutError("MCP tool 'weather' timeout after 30s"))
        assert "timeout" in err.lower()
        assert _is_conn_error(err) is True
        # Round 26 的 MCP 轮换守卫应仍然命中（contains 分支）
        s = err.lower()
        assert s.startswith("mcp tool") or "mcp tool '" in s
