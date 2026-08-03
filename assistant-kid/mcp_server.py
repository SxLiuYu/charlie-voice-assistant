"""迷你MCP服务器 - 模拟Charlie的部分能力"""
from mcp.server.fastmcp import FastMCP
from datetime import datetime
import os
import requests
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

mcp = FastMCP("magic-phone")
ALIYUN = os.getenv("ALIYUN_API_KEY", "")
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def aliyun_chat(messages, temperature=0.3):
    r = requests.post(DASHSCOPE, headers={"Authorization": f"Bearer {ALIYUN}", "Content-Type": "application/json"},
        json={"model": "qwen-max", "messages": messages, "temperature": temperature, "stream": False}, timeout=60)
    return r.json()["choices"][0]["message"]["content"]

@mcp.tool()
def get_current_time() -> str:
    """获取当前时间、日期和星期"""
    now = datetime.now()
    w = ['一','二','三','四','五','六','日'][now.weekday()]
    return f"现在是 {now.strftime('%Y年%m月%d日 %H:%M:%S')}，星期{w}"

@mcp.tool()
def _check_key(key: str, name: str):
    if not key:
        raise ValueError(f"未配置{name}API密钥")
    return key


@mcp.tool()
def search_charging_stations(city: str = "北京", count: int = 3) -> str:
    """搜索附近的充电桩(真实高德数据)。参数: city-城市, count-数量"""
    import os, requests
    AMAP = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    try:
        r = requests.get("https://restapi.amap.com/v3/place/text",
            params={"keywords":"充电桩","city":city,"citylimit":"true","offset":min(count*3,20),
                    "page":1,"key":AMAP,"extensions":"base"}, timeout=10).json()
        pois = (r.get("pois") or [])[:count]
        if not pois: return f"在{city}没找到充电桩"
        lines = [f"在{city}找到{len(pois)}个真实充电桩(高德数据):"]
        for p in pois:
            line = f"• {p.get('name','?')} 地址:{p.get('address','') or '未知'}"
            tel = p.get('tel','')
            if tel and tel != '[]': line += f" 电话:{tel}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"充电桩查询失败:{e}。请用高德地图App查看。"

@mcp.tool()
def control_tesla_ac(action: str = "on", temperature: int = 22) -> str:
    """控制特斯拉空调。action: on/off, temperature: 温度"""
    return f"特斯拉空调已{'开启' if action=='on' else '关闭'}，设定温度{temperature}度。"

# ===== 新增功能 =====

@mcp.tool()
def add_reminder(text: str, time: str = "", repeat: str = "") -> str:
    """添加提醒。text=提醒内容, time=时间(如'下午3点'/'30分钟后'/'每天8点'), repeat=重复类型(daily/weekly/weekdays, 留空=一次性)

    repeat 值:
    - 留空: 一次性提醒
    - daily: 每天重复
    - weekly: 每周重复
    - weekdays: 工作日(周一到周五)重复
    """
    from utils import parse_time_str
    from app.reminders import append_reminder

    due = parse_time_str(time) if time else None
    repeat_clean = repeat if repeat in ("daily", "weekly", "weekdays") else ""
    # 如果用户说"每天"但没传 repeat, 从 time 提取
    if not repeat_clean and time:
        if "每天" in time or "每日" in time:
            repeat_clean = "daily"
        elif "每周" in time:
            repeat_clean = "weekly"
        elif "工作日" in time:
            repeat_clean = "weekdays"
    # 去掉 time 中的重复词, 只留时间部分
    time_clean = time
    for w in ("每天", "每日", "每周", "工作日"):
        time_clean = time_clean.replace(w, "")
    if not time_clean.strip():
        time_clean = time
    due = parse_time_str(time_clean) if time_clean.strip() else None

    item = append_reminder(text, time_clean, due, repeat=repeat_clean)
    repeat_desc = {"daily": "（每天重复）", "weekly": "（每周重复）", "weekdays": "（工作日重复）"}.get(repeat_clean, "")
    when = f"，时间{due.replace('T', ' ')}" if due else ""
    return f"已添加提醒：{text}{when}{repeat_desc}"

