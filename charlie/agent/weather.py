"""Weather fast path: Amap (高德) direct weather query, bypassing LLM.

Extracted from voice_agent.py. Handles weather keyword detection and
direct Amap API calls to save 2 LLM round-trips.
"""
import os, logging, re

import requests

log = logging.getLogger("magic")

_CITY_RE = re.compile(r'([\u4e00-\u9fa5]{2,4}(?:市)?)\s*天气')
_NON_CITY_WORDS = {"今天", "明天", "后天", "昨天", "前天", "早上", "中午", "晚上", "下午", "现在"}
_CITY_ADCODE = {
    "北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
    "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
    "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000",
}


def _extract_city(text: str) -> str:
    m = _CITY_RE.search(text)
    if m:
        city = m.group(1)
        if city not in _NON_CITY_WORDS:
            return city
    # 未匹配城市时用 IP 定位获取当前位置，避免外地用户收到北京天气
    try:
        from app.geo import locate
        loc = locate()
        if loc and loc.get("city"):
            return loc["city"]
    except Exception:
        pass
    return "北京"


def _city_to_adcode(city: str, amap_key: str) -> str | None:
    if city in _CITY_ADCODE:
        return _CITY_ADCODE[city]
    try:
        geo = requests.get(
            "https://restapi.amap.com/v3/geocode/geo",
            params={"address": city, "key": amap_key}, timeout=5,
        ).json()
        return geo.get("geocodes", [{}])[0].get("adcode")
    except Exception:
        return None


def direct_weather_play(text: str) -> str:
    """天气关键词命中时直接查询天气，绕过 LLM，省掉 2 次 ARK 往返。
    优先调用 app.weather.get_weather_text()（Open-Meteo 免费优先 → AMAP 兜底），
    支持任意城市。返回自然语言天气摘要，或空字符串(失败/非天气问题时回退 brain)。"""
    city = _extract_city(text or "")
    try:
        from app.weather import get_weather_text
        reply = get_weather_text(city)
        if reply:
            log.info(f"[weather] 快路径成功[{city}]: {reply[:60]}")
            return reply
        log.warning(f"[weather] 快路径无结果[{city}]")
        return ""
    except Exception as e:
        log.warning(f"[weather] 快路径失败[{city}]: {e}")
        return ""
