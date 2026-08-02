"""
白泽老能力 MCP 服务器
把购物/翻译/提醒/系统监控/计算 包装成标准MCP工具，接入Qwen-Agent大脑
"""
import os, json, requests, datetime, fcntl, tempfile
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
try:
    import psutil
    psutil.cpu_percent(interval=None)
except ImportError:
    psutil = None
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

mcp = FastMCP("baize-skills")
TAVILY = os.getenv("TAVILY_API_KEY", "")
ALIYUN = os.getenv("ALIYUN_API_KEY", "")
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", PROJECT_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

STORE = os.path.join(DATA_DIR, "reminders.json")
STORE_LOCK_FILE = STORE + ".lock"


@contextmanager
def _locked_store(shared: bool = False):
    with open(STORE_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

def _coerce_reminders(raw):
    if not isinstance(raw, list):
        return []
    return [
        item for item in raw
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text", "").strip()
    ]

def aliyun_chat(messages, temperature=0.3):
    r = requests.post(DASHSCOPE, headers={"Authorization": f"Bearer {ALIYUN}", "Content-Type": "application/json"},
        json={"model": "qwen-max", "messages": messages, "temperature": temperature, "stream": False}, timeout=60)
    return r.json()["choices"][0]["message"]["content"]

# ===== 通用互联网搜索（免API，Bing中国爬取）=====
def _bing_scrape(query: str, max_results: int = 8) -> list:
    """爬取 cn.bing.com 搜索结果，不需要 API key"""
    import re, html as htmlmod
    r = requests.get('https://cn.bing.com/search', params={'q': query, 'count': max_results},
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}, timeout=15)
    blocks = re.findall(r'<li class="b_algo".*?</li>', r.text, re.DOTALL)
    results = []
    for b in blocks[:max_results]:
        title_m = re.search(r'<h2[^>]*>(.*?)</h2>', b, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ''
        title = htmlmod.unescape(title)
        url_m = re.search(r'<a[^>]*href="(https?://[^"]+)"', b)
        url = url_m.group(1) if url_m else ''
        snippet_m = re.search(r'<p[^>]*>(.*?)</p>', b, re.DOTALL)
        snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ''
        snippet = htmlmod.unescape(snippet)
        if title and len(title) > 5:
            results.append({'title': title, 'url': url, 'snippet': snippet})
    return results

@mcp.tool()
def web_search_free(query: str) -> str:
    """免费互联网搜索(不需要API Key)：搜新闻、知识、人物、事件等。query=搜索词"""
    results = _bing_scrape(query)
    if not results:
        return f"没搜到「{query}」的相关内容"
    docs = [f"[{i+1}] {r['title']}\n{r['snippet']}" for i, r in enumerate(results)]
    reply = aliyun_chat([
        {"role": "system", "content": f"用户搜索「{query}」。从以下搜索结果提取关键信息，用简洁中文总结要点，附来源编号。不要编造未在结果中出现的内容。"},
        {"role": "user", "content": "\n\n".join(docs)[:9000]}
    ])
    return reply

# ===== 通用互联网搜索（Tavily API，更高质量）=====
@mcp.tool()
def web_search(query: str) -> str:
    """通用互联网搜索：搜新闻、知识、人物、事件等任何内容。query=搜索词"""
    body = {"api_key": TAVILY, "query": query, "max_results": 8, "search_depth": "advanced"}
    r = requests.post("https://api.tavily.com/search", json=body, timeout=30).json()
    results = r.get("results", [])
    if not results:
        return f"没搜到「{query}」的相关内容"
    docs = [f"[{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(results)]
    reply = aliyun_chat([
        {"role": "system", "content": f"用户搜索「{query}」。从以下搜索结果提取关键信息，用简洁中文总结要点，附来源编号。不要编造未在结果中出现的内容。"},
        {"role": "user", "content": "\n\n".join(docs)[:9000]}
    ])
    return reply

# ===== 智能购物 =====
@mcp.tool()
def shopping_search(keyword: str) -> str:
    """智能购物推荐：搜什么值得买价格+评测，选出最优前5带真实价格。keyword=商品名"""
    def tavily(query, domains=None):
        body = {"api_key": TAVILY, "query": query, "max_results": 5, "search_depth": "advanced"}
        if domains: body["include_domains"] = domains
        r = requests.post("https://api.tavily.com/search", json=body, timeout=30).json()
        return r.get("results", [])
    price = tavily(f"{keyword} 价格 推荐", ["smzdm.com"])
    review = tavily(f"{keyword} 评测 推荐 型号 性价比")
    docs = ([f"[价格{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(price)] +
            [f"[评测{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(review)])
    if not docs: return f"没搜到{keyword}的相关商品"
    reply = aliyun_chat([
        {"role": "system", "content": f"你是专业购物顾问。用户想买【{keyword}】。从结果提取与{keyword}直接相关的具体产品型号和真实价格，优先[价格]来源。凑齐5个最优，每个一句话推荐理由。格式：1.【产品型号】¥XX 推荐理由"},
        {"role": "user", "content": "\n\n".join(docs)[:9000]}
    ])
    return reply

# ===== 翻译 =====
@mcp.tool()
def translate(text: str, target: str = "英文") -> str:
    """翻译。text=内容, target=目标语言(英文/中文/日文/韩文)"""
    return aliyun_chat([
        {"role": "system", "content": f"你是翻译引擎，把用户内容翻译成{target}，只输出译文。"},
        {"role": "user", "content": text}
    ])

# ===== 提醒 =====
def _read_store():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_store_locked(d):
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(STORE),
            prefix=".reminders.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(_coerce_reminders(d), temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, STORE)
    except Exception:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def _load():
    with _locked_store(shared=True):
        return _read_store()


def _save(d):
    with _locked_store():
        _write_store_locked(d)
def _parse_time(s):
    """时间解析(复用utils.parse_time_str)"""
    from utils import parse_time_str
    return parse_time_str(s)

@mcp.tool()
def add_reminder(text: str, time: str = "") -> str:
    """添加提醒/待办。text=内容, time=提醒时间如'明天上午9点''30分钟后'(可选)"""
    with _locked_store():
        d = _coerce_reminders(_read_store())
        due = _parse_time(time)
        d.append({"id": int(datetime.datetime.now().timestamp()), "text": text, "time": time, "due": due, "done": False})
        _write_store_locked(d)
        when = f"，提醒时间{due.replace('T',' ')}" if due else (f"（时间'{time}'未解析出时刻）" if time else "")
        return f"已添加提醒：{text}{when}。当前共{len([x for x in d if not x['done']])}项未完成"

@mcp.tool()
def list_reminders() -> str:
    """列出未完成的待办/提醒"""
    d = [x for x in _coerce_reminders(_load()) if not x.get("done")]
    if not d: return "当前没有待办事项"
    return "待办清单：\n" + "\n".join(
        f"{i+1}. {x['text']}" + (f" ⏰{str(x.get('due') or '')[:16].replace('T',' ')}" if x.get("due") else "")
        for i, x in enumerate(d)
    )

# ===== 系统监控 =====
@mcp.tool()
def system_status() -> str:
    """查询当前设备/服务器状态：CPU/内存/磁盘/运行时间"""
    import socket, platform, time as _t
    _psutil = psutil if psutil is not None else __import__("psutil")
    vm = _psutil.virtual_memory()
    cpu_pct = _psutil.cpu_percent(interval=None)
    disk = _psutil.disk_usage('/')
    boot = _psutil.boot_time()
    up = int(_t.time() - boot)
    return (f"设备:{socket.gethostname()} | {platform.system()} {platform.release()}\n"
        f"CPU使用率:{cpu_pct}% | {_psutil.cpu_count()}核 | 内存:{(vm.total-vm.available)//1073741824:.1f}/{vm.total//1073741824:.1f}GB({vm.percent}%)\n"
        f"磁盘:{disk.used//1073741824:.0f}/{disk.total//1073741824:.0f}GB({disk.percent}%) | 运行:{up//86400}天{up%86400//3600}时{up%3600//60}分")

# ===== 生活服务deeplink(外卖/餐厅/购物/买菜/买药/打车) =====
@mcp.tool()
def open_lifestyle_app(intent: str, keyword: str = "") -> str:
    """打开生活服务App(手机点击唤起到搜索/下单页)。intent: waimai外卖/food餐厅/shopping购物/grocery买菜/pharmacy买药/ride打车, keyword: 搜索词"""
    from urllib.parse import quote
    q = quote(keyword or "")
    links = {}
    if intent == "waimai":
        links["美团外卖"] = f"https://h5.waimai.meituan.com/waimai/mindex/search?query={q}"
        links["饿了么"] = f"https://www.ele.me/search/{q}"
    elif intent == "food":
        links["大众点评"] = f"https://m.dianping.com/searchlist?keyword={q}"
        links["美团团购"] = f"https://meituan.com/s/{q}/"
    elif intent == "shopping":
        links["淘宝"] = f"https://s.m.taobao.com/h5?q={q}"
        links["京东"] = f"https://so.m.jd.com/ware/search.action?keyword={q}"
        links["拼多多"] = f"https://mobile.yangkeduo.com/search_result.html?search_key={q}"
    elif intent == "grocery":
        links["美团买菜"] = f"https://h5.waimai.meituan.com/waimai/mindex/search?query={q}"
    elif intent == "pharmacy":
        links["美团买药"] = f"https://h5.waimai.meituan.com/waimai/mindex/search?query={q}"
        links["饿了么买药"] = f"https://www.ele.me/search/{q}"
    elif intent == "ride":
        links["滴滴出行"] = "https://common.diditaxi.com.cn/webapp_landing?from=web"
    if not links:
        return f"未知意图{intent}，支持: waimai外卖/food餐厅/shopping购物/grocery买菜/pharmacy买药/ride打车"
    return f"已为'{keyword or intent}'生成App链接(手机点击唤起下单):\n" + "\n".join(f"• {k}: {v}" for k, v in links.items())

# ===== 计算/换算 =====
def _safe_math_eval(expr: str) -> float | None:
    """安全的数学表达式求值(基于AST, 不使用eval)"""
    import ast, operator
    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
        ast.FloorDiv: operator.floordiv,
    }
    def _eval(node):
        if isinstance(node, ast.Constant):  # Python 3.8+
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"不允许的常量类型: {type(node.value)}")
        elif isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            op_type = type(node.op)
            if op_type in operators:
                return operators[op_type](left, right)
            raise ValueError(f"不允许的操作: {op_type.__name__}")
        elif isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            op_type = type(node.op)
            if op_type in operators:
                return operators[op_type](operand)
            raise ValueError(f"不允许的一元操作: {op_type.__name__}")
        else:
            raise ValueError(f"不允许的语法: {type(node).__name__}")
    try:
        tree = ast.parse(expr, mode='eval')
        return _eval(tree.body)
    except Exception:
        return None

@mcp.tool()
def calculate(expression: str) -> str:
    """计算或单位换算。expression=算式如'123*456'或'5公里等于多少英里'"""
    import re
    e = expression.strip().rstrip("=＝")
    if re.fullmatch(r"[\d.\s+\-*/()%^]+", e):
        result = _safe_math_eval(e.replace("^", "**"))
        if result is not None:
            return f"{e} = {result}"
    return aliyun_chat([{"role":"system","content":"你是计算换算助手，直接给结果和一行过程"},
        {"role":"user","content":expression}], temperature=0)



# ===== WiFi/位置定位 =====
ESP32_IP = os.getenv("ESP32_IP", "192.168.1.7")

@mcp.tool()
def get_location() -> str:
    """获取用户当前位置：ESP32 WiFi扫描(室内定位) + 浏览器GPS经纬度反向地理编码。
    返回城市/街道/经纬度。当用户问'我在哪''附近有什么'时调用。"""
    parts = []
    # 1. 尝试 ESP32 WiFi scan（室内定位）
    try:
        r = requests.get(f"http://{ESP32_IP}/api/wifi/scan", timeout=10)
        if r.status_code == 200:
            aps = r.json()
            if aps:
                parts.append(f"WiFi扫描到{len(aps)}个AP:")
                for ap in aps[:5]:
                    parts.append(f"  {ap.get('ssid','?')} RSSI={ap.get('rssi','?')}dBm")
                # 最强信号 AP 用于判断是否在家
                strongest = max(aps, key=lambda x: x.get('rssi', -999))
                if strongest.get('rssi', -999) > -50:
                    parts.append(f"最强信号: {strongest['ssid']}({strongest['rssi']}dBm) → 可能在室内")
    except Exception as e:
        parts.append(f"ESP32 WiFi扫描失败: {type(e).__name__}")

    # 2. 用高德地图 IP 定位（不需要浏览器，基于网络 IP 粗定位）
    try:
        amap_key = os.getenv("AMAP_KEY", "")
        if amap_key:
            r2 = requests.get(f"https://restapi.amap.com/v3/ip?key={amap_key}&output=json", timeout=5)
            data = r2.json()
            if data.get("city"):
                parts.append(f"IP定位: {data.get('province','')} {data.get('city','')}")
                if data.get('rectangle'):
                    parts.append(f"坐标范围: {data['rectangle']}")
    except Exception as e:
        parts.append(f"IP定位失败: {type(e).__name__}")

    if len(parts) <= 1:
        return "无法获取位置信息。请在浏览器中允许定位权限，或确保ESP32在线。"
    return "\n".join(parts)


# ===== 用户偏好(越用越懂你) =====
PREFS_FILE = os.path.join(DATA_DIR, "preferences.json")
PREFS_LOCK_FILE = PREFS_FILE + ".lock"


@contextmanager
def _locked_prefs(shared: bool = False):
    with open(PREFS_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

def _load_prefs():
    with _locked_prefs(shared=True):
        return _read_locked_prefs()

def _read_locked_prefs():
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            prefs = json.load(f)
            return prefs if isinstance(prefs, dict) else {}
    except Exception:
        return {}

def _write_locked_prefs(prefs):
    directory = os.path.dirname(PREFS_FILE)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".preferences.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(prefs, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, PREFS_FILE)
    except Exception:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise

@mcp.tool()
def set_preference(key: str, value: str) -> str:
    """记住用户偏好(越用越懂你)。key=偏好名(如'喜欢的食物'/'下班时间'), value=偏好值(如'意大利菜'/'18:00')。
    当用户说'我喜欢/我不喜欢/记住/我每天/我习惯'时调用此工具。"""
    with _locked_prefs(shared=False):
        prefs = _read_locked_prefs()
        prefs[key] = value
        _write_locked_prefs(prefs)
    return f"已记住您的偏好：{key} = {value}"

@mcp.tool()
def get_preferences() -> str:
    """查看所有已记住的用户偏好。"""
    prefs = _load_prefs()
    if not prefs:
        return "暂无已记住的偏好"
    items = [f"{k}: {v}" for k, v in prefs.items()]
    return f"已记住{len(prefs)}项偏好：" + chr(10) + chr(10).join(items)

if __name__ == "__main__":
    mcp.run()
