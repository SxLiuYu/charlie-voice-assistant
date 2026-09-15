"""假日查询 — Nager.Date 免费API（无需 Key）

https://date.nager.at/api/v3/PublicHolidays/{year}/{countryCode}
缓存按年缓存，避免重复请求。
"""
import datetime, logging, requests

log = logging.getLogger("magic")

# 按年缓存: {year: [holiday_dict, ...]}
_cache: dict[int, list[dict]] = {}
_CACHE_MAX = 20  # 最多缓存 20 年的假日数据
# 默认国家码
_DEFAULT_COUNTRY = "CN"


def _fetch_holidays(year: int, country: str = _DEFAULT_COUNTRY) -> list[dict]:
    """从 Nager.Date 获取指定年份的公共假日"""
    try:
        r = requests.get(
            f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country}",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            log.debug(f"[holiday] {country} {year} 假日数: {len(data)}")
            return data
    except Exception as e:
        log.debug(f"[holiday] 获取 {year}/{country} 失败: {e}")
    return []


def get_holidays(year: int = None, country: str = _DEFAULT_COUNTRY) -> list[dict]:
    """获取指定年份的公共假日列表（带缓存）

    返回 [{"date": "2026-01-01", "localName": "元旦", "name": "New Year's Day", ...}, ...]
    """
    if year is None:
        year = datetime.datetime.now().year
    cache_key = year
    if cache_key not in _cache:
        if len(_cache) > _CACHE_MAX:
            _cache.clear()
        _cache[cache_key] = _fetch_holidays(year, country)
    return _cache[cache_key]


def is_holiday(date: datetime.date = None, country: str = _DEFAULT_COUNTRY) -> bool:
    """判断指定日期是否为公共假日"""
    if date is None:
        date = datetime.date.today()
    holidays = get_holidays(date.year, country)
    date_str = date.isoformat()
    return any(h.get("date") == date_str for h in holidays)


def get_holiday_name(date: datetime.date = None, country: str = _DEFAULT_COUNTRY) -> str:
    """返回指定日期的假日名称（非假日返回空串）"""
    if date is None:
        date = datetime.date.today()
    holidays = get_holidays(date.year, country)
    date_str = date.isoformat()
    for h in holidays:
        if h.get("date") == date_str:
            return h.get("localName", h.get("name", ""))
    return ""


def is_workday() -> bool:
    """今天是否为工作日（非公共假日）"""
    return not is_holiday()
