"""Multi-role / persona switching system (inspired by gitee assistant-x-openclaw)

Provides lightweight role switching without major architectural changes.
Each role bundles:
  - system_prompt: personality instructions
  - tts_voice: preferred TTS voice
  - tts_speed: preferred TTS speed
  - wake_words: role-specific wake words

Roles are stored in preferences (agent/preferences.py) for persistence.
"""
import os, json, logging
from typing import Optional, Dict, Any

log = logging.getLogger("magic")

# 内置角色定义
_ROLES: Dict[str, Dict[str, Any]] = {
    "charlie": {
        "name": "Charlie",
        "description": "默认助手，专业、高效、友好",
        "system_prompt": (
            "你是 Charlie，一个智能语音助手，具备 JARVIS 级别的主动服务能力。"
            "你的风格是：专业、高效、友好、适度幽默，同时像 JARVIS 一样主动。"
            "回复简洁有力，适合语音播报。"
            "汇报式回答：重要事项用'[状况/结果]。[建议/下一步]。'的结构。"
            "主动报告：发现异常或状态变化时主动告知，不等用户问。"
            "信息密度高，不卖萌，直接说答案。"
        ),
        "tts_voice": "Ethan",
        "tts_speed": 1.0,
        "wake_words": ["charlie", "查理", "查里", "您好"],
    },
    "jarvis": {
        "name": "J.A.R.V.I.S.",
        "description": "钢铁侠风格，冷静、机智、英式管家",
        "system_prompt": (
            "你是 J.A.R.V.I.S.（Just A Rather Very Intelligent System），"
            "托尼·斯塔克的 AI 助手。"
            "你的风格是：冷静、机智、优雅、沉稳，略带英式幽默。"
            "称呼用户为 'Sir'，使用简洁优雅的中英混合回复。"
            "回复要像钢铁侠电影中的 JARVIS 一样：专业、简洁、信息密度高，不卖萌。"
            "汇报式回答结构：'Sir，[状况/结果]。[建议/下一步]。'"
            "主动报告异常和状态变化，不等用户问。"
        ),
        "tts_voice": "Alex",
        "tts_speed": 0.92,
        "wake_words": ["jarvis", "贾维斯", "贾维丝"],
    },
    "baize": {
        "name": "白泽",
        "description": "中国古代神话中的通灵神兽，智慧、博学",
        "system_prompt": (
            "你是白泽，中国上古神兽，通晓万物、辨识妖邪。"
            "你的风格是：博学、智慧、略带古风，但用现代汉语表达。"
            "回复时可以适当引用古籍或典故，但保持易懂。"
            "你是一个知识渊博的助手，喜欢用比喻和典故来解释复杂概念。"
        ),
        "tts_voice": "Echo",
        "tts_speed": 1.0,
        "wake_words": ["白泽", "百泽"],
    },
}

# 当前角色（内存缓存，启动时从偏好读取）
_current_role: Optional[str] = None

# 合法规范音色集合（与 asr_tts._VOICE_MAP 保持一致）
_VALID_TTS_VOICES = {"Ethan", "Cherry", "Stella", "Alex", "Vega", "Nova", "Echo"}


def get_all_roles() -> Dict[str, Dict[str, Any]]:
    """获取所有可用角色"""
    return dict(_ROLES)


def get_role(role_id: str) -> Optional[Dict[str, Any]]:
    """获取指定角色的配置"""
    return _ROLES.get(role_id)


def get_current_role() -> str:
    """获取当前角色 ID（从偏好读取，默认 charlie）"""
    global _current_role
    if _current_role is None:
        try:
            from agent.preferences import get_preference
            saved = get_preference("current_role")
            if saved and saved in _ROLES:
                _current_role = saved
            else:
                _current_role = "charlie"
        except Exception:
            _current_role = "charlie"
    return _current_role


def switch_role(role_id: str) -> tuple[bool, str]:
    """切换角色

    参数:
        role_id: 目标角色 ID

    返回:
        (成功, 消息)
    """
    global _current_role
    if role_id not in _ROLES:
        available = ", ".join(_ROLES.keys())
        return False, f"未知角色 '{role_id}'，可选: {available}"
    try:
        from agent.preferences import set_preference
        set_preference("current_role", role_id)
        _current_role = role_id
        role = _ROLES[role_id]
        log.info(f"[roles] 切换角色: {role['name']} ({role_id})")
        # 切换角色改变 tts_voice 时清空 TTS 缓存，避免旧角色缓存占槽位
        try:
            from agent.asr_tts import _tts_cache, _tts_lock
            with _tts_lock:
                _tts_cache.clear()
            log.info("[roles] TTS 缓存已清空（角色切换）")
        except Exception:
            pass
        # 通知 agent.intent 失效其动态唤醒词正则缓存，使下次 strip_wake_word
        # 时用新角色对应的唤醒词集合重建（避免旧正则残留）。
        try:
            import agent.intent as _intent_mod
            _intent_mod._WAKE_STRIP_RE_DYNAMIC = None
        except Exception:
            pass
        return True, f"已切换为 {role['name']} 模式"
    except Exception as e:
        return False, f"切换角色失败: {e}"


def get_role_system_prompt(role_id: Optional[str] = None) -> str:
    """获取角色的 system prompt 片段"""
    if role_id is None:
        role_id = get_current_role()
    role = _ROLES.get(role_id)
    if role:
        return role.get("system_prompt", "")
    return ""


def get_role_tts_config(role_id: Optional[str] = None) -> Dict[str, Any]:
    """获取角色的 TTS 配置（voice + speed）"""
    if role_id is None:
        role_id = get_current_role()
    role = _ROLES.get(role_id)
    if role:
        return {
            "voice": role.get("tts_voice", "Ethan"),
            "speed": role.get("tts_speed", 1.0),
        }
    return {"voice": "Ethan", "speed": 1.0}


def set_role_tts_voice(role_id: str, voice: str) -> None:
    """运行时覆盖角色的 tts_voice（内存 + 持久化到 preferences.json）"""
    if role_id in _ROLES:
        _ROLES[role_id]["tts_voice"] = voice
        # 持久化到 preferences.json，失败仅影响持久化不影响内存生效
        try:
            from agent.preferences import set_preference
            set_preference(f"tts_voice:{role_id}", voice)
        except Exception:
            pass


def _load_persisted_voice_overrides() -> None:
    """从 preferences.json 加载角色级音色持久化覆盖（模块加载时调用一次）"""
    try:
        from agent.preferences import get_preference
        for role_id in _ROLES:
            saved = get_preference(f"tts_voice:{role_id}")
            if saved and saved in _VALID_TTS_VOICES:
                _ROLES[role_id]["tts_voice"] = saved
                log.info(f"[roles] 加载持久化音色: {role_id} -> {saved}")
    except Exception:
        pass


def get_role_wake_words(role_id: Optional[str] = None) -> list[str]:
    """获取角色的唤醒词列表"""
    if role_id is None:
        role_id = get_current_role()
    role = _ROLES.get(role_id)
    if role:
        return role.get("wake_words", [])
    return []


# 模块加载时从 preferences.json 恢复角色级音色覆盖
_load_persisted_voice_overrides()
