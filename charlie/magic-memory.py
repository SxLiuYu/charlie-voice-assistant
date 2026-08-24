"""magic-memory: 叙事性记忆系统

从每次对话中提取关键事件，存储为 episodic memory。
区别于 evolution_data.json (统计模式)，这里存储的是叙事性记忆——
"用户上周说项目周五截止" 而不是 "天气查询9次"。

记忆注入: brain() 每次回复后异步提取事件，存入 episodic_memories.json
记忆检索: _build_system_msg() 注入与当前对话相关的记忆

v2 改进:
- 字符 Bigram TF-IDF 余弦相似度替代旧版字符重叠匹配
- 时间衰减平滑化 (30天以上仍有基础权重)
- 记忆修正 (correct_memory) 和语义去重 (dedup_memories)
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-memory",
    "tier": "core",
    "required_env": [],
    "label": "叙事性记忆系统"
}

from mcp.server.fastmcp import FastMCP
import os, json, datetime, re, threading, time, hashlib
from collections import defaultdict
import logging
log = logging.getLogger("magic")

mcp = FastMCP("magic-memory")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
def _get_memory_file():
    uid = os.environ.get("CHARLIE_USER_ID", "default")
    if uid == "default":
        return os.path.join(DATA_DIR, "episodic_memories.json")
    return os.path.join(DATA_DIR, f"episodic_memories_{uid}.json")
MEMORY_FILE = _get_memory_file()  # 初始路径, 切换用户时重新计算
_memory_lock = threading.Lock()

MAX_MEMORIES = 200  # 最多保留200条记忆, 超出自动清理最旧的


# ===== 语义化记忆检索: 字符级 Bigram 余弦相似度 =====

def _text_to_bigrams(text: str) -> dict:
    """提取字符bigram, 返回 {bigram: count} 字典"""
    bigrams = {}
    for i in range(len(text) - 1):
        bigram = text[i:i+2]
        if bigram.strip():
            bigrams[bigram] = bigrams.get(bigram, 0) + 1
    return bigrams


def _cosine_similarity(vec1: dict, vec2: dict) -> float:
    """计算两个bigram向量的余弦相似度, 0~1"""
    if not vec1 or not vec2:
        return 0.0
    intersection = set(vec1.keys()) & set(vec2.keys())
    if not intersection:
        return 0.0
    dot = sum(vec1[k] * vec2[k] for k in intersection)
    norm1 = sum(v * v for v in vec1.values()) ** 0.5
    norm2 = sum(v * v for v in vec2.values()) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


# ===== 关键事件提取规则 =====
# 通过关键词识别对话中是否包含值得记忆的信息
_EVENT_PATTERNS = [
    # 截止日期/重要时间
    {"tags": ["deadline", "time"], "patterns": [
        r"截止", r"到期", r"明天.{0,4}要", r"下周.{0,4}要",
        r"之前要", r"之前完成", r"最后期限", r"deadline",
    ], "summary_template": "用户提到了时间节点: {text}"},
    # 学习/工作项目
    {"tags": ["project", "learning"], "patterns": [
        r"在学", r"在看", r"在做.{0,6}项目", r"在写.{0,6}代码",
        r"在研究", r"在看.{0,6}书", r"在准备",
    ], "summary_template": "用户正在进行: {text}"},
    # 偏好表达
    {"tags": ["preference"], "patterns": [
        r"我喜欢", r"我讨厌", r"我不喜欢", r"我习惯",
        r"我偏好", r"我最爱", r"别再",
    ], "summary_template": "用户表达了偏好: {text}"},
    # 任务/计划
    {"tags": ["task", "plan"], "patterns": [
        r"帮我记", r"提醒我", r"我要去", r"我打算",
        r"计划.{0,4}去", r"准备.{0,4}做",
    ], "summary_template": "用户有计划: {text}"},
    # 问题/求助
    {"tags": ["problem"], "patterns": [
        r"怎么.{0,2}办", r"出了.{0,2}问题", r"不工作",
        r"报错", r"失败", r"连不上", r"打不开",
    ], "summary_template": "用户遇到了问题: {text}"},
    # 人物/社交
    {"tags": ["social"], "patterns": [
        r"跟.{0,2}说", r"告诉.{0,2}", r"约了", r"见面",
        r"打电话", r"发消息",
    ], "summary_template": "用户的社交活动: {text}"},
]


def _load_memories() -> list:
    log.debug("[memory] 加载记忆")
    """加载所有记忆"""
    with _memory_lock:
        try:
            if os.path.exists(_get_memory_file()):
                with open(_get_memory_file(), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            log.warning(f"[memory] 读记忆失败: {e}")
        return []


def _save_memories(memories: list):
    log.info(f"[memory] 保存记忆 {len(memories)} 条")
    """保存记忆, 超出上限自动截断"""
    with _memory_lock:
        if len(memories) > MAX_MEMORIES:
            memories = memories[-MAX_MEMORIES:]
        try:
            with open(_get_memory_file(), 'w', encoding='utf-8') as f:
                json.dump(memories, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _extract_events(user_text: str, assistant_reply: str) -> list:
    """从对话中提取关键事件, 返回事件列表"""
    events = []
    text = user_text.strip()
    if len(text) < 4:
        return events
    # 跳过闲聊/问候(不值得记忆)
    _SKIP_KEYWORDS = {"你好", "在吗", "谢谢", "再见", "好的", "嗯", "几点了",
                      "今天天气", "几点啦", "在不在", "你好啊"}
    if any(kw in text for kw in _SKIP_KEYWORDS) or len(text) <= 3:
        return events
    for rule in _EVENT_PATTERNS:
        matched = False
        for pattern in rule["patterns"]:
            if re.search(pattern, text):
                matched = True
                break
        if matched:
            summary = rule["summary_template"].format(text=text[:80])
            events.append({
                "timestamp": time.time(),
                "datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "user_text": text[:200],
                "tags": rule["tags"],
                "summary": summary,
                "bigrams": _text_to_bigrams(text[:200]),  # 预计算bigram向量
            })
            break  # 每条对话只提取一个事件
    # Fallback: 未匹配任何 pattern 但文本足够长的通用陈述，作为 general 记忆存储
    if not events and len(text) >= 15:
        # 质量过滤：跳过 ASR 乱码（重复双字词过多或标点密度异常）
        skip = False
        for i in range(len(text) - 1):
            seg = text[i:i+2]
            if text.count(seg) >= 3:
                skip = True
                break
        if not skip:
            comma_count = text.count("，") + text.count(",")
            if comma_count > len(text) / 5:
                skip = True
        if not skip:
            events.append({
                "timestamp": time.time(),
                "datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "user_text": text[:200],
                "tags": ["general"],
                "summary": f"用户提到: {text[:80]}",
                "bigrams": _text_to_bigrams(text[:200]),
            })
    return events


def remember_conversation(user_text: str, assistant_reply: str) -> int:
    """对话结束后调用, 提取并保存记忆. 返回保存的记忆数"""
    events = _extract_events(user_text, assistant_reply)
    if not events:
        return 0
    memories = _load_memories()
    new_events = []
    for event in events:
        is_dup = False
        for mem in memories:
            sim = _cosine_similarity(
                event.get("bigrams", {}),
                mem.get("bigrams", {}),
            )
            if sim > 0.9:
                is_dup = True
                break
        if not is_dup:
            new_events.append(event)
    if not new_events:
        return 0
    memories.extend(new_events)
    _save_memories(memories)
    return len(new_events)


def get_relevant_memories(query: str, limit: int = 3) -> list:
    """检索与当前查询相关的记忆, 基于bigram余弦相似度 + 标签匹配 + 时间衰减

    相比旧版(字符重叠匹配), 新方法:
    - bigram能捕捉"项目截止"与"项目进度"的语义相似度
    - 余弦相似度提供0~1的连续评分, 而非0.1的离散加分
    - 时间衰减更平滑, 30天以上仍有基础权重
    """
    memories = _load_memories()
    if not memories:
        return []
    query_bigrams = _text_to_bigrams(query)
    if not query_bigrams:
        return []
    scored = []
    for mem in memories:
        score = 0.0
        # 1. Bigram余弦相似度 (核心改进)
        mem_bigrams = mem.get("bigrams", {})
        if mem_bigrams:
            sim = _cosine_similarity(query_bigrams, mem_bigrams)
            score += sim * 4.0  # 语义匹配最高4分
        # 2. 标签匹配
        query_lower = query.lower()
        for tag in mem.get("tags", []):
            if tag in query_lower:
                score += 2.0
        # 3. 文本关键词精确匹配(作为补充, 降权)
        mem_text = mem.get("user_text", "") + " " + mem.get("summary", "")
        mem_words = set(mem_text)
        query_words = set(query)
        common = mem_words & query_words - set("，。！？、,.!? \n\t")
        if common:
            score += len(common) * 0.05
        # 4. 时间衰减: 平滑衰减
        age_hours = (time.time() - mem.get("timestamp", 0)) / 3600
        if age_hours < 24:
            score += 1.5
        elif age_hours < 168:
            score += 0.8
        elif age_hours < 720:
            score += 0.3
        else:
            score += 0.1  # 旧记忆仍有基础权重
        if score > 0:
            scored.append((score, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def find_memory(query: str) -> dict | None:
    """精确查找与查询匹配的记忆, 返回第一条或None"""
    memories = get_relevant_memories(query, limit=1)
    return memories[0] if memories else None


def correct_memory(query: str, correction: str) -> bool:
    """修正与查询匹配的记忆, 返回是否成功"""
    memories = _load_memories()
    found = False
    for mem in memories:
        if query in mem.get("user_text", "") or query in mem.get("summary", ""):
            mem["user_text"] = correction
            mem["summary"] = f"用户修正: {correction[:80]}"
            mem["bigrams"] = _text_to_bigrams(correction[:200])
            mem["timestamp"] = time.time()
            mem["datetime"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            found = True
            break
    if found:
        _save_memories(memories)
    return found


def dedup_memories() -> int:
    """合并内容相似的重复记忆, 返回删除数"""
    memories = _load_memories()
    if len(memories) < 2:
        return 0
    kept = []
    removed = 0
    for mem in memories:
        duplicate = False
        for existing in kept:
            sim = _cosine_similarity(
                mem.get("bigrams", {}),
                existing.get("bigrams", {})
            )
            if sim > 0.7 and mem.get("tags") == existing.get("tags"):
                duplicate = True
                # 合并时间戳: 保留较新的一条
                if mem.get("timestamp", 0) > existing.get("timestamp", 0):
                    kept.remove(existing)
                    kept.append(mem)
                removed += 1
                break
        if not duplicate:
            kept.append(mem)
    if removed > 0:
        _save_memories(kept)
    return removed


def format_memories_for_prompt(query: str, limit: int = 3) -> str:
    """格式化记忆摘要, 注入到 system prompt（使用混合检索）"""
    memories = recall_hybrid(query, k=limit)
    if not memories:
        return ""
    lines = []
    for mem in memories:
        dt = mem.get("datetime", "")
        summary = mem.get("summary", "")
        lines.append(f"[{dt}] {summary}")
    return "相关记忆：\n" + "\n".join(lines)


def get_memory_summary() -> dict:
    """获取记忆系统状态摘要"""
    memories = _load_memories()
    tag_counts = defaultdict(int)
    for m in memories:
        for tag in m.get("tags", []):
            tag_counts[tag] += 1
    recent = memories[-5:] if memories else []
    return {
        "total": len(memories),
        "tags": dict(tag_counts),
        "recent": [{"datetime": m.get("datetime", ""), "summary": m.get("summary", "")} for m in recent],
    }


@mcp.tool()
def recall(query: str) -> str:
    log.info(f"[memory] 检索记忆: {query[:30]}")
    """回忆与查询相关的记忆。

    参数:
    - query: 要回忆的内容关键词

    例: recall("项目") → 返回与项目相关的记忆
        recall("上周说了什么") → 返回最近的记忆
    """
    results = recall_hybrid(query, k=5)
    if not results:
        return "没有找到相关记忆。"
    lines = []
    for m in results:
        lines.append(f"[{m.get('datetime', '')}] {m.get('summary', '')}")
    return "\n".join(lines)


@mcp.tool()
def memory_status() -> str:
    log.debug("[memory] 查询记忆状态")
    """查看记忆系统状态: 总记忆数、标签分布、最近5条记忆。"""
    summary = get_memory_summary()
    lines = [f"记忆总数: {summary['total']}"]
    if summary["tags"]:
        tag_str = "、".join(f"{k}({v})" for k, v in summary["tags"].items())
        lines.append(f"标签分布: {tag_str}")
    if summary["recent"]:
        lines.append("最近记忆:")
        for r in summary["recent"]:
            lines.append(f"  [{r['datetime']}] {r['summary']}")
    return "\n".join(lines)


@mcp.tool()
def correct(query: str, correction: str) -> str:
    log.info(f"[memory] 修正记忆: {query[:30]}")
    """修正记忆中的错误信息。

    参数:
    - query: 要修正的记忆关键词
    - correction: 正确的内容

    例: correct("上周项目截止", "项目不是上周五截止，是下周三截止")
        → 修正匹配的记忆为正确内容
    """
    if correct_memory(query, correction):
        return f"已修正记忆: {correction[:80]}"
    return "没有找到匹配的记忆需要修正。"


@mcp.tool()
def dedup() -> str:
    log.info("[memory] 去重记忆")
    """合并内容相似的重复记忆, 清理冗余。

    例: dedup() → 自动合并相似记忆, 返回清理数量
    """
    removed = dedup_memories()
    return f"已清理{removed}条重复记忆。" if removed > 0 else "没有发现重复记忆。"


@mcp.tool()
def forget(query: str) -> str:
    log.info(f"[memory] 删除记忆: {query[:30]}")
    """删除与查询匹配的记忆。

    参数:
    - query: 要忘记的内容关键词

    例: forget("项目") → 删除所有与项目相关的记忆
    """
    memories = _load_memories()
    before = len(memories)
    remaining = [m for m in memories if query not in m.get("user_text", "") and query not in m.get("summary", "")]
    _save_memories(remaining)
    deleted = before - len(remaining)
    return f"已删除{deleted}条记忆。" if deleted > 0 else "没有找到匹配的记忆。"


# ===== Memory v2: 工作记忆 + 语义记忆 + 混合检索 =====

# 工作记忆：当前会话的短期记忆，会话结束清空
_working_memory = {
    "session_facts": {},       # 本轮对话中提到的事实 "project=xxx"
    "intent_stack": [],        # 用户意图栈
    "last_topic": "",          # 最后话题
    "turn_count": 0,           # 本轮对话轮数
}
_working_memory_lock = threading.Lock()


def reset_working_memory() -> None:
    """清空工作记忆（会话结束时调用）"""
    with _working_memory_lock:
        _working_memory["session_facts"].clear()
        _working_memory["intent_stack"].clear()
        _working_memory["last_topic"] = ""
        _working_memory["turn_count"] = 0


def update_working_memory(facts: dict = None, intent: str = "", topic: str = "") -> None:
    """更新工作记忆"""
    with _working_memory_lock:
        if facts:
            _working_memory["session_facts"].update(facts)
        if intent:
            stack = _working_memory["intent_stack"]
            if not stack or stack[-1] != intent:
                stack.append(intent)
        if topic:
            _working_memory["last_topic"] = topic
        _working_memory["turn_count"] += 1


def get_working_memory() -> dict:
    """获取当前工作记忆快照"""
    with _working_memory_lock:
        return {
            "session_facts": dict(_working_memory["session_facts"]),
            "intent_stack": list(_working_memory["intent_stack"]),
            "last_topic": _working_memory["last_topic"],
            "turn_count": _working_memory["turn_count"],
        }


# 语义记忆：从 episodic 中提取的语义知识（用户偏好/习惯/关系）
_SEMANTIC_FILE = None
_semantic_lock = threading.Lock()
_semantic_cache = {}


def _get_semantic_file() -> str:
    global _SEMANTIC_FILE
    if _SEMANTIC_FILE is None:
        uid = os.environ.get("CHARLIE_USER_ID", "default")
        name = f"semantic_memories_{uid}.json" if uid != "default" else "semantic_memories.json"
        _SEMANTIC_FILE = os.path.join(DATA_DIR, name)
    return _SEMANTIC_FILE


def _load_semantic_memories() -> list:
    with _semantic_lock:
        try:
            if os.path.exists(_get_semantic_file()):
                with open(_get_semantic_file(), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []


def _save_semantic_memories(memories: list) -> None:
    with _semantic_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = _get_semantic_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(memories, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _get_semantic_file())
        except Exception:
            pass


def _extract_semantic_knowledge(memories: list) -> list:
    """从叙事记忆中提取语义知识"""
    knowledge = []
    pref_patterns = [
        (r"我喜欢(.+?)[，。]", "preference", "like"),
        (r"我讨厌(.+?)[，。]", "preference", "dislike"),
        (r"我习惯(.+?)[，。]", "habit", "routine"),
        (r"我通常(.+?)[，。]", "habit", "routine"),
        (r"我的(.+?)是(.+?)[，。]", "attribute", "fact"),
    ]
    for mem in memories:
        text = mem.get("user_text", "") + " " + mem.get("summary", "")
        for pattern, ktype, ksub in pref_patterns:
            matches = re.findall(pattern, text)
            for m in matches:
                knowledge.append({
                    "type": ktype,
                    "subtype": ksub,
                    "key": m.strip()[:30],
                    "value": m.strip()[:60],
                    "source": mem.get("datetime", ""),
                    "confidence": 0.8,
                })
    return knowledge


def refresh_semantic_memory() -> int:
    """刷新语义记忆（从 episodic 重建）"""
    memories = _load_memories()
    knowledge = _extract_semantic_knowledge(memories)
    _save_semantic_memories(knowledge)
    return len(knowledge)


def get_semantic_memories() -> list:
    """获取语义记忆"""
    return _load_semantic_memories()


# ===== 混合检索 (recency + relevance + importance) =====

def _score_recency(mem: dict) -> float:
    """时间分：24h内0.8，7天内0.5，30天内0.3，否则0.1"""
    age_hours = (time.time() - mem.get("timestamp", 0)) / 3600
    if age_hours < 24:
        return 0.8
    if age_hours < 168:
        return 0.5
    if age_hours < 720:
        return 0.3
    return 0.1


def _score_relevance(mem: dict, query: str) -> float:
    """语义相关分：bigram 余弦相似度"""
    query_bigrams = _text_to_bigrams(query)
    mem_bigrams = mem.get("bigrams", {})
    if not query_bigrams or not mem_bigrams:
        return 0.0
    sim = _cosine_similarity(query_bigrams, mem_bigrams)
    return sim * 6.0


def _score_importance(mem: dict) -> float:
    """重要性分：标签权重 + 互动反馈"""
    score = 0.0
    tags = mem.get("tags", [])
    important_tags = {"deadline", "task", "plan", "problem"}
    if set(tags) & important_tags:
        score += 1.0
    if mem.get("feedback_count", 0) > 0:
        score += 0.5
    return score


def recall_hybrid(query: str, k: int = 5) -> list:
    """混合检索：recency + relevance + importance

    参数:
    - query: 查询文本
    - k: 返回数量

    例: recall_hybrid("项目进度", k=3) → 返回最相关的3条记忆
    """
    memories = _load_memories()
    if not memories:
        return []
    query_bigrams = _text_to_bigrams(query)
    if not query_bigrams:
        return []

    scored = []
    for mem in memories:
        recency = _score_recency(mem)
        relevance = _score_relevance(mem, query)
        importance = _score_importance(mem)
        total = recency + relevance + importance
        if total > 0:
            scored.append((total, recency, relevance, importance, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, _, _, _, m in scored[:k]]


def remember_working_fact(key: str, value: str) -> None:
    """记住当前会话中的一个事实"""
    update_working_memory(facts={key: value})


def forget_working_fact(key: str) -> None:
    """忘记当前会话中的一个事实"""
    with _working_memory_lock:
        _working_memory["session_facts"].pop(key, None)


if __name__ == "__main__":
    mcp.run()
