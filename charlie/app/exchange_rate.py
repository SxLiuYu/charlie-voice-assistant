"""汇率查询 — open.er-api.com 免费API（无需 Key）

https://open.er-api.com/v6/latest/{base}
166 种货币，每小时更新。
"""
import logging, requests

log = logging.getLogger("magic")
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600  # 1小时
_CACHE_MAX = 50  # 最多缓存 50 种货币


def get_rates(base: str = "USD") -> dict:
    """获取指定基准货币的汇率表"""
    import time
    base = base.upper()
    now = time.time()
    if base in _cache and now - _cache[base][0] < _CACHE_TTL:
        return _cache[base][1]
    try:
        r = requests.get(f"https://open.er-api.com/v6/latest/{base}", timeout=10)
        data = r.json()
        rates = data.get("rates", {})
        if rates:
            _cache[base] = (now, rates)
            if len(_cache) > _CACHE_MAX:
                _cache.clear()
            return rates
    except Exception as e:
        log.debug(f"[exchange] 获取汇率失败: {e}")
    return {}


def convert(amount: float, from_cur: str, to_cur: str) -> float | None:
    """汇率换算：amount 从 from_cur 到 to_cur"""
    from_cur = from_cur.upper()
    to_cur = to_cur.upper()
    if from_cur == to_cur:
        return amount
    rates = get_rates(from_cur)
    rate = rates.get(to_cur)
    if rate:
        return round(amount * rate, 2)
    return None


# 货币中文名映射
_CURRENCY_CN = {
    "CNY": "人民币", "USD": "美元", "EUR": "欧元", "JPY": "日元",
    "GBP": "英镑", "KRW": "韩元", "HKD": "港币", "TWD": "新台币",
    "SGD": "新加坡元", "AUD": "澳元", "CAD": "加元", "RUB": "卢布",
    "THB": "泰铢", "INR": "印度卢比", "CHF": "瑞士法郎",
}


def currency_name(code: str) -> str:
    """货币代码 → 中文名"""
    return _CURRENCY_CN.get(code.upper(), code.upper())
