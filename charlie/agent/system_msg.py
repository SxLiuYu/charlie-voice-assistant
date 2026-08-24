"""系统提示词构建"""
import os
import datetime
import logging
import platform as _platform
import time
import threading
from typing import Any

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_current_user_id = os.environ.get("CHARLIE_USER_ID", "default")

# system msg TTL 缓存
_SYSTEM_MSG_CACHE = {}  # key: (mcp_set, role) → (timestamp, msg)
_SYSTEM_MSG_TTL = int(os.getenv("SYSTEM_MSG_TTL", "300"))  # 5 分钟
_SYSTEM_MSG_LOCK = threading.Lock()

_MCP_SYSTEM_PROMPTS = {
    "amap-maps": (
        "可用工具：get_detailed_weather(城市)查详细天气+穿衣建议，get_location()获取当前位置，"
        "get_news(主题,条数)查新闻，get_current_time()获取当前时间，translate(文本,目标语言)翻译。"
        "调用工具获取真实数据，不要编造天气信息。"
    ),
    "magic-music": (
        "可用工具：play_music(歌名或歌手名)搜索并播放音乐，search_music(关键词)搜索歌曲，"
        "play_random_music()随机播放一首，stop_music()停止播放，"
        "list_playlists()列出歌单，play_playlist()播放歌单或每日推荐。"
        "\\n播放音乐时不要说话，直接调用工具，不要回复任何文字。"
    ),
    "magic-reminder": (
        "可用工具：add_reminder(文本, 时间)设提醒，set_timer(分钟数, 提示语)倒计时，"
        "get_calendar_today()读取Apple日历，schedule_task(名称,时间,动作)创建定时任务，"
        "list_alarms()列出所有闹钟和定时任务。"
    ),
    "magic-notes": (
        "可用工具：save_note(标题, 内容)保存备忘录，list_notes()列出所有备忘录，"
        "add_shopping_item(商品,数量)添加购物清单，list_shopping_items()查看购物清单，"
        "clear_shopping_list()清空购物清单。"
    ),
    "magic-system": (
        "可用工具：set_volume(0-100)调音量，set_speech_speed(slow/normal/fast)调语速，"
        "system_status()查CPU/内存/磁盘/运行时间。"
    ),
    "magic-info": (
        "可用工具：get_current_time()获取当前时间，get_detailed_weather(城市)查天气+穿衣建议，"
        "get_news(主题, 条数)查新闻，get_location()获取当前位置，"
        "translate(文本, 目标语言)翻译，calculate(算式)计算或换算，"
        "run_code(代码)执行Python代码进行数学计算、数据处理等。"
    ),
    "magic-life": (
        "可用工具：open_lifestyle_app(intent)打开外卖/购物/打车等App，"
        "search_charging_stations(城市)查充电桩，control_tesla_ac(on/off)控特斯拉空调，"
        "leaving_home()出门场景。"
    ),
    "magic-feishu": (
        "可用工具：search_docs(关键词)搜索飞书云端文档，send_message(用户ID,消息)发送飞书消息，"
        "list_messages(群聊ID)读取飞书最近的聊天消息，get_calendar()查看飞书日历日程。"
    ),
    "magic-douyin": (
        "可用工具：search_videos(关键词)搜索抖音视频，get_trending()获取抖音热搜，get_video_info(URL)获取视频信息。"
    ),
    "magic-taobao": (
        "可用工具：search_products(关键词)搜索淘宝商品，search_jd(关键词)搜索京东商品，compare_price(商品名)跨平台比价。"
    ),
    "magic-recipe": (
        "可用工具：get_recipe(菜名)查菜谱做法(本地346道菜+AI兜底)，"
        "search_recipe(关键词)按菜名或食材搜索，list_recipes(类别)列出菜谱(可按凉菜/热菜/汤羹/主食等筛选)，"
        "random_recipe()随机推荐一道，recommend_recipe(食材,菜系)按现有食材推荐，"
        "recommend_daily()今日个性化推荐(基于口味画像)，recommend_by_context(场景)按场景推荐(如想吃辣的/想吃肉)，"
        "add_recipe(菜名,食材,步骤,难度,时间)添加菜谱，generate_recipe(菜名,食材)AI生成菜谱。"
        "调用工具拿真实菜谱，不要编造做法。"
    ),
    "magic-apps": (
        "可用工具：打开微信/支付宝/抖音/今日头条/美团/淘宝/京东/拼多多/大众点评/猫眼/大麦/咸鱼/飞书的网页版。"
    ),
    "magic-scenes": (
        "可用工具：goodnight()晚安模式，good_morning()早安模式，movie_time()电影模式，leaving_home()出门模式。"
    ),
    "magic-browser": (
        "可用工具：browse_page(网址)打开网页，read_page()读取当前页面，"
        "search_web(搜索词,站点)通过浏览器搜索，click_element(选择器)点击，"
        "fill_form(选择器,文字)填写表单，screenshot()截取页面截图。"
    ),
    "magic-evolution": (
        "可用工具：learn_from_history()从对话历史学习，self_optimize()自动优化，"
        "suggest_preferences()建议个性化偏好，evolution_status()查看自进化状态。"
    ),
    "baize-skills": (
        "可用工具：web_search(关键词)搜索互联网，web_search_free(关键词)免费搜索，"
        "shopping_search(商品)购物推荐，deep_research(主题)深度研究。"
    ),
    "filesystem": (
        "可用工具：读写文件。"
    ),
    "ac-control": (
        "AC空调控制：必须调用 ac_control 工具，禁止编造结果。"
    ),
    "mimo-vision": (
        "你有视觉能力：可以截图或分析用户提供的图片。"
    ),
    "magic-preferences": (
        "可用工具：set_pref(偏好名, 值)记住偏好，get_pref(偏好名)查询偏好，"
        "forget_pref(偏好名)删除偏好，list_prefs()列出所有偏好。"
        "当用户表达偏好（如'以后...''我喜欢...''别再...''我习惯...'）时立即调用 set_pref 记住。"
    ),
}

