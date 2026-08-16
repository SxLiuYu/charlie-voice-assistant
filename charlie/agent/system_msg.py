"""系统提示词构建"""
import os
import datetime
import logging
import platform as _platform
from typing import Any

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_current_user_id = os.environ.get("CHARLIE_USER_ID", "default")

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
    pass

def _build_system_msg(mcp_set: str = "none") -> str:
    from agent.preferences import list_preferences
    from agent.history import _context_summaries
    now = datetime.datetime.now()
    # 设备描述按实际平台动态生成，避免在 Windows 上误称 Mac Mini
    _sys = _platform.system()
    _device = "Windows" if _sys == "Windows" else ("Mac" if _sys == "Darwin" else "本机")
    result = (
        f"你是Charlie，一个在{_device}上运行的私人AI助理。\n"
        "你不是仆人，你是搭档。你说话像朋友——直接、偶尔幽默、不废话。\n"
        "你会主动提醒重要的事，也会在用户犹豫时给出建议。\n"
        "你觉得用户的需求有问题时会说出来，不会盲目执行。\n"
        "不用每次都确认\"好的\"或\"已执行\"，直接告诉结果就好。\n\n"
        "回复规则（优先级从高到低）：\n"
        "1. 直接说答案。禁止输出占用语，第一句就是答案。\n"
        "2. 简洁。能1句说完不用2句。但如果用户问的是开放性问题，可以多说几句。\n"
        "3. 能用工具就用工具，给真实数据，不编造。\n"
        "4. 做不到直接说\"这个我做不了\"。\n"
        "5. 报时间格式：\"现在X点X分\"，不加前缀。\n"
        "6. ASR碎片或听不懂时回\"没听清，再说一遍？\"，不要猜用户意思。\n"
        "7. 工具返回的JSON/结构化数据必须转成自然语言，禁止直接输出JSON。\n"
        "8. 用户说\"对\"\"好的\"\"嗯\"等确认词时，结合上下文理解——这些词往往指刚聊的话题，不要当作新对话。\n"
        "9. 多轮对话时，记住前面聊过的内容。用户说\"那个\"\"它\"\"这个\"时，指代上文提到的事物。\n"
        "10. 用户说的话有语法不通或残缺时(语音识别误差)，结合上下文推断真实意图，尽量回应而非说\"听不懂\"。\n"
    )
    # ===== Demo 模式横幅 =====
    _ark_key = (os.getenv("ARK_KEY") or "").strip()
    _glm_key = (os.getenv("GLM_KEY") or "").strip()
    if (not _ark_key or _ark_key.startswith("你的")) and (not _glm_key or _glm_key.startswith("你的")):
        _port = 8000
        try:
            from app.config import http_port as _hp
            _port = _hp()
        except Exception:
            pass
        result += (
            "\n\n⚠️ 当前运行在 Demo 模式（本地 Ollama 模型，未配置 ARK_KEY/GLM_KEY）。"
            f"能力有限，建议在 http://localhost:{_port}/welcome 配置免费 GLM 密钥以获得完整能力。"
        )
    result += f"\n当前时间：{now.strftime('%Y年%m月%d日 %H:%M')}。"
    try:
        from app import reminders as _app_reminders
        _today_pending = 0
        _today_texts = []
        for _r in _app_reminders._load_reminders():
            if isinstance(_r, dict) and not _r.get("done"):
                _due = str(_r.get("due", ""))[:10]
                if _due == now.strftime("%Y-%m-%d"):
                    _today_pending += 1
                    _today_texts.append(str(_r.get("text", ""))[:40])
        if _today_pending > 0:
            result += f"\n今日有{_today_pending}项待办：" + "、".join(_today_texts[:5]) + "。"
    except Exception as _e:
        log.debug(f"[reminder] 待办注入跳过: {_e}")
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
        import importlib.util as _iu
        _spec_mem = _iu.spec_from_file_location("magic_memory", os.path.join(PROJECT_DIR, "magic-memory.py"))
        if _spec_mem and _spec_mem.loader:
            _mem = _iu.module_from_spec(_spec_mem)
            _spec_mem.loader.exec_module(_mem)
        _memories_text = _mem.format_memories_for_prompt(_current_input, limit=2)
        if _memories_text:
            result += "\n" + _memories_text
    except Exception as _e:
        log.debug(f"[memory] 记忆注入跳过: {_e}")
    tools_doc = _MCP_SYSTEM_PROMPTS.get(mcp_set, "")
    if tools_doc:
        result += f"\n{tools_doc}"
    return result