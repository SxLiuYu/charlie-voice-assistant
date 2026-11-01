"""统一天气查询 — AMAP 优先，Open-Meteo 兜底（免费无 Key）

被 magic-info.get_detailed_weather / voice_server._get_weather / magic-wardrobe 共用。
Open-Meteo: https://open-meteo.com/ （无需注册，无限制，CORS 友好）
"""
import copy
import datetime
import json
import os
import logging
import threading
import time

import requests

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

# AMAP 配额超限熔断状态（跨重启持久化到 data/weather_state.json）。
# 当高德接口返回配额耗尽时，在 backoff 秒内直接跳过 AMAP，避免无意义重试。
_AMAP_QUOTA_BACKOFF: float = float(
    os.getenv("AMAP_QUOTA_BACKOFF_HOURS", "4") or 4) * 3600
_WEATHER_STATE_FILE = os.path.join(
    os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__))),
    "weather_state.json",
)
_amap_blocked_until: float = 0.0
_amap_state_lock = threading.Lock()


def _read_amap_state():
    try:
        with open(_WEATHER_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        blocked = float(data.get("amap_blocked_until", 0) or 0)
        if blocked > time.time():
            return blocked
    except Exception as e:
        log.debug(f"[weather] 读取 AMAP 状态失败: {e}")
    return 0.0


def _write_amap_state(blocked_until: float) -> None:
    try:
        with _amap_state_lock, open(_WEATHER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"amap_blocked_until": blocked_until}, f)
    except Exception as e:
        log.debug(f"[weather] 写入 AMAP 状态失败: {e}")


_amap_blocked_until = _read_amap_state()

# 高德接口中表示配额耗尽/超限的 infocode 集合。
_AMAP_QUOTA_INFCODES = {"10044", "10021"}

# ===== Weather TTL cache =====
# Weather data changes on the order of minutes; cache per city to avoid
# hitting AMAP(quota-blocked) → Open-Meteo(~2s) on every "天气咋样" query.
_WEATHER_CACHE_TTL = float(os.getenv("WEATHER_CACHE_TTL_SECONDS", "600"))
_WEATHER_CACHE_MAX = int(os.getenv("WEATHER_CACHE_MAX", "32"))
_weather_cache_lock = threading.Lock()
_weather_cache: dict[str, tuple[dict, float]] = {}
_casts_cache_lock = threading.Lock()
_casts_cache: dict[str, tuple[list[dict], float]] = {}


def _evict_cache(cache: dict, now: float, ttl: float) -> None:
    """Remove expired entries, then evict oldest if still over _WEATHER_CACHE_MAX."""
    # Remove expired entries
    expired = [k for k, (_, ts) in cache.items() if now - ts > ttl]
    for k in expired:
        cache.pop(k, None)
    # Evict oldest if still over limit
    while len(cache) > _WEATHER_CACHE_MAX:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)


def _wmo_to_text(code: int) -> str:
    return _WMO_DESC.get(code, "未知")


def _is_amap_quota_error(resp: dict) -> bool:
    """判断高德响应是否属于配额类错误（命中则触发熔断）。"""
    infocode = str(resp.get("infocode", ""))
    if infocode in _AMAP_QUOTA_INFCODES:
        return True
    info = str(resp.get("info", resp.get("msg", ""))).upper()
    if resp.get("status") == "0" and ("OVER_LIMIT" in info or "LIMIT" in info):
        return True
    return False