def _build_user_profile() -> str:
    try:
        import json as _json
        uid = os.environ.get("CHARLIE_USER_ID", _current_user_id)
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

    feishu_text: swap the hardcoded 2-sentence / 40-char / no-markdown rule
    with relaxed feishu-text rules (up to 200 chars, markdown allowed).
    All other channels: return sys_msg unchanged.
    """
    if channel != "feishu_text":
        return sys_msg
    _VOICE_RULE = (
        "语音回复硬限制：最多2句、40字以内。"
        "禁止列表/标题/markdown符号，只输出纯口语句子。"
    )
    _FEISHU_RULE = (
        "飞书文本对话：回复可以适当详细（最多200字），"
        "允许使用换行和列表组织信息。"
    )
    return sys_msg.replace(_VOICE_RULE, _FEISHU_RULE)


def _build_system_msg(mcp_set: str = "none") -> str:
    from agent.roles import get_current_role
    cache_key = (mcp_set, get_current_role())
    now = time.time()
    with _SYSTEM_MSG_LOCK:
        cached = _SYSTEM_MSG_CACHE.get(cache_key)
        if cached and now - cached[0] < _SYSTEM_MSG_TTL:
            return cached[1]
    result = _build_system_msg_impl(mcp_set)
    with _SYSTEM_MSG_LOCK:
        # 构建期间角色可能已切换（impl 内部读取的是新角色的 prompt），
        # 写回前校验，避免旧 key 存新角色内容导致 TTL 内 prompt 错配
        current_role = get_current_role()
        if current_role == cache_key[1]:
            _SYSTEM_MSG_CACHE[cache_key] = (now, result)
    return result


def _build_system_msg_impl(mcp_set: str = "none") -> str:
    from agent.preferences import list_preferences
    from agent.history import _context_summaries
    from agent.roles import get_current_role, get_role_system_prompt
    now = datetime.datetime.now()
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
        "2. 语音回复硬限制：最多2句、40字以内。禁止列表/标题/markdown符号，只输出纯口语句子。\n"
        "2.1 禁止结尾追问（如'还有需要帮忙的吗'），答完即止。\n"
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
        except Exception:
            pass
        result += (
            "\n\n⚠️ 当前运行在 Demo 模式（未配置 AGNES_KEY/SAGNES_KEY/STEPFUN_KEY/GLM_KEY/ARK_KEY）。"
            f"能力有限，建议在 http://localhost:{_port}/welcome 配置免费密钥以获得完整能力。"
        )
    else:
        result += (
            "\n\n✅ AI 大脑已就绪，正在调用 LLM 推理。"
        )
    try:
        _uid_now = os.environ.get("CHARLIE_USER_ID", _current_user_id)
        _summary = _context_summaries.get(_uid_now or "default", "")
        if _summary:
            result += f"\n历史对话摘要：{_summary}"
    except Exception:
        pass
    try:
        _pref_items = list_preferences()
        if _pref_items:
            _pref_txt = "、".join(f"{k}={v}" for k, v in list(_pref_items.items())[:8])
            result += f"\n用户偏好：{_pref_txt}"
    except Exception as _e:
        log.debug(f"[pref] 偏好注入跳过: {_e}")
    _profile_parts = _build_user_profile()
    if _profile_parts:
        result += "\n" + _profile_parts
    try:
        from app import load_magic_module
        _mem = load_magic_module("magic_memory", "magic-memory.py")
        if _mem:
            try:
                from agent.history import get_recent_user_message
                _recent_msg = get_recent_user_message()
            except Exception:
                _recent_msg = ""
            _memories_text = _mem.format_memories_for_prompt(_recent_msg, limit=2)
            if _memories_text:
                result += "\n" + _memories_text
    except Exception as _e:
        log.debug(f"[memory] 记忆注入跳过: {_e}")
    tools_doc = _MCP_SYSTEM_PROMPTS.get(mcp_set, "")
    if tools_doc:
        result += f"\n{tools_doc}"
    try:
        from agent.persona import get_persona_prompt, contextual_response_style
        result += get_persona_prompt()
        result += contextual_response_style()
    except Exception:
        pass
    try:
        from agent.context import format_context_for_prompt
        ctx_snippet = format_context_for_prompt()
        if ctx_snippet:
            result += "\n\n📌 上下文感知：\n" + ctx_snippet
    except Exception:
        pass
    try:
        from agent.working_memory import get as get_working_memory
        _wm = get_working_memory()
        if _wm.get("turn_count", 0) > 2 and _wm.get("session_facts"):
            _facts = "、".join(f"{k}={v}" for k, v in list(_wm["session_facts"].items())[:5])
            result += f"\n\n🧠 工作记忆：{_facts}"
    except Exception:
        pass
    return result