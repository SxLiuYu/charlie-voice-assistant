"""测试 LLM provider 注册表 — resolve()/active_chat_endpoint()/优先级链"""
import os
import pytest
from unittest.mock import patch


class TestProviderRegistry:
    def test_providers_has_five_providers(self):
        from app.llm_config import PROVIDERS
        assert set(PROVIDERS.keys()) == {"agnes", "sagnes", "stepfun", "glm", "ark"}

    def test_resolve_returns_agnes_when_configured(self):
        from app.llm_config import resolve
        cfg = resolve()
        assert cfg["model_type"] == "oai"
        assert cfg["api_base"]          # non-empty
        assert cfg["api_key"]           # non-empty
        assert "max_tokens" in cfg["generate_cfg"]
        assert "extra_body" in cfg["generate_cfg"]

    def test_resolve_raises_when_nothing_configured(self):
        # Patch all provider checks to return False → resolve() must raise RuntimeError
        from app import llm_config
        with patch.object(llm_config, '_is_provider_configured', return_value=False):
            with pytest.raises(RuntimeError):
                llm_config.resolve()

    def test_active_chat_endpoint_returns_tuple(self):
        from app.llm_config import active_chat_endpoint
        base, key, model = active_chat_endpoint()
        assert base   # non-empty
        assert key    # non-empty
        assert model  # non-empty

    def test_active_chat_endpoint_empty_when_unconfigured(self):
        from app import llm_config
        with patch.object(llm_config, '_is_provider_configured', return_value=False):
            base, key, model = llm_config.active_chat_endpoint()
            assert base == "" and key == "" and model == ""

    def test_priority_chain_picks_first_configured(self):
        # conftest sets AGNES_KEY + GLM_KEY; put GLM first to verify it wins
        from app import llm_config
        with patch.dict(os.environ, {"LLM_PRIORITY": "glm,agnes"}):
            llm_config.reload()
            cfg = llm_config.resolve()
            assert "glm" in cfg["model"]
        # Restore default so subsequent tests see the standard order
        with patch.dict(os.environ, {"LLM_PRIORITY": "stepfun,agnes,sagnes,glm,ark"}):
            llm_config.reload()

    def test_demo_mode_active_when_no_keys(self):
        from app import llm_config
        with patch.object(llm_config, '_is_provider_configured', return_value=False):
            assert llm_config.demo_mode_active() is True

    def test_demo_mode_inactive_when_key_configured(self):
        from app.llm_config import demo_mode_active
        # conftest sets AGNES_KEY="test-agnes-key"
        assert demo_mode_active() is False

    def test_is_configured_rejects_placeholders(self):
        """_is_configured 对空串 / 'xxx' / 'your-key' 返回 False，真实 key 返回 True"""
        from app.llm_config import _is_configured
        with patch.dict(os.environ, {"TEST_KEY": ""}):
            assert not _is_configured("TEST_KEY"), "empty string rejected"
        with patch.dict(os.environ, {"TEST_KEY": "xxx"}):
            assert not _is_configured("TEST_KEY"), "'xxx' placeholder rejected"
        with patch.dict(os.environ, {"TEST_KEY": "your-key"}):
            assert not _is_configured("TEST_KEY"), "'your-key' placeholder rejected"
        with patch.dict(os.environ, {"TEST_KEY": "real-key-123"}):
            assert _is_configured("TEST_KEY"), "real key accepted"

    def test_compatibility_functions_exist(self):
        from app.llm_config import (
            is_agnes_configured, is_sagnes_configured,
            is_stepfun_configured, is_glm_configured, is_ark_configured,
        )
        assert callable(is_agnes_configured)
        assert callable(is_sagnes_configured)
        assert callable(is_stepfun_configured)
        assert callable(is_glm_configured)
        assert callable(is_ark_configured)

    def test_get_glm_models_returns_list(self):
        from app.llm_config import get_glm_models
        models = get_glm_models()
        assert isinstance(models, list)

    def test_reload_picks_up_new_env(self):
        from app import llm_config
        with patch.dict(os.environ, {"LLM_PRIORITY": "ark"}):
            llm_config.reload()
            assert "ark" in llm_config.LLM_PRIORITY
        # Restore default so subsequent tests see the standard order
        with patch.dict(os.environ, {"LLM_PRIORITY": "stepfun,agnes,sagnes,glm,ark"}):
            llm_config.reload()

    def test_resolve_generate_cfg_structure(self):
        from app.llm_config import resolve
        cfg = resolve()
        gen = cfg["generate_cfg"]
        assert gen["use_raw_api"] is True
        assert isinstance(gen["max_tokens"], int)
        # StepFun 用 reasoning_effort=low，其他 provider 用 thinking.type=disabled
        eb = gen["extra_body"]
        assert "thinking" in eb or "reasoning_effort" in eb
        if "thinking" in eb:
            assert eb["thinking"]["type"] in ("disabled", "enabled")
        if "reasoning_effort" in eb:
            assert eb["reasoning_effort"] == "low"

    def test_llm_available_when_agnes_configured(self):
        from app.llm_config import llm_available
        assert llm_available() is True

    def test_provider_default_base(self):
        from app.llm_config import PROVIDERS
        assert PROVIDERS["agnes"]["default_base"] == "https://apihub.agnes-ai.com/v1"
        assert PROVIDERS["glm"]["default_base"] == "https://open.bigmodel.cn/api/paas/v4"
        assert PROVIDERS["ark"]["default_base"] == "https://ark.cn-beijing.volces.com/api/plan/v3"

    def test_provider_thinking_disabled_default(self):
        from app.llm_config import PROVIDERS
        for name, p in PROVIDERS.items():
            assert p["thinking_disabled"] is True, f"{name} should have thinking_disabled=True"

    def test_rotate_glm_model_visible_to_resolve(self):
        """轮换后 resolve() 必须返回新模型（C1 回归测试）"""
        from app import llm_config
        with patch.dict(os.environ, {
            "LLM_PRIORITY": "glm",
            "GLM_KEY": "test-glm-key",
            "GLM_MODELS": "glm-a,glm-b,glm-c",
            "GLM_MODEL": "glm-a",
        }):
            llm_config.reload()
            before = llm_config.resolve()["model"]
            assert before == "glm-a"
            llm_config.rotate_glm_model()
            after = llm_config.resolve()["model"]
            assert after != before, f"轮换后模型应变化: before={before}, after={after}"
            assert after in ("glm-b", "glm-c"), f"轮换到列表中的下一个: got {after}"
        # Restore
        with patch.dict(os.environ, {"LLM_PRIORITY": "stepfun,agnes,sagnes,glm,ark"}):
            llm_config.reload()

    def test_try_rotate_provider_on_failure_skips_stepfun(self):
        """StepFun 连续失败后，_try_rotate_provider_on_failure 应跳过 stepfun 选下一个已配置 provider"""
        from agent import llm as _llm_mod
        from agent.llm import _try_rotate_provider_on_failure
        from app import llm_config
        with patch.dict(os.environ, {
            "LLM_PRIORITY": "stepfun,agnes",
            "STEPFUN_KEY": "test-stepfun-key",
            "AGNES_KEY": "test-agnes-key",
        }):
            llm_config.reload()
            # 当前激活的是 stepfun
            base, _, _ = llm_config.active_chat_endpoint()
            assert "stepfun" in base
            # 未达轮换阈值（连续失败 < 2）不轮换
            _llm_mod._provider_fail_counts.clear()
            assert _try_rotate_provider_on_failure() is False
            # 连续失败达到阈值后轮换
            _llm_mod._note_provider_failure()
            _llm_mod._note_provider_failure()
            rotated = _try_rotate_provider_on_failure()
            assert rotated is True
            base, _, _ = llm_config.active_chat_endpoint()
            assert "agnes" in base
            # 成功后计数清零（恢复机制）
            assert "stepfun" not in _llm_mod._provider_fail_counts
        # Restore
        with patch.dict(os.environ, {"LLM_PRIORITY": "stepfun,agnes,sagnes,glm,ark"}):
            llm_config.reload()
            _llm_mod._provider_fail_counts.clear()
