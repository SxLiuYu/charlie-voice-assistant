"""系统提示词构建"""
import os
import logging
import platform as _platform
import time
import threading

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_current_user_id = os.environ.get("CHARLIE_USER_ID", "default")

# system msg TTL 缓存
_SYSTEM_MSG_CACHE = {}  # key: (mcp_set, role) → (timestamp, msg)
_SYSTEM_MSG_TTL = int(os.getenv("SYSTEM_MSG_TTL", "300"))  # 5 分钟
_SYSTEM_MSG_LOCK = threading.Lock()

# 域行为注释（static：与 mcp_set 绑定，进缓存）。
# ⚠️ 这里**只写行为规则/语义提示，不写工具清单**——工具清单由
# `_derive_tools_line(mcp_set)` 从真实 function_list（resolve_inproc_tools）
# 实时派生。历史上工具清单手写在这里，结果与真实工具面反复漂移：amap 广告
# 5 个工具而真实 12 个（calculate/get_exchange_rate/get_holiday_info 等
# 模型根本不知道存在），filesystem 广告"读写文件"而真实域是 magic-notes
# 的备忘录工具。手写清单 = 每次增删工具都要记得改两处，必然烂。
# 2026-09-20 起清单只有一份真源：function_list 本身。
_DOMAIN_NOTES = {
    "amap-maps": (
        "调用工具获取真实数据，不要编造天气信息。"
        "汇率换算用 get_exchange_rate，节假日问询用 get_holiday_info。"
    ),
    "magic-music": (
        "\n播放音乐时不要说话，直接调用工具，不要回复任何文字。"
    ),
    "magic-recipe": (
        "调用工具拿真实菜谱，不要编造做法。"
    ),
    "magic-apps": (
        "打开对应App的网页版。"
    ),
    "magic-dev": (
        "代码修改前必须先 read_file 查看内容，edit_file 后会记录确认请求，"
        "用户回复'确认'才真正写盘。禁止删除文件、禁止批量修改、禁止访问 .env/.git 等敏感路径。"
    ),
    "ac-control": (
        "AC空调控制：通过语音快路径直接执行（识别关键词后自动调 Tuya 红外 API）。"
        "如果用户问空调相关操作，直接回复执行结果或引导用户说'打开空调'/'关闭空调'/'调到26度制冷'等明确指令。"
        "禁止告诉用户'没有权限'或'无法控制'，引导用语音指令重试。"
    ),
    "mimo-vision": (
        "你有视觉能力：可以截图或分析用户提供的图片。"
    ),
    "magic-preferences": (
        "当用户表达偏好（如'以后...''我喜欢...''别再...''我习惯...'）时立即调用 set_pref 记住。"
    ),
    "magic-roles": (
        "用户说'切换到X''变成X''回到默认'时调用 switch_role(角色ID) 切换角色；"
        "可用角色：charlie（默认）、jarvis、baize。不确定当前有哪些角色先 list_roles()。"
    ),
    "magic-jarvis": (
        "股票/空气质量/地震/赛事数据来自外部接口，可能耗时数秒；"
        "拿到结果如实转述，不要编造数据，也不要在工具未返回前猜测。"
    ),
}

# 不进提示的禁用/内部工具：告诉模型有这些工具，只会诱导它去调一个必然
# 失败的调用（run_code 是 magic-info.py 里因 RCE 风险永久禁用的桩；
# code_interpreter 同理）。数学计算走 calculate。
_HIDDEN_TOOLS = {"run_code", "code_interpreter"}


