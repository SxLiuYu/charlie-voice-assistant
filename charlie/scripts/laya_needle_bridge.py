#!/usr/bin/env python3
"""Laya + Needle3 混合端侧工具调用桥（中文可用）。

## 为什么要这个桥

Needle3（Cactus Compute 29M 端侧工具调用模型）英文实测 99ms / conf 1.0，
但**纯英文**——中文零调用（`scripts/needle3_probe.py` 已证实）。
Laya（`convaiinnovations/laya` multilingual，Apache-2.0，多语言 System-1
决策模型）中文原生，但只能做"固定候选→校准概率"，**不生成、不抽自由文本实体**。

⇒ 混合分工，各用所长：

    ┌──────────────┐   路由到工具 + 判断是否需澄清
    │  中文 utterance │──► Laya (choice + noul) ──┐
    └──────────────┘                              │
                                                  ▼
                                     需要澄清？──► 回中文澄清问句（不调 Needle3）
                                                  │ 否
                                                  ▼
    中文 → 英文（实体罗马化 + 概念映射）──► Needle3（英文单工具 schema）
                                                  │
                                                  ▼
                                      英文 tool_call JSON（city/song/text/time …）
                                                  │
                                                  ▼
                                      回传给 charlie 的 fncall 循环执行

Laya 负责"决策"（该调哪个工具 / 意图完整吗），Needle3 负责"执行"
（把英文句结构化成一个干净的工具调用）。两端都端侧、可离线。

## 用法

    # 命令行冒烟（中文端侧工具调用是否成立）
    python3 scripts/laya_needle_bridge.py

    # 作为库
    from scripts.laya_needle_bridge import LayaNeedleBridge
    bridge = LayaNeedleBridge()
    res = bridge.handle("北京今天天气怎么样")   # -> {"action":"call", "name":..., "arguments":...}
"""
import json
import os
import subprocess
import sys
import time

# ----------------------------------------------------------------------------- 路径
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # charlie/
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_NEEDLE_DIR = os.path.expanduser("~/研究/MFI_migrate/needle_engine/macos-arm64")
_NEEDLE_BIN = os.path.join(_NEEDLE_DIR, "needle")
_NEEDLE_MODEL = os.path.join(_NEEDLE_DIR, "needle3.cact")
_LAYA_DIR = os.path.expanduser("~/研究/MFI_migrate/laya/multilingual")

# Laya 延迟加载：首次 handle 时构造，避免 import 即拉 644MB 权重到内存
_LAYA_AGENT = None

# 澄清闸门豁免：纯查询类意图天然无需补充槽位（列出提醒/备忘/时间/天气
# 城市缺失时 charlie 有默认北京），对这些不触发澄清问句。
_NO_CLARIFY_INTENTS = {"time", "notes", "reminder_list", "weather"}

# ----------------------------------------------------------------------------- 工具英文 schema
# 与 charlie 真实工具同名同参（needle 的母语是英文）。每个意图只映射**单一**
# 英文工具 schema 喂给 Needle3——单工具模式实测最稳（conf 1.0），多工具会错选。
#
# 命名对齐 charlie 真实工具（magic-info / magic-reminder / magic-music 等）：
#   get_detailed_weather(city) / get_current_time() / list_reminders()
#   add_reminder(text, time) / play_music(keyword) / get_stock(code) / list_notes()
TOOL_SCHEMAS = {
    "get_detailed_weather": {
        "name": "get_detailed_weather",
        "description": "Get detailed weather and clothing advice for a city",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]}},
    "get_current_time": {
        "name": "get_current_time",
        "description": "Get the current date and time",
        "parameters": {"type": "object", "properties": {}}},
    "list_reminders": {
        "name": "list_reminders",
        "description": "List all upcoming reminders and schedules",
        "parameters": {"type": "object", "properties": {}}},
    "add_reminder": {
        "name": "add_reminder",
        "description": "Add a reminder with text and time",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"},
                                      "time": {"type": "string"}},
                       "required": ["text", "time"]}},
    "play_music": {
        "name": "play_music",
        "description": "Search and play a song by name, artist, or keyword",
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string"}},
                       "required": ["keyword"]}},
    "get_stock": {
        "name": "get_stock",
        "description": "Get the latest stock market quote for a stock code",
        "parameters": {"type": "object",
                       "properties": {"code": {"type": "string"}},
                       "required": ["code"]}},
    "list_notes": {
        "name": "list_notes",
        "description": "List the user's saved notes and memos",
        "parameters": {"type": "object", "properties": {}}},
}

