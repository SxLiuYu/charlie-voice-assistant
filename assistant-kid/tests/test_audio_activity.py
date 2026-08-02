"""
本地音频活跃度预检：在远端 ASR 前挡掉长静音/噪声。
"""
import io
import wave

from app import audio as app_audio


def _wav_bytes(samples=(), *, sample_rate=16000, channels=1, sample_width=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        if samples:
            wav.writeframes(b"".join(int(sample).to_bytes(sample_width, "little", signed=True) for sample in samples))
    return buf.getvalue()


def _silence(seconds, sample_rate=16000):
    return _wav_bytes((0 for _ in range(seconds * sample_rate)), sample_rate=sample_rate)


def _tone(seconds, amplitude=12000, sample_rate=16000):
    return _wav_bytes((amplitude for _ in range(seconds * sample_rate)), sample_rate=sample_rate)


def _silence_then_tone(silence_seconds=4, tone_seconds=1, amplitude=12000, sample_rate=16000):
    frames = [0 for _ in range(silence_seconds * sample_rate)]
    frames.extend(amplitude for _ in range(tone_seconds * sample_rate))
    return _wav_bytes(frames, sample_rate=sample_rate)


def _silence_with_scattered_clicks(seconds=30, sample_rate=16000):
    frames = [0 for _ in range(seconds * sample_rate)]
    click = 3000
    for offset in range(sample_rate // 2, len(frames), sample_rate * 3):
        for index in range(offset, min(offset + 80, len(frames))):
            frames[index] = click
    return _wav_bytes(frames, sample_rate=sample_rate)


def test_long_silence_is_detected_before_asr():
    assert app_audio.likely_empty_audio(_silence(5)) is True


def test_short_silence_is_allowed_to_reach_asr():
    assert app_audio.likely_empty_audio(_silence(1)) is False


def test_normal_voice_is_not_flagged():
    assert app_audio.likely_empty_audio(_tone(5)) is False


def test_voice_starting_after_silence_is_not_flagged():
    assert app_audio.likely_empty_audio(_silence_then_tone()) is False


def test_long_silence_with_scattered_clicks_is_still_flagged():
    assert app_audio.likely_empty_audio(_silence_with_scattered_clicks()) is True


def test_unparseable_audio_fails_open_and_reaches_asr():
    # 解析不确定时必须放行，避免误杀真实语音。
    assert app_audio.likely_empty_audio(b"not a wav file") is False


def test_unsupported_sample_width_is_allowed_to_reach_asr():
    wav = _wav_bytes((0 for _ in range(5 * 16000)), sample_width=4)

    assert app_audio.likely_empty_audio(wav) is False