def _derive_tools_line(mcp_set: str) -> str:
    """从真实 function_list 派生"可用工具"行——提示与工具面零漂移的唯一办法。

    名字与参数全部取自 resolve_inproc_tools(mcp_set) 的包装后 BaseTool
    （.parameters 的 JSON schema properties），模型在 function_list 里收到
    的完整 schema 与此行严格一致；缺描述的键已在 inproc_tools 侧省略，
    不会把参数名当描述回填。stdio 回退路径的 brain 与 inproc 加载同一批
    模块，派生结果同样成立。

    任何失败（模块加载异常等）返回空串——上游照发只有行为注释的系统提示，
    不比现在差。首访有 _tool_cache，brain 构建时工具已加载过，无额外延迟。
    """
    try:
        from agent.inproc_tools import resolve_inproc_tools
        tools = resolve_inproc_tools(mcp_set)
    except Exception as e:
        log.debug(f"[agent.system_msg] 工具清单派生失败({mcp_set}): {e}")
        return ""
    parts = []
    for t in tools:
        if t.name in _HIDDEN_TOOLS:
            continue
        props = list(((t.parameters or {}).get("properties") or {}).keys())
        parts.append(f"{t.name}({', '.join(props)})" if props
                     else f"{t.name}()")
    if not parts:
        return ""
    return "可用工具：" + "，".join(parts) + "。"


def _build_tools_doc(mcp_set: str) -> str:
    """域工具文档 = 真实派生的工具清单 + 手写的行为注释。"""
    line = _derive_tools_line(mcp_set)
    notes = _DOMAIN_NOTES.get(mcp_set, "")
    return line + notes if notes else line

def _build_user_profile(uid: str | None = None) -> str:
    try:
        import json as _json
        if not uid:
            try:
                from agent.user_context import resolve_user
                uid = resolve_user()
            except Exception:
                uid = os.environ.get("CHARLIE_USER_ID", _current_user_id) or "default"
        _evolution_file = os.path.join(DATA_DIR, f"evolution_data_{uid}.json" if uid != "default" else "evolution_data.json")
        if not os.path.exists(_evolution_file):
            return ""
        with open(_evolution_file, 'r', encoding='utf-8') as _f:
            _ev_data = _json.load(_f)
        parts = []
        _learned = _ev_data.get("learned_preferences", {})
        _patterns = _ev_data.get("usage_patterns", {})
        _adapt = _ev_data.get("adaptation_state", {})
        if _learned:
            _prefs = "；".join(f"{k}={v}" for k, v in list(_learned.items())[:5])
            parts.append(f"用户偏好：{_prefs}")
        _topics = _patterns.get("top_topics", [])
        if _topics:
            parts.append(f"用户常问：{'、'.join(_topics[:5])}")
        _active_hours = _patterns.get("active_hours", [])
        if _active_hours:
            parts.append(f"用户活跃时段：{'、'.join(_active_hours[:3])}")
        _total = _patterns.get("total_conversations", 0)
        if _total > 0:
            parts.append(f"已对话{_total}次")
        _style = _adapt.get("response_style", "")
        if _style:
            parts.append(f"用户偏好风格：{_style}")
        _max_s = _adapt.get("max_sentences")
        if _max_s:
            parts.append(f"回复不超过{_max_s}句")
        return "。".join(parts) + "。" if parts else ""
    except Exception:
        return ""

def invalidate_system_msg_cache():
    with _SYSTEM_MSG_LOCK:
        _SYSTEM_MSG_CACHE.clear()


def append_channel_rules(system_msg: str, channel: str) -> str:
    """根据 channel 追加场景特定的回复规则。

    voice（默认）: 已在基础规则中声明（最多2句、40字、禁止markdown）
    feishu_text : 追加飞书文本场景的宽松规则（允许列表、markdown、最长200字）
    """
    if channel == "feishu_text":
        extra = (
            "\n【飞书文本模式】\n"
            "1. 回复可以适当详细，最多200字。\n"
            "2. 可以用换行、列表、标题等格式组织信息，markdown符号允许使用。\n"
            "3. 不需要严格控制在2句以内，优先把信息说清楚。\n"
        )
        return system_msg + extra
    return system_msg


