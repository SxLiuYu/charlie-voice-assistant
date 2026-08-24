"""意图识别: 低意图/乱码判断/唤醒词剥离"""
import re

LOW_INTENT_ASR_REPLY = "嗯嗯，我在。"

# 唤醒词剥离: ASR 结果通常包含设备端唤醒词本身("你好小智，今天天气怎么样"),
# 这些词不该进 brain/意图分类。用正则剥离前置唤醒词 + 跟随标点。
#
# 静态 fallback 正则（roles 导入失败时仍可用），包含 jarvis 唤醒词。
_WAKE_C = r"(?:小智|小志|小助|小助手|查里|查理|查莉|查利|cha[rc]?li?e?|charl(?:ie|ey|es|li)?|jarvis|贾维斯|贾维丝)"
_WAKE_STRIP_RE = re.compile(
    r"^\s*(?:(?:嗨|哈喽|哈啰|喂|你好|您好|ok|hi|hello)\s*[,，]?\s*)?"
    + _WAKE_C
    + r"\s*[，,。.!！？~；;、\s]*",
    re.IGNORECASE,
)


def _build_wake_strip_re() -> re.Pattern:
    """动态构建唤醒词剥离正则：聚合所有角色的唤醒词（含角色名本身）。

    结果会缓存在模块级变量 `_WAKE_STRIP_RE_DYNAMIC`，避免每次调用重读
    preferences。如果 roles 模块不可用，返回静态 fallback 正则。
    """
    global _WAKE_STRIP_RE_DYNAMIC
    try:
        from agent.roles import get_all_roles
        roles = get_all_roles()
        # 收集所有角色的唤醒词 + 角色 ID（用于 "切换到 xxx" 等前缀）
        all_wake_words: list[str] = []
        for role_id, role_conf in roles.items():
            words = role_conf.get("wake_words", [])
            all_wake_words.extend(words)
            # 角色 ID 本身也可能作为唤醒词出现（如 "jarvis"、"charlie"）
            if role_id not in all_wake_words:
                all_wake_words.append(role_id)
        if not all_wake_words:
            return _WAKE_STRIP_RE
        # 转义正则元字符，拼接为 (?:w1|w2|w3) 分组
        escaped = [re.escape(w) for w in all_wake_words if w]
        if not escaped:
            return _WAKE_STRIP_RE
        wake_c = r"(?:" + "|".join(escaped) + r")"
        dynamic_re = re.compile(
            r"^\s*(?:(?:嗨|哈喽|哈啰|喂|你好|您好|ok|hi|hello)\s*[,，]?\s*)?"
            + wake_c
            + r"\s*[，,。.!！？~；;、\s]*",
            re.IGNORECASE,
        )
        _WAKE_STRIP_RE_DYNAMIC = dynamic_re
        return dynamic_re
    except Exception:
        return _WAKE_STRIP_RE


_WAKE_STRIP_RE_DYNAMIC: re.Pattern | None = None


def _get_wake_strip_re() -> re.Pattern:
    """返回当前生效的唤醒词剥离正则（优先动态，首次调用时构建并缓存）。"""
    global _WAKE_STRIP_RE_DYNAMIC
    if _WAKE_STRIP_RE_DYNAMIC is None:
        _WAKE_STRIP_RE_DYNAMIC = _build_wake_strip_re()
    return _WAKE_STRIP_RE_DYNAMIC


def strip_wake_word(text: str) -> str:
    """去掉 ASR 结果开头携带的唤醒词/称谓 (仅当出现匹配且后跟标点/空格)。"""
    if not text:
        return ""
    return _get_wake_strip_re().sub("", text, count=1).strip()

_LOW_INTENT_STRIP_RE = re.compile(
    r"[\s，。！？、,.!?~～…\-—_:：；;\"'“”‘’（）()【】\[\]{}<>《》〈〉]+"
)
_LOW_INTENT_FILLER_CHARS = set("嗯哦啊呃哈噢喔诶呀吧呢嘛哎")
_LOW_INTENT_ENGLISH_RE = re.compile(
    r"^(?:h+m+|u(?:h+|m+|hm+)|a+h+|o+h+|e+r+m?|h+u+h+|h+e+v?y*o*|y+e+a+hn?|w+h+a+t+)$"
)

_GARBLED_FILLER_CHARS = set('的了呢吗啊哦呃呀吧哼哈嘿哟哎嗯呜喵喂嗨')
_VALID_ROOT_CHARS = set('在打开关几现点今明后天气温度电视空调提醒日程搜索文件读写取消停止继续对好好的播放音乐随机首歌听唱暂停换下一首上一首你怎么这那说是不笨傻叫叫做要不用没有没有我他她它们什么为什么哪谁哪里笨呐')
_VALID_SHORT_TEXTS = {
    '你好', '谢谢', '好的', '谢谢', '再见', '可以', '不行', '没有',
    '几点了', '几点啦', '在吗', '在听吗', '打开空调', '关闭空调',
    '打开电视', '关闭电视', '帮我打开空调', '帮我关闭空调', '开空调', '关空调',
    '现在几点了', '现在几点', '在听吗', '打开', '关闭',
    '调温度', '调低', '调高', '搜索', '取消', '停止', '继续',
    '在不在', '在不在呀', '在不在啊', '你以在吗', '你在吗', '在不在呢',
    '怎么了', '干嘛', '干啥', '咋了', '啥事', '什么事',
    '播放音乐', '播放一首', '随机播放', '随便播放', '来一首',
    '播放音乐随机播放', '播放一首歌', '播放什么音乐', '随便播放一首',
    '放歌', '放音乐', '听歌', '点歌', '来首歌', '播一首',
    '导航到', '导航去', '怎么走', '怎么去', '导航',
    '哎', '嗯', '哦', '好', '对', '是', '行',
    '干吗', '看你', '什么', '拜',
}

def is_low_intent_asr(text: str) -> bool:
    normalized = _LOW_INTENT_STRIP_RE.sub("", text or "").lower()
    if not normalized:
        return True
    if len(normalized) > 16:
        return False
    if _LOW_INTENT_ENGLISH_RE.fullmatch(normalized):
        return True
    return all(char in _LOW_INTENT_FILLER_CHARS for char in normalized)

def is_garbled_asr(text: str) -> bool:
    if not text:
        return False
    stripped = text.strip(" ，。！？、,.!?~～…—_:：；;\"'""''（）()【】[]{}<>《》〈〉\n\r\t")
    if not stripped:
        return True
    length = len(stripped)
    if stripped in _VALID_SHORT_TEXTS or stripped.rstrip('。？!') in _VALID_SHORT_TEXTS:
        return False
    if length <= 8 and all(c in _GARBLED_FILLER_CHARS for c in stripped):
        return True
    if length <= 2 and not any(c in _VALID_ROOT_CHARS for c in stripped):
        return True
    if length >= 10 and not any(c in _VALID_ROOT_CHARS for c in stripped):
        return True
    if 5 <= length <= 12:
        filler_count = sum(1 for c in stripped if c in _GARBLED_FILLER_CHARS)
        if filler_count >= length * 0.6:
            return True
    return False