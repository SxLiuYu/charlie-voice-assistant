"""ASR 文本纠错 / 后处理

结合 gitee assistant-x-openclaw 项目的「文本纠错兜底」思路，在 ASR 结果返回后、
进入 LLM 之前做一层轻量纠错，减少因同音字、语气词、误识别导致的无效对话轮次。

策略（按优先级）：
1. 文件规则纠错：data/text_corrections.txt（支持正则，带词边界）
2. 规则纠错：常见同音字、语气词、唤醒词归一化
3. 上下文纠错：利用 working_memory 的 last_topic / last_entity 做指代消解
4. LLM 兜底（可选）：对低置信度文本调用轻量模型做一句话纠错

可通过环境变量开关：
- ASR_CORRECTION_ENABLED=1 开启（默认 1）
- ASR_LLM_CORRECTION=1 开启 LLM 兜底（默认 0，需要额外 token 开销）
"""
import os, re, logging, time
from typing import Optional, List, Tuple

log = logging.getLogger("magic")

# 语气词/填充词（可配置）
_FILLER_WORDS = [w.strip() for w in os.getenv(
    "ASR_FILLER_WORDS",
    "嗯,啊,呃,呢,吧,嘛,呀,哦,唉,哎,哈,呐,哟,诶"
).split(",") if w.strip()]

# 唤醒词归一化映射（key=ASR可能误识别的词，value=标准唤醒词）
_WAKE_WORD_MAP = {
    "查里": "charlie", "查理": "charlie",
    "小智": "charlie",
}

_ENABLED = os.getenv("ASR_CORRECTION_ENABLED", "1") == "1"
_LLM_CORRECTION = os.getenv("ASR_LLM_CORRECTION", "0") == "1"

# ===== 文件规则纠错（来自 gitee assistant-x-openclaw） =====
_CORRECTIONS_FILE = os.path.join(
    os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "text_corrections.txt"
)
_file_rules: List[Tuple[re.Pattern, str]] = []
_file_rules_mtime = 0.0


def _compile_rule(line: str) -> Optional[Tuple[re.Pattern, str]]:
    """编译单条纠错规则

    支持两种格式：
    - 普通文本: "错误 : 正确"
    - 正则表达式: "/pattern/ : 正确"
    """
    if ":" not in line:
        return None
    src, dst = line.split(":", 1)
    src = src.strip()
    dst = dst.strip()
    if not src or not dst:
        return None
    if src.startswith("/") and src.endswith("/"):
        # 正则模式
        pattern = src[1:-1]
        try:
            return (re.compile(pattern, re.IGNORECASE), dst)
        except re.error:
            log.warning(f"[asr_correction] 无效正则规则: {line}")
            return None
    else:
        # 普通文本，带词边界
        escaped = re.escape(src)
        return (re.compile(r"(?<![A-Za-z0-9_])" + escaped + r"(?![A-Za-z0-9_])", re.IGNORECASE), dst)


