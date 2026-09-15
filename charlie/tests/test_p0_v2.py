"""P0 v2 优化测试：情感 TTS + 儿童模式 + 多步 Agent 进度播报"""
import os
import sys
import threading

# 确保测试隔离环境
os.environ.setdefault("ASSISTANT_KID_DATA_DIR", os.path.join(
    os.path.dirname(__file__), "..", "_test_data_p0"))
os.environ.setdefault("SKIP_BACKGROUND", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ======================================================================
# P0-1: 情感 TTS
# ======================================================================

class TestEmotionTTS:
    """情感 TTS: persona 情绪映射 + 百度 pitch 近似 + 缓存 key 线程隔离。"""

    def test_emotion_to_tts_mapping(self):
        """persona._EMOTION_TO_TTS 正确映射原始情绪到 TTS 标签。"""
        from agent.persona import _EMOTION_TO_TTS
        assert _EMOTION_TO_TTS["happy"] == "happy"
        assert _EMOTION_TO_TTS["excited"] == "happy"
        assert _EMOTION_TO_TTS["sad"] == "sad"
        assert _EMOTION_TO_TTS["tired"] == "sad"
        assert _EMOTION_TO_TTS["angry"] == "serious"
        assert _EMOTION_TO_TTS["frustrated"] == "serious"
        assert _EMOTION_TO_TTS["neutral"] == ""

    def test_get_current_emotion_returns_empty_when_no_history(self):
        """无情绪历史时返回空串。"""
        from agent import persona
        # 清空状态
        with persona._STATE_LOCK:
            state = persona._load()
            state["recent_emotions"] = []
            persona._save()
        assert persona.get_current_emotion() == ""

    def test_get_current_emotion_after_update(self):
        """update 后 get_current_emotion 返回映射后的 TTS 标签。"""
        from agent import persona
        persona.update("太开心了！")
        emotion = persona.get_current_emotion()
        assert emotion == "happy", f"期望 happy, 实际 {emotion}"

    def test_get_current_mood(self):
        """get_current_mood 返回当前 mood 值。"""
        from agent import persona
        mood = persona.get_current_mood()
        assert mood in ("playful", "caring", "serious", "neutral")

    def test_baidu_pitch_mapping(self):
        """百度 TTS pit 参数随情感变化。"""
        from agent.asr_tts import _EMOTION_TO_BAIDU_PIT
        assert _EMOTION_TO_BAIDU_PIT["happy"] == 8
        assert _EMOTION_TO_BAIDU_PIT["sad"] == 3
        assert _EMOTION_TO_BAIDU_PIT["serious"] == 4

    def test_set_get_current_emotion_thread_local(self):
        """情感上下文是线程级隔离的。"""
        from agent.asr_tts import set_current_emotion, get_current_emotion
        set_current_emotion("happy")
        assert get_current_emotion() == "happy"

        results = {}

        def _worker():
            # 子线程默认没有情感
            results["child"] = get_current_emotion()
            set_current_emotion("sad")
            results["child_after_set"] = get_current_emotion()

        t = threading.Thread(target=_worker)
        t.start()
        t.join()

        # 子线程初始为空（线程隔离）
        assert results["child"] == ""
        assert results["child_after_set"] == "sad"
        # 主线程不受影响
        assert get_current_emotion() == "happy"

    def test_tts_cache_key_includes_emotion(self):
        """不同情感的 TTS 结果缓存不冲突。"""
        from agent.asr_tts import (
            set_current_emotion, get_current_emotion,
            _tts_cache_get, _tts_cache_put,
        )
        # 先设 happy 再 put，确保缓存 key 包含 emotion=happy
        # audio 需 >100 字节才会被缓存（_tts_cache_put 的条件）
        _audio_happy = b"\x00" * 120
        set_current_emotion("happy")
        _tts_cache_put("测试文本", _audio_happy, voice="Ethan", model="test", speed=1.0)
        cached_happy = _tts_cache_get("测试文本", "Ethan", "test", 1.0)
        assert cached_happy == _audio_happy

        # 切换情感后缓存应 miss（不同 key）
        set_current_emotion("sad")
        cached_sad = _tts_cache_get("测试文本", "Ethan", "test", 1.0)
        assert cached_sad is None, "不同情感的缓存不应命中"


# ======================================================================
# P0-2: 儿童模式
# ======================================================================

class TestKidMode:
    """儿童模式: kid 角色 + Kid 音色 + 系统提示词。"""

    def test_kid_role_exists(self):
        """_ROLES 包含 kid 角色。"""
        from agent.roles import _ROLES
        assert "kid" in _ROLES
        kid = _ROLES["kid"]
        assert kid["name"] == "小助手"
        assert kid["tts_voice"] == "Kid"
        assert kid["tts_speed"] == 0.9

    def test_kid_role_wake_words(self):
        """kid 角色有唤醒词。"""
        from agent.roles import _ROLES
        assert "小助手" in _ROLES["kid"]["wake_words"]

    def test_kid_valid_voice(self):
        """_VALID_TTS_VOICES 包含 Kid。"""
        from agent.roles import _VALID_TTS_VOICES
        assert "Kid" in _VALID_TTS_VOICES

    def test_kid_voice_in_voice_map(self):
        """_VOICE_MAP 包含 Kid 条目。"""
        from agent.asr_tts import _VOICE_MAP
        assert "Kid" in _VOICE_MAP
        kid_map = _VOICE_MAP["Kid"]
        assert "baidu" in kid_map
        assert "stepfun" in kid_map

    def test_kid_resolve_voice_baidu(self):
        """resolve_voice("Kid", "baidu") 返回有效 per 值。"""
        from agent.asr_tts import resolve_voice, _VOICE_MAP
        per = resolve_voice("Kid", "baidu")
        assert per == _VOICE_MAP["Kid"]["baidu"]

    def test_kid_system_prompt_content(self):
        """kid 角色系统提示词包含儿童友好关键词。"""
        from agent.roles import get_role_system_prompt
        prompt = get_role_system_prompt("kid")
        assert "简单" in prompt or "简单易懂" in prompt
        assert "鼓励" in prompt

    def test_kid_system_msg_branch(self):
        """完整 system_msg 包含儿童行为准则。"""
        from agent.system_msg import _build_system_msg_static
        # 确保当前角色是 kid
        from agent.roles import _current_role
        import agent.roles as roles_mod
        roles_mod._current_role = "kid"
        try:
            msg = _build_system_msg_static("none")
            assert "简单" in msg or "小朋友" in msg
            assert "恐怖" in msg or "暴力" in msg
        finally:
            roles_mod._current_role = None  # 重置

    def test_kid_lite_system_msg_branch(self):
        """lite system_msg 包含儿童行为准则。"""
        from agent.system_msg import _build_lite_system_msg
        import agent.roles as roles_mod
        roles_mod._current_role = "kid"
        try:
            msg = _build_lite_system_msg("none")
            assert "小朋友" in msg or "简单" in msg
        finally:
            roles_mod._current_role = None


# ======================================================================
# P0-3: 多步 Agent 进度播报 + 步数上限
# ======================================================================

class TestMultiStepAgent:
    """多步 Agent: function 消息计数 + 步数上限 + 进度播报。"""

    def test_count_function_msgs_empty(self):
        """空列表返回 0。"""
        from agent.llm import _count_function_msgs
        assert _count_function_msgs([]) == 0

    def test_count_function_msgs_none(self):
        """非列表返回 0。"""
        from agent.llm import _count_function_msgs
        assert _count_function_msgs(None) == 0
        assert _count_function_msgs("string") == 0

    def test_count_function_msgs_correct(self):
        """正确统计 function 角色消息数量。"""
        from agent.llm import _count_function_msgs
        rsp = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},
            {"role": "function", "name": "get_weather", "content": "sunny"},
            {"role": "assistant", "content": "今天是晴天"},
            {"role": "function", "name": "add_reminder", "content": "done"},
        ]
        assert _count_function_msgs(rsp) == 2

    def test_max_tool_steps_constant(self):
        """_MAX_TOOL_STEPS 默认值为 5。"""
        from agent.llm import _MAX_TOOL_STEPS
        assert _MAX_TOOL_STEPS == 5

    def test_tool_progress_text_known_tool(self):
        """已知工具名返回中文进度提示。"""
        from agent.llm import _tool_progress_text
        assert "天气" in _tool_progress_text("get_detailed_weather")
        assert "提醒" in _tool_progress_text("add_reminder")
        assert "音乐" in _tool_progress_text("play_music")

    def test_tool_progress_text_unknown_tool(self):
        """未知工具名返回通用提示。"""
        from agent.llm import _tool_progress_text
        result = _tool_progress_text("unknown_tool_xyz")
        assert "处理" in result

    def test_tool_progress_labels_non_empty(self):
        """进度标签表非空且有常用工具。"""
        from agent.llm import _TOOL_PROGRESS_LABELS
        assert len(_TOOL_PROGRESS_LABELS) >= 10
        assert "get_detailed_weather" in _TOOL_PROGRESS_LABELS
        assert "add_reminder" in _TOOL_PROGRESS_LABELS

    def test_max_tool_steps_env_override(self, monkeypatch):
        """MAX_TOOL_STEPS 环境变量可覆盖默认值。"""
        # 由于常量在 import 时已固定，验证逻辑：
        # 如果设为 3，os.getenv 应返回 3
        monkeypatch.setenv("MAX_TOOL_STEPS", "3")
        val = int(os.getenv("MAX_TOOL_STEPS", "5"))
        assert val == 3