@mcp.tool()
def set_timer(minutes: int, message: str = "") -> str:
    """设置倒计时定时器。minutes=分钟数, message=到点播报内容(可选)

    例: set_timer(5, "关火") → 5分钟后播报"主人，5分钟到了：关火"
    """
    from datetime import datetime as _dt, timedelta as _td
    from app.reminders import append_reminder

    msg = message if message else f"{minutes}分钟定时器"
    due = (_dt.now() + _td(minutes=minutes)).isoformat()
    item = append_reminder(msg, f"{minutes}分钟后", due, repeat="")
    return f"已设置{minutes}分钟定时器：{msg}，到点会提醒你。"

@mcp.tool()
def set_volume(level: int = -1) -> str:
    """控制系统音量。level=0-100(百分比), 不传或-1=当前音量

    例: set_volume(50) → 音量调到50%
        set_volume(0) → 静音
        set_volume(100) → 最大音量
    """
    import subprocess
    try:
        if level < 0:
            r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                             capture_output=True, text=True, timeout=5)
            return f"当前音量：{r.stdout.strip()}%"
        level = max(0, min(100, level))
        subprocess.run(["osascript", "-e", f"set volume output volume {level}"], timeout=5)
        return f"音量已调到{level}%"
    except Exception as e:
        return f"音量控制失败：{e}"

@mcp.tool()
def set_speech_speed(speed: str = "normal") -> str:
    """控制 Charlie 说话语速。speed: slow=慢, normal=正常, fast=快, 或直接传数字(0.5-2.0)

    例: set_speech_speed("slow") → 慢速说话
        set_speech_speed("1.0") → 正常语速
        set_speech_speed("0.8") → 稍慢
    """
    import voice_agent
    speed_map = {"slow": 0.8, "normal": 1.0, "fast": 1.2, "慢": 0.8, "正常": 1.0, "快": 1.2}
    try:
        if speed in speed_map:
            val = speed_map[speed]
        else:
            val = float(speed)
            val = max(0.5, min(2.0, val))
        voice_agent._tts_speed = val
        desc = "慢" if val < 1.0 else ("快" if val > 1.0 else "正常")
        return f"语速已调到{desc}({val:g}x)"
    except Exception as e:
        return f"语速控制失败：{e}"

@mcp.tool()
def get_detailed_weather(city: str = "北京") -> str:
    """获取详细天气预报，包括今天白天/夜间天气、温度、逐小时预报和穿衣建议。

    例: get_detailed_weather() → 返回今天详细天气+穿衣建议
        get_detailed_weather("上海") → 返回上海天气
    """
    import os, requests
    AMAP = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    # 城市名到adcode的映射(常见城市)
    city_map = {"北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
                "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
                "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000"}
    adcode = city_map.get(city, "110000")

    try:
        # 1. 日级预报
        r = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": adcode, "key": AMAP, "extensions": "all"}, timeout=10).json()
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if not casts:
            return f"获取{city}天气失败"

        today = casts[0]
        day_w = today.get("dayweather", "")
        night_w = today.get("nightweather", "")
        day_temp = today.get("daytemp", "")
        night_temp = today.get("nighttemp", "")

        # 合并天气描述
        weather_parts = []
        for w in (day_w, night_w):
            if w and w not in weather_parts:
                weather_parts.append(w)
        weather = "转".join(weather_parts) if len(weather_parts) > 1 else weather_parts[0]

        # 穿衣建议
        try:
            avg_temp = (int(day_temp) + int(night_temp)) // 2
        except (ValueError, TypeError):
            avg_temp = 20

        if avg_temp >= 28:
            clothing = "天气炎热，穿短袖短裤就行"
        elif avg_temp >= 22:
            clothing = "天气温暖，薄长袖或短袖都可以"
        elif avg_temp >= 15:
            clothing = "天气凉爽，建议穿外套或卫衣"
        elif avg_temp >= 8:
            clothing = "天气较冷，穿厚外套或羽绒服"
        elif avg_temp >= 0:
            clothing = "天气很冷，穿羽绒服+保暖内衣"
        else:
            clothing = "严寒，穿厚羽绒服+保暖内衣+围巾手套"

        # 雨伞建议
        umbrella = ""
        if any("雨" in w for w in weather_parts):
            umbrella = "，今天有雨，记得带伞"
        elif any("雪" in w for w in weather_parts):
            umbrella = "，今天有雪，注意防滑"

        # 2. 实时天气
        try:
            r2 = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
                params={"city": adcode, "key": AMAP, "extensions": "base"}, timeout=10).json()
            live = (r2.get("lives") or [{}])[0]
            now_temp = live.get("temperature", "")
            now_w = live.get("weather", "")
            now_humidity = live.get("humidity", "")
            now_wind = f"{live.get('windpower', '')}级风" if live.get("windpower") else ""
            realtime = f"实时：{now_w}，{now_temp}度"
            if now_humidity:
                realtime += f"，湿度{now_humidity}%"
            if now_wind:
                realtime += f"，{now_wind}"
        except Exception:
            realtime = ""

        result = f"{city}今天{weather}，{day_temp}到{night_temp}度。{realtime}。{clothing}{umbrella}。"
        return result
    except Exception as e:
        return f"获取{city}天气失败：{e}"

