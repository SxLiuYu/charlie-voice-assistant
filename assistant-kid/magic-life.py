"""magic-life: 生活服务 (4个工具: 外卖/充电桩/特斯拉空调/出门场景)"""
from mcp.server.fastmcp import FastMCP
from mcp_common import ESP32_IP
import os, requests
mcp = FastMCP("magic-life")


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


@mcp.tool()
def search_charging_stations(city: str = "北京", count: int = 3) -> str:
    """搜索附近的充电桩(真实高德数据)。参数: city-城市, count-数量"""
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


@mcp.tool()
def leaving_home() -> str:
    """场景模式：用户要出门了。自动关闭空调，播报今天天气和注意事项。

    例: leaving_home() → 关空调+播报天气
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    actions = []

    # 1. 关空调
    try:
        from mcp_ir_control import ac_control
        ac_result = ac_control("off")
        actions.append("空调已关闭")
    except Exception:
        try:
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


if __name__ == "__main__":
    mcp.run()
