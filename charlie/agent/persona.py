"""动态人设状态机 — 根据用户情绪和交互模式调整 Charlie 的回复风格"""
import os
import json
import time
import copy
import logging

log = logging.getLogger("magic")

DATA_DIR = os.environ.get(
    "ASSISTANT_KID_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
PERSONA_FILE = os.path.join(DATA_DIR, "persona_state.json")

# 默认人设状态（合并旧 schema + 新 schema，保持向后兼容）
_DEFAULT = {
    # 旧 schema（情绪机）
    "mood": "neutral",
    "formality": "casual",
    "humor_level": 0.3,
    "interaction_count": 0,
    "recent_emotions": [],
    # 新 schema（人格稳定性）
    "tone_profile": "casual",
    "relationship_level": "acquaintance",
    "user_formality_preference": "casual",
    "user_humor_preference": 0.3,
}

_state = None


def _load():
    global _state
    if _state is not None:
        return _state
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            _state = json.load(f)
    except Exception:
        _state = copy.deepcopy(_DEFAULT)
    # 迁移：旧文件缺少新字段时补默认值
    for k, v in _DEFAULT.items():
        _state.setdefault(k, v)
    return _state


def _save():
    try:
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.debug(f"[persona] 保存失败: {e}")


# 情绪检测关键词
_EMOTION_KEYWORDS = {
    "happy": ["开心", "高兴", "太好了", "不错", "哈哈", "😄", "👍", "好棒"],
    "excited": ["太棒了", "厉害", "牛", "哇", "！！！"],
    "frustrated": ["烦", "累", "无语", "唉", "算了", "不想", "麻烦"],
    "angry": ["气死", "什么鬼", "怎么回事", "？？？", "你有病"],
    "sad": ["难过", "伤心", "失望", "不想说话"],
    "tired": ["困", "累", "想睡", "不想动", "好累"],
}


def detect_emotion(text: str) -> str:
    """从用户文本检测情绪（启发式）"""
    text_lower = text.lower()
    scores: dict = {}
    for emotion, keywords in _EMOTION_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[emotion] = score
    # 标点情绪
    if "！！！" in text or text.endswith("!!!"):
        scores["excited"] = scores.get("excited", 0) + 2
    if "？？？" in text or text.count("?") >= 3:
        scores["angry"] = scores.get("angry", 0) + 1
    if not scores:
        return "neutral"
    return max(scores, key=scores.get)


def update(text: str):
    """根据用户输入更新人设状态"""
    state = _load()
    emotion = detect_emotion(text)
    state["interaction_count"] += 1
    recent = state.get("recent_emotions", [])
    recent.append({"emotion": emotion, "ts": time.time()})
    state["recent_emotions"] = recent[-10:]

    # 根据近期情绪调整 mood（优先级: positive > negative，允许情绪切换）
    recent_emotions = [r["emotion"] for r in state["recent_emotions"][-5:]]
    if recent_emotions.count("happy") >= 2 or recent_emotions.count("excited") >= 1:
        state["mood"] = "playful"
        state["humor_level"] = 0.5
    elif recent_emotions.count("tired") >= 2:
        state["mood"] = "caring"
        state["humor_level"] = 0.2
    elif recent_emotions.count("frustrated") >= 2 or recent_emotions.count("angry") >= 1:
        state["mood"] = "serious"
        state["humor_level"] = 0.1
    else:
        state["mood"] = "neutral"
        state["humor_level"] = 0.3

    _save()


def get_persona_prompt() -> str:
    """返回动态人设提示片段，注入到 system message"""
    state = _load()
    mood = state.get("mood", "neutral")

    prompts = {
        "serious": "用户现在可能有点急或烦躁，回复要简短直接，别开玩笑，先解决问题。",
        "playful": "用户心情不错，可以适当幽默，回复可以轻松一点。",
        "caring": "用户看起来有点累，语气温和一些，主动关心一下。",
        "neutral": "",
    }
    base = prompts.get(mood, "")
    if not base:
        return ""
    return f"\n\n⚠️ 动态人设调整：{base}"


# ===== P3: 人格稳定性增强 =====

def get_tone_profile() -> dict:
    """获取当前语气配置"""
    state = _load()
    return {
        "tone": state.get("tone_profile", "casual"),
        "formality": state.get("formality", "casual"),
        "humor_level": state.get("humor_level", 0.3),
    }


def get_relationship_level() -> str:
    """获取当前关系等级"""
    state = _load()
    return state.get("relationship_level", "acquaintance")


def set_relationship_level(level: str) -> None:
    """设置关系等级（stranger/acquaintance/friend/family）"""
    if level not in ("stranger", "acquaintance", "friend", "family"):
        level = "acquaintance"
    state = _load()
    state["relationship_level"] = level
    _save()


def update_relationship_from_interaction(text: str) -> None:
    """根据用户输入更新关系亲密度"""
    state = _load()
    state["interaction_count"] = state.get("interaction_count", 0) + 1
    count = state["interaction_count"]

    # 亲密信号
    intimate_signals = ["兄弟", "老铁", "buddy", "亲爱的", "宝贝", "家人", "咱"]
    if any(s in text for s in intimate_signals) and count > 10:
        state["relationship_level"] = "family"
    elif count > 100:
        state["relationship_level"] = "friend"
    elif count > 20:
        state["relationship_level"] = "acquaintance"
    else:
        state["relationship_level"] = "stranger"

    # 语气偏好
    formal_signals = ["您好", "请", "谢谢", "麻烦您", "请问"]
    if any(s in text for s in formal_signals):
        state["tone_profile"] = "formal"
    elif "哈哈哈" in text or text.count("哈") >= 2:
        state["tone_profile"] = "humorous"
    else:
        state["tone_profile"] = "casual"

    _save()


def contextual_response_style(level: str = "") -> str:
    """根据关系等级返回回复风格提示片段

    关系等级影响：
    - stranger:  正式、礼貌、保持距离
    - acquaintance: 友好、自然
    - friend:   轻松、幽默、可以开玩笑
    - family:   随意、亲密、像家人一样
    """
    state = _load()
    if not level:
        level = state.get("relationship_level", "acquaintance")
    tone = state.get("tone_profile", "casual")

    level_prompts = {
        "stranger": "回复保持礼貌和距离感，用'您'称呼，不要用表情或网络用语。",
        "acquaintance": "回复友好自然，像普通朋友一样。",
        "friend": "回复可以轻松一点，适当使用幽默，像好朋友聊天。",
        "family": "回复非常随意，像家人一样，可以开玩笑、用昵称。",
    }

    tone_prompts = {
        "formal": "用正式、礼貌的语气。",
        "casual": "用轻松、自然的语气。",
        "humorous": "可以适当幽默，用一些俏皮话。",
    }

    parts = []
    if level != "acquaintance":
        parts.append(level_prompts.get(level, ""))
    if tone != "casual":
        parts.append(tone_prompts.get(tone, ""))

    if not parts:
        return ""
    return "\n\n🎭 人设风格：" + "；".join(parts)

