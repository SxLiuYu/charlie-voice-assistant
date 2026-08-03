"""magic-info: 信息查询 (6个工具: 时间/天气/新闻/位置/翻译/计算)"""
from mcp.server.fastmcp import FastMCP
from mcp_common import aliyun_chat, _safe_math_eval, ESP32_IP
from datetime import datetime
import os, requests
mcp = FastMCP("magic-info")


@mcp.tool()
def get_current_time() -> str:
    """获取当前时间、日期和星期"""
    now = datetime.now()
    w = ['一','二','三','四','五','六','日'][now.weekday()]
    return f"现在是 {now.strftime('%Y年%m月%d日 %H:%M:%S')}，星期{w}"


@mcp.tool()
def get_detailed_weather(city: str = "北京") -> str:
    """获取详细天气预报，包括今天白天/夜间天气、温度、逐小时预报和穿衣建议。

    例: get_detailed_weather() → 返回今天详细天气+穿衣建议
        get_detailed_weather("上海") → 返回上海天气
    """
    AMAP = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    city_map = {"北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
                "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
                "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000"}
    adcode = city_map.get(city, "110000")

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


if __name__ == "__main__":
    mcp.run()
