"""Round 23 UX regression tests.

P1-A: _normalize_digits 支持 3-4 位数字（100度→一百度）
P1-B: max_sentences 适配状态真正限制 LLM 流式输出句数
P1-C: 自进化系统使用动态历史文件路径（多用户支持）
"""
import os
import json
import tempfile
import importlib
from unittest.mock import patch, MagicMock


# ─── P1-A: _normalize_digits ──────────────────────────────────────────────

class TestNormalizeDigitsMultiDigit:
    def test_1_digit(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("5度") == "五度"

    def test_2_digit_teens(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("15层") == "十五层"

    def test_2_digit_tens(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("20分") == "二十分"

    def test_3_digit_hundreds(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("100度") == "一百度"

    def test_3_digit_with_tens(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("120层") == "一百二十层"

    def test_3_digit_all_nonzero(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("123号") == "一百二十三号"

    def test_3_digit_zero_middle(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("105岁") == "一百零五岁"

    def test_4_digit_thousands(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("2000度") == "二千度"

    def test_4_digit_full(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("1234层") == "一千二百三十四层"

    def test_4_digit_zero_middle(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("1005号") == "一千零五号"

    def test_no_unit_no_change(self):
        from agent.asr_tts import _normalize_digits
        assert _normalize_digits("100个") == "100个"

    def test_mixed_text(self):
        from agent.asr_tts import _normalize_digits
        result = _normalize_digits("今天25度，湿度60分")
        assert "二十五度" in result
        assert "六十分" in result


# ─── P1-B: max_sentences guard ────────────────────────────────────────────

class TestMaxSentencesGuard:
    def test_get_max_sentences_no_file(self):
        """无 evolution_data 文件时返回 0（无限制）"""
        from agent.llm import _get_max_sentences
        old = os.environ.get("ASSISTANT_KID_DATA_DIR")
        try:
            os.environ["ASSISTANT_KID_DATA_DIR"] = "/tmp/nonexistent_round23_test"
            result = _get_max_sentences()
            assert result == 0
        finally:
            if old is not None:
                os.environ["ASSISTANT_KID_DATA_DIR"] = old
            else:
                os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_get_max_sentences_with_value(self):
        """有 evolution_data 且设置了 max_sentences 时返回对应值"""
        from agent.llm import _get_max_sentences
        with tempfile.TemporaryDirectory() as tmpdir:
            ev_file = os.path.join(tmpdir, "evolution_data.json")
            with open(ev_file, 'w', encoding='utf-8') as f:
                json.dump({"adaptation_state": {"max_sentences": 3}}, f)
            old = os.environ.get("ASSISTANT_KID_DATA_DIR")
            old_uid = os.environ.get("CHARLIE_USER_ID")
            try:
                os.environ["ASSISTANT_KID_DATA_DIR"] = tmpdir
                os.environ["CHARLIE_USER_ID"] = "default"
                result = _get_max_sentences()
                assert result == 3
            finally:
                if old is not None:
                    os.environ["ASSISTANT_KID_DATA_DIR"] = old
                else:
                    os.environ.pop("ASSISTANT_KID_DATA_DIR", None)
                if old_uid is not None:
                    os.environ["CHARLIE_USER_ID"] = old_uid
                else:
                    os.environ.pop("CHARLIE_USER_ID", None)

    def test_get_max_sentences_no_field(self):
        """evolution_data 存在但没有 max_sentences 字段时返回 0"""
        from agent.llm import _get_max_sentences
        with tempfile.TemporaryDirectory() as tmpdir:
            ev_file = os.path.join(tmpdir, "evolution_data.json")
            with open(ev_file, 'w', encoding='utf-8') as f:
                json.dump({"adaptation_state": {"response_style": "精简"}}, f)
            old = os.environ.get("ASSISTANT_KID_DATA_DIR")
            old_uid = os.environ.get("CHARLIE_USER_ID")
            try:
                os.environ["ASSISTANT_KID_DATA_DIR"] = tmpdir
                os.environ["CHARLIE_USER_ID"] = "default"
                result = _get_max_sentences()
                assert result == 0
            finally:
                if old is not None:
                    os.environ["ASSISTANT_KID_DATA_DIR"] = old
                else:
                    os.environ.pop("ASSISTANT_KID_DATA_DIR", None)
                if old_uid is not None:
                    os.environ["CHARLIE_USER_ID"] = old_uid
                else:
                    os.environ.pop("CHARLIE_USER_ID", None)

    def test_system_msg_injects_max_sentences(self):
        """system_msg 的 _build_user_profile 应包含 max_sentences 指令"""
        import agent.system_msg as sm_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            ev_file = os.path.join(tmpdir, "evolution_data.json")
            with open(ev_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "adaptation_state": {"max_sentences": 2, "response_style": "极简"},
                    "learned_preferences": {},
                    "usage_patterns": {}
                }, f)
            orig_dir = sm_mod.DATA_DIR
            try:
                sm_mod.DATA_DIR = tmpdir
                sm_mod.invalidate_system_msg_cache()
                result = sm_mod._build_user_profile()
                assert "不超过2句" in result
            finally:
                sm_mod.DATA_DIR = orig_dir
                sm_mod.invalidate_system_msg_cache()

    def test_system_msg_no_max_sentences_when_unset(self):
        """max_sentences 未设置时不应注入"""
        import agent.system_msg as sm_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            ev_file = os.path.join(tmpdir, "evolution_data.json")
            with open(ev_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "adaptation_state": {"response_style": "平衡"},
                    "learned_preferences": {},
                    "usage_patterns": {}
                }, f)
            orig_dir = sm_mod.DATA_DIR
            try:
                sm_mod.DATA_DIR = tmpdir
                sm_mod.invalidate_system_msg_cache()
                result = sm_mod._build_user_profile()
                assert "不超过" not in result
            finally:
                sm_mod.DATA_DIR = orig_dir
                sm_mod.invalidate_system_msg_cache()


# ─── P1-C: dynamic history file path ──────────────────────────────────────

class TestDynamicHistoryPath:
    def test_evolution_uses_get_history_file(self):
        """magic-evolution.py 应调用 agent.history._get_history_file 而非硬编码"""
        ev_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "magic-evolution.py")
        src = open(ev_path).read()
        assert "_get_history_file" in src
        # 不应再直接拼接 conversation_history.json
        assert '"conversation_history.json")' not in src

    def test_summary_uses_get_history_file(self):
        """magic-summary.py 应调用 agent.history._get_history_file"""
        sm_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "magic-summary.py")
        src = open(sm_path).read()
        assert "_get_history_file" in src

    def test_history_file_multi_user(self):
        """_get_history_file 在设置 CHARLIE_USER_ID 时返回用户特定路径"""
        from agent.history import _get_history_file
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "alice"}):
            path = _get_history_file()
            assert "conversation_history_alice.json" in path

        with patch.dict(os.environ, {"CHARLIE_USER_ID": "bob"}):
            path = _get_history_file()
            assert "conversation_history_bob.json" in path