@mcp.tool()
def get_news(topic: str = "科技", count: int = 5) -> str:
    """获取最新新闻。topic=新闻主题(科技/财经/社会/体育/国际), count=条数

    例: get_news("科技") → 获取最新科技新闻
        get_news("财经", 3) → 获取3条财经新闻
    """
    import os, sys, requests, re, html as htmlmod
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # 用 Bing 搜索新闻
    query = f"{topic}新闻 最新"
    try:
        r = requests.get('https://cn.bing.com/search',
            params={'q': query, 'count': count * 2},
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            timeout=15)
        blocks = re.findall(r'<li class="b_algo".*?</li>', r.text, re.DOTALL)
        results = []
        for b in blocks[:count * 2]:
            title_m = re.search(r'<h2[^>]*>(.*?)</h2>', b, re.DOTALL)
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ''
            title = htmlmod.unescape(title)
            snippet_m = re.search(r'<p[^>]*>(.*?)</p>', b, re.DOTALL)
            snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ''
            snippet = htmlmod.unescape(snippet)
            if title and len(title) > 5:
                results.append(f"• {title}\n  {snippet[:60]}")
        if not results:
            return f"没找到{topic}相关新闻"
        return f"最新{topic}新闻（{len(results[:count])}条）：\n" + "\n\n".join(results[:count])
    except Exception as e:
        return f"获取新闻失败：{e}"

@mcp.tool()
def leaving_home() -> str:
    """场景模式：用户要出门了。自动关闭空调，播报今天天气和注意事项。

    例: leaving_home() → 关空调+播报天气
    """
    import os, sys, requests
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    actions = []

    # 1. 关空调
    try:
        from mcp_ir_control import ac_control
        ac_result = ac_control("off")
        actions.append("空调已关闭")
    except Exception:
        try:
            # 直接调 ESP32
            esp32_ip = os.getenv("ESP32_IP", "192.168.1.7")
            requests.post(f"http://{esp32_ip}/api/ir/send",
                json={"device": "ac", "action": "power_off"}, timeout=5)
            actions.append("空调已关闭")
        except Exception:
            actions.append("空调关闭失败（设备可能不在线）")

    # 2. 播报天气
    try:
        AMAP = os.getenv("AMAP_KEY", "")
        r = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": "110000", "key": AMAP, "extensions": "all"}, timeout=10).json()
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if casts:
            today = casts[0]
            day_w = today.get("dayweather", "")
            night_w = today.get("nightweather", "")
            day_temp = today.get("daytemp", "")
            night_temp = today.get("nighttemp", "")
            weather_parts = []
            for w in (day_w, night_w):
                if w and w not in weather_parts:
                    weather_parts.append(w)
            weather = "转".join(weather_parts) if len(weather_parts) > 1 else (weather_parts[0] if weather_parts else "")
            umbrella = "，有雨记得带伞" if any("雨" in w for w in weather_parts) else ""
            actions.append(f"今天{weather}，{day_temp}到{night_temp}度{umbrella}")
    except Exception:
        pass

    # 3. 检查今日待办
    try:
        from app.reminders import _load_reminders
        import datetime
        rems = _load_reminders()
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        pending = [r for r in rems if not r.get("done") and r.get("due", "").startswith(today_str)]
        if pending:
            todo = "、".join(r["text"] for r in pending[:3])
            actions.append(f"今天还有{len(pending)}项待办：{todo}")
    except Exception:
        pass

    return "出门准备：" + "；".join(actions) + "。注意安全！"

