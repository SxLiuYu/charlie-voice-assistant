"""用户画像统一门面 — 聚合读（get_user_profile）+ 写路由（显式/自动双通道）。

## 为什么需要它

charlie 的用户状态其实早已存在，只是**散在 6 个模块、6 种 schema**里，没有
一个统一的"画像"对象可读、可查、可注入：

    偏好(stated)   → preferences.py        (key-value JSON, 按 uid 分片)
    喜好/事实      → memory_store.py       (SQLite facts, category=personal/preference/habit)
    学到的偏好     → evolution_data.json   (learned_preferences / adaptation_state)
    情绪人设       → persona.py            (emotion/mood，情绪驱动，会变)
    位置/运行时    → context.py            (location/weather/recent，60s TTL，非持久)
    对话背景       → history.py            (recent turns / session summary)

本模块**不拥有、不迁移**这些数据，只做两件事：
  1. **读**：`get_user_profile(session_id)` 把上面各源聚合成一个规范画像对象；
  2. **写**：`record_explicit` / `set_reply_style` / `apply_extracted` 把新事实
     路由到正确的底层库，并给原先**无主**的"用户显式回复风格"（reply_style）
     一个持久新家（`user_profile_{uid}.json`，canonical store 的种子）。

## 写通道（显式 + 自动，配敏感过滤）

  - 显式：用户明说"记住X / 我喜欢X / 我住在X / 说简短点" → 立即路由落库；
  - 自动：复用 `_spawn_memory_thread`（= `magic_memory.remember_conversation`）
    每轮抽取，落库前过一层敏感过滤（身份证/银行卡/密码/手机号/病历/薪资等
    一律不自动入库，除非句子带显式"记住"）。

## 安全边界

  - 全程按 uid 分片（FR-14），跨用户零泄漏；`get_user_profile` 按
    `resolve_user(session_id)` 解析身份。
  - 显式优先于自动：reply_style 只在用户明示时更新，自动抽取不覆盖显式声明。
  - 画像缺失/任一底层库异常 → 聚合结果降级为空字段，**绝不让 brain 崩**。
  - `get_user_profile` 是纯读、无副作用；行为变更全部由独立开关门控（默认关）。

## 开关（默认全关，逐级灰度）

  USER_PROFILE_ENABLED=1   brain() 里只读聚合 + 审计日志（P1，零行为变更）
  USER_PROFILE_AUTOSTORE=1 apply_extracted 真正沉淀（P3）
  USER_PROFILE_INJECT=1    system prompt 注入画像片段（P4）
"""
import json
import os
import re
import tempfile
import threading
import time
import logging

log = logging.getLogger("magic")

