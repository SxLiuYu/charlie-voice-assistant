"""测试关键词→LLM 路由改造：动作类意图走 LLM 分类+工具，而非关键词短路。
覆盖三个改动点：
1. FAST_PATHS 仅保留角色切换/社交，动作类已移除
2. _classify_intent 单域命中但复合句不走短路
3. is_garbled_asr 放宽白名单，不误杀正常短句
"""
import os
import sys

# 确保能 import charlie 模块
_CHARLIE_DIR = os.path.join(os.path.dirname(__file__), "..")
if _CHARLIE_DIR not in sys.path:
    sys.path.insert(0, _CHARLIE_DIR)


class TestFastPathsTrimmed:
    """FAST_PATHS 仅保留角色切换和社交，动作类已移除。"""

    def test_fast_paths_count(self):
        from voice_agent import FAST_PATHS
        assert len(FAST_PATHS) == 2, f"Expected 2 fast paths, got {len(FAST_PATHS)}: {[p.name for p in FAST_PATHS]}"

    def test_role_switch_kept(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "role_switch" in names

    def test_social_kept(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "social" in names

    def test_time_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "time" not in names

    def test_weather_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "weather" not in names

    def test_music_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "music" not in names

    def test_ac_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "ac" not in names

    def test_vision_camera_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "vision_camera" not in names

    def test_vision_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "vision" not in names

    def test_scene_removed(self):
        from voice_agent import FAST_PATHS
        names = [p.name for p in FAST_PATHS]
        assert "scene" not in names

    def test_brain_docstring_updated(self):
        from voice_agent import brain
        assert "LLM" in brain.__doc__ or "llm" in brain.__doc__.lower()


class TestCompoundQueryDetection:
    """_is_compound_query 正确识别复合/复杂查询。"""

    def test_short_simple_not_compound(self):
        from agent.llm import _is_compound_query
        assert not _is_compound_query("几点了")
        assert not _is_compound_query("今天天气")
        assert not _is_compound_query("播放音乐")

    def test_long_text_is_compound(self):
        from agent.llm import _is_compound_query
        assert _is_compound_query("给我详细讲讲今天的天气适合穿什么衣服")
        assert _is_compound_query("帮我查一下明天北京的天气然后设个提醒")
        assert _is_compound_query("今天天气怎么样顺便帮我推荐个菜谱")

    def test_compound_indicators(self):
        from agent.llm import _is_compound_query
        assert _is_compound_query("天气和穿搭")
        assert _is_compound_query("查天气然后设提醒")
        assert _is_compound_query("播音乐顺便查天气")
        assert _is_compound_query("天气适合穿什么")

    def test_short_no_indicator_not_compound(self):
        from agent.llm import _is_compound_query
        assert not _is_compound_query("开空调")
        assert not _is_compound_query("晚安")
        assert not _is_compound_query("来首歌")


class TestGarbledAsrRelaxed:
    """is_garbled_asr 放宽后不误杀正常短句。"""

    def test_pure_filler_short_is_garbled(self):
        from agent.intent import is_garbled_asr
        assert is_garbled_asr("嗯嗯啊啊")
        assert is_garbled_asr("哦哦")

    def test_empty_is_garbled(self):
        from agent.intent import is_garbled_asr
        # 空字符串和纯空格：is_garbled_asr 返回 False（空文本由上层处理），
        # 纯标点空格返回 True
        assert not is_garbled_asr("")
        assert is_garbled_asr("   ")

    def test_valid_short_texts_not_garbled(self):
        from agent.intent import is_garbled_asr
        assert not is_garbled_asr("你好")
        assert not is_garbled_asr("谢谢")
        assert not is_garbled_asr("几点了")

    def test_common_phrases_not_garbled(self):
        """日常用语不应被误杀（旧白名单会误判这些为乱码）。"""
        from agent.intent import is_garbled_asr
        assert not is_garbled_asr("出门")
        assert not is_garbled_asr("回家")
        assert not is_garbled_asr("吃饭")
        assert not is_garbled_asr("帮我")
        assert not is_garbled_asr("打电话")

    def test_normal_sentence_not_garbled(self):
        from agent.intent import is_garbled_asr
        assert not is_garbled_asr("今天天气怎么样")
        assert not is_garbled_asr("帮我播放一首歌")
        assert not is_garbled_asr("明天提醒我开会")

    def test_long_text_with_valid_chars_not_garbled(self):
        """长文本包含有效字符时不应判乱码（旧逻辑≥10字无白名单字即判乱码）。"""
        from agent.intent import is_garbled_asr
        assert not is_garbled_asr("帮我查一下明天的天气然后设个提醒")
        assert not is_garbled_asr("今天出门需要带伞吗顺便帮我叫个车")

    def test_pure_noise_still_garbled(self):
        """纯噪声（非填充词、非有效字符）仍应判乱码。"""
        from agent.intent import is_garbled_asr
        # 生僻符号组合
        assert is_garbled_asr("呲呲呲呲呲呲呲呲呲呲")

    def test_very_short_no_valid_chars(self):
        from agent.intent import is_garbled_asr
        assert is_garbled_asr("呲")

    def test_moderate_filler_heavy(self):
        """中等长度且填充词占比高 → 乱码。"""
        from agent.intent import is_garbled_asr
        assert is_garbled_asr("嗯啊哦嗯啊")


class TestClassifyIntentCompoundGuard:
    """_classify_intent 对复合句不走关键词短路（需要 mock LLM）。"""

    def test_compound_query_does_not_short_circuit(self, monkeypatch):
        """复合查询即使单域关键词命中也应走 LLM 分类。"""
        import agent.llm as llm_mod

        # 模拟 LLM 分类返回值
        llm_called = []

        def fake_post(url, json, headers, timeout):
            llm_called.append(json["messages"][0]["content"][:50])

            class FakeResp:
                def json(self):
                    return {"choices": [{"message": {"content": "amap-maps"}}]}
            return FakeResp()

        monkeypatch.setattr(llm_mod._session, "post", fake_post)
        monkeypatch.setattr(llm_mod, "_llm_has_any_key", lambda: True)
        monkeypatch.setattr(llm_mod, "_demo_mode_active", lambda: False)

        # 清除缓存
        with llm_mod._st.intent_cache_lock:
            llm_mod._st.intent_cache.clear()
        llm_mod._st.intent_disabled_until = 0.0

        # 复合查询："今天天气适合穿什么衣服" 命中天气域但句子复杂
        result = llm_mod._classify_intent("今天天气适合穿什么衣服")
        assert len(llm_called) > 0, "复合查询应调用 LLM 分类，不应关键词短路"
        assert result == "amap-maps"

    def test_simple_query_can_short_circuit(self, monkeypatch):
        """简单短句单域命中仍可关键词短路，不调 LLM。"""
        import agent.llm as llm_mod

        llm_called = []

        def fake_post(url, json, headers, timeout):
            llm_called.append(json)
            class FakeResp:
                def json(self):
                    return {"choices": [{"message": {"content": "amap-maps"}}]}
            return FakeResp()

        monkeypatch.setattr(llm_mod._session, "post", fake_post)

        with llm_mod._st.intent_cache_lock:
            llm_mod._st.intent_cache.clear()
        llm_mod._st.intent_disabled_until = 0.0

        # 简单短句 "今天天气" → 单域命中 amap-maps，短路
        result = llm_mod._classify_intent("今天天气")
        assert len(llm_called) == 0, "简单短句应关键词短路，不调 LLM"
        assert result == "amap-maps"