# 候选意图（与上面 schema 一一对应）——Laya 的 choice 选项
INTENT_OPTIONS = {
    "weather": "get_detailed_weather",
    "time": "get_current_time",
    "reminder_list": "list_reminders",
    "reminder": "add_reminder",
    "music": "play_music",
    "stock": "get_stock",
    "notes": "list_notes",
    "chitchat": None,   # 闲聊，不调工具
}

# 写操作保护：默认只读/可逆场景真实执行，写操作（建提醒/放歌/控设备）需要
# allow_writes=True 才实际落地，否则只返回"已规划调用"不副作用。
# 与 bench_model_toolcall.py 的安全边界一致（不建提醒/不写偏好/不控设备）。
_WRITE_TOOLS = {"add_reminder", "play_music"}

# 无参工具：参数固定（如 get_current_time / list_reminders），不需要 Needle3
# 抽参，直接构造空参调用即可——既修"时间意图边缘误判导致 zero-call"的 bug，
# 又省一次 Needle3 推理延迟。
_NO_ARG_TOOLS = {"get_current_time", "list_reminders", "list_notes"}

# ----------------------------------------------------------------------------- 越界护栏
# 双引擎只覆盖"天气/时间/列提醒/设提醒/音乐/股票/备忘"七类。对清晰落在其它域
# （浏览器/摄像头视觉/偏好记忆存写）的语句，Laya 偶尔会硬塞进最近的候选意图
# （如把"记住X"塞成 reminder_list、把 URL 塞成 stock）。这种误触发比"放手"
# 更糟——它用错工具。于是加一层确定性关键词护栏：识别到清晰越界就强制 abstain，
# 让 _brain_llm 接管（与 voice_agent 的 FastPath 关键词前置同一思路）。
import re as _re
_URL_RE = _re.compile(r"https?://")

# 偏好/记忆存写（不是设提醒）：这些应落 magic-preferences / magic-memory
_MEMORY_STORE_KW = ("记住", "记一下", "我的偏好", "我喜欢", "我住", "我养",
                    "我的口味", "记着", "记到", "存一下")
# 摄像头视觉类（不是列提醒）
_CAMERA_KW = ("猫眼", "看屏幕", "屏幕上有", "看看屏幕", "看下屏幕", "摄像头",
              "看看摄像", "看一下摄像", "院子", "看一眼", "看下院子", "看看院子")
# 设提醒已"完整"的信号：既有时间词又有内容词 → 不必追问，直接调 add_reminder
_REMINDER_TIME_KW = ("分钟", "秒", "小时", "点钟", "点整", "点", "明天", "后天",
                     "大后天", "早上", "下午", "晚上", "中午", "凌晨", "周末",
                     "周", "星期", "今天", "每天", "下次", "来年", "下月", "本月")
_REMINDER_CONTENT_KW = ("喝水", "吃药", "交", "会", "取", "快递", "出门", "睡觉",
                        "叫", "看", "检查", "买", "做", "路", "垃圾", "饭", "班",
                        "电", "衣服", "猫", "狗", "花", "灯", "窗", "门", "早餐",
                        "缴费", "还", "洗", "晒", "浇", "关", "开", "收", "发",
                        "打", "体检", "锻炼", "散步")


def _reminder_is_complete(text: str) -> bool:
    """设提醒语句是否已含明确时间 + 内容（无需追问即可执行）。"""
    has_time = any(k in text for k in _REMINDER_TIME_KW)
    has_content = any(k in text for k in _REMINDER_CONTENT_KW)
    return has_time and has_content


def _guard_route(text: str, intent: str, route_r: dict) -> str | None:
    """清晰越界 → 返回 "abstain"（强制放手给 LLM）；否则 None。

    对"设提醒已完整"的特例：就地把 route_r 的 needs_clarify 压到 0，避免
    Laya 的澄清闸门对明显完整的句子多问一句（如"提醒我1分钟后喝水"）。
    """
    # URL 必是浏览器域，双引擎不覆盖
    if _URL_RE.search(text):
        return "abstain"
    # 偏好/记忆存写不是设提醒
    if intent in ("reminder", "reminder_list") and any(
            k in text for k in _MEMORY_STORE_KW) and not any(
            k in text for k in ("有哪些", "有什么", "今天", "我的提醒", "列表")):
        return "abstain"
    # 摄像头视觉不是列提醒
    if intent == "reminder_list" and any(k in text for k in _CAMERA_KW):
        return "abstain"
    # 设提醒已完整 → 压制澄清闸门
    if intent == "reminder" and _reminder_is_complete(text):
        if route_r.get("needs_clarify", 0) > 0.5:
            route_r["needs_clarify"] = 0.0
    return None

