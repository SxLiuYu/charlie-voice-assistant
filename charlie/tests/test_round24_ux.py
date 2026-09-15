"""Round 24 UX regression tests.

P0: _normalize_digits 小数朗读（35.5度→三十五点五度）
P1-A: TTS 失败保留原始回复文字
P1-B: 静音时段提醒推送飞书兜底
P1-C: 飞书空白回复改为自然提示
P1-D: ASR 填充词不剥离句尾"呢/吧"
"""
import os
from unittest.mock import patch, MagicMock


# ─── P0: Decimal number normalization ─────────────────────────────────────

class TestDecimalNormalization:
    def test_single_decimal_digit(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("35.5度") == "三十五点五度"

    def test_decimal_with_single_int(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("3.5度") == "三点五度"

    def test_decimal_zero_int(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("0.5度") == "零点五度"

    def test_decimal_multi_digits_after_point(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("3.14度") == "三点一四度"

    def test_decimal_one_digit_after_point(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("36.1度") == "三十六点一度"

    def test_integer_unchanged(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("100度") == "一百度"

    def test_integer_4_digit_unchanged(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("1234层") == "一千二百三十四层"

    def test_decimal_in_sentence(self):
        from agent.asr_tts import _normalize_digits
        result = _normalize_digits("今天气温35.5度，湿度60分")
        assert "三十五点五度" in result
        assert "六十分" in result

    def test_decimal_with_other_units(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("12.5分") == "十二点五分"
        assert _normalize_digits("3.5秒") == "三点五秒"

    def test_no_unit_decimal_unchanged(self):
        from agent.asr_tts import _normalize_digits
        # "个" is not a unit in _UNIT_AFTER_DIGIT, so no conversion
        assert _normalize_digits("3.5个") == "3.5个"


# ─── P1-A: TTS failure preserves reply ────────────────────────────────────

class TestTTSFailurePreservesReply:
    def test_reply_not_overwritten_on_tts_failure(self):
        """TTS 失败时 reply 应保留原始 brain 回复，而非替换为错误消息"""
        import voice_agent
        src = open(voice_agent.__file__).read()
        # 不应存在 reply = "抱歉.*语音合成 这样的覆盖
        assert 'reply = "抱歉' not in src
        assert '语音合成暂时不可用' not in src or 'reply' not in src.split('语音合成暂时不可用')[0][-50:]

    def test_audio_out_empty_on_failure(self):
        """TTS 失败时 audio_out 应为空字节"""
        import voice_agent
        src = open(voice_agent.__file__).read()
        # 在 except 块中应有 audio_out = b""
        assert 'audio_out = b""' in src


# ─── P1-B: Quiet hours Feishu fallback ────────────────────────────────────

class TestQuietHoursFeishuFallback:
    def test_feishu_push_in_quiet_hours(self):
        """静音时段：add_notification 推飞书（reminder 类型加入 FEISHU_HIGH_VALUE）"""
        import app.schedulers as sched
        src = open(sched.__file__).read()
        # add_notification 在静音时段块之前调用，reminder 类型已由 notifications.py 推飞书
        assert 'add_notification' in src
        assert '_feishu_done=True' in src or '_feishu_done = True' in src


# ─── P1-C: Feishu empty reply message ─────────────────────────────────────

class TestFeishuEmptyReply:
    def test_no_empty_placeholder(self):
        """飞书空白回复不应显示"(空回复)" """
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        assert "(空回复)" not in src

    def test_natural_fallback_message(self):
        """飞书空白回复应显示自然提示"""
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        assert "没听明白" in src or "再说一遍" in src


# ─── P1-D: ASR filler stripping preserves sentence-final particles ────────

class TestASRFillerSentenceFinal:
    def test_ne_not_stripped_from_end(self):
        """句尾"呢"应保留（疑问语气词）"""
        from agent.asr_correction import _strip_fillers
        assert _strip_fillers("怎么回事呢") == "怎么回事呢"

    def test_ba_not_stripped_from_end(self):
        """句尾"吧"应保留（推测/建议语气词）"""
        from agent.asr_correction import _strip_fillers
        assert _strip_fillers("你在吧") == "你在吧"

    def test_ne_not_in_filler_list(self):
        """呢 不应在填充词列表中"""
        from agent.asr_correction import _FILLER_WORDS
        assert "呢" not in _FILLER_WORDS

    def test_ba_not_in_filler_list(self):
        """吧 不应在填充词列表中"""
        from agent.asr_correction import _FILLER_WORDS
        assert "吧" not in _FILLER_WORDS

    def test_en_still_stripped_from_start(self):
        """句首"嗯"仍应被剥离（真正的填充词）"""
        from agent.asr_correction import _strip_fillers
        result = _strip_fillers("嗯你好")
        assert result == "你好"

    def test_en_still_stripped_from_end(self):
        """句尾"嗯"仍应被剥离"""
        from agent.asr_correction import _strip_fillers
        result = _strip_fillers("你好嗯")
        assert result == "你好"

    def test_a_still_stripped(self):
        """"啊"仍应被剥离"""
        from agent.asr_correction import _strip_fillers
        result = _strip_fillers("啊那个")
        assert result == "那个"
