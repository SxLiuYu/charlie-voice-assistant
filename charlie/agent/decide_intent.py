"""Decider-2B 本地意图决策头（`DECIDE_ENABLED=1` 启用，decider-first）.

Mapika/decider-2b（MLX 8bit，1.9GB，本地 HTTP 127.0.0.1:8080）不是生成式模型，
是"问题+选项→校准概率"的决策头。charlie 的意图分类（`agent/llm._classify_intent`
的 LLM 兜底环节）正是这个形状，且实测痛点明确：云端 LLM 分类 0.82–1.08s、
reasoning 模型思维链饿死过 P0。decider 在本机 M4 上：

  | 配置                       | 精度                    | 延迟        |
  | 域说明做 context（全量）    | 25/28 全对 0 错判        | 634ms       |
  | + context 缓存（state_first)| 8/8 全对                | 中位 253ms  |
  | 裸 context                 | 53% 弃权（不可用）        | 271ms       |

接入形状：decider-first——conf ≥ `DECIDE_INTENT_ABSTAIN` 直接用（省云端 1s）；
弃权 / 未启用 / 服务不可用 → 返回 None，调用方落回既有 LLM 分类，行为不变。

红线（来自 /Users/sxliuyu/models/decide_使用方案.md）：
- context 必须非空（空串服务端 400）；
- 本模块只走全量/state_first 标定（T=1.30），不碰 schema 路径（T=1.18），
  阈值不可混用；
- context_id 相同 = 同一文本：域说明变更必须换 id（如 charlie-intent-v2）；
- 服务进程常驻 ~4.6GB，挂了本模块静默回落（连续失败 3 次冷却 300s，
  不在语音热路径上反复吃连接超时）。
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("magic")

_DECIDE_CTX_ID = "charlie-intent-v5"   # 见下方 context 注释：改说明必须换 id
_CONNECT_TIMEOUT_S = 1.0
_READ_TIMEOUT_S = 2.5
_FAIL_THRESHOLD = 3          # 连续失败 N 次 → 冷却（服务挂了别每句都等超时）
_FAIL_COOLDOWN_S = 300.0

# 29 个域，与 _classify_intent 的 prompt 选项同一集合、同一顺序
DOMAINS = ["none", "amap-maps", "baize-skills", "filesystem", "magic-music",
           "magic-reminder", "magic-notes", "magic-system", "magic-info",
           "magic-life", "ac-control", "magic-recipe", "magic-preferences",
           "magic-dev", "magic-scenes", "magic-evolution", "magic-browser",
           "magic-apps", "magic-feishu", "magic-douyin", "magic-taobao",
           "magic-wardrobe", "magic-jarvis", "magic-memory", "magic-habits",
           "magic-summary", "mimo-vision", "magic-tv", "magic-agent"]

# 域说明原文做 context——改内容必须换 _DECIDE_CTX_ID（同 id = 同文本，服务端缓存）
# 校准史（2026-09-20，67 句语料多变体对比，阈值 0.5 下的实错判数）：
#   v1 原版: 3 错（电视音量→ac-control / 太阳系→magic-info / 晚安→scenes）
#   v2: 2 错（+magic-info 负向说明修掉前两个，但"音箱等本机设备"把风扇带到 magic-system）
#   v5 实验稿（未出厂，作废）: 3 错（magic-system 提"空调/灯/风扇"字样反而吸走
#       "电视音量"——描述里提电器名会反噬）
#   v4: 1 错（magic-system 只讲播放参数不提电器名，风扇回 ac-control；仅剩晚安→scenes，
#             说晚安进夜间模式属可辩护的产品行为；阈值维持 0.5——抬高到 0.6 只能多挡风扇、
#             却让 4 个正确快命中回落 LLM，帕累托不划算）
#   v5 本版（2026-09-20 晚）: v4 文本 + magic-agent 开放任务域行，74 句重校准：
#       8 句 magic-agent 语料全对（conf 0.78–0.96）；负向全对（"搜索备份软件"
#       →baize 0.63、"整理一下笔记"→notes 0.99）；阈值 0.5 下 2 错与 v4 同级
#       （晚安→scenes 可辩护、那后天→info 是上下文追问的正确行为）；"我出门了"
#       选 magic-life 但 conf=0.41<0.5 弃权落 LLM（magic-life 描述里有"出门"，
#       弃权兜住）。阈值维持 0.5——0.45 多 1 错、0.55 多挡 3 个正确快命中。
# 结论：准确率修复走 context 描述而非抬阈值（抬阈值对全部查询收效率税，描述修复免费）。
_DECIDE_CTX = """任务: 判断用户输入需要哪个工具域。
- none = 闲聊/问候/常识问答/讲笑话/感恩告别（你好/谢谢/晚安/讲个笑话/太阳系最大的行星/1加1等于几）
- amap-maps = 天气/地图/导航/我在哪/附近/路线/加油站/温度
- baize-skills = 搜索互联网/查资料/购物/买东西/票房
- magic-music = 播放/歌单/音乐/随机播放/唱歌
- magic-reminder = 提醒/日程/定时器/日历/叫我起床
- magic-notes = 备忘录/笔记/记一下/记录
- magic-system = 音箱音量/语速/屏幕亮度调节（播放参数；查询硬件"设备状态"也归本域）
- magic-info = 时间/日期/新闻/翻译/计算/几点/算术（只处理这些，不答常识题）
- magic-memory = 回忆/查询过去对话记住的事（我的猫叫什么/你记得吗/我之前说过什么）
- magic-preferences = 设置/查询/删除用户偏好（我喜欢/我的习惯/记住我的）
- magic-life = 外卖/充电桩/特斯拉/出门
- magic-recipe = 做菜/菜谱/食谱/食材/吃什么/怎么做
- magic-scenes = 明确说"模式"的场景切换（晚安模式/早安模式/出门场景）——只说"晚安"是告别=none
- magic-browser = 浏览器/打开网页/搜索网页/截图/百度
- ac-control = 空调/制冷/制热/风扇/灯光/温度调节（电视控制归 magic-tv，不是这里）
- mimo-vision = 截图/看看屏幕/识别图片/图中有什么
- magic-tv = 电视DLNA控制（电视音量/电视静音/投屏到电视，所有电视控制都归这里）
- magic-agent = 开放式系统任务：清理磁盘/释放空间/整理下载/排查网络/备份归档/修复故障等
  需要多步操作、固定工具域覆盖不了的系统维护请求（问"为什么慢/怎么办"也算）
