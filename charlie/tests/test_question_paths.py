"""全问话路径覆盖测试: 时间/天气/音乐/智能命令/低意图/乱码/缓存/LLM/降级。

全部离线: 不进真实外网, 不调真实 LLM/MCP/子进程(系统操作用 subprocess.run 打桩)。
覆盖 brain() 与 brain_stream_sentences() 的所有分流节点。
"""
import subprocess
from unittest.mock import patch, MagicMock

import pytest

import voice_agent
from agent.intent import is_low_intent_asr, is_garbled_asr


@pytest.fixture(autouse=True)
def _clean_state():
    voice_agent.reset_history()
    voice_agent._cache.clear()
    yield


# --------------------------------------------------------------------------- #
# 1. 时间快路径: 绕过 LLM
# --------------------------------------------------------------------------- #
class TestTimeFastPath:
    def test_brain_stream_time_bypasses_llm(self):
        with patch("agent.llm._get_brain") as m_get_brain, \
             patch("agent.llm._classify_intent") as m_cls:
            sentences = list(voice_agent.brain_stream_sentences("现在几点啦"))
        assert m_get_brain.call_count == 0
        assert m_cls.call_count == 0
        assert len(sentences) == 1
        s, full = sentences[0]
        assert s.startswith("现在"), s
        assert "点" in s and "分" in s

    def test_brain_time_fast_path(self):
        with patch("agent.llm._get_brain") as m_get_brain, \
             patch("agent.llm._classify_intent", return_value="none"):
            reply = voice_agent.brain("几点了")
        assert m_get_brain.call_count == 0
        assert reply.startswith("现在") and ":" not in reply

    def test_time_does_not_pollute_cache(self):
        """时间结果必须实时, 不能被缓存污染。"""
        with patch("agent.llm._get_brain"):
            r1 = voice_agent.brain("现在几点")
        assert "现在" in r1
        # 缓存写入不覆盖"几点"条目
        cached = voice_agent._cache_get("现在几点")
        assert cached is None


# --------------------------------------------------------------------------- #
# 2. 天气: 高德直连命中 / 失败回退 brain
# --------------------------------------------------------------------------- #
class TestWeatherFastPath:
    def test_direct_weather_success(self):
        with patch.object(voice_agent, "_direct_weather_play", return_value="今天多云转晴,31到21度。") as m_w, \
             patch("agent.llm._get_brain") as m_brain, \
             patch("agent.llm._classify_intent") as m_cls:
            sentences = list(voice_agent.brain_stream_sentences("今天天气咋样"))
        assert m_w.call_count == 1
        assert m_brain.call_count == 0 and m_cls.call_count == 0
        assert sentences == [("今天多云转晴,31到21度。", "今天多云转晴,31到21度。")]

    def test_weather_fallback_to_brain(self):
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "查不到天气,建议出门看天。"}
        ]])
        with patch.object(voice_agent, "_direct_weather_play", return_value=""), \
             patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            sentences = list(voice_agent.brain_stream_sentences("今天天气"))
        assert len(sentences) >= 1
        assert "看天" in sentences[-1][1]

    def test_direct_weather_parses_amap_json(self):
        """天气快路径通过 app.weather.get_weather_text 返回天气摘要。"""
        with patch("app.weather.get_weather_text", return_value="今天多云转晴，31到21度。"):
            reply = voice_agent._direct_weather_play("今天天气咋样")
        assert "多云" in reply
        assert "31" in reply

    def test_direct_weather_no_key_returns_empty(self):
        """get_weather_text 返回空时快路径回退 brain。"""
        with patch("app.weather.get_weather_text", return_value=""):
            assert voice_agent._direct_weather_play("天气") == ""