# ----------------------------------------------------------------------------- 中英概念映射（轻量，覆盖常见动词/时间词）
_CONCEPT = {
    "天气": "weather", "穿衣": "wear", "穿什么": "wear",
    "怎么样": "", "如何": "", "气温": "temperature", "温度": "temperature",
    "几度": "temperature", "下雨": "rain", "带伞": "bring an umbrella",
    "提醒": "remind", "提醒我": "remind me", "设个提醒": "set a reminder",
    "备忘": "remind", "日程": "schedule", "叫我": "remind me to",
    "交电费": "pay the electricity bill", "电费": "electricity bill",
    "吃药": "take medicine", "开会": "attend the meeting", "起床": "wake up",
    "放首": "play", "播放": "play", "歌": "song", "听": "play",
    "现在几点": "what time is it now", "几点了": "what time is it",
    "今天": "today", "明天": "tomorrow", "早上": "in the morning",
    "上午": "in the morning", "下午": "in the afternoon", "晚上": "in the evening",
    "八点": "at 8am", "八点整": "at 8am", "九点": "at 9am",
}


def _get_laya():
    global _LAYA_AGENT
    if _LAYA_AGENT is None:
        import sys
        sys.path.insert(0, os.path.dirname(_LAYA_DIR))
        sys.path.insert(0, _LAYA_DIR)
        from rl_agent_api import RLAgent
        _LAYA_AGENT = RLAgent(_LAYA_DIR, device="cpu")
    return _LAYA_AGENT


def _laya_ask(state: str, questions: dict):
    return _get_laya().system_one(state, questions)["answers"]


def _romanize_entity(text: str) -> str:
    """把中文专有名词（城市/人名/歌名）罗马化，英文句里 Needle3 才能消费。

    只取 CJK 连续段做拼音，其余(已英文/数字)原样保留。
    """
    from pypinyin import pinyin, Style
    out = []
    buf = []
    for ch in text:
        if "一" <= ch <= "鿿":
            buf.append(ch)
        else:
            if buf:
                py = [x[0].capitalize() for x in pinyin("".join(buf), style=Style.NORMAL)]
                out.append("".join(py))
                buf = []
            out.append(ch)
    if buf:
        py = [x[0].capitalize() for x in pinyin("".join(buf), style=Style.NORMAL)]
        out.append("".join(py))
    return "".join(out).strip()


def to_english(utterance: str) -> str:
    """中文 utterance → 紧凑英文句（实体罗马化 + 概念映射 + 模板）。

    目标不是文学翻译，是给 Needle3 一个它母语的、实体已英文化的干净输入，
    让它专注做结构化抽参。
    """
    # 1) 概念替换（长串优先）
    s = utterance
    for cn, en in sorted(_CONCEPT.items(), key=lambda kv: -len(kv[0])):
        if en:
            s = s.replace(cn, " " + en + " ")
    # 2) 剩余 CJK（专有名词）罗马化
    s = _romanize_entity(s)
    # 3) 清洗
    s = " ".join(s.split())
    s = s.strip(" .,，。")
    # 4) 兜底模板：若几乎没有英文内容，至少把原始中文罗马化作为主体
    if len(s) < 3:
        s = _romanize_entity(utterance)
    return s or utterance


def _run_needle(tool_schema: dict, english_prompt: str) -> dict:
    """调 Needle3 二进制，返回 {name, arguments, confidence} 或 None。"""
    if not os.path.exists(_NEEDLE_BIN):
        raise FileNotFoundError(f"needle 二进制不存在: {_NEEDLE_BIN}")
    tools_path = "/tmp/laya_needle_tools.json"
    with open(tools_path, "w", encoding="utf-8") as f:
        json.dump([tool_schema], f, ensure_ascii=False)
    t0 = time.time()
    out = subprocess.run(
        [_NEEDLE_BIN, "--model", _NEEDLE_MODEL, "--tools", tools_path,
         "--prompt", english_prompt],
        capture_output=True, text=True, timeout=60)
    dt = time.time() - t0
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": "needle 输出解析失败", "raw": out.stdout[-200:], "stderr": out.stderr[-200:]}
    calls = d.get("function_calls") or []
    if not calls:
        return {"action": "none", "confidence": d.get("confidence"), "latency_ms": round(dt * 1000, 1)}
    c = calls[0]
    return {"action": "call", "name": c.get("name"), "arguments": c.get("arguments"),
            "confidence": d.get("confidence"), "latency_ms": round(dt * 1000, 1)}