注意："音量调大"没有说电视时是音箱设备 = magic-system；"记一下开会"是备忘 = magic-notes。"""

_session = None              # 惰性 requests.Session（测试替换入口）
_registered = False
_reg_lock = threading.Lock()
_laya_agent = None            # Laya RLAgent 单例（644MB 权重，只加载一次）
_fail_streak = 0
_disabled_until = 0.0
_fail_lock = threading.Lock()


def _base() -> str:
    return os.getenv("DECIDE_BASE", "http://127.0.0.1:8080").rstrip("/")


def _abstain() -> float:
    try:
        return float(os.getenv("DECIDE_INTENT_ABSTAIN", "0.5"))
    except ValueError:
        return 0.5


# Laya 后端：29 域全量塞给 Laya 会撑爆 head_max_len（选项过多退化，实测
# "北京天气"→none）。正确粒度是**粗分组**（≤10 选项，Laya 校准概率最稳）。
# 这里只让 Laya 做它擅长的中文粗路由 + 工具/闲聊判定；命中粗组且 conf 够高
# 时用该组的主域，否则落回 LLM 做细粒度 29 域分类（decider-first 精神）。
#
# 粗组 → (主域, 该组覆盖的细域)。主域用于直接返回；细域列表供后续扩展。
_LAYA_GROUPS = {
    "weather": ("amap-maps", ["amap-maps"]),
    "music": ("magic-music", ["magic-music"]),
    "reminder": ("magic-reminder", ["magic-reminder"]),
    "time": ("magic-info", ["magic-info"]),
    "browser": ("magic-browser", ["magic-browser", "magic-apps", "magic-douyin",
                                   "magic-taobao", "magic-feishu"]),
    "device": ("ac-control", ["ac-control", "magic-tv", "magic-system"]),
    "memory": ("magic-memory", ["magic-memory", "magic-notes", "magic-preferences",
                                "magic-habits", "magic-summary"]),
    "recipe": ("magic-recipe", ["magic-recipe"]),
    "agent": ("magic-agent", ["magic-agent", "magic-dev", "magic-evolution",
                              "magic-life", "magic-scenes", "magic-roles",
                              "magic-jarvis", "mimo-vision", "baize-skills",
                              "filesystem", "magic-wardrobe"]),
    "chitchat": ("none", ["none"]),
}
# Laya choice 的 criteria 文本：各组尽量**互斥**、短、聚焦典型触发词，
# 避免"天气/时间""做菜/唱歌""清理/记忆"这类跨组串味（实测误判来源）。
_GROUP_CRITERIA = {
    "weather": "问天气、气温几度、穿衣建议、地图导航、我在哪、附近路线",
    "music": "播放音乐、放首歌、歌单、随机播放、唱首歌",
    "reminder": "设置提醒、定日程、定时器、待办事项、叫我起床",
    "time": "问现在几点、今天几号、日期、简单算术计算、查新闻翻译",
    "browser": "用浏览器打开网页、网页搜索、截图、打开抖音淘宝飞书等App",
    "device": "调空调温度、开关灯风扇、电视音量、音箱音量、设备状态",
    "memory": "回忆以前说的、记一下备忘、设置用户偏好、我的习惯总结",
    "recipe": "做菜、菜谱、今天吃什么、怎么做饭、食材做法",
    "agent": "系统维护任务：清理磁盘、排查网络、备份归档、场景模式切换、角色切换、搜索资料",
    "chitchat": "闲聊问候、讲笑话、常识问答、感恩告别、心情抒发",
}


def _ask_laya_http(text: str):
    """Laya 常驻服务（scripts/laya_service.py，默认 127.0.0.1:8091）路径。

    权重只加载一次、进程级常驻，所有 charlie 入口共享同一热实例，避免每个
    进程冷启动 ~20s。服务不存在 / 异常 → 返回 None，调用方落回进程内加载。
    """
    base = os.getenv("DECIDE_LAYA_URL", "http://127.0.0.1:8091").rstrip("/")
    try:
        import requests
        r = requests.post(
            f"{base}/v1/decide",
            json={"context_id": "charlie-laya-intent",
                  "questions": [{"question": f"用户说：「{text[:100]}」",
                                 "options": list(_LAYA_GROUPS.keys())}],
                  "abstain_below": 0.0},
            timeout=(1.0, 2.5))
        if r.status_code != 200:
            return None
        a = (r.json().get("answers") or [{}])[0]
        choice = a.get("choice")
        conf = a.get("confidence")
        # 服务已把粗组映射到 29 域主域返回；若返回的是粗组 key（旧服务）也接纳
        if choice is None or conf is None:
            return {"choice": None, "conf": 0.0, "status": 200}
        if choice in _LAYA_GROUPS:
            choice = _LAYA_GROUPS[choice][0]
        if choice not in DOMAINS:
            return {"choice": None, "conf": 0.0, "status": 200}
        return {"choice": choice, "conf": float(conf), "status": 200}
    except Exception as e:
        log.debug("[decide.laya] 服务不可用，落回进程内加载: %s", e)
        return None


def _ask_laya(text: str):
    """Laya 本地决策头（粗分组路由）：中文 utterance → (主域, conf)。

    优先走常驻 HTTP 服务（脚本 scripts/laya_service.py，权重只加载一次、
    跨进程共享）；服务不在 → 进程内加载单例（首次 ~20s，之后复用）。
    返回与 _ask 同形的 {"choice": str|None, "conf": float, "status": int|None}，
    choice 已映射到 29 域中的主域。永不抛异常。
    """
    if os.getenv("DECIDE_LAYA_URL", "1") != "0":   # 设 0 强制进程内
        res = _ask_laya_http(text)
        if res is not None:
            return res
    try:
        import sys
        laya_root = os.path.expanduser("~/研究/MFI_migrate/laya")
        sys.path.insert(0, laya_root)
        sys.path.insert(0, os.path.join(laya_root, "multilingual"))
        from rl_agent_api import RLAgent
    except Exception as e:
        log.warning("[decide.laya] 依赖不可用: %s", e)
        return {"choice": None, "conf": 0.0, "status": None}

    global _laya_agent
    if _laya_agent is None:
        try:
            _laya_agent = RLAgent(os.path.join(laya_root, "multilingual"), device="cpu")
        except Exception as e:
            log.warning("[decide.laya] 模型加载失败: %s", e)
            return {"choice": None, "conf": 0.0, "status": None}

    questions = {
        "intent_route": {
            "type": "choice",
            "instructions": "用户的一句话属于哪类需求？从候选中选最匹配的一项。",
            "criteria": _GROUP_CRITERIA,
        }
    }
    try:
        out = _laya_agent.system_one(f"用户：{text[:200]}", questions)
        ans = out["answers"]["intent_route"]
        group = ans.get("choice")
        conf = ans.get("confidence", 0.0)
        if group not in _LAYA_GROUPS:
            return {"choice": None, "conf": 0.0, "status": 200}
        return {"choice": _LAYA_GROUPS[group][0], "conf": float(conf), "status": 200}
    except Exception as e:
        log.warning("[decide.laya] 推理失败: %s", e)
        return {"choice": None, "conf": 0.0, "status": None}


def enabled() -> bool:
    """每次调用现读（改 .env 后下一句生效，无需重启）。"""
    return os.getenv("DECIDE_ENABLED", "0") == "1"


def backend() -> str:
    """决策后端：http=Decider-2B（默认，原行为）/ laya=Laya 多语言本地决策头。

    Laya 中文本地、无需常驻 HTTP 服务、CPU ~95ms，与 http 后端走同一套
    decider-first / abstain→LLM 落回逻辑，仅在"怎么拿到 (choice, conf)"上不同。
    """
    return os.getenv("DECIDE_BACKEND", "http").lower()


def _get_session():
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def _register_context() -> bool:
    """注册域说明 context（进程内一次；服务重启/LRU 淘汰后由 decide 内自愈重注册）。"""
    global _registered
    with _reg_lock:
        if _registered:
            return True
        try:
            r = _get_session().post(
                f"{_base()}/v1/contexts",
                json={"context_id": _DECIDE_CTX_ID, "context": _DECIDE_CTX},
                timeout=(_CONNECT_TIMEOUT_S, 10.0))  # 注册要 prefill 全文，放宽读超时
            if r.status_code == 200:
                _registered = True
                log.info("[decide] context 注册完成: %s", _DECIDE_CTX_ID)
                return True
            log.warning("[decide] context 注册失败: HTTP %s %s",
                        r.status_code, r.text[:120])
        except Exception as e:
            log.warning("[decide] context 注册失败: %s", e)
        return False


def _cooldown_active() -> bool:
    with _fail_lock:
        return time.monotonic() < _disabled_until


def _mark_fail() -> None:
    global _fail_streak, _disabled_until
    with _fail_lock:
        _fail_streak += 1
        if _fail_streak >= _FAIL_THRESHOLD:
            _disabled_until = time.monotonic() + _FAIL_COOLDOWN_S
            log.warning("[decide] 连续失败%d次，冷却%gs（期间直接走LLM分类）",
                        _fail_streak, _FAIL_COOLDOWN_S)


def _mark_ok() -> None:
    global _fail_streak, _disabled_until
    with _fail_lock:
        _fail_streak = 0
        _disabled_until = 0.0


def decide_intent(text: str):
    """decider-first 意图决策。

    返回域名（str）= 高置信命中，调用方直接使用；
    返回 None = 弃权 / 未启用 / 冷却中 / 服务不可用，调用方落回 LLM 分类。
    **永不抛异常**——它在语音热路径上。
    """
    if not enabled() or not text or not text.strip():
        return None
    if _cooldown_active():
        return None
    try:
        t0 = time.monotonic()
        # Laya 后端：中文本地决策头，无需 context 注册 / HTTP，路径独立
        if backend() == "laya":
            res = _ask_laya(text)
            if res is None or res["choice"] is None:
                # Laya 失败：不计冷却（本地权重偶发异常），落回 LLM
                return None
            choice, conf = res["choice"], res["conf"]
            if conf < _abstain():
                log.info("[decide.laya] 弃权: '%s' conf=%.2f < %.2f → 走LLM分类",
                         text[:30], conf, _abstain())
                return None
            if choice not in DOMAINS:
                # 防御性：_ask_laya 已映射主域，这里再核一道越界（热路径安全）
                log.warning("[decide.laya] 越界主域 '%s'，落回LLM", choice)
                return None
            log.info("[decide.laya] 命中: '%s' → %s (conf=%.2f, %.0fms)",
                     text[:30], choice, conf, (time.monotonic() - t0) * 1000)
            return choice
        s = _get_session()
        if not _register_context():
            _mark_fail()
            return None
        res = _ask(s, text)
        if res is None:
            # 网络/解析失败：服务不可用，计失败（连续 3 次冷却，别每句都等超时）
            _mark_fail()
            return None
        if res["choice"] is None:
            if res["status"] == 400:
                # 服务重启 / LRU 淘汰 → unknown context_id：重注册再试一次
                global _registered
                with _reg_lock:
                    _registered = False
                if not _register_context():
                    _mark_fail()
                    return None
                res = _ask(s, text)
                if res is None:
                    _mark_fail()
                    return None
            if res["choice"] is None:
                # 服务端弃权/异常结构：服务是好的，不计失败也不计成功
                return None
        choice, conf = res["choice"], res["conf"]
        if choice not in DOMAINS:
            log.warning("[decide] 越界选项 '%s'，弃权", choice)
            return None
        if conf < _abstain():
            log.info("[decide] 弃权: '%s' conf=%.2f < %.2f → 走LLM分类",
                     text[:30], conf, _abstain())
            _mark_ok()  # 服务是好的，别让弃权累积成冷却
            return None
        _mark_ok()
        log.info("[decide] 命中: '%s' → %s (conf=%.2f, %.0fms, mode=state_first_cached)",
                 text[:30], choice, conf, (time.monotonic() - t0) * 1000)
        return choice
    except Exception as e:
        log.warning("[decide] 决策失败: %s", e)
        _mark_fail()
        return None


def _ask(s, text):
    """单次 /v1/decide 调用。

    返回 {"choice": str|None, "conf": float, "status": int|None}；
    网络/解析失败返回 None（与"服务端给了不可用答案"区分开）。
    """
    try:
        r = s.post(
            f"{_base()}/v1/decide",
            json={
                "context_id": _DECIDE_CTX_ID,
                "questions": [{"question": f"用户说：「{text[:100]}」。这句话需要哪个工具域？",
                               "options": DOMAINS}],
                "abstain_below": 0.0,   # 阈值由本模块按 _abstain() 把关，服务端不弃权
            },
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S))
        if r.status_code != 200:
            log.warning("[decide] HTTP %s: %s", r.status_code, r.text[:120])
            return {"choice": None, "conf": 0.0, "status": r.status_code}
        a = (r.json().get("answers") or [{}])[0]
        choice = a.get("choice")
        conf = a.get("confidence")
        if choice is None or conf is None:
            return {"choice": None, "conf": 0.0, "status": r.status_code}
        return {"choice": str(choice), "conf": float(conf), "status": r.status_code}
    except Exception as e:
        log.warning("[decide] 请求失败: %s", e)
        return None


def status() -> dict:
    """观测用：开关/冷却/注册态。不回传敏感内容。"""
    with _fail_lock:
        return {
            "enabled": enabled(),
            "base": _base(),
            "registered": _registered,
            "fail_streak": _fail_streak,
            "cooldown_left_s": max(0.0, round(_disabled_until - time.monotonic(), 1)),
            "abstain_below": _abstain(),
            "context_id": _DECIDE_CTX_ID,
        }


def _reset_for_tests() -> None:
    global _session, _registered, _fail_streak, _disabled_until
    _session = None
    _registered = False
    _fail_streak = 0
    _disabled_until = 0.0