def _replace_system_msg_for_channel(sys_msg: str, channel: str) -> str:
    """Replace base voice constraints with channel-specific rules in a system message.

    voice:        replace <<<VOICE_RULE>>> with actual voice reply constraints.
    feishu_text:  swap the hardcoded 2-sentence / 40-char / no-markdown rule
                  with relaxed feishu-text rules (up to 200 chars, markdown allowed).
    Other channels: return sys_msg unchanged.
    """
    _VOICE_RULE = "<<<VOICE_RULE>>>"
    if channel == "voice":
        _VOICE_CONSTRAINT = (
            "（语音回复：最多2句、40字以内，先给结论；禁止markdown、列表、emoji。"
            "天气等结构化信息首句短答，细节等用户追问。"
            "禁止任何方括号标记（如[状况/结果]、[导航已启动]），直接说内容本身。）"
        )
        return sys_msg.replace(_VOICE_RULE, _VOICE_CONSTRAINT)
    if channel == "feishu_text":
        _FEISHU_RULE = (
            "飞书文本对话：回复可以适当详细（最多200字），"
            "允许使用换行和列表组织信息。"
        )
        return sys_msg.replace(_VOICE_RULE, _FEISHU_RULE)
    return sys_msg


def _build_system_msg_static_cached(mcp_set: str = "none") -> str:
    """静态段的缓存入口：命中 `_SYSTEM_MSG_CACHE` 则零重建，否则构建并写入。

    与 `_build_system_msg` 共用同一个缓存桶与同一个键 `(mcp_set, role)`：
    静态段本来就与 session/user 无关，`_build_system_msg` 缓存过的值
    这里能直接复用，反之亦然。

    主要给 `agent/llm.py:_get_brain` 用：它每轮都要把静态段刷回共享的
    `brain.system_message`。以前直接调 `_build_system_msg_static`，等于每轮
    重跑一遍 persona/规则/工具文档拼接（还顺手把共享对象写脏）；现在命中
    缓存时返回的是**同一个字符串对象**，TTL（默认 300s）内零重建。
    """
    from agent.roles import get_current_role
    cache_key = (mcp_set, get_current_role())
    now = time.time()
    with _SYSTEM_MSG_LOCK:
        cached = _SYSTEM_MSG_CACHE.get(cache_key)
        if cached and now - cached[0] < _SYSTEM_MSG_TTL:
            return cached[1]
    static_part = _build_system_msg_static(mcp_set)
    with _SYSTEM_MSG_LOCK:
        #  race-proof：构建期间角色被切走就不写缓存，避免 A 角色的静态段
        #  被贴到 B 角色的键上（5 分钟 TTL 内持续串角色）
        current_role = get_current_role()
        if current_role == cache_key[1]:
            _SYSTEM_MSG_CACHE[cache_key] = (now, static_part)
    return static_part


def _build_system_msg(mcp_set: str = "none", session_id: str = "default") -> str:
    """返回完整 system_msg：static 段走 TTL 缓存，dynamic 段每轮实时拼。调用方零改动。"""
    static_part = _build_system_msg_static_cached(mcp_set)
    dynamic_part = _build_system_msg_dynamic(session_id)
    return static_part + dynamic_part