@mcp.tool()
def get_calendar_today() -> str:
    """获取今天的日历日程（从 Apple Calendar 读取）"""
    import subprocess
    try:
        script = '''
        set today to current date
        set time of today to 0
        set tomorrow to today + (1 * days)
        set output to ""
        tell application "Calendar"
            repeat with c in calendars
                set evts to (every event of c whose start date >= today and start date < tomorrow)
                repeat with e in evts
                    set s to start date of e
                    set t to summary of e
                    set h to hours of s
                    set m to minutes of s
                    set output to output & (h as string) & ":" & (text -2 thru -1 of ("0" & (m as string))) & " " & t & "\n"
                end repeat
            end repeat
        end tell
        return output
        '''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        result = r.stdout.strip()
        if not result:
            return "今天没有日程安排。"
        return f"今天的日程：\n{result}"
    except Exception as e:
        return f"读取日历失败：{e}。请直接看手机日历。"

@mcp.tool()
def save_note(title: str = "", content: str = "") -> str:
    """保存语音备忘录。title=标题(可选，自动生成), content=内容

    例: save_note("明天带身份证") → 保存到备忘录目录
    """
    from datetime import datetime
    notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes")
    os.makedirs(notes_dir, exist_ok=True)

    if not title:
        now = datetime.now()
        title = f"备忘录_{now.strftime('%Y%m%d_%H%M')}"
    filename = f"{title.replace('/', '_').replace(' ', '_')}.md"
    filepath = os.path.join(notes_dir, filename)

    now = datetime.now()
    full = f"# {title}\n\n{content}\n\n_记录于 {now.strftime('%Y-%m-%d %H:%M')}_\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full)

    return f"已保存备忘录：{title} → {filename}"

@mcp.tool()
def list_notes() -> str:
    """列出所有语音备忘录"""
    notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes")
    if not os.path.exists(notes_dir):
        return "还没有备忘录。"
    files = [f for f in os.listdir(notes_dir) if f.endswith(".md")]
    if not files:
        return "还没有备忘录。"
    files.sort(reverse=True)
    lines = [f"共 {len(files)} 条备忘录："]
    for f in files[:20]:
        size = os.path.getsize(os.path.join(notes_dir, f))
        lines.append(f"• {f.replace('.md','')} ({size}字节)")
    return "\n".join(lines)


# ===== 翻译 =====
@mcp.tool()
def translate(text: str, target: str = "英文") -> str:
    """翻译。text=内容, target=目标语言(英文/中文/日文/韩文)"""
    return aliyun_chat([
        {"role": "system", "content": f"你是翻译引擎，把用户内容翻译成{target}，只输出译文。"},
        {"role": "user", "content": text}
    ])


# ===== 系统监控 =====
@mcp.tool()
def system_status() -> str:
    """查询当前设备/服务器状态：CPU/内存/磁盘/运行时间"""
    import socket, platform, time as _t
    try:
        import psutil
        psutil.cpu_percent(interval=None)
    except ImportError:
        psutil = None
    _psutil = psutil if psutil is not None else __import__("psutil")
    vm = _psutil.virtual_memory()
    cpu_pct = _psutil.cpu_percent(interval=None)
    disk = _psutil.disk_usage('/')
    boot = _psutil.boot_time()
    up = int(_t.time() - boot)
    return (f"设备:{socket.gethostname()} | {platform.system()} {platform.release()}\n"
        f"CPU使用率:{cpu_pct}% | {_psutil.cpu_count()}核 | 内存:{(vm.total-vm.available)//1073741824:.1f}/{vm.total//1073741824:.1f}GB({vm.percent}%)\n"
        f"磁盘:{disk.used//1073741824:.0f}/{disk.total//1073741824:.0f}GB({disk.percent}%) | 运行:{up//86400}天{up%86400//3600}时{up%3600//60}分")