class LayaNeedleBridge:
    """Laya 决策 + Needle3 执行的混合端侧工具调用桥。"""

    def __init__(self, clarify_threshold: float = 0.5, intent_threshold: float = 0.5):
        self.clarify_threshold = clarify_threshold
        self.intent_threshold = intent_threshold

    def route(self, utterance: str) -> dict:
        """Laya 中文意图路由 + 澄清闸门。

        先用**确定性关键词**做时间/天气消歧（Laya 偶把"几点"误判成 weather，
        但两者触发词不重叠，关键词硬判零误判、且省一次 Laya 调用）；其余再交 Laya。
        """
        # 关键词前置：时间类（不含天气词）→ 直接 time，跳过 Laya
        _TIME_KW = ["几点", "几号", "星期几", "今天日期", "现在时间", "现在几点",
                    "日期", "礼拜几", "什么时间"]
        _WEATHER_KW = ["天气", "气温", "温度", "穿衣", "带伞", "下雨", "几度"]
        has_time = any(k in utterance for k in _TIME_KW) or utterance in ("现在几点了", "几点了")
        has_weather = any(k in utterance for k in _WEATHER_KW)
        if has_time and not has_weather:
            intent, conf = "time", 1.0
            clarify = 0.0
            return {"intent": intent, "intent_conf": conf, "needs_clarify": clarify,
                    "clarify_threshold": self.clarify_threshold,
                    "intent_threshold": self.intent_threshold, "keyword_hit": True}
        questions = {
            "intent_route": {
                "type": "choice",
                "instructions": "用户这句话想做什么？从候选中选最匹配的一项。",
                "criteria": {
                    "weather": "询问天气/气温/穿衣建议",
                    "time": "询问时间/日期/几点",
                    "reminder_list": "查看/列出已有的提醒和日程",
                    "reminder": "设置提醒/日程/待办/定时器",
                    "music": "播放音乐/歌曲/歌单",
                    "stock": "查询股票行情/大盘指数",
                    "notes": "查看/列出备忘和笔记",
                    "chitchat": "闲聊/问候/一般问答",
                },
            },
            "needs_clarify": {
                "type": "noul",
                "instructions": "用户这句话的意图是否完整，不需要再追问就能直接执行？",
                "criteria": {"false": "意图不完整，需要追问", "true": "意图完整，可直接执行"},
            },
        }
        ans = _laya_ask(utterance, questions)
        intent = ans["intent_route"]["choice"]
        conf = ans["intent_route"].get("confidence", 0.0)
        clarify = ans["needs_clarify"]["noul"]
        return {"intent": intent, "intent_conf": conf,
                "needs_clarify": clarify, "clarify_threshold": self.clarify_threshold,
                "intent_threshold": self.intent_threshold}

    def warmup(self) -> float:
        """预加载 Laya 权重（~644MB 首次进内存约 20s），进程启动时调一次，
        避免首句用户 utterance 卡在模型加载上。返回耗时秒。"""
        t0 = time.time()
        _get_laya()
        return time.time() - t0

    def handle(self, utterance: str) -> dict:
        """端到端：中文 utterance → 工具调用 / 澄清 / 闲聊。

        两级闸门（阈值与现有 decide_intent 的 abstain=0.5 一致）：
          - 意图置信度 < intent_threshold：Laya 不确定，落闲聊/LLM，不调 Needle3；
          - 槽位不完整 (needs_clarify > clarify_threshold)：回澄清问句，不调 Needle3；
          - 否则：罗马化→英文→Needle3 执行英文工具调用。
        """
        t0 = time.time()
        r = self.route(utterance)
        intent = r["intent"]
        conf = r["intent_conf"]
        tool_name = INTENT_OPTIONS.get(intent)

        # 越界护栏：清晰度落在双引擎覆盖域之外的句子强制放手给 LLM，
        # 避免 Laya 把"记住X/看URL/看猫眼"硬塞成最近候选（误触发比放手更糟）。
        # 注意：必须在读 clarify 之前调，护栏内部会把"设提醒已完整"的 needs_clarify
        # 压成 0，这里必须重新读 r 才能生效（之前在一处 capture 早了导致压不动）。
        guard = _guard_route(utterance, intent, r)
        if guard == "abstain":
            return {"action": "chitchat", "intent": intent, "guarded": True,
                    "needs_clarify": r["needs_clarify"],
                    "latency_ms": round((time.time() - t0) * 1000, 1)}
        clarify = r["needs_clarify"]

        if intent == "chitchat" or tool_name is None:
            return {"action": "chitchat", "intent": intent,
                    "needs_clarify": clarify, "latency_ms": round((time.time() - t0) * 1000, 1)}

        # 意图置信度闸门：Laya 都不确定，别让 Needle3 瞎执行
        if conf < self.intent_threshold:
            return {"action": "chitchat", "intent": intent, "intent_conf": conf,
                    "needs_clarify": clarify, "latency_ms": round((time.time() - t0) * 1000, 1)}

        if clarify > self.clarify_threshold and intent not in _NO_CLARIFY_INTENTS:
            return {"action": "clarify", "intent": intent,
                    "needs_clarify": clarify,
                    "clarify_question": self._clarify_question(intent),
                    "latency_ms": round((time.time() - t0) * 1000, 1)}

        # 无参工具直接构造调用，绕过 Needle3（省延迟 + 避免边缘误判 zero-call）
        if tool_name in _NO_ARG_TOOLS:
            return {"action": "call", "name": tool_name, "arguments": {},
                    "confidence": None, "intent": intent, "intent_conf": r["intent_conf"],
                    "english_prompt": None,
                    "latency_ms": round((time.time() - t0) * 1000, 1)}

        en = to_english(utterance)
        res = _run_needle(TOOL_SCHEMAS[tool_name], en)
        res.update({"intent": intent, "intent_conf": r["intent_conf"],
                    "english_prompt": en, "latency_ms": round((time.time() - t0) * 1000, 1)})
        return res

    def execute(self, utterance: str, allow_writes: bool = False) -> dict:
        """端到端 + 真实执行：中文 utterance → Laya 路由 → Needle3 抽参 →
        调 charlie 真实工具（resolve_inproc_tools）拿到工具结果。

        allow_writes=False（默认）：写操作（add_reminder / play_music）只返回
        规划好的调用、不实际落地（安全边界：不建提醒/不放歌/不控设备）。
        allow_writes=True：写操作也真实执行。只读工具任何时候都执行。
        """
        plan = self.handle(utterance)
        if plan.get("action") != "call":
            return plan
        name = plan.get("name")
        args = plan.get("arguments") or {}
        # 槽位为空（Needle3 没抽到）也不硬执行
        if not name:
            return {**plan, "executed": False, "result": None}
        is_write = name in _WRITE_TOOLS
        if is_write and not allow_writes:
            return {**plan, "executed": False,
                    "result": None,
                    "note": "写操作未授权（allow_writes=False），仅规划不执行"}
        try:
            # 真实工具解析：沿用 charlie 的进程内工具层，按 mcp_set 加载同名工具
            from agent.inproc_tools import resolve_inproc_tools
            # 找工具所属的 mcp_set：遍历已知域名，命中即停
            target = None
            for mcp in ("amap-maps", "magic-info", "magic-reminder",
                        "magic-music", "magic-jarvis", "magic-notes"):
                for tool in resolve_inproc_tools(mcp):
                    if getattr(tool, "name", None) == name:
                        target = tool
                        break
                if target:
                    break
            if target is None:
                return {**plan, "executed": False,
                        "result": None, "error": f"charlie 无同名工具: {name}"}
            raw = target.call(json.dumps(args, ensure_ascii=False))
            return {**plan, "executed": True, "result": raw[:500]}
        except Exception as e:
            return {**plan, "executed": False, "result": None, "error": str(e)[:160]}

    def _clarify_question(self, intent: str) -> str:
        return {
            "weather": "你想查哪个城市的天气？",
            "time": "你想知道现在的时间吗？",
            "reminder": "你想设置什么提醒、在什么时间？",
            "reminder_list": "你想看哪类提醒？",
            "music": "你想听哪首歌或哪位歌手？",
            "stock": "你想查哪只股票？",
            "notes": "你想看哪类备忘？",
        }.get(intent, "能再说具体一点吗？")


def _demo():
    bridge = LayaNeedleBridge()
    print(">> warming up Laya (first load ~20s) ...", flush=True)
    wt = bridge.warmup()
    print("   warmup %.1fs" % wt)
    print("== Laya + Needle3 混合端侧工具调用桥（含 charlie 真实工具执行）==")
    cases = [
        "北京今天天气怎么样，要穿什么",
        "上海明天天气如何，要带伞吗",
        "提醒我明天早上八点交电费",
        "放首周杰伦的歌",
        "现在几点了",
        "我今天有什么提醒",
        "帮我设个提醒",
        "今天心情不太好",
    ]
    for c in cases:
        r = bridge.execute(c, allow_writes=False)  # 默认只读，写操作只规划不执行
        print(f"\n· {c}")
        print(f"  {json.dumps(r, ensure_ascii=False)}")


if __name__ == "__main__":
    _demo()