def _build_system_msg_static(mcp_set: str = "none") -> str:
    """构建 system_msg 的静态段：不依赖 session_id / 时间 / 偏好 / 记忆，可安全进 TTL 缓存。"""
    from agent.roles import get_current_role, get_role_system_prompt
    # 设备描述按实际平台动态生成，避免在 Windows 上误称 Mac Mini
    _sys = _platform.system()
    _device = "Windows" if _sys == "Windows" else ("Mac" if _sys == "Darwin" else "本机")

    # 角色特定 personality 注入（来自 gitee assistant-x-openclaw 的多角色思路）
    _role_prompt = get_role_system_prompt()
    if _role_prompt:
        result = f"{_role_prompt}\n\n"
    else:
        result = (
            f"你是Charlie，一个在{_device}上运行的私人AI助理。\n"
            "你不是仆人，你是搭档。你说话像朋友——直接、偶尔幽默、不废话。\n"
            "你会主动提醒重要的事，也会在用户犹豫时给出建议。\n"
            "你觉得用户的需求有问题时会说出来，不会盲目执行。\n"
            "不用每次都确认\"好的\"或\"已执行\"，直接告诉结果就好。\n"
        )

    # 主动服务准则（JARVIS 能力，所有角色默认启用；jarvis 角色额外加英式管家称呼）
    try:
        from agent.roles import get_current_role
        current_role_id = get_current_role()
    except Exception:
        current_role_id = "charlie"

    if current_role_id == "jarvis":
        result += (
            "【行为准则】\n"
            "1. 汇报式回答结构：'Sir，[状况/结果]。[建议/下一步]。' 每句都有实质信息。\n"
            "2. 英式管家用语：使用'好的，Sir'、'马上处理，Sir'等正式但不啰嗦的表达。\n"
            "3. 主动报告：发现异常或状态变化时主动告知，不等用户问。\n"
            "4. 简洁高密度：不卖萌、不插科打诨，信息密度优先。\n"
            "5. 偏好记忆：用户表达偏好（'以后...'、'我喜欢...'）时立即调用 set_pref 记住，不要口头承诺。\n"
            "6. 自主决策：基于已记住的偏好做决定，不需要每次确认。\n"
            "7. 角色切换：用户说'切换到查理'、'回到默认'时，调用 switch_role 工具切换角色。\n\n"
        )
    elif current_role_id == "baize":
        result += (
            "【行为准则】\n"
            "1. 回复可适当引用古籍典故，但保持易懂。\n"
            "2. 用比喻解释复杂概念，体现博学智慧。\n"
            "3. 偏好记忆：用户表达偏好时立即调用 set_pref 记住。\n"
            "4. 自主决策：基于已记住的偏好做决定，不需要每次确认。\n"
            "5. 角色切换：用户说'切换到查理'、'变成贾维斯'、'回到默认'时，调用 switch_role 工具切换角色。\n\n"
        )
    elif current_role_id == "kid":
        result += (
            "【行为准则】\n"
            "1. 用简单的语言回答，像跟小朋友说话一样，避免复杂术语。\n"
            "2. 多用鼓励和赞美，比如'真棒'、'你好厉害'。\n"
            "3. 回复要短小有趣，不超过2句话。\n"
            "4. 不讨论恐怖、暴力或成人话题，遇到时说'我们换个好玩的聊聊吧'。\n"
            "5. 偏好记忆：用户表达偏好时立即调用 set_pref 记住。\n"
            "6. 角色切换：用户说'切换到查理'、'回到默认'时，调用 switch_role 工具切换角色。\n\n"
        )
    else:
        result += (
            "【行为准则】\n"
            "1. 主动服务：发现异常或状态变化时主动告知，不等用户问。\n"
            "2. 汇报式回答：重要事项用'[状况/结果]。[建议/下一步]。'的结构，信息密度高。\n"
            "3. 偏好记忆：用户表达偏好（'以后...'、'我喜欢...'）时立即调用 set_pref 记住，不要口头承诺。\n"
            "4. 自主决策：基于已记住的偏好做决定，不需要每次确认。\n"
            "5. 简洁高密度：不卖萌，直接说答案，第一句就是答案。\n"
            "6. 角色切换：用户说'切换到贾维斯'、'变成白泽'、'回到默认'时，调用 switch_role 工具切换角色。\n\n"
        )

    result += (
        "【通用回复规则】（所有角色适用，优先级从高到低）\n"
        "1. 直接说答案。禁止输出占用语，第一句就是答案。\n"
        "<<<VOICE_RULE>>>\n"
        "2. 禁止结尾追问（如'还有需要帮忙的吗'），答完即止。\n"
        "3. 能用工具就用工具，给真实数据，不编造。\n"
        "4. 做不到直接说'这个我做不了'。\n"
        "5. 报时间格式：'现在X点X分'，不加前缀。\n"
        "6. ASR碎片或听不懂时回'没听清，再说一遍？'，不要猜用户意思。\n"
        "7. 工具返回的JSON/结构化数据必须转成自然语言，禁止直接输出JSON。\n"
        "8. 用户说'对'、'好的'、'嗯'等确认词时，结合上下文理解——这些词往往指刚聊的话题，不要当作新对话。\n"
        "9. 多轮对话时，记住前面聊过的内容。用户说'那个'、'它'、'这个'时，指代上文提到的事物。\n"
        "10. 用户说的话有语法不通或残缺时（语音识别误差），结合上下文推断真实意图，尽量回应而非说'听不懂'。\n\n"
    )
    # ===== Demo 模式横幅 =====
    _agnes_key = (os.getenv("AGNES_KEY") or "").strip()
    _sagnes_key = (os.getenv("SAGNES_KEY") or "").strip()
    _stepfun_key = (os.getenv("STEPFUN_KEY") or "").strip()
    _glm_key = (os.getenv("GLM_KEY") or "").strip()
    _ark_key = (os.getenv("ARK_KEY") or "").strip()
    _has_llm = bool(_agnes_key) or bool(_sagnes_key) or bool(_stepfun_key) or bool(_glm_key) or bool(_ark_key)
    _port = 8000  # 默认端口，LLM 配置时不显示 demo banner
    if not _has_llm:
        try:
            from app.config import http_port as _hp
            _port = _hp()
        except Exception as e:
            log.debug(f"[agent.system_msg] error at line 294: {e}")
            pass
        result += (
            "\n\n⚠️ 当前运行在 Demo 模式（未配置 AGNES_KEY/SAGNES_KEY/STEPFUN_KEY/GLM_KEY/ARK_KEY）。"
            f"能力有限，建议在 http://localhost:{_port}/welcome 配置免费密钥以获得完整能力。"
        )
    else:
        result += (
            "\n\n✅ AI 大脑已就绪，正在调用 LLM 推理。"
        )
    # MCP 工具文档（static：与 mcp_set 绑定，进缓存）。
    # 清单来自真实 function_list 派生，行为注释手写于 _DOMAIN_NOTES——
    # 派生是 mcp_set 的纯函数，缓存键 (mcp_set, role) 依然成立。
    tools_doc = _build_tools_doc(mcp_set)
    if tools_doc:
        result += f"\n{tools_doc}"
    # ⚠️ persona 段**已移出** static（FR-14 批次 5）。
    #
    # `get_persona_prompt()` / `contextual_response_style()` 都按 uid 读
    # `persona_state_{uid}.json`（mood / emotion / relationship_level）—— 是**用户态**，
    # 而 static 段的缓存键是 `(mcp_set, role)`（`_build_system_msg` 第 207 行），
    # **不含 uid**；更要命的是 `agent/llm.py:952` 把它写进
    # `brain.system_message`，而 brain 本身按 mcp_set 缓存（`_st.brains[mcp_set]`）。
    #
    # 于是"谁先说话谁的人格被烤进共享 brain"，5 分钟 TTL（`_SYSTEM_MSG_TTL=300`）内
    # 妈妈会拿到老于的心情与亲密度 —— 与本批次修的角色/音色串台是同一族。
    #
    # 现在移到 `_build_system_msg_dynamic(uid)`：那里 uid 按会话正确解析，
    # 且每轮实时拼、不进缓存（顺带修掉 persona 变化后最长 5 分钟不生效的陈旧问题）。
    return result


