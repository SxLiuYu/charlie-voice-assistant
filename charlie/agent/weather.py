"""Weather fast path: Amap (高德) direct weather query, bypassing LLM.

Extracted from voice_agent.py. Handles weather keyword detection and
direct Amap API calls to save 2 LLM round-trips.
"""
import os, logging, re

import requests

log = logging.getLogger("magic")

_CITY_RE = re.compile(r'([\u4e00-\u9fa5]{2,4}?)(?:市|省|县|区)?(?:的)?\s*天气')
_NON_CITY_WORDS = {
    "今天", "明天", "后天", "昨天", "前天", "早上", "中午", "晚上", "下午", "现在",
    "这周", "本周", "下周", "一周", "周末", "这星期", "本星期", "未来", "最近",
    "今天天气", "明天天气", "当前", "当地", "本地", "我们这", "这里",
    "看看", "查查", "查下", "一下", "问问", "报下", "报个", "怎样", "如何",
    "早上八点", "早上几点", "上午", "傍晚", "凌晨", "半夜", "夜里",
    "八点", "几点", "点", "分", "小时", "每天", "每日",
}


def _looks_like_time_word(s: str) -> bool:
    """“早上八点”/“八点”/“下午三点”不是城市。"""
    if not s:
        return False
    if s in _NON_CITY_WORDS:
        return True
    if re.search(r'(早上|上午|中午|下午|晚上|傍晚|凌晨|半夜|夜里|[零一二两三四五六七八九十\d]+点)', s):
        return True
    return False
# 常见城市表：优先于正则，避免 "北京这周天气" 被吃成城市 "北京这周"
_KNOWN_CITIES = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安",
    "重庆", "苏州", "天津", "厦门", "青岛", "大连", "哈尔滨", "昆明", "拉萨",
    "长沙", "郑州", "沈阳", "福州", "合肥", "济南", "南宁", "贵阳", "兰州",
    "太原", "石家庄", "南昌", "长春", "银川", "西宁", "乌鲁木齐", "呼和浩特",
    "海口", "三亚", "珠海", "佛山", "东莞", "宁波", "无锡", "常州", "温州",
    "金华", "绍兴", "嘉兴", "泉州", "烟台", "潍坊", "洛阳", "宜昌", "岳阳",
    "徐州", "扬州", "南通", "无锡", "保定", "唐山", "淄博", "临沂", "泰安",
)
_KNOWN_CITIES = tuple(sorted(set(_KNOWN_CITIES), key=len, reverse=True))
_CITY_ADCODE = {
    "北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
    "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
    "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120000",
}


def _extract_city(text: str) -> str:
    text = text or ""
    for c in _KNOWN_CITIES:
        if c in text:
            return c
    m = _CITY_RE.search(text)
    if m:
        city = m.group(1)
        for w in ("这周", "本周", "下周", "周末", "今天", "明天", "后天", "昨天", "现在"):
            city = city.replace(w, "")
        city = city.strip()
        if (city and city not in _NON_CITY_WORDS and len(city) >= 2
                and not _looks_like_time_word(city)):
            return city
    # 城市+时间词（"厦门明天会下雨吗" 没有"天气"二字）
    m2 = re.search(
        r'([\u4e00-\u9fa5]{2,4}?)(?=今天|明天|后天|这周|本周|下周|周末|会下雨|下雨|几度|温度|气温|天气)',
        text)
    if m2:
        city = m2.group(1)
        if (city and city not in _NON_CITY_WORDS and len(city) >= 2
                and not _looks_like_time_word(city)):
            return city
    # 未匹配城市时用 IP 定位获取当前位置，避免外地用户收到北京天气
    try:
        from app.geo import locate
        loc = locate()
        if loc and loc.get("city"):
            return loc["city"]
    except Exception as e:
        log.debug(f"[agent.weather] error at line 33: {e}")
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
    except Exception as e:
        log.debug(f"[agent.weather] error at line 47: {e}")
        return None


def direct_weather_play(text: str) -> str:
    """天气关键词命中时直接查询天气，绕过 LLM，省掉 2 次 ARK 往返。
    优先调用 app.weather.get_weather_text()（Open-Meteo 免费优先 → AMAP 兜底），
    支持任意城市。返回自然语言天气摘要，或空字符串(失败/非天气问题时回退 brain)。"""
    city = _extract_city(text or "")
    try:
        from app.weather import get_weather_text, get_weather_week_text
        is_week = any(k in (text or "") for k in (
            "这周", "本周", "一周", "下周", "未来几天", "未来一周",
            "七天", "7天", "这星期", "本星期", "未来7天"))
        if is_week:
            reply = get_weather_week_text(city)
            if reply:
                log.info(f"[weather] 周报快路径成功[{city}]: {reply[:60]}")
                return reply
            log.warning(f"[weather] 周报快路径无结果[{city}]，回退今日")
        day_offset = 0
        if "后天" in text:
            day_offset = 2
        elif "明天" in text or "次日" in text:
            day_offset = 1
        reply = get_weather_text(city, day_offset=day_offset)
        if reply:
            log.info(f"[weather] 快路径成功[{city}]: {reply[:60]}")
            return reply
        log.warning(f"[weather] 快路径无结果[{city}]")
        return ""
    except Exception as e:
        log.warning(f"[weather] 快路径失败[{city}]: {e}")
        return ""
