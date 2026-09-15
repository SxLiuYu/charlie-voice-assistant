"""
Charlie - pytest配置
设置sys.path + 公共fixtures
"""
import sys, os, tempfile, copy, threading
import pytest
# 确保能导入项目模块
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# 设置环境变量(测试用, 避免加载真实密钥)
os.environ.setdefault("FINNA_BASE", "https://test.example.com/v1")
os.environ.setdefault("GLM_KEY", "test-glm-key")
os.environ.setdefault("TTS_KEY", "test-tts-key")
os.environ.setdefault("ASR_KEY", "test-asr-key")
os.environ.setdefault("TTS_VOICE", "Cherry")
os.environ.setdefault("SKIP_BACKGROUND", "1")  # 跳过后台调度器
os.environ.setdefault("AGNES_KEY", "test-agnes-key")  # LLM provider 测试用

# 测试必须使用独立数据/日志目录，避免覆盖真实对话、提醒、偏好和运行日志。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="charlie-test-data-")
_TEST_LOG_DIR = tempfile.mkdtemp(prefix="charlie-test-logs-")
os.makedirs(_TEST_DATA_DIR, exist_ok=True)
os.makedirs(_TEST_LOG_DIR, exist_ok=True)
os.environ["ASSISTANT_KID_DATA_DIR"] = _TEST_DATA_DIR
os.environ["ASSISTANT_KID_LOG_DIR"] = _TEST_LOG_DIR


def pytest_sessionfinish(session, exitstatus):
    """测试结束后清理临时数据和日志目录，避免pytest垃圾堆积。"""
    import shutil
    for path in (_TEST_DATA_DIR, _TEST_LOG_DIR):
        shutil.rmtree(path, ignore_errors=True)


# ===== 测试间全局状态隔离安全网 =====
# 每个测试前快照、yield 后恢复关键模块级状态，防止跨测试污染。

_STATE_SNAPSHOT_KEYS = [
    # (module_import_path, attribute_name)
    # 注意：voice_agent._brains 是 llm_state.brains 的引用，两边一起拍快照
    ("voice_agent", "_brains"),
    ("agent.llm_state", "brain_failures"),
    ("agent.llm_state", "intent_failures"),
    ("agent.llm_state", "intent_disabled_until"),
    ("agent.asr_tts", "_tts_failures"),
    ("agent.asr_tts", "_tts_unavailable_until"),
    ("app.xiaozhi_ws", "_finna_cooldown_until"),
    ("agent.asr_tts", "_tts_cache"),
    ("agent.asr_tts", "_baidu_token"),
    ("agent.asr_tts", "_asr_fallback_times"),
    ("agent.asr_tts", "_tts_speed"),
    ("agent.asr_tts", "TTS_VOICE"),
    ("app.xiaozhi_ws", "_prewarm_started"),
    ("app.xiaozhi_ws", "_dead_link_count"),
    ("app.xiaozhi_ws", "_rebuild_last_time"),
    # P2-A 补充：provider 轮换/意图/角色/LLM 关键状态
    ("agent.llm", "_provider_fail_counts"),
    ("app.llm_config", "_glm_rotation_idx"),
    ("app.llm_config", "GLM_MODEL"),
    ("agent.roles", "_current_role"),
    ("agent.roles", "_ROLES"),
    ("agent.intent", "_WAKE_STRIP_RE_DYNAMIC"),
    ("agent.llm_state", "intent_cache"),
    ("agent.llm_state", "brain_total_failures"),
    # 第五轮补充：会话/缓存/patch 安装状态（app.state 全部模块级 mutable）
    ("agent.cache", "_cache"),
    ("agent.llm", "_wm_local"),
    ("app.state", "_rate_buckets"),
    ("app.state", "_session_buckets"),
    ("app.state", "_sse_clients"),
    ("app.state", "_ws_clients"),
    ("app.state", "_ws_session_groups"),
    ("app.state", "_ws_client_locations"),
    ("app.state", "_continuous_mode"),
    ("app.state", "_xiaozhi_pending"),
    ("app.state", "_xiaozhi_clients"),
    # Round 9: system_msg TTL 缓存需跨测试隔离，防止缓存污染
    ("agent.system_msg", "_SYSTEM_MSG_CACHE"),
    # Round 9: context 缓存同样需隔离
    ("agent.context", "_context_cache"),
    ("agent.context", "_context_cache_ts"),
    # Round 11: 历史摘要跨测试隔离
    ("agent.history", "_context_summaries"),
    # P0: 偏好/历史文件路径与锁（被 round2 _restore_user_state 局部快照，此处作为全局安全网）
    ("agent.preferences", "PREFS_FILE"),
    ("agent.preferences", "PREFS_LOCK_FILE"),
    ("agent.history", "HISTORY_FILE"),
    ("agent.history", "HISTORY_LOCK_FILE"),
    # P0: 历史文件签名/缓存（dict，deepcopy+clear/update 正确处理）
    ("agent.history", "_history_file_signatures"),
    ("agent.history", "_history_file_caches"),
    # P1: context 退避状态
    ("agent.context", "_context_fail_count"),
    ("agent.context", "_context_backoff_until"),
    # Round 9: AMAP 配额熔断时间戳（跨测试泄漏会让后续 AMAP 测试被短路）
    ("app.weather", "_amap_blocked_until"),
    # Round 13: 天气缓存（跨测试泄漏会让 mock 失效，污染矛盾标注断言）
    ("app.weather", "_weather_cache"),
    # Round 9 reviewer 建议：域关键词集合为 |= 增量增长，防跨测试累积
    ("agent.llm", "_ALL_DOMAIN_KEYWORDS"),
    # Round 21: WM 按会话隔离的数据（dict 隔离）；thread-local 不可深拷贝，单独清理
    ("agent.working_memory", "_session_data"),
]