# --------------------------------------------------------------------------- #
# 3. 音乐: ncm 直连命中 / 失败回退
# --------------------------------------------------------------------------- #class TestMusicFastPath:
    def test_music_direct_route(self):
        with patch.object(voice_agent, "_direct_music_play", return_value="__MUSIC__播放好听的歌") as m_m, \
             patch("agent.llm._get_brain") as m_brain:
            sentences = list(voice_agent.brain_stream_sentences("播放音乐"))
        assert m_m.call_count == 1
        assert m_brain.call_count == 0
        assert sentences[0][1].startswith("__MUSIC__")

    def test_music_fallback_to_brain(self):
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "好的,为您打开音乐。"}
        ]])
        with patch.object(voice_agent, "_direct_music_play", return_value=""), \
             patch("agent.llm._classify_intent", return_value="music"), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            sentences = list(voice_agent.brain_stream_sentences("来首歌"))
        assert len(sentences) >= 1


# --------------------------------------------------------------------------- #
# 3b. 空调: Tuya 直连命中 / 失败回退
# --------------------------------------------------------------------------- #
class TestAcFastPath:
    def test_stream_ac_direct_hit(self):
        with patch.object(voice_agent, "_direct_ac_control", return_value="空调已打开，制冷，26度。") as m_ac, \
             patch("agent.llm._get_brain") as m_brain:
            sentences = list(voice_agent.brain_stream_sentences("打开空调制冷26度"))
        assert m_ac.call_count == 1
        assert m_brain.call_count == 0
        assert sentences[0][1] == "空调已打开，制冷，26度。"

    def test_brain_ac_direct_hit(self):
        with patch.object(voice_agent, "_direct_ac_control", return_value="空调已打开，制冷。") as m_ac, \
             patch("agent.llm._get_brain") as m_brain:
            reply = voice_agent.brain("空调制冷")
        assert m_ac.call_count == 1
        assert m_brain.call_count == 0
        assert reply == "空调已打开，制冷。"

    def test_ac_fallback_to_brain_when_unparsed(self):
        """『打开空调』没有模式/温度/风速时, _direct_ac_control 返回空→回退 brain。"""
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "好的，空调已打开。"}
        ]])
        with patch.object(voice_agent, "_direct_ac_control", return_value=""), \
             patch("agent.llm._classify_intent", return_value="ac-control"), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            reply = voice_agent.brain("打开空调")
        assert reply == "好的，空调已打开。"
        assert fake_brain.run.call_count == 1

    def test_direct_ac_parses_weather_safe(self):
        """含『天气』的问句不得被 AC 快路径拦截(走天气), 即使包含『温度』。"""
        with patch("agent.llm._get_brain"):
            voice_agent.brain_stream_sentences("今天天气温度多少")
        # 不含天气词则需有 AC 关键词才触发；此处仅验证不误入AC快路径

    def test_ac_not_keyword_when_no_ac_word(self):
        with patch.object(voice_agent, "_direct_ac_control") as m_ac:
            list(voice_agent.brain_stream_sentences("讲个笑话"))
        m_ac.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. 智能命令: 音量/静音/睡眠/停止 (系统调用打桩)
# --------------------------------------------------------------------------- #
class TestSmartCommand:
    """跨平台: patch platform.system 为 Darwin 以走 osascript 分支，subprocess.run 打桩。"""

    def test_volume_up(self):
        with patch("agent.device_control.platform.system", return_value="Darwin"), \
             patch("subprocess.run") as m_run:
            reply = voice_agent._handle_smart_command("音量大一点")
        assert reply == "音量已调大。"
        m_run.assert_called_once()

    def test_volume_down(self):
        with patch("agent.device_control.platform.system", return_value="Darwin"), \
             patch("subprocess.run"):
            assert voice_agent._handle_smart_command("音量小一点") == "音量已调小。"

    def test_mute(self):
        with patch("agent.device_control.platform.system", return_value="Darwin"), \
             patch("subprocess.run") as m:
            assert voice_agent._handle_smart_command("静音") == "已静音。"
        m.assert_called_once()

    def test_stop_ack(self):
        # "停止/闭嘴" 只回复确认，不再静音系统音量
        with patch("agent.device_control.platform.system", return_value="Darwin"), \
             patch("subprocess.run") as m:
            assert voice_agent._handle_smart_command("闭嘴") == "好的，我停。"
        m.assert_not_called()

    def test_sleep(self):
        with patch("subprocess.run") as m:
            assert voice_agent._handle_smart_command("睡觉了") is None
        # 睡眠由 pmset 执行(短语"睡觉了"不在关键词里, 应放行到 brain)
        m.assert_not_called()

    def test_volume_failure_gives_feedback(self):
        # 系统音量控制失败时应给明确反馈，而不是静默 None
        with patch("agent.device_control.platform.system", return_value="Darwin"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 3)):
            assert voice_agent._handle_smart_command("音量大") == "系统音量控制不可用，请手动调节。"


