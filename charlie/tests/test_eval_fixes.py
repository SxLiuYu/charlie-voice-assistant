"""回归测试：2026-08-26 大脑&硬件链路评估修复（见 FIX_PLAN.md）

覆盖可离线验证的修复项：
- B1 响应缓存按 session/角色隔离（agent/cache.py）
- B3 working_memory 按会话保存/恢复（agent/working_memory.py）
- B4 persona 并发安全 + 原子落盘（agent/persona.py）
- H3 空调快路径否定/陈述守卫 + 失败反馈（agent/device_control.py）
- H8 百度 token 鉴权错误失效（agent/asr_tts.py）
"""
import json
import os
import threading

import pytest
from unittest.mock import patch, MagicMock


# ===== B1: 响应缓存会话隔离 =====

class TestCacheSessionIsolation:
    def setup_method(self):
        from agent import cache
        with cache._cache_lock:
            cache._cache.clear()

    def teardown_method(self):
        self.setup_method()

    def test_same_session_hits(self):
        from agent.cache import _cache_get, _cache_set
        _cache_set("明天呢", "明天晴", session_id="dev-a")
        assert _cache_get("明天呢", session_id="dev-a") == "明天晴"

    def test_cross_session_isolated(self):
        """短指代句的答案属于特定会话上下文，不得跨会话命中"""
        from agent.cache import _cache_get, _cache_set
        _cache_set("明天呢", "北京明天晴", session_id="dev-a")
        assert _cache_get("明天呢", session_id="dev-b") is None

    def test_role_change_invalidates(self):
        from agent.cache import _cache_get, _cache_set
        from agent import roles
        _cache_set("讲个笑话", "charlie版笑话", session_id="s")
        # 直接改 _current_role 而非 switch_role()：全量跑时 switch 的持久化
        # 依赖外部状态（偏好文件/角色表），这里只验证缓存 key 含角色维度
        saved = roles._current_role
        try:
            roles._current_role = "jarvis"
            assert _cache_get("讲个笑话", session_id="s") is None
            _cache_set("讲个笑话", "jarvis版笑话", session_id="s")
            assert _cache_get("讲个笑话", session_id="s") == "jarvis版笑话"
        finally:
            roles._current_role = saved

    def test_backward_compat_default_session(self):
        """不传 session_id 的旧调用方仍走 default 桶"""
        from agent.cache import _cache_get, _cache_set
        _cache_set("你好", "你好呀")
        assert _cache_get("你好") == "你好呀"


# ===== B3: working_memory 会话隔离 =====

class TestWorkingMemorySwitchSession:
    def setup_method(self):
        from agent import working_memory as wm
        # 重置 thread-local session 到 default，清空所有会话数据
        wm._session_data.clear()
        wm._session_local = threading.local()
        wm.switch_session("default")
        wm.reset()

    def test_switch_to_new_session_clears_state(self):
        from agent import working_memory as wm
        wm.update(facts={"城市": "北京"}, topic="天气")
        wm.switch_session("xiaozhi-dev-b")
        assert wm.get_fact("城市") == ""
        assert wm.get()["last_topic"] == ""

    def test_switch_back_restores_previous_session(self):
        from agent import working_memory as wm
        wm.update(facts={"城市": "北京"})
        wm.switch_session("xiaozhi-dev-b")
        wm.update(facts={"城市": "上海"})
        wm.switch_session("default")
        # 回到原会话：事实恢复且不丢失
        assert wm.get_fact("城市") == "北京"

    def test_facts_survive_across_turns_in_same_session(self):
        """修复的核心场景：会话 B 活跃期间其事实跨轮次保留，
        不再被'快照恢复'回滚"""
        from agent import working_memory as wm
        wm.switch_session("xiaozhi-dev-b")
        wm.update(facts={"项目": "charlie"})
        # 模拟下一轮请求：同 session 不触发切换
        wm.switch_session("xiaozhi-dev-b")
        assert wm.get_fact("项目") == "charlie"

    def test_same_session_noop(self):
        from agent import working_memory as wm
        wm.update(facts={"k": "v"})
        wm.switch_session("default")
        assert wm.get_fact("k") == "v"

    def test_store_bounded(self):
        from agent import working_memory as wm
        for i in range(20):
            wm.switch_session(f"tmp-{i}")
        assert len(wm._session_data) <= wm._SESSION_STORE_MAX


# ===== B4: persona 原子写 =====