DATA_DIR = os.environ.get(
    "ASSISTANT_KID_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

# reply_style 的合法取值（显式声明用）
_TONES = ("casual", "formal", "playful")       # 随意 / 正式 / 俏皮
_LENGTHS = ("short", "medium", "detailed")     # 短 / 中 / 详细
_FORMALITY = ("low", "medium", "high")

# 统一画像的对象 key（get_user_profile 返回结构，测试据此断言）
PROFILE_KEYS = ("uid", "identity", "location", "preferences", "interests",
                "reply_style", "persona_state", "conversation_background", "meta")

# ============================== 敏感信息过滤 ==============================
# 自动抽取通道命中任一项即**拒绝落库**（显式"记住"通道不受此限——用户主动要求）。
# 覆盖常见 PII；宁可漏存也不误存隐私。
_SENSITIVE_PATTERNS = [
    (re.compile(r"身份证|证件号|护照"), "id"),
    (re.compile(r"银行卡|卡号|信用卡|CVV"), "bank"),
    (re.compile(r"密码|password|passwd|pin\s*码", re.I), "password"),
    (re.compile(r"1[3-9]\d{9}"), "phone"),                     # 11 位手机号
    (re.compile(r"\b\d{16,19}\b"), "cardno"),                  # 16-19 位卡号
    (re.compile(r"病历|病史|确诊|住院|诊断书|抑郁症|癌"), "health"),
    (re.compile(r"工资|薪资|月薪|年薪|收入|存款|余额"), "finance"),
]


def is_sensitive(text: str) -> str | None:
    """命中敏感模式返回类别名，否则 None。"""
    if not text:
        return None
    for pat, cat in _SENSITIVE_PATTERNS:
        if pat.search(text):
            return cat
    return None


# ============================== canonical store ==============================
# 只放"无主"字段（当前仅 reply_style）。按 uid 分片、tmp+replace 原子写。
_store_lock = threading.Lock()


def _profile_file(uid: str) -> str:
    name = "user_profile.json" if uid in (None, "default") else f"user_profile_{uid}.json"
    return os.path.join(DATA_DIR, name)


def _resolve_uid(session_id: str | None = None) -> str:
    if session_id in (None, "", "default"):
        try:
            from agent.user_context import resolve_user
            return resolve_user(session_id)
        except Exception:
            return os.environ.get("CHARLIE_USER_ID", "default") or "default"
    try:
        from agent.user_context import resolve_user
        return resolve_user(session_id)
    except Exception:
        return session_id


def _read_store(uid: str) -> dict:
    path = _profile_file(uid)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug(f"[user_profile] 读 store 失败({path}): {e}")
        return {}


def _write_store(uid: str, data: dict) -> None:
    path = _profile_file(uid)
    with _store_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".user_profile_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            log.warning(f"[user_profile] 写 store 失败({path}): {e}")


_DEFAULT_STYLE = {"tone": "casual", "length": "medium", "formality": "low",
                  "humor": False, "source": "default", "declared_at": None}


def _normalize_style(style: dict) -> dict:
    """把用户给的风格规整到合法枚举，非法值回退默认。"""
    out = dict(_DEFAULT_STYLE)
    if not isinstance(style, dict):
        return out
    if style.get("tone") in _TONES:
        out["tone"] = style["tone"]
    if style.get("length") in _LENGTHS:
        out["length"] = style["length"]
    if style.get("formality") in _FORMALITY:
        out["formality"] = style["formality"]
    if isinstance(style.get("humor"), bool):
        out["humor"] = style["humor"]
    out["source"] = "explicit"
    out["declared_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return out


def set_reply_style(session_id: str | None, style: dict) -> dict:
    """持久化用户显式声明的回复风格（reply_style 的持久新家）。返回归一化后的风格。"""
    uid = _resolve_uid(session_id)
    norm = _normalize_style(style)
    store = _read_store(uid)
    store["reply_style"] = norm
    store["updated_at"] = norm["declared_at"]
    _write_store(uid, store)
    return norm


def get_reply_style(session_id: str | None = None) -> dict:
    """读显式回复风格；未声明返回默认风格。"""
    uid = _resolve_uid(session_id)
    style = _read_store(uid).get("reply_style")
    return style if isinstance(style, dict) else dict(_DEFAULT_STYLE)


# ============================== 读：统一聚合视图 ==============================
def get_user_profile(session_id: str = "default") -> dict:
    """把散在各处的用户状态聚合成一个规范画像对象。纯读、无副作用。

    任一底层库缺失/异常 → 对应字段降级为空，绝不影响其它字段，更不抛异常。
    """
    uid = _resolve_uid(session_id)
    profile = {k: None for k in PROFILE_KEYS}
    profile["uid"] = uid
    sources: dict[str, str] = {}

    # identity / preferences / location / interests
    profile["preferences"] = _agg_preferences(uid, sources)
    profile["location"] = _agg_location(sources)
    profile["interests"] = _agg_interests(uid, sources)
    profile["identity"] = _agg_identity(uid, sources)
    # reply_style（canonical，本模块拥有）
    profile["reply_style"] = get_reply_style(session_id)
    if profile["reply_style"].get("source") == "explicit":
        sources["reply_style"] = "user_profile"
    # persona_state（情绪人设，透出；会变，非用户声明）
    profile["persona_state"] = _agg_persona(uid, sources)
    # conversation_background（最近对话 + 摘要）
    profile["conversation_background"] = _agg_background(session_id, uid, sources)

    profile["meta"] = {"sources": sources,
                       "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return profile


def _agg_preferences(uid: str, sources: dict) -> dict:
    try:
        from agent.preferences import list_preferences
        prefs = list_preferences(uid) or {}
        # 过滤内部保留键，避免把实现细节当偏好呈现
        return {k: v for k, v in prefs.items() if not str(k).startswith("__")}
    except Exception as e:
        log.debug(f"[user_profile] preferences 聚合跳过: {e}")
        return {}


def _agg_location(sources: dict) -> str:
    try:
        from agent.weather import pref_location
        loc = pref_location() or ""
        if loc:
            sources["location"] = "preferences"
        return loc
    except Exception as e:
        log.debug(f"[user_profile] location 聚合跳过: {e}")
        return ""


def _agg_interests(uid: str, sources: dict) -> list[str]:
    """喜好/事实：来自 memory_store facts 的 personal/preference/habit 三类。"""
    out: list[str] = []
    try:
        from agent.memory_store import get_facts_by_category
        for cat in ("personal", "preference", "habit"):
            for row in get_facts_by_category(cat, limit=20):
                v = row.get("value")
                if v and v not in out:
                    out.append(v)
        if out:
            sources["interests"] = "memory_store"
    except Exception as e:
        log.debug(f"[user_profile] interests 聚合跳过: {e}")
    return out


def _agg_identity(uid: str, sources: dict) -> dict:
    """身份：从 memory_store facts 里找名字类键。"""
    ident: dict = {}
    try:
        from agent.memory_store import get_fact
        for key in ("name", "我叫", "名字", "nickname"):
            row = get_fact(key)
            if row and row.get("value"):
                ident["name"] = row["value"]
                sources["identity"] = "memory_store"
                break
    except Exception as e:
        log.debug(f"[user_profile] identity 聚合跳过: {e}")
    return ident


def _agg_persona(uid: str, sources: dict) -> dict:
    try:
        from agent.persona import get_current_emotion, get_current_mood
        st = {"emotion": get_current_emotion(uid), "mood": get_current_mood(uid)}
        if st.get("mood") or st.get("emotion"):
            sources["persona_state"] = "persona"
        return st
    except Exception as e:
        log.debug(f"[user_profile] persona 聚合跳过: {e}")
        return {}


def _agg_background(session_id: str, uid: str, sources: dict) -> dict:
    bg: dict = {"recent": [], "session_summary": ""}
    try:
        from agent.history import _history_snapshot, _context_summaries
        snap = _history_snapshot(session_id)
        recent = [m.get("content", "") for m in snap[-6:]
                  if isinstance(m, dict) and m.get("content")]
        bg["recent"] = recent
        if recent:
            sources["conversation_background"] = "history"
    except Exception as e:
        log.debug(f"[user_profile] background.recent 聚合跳过: {e}")
    try:
        from agent.history import _context_summaries
        bg["session_summary"] = _context_summaries.get(session_id, "")
    except Exception:
        pass
    return bg


# ============================== 写：显式通道 ==============================
# 轻量确定性解析：用户明说的画像指令 → 路由到底层库。宁缺毋滥，认不准就跳过。
_LIKE_RE = re.compile(r"我(?:最)?(?:喜欢|爱)([^，。！？,.!?\s][^，。！？,.!?]*)")
_LIVE_RE = re.compile(r"我(?:住在|家在|坐标在|常住在)\s*([^，。！？,.!?\s][^，。！？,.!?]*)")
_REMEMBER_RE = re.compile(r"记住[：:]?\s*(.+)")

# "说简短点/别太正式/幽默点…" → reply_style 增量
_STYLE_RULES = [
    (re.compile(r"简短|简单点|简短一点|别啰嗦|少说点|精炼"), {"length": "short"}),
    (re.compile(r"详细|说清楚|展开讲|讲细一点|具体点"), {"length": "detailed"}),
    (re.compile(r"正式|严肃点|别开玩笑|认真点"), {"tone": "formal", "humor": False}),
    (re.compile(r"幽默|搞笑|俏皮|逗一点|风趣"), {"humor": True, "tone": "playful"}),
    (re.compile(r"说人话|别拽文|通俗|大白话|别文绉绉"), {"formality": "low", "tone": "casual"}),
    (re.compile(r"随意|放松|自然点|像朋友"), {"tone": "casual", "formality": "low"}),
]


def record_explicit(session_id: str, user_text: str) -> list[str]:
    """解析用户显式画像指令并路由落库，返回本次写入的动作描述（可审计）。

    覆盖：我喜欢X / 我住在X / 说简短点… / 记住X。认不准的一律跳过（返回空）。
    """
    uid = _resolve_uid(session_id)
    text = (user_text or "").strip()
    if not text:
        return []
    done: list[str] = []

    # 1) 回复风格（可多规则叠加，合并进现有显式风格）
    style_delta: dict = {}
    for pat, delta in _STYLE_RULES:
        if pat.search(text):
            style_delta.update(delta)
    if style_delta:
        merged = {**get_reply_style(session_id), **style_delta}
        set_reply_style(session_id, merged)
        done.append(f"reply_style:{sorted(style_delta)}")

    # 2) 居住位置 → preferences["location"]（pref_location() 自动接住）
    m = _LIVE_RE.search(text)
    if m:
        loc = m.group(1).strip(" ，。,.")
        if loc and not is_sensitive(loc):
            try:
                from agent.preferences import set_preference
                set_preference("location", loc, uid)
                done.append(f"location:{loc}")
            except Exception as e:
                log.debug(f"[user_profile] 位置写入跳过: {e}")

    # 3) 喜好 → memory_store fact(category=preference)
    m = _LIKE_RE.search(text)
    if m:
        like = m.group(1).strip(" ，。,.")
        # 砍掉"喜欢你"这类情感对象（非画像喜好）
        if like and like not in ("你",) and not like.startswith("你") and not is_sensitive(like):
            try:
                from agent.memory_store import upsert_fact
                upsert_fact(key=f"喜欢:{like}", value=like,
                            category="preference", confidence=1.0, source="explicit")
                done.append(f"interest:{like}")
            except Exception as e:
                log.debug(f"[user_profile] 喜好写入跳过: {e}")

    # 4) 泛指"记住X" → memory_store fact(explicit 源，跳过敏感过滤因为是用户主动)
    m = _REMEMBER_RE.search(text)
    if m and not done:  # 已被上面更具体规则吃掉的就不再重复记
        fact = m.group(1).strip(" ，。,.")
        if fact and len(fact) <= 120:
            try:
                from agent.memory_store import upsert_fact
                upsert_fact(key=f"记住:{fact[:24]}", value=fact,
                            category="personal", confidence=1.0, source="explicit")
                done.append(f"memory:{fact[:24]}")
            except Exception as e:
                log.debug(f"[user_profile] 泛指记忆写入跳过: {e}")

    return done


# ============================== 写：自动通道 ==============================
def apply_extracted(session_id: str, user_text: str, assistant_reply: str) -> dict:
    """每轮对话后台自动抽取沉淀（复用现有记忆抽取 + 敏感过滤）。

    复用 `_spawn_memory_thread`（= magic_memory.remember_conversation），它已经
    把对话增量写进 memory_store events/facts；本函数在此之上加一道**敏感闸**：
    命中 PII 的轮次跳过自动沉淀（显式"记住"不受限，由 record_explicit 处理）。

    USER_PROFILE_AUTOSTORE 未开 → 直接返回（不改任何行为）。
    """
    if os.environ.get("USER_PROFILE_AUTOSTORE") != "1":
        return {"skipped": True}
    blob = f"{user_text or ''}\n{assistant_reply or ''}"
    cat = is_sensitive(blob)
    if cat:
        log.info(f"[user_profile] 自动沉淀命中敏感({cat})，跳过本轮")
        return {"skipped": True, "sensitive": cat}
    try:
        from agent.llm import _spawn_memory_thread
        _spawn_memory_thread(user_text, assistant_reply)
        return {"skipped": False}
    except Exception as e:
        log.debug(f"[user_profile] 自动沉淀启动失败: {e}")
        return {"skipped": True, "error": str(e)[:80]}


# ============================== 注入：system prompt 片段 ==============================
def format_profile_for_prompt(profile: dict, uid: str | None = None) -> str:
    """把规范画像压成一段紧凑 system prompt 片段。

    只放**稳定、持久**的字段（偏好/喜好/位置/显式回复风格），不放会变的情绪
    人设（那由 persona 自己的通道注入，避免重复/打架）。空画像返回 ""。
    """
    if not profile:
        return ""
    lines: list[str] = []
    loc = profile.get("location")
    if loc:
        lines.append(f"用户在{loc}")
    interests = profile.get("interests") or []
    if interests:
        lines.append("喜好：" + "、".join(interests[:6]))
    prefs = profile.get("preferences") or {}
    if prefs:
        lines.append("偏好：" + "、".join(f"{k}={v}" for k, v in list(prefs.items())[:6]))
    rs = profile.get("reply_style") or {}
    if rs.get("source") == "explicit":
        tone = {"casual": "随意", "formal": "正式", "playful": "俏皮"}.get(rs.get("tone"), "")
        length = {"short": "简短", "medium": "适中", "detailed": "详细"}.get(rs.get("length"), "")
        bits = [b for b in (tone, length) if b]
        if rs.get("humor"):
            bits.append("可适度幽默")
        if bits:
            lines.append("用户偏好回复风格：" + "、".join(bits))
    if not lines:
        return ""
    return "【用户画像】" + "；".join(lines) + "。"
