"""Weather fast path: Amap (高德) direct weather query, bypassing LLM.

Extracted from voice_agent.py. Handles weather keyword detection and
direct Amap API calls to save 2 LLM round-trips.
"""
import os, logging, requests

log = logging.getLogger("magic")


def direct_weather_play(text: str) -> str:
    """天气关键词命中时直接调用高德接口，绕过 LLM，省掉 2 次 ARK 往返。
    返回自然语言天气摘要，或空字符串(失败/非天气问题时回退 brain)。"""
    AMAP = os.getenv("AMAP_KEY", "")
    if not AMAP:
        return ""
    try:
        r = requests.get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": "110000", "key": AMAP, "extensions": "all"},
            timeout=8,
        ).json()
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if not casts:
            return ""
        today = casts[0]
        day_w = today.get("dayweather", "")
        night_w = today.get("nightweather", "")
        day_t = today.get("daytemp", "")
        night_t = today.get("nighttemp", "")
        parts = []
        for w in (day_w, night_w):
            if w and w not in parts:
                parts.append(w)
        weather = "转".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
        if not weather:
            return ""
        reply = f"今天{weather}，{day_t}到{night_t}度。"
        log.info(f"[weather] 高德直连成功: {reply}")
        return reply
    except Exception as e:
        log.warning(f"[weather] 高德直连失败: {e}")
        return ""