def _amap_get(city: str, return_casts: bool = False) -> dict | list | None:
    """用高德 API 查天气，返回标准化 dict 或 casts 列表或 None（未配置/失败时）。"""
    global _amap_blocked_until
    amap = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", ""))
    if not amap or amap.startswith("你的"):
        return None
    # 配额熔断：在 backoff 窗口内直接返回 None，不发送请求。
    if time.time() < _amap_blocked_until:
        return None
    city_map = {"北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
                "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
                "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000"}
    adcode = city_map.get(city)
    if not adcode:
        try:
            geo = requests.get("https://restapi.amap.com/v3/geocode/geo",
                params={"address": city, "key": amap}, timeout=5).json()
            if _is_amap_quota_error(geo):
                _amap_blocked_until = time.time() + _AMAP_QUOTA_BACKOFF
                _write_amap_state(_amap_blocked_until)
                log.warning(
                    "[weather] AMAP 配额超限(geocode)，%s 小时内直接走 Open-Meteo",
                    _AMAP_QUOTA_BACKOFF / 3600)
                return None
            adcode = geo.get("geocodes", [{}])[0].get("adcode")
        except Exception:
            adcode = None
    if not adcode:
        return None
    try:
        r = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": adcode, "key": amap, "extensions": "all"}, timeout=10).json()
        if _is_amap_quota_error(r):
            _amap_blocked_until = time.time() + _AMAP_QUOTA_BACKOFF
            _write_amap_state(_amap_blocked_until)
            log.warning(
                "[weather] AMAP 配额超限，%s 小时内直接走 Open-Meteo",
                _AMAP_QUOTA_BACKOFF / 3600)
            return None
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if not casts:
            return None
        if return_casts:
            return casts
        today = casts[0]
        return {
            "city": city,
            "day_weather": today.get("dayweather", ""),
            "night_weather": today.get("nightweather", ""),
            "day_temp": int(float(today.get("daytemp", 20) or 20)),
            "night_temp": int(float(today.get("nighttemp", 20) or 20)),
            "weather_text": today.get("dayweather", ""),
            "source": "amap",
        }
    except Exception as e:
        log.debug(f"[weather] AMAP 失败: {e}")
        return None


_geo_cache = {}  # city -> (lat, lon) | None；进程级，避免每问都打 geocoding API


def _open_meteo_fetch_raw(city: str) -> tuple[dict, dict] | None:
    """Geocode city and fetch Open-Meteo daily + current_weather data.

    Returns (daily_dict, current_weather_dict) or None on failure.
    """
    try:
        lat, lon = _geo_cache.get(city, (None, None))
        if lat is None:
            pinyin_map = {
                "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou",
                "深圳": "Shenzhen", "杭州": "Hangzhou", "成都": "Chengdu",
                "武汉": "Wuhan", "南京": "Nanjing", "西安": "Xian",
                "重庆": "Chongqing", "苏州": "Suzhou", "天津": "Tianjin",
                "厦门": "Xiamen", "青岛": "Qingdao", "大连": "Dalian",
                "哈尔滨": "Harbin", "昆明": "Kunming", "拉萨": "Lhasa",
                "长沙": "Changsha", "郑州": "Zhengzhou", "沈阳": "Shenyang",
                "福州": "Fuzhou", "合肥": "Hefei", "济南": "Jinan",
                "南宁": "Nanning", "贵阳": "Guiyang", "兰州": "Lanzhou",
                "太原": "Taiyuan", "石家庄": "Shijiazhuang", "南昌": "Nanchang",
                "长春": "Changchun", "银川": "Yinchuan", "西宁": "Xining",
            }
            candidates = [city]
            if not city.endswith("市"):
                candidates.append(city + "市")
            en = pinyin_map.get(city)
            if en:
                candidates.append(en)
            results = None
            for name in candidates:
                try:
                    geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": name, "count": 1, "language": "zh"}, timeout=5)
                    results = geo.json().get("results")
                except Exception:
                    results = None
                if results:
                    break
            if not results:
                log.debug(f"[weather] Open-Meteo 找不到城市: {city}")
                _geo_cache[city] = (None, None)
                return None
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            _geo_cache[city] = (lat, lon)

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
        return daily, cw
    except Exception as e:
        log.debug(f"[weather] Open-Meteo 失败: {e}")
        return None


