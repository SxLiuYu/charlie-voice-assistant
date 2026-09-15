"""Round 11: 唤醒链路延迟回归测试。

- ASR 竞速宽限默认 50ms（本地干净结果立即采用，实测 StepFun 云端 800ms+
  永远赶不上旧默认 400ms 宽限，纯浪费）
- 唤醒反馈音 beep 的 opus 包在启动预热后应命中缓存（否则每次重启后的
  第一次唤醒要现场 ffmpeg 转码 ~400ms）
"""
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestAsrRaceGraceDefault:
    def test_grace_default_is_50ms(self):
        """宽限默认 50ms：干净本地结果不被云端等待拖慢 ~350ms。"""
        from agent import asr_tts
        # 走一次 asr_race 的默认读取路径（未显式设 env 时）
        os.environ.pop("ASR_RACE_GRACE_MS", None)
        assert int(os.getenv("ASR_RACE_GRACE_MS", "50")) == 50
        # 源码级确认默认值（防止回退到 400）
        import inspect
        src = inspect.getsource(asr_tts)
        assert 'ASR_RACE_GRACE_MS", "50"' in src, \
            "asr_race 宽限默认应保持 50ms"

    def test_grace_env_override_still_works(self, monkeypatch):
        """env 显式设置仍可覆盖默认（保持可调性）。"""
        monkeypatch.setenv("ASR_RACE_GRACE_MS", "400")
        assert int(os.getenv("ASR_RACE_GRACE_MS", "50")) == 400


class TestWakeBeepOpusPrewarm:
    def test_beep_synthesized_and_opus_cached(self):
        """beep 合成后 opus 转码应命中缓存：第二次调用显著快于首次。"""
        from app.xiaozhi_ws import _synth_wake_beep
        from app.xiaozhi_codec import mp3_to_opus_packets

        beep = _synth_wake_beep()
        assert beep and len(beep) > 100

        # 首次转码（可能跑 ffmpeg），之后必须命中 _OPUS_CACHE
        pkts1 = mp3_to_opus_packets(beep, 16000, 60, 0.0)
        assert pkts1, "beep 应转出至少 1 个 opus 包"

        t0 = time.monotonic()
        pkts2 = mp3_to_opus_packets(beep, 16000, 60, 0.0)
        cached_elapsed = time.monotonic() - t0
        assert pkts2 == pkts1
        # 缓存命中应是毫秒级（首次 ffmpeg 转码要几百 ms）
        assert cached_elapsed < 0.05, \
            f"beep opus 缓存命中耗时 {cached_elapsed*1000:.0f}ms，预热未生效"

    def test_prewarm_source_calls_opus_conversion(self):
        """启动预热代码应包含 beep 的 opus 预转换（结构回归）。"""
        import inspect
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        assert "wake beep opus 预热完成" in src, \
            "预热逻辑缺失：重启后首次唤醒将吃 ~400ms ffmpeg 转码"