@pytest.fixture(autouse=True)
def _clean_weather_state_file():
    """Ensure AMAP block state file does not leak between tests."""
    try:
        import app.weather as weather_mod
        state_file = getattr(weather_mod, "_WEATHER_STATE_FILE", None)
        if state_file and os.path.exists(state_file):
            os.unlink(state_file)
    except Exception:
        pass
    yield
    try:
        import app.weather as weather_mod
        state_file = getattr(weather_mod, "_WEATHER_STATE_FILE", None)
        if state_file and os.path.exists(state_file):
            os.unlink(state_file)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _snapshot_global_state():
    """每个测试前后快照/恢复关键模块级状态（只恢复快照里存在的属性）"""
    snapshot: dict[tuple[str, str], object] = {}
    for mod_path, attr_name in _STATE_SNAPSHOT_KEYS:
        try:
            import importlib
            mod = importlib.import_module(mod_path)
            if hasattr(mod, attr_name):
                val = getattr(mod, attr_name)
                # 对 dict/list 做深拷贝，避免引用共享（测试内 append/update 不污染快照）
                if isinstance(val, dict):
                    snapshot[(mod_path, attr_name)] = copy.deepcopy(val)
                elif isinstance(val, list):
                    snapshot[(mod_path, attr_name)] = copy.deepcopy(val)
                else:
                    snapshot[(mod_path, attr_name)] = val
        except Exception:
            # 某些模块名可能在不同上下文中不可导入，静默跳过
            pass

    yield  # 测试运行

    # P1: 清理 context 刷新线程（不可深拷贝），避免跨测试泄漏
    try:
        import agent.context as _ctx_mod
        if getattr(_ctx_mod, "_context_refresh_thread", None) is not None:
            _ctx_mod._context_refresh_thread.join(timeout=2.0)
            _ctx_mod._context_refresh_thread = None
        _ctx_mod._context_fail_count = 0
        _ctx_mod._context_backoff_until = 0.0
    except Exception:
        pass

    # Round 21: 清理 WM thread-local session（threading.local 不可深拷贝）
    try:
        import agent.working_memory as _wm_mod
        if hasattr(_wm_mod, "_session_local"):
            _wm_mod._session_local = threading.local()
    except Exception:
        pass

    # 恢复：只恢复快照里存在的属性，键缺失时不新增
    for (mod_path, attr_name), orig_val in snapshot.items():
        try:
            import importlib
            mod = importlib.import_module(mod_path)
            if hasattr(mod, attr_name):
                cur_val = getattr(mod, attr_name)
                if isinstance(orig_val, dict) and isinstance(cur_val, dict):
                    cur_val.clear()
                    cur_val.update(orig_val)
                else:
                    setattr(mod, attr_name, orig_val)
        except Exception:
            pass
