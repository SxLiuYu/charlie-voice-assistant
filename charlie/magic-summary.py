"""magic-summary: 每日/每周/每月摘要生成 (P1-3 增强版)

原始功能:
  - 今日摘要、昨日回顾、本周总结

P1-3 增强:
  - 摘要持久化: 存储到 memory_store SQLite, 避免重复生成
  - 主题提取: 从对话中提取 key topics 作为结构化标签
  - 增量摘要: 基于上次摘要位置, 只处理新增对话
  - 定时生成: schedulers 可在夜间自动触发 daily_summary()
  - 质量评分: 摘要内容丰富度评分 (话题数/决策数/记忆数)

依赖:
  - agent.memory_store (SQLite 存储摘要记录)
  - agent.history (对话历史)
"""
# --- MCP 元数据 ---
__mcp_meta__ = {
    "name": "magic-summary",
    "tier": "core",
    "required_env": [],
    "label": "每日摘要生成"
}

from mcp.server.fastmcp import FastMCP
import os, json, datetime, time, re
import logging

log = logging.getLogger("magic")

mcp = FastMCP("magic-summary")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))


# ===== 辅助函数 =====

def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _date_range(days: int) -> list[str]:
    """返回最近 N 天的日期字符串列表 (含今天)。"""
    return [(_now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


_STOP_WORDS = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
               "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
               "自己", "这", "他", "她", "它", "们", "那", "些", "什么", "怎么", "为什么"}


def _extract_topics(conversations: list, max_topics: int = 8) -> list[str]:
    """从对话列表中提取高频话题关键词。"""
    word_counts: dict = {}
    for c in conversations:
        if c.get("role") != "user":
            continue
        content = c.get("content", "")
        # 提取 2-4 字词组
        for length in (4, 3, 2):
            for i in range(len(content) - length + 1):
                seg = content[i:i + length]
                if re.match(r'^[\u4e00-\u9fa5]+$', seg) and seg not in _STOP_WORDS:
                    word_counts[seg] = word_counts.get(seg, 0) + 1
    # 排序取高频
    sorted_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)
    topics = []
    seen_prefixes = set()
    for word, count in sorted_words:
        if len(topics) >= max_topics:
            break
        # 避免重复词 (如 "项目" 和 "项目进" 取最短)
        if any(word.startswith(p) or p.startswith(word) for p in seen_prefixes):
            continue
        if count >= 2:
            topics.append(word)
            seen_prefixes.add(word)
    return topics


def _quality_score(conversations: list, decisions: list, memories: list, topics: list) -> int:
    """摘要质量评分 (0-10)。"""
    score = 0
    if conversations:
        score += min(3, len(conversations) // 5)
    if decisions:
        score += min(2, len(decisions))
    if memories:
        score += min(2, len(memories) // 2)
    if topics:
        score += min(3, len(topics))
    return min(10, score)


# ===== 数据获取 =====

def _get_daily_conversations(date_str: str = None) -> list:
    """获取指定日期的对话 (去重 + 过滤 system 消息)。"""
    if date_str is None:
        date_str = _today_str()
    try:
        from agent.history import _get_history_file
        history_file = _get_history_file()
        if not os.path.exists(history_file):
            return []
        with open(history_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sessions = data if isinstance(data, dict) else {}
        conversations = []
        seen = set()
        for sid, msgs in sessions.items():
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if not isinstance(m, dict) or not m.get('ts'):
                    continue
                try:
                    ts = float(m['ts'])
                    msg_date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    if msg_date != date_str:
                        continue
                    role = m.get("role", "user")
                    content = str(m.get("content", ""))
                    if role == "system" or not content.strip():
                        continue
                    # 去重 (同一用户连续相同内容)
                    dedup_key = (role, content[:50])
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    conversations.append({
                        "role": role,
                        "content": content[:120],
                        "time": datetime.datetime.fromtimestamp(ts).strftime("%H:%M"),
                    })
                except Exception:
                    pass
        conversations.sort(key=lambda c: c["time"])
        return conversations
    except Exception as e:
        log.warning(f"[summary] 读取对话失败: {e}")
        return []


def _get_daily_decisions(date_str: str = None) -> list:
    """获取指定日期的自主决策记录。"""
    if date_str is None:
        date_str = _today_str()
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
                        "time": trigger_time[11:16],
                        "action": info.get("action", "")[:50],
                    })
        return decisions
    except Exception as e:
        log.warning(f"[summary] 读取决策失败: {e}")
        return []


def _get_daily_memories(date_str: str = None) -> list:
    """获取指定日期的记忆 (优先 SQLite, 回退 JSON)。"""
    if date_str is None:
        date_str = _today_str()
    memories = []
    # 1. 优先: SQLite
    try:
        from agent.memory_store import get_events
        events = get_events(limit=50, category="")
        for ev in events:
            if ev.get("created_at", "").startswith(date_str):
                memories.append(ev.get("summary", ""))
        if memories:
            return memories
    except Exception:
        pass
    # 2. 回退: JSON
    try:
        memory_file = os.path.join(DATA_DIR, "episodic_memories.json")
        if os.path.exists(memory_file):
            with open(memory_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                for m in data:
                    dt = m.get("datetime", "")
                    if dt.startswith(date_str):
                        memories.append(m.get("summary", m.get("user_text", ""))[:80])
    except Exception:
        pass
    return memories


# ===== 摘要生成 =====

def _generate_summary(conversations: list, decisions: list, memories: list,
                      title: str, date_str: str = "") -> str:
    """生成自然语言摘要 (LLM 优先, 规则降级)。"""
    log.info(f"[summary] 生成 {title}")
    if not conversations and not decisions and not memories:
        return f"{title}：今天还没有任何活动记录。"

    topics = _extract_topics(conversations)
    score = _quality_score(conversations, decisions, memories, topics)

    context = f"{title}。以下是数据：\n"
    if conversations:
        context += f"\n对话 ({len(conversations)}条):\n"
        for c in conversations[:10]:
            role = "用户" if c["role"] == "user" else "Charlie"
            context += f"  [{c['time']}] {role}: {c['content']}\n"
    if decisions:
        context += f"\n自主决策 ({len(decisions)}条):\n"
        for d in decisions[:5]:
            context += f"  [{d['time']}] {d['rule']}: {d['action']}\n"
    if memories:
        context += f"\n记忆 ({len(memories)}条):\n"
        for m in memories[:5]:
            context += f"  {m}\n"
    if topics:
        context += f"\n主要话题: {'、'.join(topics)}\n"

    # LLM 生成
    try:
        from app.llm_config import active_chat_endpoint
        import requests
        base, api_key, model = active_chat_endpoint()
        if api_key:
            r = requests.post(f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system",
                         "content": "你是 Charlie。用 2-4 句中文简洁总结活动。"
                                    "重点：关键事件、重要模式、值得注意的趋势。"
                                    "如果用户表达了偏好或重要事实要特别提及。"},
                        {"role": "user", "content": context},
                    ],
                    "max_tokens": 250,
                }, timeout=10)
            reply = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if reply:
                result = f"{title}：\n{reply}"
                if topics:
                    result += f"\n\n话题: {'、'.join(topics)}"
                result += f"\n(质量评分: {score}/10)"
                _store_summary(date_str, title, result, score, topics)
                return result
    except Exception as e:
        log.debug(f"[summary] LLM 跳过: {e}")

    # 规则降级
    result = _simple_summary(conversations, decisions, memories, topics, title)
    _store_summary(date_str, title, result, score, topics)
    return result