# ===== 生活服务deeplink(外卖/餐厅/购物/买菜/买药/打车) =====
@mcp.tool()
def open_lifestyle_app(intent: str, keyword: str = "") -> str:
    """打开生活服务App(手机点击唤起到搜索/下单页)。intent: waimai外卖/food餐厅/shopping购物/grocery买菜/pharmacy买药/ride打车, keyword: 搜索词"""
    from urllib.parse import quote
    q = quote(keyword or "")
    links = {}
    if intent == "waimai":
        links["美团外卖"] = f"https://h5.waimai.meituan.com/waimai/mindex/search?query={q}"
        links["饿了么"] = f"https://www.ele.me/search/{q}"
    elif intent == "food":
        links["大众点评"] = f"https://m.dianping.com/searchlist?keyword={q}"
        links["美团团购"] = f"https://meituan.com/s/{q}/"
    elif intent == "shopping":
        links["淘宝"] = f"https://s.m.taobao.com/h5?q={q}"
        links["京东"] = f"https://so.m.jd.com/ware/search.action?keyword={q}"
        links["拼多多"] = f"https://mobile.yangkeduo.com/search_result.html?search_key={q}"
    elif intent == "grocery":
        links["美团买菜"] = f"https://h5.waimai.meituan.com/waimai/mindex/search?query={q}"
    elif intent == "pharmacy":
        links["美团买药"] = f"https://h5.waimai.meituan.com/waimai/mindex/search?query={q}"
        links["饿了么买药"] = f"https://www.ele.me/search/{q}"
    elif intent == "ride":
        links["滴滴出行"] = "https://common.diditaxi.com.cn/webapp_landing?from=web"
    if not links:
        return f"未知意图{intent}，支持: waimai外卖/food餐厅/shopping购物/grocery买菜/pharmacy买药/ride打车"
    return f"已为'{keyword or intent}'生成App链接(手机点击唤起下单):\n" + "\n".join(f"• {k}: {v}" for k, v in links.items())


# ===== 计算/换算 =====
def _safe_math_eval(expr: str) -> float | None:
    """安全的数学表达式求值(基于AST, 不使用eval)"""
    import ast, operator
    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
        ast.FloorDiv: operator.floordiv,
    }
    def _eval(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"不允许的常量类型: {type(node.value)}")
        elif isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            op_type = type(node.op)
            if op_type in operators:
                return operators[op_type](left, right)
            raise ValueError(f"不允许的操作: {op_type.__name__}")
        elif isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            op_type = type(node.op)
            if op_type in operators:
                return operators[op_type](operand)
            raise ValueError(f"不允许的一元操作: {op_type.__name__}")
        else:
            raise ValueError(f"不允许的语法: {type(node).__name__}")
    try:
        tree = ast.parse(expr, mode='eval')
        return _eval(tree.body)
    except Exception:
        return None

@mcp.tool()
def calculate(expression: str) -> str:
    """计算或单位换算。expression=算式如'123*456'或'5公里等于多少英里'"""
    import re
    e = expression.strip().rstrip("=＝")
    if re.fullmatch(r"[\d.\s+\-*/()%^]+", e):
        result = _safe_math_eval(e.replace("^", "**"))
        if result is not None:
            return f"{e} = {result}"
    return aliyun_chat([{"role":"system","content":"你是计算换算助手，直接给结果和一行过程"},
        {"role":"user","content":expression}], temperature=0)


# ===== WiFi/位置定位 =====
ESP32_IP = os.getenv("ESP32_IP", "192.168.1.7")