class TestPersonaAtomicSave:
    def test_save_produces_valid_json(self, tmp_path):
        persona_file = tmp_path / "persona_state.json"
        with patch.dict(os.environ, {"ASSISTANT_KID_DATA_DIR": str(tmp_path)}):
            import importlib
            from agent import persona
            importlib.reload(persona)
            persona._state = None  # 强制重新加载（新目录 → 默认值）
            persona.update("今天真开心哈哈")
            data = json.loads(persona_file.read_text(encoding="utf-8"))
            assert data["interaction_count"] == 1

    def test_concurrent_updates_no_lost_counts(self, tmp_path):
        with patch.dict(os.environ, {"ASSISTANT_KID_DATA_DIR": str(tmp_path)}):
            import importlib
            from agent import persona
            importlib.reload(persona)
            persona._state = None
            threads = [threading.Thread(target=persona.update, args=("测试消息",))
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert persona._load()["interaction_count"] == 8


# ===== H3: 空调快路径守卫与失败反馈 =====

@pytest.fixture()
def mock_tuya_true():
    env = {
        "TUYA_IR_DEVICE_ID": "ir-123",
        "TUYA_AC_DEVICE_ID": "ac-123",
        "TUYA_CLIENT_ID": "cid",
        "TUYA_ACCESS_KEY": "sk-xxx",
    }
    with patch.dict(os.environ, env, clear=False), \
         patch("tuya_api.TuyaCloudAPI") as MockAPI:
        instance = MockAPI.return_value
        instance.ac_status.return_value = {"power": 0}
        instance.ac_scenes_command.return_value = True
        yield instance


class TestACNegationGuard:
    def test_negation_does_not_power_on(self, mock_tuya_true):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("不要开空调")
        mock_tuya_true.ac_scenes_command.assert_not_called()
        assert result == ""

    def test_negation_does_not_power_off(self, mock_tuya_true):
        from agent.device_control import direct_ac_control
        direct_ac_control("先别关空调")
        mock_tuya_true.ac_scenes_command.assert_not_called()

    def test_statement_past_tense_does_not_power_off(self, mock_tuya_true):
        """'我把空调关了' 是陈述不是指令"""
        from agent.device_control import direct_ac_control
        direct_ac_control("我不小心把空调关了")
        mock_tuya_true.ac_scenes_command.assert_not_called()

    def test_imperative_with_wo_still_works(self, mock_tuya_true):
        """'我要开空调' 是指令不是陈述"""
        from agent.device_control import direct_ac_control
        result = direct_ac_control("我要开空调")
        mock_tuya_true.ac_scenes_command.assert_called_once()
        assert "已打开" in result or "打开" in result


class TestACFailureFeedback:
    def test_failed_off_reports_failure(self, mock_tuya_true):
        """命令返回 False 时必须如实反馈，不能播报假成功"""
        from agent.device_control import direct_ac_control
        mock_tuya_true.ac_scenes_command.return_value = False
        result = direct_ac_control("关闭空调")
        assert "没生效" in result
        assert "已关闭" not in result

    def test_failed_on_reports_failure(self, mock_tuya_true):
        from agent.device_control import direct_ac_control
        mock_tuya_true.ac_scenes_command.return_value = False
        result = direct_ac_control("打开空调")
        assert "没生效" in result

    def test_success_reports_ok(self, mock_tuya_true):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("关闭空调")
        assert "已关闭" in result


# ===== H8: 百度 token 鉴权错误失效 =====

class TestBaiduTokenInvalidation:
    def test_invalidate_clears_token(self):
        from agent import asr_tts
        with asr_tts._baidu_token_lock:
            asr_tts._baidu_token["token"] = "stale-token"
            asr_tts._baidu_token["at"] = 1.0
        asr_tts._baidu_invalidate_token()
        assert asr_tts._baidu_token["token"] == ""
        assert asr_tts._baidu_token["at"] == 0.0

    def test_asr_auth_error_invalidates_token(self):
        """err_no=110（token 无效）必须清 token，否则坏 token 用满 28 天"""
        from agent import asr_tts
        resp = MagicMock()
        resp.json.return_value = {"err_no": 110, "err_msg": "Access token invalid"}
        resp.raise_for_status.return_value = None
        fake_session = MagicMock()
        fake_session.post.return_value = resp
        fake_token_store = {"token": "stale", "at": __import__("time").time()}
        with patch.object(asr_tts, "_baidu_token", fake_token_store):
            with patch("voice_agent._session", fake_session):
                with pytest.raises(RuntimeError):
                    asr_tts._asr_baidu(b"1234", "wav")
                # 必须在 patch 上下文内断言：退出后 conftest 快照会还原原 dict
                assert fake_token_store["token"] == ""