def _build_om_casts(daily: dict) -> list[dict]:
    """Convert Open-Meteo daily arrays to AMAP-style cast dicts."""
    times = daily.get("time", [])
    codes = daily.get("weathercode", [])
    max_temps = daily.get("temperature_2m_max", [])
    min_temps = daily.get("temperature_2m_min", [])
    rain_probs = daily.get("precipitation_probability_max", [])

    # Fallback: if time is missing (unlikely from real API), derive count from
    # the other arrays so single-day dicts still build correctly.
    n = len(times) or len(codes) or len(max_temps) or len(min_temps)
    casts = []
    for i in range(n):
        code = codes[i] if i < len(codes) else 0
        day_temp = max_temps[i] if i < len(max_temps) else 20
        night_temp = min_temps[i] if i < len(min_temps) else 20
        weather_text = _wmo_to_text(code)
        date_str = times[i] if i < len(times) else ""
        casts.append({
            "date": date_str,
            "dayweather": weather_text,
            "nightweather": weather_text,
            "daytemp": str(int(round(day_temp))),
            "nighttemp": str(int(round(night_temp))),
        })
    return casts


def _open_meteo_get(city: str) -> dict | None:
    """用 Open-Meteo 免费API查天气（无需 Key），返回标准化 dict 或 None。"""
    result = _open_meteo_fetch_raw(city)
    if not result:
        return None
    daily, cw = result
    casts = _build_om_casts(daily)
    if not casts:
        return None
    today = casts[0]
    _raw_rain = daily.get("precipitation_probability_max", [0])[0]
    rain_prob = int(round(float(_raw_rain))) if _raw_rain else 0
    return {
        "city": city,
        "day_weather": today["dayweather"],
        "night_weather": today["nightweather"],
        "day_temp": int(float(today["daytemp"])),
        "night_temp": int(float(today["nighttemp"])),
        "weather_text": today["dayweather"],
        "current_temp": int(round(float(cw.get("temperature", 20)))),
        "current_weather": _wmo_to_text(cw.get("weathercode", 0)),
        "rain_prob": rain_prob,
        "source": "open-meteo",
    }


def _open_meteo_get_casts(city: str) -> list[dict] | None:
    """用 Open-Meteo 免费API查天气，返回多日 casts 列表（AMAP 格式）。"""
    result = _open_meteo_fetch_raw(city)
    if not result:
        return None
    daily, _ = result
    return _build_om_casts(daily)


def get_weather(city: str = "北京") -> dict:
    """统一天气入口：AMAP 优先（0.1s, 国内低延迟）→ Open-Meteo 兜底 → 默认值。

    返回标准化 dict:
        city, day_weather, night_weather, day_temp, night_temp,
        weather_text, current_temp (可能缺), current_weather (可能缺),
        rain_prob (可能缺), source
    """
    city = (city or "北京").strip() or "北京"
    now = time.time()
    with _weather_cache_lock:
        entry = _weather_cache.get(city)
        if entry is not None:
            data, ts = entry
            if now - ts <= _WEATHER_CACHE_TTL:
                log.debug("[weather] cache hit: %s (age=%.0fs)", city, now - ts)
                return copy.deepcopy(data)
            else:
                _weather_cache.pop(city, None)
    w = _amap_get(city)
    if w:
        with _weather_cache_lock:
            _weather_cache[city] = (w, now)
            _evict_cache(_weather_cache, now, _WEATHER_CACHE_TTL)
        return copy.deepcopy(w)
    log.info(f"[weather] AMAP 失败，降级 Open-Meteo: {city}")
    w = _open_meteo_get(city)
    if w:
        with _weather_cache_lock:
            _weather_cache[city] = (w, now)
            _evict_cache(_weather_cache, now, _WEATHER_CACHE_TTL)
        return copy.deepcopy(w)
    # 最终兜底
    result = {"city": city, "day_weather": "晴", "night_weather": "晴",
              "day_temp": 20, "night_temp": 20, "weather_text": "晴", "source": "default"}
    return copy.deepcopy(result)