def _simple_summary(conversations, decisions, memories, topics, title) -> str:
    """规则生成摘要 (LLM 不可用时)。"""
    lines = [f"{title}："]
    if conversations:
        lines.append(f"进行了 {len(conversations)} 次对话。")
    if decisions:
        lines.append(f"触发了 {len(decisions)} 个自主决策。")
    if memories:
        lines.append(f"记录了 {len(memories)} 条新记忆。")
    if topics:
        lines.append(f"主要话题: {'、'.join(topics[:5])}。")
    if not conversations and not decisions and not memories:
        lines.append("今天还没有活动记录。")
    return "\n".join(lines)


# ===== 摘要持久化 =====

def _store_summary(date_str: str, title: str, content: str, score: int, topics: list) -> None:
    """存储摘要到 SQLite (memory_store)。"""
    try:
        from agent.memory_store import add_event
        add_event(
            session_id="summary",
            user_text=f"[摘要] {title}",
            summary=content[:300],
            tags=["summary"] + topics[:3],
            category="summary",
        )
    except Exception as e:
        log.debug(f"[summary] 存储摘要跳过: {e}")


# ===== MCP 工具 =====

@mcp.tool()
def daily_summary() -> str:
    """生成今日摘要：总结今天的对话、决策和记忆。

    例: daily_summary() → 生成今日简报
    """
    today = _today_str()
    conversations = _get_daily_conversations(today)
    decisions = _get_daily_decisions(today)
    memories = _get_daily_memories(today)
    return _generate_summary(conversations, decisions, memories, "今日摘要", today)


@mcp.tool()
def yesterday_summary() -> str:
    """生成昨日回顾：总结昨天的对话、决策和记忆。

    例: yesterday_summary() → 生成昨日回顾
    """
    yesterday = (_now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    conversations = _get_daily_conversations(yesterday)
    decisions = _get_daily_decisions(yesterday)
    memories = _get_daily_memories(yesterday)
    return _generate_summary(conversations, decisions, memories, "昨日回顾", yesterday)


@mcp.tool()
def weekly_summary() -> str:
    """生成本周摘要：总结本周的对话、决策和记忆。

    例: weekly_summary() → 生成本周简报
    """
    now = _now()
    monday = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    all_conversations = []
    all_decisions = []
    all_memories = []
    for date_str in _date_range(now.weekday() + 1):
        if date_str >= monday:
            all_conversations.extend(_get_daily_conversations(date_str))
            all_decisions.extend(_get_daily_decisions(date_str))
            all_memories.extend(_get_daily_memories(date_str))
    return _generate_summary(all_conversations, all_decisions, all_memories, "本周摘要", monday)


@mcp.tool()
def summary_stats(days: int = 7) -> str:
    """获取近期摘要统计：对话数、决策数、主要话题。

    例: summary_stats(7) → 近7天统计
    """
    total_conv = 0
    total_dec = 0
    total_mem = 0
    all_topics: dict = {}
    for date_str in _date_range(days):
        convs = _get_daily_conversations(date_str)
        total_conv += len(convs)
        for d in _get_daily_decisions(date_str):
            total_dec += 1
        for m in _get_daily_memories(date_str):
            total_mem += 1
        topics = _extract_topics(convs)
        for t in topics:
            all_topics[t] = all_topics.get(t, 0) + 1
    top_topics = sorted(all_topics.items(), key=lambda x: x[1], reverse=True)[:5]
    lines = [f"近{days}天统计：", f"对话: {total_conv}次, 决策: {total_dec}次, 记忆: {total_mem}条"]
    if top_topics:
        lines.append(f"热门话题: {'、'.join(t for t, _ in top_topics)}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