@mcp.tool()
def get_location() -> str:
    """获取用户当前位置：ESP32 WiFi扫描(室内定位) + 浏览器GPS经纬度反向地理编码。
    返回城市/街道/经纬度。当用户问'我在哪''附近有什么'时调用。"""
    parts = []
    # 1. 尝试 ESP32 WiFi scan（室内定位）
    try:
        r = requests.get(f"http://{ESP32_IP}/api/wifi/scan", timeout=10)
        if r.status_code == 200:
            aps = r.json()
            if aps:
                parts.append(f"WiFi扫描到{len(aps)}个AP:")
                for ap in aps[:5]:
                    parts.append(f"  {ap.get('ssid','?')} RSSI={ap.get('rssi','?')}dBm")
                strongest = max(aps, key=lambda x: x.get('rssi', -999))
                if strongest.get('rssi', -999) > -50:
                    parts.append(f"最强信号: {strongest['ssid']}({strongest['rssi']}dBm) → 可能在室内")
    except Exception as e:
        parts.append(f"ESP32 WiFi扫描失败: {type(e).__name__}")

    # 2. 用高德地图 IP 定位
    try:
        amap_key = os.getenv("AMAP_KEY", "")
        if amap_key:
            r2 = requests.get(f"https://restapi.amap.com/v3/ip?key={amap_key}&output=json", timeout=5)
            data = r2.json()
            if data.get("city"):
                parts.append(f"IP定位: {data.get('province','')} {data.get('city','')}")
                if data.get('rectangle'):
                    parts.append(f"坐标范围: {data['rectangle']}")
    except Exception as e:
        parts.append(f"IP定位失败: {type(e).__name__}")

    if len(parts) <= 1:
        return "无法获取位置信息。请在浏览器中允许定位权限，或确保ESP32在线。"
    return "\n".join(parts)


# ===== 音乐播放(网易云音乐 ncm-cli) =====
NCM_BIN = os.path.expanduser("~/.local/bin/ncm")