def get_weather_text(city: str = "北京", detail: bool = False, day_offset: int = 0) -> str:
    """返回格式化天气文本，供 voice_agent / TTS 直接使用。

    detail=False（默认）：语音短答——"城市今天X，A到B度"+ 伞提示，~3 秒播完。
    detail=True：完整版（实时温度 + 穿衣建议），供晨报/飞书等文本场景。
    day_offset: 0=今天, 1=明天, 2=后天（Open-Meteo/AMAP 都有逐日预报）。
    """
    if day_offset > 0:
        try:
            casts = get_weather_casts(city)
            if casts and len(casts) > day_offset:
                c = casts[day_offset]
                label = {1: "明天", 2: "后天"}.get(day_offset, c.get("date") or "那天")
                dt = int(float(c.get("daytemp", 20)))
                nt = int(float(c.get("nighttemp", 20)))
                wx = c.get("dayweather") or c.get("day_weather") or "晴"
                bits = [f"{city}{label}{wx}，{nt}到{dt}度"]
                if "雨" in wx:
                    bits.append("记得带伞")
                elif "雪" in wx:
                    bits.append("注意防滑")
                return "。".join(bits) + "。"
        except Exception as e:
            log.debug(f"[weather] day_offset 查询失败: {e}")
        # 预报拿不到 → 落回今天，别报错
    w = get_weather(city)
    if w.get("source") == "default":
        return f"{city}天气数据暂时不可用，请稍后再试。"
    day = w.get("day_temp", 20)
    night = w.get("night_temp", 20)
    weather = w.get("weather_text", "晴")
    cur = w.get("current_temp")
    cur_w = w.get("current_weather", "")

    # 实时与预报矛盾处理：预报明确有雨雪而实时未反映时，实时字段标注
    # "可能未更新"（06:07 生产案例：预报小雨/实时阴，用户当场纠正"已经下雨了"）
    _wet = ("雨" in weather) or ("雪" in weather)
    _cur_wet = cur_w and (("雨" in cur_w) or ("雪" in cur_w))
    if _wet and cur is not None and cur_w and not _cur_wet:
        cur_text = f"实时报{cur_w}（可能未更新）"
    else:
        cur_text = f"实时{cur_w}，{cur}度" if (cur is not None and cur_w) else ""

    if not detail:
        # 语音短答压到 3s 内：去掉冗余的"今天"（语境已是今天），伞/滑提示
        # 并入同一句用逗号连读。实测 TTS_SPEED=1.3 下
        # "北京今天阴，5到16度。" 2.60s → "北京阴，5到16度。" 1.94s；
        # 雨雪句 "北京小雨，5到16度，带伞。" 3.00s（原来 3.74s）。
        parts = [f"{city}{weather}，{night}到{day}度"]
        rain = w.get("rain_prob", 0)
        if (rain and rain >= 50) or "雨" in weather:
            parts.append("带伞")
        elif "雪" in weather:
            parts.append("路滑")
        # 26 度穿衣法：说清"现在这个温度该穿什么"。
        # 有实时温度就用实时（用户此刻体感），否则退回白天预报温度。
        _wear_temp = cur if cur is not None else day
        try:
            _outfit, _feel = outfit_for_26(_wear_temp, short=True)
            parts.append(f"穿{_outfit}")
        except Exception as e:
            log.debug(f"[weather] 26度穿衣法失败: {e}")
        if _wet and cur_text.startswith("实时报"):
            parts.append(cur_text)  # 仅矛盾提示时补充，避免首句超长
        return "，".join(parts) + "。"

    parts = [f"{city}今天{weather}，{night}到{day}度"]
    if cur_text:
        parts.append(cur_text)

    rain = w.get("rain_prob", 0)
    if rain and rain >= 50:
        parts.append("今天降雨概率较高，记得带伞")
    elif "雨" in weather:
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


_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


