"""多轮指代消解 — 把当前轮里的代词/省略/指示词还原成字面实体。

## 为什么需要它

Charlie 的对话历史长达 30 轮，但：
  1. **Laya（System-1 决策模型）是**无状态**的**——它只吃一个字符串、不能
     回填历史；遇到"把它关了""再看一眼院子""那首歌再放一遍"这类省略/代词
     就直接判 out-of-scope 或抽错槽。
  2. 即便走 LLM，把指代**预先消解为字面 query** 也比靠 LLM 从历史里自己猜
     更稳、更快、更省 token——尤其 ASR 场景下"它/这个/那个/上次"是高频噪声。

## 做法：把上下文折叠进当前句（fold-context）

不做完整的共指图（那是 LLM 的活），只做**高精度的就近消解**：
  1. 取最近若干轮（user/assistant 交替）当候选 antecedent 池；
  2. 对当前句里的代词/零指代/指示词，按"最近优先、类型匹配"规则替换成字面实体；
  3. 返回 `(消解后的句子, 是否改动, 还原映射)`。

设计约束（与画像模块一致）：
  - **纯函数、无副作用、不落库、不联网**；
  - 任何异常都降级为"原句返回、不改动"——绝不能因为消解崩了让用户没话；
  - 默认由调用方门控（见 voice_agent），未启用即等同透传。

## 安全边界
  - 只替换**低风险**的指代（物体/设备/地点/歌曲/提醒等）；绝不编造实体——
    候选池里找不到合适 antecedent 就保留原词，让 LLM 兜底。
  - 姓名/身份类实体不在此处替换，避免误伤隐私相关指代。
"""
import re
import logging

log = logging.getLogger("magic")

# ============================== 实体抽取 ==============================
# 从历史里捞"可指代"的实体：设备、地点、歌曲/艺人、提醒、天气城市、App。
# 用轻量正则 + 词表，刻意保守——宁可漏捞也不误替。
_DEVICE_KW = ("灯", "空调", "窗帘", "电视", "风扇", "加湿器", "净化器",
              "音箱", "热水器", "插座", "摄像头", "门", "锁", "扫地机器人")
_SONG_KW = ("歌", "音乐", "单曲", "专辑")
_PLACE_KW = ("家", "公司", "办公室", "卧室", "客厅", "厨房", "院子", "阳台", "车库")
_REMIND_KW = ("提醒", "闹钟", "备忘", "日程")
_WEATHER_CITY_RE = re.compile(r"([一-龥]{2,8}?)(?:的|今天|明天|后天|现在)?天气")
_SONG_RE = re.compile(r"(?:放|听|播|唱)(?:一(?:首|遍|下)|首)?\s*([^\s，。！？,!?的]{1,12}?)(?:的)?(?:歌|音乐|单曲|专辑)")
# assistant 确认播放的歌名（"正在播放晴天""播放 稻香"）→ 作为最权威的 antecedent
_PLAYED_RE = re.compile(r"(?:正在)?播放\s*[:：]?\s*([一-龥A-Za-z0-9]{1,12})")
_REMIND_RE = re.compile(r"(?:提醒|备忘|闹钟)[^，。！？,!?]*?(?:关于|内容是|事项是|做)?\s*([^\s，。！？,!?]{1,20})")
_PLACE_RE = re.compile(r"(?:在|去|回|到)\s*([一-龥]{1,8}?)(?:的|里|那|这边|那边)?\s*(?:看一下|看看|监控|摄像头|转|开|关|灯|空调)?")


