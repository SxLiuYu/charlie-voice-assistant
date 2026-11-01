"""Round 11: 唤醒链路延迟回归测试。

- ASR 本地优先：本地干净结果立即采用，不再并发云端
- 唤醒反馈音 beep 的 opus 包在启动预热后应命中缓存（否则每次重启后的
  第一次唤醒要现场 ffmpeg 转码 ~400ms）
"""
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestAsrRaceLocalFirst:
    def test_local_first_no_cloud_wait(self):
        """本地优先：干净本地结果不再等云端，也不再并发发云端请求。

        旧实现并行发云端 + 等 400ms 宽限，白花 ~350ms；更糟的是云端 future
        没人取消，占满 2 worker 的池子，连续对话时本地任务排队超 deadline
        直接掉 legacy（581-1387ms）。现在本地干净就直接返回。
        """
        import inspect
        from agent import asr_tts
        src = inspect.getsource(asr_tts.asr_race)
        assert "ASR_RACE_GRACE_MS" not in src, \
            "本地优先后不该再等云端宽限"
        local_ret = src.index('return local_text, "sensevoice"')
        cloud_submit = src.index("pool.submit(cloud_fn")
        assert local_ret < cloud_submit, \
            "本地干净时应先于云端提交返回（云端只在本地空/乱码时才发）"


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