@mcp.tool()
def search_music(keyword: str, limit: int = 5) -> str:
    """搜索网易云音乐歌曲。keyword=歌名或歌手名, limit=返回条数(默认5)

    例: search_music("周杰伦 晴天") → 搜索周杰伦的晴天
        search_music("邓紫棋") → 搜索邓紫棋的歌曲
    """
    import subprocess, json as _json
    try:
        r = subprocess.run([NCM_BIN, "search", "song", keyword, "--limit", str(limit), "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"搜索失败: {r.stderr.strip() or '未知错误'}"
        data = _json.loads(r.stdout)
        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return f"没搜到'{keyword}'相关的歌"
        lines = [f"搜到{len(songs)}首歌："]
        for s in songs:
            ars = "/".join(a.get("name", "") for a in s.get("ar", []))
            lines.append(f"• [{s['id']}] {s['name']} - {ars}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索音乐失败: {e}"


@mcp.tool()
def play_music(keyword: str = "", song_id: int = 0) -> str:
    """播放音乐。keyword=歌名或歌手名(搜索后播放第一首), song_id=歌曲ID(直接播放)

    例: play_music("周杰伦 晴天") → 搜索并播放周杰伦的晴天
        play_music(song_id=186016) → 直接播放指定歌曲
    """
    import subprocess, json as _json, tempfile, os as _os
    try:
        if not song_id and keyword:
            # 先搜索取第一首
            r = subprocess.run([NCM_BIN, "search", "song", keyword, "--limit", "1", "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"搜索失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            songs = data.get("result", {}).get("songs", [])
            if not songs:
                return f"没搜到'{keyword}'"
            song_id = songs[0]["id"]
            song_name = songs[0]["name"]
            ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))
        elif song_id:
            # 获取歌曲信息
            r = subprocess.run([NCM_BIN, "song", str(song_id), "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                data = _json.loads(r.stdout)
                songs = data.get("songs", [])
                if songs:
                    song_name = songs[0].get("name", str(song_id))
                    ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))
                else:
                    song_name, ars = str(song_id), ""
            else:
                song_name, ars = str(song_id), ""
        else:
            return "请说歌名或歌曲ID"

        # 获取播放地址
        r = subprocess.run([NCM_BIN, "url", str(song_id), "--level", "exhigh", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取播放地址失败: {r.stderr.strip()}"
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            # 可能有版权限制
            return f"'{song_name}'暂时无法播放（版权限制或需要VIP）"

        # 返回特殊格式让前端在浏览器播放
        # 强制 https:// 避免 HTTPS 页面加载 HTTP 资源被浏览器阻止(混合内容)
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        return f"播放音乐失败: {e}"


@mcp.tool()
def play_random_music() -> str:
    """随机播放一首歌。从每日推荐歌曲中随机选一首播放。

    例: play_random_music() → 随机播放一首推荐歌曲
    """
    import subprocess, json as _json, random
    try:
        # 获取每日推荐歌曲列表
        r = subprocess.run([NCM_BIN, "recommend", "songs", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            # 降级: 用播放记录作为随机源
            r = subprocess.run([NCM_BIN, "record", "--json", "--limit", "30"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"获取推荐歌曲失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            songs = data.get("data", []) or data.get("record", [])
        else:
            data = _json.loads(r.stdout)
            songs = data.get("recommend", [])

        if not songs:
            return "暂时没有推荐歌曲"

        # 随机选一首
        song = random.choice(songs)
        song_id = song["id"]
        song_name = song.get("name", "")
        ars = "/".join(a.get("name", "") for a in song.get("ar", []))

        # 获取播放地址
        r = subprocess.run([NCM_BIN, "url", str(song_id), "--level", "exhigh", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取播放地址失败: {r.stderr.strip()}"
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return f"'{song_name}'暂时无法播放（版权限制）"

        # 强制 https:// 避免 HTTPS 页面加载 HTTP 资源被浏览器阻止(混合内容)
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        return f"随机播放失败: {e}"


@mcp.tool()
def stop_music() -> str:
    """停止当前正在播放的音乐"""
    return "__MUSIC_STOP__"


@mcp.tool()
def list_playlists() -> str:
    """列出你的网易云音乐歌单"""
    import subprocess, json as _json
    try:
        r = subprocess.run([NCM_BIN, "playlist", "list", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取歌单失败: {r.stderr.strip()}"
        data = _json.loads(r.stdout)
        playlists = data.get("playlist", [])
        if not playlists:
            return "没有歌单"
        lines = [f"共{len(playlists)}个歌单："]
        for p in playlists[:10]:
            lines.append(f"• [{p['id']}] {p['name']} ({p.get('trackCount', 0)}首)")
        return "\n".join(lines)
    except Exception as e:
        return f"获取歌单失败: {e}"


@mcp.tool()
def play_playlist(playlist_id: int = 0) -> str:
    """播放歌单。playlist_id=歌单ID(不传则播放每日推荐)"""
    import subprocess, json as _json, tempfile, os as _os
    try:
        if not playlist_id:
            # 播放每日推荐第一首
            r = subprocess.run([NCM_BIN, "recommend", "songs", "--limit", "1", "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"获取每日推荐失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            songs = data.get("data", {}).get("dailySongs", [])
            if not songs:
                return "暂无每日推荐"
            song = songs[0]
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))
        else:
            # 获取歌单第一首歌
            r = subprocess.run([NCM_BIN, "playlist", "show", str(playlist_id), "--limit", "1", "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"获取歌单失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            tracks = data.get("tracks", data.get("songs", []))
            if not tracks:
                return "歌单为空"
            song = tracks[0]
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))

        # 获取播放地址并播放
        r = subprocess.run([NCM_BIN, "url", str(song_id), "--level", "exhigh", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取播放地址失败: {r.stderr.strip()}"
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return f"'{song_name}'暂时无法播放（版权限制）"

        # 强制 https:// 避免 HTTPS 页面加载 HTTP 资源被浏览器阻止(混合内容)
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        return f"播放歌单失败: {e}"


if __name__ == "__main__":
    mcp.run()