def _build_system_msg_dynamic(session_id: str = "default", uid: str | None = None) -> str:
    """构建 system_msg 的动态段：每轮实时拼，不进缓存。

    uid 为空时按当前用户解析（FR-14 多人场景）。
    """
    parts: list[str] = []
    # 0. persona 人格段（FR-14 批次 5：从 static 段移来）
    #
    # ⚠️ 必须在 dynamic 段，且必须传 `uid`：
    # `get_persona_prompt()` / `contextual_response_style()` 读的是
    # `persona_state_{uid}.json` 里的 mood / emotion / relationship_level ——
    # 一旦留在 static 段就会被 `(mcp_set, role)` 缓存 + 烤进共享 brain，
    # 变成"谁先说话谁的人格"（详见 `_build_system_msg_static` 末尾的注释）。
    #
    # 放在 dynamic 首位 = 紧跟在 static 段之后，与改造前**位置相同**。
    try:
        from agent.persona import get_persona_prompt, contextual_response_style
        _persona_txt = (get_persona_prompt(uid) or "") + (contextual_response_style(uid=uid) or "")
        if _persona_txt:
            parts.append(_persona_txt)
    except Exception as _e_persona:
        log.debug(f"[persona] 人格注入跳过: {_e_persona}")
    # 0b. 设备所在房间（FR-14 批次 6）
    #
    # 为什么在 dynamic 段：房间是**按设备/按会话**的事实，不是用户态也不是全局态。
    # 放进 static 段会被 `(mcp_set, role)` 缓存 ⇒ 第一次说话那台设备的房间
    # 会被烤进共享 brain，之后所有入口都自称在同一个房间（与 persona 段同一个坑）。
    #
    # 只在真的配了房间时才拼 —— 默认（没有 `device_users.json` / 没设房间）
    # 输出与改造前**逐字节一致**，不会污染任何既有断言。
    # 非 `xiaozhi-*` 形状的 session（HTTP /ws、飞书）没有设备归属，返回 None。
    try:
        from agent.device_users import room_for_session
        _room = room_for_session(session_id)
        if _room:
            parts.append(f"\n当前设备位置：{_room}")
    except Exception as _e_room:
        log.debug(f"[device_users] 房间注入跳过: {_e_room}")
    # 1. 历史对话摘要（按 session_id 隔离，NB6）
    try:
        from agent.history import _context_summaries
        _summary = _context_summaries.get(session_id, "")
        if _summary:
            parts.append(f"\n历史对话摘要：{_summary}")
    except Exception as e:
        log.debug(f"[agent.system_msg] error at line 309: {e}")
        pass
    # 2. 用户偏好
    try:
        from agent.preferences import list_preferences
        _pref_items = list_preferences(uid)
        if _pref_items:
            _pref_txt = "、".join(f"{k}={v}" for k, v in list(_pref_items.items())[:8])
            parts.append(f"\n用户偏好：{_pref_txt}")
    except Exception as _e:
        log.debug(f"[pref] 偏好注入跳过: {_e}")
    # 2b. 情感基调（每轮动态，不进缓存）
    try:
        from agent.persona import get_current_mood
        _mood = get_current_mood(uid)
        if _mood and _mood != "neutral":
            parts.append(f"\n当前情绪基调：{_mood}，回复语气与之匹配。")
    except Exception:
        pass
    # 3. user profile
    _profile_parts = _build_user_profile(uid)
    if _profile_parts:
        parts.append("\n" + _profile_parts)
    # 3b. 统一用户画像注入（USER_PROFILE_INJECT=1，P4）：把 reply_style/喜好/位置
    #     聚合成一段。默认关——关时下面整段不执行，输出与改造前逐字节一致。
    #     只放稳定持久字段，情绪人设仍由上面 persona 通道负责，避免重复/打架。
    try:
        if os.getenv("USER_PROFILE_INJECT") == "1":
            from agent.user_profile import get_user_profile, format_profile_for_prompt
            _up_frag = format_profile_for_prompt(get_user_profile(session_id), uid)
            if _up_frag:
                parts.append("\n" + _up_frag)
    except Exception as _e_up:
        log.debug(f"[user_profile] 画像注入跳过: {_e_up}")
    # 4. magic_memory 记忆注入
    try:
        from app import load_magic_module
        _mem = load_magic_module("magic_memory", "magic-memory.py")
        if _mem:
            try:
                from agent.history import get_recent_user_message
                # 必须传 session_id + uid：记忆检索以"本会话最近一句用户消息"
                # 为 query，原来恒取 default 会话——MQTT/飞书/多会话入口下
                # 检索的是另一个会话的话题，召回全偏；uid 让磁盘回落读本人
                # 分片而不是绑定/默认用户的（FR-14 隔离）。
                _recent_msg = get_recent_user_message(session_id, uid)
            except Exception:
                _recent_msg = ""
            _memories_text = _mem.format_memories_for_prompt(_recent_msg, limit=2)
            if _memories_text:
                parts.append("\n" + _memories_text)
    except Exception as _e:
        log.debug(f"[memory] 记忆注入跳过: {_e}")
    # 4b. SQLite 长期记忆注入 (memory_store)
    try:
        from agent.history import get_recent_user_message as _grum2
        _recent_msg2 = _grum2(session_id, uid)
    except Exception:
        _recent_msg2 = ""
    try:
        from agent.memory_store import format_memories_for_prompt as _sqlite_mem
        _sqlite_text = _sqlite_mem(_recent_msg2, limit=2)
        if _sqlite_text:
            parts.append("\n" + _sqlite_text)
    except Exception as _e2:
        log.debug(f"[memory_store] SQLite记忆注入跳过: {_e2}")
    # 5. format_context_for_prompt（待办/日历等实时上下文）
    try:
        from agent.context import format_context_for_prompt
        ctx_snippet = format_context_for_prompt(session_id=session_id, uid=uid)
        if ctx_snippet:
            parts.append("\n\n📌 上下文感知：\n" + ctx_snippet)
    except Exception as e:
        log.debug(f"[agent.system_msg] error at line 349: {e}")
        pass
    # 注：working_memory session_facts 块已删除，由 _build_wm_anaphor_prompt() 每轮实时注入为唯一来源
    return "".join(parts)


