"""magic-feishu: 飞书MCP (4个工具: 搜索文档/发消息/读消息/查日历)

通过飞书开放平台API操作，需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-feishu",
    "tier": "optional",
    "required_env": ['FEISHU_APP_ID', 'FEISHU_APP_SECRET'],
    "label": "飞书集成(文档/消息/日历)"
}

import os, json, requests, time
from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

mcp = FastMCP("magic-feishu")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BASE = "https://open.feishu.cn/open-apis"
_token_cache = {"token": "", "at": 0}


def _get_token() -> str:
    """获取飞书 tenant_access_token"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return ""
    now = time.time()
    if _token_cache["token"] and now - _token_cache["at"] < 7200:
        return _token_cache["token"]
    try:
        r = requests.post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
        token = r.json().get("tenant_access_token", "")
        if token:
            _token_cache["token"] = token
            _token_cache["at"] = now
        return token
    except Exception:
        return ""


@mcp.tool()
def search_docs(query: str, count: int = 5) -> str:
    log.debug("[search_docs] 被调用")
    """搜索飞书云文档。query=搜索关键词, count=返回条数

    例: search_docs("项目计划") → 搜索包含"项目计划"的飞书文档
    """
    token = _get_token()
    if not token:
        return "飞书未配置，请在.env中设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"
    try:
        r = requests.post(f"{FEISHU_BASE}/drive/v1/files/search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query, "page_size": count}, timeout=10)
        data = r.json()
        files = data.get("data", {}).get("files", [])
        if not files:
            return f"没搜到包含「{query}」的飞书文档"
        lines = [f"搜到{len(files)}个飞书文档："]
        for f in files[:count]:
            lines.append(f"• {f.get('name', '?')} ({f.get('type', '?')})")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索飞书文档失败: {e}"


@mcp.tool()
def send_message(user_id: str, text: str) -> str:
    log.info(f"[feishu] send_message(to={user_id})")
    """发送飞书消息。user_id=用户ID或open_id, text=消息内容

    例: send_message("ou_xxx", "会议改到下午3点") → 给指定用户发消息
    """
    token = _get_token()
    if not token:
        return "飞书未配置"
    try:
        r = requests.post(f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": user_id,
                "msg_type": "text",
                "content": json.dumps({"text": text})
            }, timeout=10)
        if r.status_code == 200:
            return f"已发送飞书消息：{text[:50]}"
        return f"发送失败: {r.json().get('msg', '未知错误')}"
    except Exception as e:
        return f"发送飞书消息失败: {e}"


@mcp.tool()
def list_messages(chat_id: str = "", count: int = 10) -> str:
    log.debug("[list_messages] 被调用")
    """读取飞书消息。chat_id=群聊ID(可选), count=消息条数

    例: list_messages() → 读取最近消息
    """
    token = _get_token()
    if not token:
        return "飞书未配置"
    try:
        url = f"{FEISHU_BASE}/im/v1/messages"
        if chat_id:
            url += f"?container_id={chat_id}&container_id_type=chat"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
            params={"page_size": count}, timeout=10)
        data = r.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            return "没有新消息"
        lines = [f"最近{len(items)}条飞书消息："]
        for item in items[:count]:
            body = json.loads(item.get("body", {}).get("content", "{}"))
            msg_text = body.get("text", "")[:50]
            lines.append(f"• {msg_text}")
        return "\n".join(lines)
    except Exception as e:
        return f"读取飞书消息失败: {e}"


@mcp.tool()
def get_calendar() -> str:
    log.debug("[get_calendar] 被调用")
    """获取今天的飞书日历日程

    例: get_calendar() → 查看今天的飞书日程
    """
    token = _get_token()
    if not token:
        return "飞书未配置"
    try:
        import datetime
        # 先获取日历列表
        r = requests.get(f"{FEISHU_BASE}/calendar/v4/calendars",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10)
        calendars = r.json().get("data", {}).get("calendar_list", [])
        if not calendars:
            return "没有找到飞书日历"
        # 使用主日历
        calendar_id = calendars[0].get("calendar_id", "primary")
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0).isoformat() + "+08:00"
        end = now.replace(hour=23, minute=59, second=59).isoformat() + "+08:00"
        r2 = requests.get(f"{FEISHU_BASE}/calendar/v4/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            params={"start_time": start, "end_time": end, "page_size": 20},
            timeout=10)
        data = r2.json()
        events = data.get("data", {}).get("items", [])
        if not events:
            return "今天没有飞书日程"
        lines = [f"今天有{len(events)}个飞书日程："]
        for e in events[:10]:
            summary = e.get("summary", "无标题")
            start_time = e.get("start_time", {}).get("date_time", "")[:16]
            lines.append(f"• {start_time} {summary}")
        return "\n".join(lines)
    except Exception as e:
        return f"获取飞书日历失败: {e}"


if __name__ == "__main__":
    mcp.run()
