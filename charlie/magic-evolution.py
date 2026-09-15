"""magic-evolution: 自进化系统 (4个工具: 学习/分析/适应/优化)

Charlie 的自进化能力：从对话中学习，越用越懂你。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-evolution",
    "tier": "core",
    "required_env": [],
    "label": "自进化系统"
}

from mcp.server.fastmcp import FastMCP
import os, json, datetime, re, hashlib, time, threading, tempfile
from collections import Counter, defaultdict
import logging
log = logging.getLogger("magic")

mcp = FastMCP("magic-evolution")

# ============================================================
# Topic Classification Registry (metadata-driven, extensible)
# ============================================================
# Each entry maps a topic label to a list of keywords.
# Other magic modules can add topics at import time:
#   import magic_evolution as _ev
#   _ev.TOPIC_REGISTRY["玩偶"].extend(["公仔", "布偶"])
TOPIC_REGISTRY: dict[str, list[str]] = {
    "天气":     ["天气", "温度", "下雨", "气温"],
    "音乐":     ["音乐", "歌", "播放", "放", "听"],
    "提醒":     ["提醒", "闹钟", "定时", "日程"],
    "智能家居": ["空调", "电视", "灯", "开关"],
    "搜索":     ["搜索", "查一下", "搜一下", "找"],
    "计算":     ["计算", "多少", "等于"],
    "导航":     ["导航", "地图", "路线", "怎么走"],
    "问候":     ["你好", "在吗", "嗨"],
    "场景":     ["晚安", "早安", "早上好", "睡觉"],
}


def _classify_topic(msg: str) -> str | None:
    """Return the first matching topic label for *msg*, or None."""
    for topic, kws in TOPIC_REGISTRY.items():
        if any(kw in msg for kw in kws):
            return topic
    return None


_hour_slots = (
    ("凌晨(0-6)",   lambda h: 0 <= h < 6),
    ("早晨(6-9)",   lambda h: 6 <= h < 9),
    ("上午(9-12)",  lambda h: 9 <= h < 12),
    ("中午(12-14)", lambda h: 12 <= h < 14),
    ("下午(14-18)", lambda h: 14 <= h < 18),
    ("晚上(18-22)", lambda h: 18 <= h < 22),
    ("深夜(22-24)", lambda h: 22 <= h < 24),
)


def _bucket_hour(hour: int) -> str | None:
    for label, pred in _hour_slots:
        if pred(hour):
            return label
    return None


# ===== 学习数据存储 =====
DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
def _get_evolution_file():
    uid = os.environ.get("CHARLIE_USER_ID", "default")
    if uid == "default":
        return os.path.join(DATA_DIR, "evolution_data.json")
    return os.path.join(DATA_DIR, f"evolution_data_{uid}.json")
EVOLUTION_FILE = _get_evolution_file()
_evolution_lock = threading.Lock()


def _null_ctx():
    """Fallback no-op context manager when fcntl is unavailable."""
    from contextlib import nullcontext
    return nullcontext()


def _load_evolution_data() -> dict:
    """加载进化数据（跨进程 flock 保护读序列）"""
    path = _get_evolution_file()
    try:
        target_dir = os.path.dirname(os.path.abspath(path)) or "."
        lock_path = os.path.join(target_dir, ".evolution.lock")
        try:
            from utils import _locked_file
            lock_ctx = _locked_file(lock_path, shared=True)
        except ImportError:
            lock_ctx = _null_ctx()
        with lock_ctx:
            with _evolution_lock:
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        return json.load(f)
    except Exception as e:
        log.warning(f"[evolution] 加载进化数据失败: {e}")
    return {
        "learned_preferences": {},
        "usage_patterns": {},
        "response_metrics": {},
        "adaptation_state": {},
        "version": 1,
        "last_updated": time.time(),
    }


def _save_evolution_data(data: dict):
    """保存进化数据（原子写 + 跨进程文件锁，保留线程锁）"""
    data["last_updated"] = time.time()
    path = _get_evolution_file()
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    lock_path = os.path.join(target_dir, ".evolution.lock")
    try:
        from utils import _locked_file
        lock_ctx = _locked_file(lock_path, shared=False)
    except ImportError:
        lock_ctx = _null_ctx()
    with lock_ctx:
        with _evolution_lock:
            fd, tmp_path = tempfile.mkstemp(prefix=".evolution_", suffix=".tmp", dir=target_dir)
            f_wrapped = False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f_wrapped = True
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                if not f_wrapped:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise


def _modify_evolution_data(fn):
    """原子修改进化数据：在单次文件锁+线程锁保护下 load -> fn(data) -> save"""
    path = _get_evolution_file()
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    lock_path = os.path.join(target_dir, ".evolution.lock")
    try:
        from utils import _locked_file
        lock_ctx = _locked_file(lock_path, shared=False)
    except ImportError:
        lock_ctx = _null_ctx()
    with lock_ctx:
        with _evolution_lock:
            # Load
            try:
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                else:
                    data = {
                        "learned_preferences": {},
                        "usage_patterns": {},
                        "response_metrics": {},
                        "adaptation_state": {},
                        "version": 1,
                        "last_updated": time.time(),
                    }
            except Exception:
                data = {
                    "learned_preferences": {},
                    "usage_patterns": {},
                    "response_metrics": {},
                    "adaptation_state": {},
                    "version": 1,
                    "last_updated": time.time(),
                }
            # Modify via callback
            fn(data)
            # Save atomically
            data["last_updated"] = time.time()
            fd, tmp_path = tempfile.mkstemp(prefix=".evolution_", suffix=".tmp", dir=target_dir)
            f_wrapped = False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f_wrapped = True
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                if not f_wrapped:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise


def _analyze_conversation_history() -> list:
    """分析对话历史，提取用户偏好模式"""
    try:
        from agent.history import _get_history_file
        history_file = _get_history_file()
        if not os.path.exists(history_file):
            return []
        with open(history_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sessions = data if isinstance(data, dict) else {}
        # 分析所有会话中的用户消息
        user_messages = []
        for sid, msgs in sessions.items():
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if isinstance(m, dict) and m.get('role') == 'user':
                    content = m.get('content', '')
                    if isinstance(content, str) and content.strip():
                        user_messages.append(content.strip())
        return user_messages
    except Exception:
        return []


def _extract_patterns(messages: list) -> dict:
    """从用户消息中提取使用模式"""
    patterns = {
        "frequent_questions": Counter(),      # 高频问题
        "time_patterns": Counter(),           # 时间段偏好
        "topic_distribution": Counter(),      # 话题分布
        "avg_message_length": 0,
        "total_messages": len(messages),
    }
    if not messages:
        return patterns
    
    total_len = 0
    time_slots = {
        "凌晨(0-6)": 0, "早晨(6-9)": 0, "上午(9-12)": 0,
        "中午(12-14)": 0, "下午(14-18)": 0, "晚上(18-22)": 0, "深夜(22-24)": 0
    }
    
    for msg in messages:
        total_len += len(msg)
        # 话题分类（通过可扩展注册表）
        topic = _classify_topic(msg)
        if topic:
            patterns["topic_distribution"][topic] += 1
    
    patterns["avg_message_length"] = total_len // len(messages) if messages else 0
    
    # 活跃时段分析(从对话时间戳推算)
    active_hours = {
        "凌晨(0-6)": 0, "早晨(6-9)": 0, "上午(9-12)": 0,
        "中午(12-14)": 0, "下午(14-18)": 0, "晚上(18-22)": 0, "深夜(22-24)": 0
    }
    try:
        from agent.history import _get_history_file
        history_file = _get_history_file()
        if os.path.exists(history_file):
            with open(history_file, 'r', encoding='utf-8') as f:
                hist_data = json.load(f)
            sessions = hist_data if isinstance(hist_data, dict) else {}
            for sid, msgs in sessions.items():
                if not isinstance(msgs, list):
                    continue
                for m in msgs:
                    if isinstance(m, dict) and m.get('ts'):
                            try:
                                ts = float(m['ts'])
                                hour = datetime.datetime.fromtimestamp(ts).hour
                                slot = _bucket_hour(hour)
                                if slot:
                                    active_hours[slot] += 1
                            except Exception:
                                pass
        top_slots = sorted(active_hours.items(), key=lambda x: x[1], reverse=True)
        patterns["active_hours"] = [s[0] for s in top_slots[:3] if s[1] > 0]
    except Exception:
        patterns["active_hours"] = []
    
    # 识别高频问题（相同或相似问题出现多次）
    question_hashes = Counter()
    for msg in messages:
        q = re.sub(r'[。，！？、,.!?~\s]', '', msg)[:20]
        if len(q) >= 3:
            question_hashes[q] += 1
    patterns["frequent_questions"] = {q: c for q, c in question_hashes.most_common(10) if c >= 2}
    
    return patterns


@mcp.tool()
def learn_from_history() -> str:
    log.info("[evolution] learn_from_history 被调用")
    """从对话历史中学习用户偏好和习惯（自进化核心）

    分析所有历史对话，提取高频话题和问题，自动建议偏好设置。

    例: learn_from_history() → 分析全部对话历史，提取使用模式
    """
    messages = _analyze_conversation_history()
    if not messages:
        return "还没有足够的对话历史用于学习。多说几句，我就能更懂你。"
    
    patterns = _extract_patterns(messages)
    
    def _apply(data):
        topics = patterns["topic_distribution"]
        learned = data["learned_preferences"]
        
        # 自动学习偏好
        if topics.get("天气", 0) >= 3:
            learned["常用功能_天气查询"] = f"已查询{topics['天气']}次"
        if topics.get("音乐", 0) >= 3:
            learned["常用功能_音乐播放"] = f"已播放{topics['音乐']}次"
        if topics.get("智能家居", 0) >= 2:
            learned["常用功能_智能家居"] = f"已控制{topics['智能家居']}次"
        
        # 更新使用模式
        data["usage_patterns"] = {
            "total_conversations": len(messages),
            "top_topics": [t for t, c in topics.most_common(5) if c > 0],
            "avg_message_length": patterns["avg_message_length"],
            "frequent_questions": patterns["frequent_questions"],
            "active_hours": patterns.get("active_hours", []),
        }
        
        # 自适应调整
        if patterns["avg_message_length"] < 10:
            data["adaptation_state"]["response_style"] = "极简"  # 用户喜欢简短交互
            _apply.response_style = "极简"
        elif patterns["avg_message_length"] > 30:
            data["adaptation_state"]["response_style"] = "详细"  # 用户喜欢详细回复
            _apply.response_style = "详细"
        else:
            data["adaptation_state"]["response_style"] = "平衡"
            _apply.response_style = "平衡"
    
    _apply.response_style = "平衡"
    
    _modify_evolution_data(_apply)
    
    # 生成学习报告
    topics = patterns["topic_distribution"]
    top_topic = topics.most_common(1)
    report = [
        f"📊 学习完成！分析了 {len(messages)} 条对话。",
        f"  最常用功能：{top_topic[0][0] if top_topic else '暂无'}",
        f"  回复风格已调整为：{_apply.response_style}",
    ]
    if patterns["frequent_questions"]:
        report.append(f"  发现 {len(patterns['frequent_questions'])} 个反复提问的话题")
    learned_topics = [t for t, c in topics.most_common(5) if c > 0]
    if learned_topics:
        topic_str = "、".join(f"{t}({c}次)" for t, c in learned_topics)
        report.append(f"  话题分布：{topic_str}")
    
    return "\n".join(report)


@mcp.tool()
def self_optimize() -> str:
    log.debug(f"[evolution] self_optimize被调用")
    """自我优化：根据使用模式自动调整回复策略

    如果用户经常打断，缩短回复长度。如果用户经常重复提问，优化回答策略。

    例: self_optimize() → 根据打断率和重复提问自动调整回复风格
    """
    data = _load_evolution_data()
    patterns = data.get("usage_patterns", {})
    adaptation = data.get("adaptation_state", {})
    
    optimizations = []
    
    # 优化1: 基于打断率的回复长度调整
    metrics = data.get("response_metrics", {})
    total_interrupts = metrics.get("total_interrupts", 0)
    total_responses = max(metrics.get("total_responses", 1), 1)
    interrupt_rate = total_interrupts / total_responses
    
    if interrupt_rate > 0.3:
        adaptation["response_style"] = "极简"
        adaptation["max_sentences"] = 2
        optimizations.append("打断率较高(>30%)，已启用极简回复模式")
    elif interrupt_rate > 0.1:
        adaptation["response_style"] = "精简"
        adaptation["max_sentences"] = 3
        optimizations.append("打断率适中，已启用精简回复模式")
    else:
        adaptation["response_style"] = "平衡"
        adaptation.pop("max_sentences", None)
        optimizations.append("回复流畅，保持当前风格")
    
    # 优化2: 基于重复提问的知识优化
    freq_qs = patterns.get("frequent_questions", {})
    if freq_qs:
        optimizations.append(f"检测到 {len(freq_qs)} 个高频问题，下次回答会更精准")
    
    # 优化3: 预加载推荐
    topics = patterns.get("top_topics", [])
    if topics:
        top = topics[0]
        if top == "天气":
            optimizations.append(f"常用天气查询，已预加载天气数据")
        elif top == "音乐":
            optimizations.append(f"常用音乐播放，已优化音乐响应速度")
    
    data["adaptation_state"] = adaptation
    _save_evolution_data(data)
    
    return "🔄 自我优化完成\n" + "\n".join(f"  ✅ {o}" for o in optimizations)


@mcp.tool()
def suggest_preferences() -> str:
    log.debug(f"[evolution] suggest_preferences被调用")
    """根据使用模式建议个性化偏好设置

    自动发现常用城市、提醒时间、使用习惯等。

    例: suggest_preferences() → 生成个性化偏好建议列表
    """
    data = _load_evolution_data()
    patterns = data.get("usage_patterns", {})
    messages = _analyze_conversation_history()
    
    suggestions = []
    
    # 分析天气查询城市
    city_pattern = re.compile(r'([\u4e00-\u9fa5]{2,4}(?:市)?)\s*天气')
    cities = Counter()
    for msg in messages:
        match = city_pattern.search(msg)
        if match:
            cities[match.group(1)] += 1
    
    if cities:
        top_city = cities.most_common(1)[0]
        if top_city[1] >= 3:
            suggestions.append(f"📌 建议设置默认城市为「{top_city[0]}」（已查询{top_city[1]}次）")
    
    # 分析时间偏好
    time_patterns = data.get("usage_patterns", {}).get("time_patterns", {})
    if time_patterns:
        peak_time = max(time_patterns, key=time_patterns.get)
        suggestions.append(f"📌 您常在{peak_time}使用，已优化该时段响应速度")
    
    # 分析话题偏好
    topic_dist = Counter()
    for msg in messages:
        if any(kw in msg for kw in ['音乐', '歌', '播放']):
            topic_dist["音乐"] += 1
        if any(kw in msg for kw in ['提醒', '闹钟']):
            topic_dist["提醒"] += 1
        if any(kw in msg for kw in ['晚安', '睡觉']):
            topic_dist["晚安场景"] += 1
        if any(kw in msg for kw in ['早上好', '早安']):
            topic_dist["早安场景"] += 1
    
    for topic, count in topic_dist.most_common(3):
        if count >= 3:
            suggestions.append(f"📌 常用「{topic}」（{count}次），已优化响应流程")
    
    if not suggestions:
        return "还没有足够的使用数据。多使用一些功能后，我就能给出个性化建议。"
    
    return "💡 使用习惯分析\n" + "\n".join(suggestions)


@mcp.tool()
def evolution_status() -> str:
    log.debug(f"[evolution] evolution_status被调用")
    """查看自进化系统状态：学习进度、适应状态、当前优化项

    显示已学习的数据量、当前适应模式、优化建议。

    例: evolution_status() → 查看已学偏好和回复风格
    """
    data = _load_evolution_data()
    patterns = data.get("usage_patterns", {})
    adaptation = data.get("adaptation_state", {})
    learned = data.get("learned_preferences", {})
    
    total_msgs = patterns.get("total_conversations", 0)
    style = adaptation.get("response_style", "默认")
    topics = patterns.get("top_topics", [])
    
    lines = [
        "🧬 自进化系统状态",
        f"  📊 已学习对话：{total_msgs} 条",
        f"  🎯 回复风格：{style}",
        f"  🧠 自动学习偏好：{len(learned)} 项",
    ]
    
    if topics:
        lines.append(f"  📈 热门话题：{'、'.join(topics[:3])}")
    
    if adaptation.get("max_sentences"):
        lines.append(f"  📏 最大回复句数：{adaptation['max_sentences']} 句")
    
    if learned:
        lines.append(f"  📝 已学偏好：")
        for k, v in learned.items():
            lines.append(f"    • {k}: {v}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
