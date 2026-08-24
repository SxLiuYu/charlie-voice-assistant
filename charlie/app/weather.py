"""统一天气查询 — AMAP 优先，Open-Meteo 兜底（免费无 Key）

被 magic-info.get_detailed_weather / voice_server._get_weather / magic-wardrobe 共用。
Open-Meteo: https://open-meteo.com/ （无需注册，无限制，CORS 友好）
"""
import os, logging, requests

log = logging.getLogger("magic")

# WMO 天气码 → 中文描述
_WMO_DESC = {
    0: "晴", 1: "晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "阵雪",
    80: "阵雨", 81: "中阵雨", 82: "大阵雨",
    85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨", 99: "雷阵雨",
}


def _wmo_to_text(code: int) -> str:
    return _WMO_DESC.get(code, "未知")


def _amap_get(city: str) -> dict | None:
    """用高德 API 查天气，返回标准化 dict 或 None（未配置/失败时）。"""
    amap = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    if not amap or amap.startswith("你的"):
        return None
    city_map = {"北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
                "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
                "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000"}
    adcode = city_map.get(city)
    if not adcode:
        try:
            geo = requests.get("https://restapi.amap.com/v3/geocode/geo",
                params={"address": city, "key": amap}, timeout=5).json()
            adcode = geo.get("geocodes", [{}])[0].get("adcode")
        except Exception:
            adcode = None
    if not adcode:
        return None
    try:
        r = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": adcode, "key": amap, "extensions": "all"}, timeout=10).json()
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if not casts:
            return None
        today = casts[0]
        return {
            "city": city,
            "day_weather": today.get("dayweather", ""),
            "night_weather": today.get("nightweather", ""),
            "day_temp": int(today.get("daytemp", 20) or 20),
            "night_temp": int(today.get("nighttemp", 20) or 20),
            "weather_text": today.get("dayweather", ""),
            "source": "amap",
        }
    except Exception as e:
        log.debug(f"[weather] AMAP 失败: {e}")
        return None


def _open_meteo_get(city: str) -> dict | None:
    """用 Open-Meteo 免费API查天气（无需 Key），返回标准化 dict 或 None。"""
    try:
        # 1. 城市名 → 经纬度
        geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "zh"}, timeout=5)
        results = geo.json().get("results")
        if not results:
            # 尝试英文拼音
            pinyin_map = {"北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou",
                          "深圳": "Shenzhen", "杭州": "Hangzhou", "成都": "Chengdu",
                          "武汉": "Wuhan", "南京": "Nanjing", "西安": "Xian",
                          "重庆": "Chongqing", "苏州": "Suzhou", "天津": "Tianjin"}
            en = pinyin_map.get(city, city)
            geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                params={"name": en, "count": 1}, timeout=5)
            results = geo.json().get("results")
        if not results:
            log.debug(f"[weather] Open-Meteo 找不到城市: {city}")
            return None
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]

        # 2. 获取天气预报
        r = requests.get("https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "current_weather": True,
                "timezone": "Asia/Shanghai",
            }, timeout=10)
        data = r.json()
        daily = data.get("daily", {})
        cw = data.get("current_weather", {})
        if not daily or not cw:
            return None

        code = daily.get("weathercode", [0])[0]
        weather_text = _wmo_to_text(code)
        day_temp = int(daily.get("temperature_2m_max", [20])[0])
        night_temp = int(daily.get("temperature_2m_min", [20])[0])
        rain_prob = daily.get("precipitation_probability_max", [0])[0]

        return {
            "city": city,
            "day_weather": weather_text,
            "night_weather": weather_text,
            "day_temp": day_temp,
            "night_temp": night_temp,
            "weather_text": weather_text,
            "current_temp": int(cw.get("temperature", 20)),
            "current_weather": _wmo_to_text(cw.get("weathercode", 0)),
            "rain_prob": rain_prob,
            "source": "open-meteo",
        }
    except Exception as e:
        log.debug(f"[weather] Open-Meteo 失败: {e}")
        return None


def get_weather(city: str = "北京") -> dict:
    """统一天气入口：Open-Meteo 免费 API 优先 → AMAP 兜底 → 默认值。

    返回标准化 dict:
        city, day_weather, night_weather, day_temp, night_temp,
        weather_text, current_temp (可能缺), current_weather (可能缺),
        rain_prob (可能缺), source
    """
    w = _open_meteo_get(city)
    if w:
        return w
    log.info(f"[weather] Open-Meteo 失败，降级 AMAP: {city}")
    w = _amap_get(city)
    if w:
        return w
    # 最终兜底
    return {"city": city, "day_weather": "晴", "night_weather": "晴",
            "day_temp": 20, "night_temp": 20, "weather_text": "晴", "source": "default"}


def get_weather_text(city: str = "北京") -> str:
    """返回格式化天气文本，供 voice_agent / TTS 直接使用。"""
    w = get_weather(city)
    day = w.get("day_temp", 20)
    night = w.get("night_temp", 20)
    weather = w.get("weather_text", "晴")
    src = w.get("source", "")
    parts = [f"{city}今天{weather}，{night}到{day}度"]

    cur = w.get("current_temp")
    cur_w = w.get("current_weather")
    if cur is not None and cur_w:
        parts.append(f"实时{cur_w}，{cur}度")

    rain = w.get("rain_prob", 0)
    if rain and rain >= 50:
        parts.append("今天降雨概率较高，记得带伞")

    if "雨" in weather:
        parts.append("今天有雨，记得带伞")
    elif "雪" in weather:
        parts.append("今天有雪，注意防滑")

    avg = (day + night) // 2
    if avg >= 28:
        parts.append("天气炎热，穿短袖短裤")
    elif avg >= 22:
        parts.append("薄长袖或短袖都可以")
    elif avg >= 15:
        parts.append("天气凉爽，穿外套或卫衣")
    elif avg >= 8:
        parts.append("穿厚外套或羽绒服")
    else:
        parts.append("穿羽绒服+保暖内衣")

    result = "。".join(parts) + "。"
    return result


def get_weather_alerts(city: str = "北京") -> list[str]:
    """获取天气预警（暴雨/高温/寒潮/大风等）

    使用 Open-Meteo 免费 API 检查极端天气。
    返回预警文本列表，空列表表示无预警。
    """
    alerts = []
    try:
        w = get_weather(city)
        day_temp = w.get("day_temp", 20)
        night_temp = w.get("night_temp", 20)
        weather = w.get("weather_text", "")
        rain = w.get("rain_prob", 0)

        # 高温预警
        if day_temp >= 35:
            alerts.append(f"高温预警：今天最高{day_temp}度，尽量减少外出，注意防暑降温")
        elif day_temp >= 32:
            alerts.append(f"今天较热，最高{day_temp}度，注意补水防晒")

        # 寒潮预警
        if day_temp <= 0:
            alerts.append(f"寒潮预警：今天最高仅{day_temp}度，注意保暖防寒")
        elif day_temp <= 5:
            alerts.append(f"今天很冷，最高{day_temp}度，穿厚外套")

        # 暴雨预警
        if "暴雨" in weather:
            alerts.append("暴雨预警：今天有暴雨，尽量不要外出，注意关好门窗")
        elif "大雨" in weather:
            alerts.append("今天有大雨，出门记得带伞")
        elif rain and rain >= 80:
            alerts.append(f"降雨概率{rain}%，很可能下雨，记得带伞")

        # 大风/雪
        if "雪" in weather and day_temp <= 0:
            alerts.append("今天有雪且气温低，注意路滑保暖")
    except Exception as e:
        log.debug(f"[weather] 预警检查失败: {e}")
    return alerts