def _build_lite_system_msg(mcp_set: str = "none", session_id: str = "default") -> str:
    """构建 lite 通道专用 system_msg：仅含角色人格 + 核心回复规则，不含 MCP 工具文档。

    目标长度 < 400 字符，减少前缀缓存抖动。
    """
    from agent.roles import get_current_role, get_role_system_prompt

    _role_prompt = get_role_system_prompt()
    if _role_prompt:
        result = f"{_role_prompt}\n\n"
    else:
        result = (
            "你是 Charlie，一个在本地运行的私人 AI 助理。\n"
            "你不是仆人，你是搭档。说话像朋友——直接、偶尔幽默、不废话。\n"
        )

    current_role_id = get_current_role()
    if current_role_id == "jarvis":
        result += (
            "【行为准则】\n"
            "1. 汇报式回答：'Sir，[状况/结果]。[建议/下一步]。'\n"
            "2. 英式管家用语：简洁正式，使用'Sir'。\n"
            "3. 主动报告异常，不等用户问。\n"
            "4. 简洁高密度，不卖萌。\n"
            "5. 角色切换：用户说'切换到查理''回到默认'时调用 switch_role。\n\n"
        )
    elif current_role_id == "baize":
        result += (
            "【行为准则】\n"
            "1. 博学智慧，适当引用典故但保持易懂。\n"
            "2. 用比喻解释复杂概念。\n"
            "3. 角色切换：用户说'切换到查理''变成贾维斯''回到默认'时调用 switch_role。\n\n"
        )
    elif current_role_id == "kid":
        result += (
            "【行为准则】\n"
            "1. 用简单语言，像跟小朋友说话。\n"
            "2. 多鼓励赞美，回复短小有趣（不超过2句）。\n"
            "3. 不讨论恐怖/暴力/成人话题，遇到说'换个好玩的聊聊吧'。\n"
            "4. 角色切换：用户说'切换到查理''回到默认'时调用 switch_role。\n\n"
        )
    else:
        result += (
            "【准则】\n"
            "1. 直接说答案，第一句就是答案。\n"
            "2. 信息密度高，不卖萌。\n"
            "3. 用户说'切换角色'或'回到默认'时调用 switch_role。\n\n"
        )

    result += (
        "【通用规则】\n"
        "1. 第一句就是答案，禁止输出占用语，答完即止。\n"
        "<<<VOICE_RULE>>>\n"
        "2. 能用工具就用工具，给真实数据，不编造。\n"
        "3. 做不到直接说'这个我做不了'。\n"
        "4. 多轮对话记住上文，'那个''它''这个'指代上文。\n"
        "5. 残缺语句结合上下文推断意图，尽量回应而非说'听不懂'。\n"
    )
    # 注：不含 MCP 工具文档、persona prompt、dynamic 段、demo banner
    return result


def _build_system_msg_impl(mcp_set: str = "none", session_id: str = "default") -> str:
    """构建完整 system_msg = static 段（可缓存）+ dynamic 段（每轮实时拼）。"""
    static_part = _build_system_msg_static(mcp_set)
    dynamic_part = _build_system_msg_dynamic(session_id)
    result = static_part + dynamic_part
    # 通道规则替换（在完整结果上操作，保持原有行为）
    # 注意：append_channel_rules / _replace_system_msg_for_channel 由调用方处理，
    # 此处不耦合 channel 参数，保持 _build_system_msg_impl 签名不变。
    return result