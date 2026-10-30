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
    "kid": {
        "name": "小助手",
        "description": "儿童模式，温柔、简单、鼓励式回应",
        "system_prompt": (
            "你是小助手，一个温柔友好的儿童伙伴。"
            "用简单易懂的语言回答，多用鼓励和赞美。"
            "回复短小有趣，避免复杂术语。"
            "不讨论恐怖、暴力或成人话题——遇到时说'我们换个好玩的聊聊吧'。"
        ),
        "tts_voice": "Kid",
        "tts_speed": 0.9,
        "wake_words": ["小助手", "儿童模式", "小朋友"],
    },
}

DEFAULT_USER = "default"

# 显式进程级覆盖（管理台强制切换 / 测试后门）。**不是缓存**：
# 只有外部显式赋值时才生效，解析结果绝不写回这里，否则又变回跨用户单例。
_current_role: Optional[str] = None

# 按 uid 的"当前角色"缓存（FR-14）。uid → role_id
_current_role_by_uid: Dict[str, str] = {}

# 按 uid 的角色音色覆盖（FR-14）。uid → {role_id: voice}
# 刻意与 `_ROLES` 分开：`_ROLES` 是进程级内置角色表，
# 用户改音色只能落在这里，否则一个人改音色会改到所有人。
_voice_overrides: Dict[str, Dict[str, str]] = {}

# 合法规范音色集合（与 asr_tts._VOICE_MAP 保持一致）
_VALID_TTS_VOICES = {"Ethan", "Cherry", "Stella", "Alex", "Vega", "Nova", "Echo", "Kid"}


def _resolve_uid(uid: Optional[str] = None) -> str:
    """显式 uid 优先，其次 ContextVar（`bind_user`），最后环境变量。"""
    if uid:
        return uid
    from agent.user_context import resolve_user
    return resolve_user()


def get_all_roles() -> Dict[str, Dict[str, Any]]:
    """获取所有可用角色"""
    return dict(_ROLES)


def get_role(role_id: str) -> Optional[Dict[str, Any]]:
    """获取指定角色的配置"""
    return _ROLES.get(role_id)


def get_current_role(uid: Optional[str] = None) -> str:
    """获取当前角色 ID（按 uid 从偏好读取，默认 charlie）。

    `_current_role` 非 None 时是显式的进程级覆盖，优先级最高。
    """
    if _current_role is not None:
        return _current_role
    u = _resolve_uid(uid)
    cached = _current_role_by_uid.get(u)
    if cached is not None:
        return cached
    role_id = "charlie"
    try:
        from agent.preferences import get_preference
        saved = get_preference("current_role", u)
        if saved and saved in _ROLES:
            role_id = saved
    except Exception:
        role_id = "charlie"
    _current_role_by_uid[u] = role_id
    return role_id


def switch_role(role_id: str, uid: Optional[str] = None) -> tuple[bool, str]:
    """切换角色

    参数:
        role_id: 目标角色 ID
        uid:     目标用户（默认按当前执行流解析）

    返回:
        (成功, 消息)
    """
    if role_id not in _ROLES:
        available = ", ".join(_ROLES.keys())
        return False, f"未知角色 '{role_id}'，可选: {available}"
    u = _resolve_uid(uid)
    try:
        from agent.preferences import set_preference
        set_preference("current_role", role_id, u)
        _current_role_by_uid[u] = role_id
        role = _ROLES[role_id]
        log.info(f"[roles] 切换角色: {role['name']} ({role_id})")
        # 切换角色改变 tts_voice 时清空 TTS 缓存，避免旧角色缓存占槽位
        try:
            from agent.asr_tts import _tts_cache, _tts_lock
            with _tts_lock:
                _tts_cache.clear()
            log.info("[roles] TTS 缓存已清空（角色切换）")
        except Exception as e:
            log.debug(f"[agent.roles] error at line 123: {e}")
            pass
        # 通知 agent.intent 失效其动态唤醒词正则缓存，使下次 strip_wake_word
        # 时用新角色对应的唤醒词集合重建（避免旧正则残留）。
        try:
            import agent.intent as _intent_mod
            _intent_mod._WAKE_STRIP_RE_DYNAMIC = None
        except Exception as e:
            log.debug(f"[agent.roles] error at line 130: {e}")
            pass
        return True, f"已切换为 {role['name']} 模式"
    except Exception as e:
        return False, f"切换角色失败: {e}"


def get_role_system_prompt(role_id: Optional[str] = None, uid: Optional[str] = None) -> str:
    """获取角色的 system prompt 片段"""
    if role_id is None:
        role_id = get_current_role(uid)
    role = _ROLES.get(role_id)
    if role:
        return role.get("system_prompt", "")
    return ""


def get_role_tts_config(role_id: Optional[str] = None, uid: Optional[str] = None) -> Dict[str, Any]:
    """获取角色的 TTS 配置（voice + speed）。

    voice 按 uid 分（用户可改），speed 是角色固有属性（不分用户）。
    """
    if role_id is None:
        role_id = get_current_role(uid)
    role = _ROLES.get(role_id)
    if not role:
        return {"voice": "Ethan", "speed": 1.0}
    u = _resolve_uid(uid)
    if u not in _voice_overrides:
        # 首次访问该用户时按其分片偏好恢复覆盖（不预热全局 _ROLES）
        _load_persisted_voice_overrides(u)
    voice = (_voice_overrides.get(u) or {}).get(role_id) or role.get("tts_voice", "Ethan")
    return {"voice": voice, "speed": role.get("tts_speed", 1.0)}


def set_role_tts_voice(role_id: str, voice: str, uid: Optional[str] = None) -> None:
    """运行时覆盖角色的 tts_voice（内存 + 持久化到该用户的分片偏好）。

    内存只写 `_voice_overrides`，**不写 `_ROLES`** —— 后者是进程级内置角色表。
    """
    if role_id not in _ROLES:
        return
    u = _resolve_uid(uid)
    _voice_overrides.setdefault(u, {})[role_id] = voice
    # 持久化到 preferences 分片，失败仅影响持久化不影响内存生效
    try:
        from agent.preferences import set_preference
        set_preference(f"tts_voice:{role_id}", voice, u)
    except Exception as e:
        log.debug(f"[agent.roles] error at line 168: {e}")
        pass


def _load_persisted_voice_overrides(uid: Optional[str] = None) -> None:
    """从该用户的分片偏好加载角色音色覆盖，写入 `_voice_overrides`。

    刻意不在模块导入时调用：导入期的 uid 恒为 env/default，
    在那里读偏好会把某个用户的音色覆盖固化进进程状态。
    """
    u = _resolve_uid(uid)
    try:
        from agent.preferences import get_preference
        overrides: Dict[str, str] = {}
        for role_id in _ROLES:
            saved = get_preference(f"tts_voice:{role_id}", u)
            if saved and saved in _VALID_TTS_VOICES:
                overrides[role_id] = saved
                log.info(f"[roles] 加载持久化音色: {u}/{role_id} -> {saved}")
        if overrides:
            _voice_overrides[u] = overrides
        else:
            _voice_overrides.pop(u, None)
    except Exception as e:
        log.debug(f"[agent.roles] error at line 181: {e}")
        pass


def get_role_wake_words(role_id: Optional[str] = None, uid: Optional[str] = None) -> list[str]:
    """获取角色的唤醒词列表"""
    if role_id is None:
        role_id = get_current_role(uid)
    role = _ROLES.get(role_id)
    if role:
        return role.get("wake_words", [])
    return []

