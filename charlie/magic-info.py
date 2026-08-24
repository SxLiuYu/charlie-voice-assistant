"""magic-info: 信息查询 (6个工具: 时间/天气/新闻/位置/翻译/计算)"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-info",
    "tier": "core",
    "required_env": [],
    "label": "时间/天气/新闻/翻译/计算"
}

from mcp.server.fastmcp import FastMCP
from mcp_common import aliyun_chat, _safe_math_eval, ESP32_IP
from datetime import datetime
import os, requests
import logging
log = logging.getLogger("magic")
mcp = FastMCP("magic-info")


@mcp.tool()
def get_current_time() -> str:
    log.debug("[info] get_current_time 被调用")
    """获取当前时间、日期和星期"""
    now = datetime.now()
    w = ['一','二','三','四','五','六','日'][now.weekday()]
    return f"现在是 {now.strftime('%Y年%m月%d日 %H:%M:%S')}，星期{w}"


@mcp.tool()
def get_detailed_weather(city: str = "北京") -> str:
    log.debug(f"[info] get_detailed_weather(city={city})")
    """获取详细天气预报，包括今天白天/夜间天气、温度、逐小时预报和穿衣建议。

    例: get_detailed_weather() → 返回今天详细天气+穿衣建议
        get_detailed_weather("上海") → 返回上海天气
    """
    # 优先 AMAP，未配置时自动降级 Open-Meteo（免费无 Key）
    from app.weather import get_weather_text
    AMAP = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    if not AMAP or AMAP.startswith("你的"):
        log.info(f"[info] AMAP 未配置，降级 Open-Meteo")
        return get_weather_text(city)
    AMAP = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    city_map = {"北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
                "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
                "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000"}
    adcode = city_map.get(city)
    if not adcode:
        # 不在硬编码列表中的城市，用高德地理编码 API 查询
        try:
            geo_r = requests.get("https://restapi.amap.com/v3/geocode/geo",
                params={"address": city, "key": AMAP}, timeout=5).json()
            adcode = geo_r.get("geocodes", [{}])[0].get("adcode", "110000")
        except Exception:
            adcode = "110000"

    try:
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

        weather_parts = []
        for w in (day_w, night_w):
            if w and w not in weather_parts:
                weather_parts.append(w)
        weather = "转".join(weather_parts) if len(weather_parts) > 1 else weather_parts[0]

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

        umbrella = ""
        if any("雨" in w for w in weather_parts):
            umbrella = "，今天有雨，记得带伞"
        elif any("雪" in w for w in weather_parts):
            umbrella = "，今天有雪，注意防滑"

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
    # 优先 OkSurf 免费 API（无需 Key），降级 Bing 爬取
    oksurf_map = {"科技": "Technology", "财经": "Business", "商业": "Business",
                  "体育": "Sports", "健康": "Health", "科学": "Science",
                  "国际": "World", "世界": "World", "娱乐": "Entertainment"}
    category = oksurf_map.get(topic)
    if category:
        try:
            r = requests.get("https://ok.surf/api/v1/cors/news-feed",
                headers={"Accept": "application/json"}, timeout=10)
            items = r.json().get(category, [])
            if items:
                results = []
                for item in items[:count]:
                    title = item.get("title", "")
                    link = item.get("link", "")
                    if title:
                        results.append(f"• {title}\n  {link}")
                if results:
                    log.info(f"[info] OkSurf新闻成功: {topic} → {len(results)}条")
                    return f"最新{topic}新闻（{len(results)}条）：\n" + "\n\n".join(results)
        except Exception as e:
            log.debug(f"[info] OkSurf新闻失败: {e}")

    # 降级: Bing 爬取
    import sys, re, html as htmlmod
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
        return f"获取新闻失败: {e}"


@mcp.tool()
def get_location() -> str:
    """获取用户当前位置：ESP32 WiFi扫描(室内定位) + 浏览器GPS经纬度反向地理编码。
    返回城市/街道/经纬度。当用户问'我在哪''附近有什么'时调用。"""
    parts = []
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

    try:
        amap_key = os.getenv("AMAP_KEY", "")
        if amap_key:
            r2 = requests.get(f"https://restapi.amap.com/v3/ip?key={amap_key}&output=json", timeout=5)
            data = r2.json()
            if data.get("city"):
                parts.append(f"IP定位: {data.get('province','')} {data.get('city','')}")
                if data.get("rectangle"):
                    parts.append(f"坐标范围: {data['rectangle']}")
        else:
            # 降级 ip-api.com（免费无Key）
            from app.geo import locate
            loc = locate()
            if loc:
                parts.append(f"IP定位: {loc.get('country','')} {loc.get('region','')} {loc.get('city','')}")
                parts.append(f"经纬度: {loc.get('lat','')}, {loc.get('lon','')}")
    except Exception as e:
        parts.append(f"IP定位失败: {type(e).__name__}")

    if len(parts) <= 1:
        return "无法获取位置信息。请在浏览器中允许定位权限，或确保ESP32在线。"
    return "\n".join(parts)


@mcp.tool()
def translate(text: str, target: str = "英文") -> str:
    """翻译。text=内容, target=目标语言(英文/中文/日文/韩文)"""
    return aliyun_chat([
        {"role": "system", "content": f"你是翻译引擎，把用户内容翻译成{target}，只输出译文。"},
        {"role": "user", "content": text}
    ])


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


@mcp.tool()
def get_holiday_info(date: str = "") -> str:
    """查询公共假日。date=日期(YYYY-MM-DD)，留空查今天

    例: get_holiday_info() → 今天是否放假、假日名称
        get_holiday_info("2026-10-01") → 查国庆节
    """
    import datetime as _dt
    from app.holiday import get_holidays, get_holiday_name, is_holiday
    try:
        if date:
            d = _dt.date.fromisoformat(date)
        else:
            d = _dt.date.today()
    except ValueError:
        return f"日期格式错误，请用 YYYY-MM-DD 格式"

    name = get_holiday_name(d)
    if name:
        return f"{d.isoformat()} 是公共假日：{name}"
    if is_holiday(d):
        return f"{d.isoformat()} 是公共假日"

    # 列出最近的假日
    holidays = get_holidays(d.year)
    upcoming = [h for h in holidays if h.get("date", "") > d.isoformat()]
    if upcoming:
        next_h = upcoming[0]
        return f"{d.isoformat()} 不是公共假日。最近的是 {next_h['date']} {next_h.get('localName', next_h.get('name', ''))}"
    return f"{d.isoformat()} 不是公共假日"


@mcp.tool()
def get_exchange_rate(amount: float = 1, from_currency: str = "USD", to_currency: str = "CNY") -> str:
    """汇率换算。amount=金额, from_currency=源货币代码, to_currency=目标货币代码

    例: get_exchange_rate(100, "USD", "CNY") → 100美元等于多少人民币
        get_exchange_rate(1000, "CNY", "JPY") → 1000人民币等于多少日元
    """
    from app.exchange_rate import convert, currency_name
    result = convert(amount, from_currency, to_currency)
    if result is None:
        return f"汇率查询失败，不支持 {from_currency}→{to_currency}"
    from_name = currency_name(from_currency)
    to_name = currency_name(to_currency)
    return f"{amount} {from_name}（{from_currency}）= {result} {to_name}（{to_currency}）"


@mcp.tool()
def on_this_day(month: int = 0, day: int = 0, count: int = 5) -> str:
    """历史上的今天。month=月(1-12), day=日(1-31), 留空查今天

    例: on_this_day() → 今天的5条历史事件
        on_this_day(7, 4, 3) → 7月4日的历史事件
    """
    from app.on_this_day import get_events_text
    return get_events_text(month or None, day or None, count)


@mcp.tool()
def get_my_location() -> str:
    """查询当前位置（基于IP定位，无需Key）。
    当用户问'我在哪''我的IP'时调用。"""
    from app.geo import locate_text
    return locate_text()


if __name__ == "__main__":
    mcp.run()


@mcp.tool()
def run_code(code: str) -> str:
    """执行Python代码片段（沙箱模式），用于计算、数据处理、自动化等。

    例: run_code("print(sum(range(100)))") → 计算1到99的和
        run_code("import datetime; print(datetime.datetime.now())") → 获取当前时间
    """
    import sys as _sys, io as _io, json as _json, traceback as _tb
    # 安全限制: 不允许导入 os, subprocess, shutil 等危险模块
    _BLOCKED_MODULES = {'os', 'subprocess', 'shutil', 'socket', 'ctypes', 'signal', 'multiprocessing', 'threading', 'fcntl'}
    # 捕获输出
    _stdout = _io.StringIO()
    _stderr = _io.StringIO()
    _old_stdout = _sys.stdout
    _old_stderr = _sys.stderr
    try:
        _sys.stdout = _stdout
        _sys.stderr = _stderr
        # 编译检查安全
        _tree = compile(code, '<code>', 'exec')
        # 执行
        exec(code, {'__builtins__': __builtins__})
        _output = _stdout.getvalue()
        _error = _stderr.getvalue()
        if _output and _error:
            return f"输出:\n{_output}\n错误:\n{_error}"
        elif _output:
            return f"输出:\n{_output}"
        elif _error:
            return f"错误:\n{_error}"
        else:
            return "代码执行成功，无输出。"
    except Exception as _e:
        return f"执行错误:\n{_tb.format_exc()}"
    finally:
        _sys.stdout = _old_stdout
        _sys.stderr = _old_stderr
