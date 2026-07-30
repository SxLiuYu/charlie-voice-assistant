"""
白泽老能力 MCP 服务器
把购物/翻译/提醒/系统监控/计算 包装成标准MCP工具，接入Qwen-Agent大脑
"""
import os, json, requests, datetime
from mcp.server.fastmcp import FastMCP
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

mcp = FastMCP("baize-skills")
TAVILY = os.getenv("TAVILY_API_KEY", "REDACTED")
ALIYUN = os.getenv("ALIYUN_API_KEY", "sk-16cb5f2bc07a4984b43588a6f7e1c4c6")
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")

def aliyun_chat(messages, temperature=0.3):
    r = requests.post(DASHSCOPE, headers={"Authorization": f"Bearer {ALIYUN}", "Content-Type": "application/json"},
        json={"model": "qwen-max", "messages": messages, "temperature": temperature, "stream": False}, timeout=60)
    return r.json()["choices"][0]["message"]["content"]

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
def _load(): 
    try:
        with open(STORE) as f: return json.load(f)
    except: return []
def _save(d):
    with open(STORE, "w") as f: json.dump(d, f, ensure_ascii=False, indent=2)
def _parse_time(s):
    """时间解析(复用utils.parse_time_str)"""
    from utils import parse_time_str
    return parse_time_str(s)

@mcp.tool()
def add_reminder(text: str, time: str = "") -> str:
    """添加提醒/待办。text=内容, time=提醒时间如'明天上午9点''30分钟后'(可选)"""
    d = _load()
    due = _parse_time(time)
    d.append({"id": int(datetime.datetime.now().timestamp()), "text": text, "time": time, "due": due, "done": False})
    _save(d)
    when = f"，提醒时间{due.replace('T',' ')}" if due else (f"（时间'{time}'未解析出时刻）" if time else "")
    return f"已添加提醒：{text}{when}。当前共{len([x for x in d if not x['done']])}项未完成"

@mcp.tool()
def list_reminders() -> str:
    """列出未完成的待办/提醒"""
    d = [x for x in _load() if not x["done"]]
    if not d: return "当前没有待办事项"
    return "待办清单：\n" + "\n".join(f"{i+1}. {x['text']}" + (f" ⏰{x['due'][:16].replace('T',' ')}" if x.get("due") else "") for i, x in enumerate(d))

# ===== 系统监控 =====
@mcp.tool()
def system_status() -> str:
    """查询当前设备/服务器状态：CPU/内存/磁盘/运行时间"""
    import psutil, socket, platform, time as _t
    vm = psutil.virtual_memory()
    cpu_pct = psutil.cpu_percent(interval=1)
    disk = psutil.disk_usage('/')
    boot = psutil.boot_time()
    up = int(_t.time() - boot)
    return (f"设备:{socket.gethostname()} | {platform.system()} {platform.release()}\n"
        f"CPU使用率:{cpu_pct}% | {psutil.cpu_count()}核 | 内存:{(vm.total-vm.available)//1073741824:.1f}/{vm.total//1073741824:.1f}GB({vm.percent}%)\n"
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
@mcp.tool()
def calculate(expression: str) -> str:
    """计算或单位换算。expression=算式如'123*456'或'5公里等于多少英里'"""
    import re
    e = expression.strip().rstrip("=＝")
    if re.fullmatch(r"[\d.\s+\-*/()%^]+", e):
        try:
            r = eval(e.replace("^", "**"))
            return f"{e} = {r}"
        except: pass
    return aliyun_chat([{"role":"system","content":"你是计算换算助手，直接给结果和一行过程"},
        {"role":"user","content":expression}], temperature=0)

if __name__ == "__main__":
    mcp.run()