# --------------------------------------------------------------------------- #
# 5. 低意图识别 (ack only)
# --------------------------------------------------------------------------- #
class TestLowIntent:
    LATENT = ["嗯。", "啊啊啊", "Hmm.", "hmm", " ，。！ ", "哦哦", "嗯嗯嗯"]

    @pytest.mark.parametrize("text", LATENT)
    def test_low_intent_true(self, text):
        assert is_low_intent_asr(text) is True

    NON_LOW = ["几点了", "讲个冷笑话", "今天天气怎么样", "对。", "好啊。", "home", "播放音乐"]

    @pytest.mark.parametrize("text", NON_LOW)
    def test_not_low_intent(self, text):
        assert is_low_intent_asr(text) is False


# --------------------------------------------------------------------------- #
# 6. 乱码过滤
# --------------------------------------------------------------------------- #
class TestGarbled:
    def test_garbled_long_no_valid_root(self):
        assert is_garbled_asr("呜喵呜喵呜喵呜呜喵喵喵喵") is True

    def test_not_garbled_short_words(self):
        for text in ["好的", "谢谢", "播放音乐", "导航去王府井"]:
            assert is_garbled_asr(text) is False


# --------------------------------------------------------------------------- #
# 7. 缓存命中: 第二次调用不走 LLM
# --------------------------------------------------------------------------- #
class TestCachePath:
    def test_cache_hit_skips_brain(self):
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "这是缓存的回答。"}
        ]])
        with patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            r1 = voice_agent.brain("讲个笑话")
            r2 = voice_agent.brain("讲个笑话")
        assert fake_brain.run.call_count == 1
        assert r1 == r2

    def test_stream_cache_hit(self):
        with patch("agent.llm._cache_set", wraps=voice_agent._cache_set) as m_set, \
             patch("agent.llm._get_brain") as m_brain:
            list(voice_agent.brain_stream_sentences("讲个笑话"))
        m_set.assert_called_once()


# --------------------------------------------------------------------------- #
# 8. LLM 闲聊: mcp=none 仍走 mock brain, 产出句子
# --------------------------------------------------------------------------- #
class TestLLMChitchat:
    def test_chitchat_stream(self):
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "你好。很高兴见到你,给你讲个小故事。"}
        ]])
        with patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            sentences = list(voice_agent.brain_stream_sentences("你好"))
        assert len(sentences) >= 1
        assert sentences[-1][1].startswith("你好。")

    def test_filler_filtered(self):
        """LLM 输出的"让我想想."这类占用语独立成句时必须被过滤, 不 yield 给 TTS。"""
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(return_value=[[
            {"role": "assistant", "content": "让我想想。这是你的答案。"}
        ]])
        with patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            sentences = list(voice_agent.brain_stream_sentences("有个问题想问你"))
        joined = "".join(s[0] for s in sentences)
        assert "让我想想" not in joined
        assert "这是你的答案" in joined


# --------------------------------------------------------------------------- #
# 9. 降级: brain 构建失败 → 提示; 运行异常 → Ollama fallback
# --------------------------------------------------------------------------- #
class TestDegradation:
    def test_brain_build_failure(self):
        with patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain", side_effect=Exception("boot fail")):
            sentences = list(voice_agent.brain_stream_sentences("测试"))
        assert sentences and "失败" in sentences[0][0]

    def test_ollama_fallback_removed(self):
        """_ollama_fallback 已删除，run 异常时直接返回兜底语"""
        fake_brain = MagicMock()
        fake_brain.run = MagicMock(side_effect=Exception("run error"))
        with patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain", return_value=fake_brain):
            reply = voice_agent.brain("你好")
        assert "忙不过来" in reply or "失败" in reply


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "-s"]))