def _extract_entities(utterances: list[str]) -> dict:
    """从一组历史句子里抽取候选 antecedent，按类别收集（保持出现顺序）。"""
    ents: dict[str, list] = {"device": [], "song": [], "place": [], "reminder": [], "weather_city": []}
    for u in utterances:
        if not u:
            continue
        for kw in _DEVICE_KW:
            if kw in u:
                ent = u if len(u) <= 12 else None
                if ent and ent not in ents["device"]:
                    ents["device"].append(ent)
        m = _SONG_RE.search(u)
        if m and m.group(1) and m.group(1) not in ents["song"]:
            ents["song"].append(m.group(1))
        # 助手确认播放的歌名优先级更高，后追加（最近优先命中它）
        mp = _PLAYED_RE.search(u)
        if mp and mp.group(1) and mp.group(1) not in ents["song"]:
            ents["song"].append(mp.group(1))
        m = _REMIND_RE.search(u)
        if m and m.group(1) and m.group(1) not in ents["reminder"]:
            ents["reminder"].append(m.group(1))
        m = _PLACE_RE.search(u)
        if m and m.group(1) and m.group(1) not in ents["place"]:
            ents["place"].append(m.group(1))
        m = _WEATHER_CITY_RE.search(u)
        if m and m.group(1) and m.group(1) not in ents["weather_city"]:
            ents["weather_city"].append(m.group(1))
    return ents


# ============================== 指代模式 ==============================
# 代词/零指代 → 类目。匹配顺序即优先级（更具体的先匹配）。
_PRONOUN_RULES = [
    # (正则, 目标类目, 用该类目第几个候选：默认最近=最后一个)
    (re.compile(r"那首歌|这首歌|那首|那支|刚才那首|上一首"), "song"),
    (re.compile(r"那个提醒|这个提醒|那条提醒|刚说的提醒|刚才的提醒"), "reminder"),
    (re.compile(r"那个地方|这地方|那个位置|这儿|那儿|那个房间"), "place"),
    (re.compile(r"那个城市|那座城市|那边的天气"), "weather_city"),
    (re.compile(r"它|它们|这个|那个|这玩意|那玩意|这东西|那东西|这设备|那设备"), "device"),
    (re.compile(r"再(?:一|来一|放一)?遍|再(?:放|听|播)一下"), "song"),
    (re.compile(r"再(?:看|查|打开|关|开|调)一?下|再(?:关|开|调)一次"), "device"),
]


def resolve_references(current: str, history: list[str], max_history: int = 6) -> tuple[str, bool, dict]:
    """消解当前句里的多轮指代，返回 (消解后句子, 是否改动, 还原映射)。

    current: 当前轮用户原句；history: 最近的历史句（user/assistant 混合，
    按时间从旧到新）。近因优先——同类实体取最近一条。

    任何异常 → 返回 (current, False, {})，绝不抛错。
    """
    if not current:
        return current, False, {}
    try:
        recent = [h for h in history[-max_history:] if h] if history else []
        if not recent:
            return current, False, {}
        ents = _extract_entities(recent)
        if not any(ents.values()):
            return current, False, {}

        result = current
        mapping: dict = {}
        for pat, cat in _PRONOUN_RULES:
            if pat.search(result):
                candidates = ents.get(cat) or []
                if not candidates:
                    continue
                antecedent = candidates[-1]  # 最近优先
                # 把代词整体替换为字面实体（保留修饰动词）
                repl = result
                for m in pat.finditer(repl):
                    repl = repl[:m.start()] + antecedent + repl[m.end():]
                if repl != result:
                    mapping[pat.pattern] = antecedent
                    result = repl
        changed = result != current
        return result, changed, mapping
    except Exception as e:
        log.debug(f"[coref] 消解跳过，原句透传: {e}")
        return current, False, {}


# ============================== 便捷封装 ==============================
def resolve_for_session(current: str, session_id: str = "default") -> tuple[str, bool, dict]:
    """用会话历史直接消解出当前句。从 agent.history 拉最近会话文本。

    拉不到历史 / 任何异常 → 原句透传（零行为变更）。
    """
    try:
        from agent.history import _history_snapshot
        snap = _history_snapshot(session_id)
        hist = [m.get("content", "") for m in snap if isinstance(m, dict) and m.get("role") == "assistant"]
        # 用 assistant 回复当 antecedent 源（"放首周杰伦的歌"→assistant 确认了歌名）
        if not hist:
            hist = [m.get("content", "") for m in snap if isinstance(m, dict) and m.get("role") == "user"]
        return resolve_references(current, hist)
    except Exception as e:
        log.debug(f"[coref] 会话消解跳过: {e}")
        return current, False, {}
