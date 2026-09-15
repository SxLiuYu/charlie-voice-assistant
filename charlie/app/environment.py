"""贾维斯环境感知 — 空气质量/紫外线/地震预警

Open-Meteo Air Quality API（免费无Key）+ USGS Earthquake API
"""
import datetime, logging, requests

log = logging.getLogger("magic")

# 中国主要城市经纬度
_CITY_COORDS = {
    "北京": (39.9, 116.4), "上海": (31.2, 121.5), "广州": (23.1, 113.3),
    "深圳": (22.5, 114.1), "杭州": (30.3, 120.2), "成都": (30.6, 104.1),
    "武汉": (30.6, 114.3), "南京": (32.1, 118.8), "西安": (34.3, 108.9),
    "重庆": (29.6, 106.5), "天津": (39.1, 117.2), "苏州": (31.3, 120.6),
}


def _get_coords(city: str) -> tuple[float, float]:
    if city in _CITY_COORDS:
        return _CITY_COORDS[city]
    # 降级 Open-Meteo geocoding
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "zh"}, timeout=5)
        results = r.json().get("results")
        if results:
            return results[0]["latitude"], results[0]["longitude"]
    except Exception as e:
        log.debug(f"[app.environment] error at line 28: {e}")
        pass
    return 39.9, 116.4  # 默认北京


def get_air_quality(city: str = "北京") -> dict:
    """获取空气质量数据（PM2.5/PM10/AQI/UV）"""
    lat, lon = _get_coords(city)
    try:
        r = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality",
            params={
                "latitude": lat, "longitude": lon,
                "current": "pm2_5,pm10,us_aqi,uv_index",
                "timezone": "Asia/Shanghai",
            }, timeout=10)
        cw = r.json().get("current", {})
        aqi = cw.get("us_aqi", 0)
        # AQI 等级
        if aqi <= 50:
            level = "优"
        elif aqi <= 100:
            level = "良"
        elif aqi <= 150:
            level = "轻度污染"
        elif aqi <= 200:
            level = "中度污染"
        elif aqi <= 300:
            level = "重度污染"
        else:
            level = "严重污染"
        uv = cw.get("uv_index", 0)
        if uv <= 2:
            uv_level = "低"
        elif uv <= 5:
            uv_level = "中等"
        elif uv <= 7:
            uv_level = "强"
        elif uv <= 10:
            uv_level = "很强"
        else:
            uv_level = "极强"
        return {
            "city": city,
            "pm25": cw.get("pm2_5", 0),
            "pm10": cw.get("pm10", 0),
            "aqi": aqi,
            "aqi_level": level,
            "uv_index": uv,
            "uv_level": uv_level,
        }
    except Exception as e:
        log.debug(f"[jarvis] 空气质量获取失败: {e}")
    return {"city": city, "pm25": 0, "pm10": 0, "aqi": 0, "aqi_level": "未知", "uv_index": 0, "uv_level": "未知"}


def get_air_quality_text(city: str = "北京") -> str:
    """格式化空气质量输出"""
    aq = get_air_quality(city)
    parts = [f"{city}空气质量：AQI {aq['aqi']}（{aq['aqi_level']}）"]
    if aq.get("pm25"):
        parts.append(f"PM2.5: {aq['pm25']:.0f}μg/m³")
    if aq.get("pm10"):
        parts.append(f"PM10: {aq['pm10']:.0f}μg/m³")
    if aq.get("uv_index") is not None:
        parts.append(f"紫外线: {aq['uv_index']:.1f}（{aq['uv_level']}）")
        if aq["uv_index"] >= 6:
            parts.append("建议防晒")
    if aq["aqi"] > 100:
        parts.append("建议减少户外活动")
    return "，".join(parts) + "。"


def get_earthquake(min_magnitude: float = 4.5) -> list[dict]:
    """获取近期地震（USGS，24小时内≥指定震级）"""
    try:
        r = requests.get(
            f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{min_magnitude:.1f}_day.geojson",
            timeout=10,
        )
        quakes = []
        for q in r.json().get("features", []):
            props = q["properties"]
            coords = q["geometry"]["coordinates"]
            quakes.append({
                "magnitude": props.get("mag", 0),
                "place": props.get("place", "未知"),
                "time": datetime.datetime.fromtimestamp(
                    props.get("time", 0) / 1000
                ).strftime("%Y-%m-%d %H:%M"),
                "lat": coords[1], "lon": coords[0],
                "tsunami": props.get("tsunami", 0) == 1,
            })
        quakes.sort(key=lambda x: x["magnitude"], reverse=True)
        return quakes
    except Exception as e:
        log.debug(f"[jarvis] 地震数据获取失败: {e}")
    return []


def get_earthquake_text(min_magnitude: float = 4.5, count: int = 5) -> str:
    """格式化地震预警输出"""
    quakes = get_earthquake(min_magnitude)
    if not quakes:
        return f"近24小时无≥{min_magnitude}级地震记录"
    lines = [f"近24小时≥{min_magnitude}级地震（{len(quakes)}次）："]
    for q in quakes[:count]:
        tsunami = " ⚠️海啸预警" if q["tsunami"] else ""
        lines.append(f"  M{q['magnitude']:.1f} {q['place']} ({q['time']}){tsunami}")
    return "\n".join(lines)
