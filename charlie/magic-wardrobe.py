"""magic-wardrobe: AI 穿搭推荐 MCP

融合电子衣橱规则库 + 天气 + ARK LLM 精排，给穿搭建议。
支持:
- 基于天气自动推荐搭配
- 按场合推荐 (正式/休闲/约会/运动)
- 查看衣橱列表
- 添加/删除衣物
- 后期接入摄像头: 拍照 → 识别 → 推荐

依赖: ~/.charlie/wardrobe/ (颜色搭配规则 + 衣橱数据)
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-wardrobe",
    "tier": "optional",
    "required_env": ['AMAP_KEY'],
    "label": "AI穿搭推荐"
}

from mcp.server.fastmcp import FastMCP
import os, json, requests, datetime, re
import logging
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("magic")

mcp = FastMCP("magic-wardrobe")

WARDROBE_DIR = os.environ.get("CHARLIE_WARDROBE_DIR") or os.path.join(
    os.environ.get("ASSISTANT_KID_DATA_DIR") or os.path.expanduser("~"), "charlie", "wardrobe")
DATA_DIR = os.path.join(WARDROBE_DIR, "data")
WARDROBE_FILE = os.path.join(DATA_DIR, "wardrobe.json")
os.makedirs(DATA_DIR, exist_ok=True)

# 颜色搭配规则 (从 wardrobe/app.py 提取)
COLOR_PAIRS_GOOD = {
    "白色": ["黑色", "灰色", "蓝色", "卡其色", "粉色", "红色", "绿色", "棕色", "米色"],
    "黑色": ["白色", "灰色", "红色", "金色", "银色", "米色", "蓝色"],
    "灰色": ["白色", "黑色", "粉色", "蓝色", "紫色", "绿色"],
    "蓝色": ["白色", "灰色", "卡其色", "黑色", "米色", "棕色"],
    "卡其色": ["白色", "蓝色", "黑色", "棕色", "绿色", "米色"],
    "米色": ["白色", "黑色", "棕色", "蓝色", "灰色", "绿色"],
    "棕色": ["白色", "米色", "卡其色", "蓝色", "绿色"],
    "红色": ["黑色", "白色", "灰色", "蓝色"],
    "粉色": ["白色", "灰色", "黑色", "蓝色", "紫色"],
    "绿色": ["白色", "黑色", "卡其色", "米色", "棕色", "灰色"],
    "紫色": ["白色", "灰色", "黑色", "粉色"],
    "黄色": ["白色", "黑色", "灰色", "蓝色"],
    "橙色": ["白色", "黑色", "灰色", "蓝色"],
}

OCCASION_STYLES = {
    "正式": ["正式"],
    "商务": ["正式"],
    "面试": ["正式"],
    "会议": ["正式"],
    "休闲": ["休闲", "日常"],
    "日常": ["休闲", "日常", "约会"],
    "逛街": ["休闲", "日常"],
    "约会": ["约会", "休闲"],
    "聚会": ["约会", "休闲"],
    "运动": ["运动"],
    "户外": ["运动", "休闲"],
    "健身": ["运动"],
}

# 温度 → 厚度建议
def warmth_from_temp(temp: float) -> list:
    if temp < 5: return ["厚"]
    if temp < 15: return ["厚", "适中"]
    if temp < 23: return ["适中", "薄"]
    return ["薄"]

# 温度 → 穿搭建议 (纯规则版, 无衣橱时降级使用)
def simple_outfit_advice(city: str, temp: int, weather_type: str = "晴") -> str:
    if temp >= 28:
        return f"今天{city}{temp}°C{weather_type}，很热。建议穿短袖T恤、短裤、裙子，注意防晒，多喝水。"
    elif 23 <= temp < 28:
        return f"今天{city}{temp}°C{weather_type}，温暖舒适。建议穿长袖T恤、薄衬衫或薄外套，早晚可带开衫。"
    elif 15 <= temp < 23:
        return f"今天{city}{temp}°C{weather_type}，天气凉爽。建议穿薄风衣、夹克、长袖卫衣，里面搭衬衫或T恤。"
    elif 5 <= temp < 15:
        return f"今天{city}{temp}°C{weather_type}，偏凉。建议穿毛衣、线衫加外套，或厚风衣，怕冷戴围巾。"
    else:
        return f"今天{city}{temp}°C{weather_type}，很冷。建议穿厚羽绒服或棉服，搭配毛衣、秋裤，注意保暖。"


def load_wardrobe() -> list:
    if os.path.exists(WARDROBE_FILE):
        try:
            with open(WARDROBE_FILE, 'r', encoding='utf-8') as f:
                items = json.load(f)
            log.debug(f"[wardrobe] 加载衣橱 {len(items)} 件衣物")
            return items
        except Exception as e:
            log.warning(f"[wardrobe] 读衣橱失败: {e}")
            return []
    log.debug("[wardrobe] 衣橱文件不存在，返回空")
    return []


def save_wardrobe(items: list):
    try:
        with open(WARDROBE_FILE, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        log.info(f"[wardrobe] 保存衣橱 {len(items)} 件衣物")
    except Exception as e:
        log.error(f"[wardrobe] 写衣橱失败: {e}")
        raise


def get_weather(city: str) -> dict:
    # 先用 amap (Charlie 已有高德 key)
    amap_key = os.getenv("AMAP_KEY", "")
    if amap_key:
        try:
            # 先查城市编码
            r = requests.get("https://restapi.amap.com/v3/config/district",
                params={"keywords": city, "subdistrict": 0, "key": amap_key}, timeout=5)
            data = r.json()
            if data.get("districts"):
                adcode = data["districts"][0].get("adcode", "")
                if adcode:
                    r2 = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
                        params={"city": adcode, "key": amap_key, "extensions": "base"}, timeout=5)
                    w = r2.json()
                    if w.get("status") == "1" and w.get("lives"):
                        live = w["lives"][0]
                        log.debug(f"[wardrobe] 高德天气成功: {city} {live.get('weather')} {live.get('temperature')}度")
                        return {
                            "city": live.get("city", city),
                            "temp": int(live.get("temperature", 20)),
                            "weather": live.get("weather", "晴"),
                            "humidity": live.get("humidity", "50"),
                            "wind": live.get("winddirection", "") + live.get("windpower", "") + "级",
                        }
        except Exception as e:
            log.warning(f"[wardrobe] 高德天气失败: {e}")
    # 降级 Open-Meteo（免费无 Key），再降级 wttr.in
    try:
        from app.weather import _open_meteo_get
        w = _open_meteo_get(city)
        if w:
            log.debug(f"[wardrobe] Open-Meteo降级成功: {city} {w.get('weather_text')} {w.get('current_temp', w.get('day_temp', 20))}度")
            return {"city": city, "temp": w.get("current_temp", w.get("day_temp", 20)),
                    "weather": w.get("weather_text", "晴"), "humidity": "50", "wind": ""}
    except Exception as e:
        log.debug(f"[wardrobe] Open-Meteo失败: {e}")
    # 最终降级 wttr.in
    try:
        r = requests.get(f"https://wttr.in/{city}?format=j1", timeout=8, headers={"User-Agent": "Wardrobe/1.0"})
        d = r.json()
        cur = d.get("current_condition", [{}])[0]
        temp = int(cur.get("temp_C", 20))
        desc = cur.get("weatherDesc", [{}])[0].get("value", "晴")
        log.debug(f"[wardrobe] wttr.in降级成功: {city} {desc} {temp}度")
        return {"city": city, "temp": temp, "weather": desc}
    except Exception as e:
        log.warning(f"[wardrobe] wttr.in也失败: {e}")
        return {"city": city, "temp": 20, "weather": "晴"}


def generate_outfit_with_llm(weather: dict, occasion: str = "日常", wardrobe_items: list = None) -> str:
    """用 ARK LLM 生成穿搭建议"""
    from app.llm_config import active_chat_endpoint

    log.info(f"[wardrobe] LLM生成穿搭: occasion={occasion}, weather={weather.get('weather')} {weather.get('temp')}度")
    base, api_key, model = active_chat_endpoint()

    city = weather.get("city", "你所在的城市")
    temp = weather.get("temp", 20)
    w = weather.get("weather", "晴")
    hum = weather.get("humidity", "50")

    wardrobe_info = ""
    if wardrobe_items:
        cats = {}
        for it in wardrobe_items:
            cat = it.get("category", "未知")
            if cat not in cats: cats[cat] = []
            cats[cat].append(f"{it.get('name','')}({it.get('color','')})")
        wardrobe_info = "\n用户衣橱:\n"
        for cat, names in cats.items():
            wardrobe_info += f"  {cat}: {', '.join(names[:5])}\n"

    prompt = f"""你是用户的私人穿搭顾问。用户是一位女士的老公在帮她问穿搭。

