"""个性化推荐主动推送 — "懂你, 推你想看的"

基于 Charlie 记忆/偏好/自进化画像 → 提取用户兴趣 → 抖音热搜/新闻 → ARK LLM 智能筛选 → 推飞书。
定时(每4h)推送3条"你可能感兴趣的热点", 结合用户兴趣画像给推荐理由。

数据源:
- evolution_data.json (learned_preferences: 常用功能频次)
- episodic_memories.json (tags/summary: 记忆话题)
- preferences.json (用户偏好)
- 抖音热搜 API (trending_list)
- ARK LLM (兴趣×热点→推荐)
"""
import os, json, time, logging, requests
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("magic")
_base = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

DATA_DIR = os.getenv("ASSISTANT_KID_DATA_DIR", _base)
FEISHU_PUSH_OPEN_ID = os.getenv("FEISHU_PUSH_OPEN_ID", "")

# 去重: 记录已推送过的热点词, 7天后过期
_PUSHED_CACHE = {}  # {word: timestamp}
_PUSHED_TTL = 7 * 24 * 3600  # 7天
_PUSHED_FILE = os.path.join(DATA_DIR, "pushed_hot_topics.json")


def _load_pushed():
    """从磁盘加载已推送记录"""
    global _PUSHED_CACHE
    try:
        with open(_PUSHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        _PUSHED_CACHE = {k: v for k, v in data.items() if now - v < _PUSHED_TTL}
    except Exception:
        _PUSHED_CACHE = {}


def _save_pushed():
    """保存已推送记录到磁盘"""
    try:
        tmp = _PUSHED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_PUSHED_CACHE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PUSHED_FILE)
    except Exception as e:
        log.warning(f"[push] 保存去重记录失败: {e}")


def _filter_pushed(hots: list) -> list:
    """过滤掉已推送过的热点词"""
    if not _PUSHED_CACHE:
        _load_pushed()
    now = time.time()
    # 清理过期记录
    expired = [k for k, v in _PUSHED_CACHE.items() if now - v >= _PUSHED_TTL]
    for k in expired:
        del _PUSHED_CACHE[k]
    # 过滤
    fresh = [h for h in hots if h not in _PUSHED_CACHE]
    log.info(f"[push] 去重: {len(hots)}条热点 → {len(fresh)}条未推送过")
    return fresh


def _mark_pushed(words: list):
    """标记已推送"""
    now = time.time()
    for w in words:
        _PUSHED_CACHE[w] = now
    _save_pushed()


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_user_interests() -> str:
    """提取用户兴趣画像(常用功能+记忆话题+偏好)"""
    lines = []
    # 自进化 learned_preferences
    d = _read_json(os.path.join(DATA_DIR, "evolution_data.json"))
    prefs = d.get("learned_preferences", {})
    for k, v in prefs.items():
        lines.append(f"{k}: {v}")
    # 叙事记忆 summary
    d = _read_json(os.path.join(DATA_DIR, "episodic_memories.json"))
    items = d if isinstance(d, list) else d.get("memories", [])
    for m in items[-8:]:
        s = m.get("summary", "")
        if s:
            lines.append(s)
    # 偏好
    d = _read_json(os.path.join(DATA_DIR, "preferences.json"))
    for k, v in d.items():
        if v:
            lines.append(f"{k}: {v}")
    return "\n".join(lines[:20]) if lines else "无明确兴趣记录"


def get_hot_topics() -> list:
    """抖音热搜词列表"""
    hots = []
    try:
        r = requests.get("https://www.douyin.com/aweme/v1/web/hot/search/list/",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            }, timeout=10)
        data = r.json().get("data", {})
        trending = data.get("trending_list", []) or data.get("hot_list", [])
        for item in trending[:15]:
            w = item.get("word", "")
            if w:
                hots.append(w)
    except Exception as e:
        log.warning(f"[push] 抖音热搜获取失败: {e}")
    return hots


def recommend_with_llm(interests: str, hots: list) -> list:
    """LLM: 用户兴趣 × 热点 → 筛选3条热点词(只筛选, 不解释)"""
    from app.llm_config import active_chat_endpoint

    if not hots:
        return []
    base, api_key, model = active_chat_endpoint()
    if not api_key:
        return hots[:3]

    hot_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(hots[:15]))
    prompt = (
        f"用户兴趣画像:\n{interests}\n\n"
        f"今日热点:\n{hot_text}\n\n"
        f"根据用户兴趣画像, 从上面热点里选出3条用户最可能感兴趣的。"
        f"只返回热点词, 每行一个, 不要序号不要解释不要理由。"
    )
    try:
        r = requests.post(f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300, "temperature": 0.5,
            }, timeout=30)
        text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # 解析: 每行一个热点词, 清理序号/引号/前缀
        import re
        lines = [re.sub(r'^[\d.、\-\*\s"「」]+', '', l.strip()).rstrip('。，')
                 for l in text.split("\n") if l.strip()]
        # 只保留和原始热点匹配的(避免LLM编造)
        matched = [l for l in lines if any(l in h or h in l for h in hots)][:3]
        return matched if matched else hots[:3]
    except Exception as e:
        log.warning(f"[push] LLM推荐失败: {e}")
        return hots[:3]


def _push_feishu(text: str):
    """发飞书消息(内联, 同 _push_feishu_async 的 _send 逻辑)"""
    if not FEISHU_PUSH_OPEN_ID:
        return
    try:
        r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": os.getenv("FEISHU_APP_ID", ""), "app_secret": os.getenv("FEISHU_APP_SECRET", "")},
            timeout=10)
        token = r.json().get("tenant_access_token", "")
        if not token:
            return
        requests.post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": FEISHU_PUSH_OPEN_ID, "msg_type": "text",
                  "content": json.dumps({"text": text})}, timeout=10)
        log.info(f"[push] 个性化推荐推送成功")
    except Exception as e:
        log.warning(f"[push] 个性化推荐推送失败: {e}")


def push_personalized_digest():
    """组合: 兴趣+热点→去重→LLM筛选→加链接→推飞书"""
    interests = get_user_interests()
    hots = get_hot_topics()
    if not hots:
        log.info("[push] 无热点数据, 跳过")
        return
    # 去重: 过滤掉已推送过的
    fresh = _filter_pushed(hots)
    if not fresh:
        log.info("[push] 所有热点均已推送过, 跳过")
        return
    # LLM从未推送的热点中筛选
    picks = recommend_with_llm(interests, fresh)
    if not picks:
        log.info("[push] LLM无推荐, 跳过")
        return
    # 标记已推送
    _mark_pushed(picks)
    import urllib.parse
    lines = ["📊 今日热点精选"]
    for i, word in enumerate(picks, 1):
        url = f"https://www.douyin.com/search/{urllib.parse.quote(word)}"
        lines.append(f"{i}. {word}\n   {url}")
    text = "\n\n".join(lines)
    _push_feishu(text)


def personalized_push_loop():
    """定时推送循环(每4h)"""
    interval = int(os.getenv("PERSONALIZED_PUSH_INTERVAL", str(3600)))  # 默认1h
    log.info(f"[push] 个性化推荐推送已启动(间隔{interval}s)")
    # 首次延迟10min(等服务就绪)
    time.sleep(600)
    while True:
        try:
            push_personalized_digest()
        except Exception as e:
            log.warning(f"[push] 推送异常: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    # 手动测试
    print("=== 用户兴趣画像 ===")
    print(get_user_interests())
    print("\n=== 热点 ===")
    print(get_hot_topics())
    print("\n=== LLM推荐 ===")
    rec = recommend_with_llm(get_user_interests(), get_hot_topics())
    print(rec)
