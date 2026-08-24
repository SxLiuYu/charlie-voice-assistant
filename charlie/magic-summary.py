"""magic-summary: 每日摘要生成

从对话历史、决策记录、记忆中提取关键信息，生成自然语言摘要。
支持: 每日简报、昨日回顾、本周总结

用法:
- 用户说 "今天发生了什么事" → 生成今日摘要
- 用户说 "总结一下昨天" → 生成昨日回顾
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-summary",
    "tier": "core",
    "required_env": [],
    "label": "每日摘要生成"
}

from mcp.server.fastmcp import FastMCP
import os, json, datetime, time, re
import logging
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("magic")

mcp = FastMCP("magic-summary")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))


def _get_daily_conversations(date_str: str = None) -> list:
    """获取指定日期的对话摘要"""
    if date_str is None:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        history_file = os.path.join(DATA_DIR, "conversation_history.json")
        if not os.path.exists(history_file):
            return []
        with open(history_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sessions = data if isinstance(data, dict) else {}
        conversations = []
        for sid, msgs in sessions.items():
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if isinstance(m, dict) and m.get('ts'):
                    try:
                        ts = float(m['ts'])
                        msg_date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                        if msg_date == date_str:
                            conversations.append({
                                "role": m.get("role", "user"),
                                "content": str(m.get("content", ""))[:100],
                                "time": datetime.datetime.fromtimestamp(ts).strftime("%H:%M"),
                            })
                    except Exception as e:
                        log.warning(f"[summary] 解析消息失败: {e}")
                        pass
        return conversations
    except Exception as e:
        log.warning(f"[summary] 读会话文件失败: {e}")
        return []


def _get_daily_decisions(date_str: str = None) -> list:
    """获取指定日期的决策"""
    if date_str is None:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        decision_file = os.path.join(DATA_DIR, "decision_history.json")
        if not os.path.exists(decision_file):
            return []
        with open(decision_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        decisions = []
        for rule_id, info in data.items():
            if isinstance(info, dict):
                trigger_time = info.get("trigger_time", "")
                if trigger_time.startswith(date_str):
                    decisions.append({
                        "rule": rule_id,
                        "time": trigger_time,
                    })
        return decisions
    except Exception as e:
        log.warning(f"[summary] 读决策文件失败: {e}")
        return []


def _get_daily_memories(date_str: str = None) -> list:
    """获取指定日期的记忆"""
    if date_str is None:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        memory_file = os.path.join(DATA_DIR, "episodic_memories.json")
        if not os.path.exists(memory_file):
            return []
        with open(memory_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        memories = []
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict) and m.get("datetime", "").startswith(date_str):
                    memories.append(m.get("summary", m.get("user_text", ""))[:100])
        return memories
    except Exception as e:
        log.warning(f"[summary] 读决策文件失败: {e}")
        return []


def _generate_summary(conversations: list, decisions: list, memories: list, title: str) -> str:
    log.info(f"[summary] 生成{title}摘要")
    """用 LLM 生成自然语言摘要"""
    if not conversations and not decisions and not memories:
        return f"{title}：今天还没有任何活动记录。"

    context = f"{title}。以下是今天的数据：\n"
    if conversations:
        context += f"\n对话 ({len(conversations)}条):\n"
        for c in conversations[:10]:
            role = "用户" if c["role"] == "user" else "Charlie"
            context += f"  [{c['time']}] {role}: {c['content']}\n"
    if decisions:
        context += f"\n自主决策 ({len(decisions)}条):\n"
        for d in decisions:
            context += f"  {d['time']} 触发: {d['rule']}\n"
    if memories:
        context += f"\n记忆 ({len(memories)}条):\n"
        for m in memories[:5]:
            context += f"  {m}\n"

    # 用 LLM 生成摘要
    from app.llm_config import active_chat_endpoint

    try:
        import requests
        base, api_key, model = active_chat_endpoint()
        if not api_key:
            return _simple_summary(conversations, decisions, memories, title)

        r = requests.post(f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是 Charlie，用2-3句中文简洁总结一天的活动。重点：关键对话、重要事件、值得注意的模式。"},
                    {"role": "user", "content": context},
                ],
                "max_tokens": 200,
            }, timeout=10)
        reply = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if reply:
            return f"{title}：\n{reply}"
    except Exception:
        pass
    return _simple_summary(conversations, decisions, memories, title)


def _simple_summary(conversations: list, decisions: list, memories: list, title: str) -> str:
    """简单规则摘要 (LLM 不可用时的降级)"""
    lines = [f"{title}："]
    if conversations:
        lines.append(f"进行了 {len(conversations)} 次对话。")
        topics = set()
        for c in conversations:
            if c["role"] == "user":
                content = c["content"][:20]
                if len(content) > 3:
                    topics.add(content)
        if topics:
            lines.append(f"话题包括：{'、'.join(list(topics)[:5])}。")
    if decisions:
        lines.append(f"触发了 {len(decisions)} 个自主决策。")
    if memories:
        lines.append(f"记录了 {len(memories)} 条新记忆。")
    if not conversations:
        lines.append("今天还没有对话记录。")
    return "\n".join(lines)


@mcp.tool()
def daily_summary() -> str:
    log.info("[summary] 生成今日摘要")
    """生成今日摘要：总结今天的对话、决策和记忆。

    例: daily_summary() → 生成今天的每日简报
    """
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    conversations = _get_daily_conversations(today)
    decisions = _get_daily_decisions(today)
    memories = _get_daily_memories(today)
    return _generate_summary(conversations, decisions, memories, "今日摘要")


@mcp.tool()
def yesterday_summary() -> str:
    log.info("[summary] 生成昨日回顾")
    """生成昨日回顾：总结昨天的对话、决策和记忆。

    例: yesterday_summary() → 生成昨天的回顾
    """
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    conversations = _get_daily_conversations(yesterday)
    decisions = _get_daily_decisions(yesterday)
    memories = _get_daily_memories(yesterday)
    return _generate_summary(conversations, decisions, memories, "昨日回顾")


@mcp.tool()
def weekly_summary() -> str:
    log.info("[summary] 生成本周总结")
    """生成本周摘要：总结本周的对话、决策和记忆。

    例: weekly_summary() → 生成本周简报
    """
    now = datetime.datetime.now()
    monday = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    all_conversations = []
    all_decisions = []
    all_memories = []
    for i in range(7):
        date_str = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        if date_str >= monday:
            all_conversations.extend(_get_daily_conversations(date_str))
            all_decisions.extend(_get_daily_decisions(date_str))
            all_memories.extend(_get_daily_memories(date_str))
    return _generate_summary(all_conversations, all_decisions, all_memories, "本周摘要")


if __name__ == "__main__":
    mcp.run()