# ===== 26 度穿衣法 =====
# 公式：气温 + 各层衣物"保暖温度"之和 ≈ 26℃ 时体感最舒适。
# 每件衣物标注的是它能等效补偿的温差（不是它的标称厚度），
# 所以按当前气温逐件挑选，凑到 26 就停。
CLOTHING_TEMP = {
    # 上衣
    "短袖": 1, "薄衬衫": 2, "长袖T恤": 2, "薄卫衣": 3, "衬衫加薄外套": 4,
    "毛衣": 4, "卫衣加外套": 5, "夹克": 4, "风衣": 4, "薄羽绒服": 6,
    "厚毛衣": 6, "羽绒服": 8, "厚羽绒服": 10,
    # 下装
    "短裤": 1, "薄长裤": 2, "牛仔裤": 3, "运动裤": 3, "加绒裤": 5, "保暖裤": 7,
    # 外层补充
    "围巾": 1, "帽子": 1,
}

# 由热到冷的候选穿搭：(完整名, 语音短名, 衣物层)。
# 顺序必须单调递增保暖值（越往后越暖），挑第一个能满足 26 度的。
# 相邻档只差 1-3 度，避免"20 度直接跳到体感 28"这种过冲。
# 语音短名是为了控制句长：中文 TTS ≈0.2s/字，一句"天气+温度+穿搭"
# 最好压在 16 字内（≈3.2s），所以简写成"夹克薄卫衣"而不是
# "夹克加薄卫衣加牛仔裤"。
_CLOTHING_OUTFITS = (
    ("短袖短裤", "短袖", ("短袖", "短裤")),                        # +2
    ("短袖长裤", "短袖", ("短袖", "薄长裤")),                      # +3
    ("短袖牛仔裤", "短袖", ("短袖", "牛仔裤")),                    # +4
    ("长袖T恤牛仔裤", "长袖", ("长袖T恤", "牛仔裤")),               # +5
    ("薄卫衣牛仔裤", "薄卫衣", ("薄卫衣", "牛仔裤")),                # +6
    ("卫衣外套", "卫衣外套", ("卫衣加外套", "牛仔裤")),             # +8
    ("夹克薄卫衣", "夹克加薄卫衣", ("夹克", "薄卫衣", "牛仔裤")),       # +9
    ("夹克毛衣", "夹克加毛衣", ("夹克", "毛衣", "牛仔裤")),           # +11
    ("薄羽绒加绒裤", "薄羽绒服", ("薄羽绒服", "加绒裤")),           # +11
    ("薄羽绒加绒裤围巾", "薄羽绒服", ("薄羽绒服", "加绒裤", "围巾")), # +12
    ("风衣毛衣加绒裤", "风衣加毛衣", ("风衣", "毛衣", "加绒裤")),      # +13
    ("薄羽绒保暖裤", "薄羽绒服", ("薄羽绒服", "保暖裤")),           # +13
    ("风衣厚毛衣加绒裤", "风衣加厚毛衣", ("风衣", "厚毛衣", "加绒裤")), # +15
    ("羽绒服保暖裤", "羽绒服", ("羽绒服", "保暖裤")),              # +15
    ("厚羽绒保暖裤", "厚羽绒服", ("厚羽绒服", "保暖裤")),           # +17
    ("厚羽绒保暖裤围巾", "厚羽绒服", ("厚羽绒服", "保暖裤", "围巾")), # +18
)

COMFORT_TARGET = 26


def outfit_for_26(current_temp: float, short: bool = False) -> tuple[str, float]:
    """按 26 度穿衣法给当前气温挑一套衣服。

    返回 (搭配名, 体感温度)。体感 = 气温 + 衣物保暖值之和。
    short=True 返回适合语音播报的短名（"夹克薄卫衣"而非"夹克薄卫衣牛仔裤"）。
    """
    gap = COMFORT_TARGET - float(current_temp)
    if gap <= 0:
        # 气温本身就 ≥26：不需要任何保暖层
        return ("短袖" if short else "短袖短裤", float(current_temp))
    best = _CLOTHING_OUTFITS[-1]
    for item in _CLOTHING_OUTFITS:
        layers = item[2]
        if sum(CLOTHING_TEMP.get(layer, 0) for layer in layers) >= gap:
            best = item
            break
    feel = float(current_temp) + sum(CLOTHING_TEMP.get(l, 0) for l in best[2])
    return ((best[1] if short else best[0]), feel)