天气: {city} {temp}°C, {w}, 湿度{hum}%
场合: {occasion}
{wardrobe_info}

要求:
1. 给3套搭配方案 (上装+下装/裙子+外套+鞋子)
2. 考虑温度和天气 (下雨推荐带伞/防水外套, 冷推荐厚衣服)
3. 每套写一句推荐理由, 口语化有画面感
4. 最后给出穿搭小贴士 (防晒/保暖/配饰等)

输出格式:
方案一: [上装]+[下装/裙子]+[外套]+[鞋子] → 推荐理由
方案二: ...
方案三: ...
小贴士: ..."""

    if not api_key:
        return simple_outfit_advice(city, temp, w)

    try:
        r = requests.post(f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是穿搭顾问, 用中文给出搭配建议。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 300,
            }, timeout=15)
        reply = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if reply:
            return reply
    except Exception:
        pass
    return simple_outfit_advice(city, temp, w)


@mcp.tool()
def _get_default_city() -> str:
    """获取默认城市: 用户位置 > 家位置 > 环境变量 > 北京"""
    # 1. 尝试从用户状态获取位置
    try:
        from voice_agent import get_user_state
        state = get_user_state()
        loc = state.get("last_location")
        if loc:
            # 逆地理编码: lat,lng → 城市名
            amap_key = os.getenv("AMAP_KEY", "")
            if amap_key:
                r = requests.get("https://restapi.amap.com/v3/geocode/regeo",
                    params={"location": f"{loc[1]},{loc[0]}", "key": amap_key, "extensions": "base"},
                    timeout=5)
                data = r.json()
                if data.get("status") == "1" and data.get("regeocode"):
                    city = data["regeocode"].get("addressComponent", {}).get("city", "")
                    if city:
                        return city.rstrip("市")
    except Exception:
        pass
    # 2. 环境变量
    return os.getenv("DEFAULT_CITY", "北京")

def outfit_recommend(city: str = "", occasion: str = "日常") -> str:
    """穿搭推荐: 根据今天天气和场合推荐3套搭配。

    参数:
    - city: 城市名 (默认自动检测位置, 否则北京)
    - occasion: 场合 (正式/商务/休闲/约会/运动/日常/聚会)

    例: outfit_recommend() → 自动检测位置
        outfit_recommend("北京", "约会") → 北京, 约会
    """
    if not city:
        city = _get_default_city()
    weather = get_weather(city)
    wardrobe = load_wardrobe()
    return generate_outfit_with_llm(weather, occasion, wardrobe)


@mcp.tool()
def wardrobe_list(category: str = "") -> str:
    """查看衣橱里的衣物列表。可按类别筛选 (上装/下装/外套/裙子/鞋子/配饰)。

    参数:
    - category: 类别筛选 (可选)

    例: wardrobe_list() → 查看全部
        wardrobe_list("上装") → 只看上装
    """
    items = load_wardrobe()
    if not items:
        return "衣橱还没有衣物。可以添加衣物: add_clothing('白色短袖T恤', '上装', '白色', '休闲', '薄')"
    if category:
        items = [it for it in items if it.get("category", "") == category]
    cats = {}
    for it in items:
        cat = it.get("category", "其他")
        if cat not in cats: cats[cat] = []
        cats[cat].append(f"{it.get('name','')}({it.get('color','')}, {it.get('warmth','')}, {it.get('style','')})")
    lines = [f"衣橱共 {len(items)} 件衣物："]
    for cat, names in sorted(cats.items()):
        lines.append(f"  【{cat}】({len(names)}件)")
        for n in names[:8]:
            lines.append(f"    • {n}")
        if len(names) > 8:
            lines.append(f"    ... 还有 {len(names)-8} 件")
    return "\n".join(lines)


@mcp.tool()
def add_clothing(name: str, category: str, color: str = "", style: str = "", warmth: str = "") -> str:
    """添加一件衣物到衣橱。

    参数:
    - name: 衣物名称 (如: 白色短袖T恤)
    - category: 类别 (上装/下装/外套/裙子/鞋子/配饰)
    - color: 颜色 (可选)
    - style: 风格 (正式/休闲/运动/约会/日常)
    - warmth: 厚度 (薄/适中/厚)

    例: add_clothing("白色短袖T恤", "上装", "白色", "休闲", "薄")
    """
    items = load_wardrobe()
    new_item = {
        "id": f"cloth_{len(items)+1}_{datetime.datetime.now().strftime('%m%d')}",
        "name": name,
        "category": category,
        "color": color,
        "style": style,
        "warmth": warmth,
        "added_at": datetime.datetime.now().isoformat(),
    }
    items.append(new_item)
    save_wardrobe(items)
    return f"已添加: {name} ({category}, {color})，衣橱现在 {len(items)} 件衣物。"


@mcp.tool()
def remove_clothing(name: str) -> str:
    """从衣橱删除指定衣物。

    参数:
    - name: 衣物名称

    例: remove_clothing("白色短袖T恤")
    """
    items = load_wardrobe()
    before = len(items)
    items = [it for it in items if name not in it.get("name", "")]
    save_wardrobe(items)
    deleted = before - len(items)
    if deleted > 0:
        return f"已删除 {deleted} 件衣物。"
    return f"未找到 '{name}'。"


if __name__ == "__main__":
    mcp.run()