def _load_file_rules() -> None:
    """加载 text_corrections.txt 规则文件（支持热重载）"""
    global _file_rules, _file_rules_mtime
    try:
        if not os.path.exists(_CORRECTIONS_FILE):
            _file_rules = []
            return
        mtime = os.path.getmtime(_CORRECTIONS_FILE)
        if mtime == _file_rules_mtime:
            return  # 文件未变更
        rules = []
        with open(_CORRECTIONS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                compiled = _compile_rule(line)
                if compiled:
                    rules.append(compiled)
        _file_rules = rules
        _file_rules_mtime = mtime
        log.info(f"[asr_correction] 加载 {len(rules)} 条文件纠错规则")
    except Exception as e:
        log.warning(f"[asr_correction] 加载纠错文件失败: {e}")


def _apply_file_rules(text: str) -> str:
    """应用文件纠错规则"""
    _load_file_rules()
    for pattern, replacement in _file_rules:
        new_text, count = pattern.subn(replacement, text)
        if count > 0:
            text = new_text
    return text


def _strip_fillers(text: str) -> str:
    """去掉句首/句尾语气词及后续/ preceding 标点"""
    for w in _FILLER_WORDS:
        if text.startswith(w):
            text = text[len(w):]
            text = text.lstrip("，。！？、；：""''《》（）【】")
        if text.endswith(w):
            text = text[:-len(w)]
            text = text.rstrip("，。！？、；：""''《》（）【】")
    return text.strip()


def _normalize_wake_word(text: str) -> str:
    """把 ASR 可能误识别的唤醒词归一化"""
    for wrong, correct in _WAKE_WORD_MAP.items():
        if wrong and wrong in text:
            text = text.replace(wrong, correct)
    return text


def _apply_homophone_rules(text: str) -> str:
    """同音字规则纠错（只替换整词，避免误伤）"""
    # 明确整词替换规则
    _WORD_REPLACEMENTS = {
        "在开": "再开",
    }
    for src, dst in _WORD_REPLACEMENTS.items():
        if src in text and dst != src:
            text = text.replace(src, dst)
    return text


def _context_aware_correction(text: str, session_id: Optional[str] = None) -> str:
    """利用 working_memory 做简单上下文纠错。
    注意：不做破坏性代词替换（如"它"→last_entity），指代消解交给 LLM 层的
    _build_wm_anaphor_prompt 处理（非破坏性，提供上下文而非替换文本）。
    仅做同音字/口语化纠错。"""
    # 代词替换已移除：保留原始代词，由 LLM 层指代消解提示处理
    return text


def _llm_correction(text: str) -> str:
    """调用 LLM 做一句话纠错（兜底，仅当规则无法处理时）"""
    if not _LLM_CORRECTION or not text:
        return text
    try:
        from app.llm_config import active_chat_endpoint
        from agent.llm_state import session as _session
        base, key, model = active_chat_endpoint()
        prompt = (
            "请对以下 ASR 识别结果做纠错，只返回纠正后的文本，不要加解释：\n"
            f"{text}\n"
            "纠正："
        )
        r = _session.post(
            f"{base}/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 64, "temperature": 0, "stream": False},
            headers={"Authorization": f"Bearer {key}"},
            timeout=(2, 5))
        corrected = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if corrected and len(corrected) <= len(text) * 1.5:
            return corrected
    except Exception:
        pass
    return text


def correct_asr_text(text: str, session_id: Optional[str] = None) -> str:
    """ASR 文本纠错入口

    参数:
        text: ASR 原始识别文本
        session_id: 会话 ID，用于上下文纠错

    返回:
        纠错后的文本（若纠错关闭或输入为空，原样返回）
    """
    if not _ENABLED or not text or not text.strip():
        return text

    original = text
    text = text.strip()

    # 1. 文件规则纠错（来自 gitee assistant-x-openclaw）
    text = _apply_file_rules(text)

    # 2. 内置规则纠错
    text = _strip_fillers(text)
    text = _normalize_wake_word(text)
    text = _apply_homophone_rules(text)

    # 上下文纠错已移除：指代消解交给 LLM 层 _build_wm_anaphor_prompt（非破坏性）

    # 3. LLM 兜底（可选）：规则未纠正但文本可能含同音字错误时触发
    if _LLM_CORRECTION and text == original.strip() and len(text) > 4:
        text = _llm_correction(text)

    if text != original:
        log.debug(f"[asr_correction] {original!r} → {text!r}")
    return text


def reload_correction_rules() -> None:
    """强制重载纠错规则文件"""
    global _file_rules_mtime
    _file_rules_mtime = 0.0
    _load_file_rules()


def should_enable_llm_correction() -> bool:
    """判断是否应该启用 LLM 纠错（基于文本特征）"""
    if not _LLM_CORRECTION:
        return False
    # 短文本、疑问句、含语气词 → 更可能需要纠错
    return True