def get_weather_week_text(city: str = "北京") -> str:
    """未来 7 天语音周报（Open-Meteo daily 已有 7 天，无需额外 Key）。

    天猫精灵风格：前 4 天逐日 + 一周温度范围 + 雨/雪提示，控制在 2-3 句。
    失败返回空串，调用方回退今日天气。
    """
    try:
        casts = get_weather_casts(city)
    except Exception as e:
        log.debug(f"[weather] 周报 casts 失败: {e}")
        return ""
    if not casts:
        return ""
    days = casts[:7]
    daytemps: list[int] = []
    nighttemps: list[int] = []
    rain_labels: list[str] = []
    snow_labels: list[str] = []
    pieces: list[str] = []
    for i, c in enumerate(days):
        try:
            hi = int(float(c.get("daytemp", 20)))
            lo = int(float(c.get("nighttemp", 20)))
        except Exception:
            hi, lo = 20, 10
        daytemps.append(hi)
        nighttemps.append(lo)
        wx = (c.get("dayweather") or c.get("day_weather") or "晴").strip()
        date = c.get("date") or ""
        if i == 0:
            label = "今天"
        elif i == 1:
            label = "明天"
        else:
            try:
                label = _WEEKDAY_NAMES[datetime.date.fromisoformat(date).weekday()]
            except Exception:
                label = f"{i + 1}号"
        pieces.append(f"{label}{wx}{lo}到{hi}度")
        if "雨" in wx:
            rain_labels.append(label)
        if "雪" in wx:
            snow_labels.append(label)
    if not pieces:
        return ""
    lo_all = min(nighttemps) if nighttemps else 10
    hi_all = max(daytemps) if daytemps else 20
    spoken = "；".join(pieces[:4])
    if len(pieces) > 4:
        spoken += f"；之后{pieces[-1]}"
    if snow_labels:
        tail = f"{'、'.join(snow_labels[:3])}有雪，注意防滑保暖"
    elif rain_labels:
        tail = f"{'、'.join(rain_labels[:3])}有雨，记得带伞"
    else:
        tail = "一周基本无雨"
    return f"{city}{spoken}。整周气温{lo_all}到{hi_all}度，{tail}。"


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


def get_weather_casts(city: str = "北京") -> list[dict]:
    """返回 AMAP 风格的 casts 列表（多日预报），带 TTL 缓存。

    优先走 AMAP（extensions=all 返回 7 日 casts），降级 Open-Meteo 转 casts。
    空列表表示数据不可用（不抛异常）。
    """
    city = (city or "北京").strip() or "北京"
    now = time.time()
    with _casts_cache_lock:
        entry = _casts_cache.get(city)
        if entry is not None:
            data, ts = entry
            if now - ts <= _WEATHER_CACHE_TTL:
                log.debug("[weather] casts cache hit: %s (age=%.0fs)", city, now - ts)
                return copy.deepcopy(data)
            else:
                _casts_cache.pop(city, None)
    casts = _amap_get(city, return_casts=True)
    if casts:
        with _casts_cache_lock:
            _casts_cache[city] = (casts, now)
            _evict_cache(_casts_cache, now, _WEATHER_CACHE_TTL)
        return copy.deepcopy(casts)
    log.info(f"[weather] AMAP casts 失败，降级 Open-Meteo: {city}")
    casts = _open_meteo_get_casts(city)
    if casts:
        with _casts_cache_lock:
            _casts_cache[city] = (casts, now)
            _evict_cache(_casts_cache, now, _WEATHER_CACHE_TTL)
        return copy.deepcopy(casts)
    return []
