"""
Charlie - 语音Agent核心
语音闭环: ASR(qwen3-asr) → 大脑(deepseek-v4-flash+Qwen-Agent+MCP) → TTS(qwen3-tts)
连接韧性: Session复用 + 自动重试 + 异常降级
对话记忆: 跨请求保留历史上下文，支持多轮连续对话，持久化到磁盘
"""
import os, sys, json, copy, base64, requests, datetime, time, logging, asyncio, re, random, tempfile, threading, fcntl
from typing import Optional, Generator, Tuple, List, Dict, Any, Callable
from contextlib import contextmanager
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

log = logging.getLogger("magic")


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", PROJECT_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

FINNA = os.getenv("FINNA_BASE", "https://www.finna.com.cn/v1")
# ASR/TTS 全部使用百度智能云(免费, 0.38s, 零成本)
# ASR 免费 10h/月, TTS 有免费额度
BAIDU_APP_ID = os.getenv("BAIDU_APP_ID", "")
BAIDU_API_KEY = os.getenv("BAIDU_API_KEY", "")
BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY", "")
_baidu_token = {"token": "", "at": 0.0}
_baidu_token_lock = threading.Lock()
BAIDU_TOKEN_FILE = os.path.join(DATA_DIR, ".baidu_token.json")
# 启动时从磁盘恢复百度 token，避免重启后冷启动
if os.path.exists(BAIDU_TOKEN_FILE):
    try:
        with open(BAIDU_TOKEN_FILE) as f:
            saved = json.load(f)
            if isinstance(saved, dict) and saved.get("token"):
                _baidu_token["token"] = saved["token"]
                _baidu_token["at"] = saved.get("at", 0.0)
    except Exception:
        pass
# 支持多GLM密钥故障转移(GLM_KEY, GLM_KEY_2, GLM_KEY_3...)
_glm_keys = [os.getenv("GLM_KEY", "")]
for i in range(2, 6):
    k = os.getenv(f"GLM_KEY_{i}", "")
    if k:
        _glm_keys.append(k)
_glm_key_idx = 0  # 当前使用的密钥索引
_glm_key_failures = {}  # {key_prefix: failure_count}

def _get_glm_key() -> str:
    """获取当前GLM密钥(支持故障转移)"""
    return _glm_keys[_glm_key_idx] if _glm_key_idx < len(_glm_keys) else _glm_keys[0]

def _rotate_glm_key() -> bool:
    """轮换到下一个可用的GLM密钥, 返回是否成功"""
    global _glm_key_idx
    if len(_glm_keys) <= 1:
        return False  # 只有一个密钥,无法轮换
    _glm_key_idx = (_glm_key_idx + 1) % len(_glm_keys)
    log.warning(f"[glm] 密钥故障转移: 切换到密钥#{_glm_key_idx + 1}")
    return True
EMPTY_ASR_TEXT = "(未识别到语音)"
EMPTY_ASR_REPLY = "抱歉，我没听清，请再说一遍。"
LOW_INTENT_ASR_REPLY = "嗯嗯，我在。"
MAX_RETRIES = 3
RETRY_BACKOFF = [3, 10, 30]  # 秒，429限流退避(第1次3s, 第2次10s, 第3次30s)
RETRY_AFTER_CAP = float(os.getenv("ASSISTANT_KID_RETRY_AFTER_CAP", "60"))  # 尊重服务端Retry-After, 最大60s
INTENT_FAILURE_THRESHOLD = int(os.getenv("ASSISTANT_KID_INTENT_FAILURE_THRESHOLD", "2"))
INTENT_FAILURE_COOLDOWN = float(os.getenv("ASSISTANT_KID_INTENT_FAILURE_COOLDOWN", "30"))

_LOW_INTENT_STRIP_RE = re.compile(
    r"[\s，。！？、,.!?~～…\-—_:：；;\"'“”‘’（）()【】\[\]{}<>《》〈〉]+"
)
_LOW_INTENT_FILLER_CHARS = set("嗯哦啊呃哈噢喔诶呀吧呢嘛哎")
_LOW_INTENT_ENGLISH_RE = re.compile(
    r"^(?:h+m+|u(?:h+|m+|hm+)|a+h+|o+h+|e+r+m?)$"
)


def is_low_intent_asr(text: str) -> bool:
    """保守识别纯语气词，避免把确认/答应类短答误挡在大脑之外。"""
    normalized = _LOW_INTENT_STRIP_RE.sub("", text or "").lower()
    if not normalized:
        return True
    if len(normalized) > 16:
        return False
    if _LOW_INTENT_ENGLISH_RE.fullmatch(normalized):
        return True
    return all(char in _LOW_INTENT_FILLER_CHARS for char in normalized)


# 常见中文虚词/功能词（不成句时大概率是ASR碎片）
_GARBLED_FILLER_CHARS = set('的了呢吗啊哦呃呀吧哼哈嘿哟哎嗯呜喵喂嗨')

# 有效中文短指令中常见动词/名词（如果文本完全不包含这些，大概率是碎片）
_VALID_ROOT_CHARS = set('在打开关几现点今明后天气温度电视空调提醒日程搜索文件读写取消停止继续对好好的播放音乐随机首歌听唱暂停换下一首上一首')

# 低意图/碎词语料：这些词虽短但有效，不应判定为乱码
_VALID_SHORT_TEXTS = {
    '你好', '谢谢', '好的', '谢谢', '再见', '可以', '不行', '没有',
    '几点了', '几点啦', '在吗', '在听吗', '打开空调', '关闭空调',
    '打开电视', '关闭电视', '帮我打开空调', '帮我关闭空调', '开空调', '关空调',
    '现在几点了', '现在几点', '在听吗', '打开', '关闭',
    '调温度', '调低', '调高', '搜索', '取消', '停止', '继续',
    '在不在', '在不在呀', '在不在啊', '你以在吗', '你在吗', '在不在呢',
    '怎么了', '干嘛', '干啥', '咋了', '啥事', '什么事',
}


def is_garbled_asr(text: str) -> bool:
    """判断 ASR 输出是否为乱码/碎片（不构成有效指令）。"""
    if not text:
        return False
    stripped = text.strip(" ，。！？、,.!?~～…—_:：；;\"'""''（）()【】[]{}<>《》〈〉\n\r\t")
    if not stripped:
        return True
    length = len(stripped)
    # 1. 已知的短指令 → 有效
    if stripped in _VALID_SHORT_TEXTS or stripped.rstrip('。？!') in _VALID_SHORT_TEXTS:
        return False
    # 2. 纯虚词/语气词堆叠（≤8字）→ 乱码
    if length <= 8 and all(c in _GARBLED_FILLER_CHARS for c in stripped):
        return True
    # 3. 极短碎片（≤2字且不含有效字符）→ 乱码（如纯标点、纯空格、乱码碎片）
    if length <= 2 and not any(c in _VALID_ROOT_CHARS for c in stripped):
        return True
    # 4. 长文本（≥6字）但不含任何有效动词/名词根 → 乱码
    if length >= 6 and not any(c in _VALID_ROOT_CHARS for c in stripped):
        return True
    # 5. 长度适中但虚词占比 ≥ 60% → 乱码（碎片句）
    if 5 <= length <= 12:
        filler_count = sum(1 for c in stripped if c in _GARBLED_FILLER_CHARS)
        if filler_count >= length * 0.6:
            return True
    return False

# ===== 连接池复用(调优: max_connections=10, keep_alive=30s) =====
import requests.adapters
_session = requests.Session()
_session.headers.update({"Connection": "keep-alive"})
# 连接池调优: 每个主机最多10个连接, 超时30秒
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,    # 连接池大小
    pool_maxsize=10,        # 最大连接数
    max_retries=0,          # 重试由_retry()处理
    pool_block=False,       # 不阻塞, 满了直接新建
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# ===== 响应缓存(60秒TTL, 减少重复GLM调用) =====
_cache = {}
_CACHE_TTL = 300  # 秒 (5分钟, 提高重复问题命中率)
_CACHE_MAX = 50

def _cache_get(text: str) -> Optional[str]:
    """获取缓存响应(60秒内有效)"""
    key = f"text\x00{text.strip().lower()}"
    if key in _cache:
        reply, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return reply
        del _cache[key]
    return None

def _cache_get_interrupted(text: str, interrupted_reply: str) -> Optional[str]:
    key = f"interrupted\x00{interrupted_reply.strip()[:200].lower()}\x00{text.strip().lower()}"
    if key in _cache:
        reply, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return reply
        del _cache[key]
    return None

def _cache_set(text: str, reply: str, interrupted_reply: str = "") -> None:
    """设置缓存响应"""
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))  # 移除最旧的
    if interrupted_reply.strip():
        key = f"interrupted\x00{interrupted_reply.strip()[:200].lower()}\x00{text.strip().lower()}"
    else:
        key = f"text\x00{text.strip().lower()}"
    _cache[key] = (reply, time.time())

# ===== 对话历史(多会话, 跨请求持久化) =====
_history = []  # 默认会话历史(向后兼容)
_sessions = {"default": _history}  # 多会话: {session_id: [history]}
MAX_HISTORY = 20  # 每个会话保留最近20轮对话(40条消息)
MAX_SESSIONS = 10  # 最多10个并发会话
_history_lock = threading.Lock()  # 防止多线程同时修改对话历史
HISTORY_FILE = os.path.join(DATA_DIR, "conversation_history.json")
HISTORY_LOCK_FILE = HISTORY_FILE + ".lock"
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")
_history_file_signature = None
_history_file_cache = None
_history_file_lock = threading.Lock()
_history_save_seq = 0


def _history_file_sig() -> Optional[tuple]:
    try:
        st = os.stat(HISTORY_FILE)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@contextmanager
def _locked_history_file(shared: bool = False):
    with open(HISTORY_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read_history_file_locked() -> Optional[dict]:
    """Read and cache the history file while holding ``_history_file_lock``."""
    global _history_file_signature, _history_file_cache

    sig = _history_file_sig()
    if sig is None:
        _history_file_signature = None
        _history_file_cache = None
        return None
    if sig == _history_file_signature and _history_file_cache is not None:
        return _history_file_cache

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        _history_file_signature = None
        _history_file_cache = None
        return None

    if isinstance(data, dict):
        parsed = data
    elif isinstance(data, list):
        parsed = {"default": data}
    else:
        parsed = {}

    normalized = {}
    for session_id, messages in parsed.items():
        if isinstance(messages, list):
            normalized[session_id] = messages
    _history_file_signature = sig
    _history_file_cache = normalized
    return normalized


def _read_history_file() -> Optional[dict]:
    """Read the history file without blocking in-memory history access."""
    with _history_file_lock, _locked_history_file(shared=True):
        return _read_history_file_locked()


def _searchable_history(session_id: str = "default") -> list:
    """Return the most complete in-memory or on-disk history for search."""
    with _history_lock:
        memory_hist = _sessions.get(session_id)
        memory = list(memory_hist) if isinstance(memory_hist, list) else []
    memory_count = len(memory)

    file_data = _read_history_file()
    if file_data is None:
        return memory

    file_hist = file_data.get(session_id, [])
    if not isinstance(file_hist, list):
        file_hist = []

    if len(file_hist) > memory_count:
        return list(file_hist)
    return memory

def _get_history(session_id: str = "default") -> list:
    """获取指定会话的历史(不存在则自动创建)"""
    with _history_lock:
        if session_id not in _sessions:
            _sessions[session_id] = []
            # 超过最大会话数时移除最旧的(非default)
            if len(_sessions) > MAX_SESSIONS:
                for k in list(_sessions.keys()):
                    if k != "default":
                        del _sessions[k]
                        break
        return _sessions[session_id]


def _history_snapshot(session_id: str = "default") -> list:
    """Return a stable copy of one session for request serialization."""
    with _history_lock:
        if session_id not in _sessions:
            _sessions[session_id] = []
            if len(_sessions) > MAX_SESSIONS:
                for k in list(_sessions.keys()):
                    if k != "default":
                        del _sessions[k]
                        break
        hist = _sessions.get(session_id, [])
        if not isinstance(hist, list):
            return []
        return list(hist)


def _session_summaries() -> list:
    """Build session metadata under the history lock."""
    summaries = []
    with _history_lock:
        for session_id, hist in _sessions.items():
            if not isinstance(hist, list):
                continue
            last_message = ""
            if hist and isinstance(hist[-1], dict):
                last_message = str(hist[-1].get("content", ""))[:50]
            summaries.append({
                "session_id": session_id[:16] + "..." if len(session_id) > 16 else session_id,
                "message_count": len(hist),
                "last_message": last_message,
            })
    return summaries

def _save_history() -> None:
    global _history_file_signature, _history_file_cache, _history_save_seq

    with _history_lock:
        # Only copy under the history lock; disk I/O happens outside this critical section.
        snapshot = {
            session_id: list(messages)
            for session_id, messages in _sessions.items()
            if isinstance(messages, list)
        }
        save_seq = _history_save_seq + 1
        _history_save_seq = save_seq

    temp_path = None
    with _history_file_lock, _locked_history_file(shared=False):
        if save_seq != _history_save_seq:
            return
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=os.path.dirname(HISTORY_FILE),
                delete=False,
            ) as f:
                temp_path = f.name
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if save_seq != _history_save_seq:
                return
            os.replace(temp_path, HISTORY_FILE)
            temp_path = None

            if save_seq == _history_save_seq:
                _history_file_signature = _history_file_sig()
                _history_file_cache = snapshot
        except (OSError, IOError) as e:
            if save_seq == _history_save_seq:
                _history_file_signature = None
                _history_file_cache = None
            log.warning(f"历史保存失败: {e}")
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

def _append_history(hist: list, user_text: str, assistant_reply: str) -> None:
    """追加一轮对话到历史列表并持久化(线程安全, 带截断)"""
    with _history_lock:
        ts = datetime.datetime.now().isoformat()
        hist.append({'role': 'user', 'content': user_text, 'ts': ts})
        hist.append({'role': 'assistant', 'content': assistant_reply, 'ts': ts})
        if len(hist) > MAX_HISTORY * 2:
            del hist[:len(hist) - (MAX_HISTORY * 2)]
    _save_history()

def _load_history() -> None:
    global _history, _sessions, _history_file_signature, _history_file_cache

    data = _read_history_file()
    with _history_lock:
        if data is None:
            _history = []
            _sessions = {"default": _history}
            return

        _sessions = {
            session_id: list(messages)
            for session_id, messages in data.items()
            if isinstance(messages, list)
        }
        _history = _sessions.get("default", [])
        if not isinstance(_history, list):
            _history = []
        _sessions["default"] = _history

_load_history()

# ===== 对话上下文管理(token感知截断) =====
MAX_CONTEXT_TOKENS = 4000
_context_summaries = {}  # per-session summaries
MAX_SUMMARY_LEN = 200  # 历史对话token预算(留空间给系统提示+新消息+回复)
_TOKEN_CHINESE_RE = re.compile(r'[\u4e00-\u9fff]')
_TOKEN_ENGLISH_RE = re.compile(r'[a-zA-Z]+')

def _estimate_tokens(text: str) -> int:
    """粗略估算token数: 中文1字≈1.5token, 英文1词≈1.3token, 符号1个≈1token"""
    if not text:
        return 0
    # 中文字符数
    cn_matches = _TOKEN_CHINESE_RE.findall(text)
    cn = len(cn_matches)
    # 英文单词数
    en_matches = _TOKEN_ENGLISH_RE.findall(text)
    en = len(en_matches)
    # 其他字符(符号/数字)
    other = len(text) - cn - sum(len(w) for w in en_matches)
    return int(cn * 1.5 + en * 1.3 + other * 0.5)

def _estimate_msg_tokens(msg: dict) -> int:
    """估算单条消息的token数(含role开销)"""
    content = msg.get("content", "")
    return _estimate_tokens(content) + 4  # role标记约4token

def _trim_history_tokens(hist: list, max_tokens: int = MAX_CONTEXT_TOKENS, session_id: str = "default") -> None:
    """原地截断对话历史, 保持在token预算内。
    策略: 从最新的开始保留, 如果超限则移除最旧的。
    总是保留最近2轮(4条消息)作为即时上下文。
    """
    if len(hist) <= 4:
        return  # 不超过4条不截断
    token_costs = [_estimate_msg_tokens(m) for m in hist]
    total = sum(token_costs)
    if total <= max_tokens:
        return  # 未超限
    # Collect key info from removed messages
    removed_topics = []
    # 从最旧的开始移除, 但保留最近4条
    min_keep = 4
    remove_count = 0
    while len(hist) - remove_count > min_keep and total > max_tokens:
        msg = hist[remove_count]
        total -= token_costs[remove_count]
        # Extract key info for context summary
        content = msg.get("content", "")[:50]
        if content:
            removed_topics.append(content[:15] if msg.get("role") == "user" else content[:10])
        remove_count += 1
    if remove_count:
        del hist[:remove_count]
    if removed_topics:
        old_sum = _context_summaries.get(session_id, "")
        new_part = ", ".join(removed_topics[-5:])
        combined = (old_sum + ", " + new_part) if old_sum else new_part
        _context_summaries[session_id] = combined[-MAX_SUMMARY_LEN:]
        if session_id == "default":
            pass
        log.info(f"[context] {session_id[:8]} summary: {_context_summaries[session_id][:60]}")
    log.debug(f"[context] 截断至{len(hist)}条({total}tok, 预算{max_tokens})")

def reset_history(session_id: str = "default") -> None:
    """重置指定会话的历史(原地清空, 保持引用有效)"""
    with _history_lock:
        hist = _sessions.get(session_id)
        if hist is not None:
            hist.clear()  # 原地清空, 不重新赋值
        else:
            _sessions[session_id] = []
    _save_history()

# ===== 用户偏好系统(越用越懂你) =====
PREFS_FILE = os.path.join(DATA_DIR, "preferences.json")
PREFS_LOCK_FILE = PREFS_FILE + ".lock"
_preferences = {}
_prefs_lock = threading.RLock()  # RLock: 允许同线程重入(防死锁)
_preferences_revision = 0
_preferences_file_lock = threading.Lock()
_preferences_save_seq = 0
_preferences_file_signature = None


@contextmanager
def _locked_preferences(shared: bool = False):
    with open(PREFS_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _preferences_file_signature_now():
    try:
        st = os.stat(PREFS_FILE)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _read_locked_preferences() -> dict:
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            prefs = json.load(f)
            return prefs if isinstance(prefs, dict) else {}
    except Exception:
        return {}


def _write_preferences_temp_locked(prefs: dict) -> str:
    """在已持有偏好文件锁时写入临时文件，返回临时路径供调用方决定何时替换。"""
    directory = os.path.dirname(PREFS_FILE)
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
    return temp_path


def _write_locked_preferences(prefs: dict) -> None:
    """在已持有偏好文件锁时原子写入偏好文件。"""
    temp_path = ""
    try:
        temp_path = _write_preferences_temp_locked(prefs)
        os.replace(temp_path, PREFS_FILE)
        temp_path = ""
    except Exception:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def _bump_preferences_revision() -> None:
    global _preferences_revision
    _preferences_revision += 1

def _load_preferences() -> None:
    """加载用户偏好"""
    global _preferences, _preferences_revision, _preferences_file_signature
    with _locked_preferences(shared=True):
        prefs = _read_locked_preferences()
        signature = _preferences_file_signature_now()
    with _prefs_lock:
        _preferences = prefs
        _preferences_file_signature = signature
        _bump_preferences_revision()


def _refresh_preferences_if_changed() -> bool:
    """检测其他进程对偏好文件的修改并重载内存快照。"""
    global _preferences, _preferences_revision, _preferences_file_signature

    with _prefs_lock:
        old_signature = _preferences_file_signature

    with _locked_preferences(shared=True):
        new_signature = _preferences_file_signature_now()
        if new_signature == old_signature:
            return False
        prefs = _read_locked_preferences()

    with _prefs_lock:
        if new_signature == _preferences_file_signature:
            return False
        _preferences = prefs
        _preferences_file_signature = new_signature
        _bump_preferences_revision()

    return True

def _save_preferences() -> None:
    """原子保存当前内存偏好；供低层调用和并发回归测试使用。"""
    global _preferences_save_seq, _preferences_file_signature

    with _prefs_lock:
        snapshot = dict(_preferences)
        save_seq = _preferences_save_seq + 1
        _preferences_save_seq = save_seq

    with _preferences_file_lock:
        with _locked_preferences(shared=False):
            if save_seq != _preferences_save_seq:
                return
            disk_prefs = _read_locked_preferences()
            merged = dict(disk_prefs)
            merged.update(snapshot)
            if save_seq != _preferences_save_seq:
                return
            temp_path = ""
            try:
                temp_path = _write_preferences_temp_locked(merged)
                if save_seq != _preferences_save_seq:
                    return
                os.replace(temp_path, PREFS_FILE)
                temp_path = ""
            except (OSError, IOError) as e:
                log.warning(f"[prefs] 保存失败: {e}")
                return
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            _preferences_file_signature = _preferences_file_signature_now()


def _commit_preferences(mutate: Callable[[dict], Any]) -> Any:
    """在跨进程排他锁内合并磁盘偏好、提交变更并原子落盘。"""
    global _preferences_file_signature

    with _preferences_file_lock:
        with _locked_preferences(shared=False):
            disk_prefs = _read_locked_preferences()
            with _prefs_lock:
                merged = dict(disk_prefs)
                merged.update(_preferences)
                result = mutate(merged)
                _preferences.clear()
                _preferences.update(merged)
                _bump_preferences_revision()

            try:
                _write_locked_preferences(merged)
            except (OSError, IOError) as e:
                log.warning(f"[prefs] 保存失败: {e}")
                return result
            _preferences_file_signature = _preferences_file_signature_now()
    return result

def set_preference(key: str, value: str) -> str:
    """设置用户偏好(供MCP工具调用)"""
    _commit_preferences(lambda prefs: prefs.__setitem__(key, value))
    log.info(f"[prefs] 设置偏好: {key}={value[:30]}")
    return f"已记住：{key}={value}"

def get_preference(key: str) -> str:
    """获取用户偏好"""
    _refresh_preferences_if_changed()
    with _prefs_lock:
        return _preferences.get(key, "")

def preference_count() -> int:
    """返回偏好数量，不复制偏好字典。"""
    _refresh_preferences_if_changed()
    with _prefs_lock:
        return len(_preferences)

def list_preferences() -> dict:
    """列出所有用户偏好"""
    _refresh_preferences_if_changed()
    with _prefs_lock:
        return dict(_preferences)

def preferences_etag_token() -> str:
    """返回偏好数据的稳定版本 token，供 GET 接口在复制数据前做 304 判断。"""
    _refresh_preferences_if_changed()
    with _prefs_lock:
        return f"preferences:{_preferences_revision}:{len(_preferences)}"

def preferences_snapshot() -> tuple[dict, str]:
    """原子复制偏好并返回对应版本 token，避免复制期间变更导致 ETag 与内容不一致。"""
    _refresh_preferences_if_changed()
    with _prefs_lock:
        return dict(_preferences), f"preferences:{_preferences_revision}:{len(_preferences)}"

def preferences_conditional(
    etag_matches: Callable[[str], bool],
    etag_for_token: Callable[[str], str],
) -> tuple[dict | None, str]:
    """在偏好锁内先做 304 判断；只有未命中时才复制偏好字典。"""
    _refresh_preferences_if_changed()
    with _prefs_lock:
        token = f"preferences:{_preferences_revision}:{len(_preferences)}"
        if etag_matches(etag_for_token(token)):
            return None, token
        return dict(_preferences), token

def del_preference(key: str) -> str:
    """删除用户偏好"""
    def remove_key(prefs: dict) -> bool:
        if key not in prefs:
            return False
        del prefs[key]
        return True

    found = _commit_preferences(remove_key)
    if found:
        return f"已忘记：{key}"
    return f"未找到偏好：{key}"

_load_preferences()

# 系统提示词不含缓存，每次构建时自动刷新时间/待办等动态信息

# 按意图的工具说明: 每个意图只列出该MCP相关的工具
_MCP_SYSTEM_PROMPTS = {
    "amap-maps": (
        "可用工具：get_detailed_weather(城市)查详细天气+穿衣建议，get_location()获取当前位置。"
        "调用工具获取真实数据，不要编造天气信息。"
    ),
    "magic-music": (
        "可用工具：play_music(歌名或歌手名)搜索并播放音乐，search_music(关键词)搜索歌曲，"
        "play_random_music()随机播放一首，stop_music()停止播放，"
        "list_playlists()列出歌单，play_playlist()播放歌单或每日推荐。"
        "\\n播放音乐时不要说话，直接调用工具，不要回复任何文字。"
    ),
    "magic-reminder": (
        "可用工具：add_reminder(文本, 时间)设提醒，set_timer(分钟数, 提示语)倒计时，"
        "get_calendar_today()读取Apple日历。"
    ),
    "magic-notes": (
        "可用工具：save_note(标题, 内容)保存备忘录，list_notes()列出所有备忘录。"
    ),
    "magic-system": (
        "可用工具：set_volume(0-100)调音量，set_speech_speed(slow/normal/fast)调语速，"
        "system_status()查CPU/内存/磁盘/运行时间。"
    ),
    "magic-info": (
        "可用工具：get_current_time()获取当前时间，get_detailed_weather(城市)查天气+穿衣建议，"
        "get_news(主题, 条数)查新闻，get_location()获取当前位置，"
        "translate(文本, 目标语言)翻译，calculate(算式)计算或换算。"
    ),
    "magic-life": (
        "可用工具：open_lifestyle_app(intent)打开外卖/购物/打车等App，"
        "search_charging_stations(城市)查充电桩，control_tesla_ac(on/off)控特斯拉空调，"
        "leaving_home()出门场景（关空调+播天气+待办）。"
    ),
    "baize-skills": (
        "可用工具：搜索互联网获取实时信息。调用搜索工具获取真实结果，不要编造。"
    ),
    "filesystem": (
        "可用工具：读写文件。可以读写用户指定路径的文件。"
    ),
    "ac-control": (
        "可用工具：ac_control(on/off/cool/heat/dry/auto)控制空调，tv_control(power/volume_up/...)控制电视。"
        "工具调用如果没成功，如实说\"没控制成功，可能设备没连上\"。"
    ),
}

def _build_system_msg(mcp_set: str = "none") -> str:
    """按意图构建 system prompt: 基础规则 + 相关工具说明"""
    now = datetime.datetime.now()
    weekdays = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"]
    h = now.hour
    if 5 <= h < 9: period = "清晨"
    elif 9 <= h < 12: period = "上午"
    elif 12 <= h < 14: period = "中午"
    elif 14 <= h < 18: period = "下午"
    elif 18 <= h < 23: period = "晚上"
    else: period = "深夜"
    try:
        from app.reminders import _load_reminders
        rems = _load_reminders()
        today = now.strftime("%Y-%m-%d")
        today_rems = [r for r in rems if not r.get("done") and r.get("due","").startswith(today)]
        todo_ctx = f"今日有{len(today_rems)}项待办" if today_rems else "今日无待办"
    except Exception:
        todo_ctx = ""
    ctx = (f"当前时间：{now.strftime('%Y年%m月%d日')} {weekdays[now.weekday()]} "
           f"{period} {now.strftime('%H:%M')}。{todo_ctx}。")
    prefs_ctx = ""
    prefs = list_preferences()
    if prefs:
        prefs_items = [f"{k}: {v}" for k, v in list(prefs.items())[:10]]
        prefs_ctx = f"\n用户偏好(请主动应用)：{'，'.join(prefs_items)}。"
    summary = _context_summaries.get("default", "")
    summary_ctx = f"\n之前对话过的内容: {summary}。" if summary else ""
    # 基础规则(所有意图都适用)
    result = (f"你是Charlie，用户的私人AI助理。{ctx}{prefs_ctx}{summary_ctx}\n"
                "原则：\n"
                "0. 禁止输出任何占用语（\"让我想想\"\"稍等一下\"\"嗯\"\"让我思考一下\"\"想一想\"\"让我看看\"\"我看看\"\"好的\"\"好\"\"嗯嗯\"等），"
                "想好了直接说答案，直接输出第一句就是答案。\n"
                "1. 回复极简，能1句说完不用2句。绝不寒暄——用户说\"你好\"你回\"在呢，说\"，不说\"嘿又见面啦今天怎么样\"。\n"
            "2. 用户说\"在吗\"只回\"在\"，不多说。\n"
            "3. 能用工具就用工具，给真实数据。\n"
            "4. 如果用户要你做做不到的事，直接说\"这个我做不了\"。\n"
            "5. 工具调用如果没成功，如实说\"没控制成功，可能设备没连上\"，不要假装成功。\n"
            "6. 报时间直接说\"现在X点X分\"，不加任何前缀。\n"
            "7. 如果用户输入看起来是ASR误识别（不成句的碎片），回\"没听清，再说一遍？\"。\n")
    # 按意图追加工具说明
    tools_doc = _MCP_SYSTEM_PROMPTS.get(mcp_set, "")
    if tools_doc:
        result += f"\n{tools_doc}\n"
    return result

# ===== 带重试的请求封装 =====
def _http_error_message(exc: requests.exceptions.HTTPError, name: str) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        return f"{name}HTTP异常: {_exception_message(exc)}"
    text = ""
    try:
        text = (response.text or "").strip().replace("\n", " ")[:200]
    except Exception:
        text = ""
    return f"{name}HTTP异常: {status}" + (f" - {text}" if text else "")


def _exception_message(exc: BaseException) -> str:
    """返回适合日志展示的异常信息，空字符串异常至少保留类型名。"""
    message = str(exc).strip()
    return message or type(exc).__name__


def _is_retryable_http_status(status: int) -> bool:
    return status == 408 or status == 429 or status >= 500


def _retry_after_delay(response, attempt: int) -> float:
    raw = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        seconds = float(raw)
        if seconds >= 0:
            return min(seconds, RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        pass
    return RETRY_BACKOFF[attempt]


def _retry(fn, name: str = "请求") -> Any:
    """带重试的函数调用封装"""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            response = getattr(e, "response", None)
            status = getattr(response, "status_code", None)
            message = _http_error_message(e, name)
            if status is not None and not _is_retryable_http_status(status):
                log.warning(f"{name}第{attempt+1}次失败: {message}，不重试")
                raise Exception(message) from e
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                delay = _retry_after_delay(response, attempt)
                # 加30% jitter防止thundering herd
                delay += random.uniform(0, delay * 0.3)
                log.warning(f"{name}第{attempt+1}次失败: {message}，{delay:.0f}秒后重试...")
                time.sleep(delay)
            else:
                log.warning(f"{name}第{attempt+1}次失败: {message}，放弃")
        except requests.exceptions.Timeout as e:
            last_exc = e
            message = _exception_message(e)
            log.warning(f"{name}第{attempt+1}次超时: {message}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            message = _exception_message(e)
            log.warning(f"{name}第{attempt+1}次连接失败: {message}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        except Exception as e:
            last_exc = e
            message = _exception_message(e)
            log.warning(f"{name}第{attempt+1}次异常: {message}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        if attempt < MAX_RETRIES - 1:
            continue
    if isinstance(last_exc, requests.exceptions.HTTPError):
        raise Exception(_http_error_message(last_exc, name)) from last_exc
    if isinstance(last_exc, requests.exceptions.Timeout):
        raise Exception(f"{name}超时: {_exception_message(last_exc)}") from last_exc
    if isinstance(last_exc, requests.exceptions.ConnectionError):
        raise Exception(f"{name}连接失败: {_exception_message(last_exc)}") from last_exc
    raise Exception(f"{name}失败: {_exception_message(last_exc)}") from last_exc

# ===== TTS: 文字 → 音频bytes =====
def _tts_unisound(text: str) -> bytes:
    """云知声 U2-TTS (异步上传文本→轮询→下载音频) — 返回 WAV bytes"""
    import tempfile, subprocess, json as _json
    from pathlib import Path
    script = Path(__file__).resolve().parent / "skills" / "u2-tts-pro" / "scripts" / "synthesize_speech.py"
    if not script.exists():
        raise RuntimeError(f"云知声TTS技能未安装: {script}")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, str(script),
             "--voice-id", "cn_male_chenyu_fluent",
             "--text", text,
             "--format", "pcm",
             "--audio-sample-rate", "16000",
             "--output-path", tmp],
            capture_output=True, timeout=120, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"云知声TTS失败: {r.stderr}")
        data = _json.loads(r.stdout)
        path = data.get("summary", {}).get("downloaded_file_path", "")
        if path and Path(path).exists():
            return Path(path).read_bytes()
        raise RuntimeError(f"云知声TTS: 未获取音频文件, 返回 {data.get('summary', {})}")
    finally:
        try: os.unlink(tmp)
        except: pass

def tts(text: str) -> bytes:
    """文字→语音(WAV bytes) — 百度智能云 → 云知声(备用)"""
    cleaned = _clean_for_tts(text).strip()
    if not cleaned:
        return b""
    try:
        return _tts_baidu(cleaned)
    except Exception as e:
        log.warning(f"百度TTS失败，降级到云知声: {e}")
    try:
        return _tts_unisound(cleaned)
    except Exception as e:
        log.error(f"TTS最终失败: {e}")
        return b""

def tts_status() -> Dict[str, Any]:
    """TTS 状态 (百度在线)"""
    return {
        "active": False,
        "remaining_seconds": 0,
        "cooldown_seconds": 0,
        "consecutive_failures": 0,
        "failure_threshold": 0,
    }

# ===== ASR: 音频bytes → 文字 =====
# 百度智能云(免费, 0.38s) → 降级云知声(备用, 1.8亿Credits)

def _asr_unisound(audio_bytes: bytes, fmt: str = "mp3") -> str:
    """云知声 U2-ASR (异步上传→轮询→获取结果)"""
    import tempfile, subprocess, json as _json
    from pathlib import Path
    script = Path(__file__).resolve().parent / "skills" / "u2-asr-pro" / "scripts" / "transcribe_audio.py"
    if not script.exists():
        raise RuntimeError(f"云知声ASR技能未安装: {script}")
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as f:
        f.write(audio_bytes)
        tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, str(script), tmp, "--language", "zh-CN"],
            capture_output=True, timeout=90, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"云知声ASR失败: {r.stderr}")
        data = _json.loads(r.stdout)
        preview = data.get("summary", {}).get("transcript_preview", "")
        return preview.strip()
    finally:
        try: os.unlink(tmp)
        except: pass

def asr(audio_bytes: bytes, fmt: str = "mp3") -> str:
    """ASR: 百度智能云 → 云知声(备用)"""
    try:
        return _asr_baidu(audio_bytes, fmt)
    except Exception as e:
        log.warning(f"百度ASR失败，降级到云知声: {e}")
    try:
        return _asr_unisound(audio_bytes, fmt)
    except Exception as e:
        log.error(f"ASR最终失败: {e}")
        return ""


# ===== 百度智能云语音 (免费额度: 实时ASR 10h/月) =====
def _baidu_get_token() -> str:
    """获取百度 access_token (缓存到过期前1天)"""
    now = time.time()
    with _baidu_token_lock:
        if _baidu_token["token"] and now - _baidu_token["at"] < 86400 * 28:
            return _baidu_token["token"]
    url = "https://aip.baidubce.com/oauth/2.0/token"
    r = _session.post(url, params={
        "grant_type": "client_credentials",
        "client_id": BAIDU_API_KEY,
        "client_secret": BAIDU_SECRET_KEY,
    }, timeout=10)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token", "")
    with _baidu_token_lock:
        _baidu_token["token"] = token
        _baidu_token["at"] = now
    # 持久化到磁盘，重启后热启动
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=DATA_DIR, prefix=".baidu_token_tmp", suffix=".json", delete=False
        ) as f:
            json.dump({"token": token, "at": now}, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, BAIDU_TOKEN_FILE)
    except Exception:
        pass
    return token


def _asr_baidu(audio_bytes: bytes, fmt: str = "mp3") -> str:
    """百度短语音识别 (REST API, 免费 10万次/企业)"""
    # 百度 ASR 需要 PCM 格式 (去 WAV header) 或完整 WAV
    # 先转 16kHz mono PCM
    import base64
    try:
        import subprocess
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1",
             "-f", "wav", "pipe:1"],
            input=audio_bytes, capture_output=True, timeout=10)
        if r.stdout and len(r.stdout) > 100:
            # 去掉 WAV header (前44字节), 只发 PCM
            wav = r.stdout
            audio_bytes = wav[44:] if len(wav) > 44 else wav
            fmt = "pcm"
    except Exception:
        pass

    token = _baidu_get_token()
    speech_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    speech_len = len(audio_bytes)

    # 注意: token 和 dev_pid 必须放在 JSON body 里, 不能放 URL params
    # dev_pid 1537 = 普通话近场模型 (默认)
    r = _session.post("https://vop.baidu.com/server_api",
         json={
             "format": fmt, "rate": 16000, "channel": 1,
             "token": str(token),
             "cuid": "charlie-mac",
             "dev_pid": 1537,
             "speech": speech_b64, "len": speech_len,
         }, timeout=10)
    r.raise_for_status()
    data = r.json()
    err_no = data.get("err_no", -1)
    if err_no != 0:
        raise RuntimeError(f"百度ASR错误 {err_no}: {data.get('err_msg', '')}")
    return data.get("result", [""])[0]


def _tts_baidu(text: str) -> bytes:
    """百度在线 TTS (免费额度, 女声/男声)"""
    token = _baidu_get_token()
    # per=0 女声, per=1 男声, per=4 度逍遥(男), per=5 度小丫(女)
    url = "https://tsn.baidu.com/text2audio"
    r = _session.post(url, data={
        "tex": text,
        "tok": token,
        "cuid": "charlie-mac",
        "ctp": 1,
        "lan": "zh",
        "spd": 5,
        "pit": 5,
        "vol": 9,
        "per": 3,  # 度逍遥(成熟男声)
        "aue": 6,  # 6=MP3, 3=WAV
    }, timeout=15)
    content_type = r.headers.get("Content-Type", "")
    if "json" in content_type:
        err = r.json()
        raise RuntimeError(f"百度TTS错误 {err.get('err_no', -1)}: {err.get('err_msg', '')}")
    r.raise_for_status()
    return r.content

# ===== 大脑: deepseek-v4-flash + Qwen-Agent + MCP =====
_UNKNOWN_KWARG_RE = re.compile(r"got an unexpected keyword argument '([^']+)'")


def _wrap_openai_create_unknown_kwargs(create_fn):
    """把 OpenAI SDK 不认识的上游私有参数移入 extra_body 后重试一次。"""
    if getattr(create_fn, "_assistant_kid_compat_wrapped", False):
        return create_fn

    def wrapped(*args, **kwargs):
        try:
            return create_fn(*args, **kwargs)
        except TypeError as exc:
            match = _UNKNOWN_KWARG_RE.search(str(exc))
            if not match:
                raise
            unknown_key = match.group(1)
            if unknown_key not in kwargs:
                raise
            retry_kwargs = copy.deepcopy(kwargs)
            extra_body = retry_kwargs.pop("extra_body", None)
            if not isinstance(extra_body, dict):
                extra_body = {}
            extra_body[unknown_key] = retry_kwargs.pop(unknown_key)
            retry_kwargs["extra_body"] = extra_body
            log.info(f"[brain] SDK不支持参数 {unknown_key}，改用 extra_body 重试")
            return create_fn(*args, **retry_kwargs)

    wrapped._assistant_kid_compat_wrapped = True
    return wrapped


def _install_openai_compat(brain) -> None:
    """为 Qwen-Agent 主大脑和记忆大脑安装 OpenAI 参数兼容层。"""
    targets = [getattr(brain, "llm", None)]
    mem = getattr(brain, "mem", None)
    if mem is not None:
        targets.append(getattr(mem, "llm", None))
    for llm in targets:
        if llm is None:
            continue
        for attr in ("_chat_complete_create", "_complete_create"):
            original = getattr(llm, attr, None)
            if callable(original):
                setattr(llm, attr, _wrap_openai_create_unknown_kwargs(original))


def _build_brain(mcp_set="all"):
    """构建大脑, mcp_set控制加载哪些MCP: none/all/单个MCP名"""
    # 内存检查: 防止OOM崩溃
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.percent > 90:
            raise RuntimeError(f"内存不足({mem.percent}%), 拒绝构建大脑防OOM")
        log.info(f"[brain] 内存检查通过: {mem.percent}% ({(mem.total-mem.available)//1073741824:.1f}GB可用)")
    except ImportError:
        pass
    from qwen_agent.agents import Assistant
    llm_cfg = {
        'model': 'deepseek-v4-flash', 'model_type': 'oai',  # 写死: 直连finna deepseek-v4-flash, 不路由不切换
        'api_base': FINNA, 'api_key': _get_glm_key(),
        'generate_cfg': {'use_raw_api': True, 'extra_body': {'extra_body': {'enable_thinking': False}}, 'max_tokens': 512},
    }
    # MCP服务器配置(可通过MCP_SERVERS环境变量控制启用哪些, 逗号分隔)
    all_mcp = {
        "amap-maps": {"command": "npx", "args": ["-y", "@amap/amap-maps-mcp-server"],
            "env": {"AMAP_MAPS_API_KEY": os.getenv("AMAP_KEY", "")}},
        "magic-music": {"command": sys.executable,
            "args": ["magic-music.py"], "cwd": os.getcwd()},
        "magic-reminder": {"command": sys.executable,
            "args": ["magic-reminder.py"], "cwd": os.getcwd()},
        "magic-notes": {"command": sys.executable,
            "args": ["magic-notes.py"], "cwd": os.getcwd()},
        "magic-system": {"command": sys.executable,
            "args": ["magic-system.py"], "cwd": os.getcwd()},
        "magic-info": {"command": sys.executable,
            "args": ["magic-info.py"], "cwd": os.getcwd()},
        "magic-life": {"command": sys.executable,
            "args": ["magic-life.py"], "cwd": os.getcwd()},
        "baize-skills": {"command": sys.executable,
            "args": ["baize_skills_mcp.py"], "cwd": os.getcwd(),
            "env": {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY", ""),
                    "ALIYUN_API_KEY": os.getenv("ALIYUN_API_KEY", "")}},
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", os.getenv("FS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))]},
        "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
        "sequential-thinking": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]},
        "ac-control": {"command": sys.executable,
            "args": ["mcp_ir_control.py"], "cwd": os.getcwd()},
    }
    # 按意图路由选择MCP
    enabled_env = os.getenv("MCP_SERVERS", "amap-maps,baize-skills,filesystem,magic-music,magic-reminder,magic-notes,magic-system,magic-info,magic-life").split(",")
    enabled_env = [s.strip() for s in enabled_env if s.strip()]
    if mcp_set == "none":
        mcp_servers = {}
    elif mcp_set == "all":
        mcp_servers = {k: v for k, v in all_mcp.items() if k in enabled_env}
    else:
        mcp_servers = {mcp_set: all_mcp[mcp_set]} if mcp_set in all_mcp else {}
    log.info(f"[brain] 构建大脑 mcp_set={mcp_set}, 启用{len(mcp_servers)}个MCP: {list(mcp_servers.keys())}")
    tools = [{"mcpServers": mcp_servers}] if mcp_servers else []
    brain = Assistant(llm=llm_cfg, name='Charlie',
        system_message=_build_system_msg(mcp_set),
        function_list=tools)
    _install_openai_compat(brain)
    return brain

# ===== 意图路由: 用LLM快速判断需要哪些MCP =====
_brains = {}              # key=mcp_set, value=Assistant实例
_brain_build_time = 0     # 首次构建时间
_brain_failures = 0       # 连续失败计数
_brain_total_failures = 0  # 累计失败总数（不重置）
_brain_last_failure = 0   # 上次失败时间戳
_brain_last_success = 0   # 上次成功时间戳
_MAX_BRAIN_FAILURES = 5   # 连续失败5次后自动重建大脑(429限流单独阈值10)
_intent_failures = 0
_intent_disabled_until = 0.0
_intent_cache = {}  # 独立意图缓存字典, 不与对话缓存冲突

def intent_classifier_status() -> Dict[str, Any]:
    """只读本地意图分类熔断状态。"""
    now = time.time()
    remaining = max(0.0, _intent_disabled_until - now)
    return {
        "circuit_open": remaining > 0,
        "remaining_seconds": round(remaining, 1),
        "consecutive_failures": _intent_failures,
        "failure_threshold": INTENT_FAILURE_THRESHOLD,
        "cooldown_seconds": INTENT_FAILURE_COOLDOWN,
    }

def _classify_intent(text: str) -> str:
    """用本地Ollama Qwen3快速判断意图(108ms vs Finna 1500ms),返回MCP组合名"""
    global _intent_failures, _intent_disabled_until
    # 缓存: 相同文本不重复分类 (使用独立缓存字典, 不与对话缓存冲突)
    cached_intent = _intent_cache.get(text)
    if cached_intent:
        log.info(f"[intent] 缓存命中: {text[:30]} → {cached_intent}")
        return cached_intent
    now = time.time()
    if now < _intent_disabled_until:
        log.info(f"[intent] 本地分类冷却中，{_intent_disabled_until - now:.0f}秒内默认none")
        return "none"
    # 快速关键词预判: 如果文本明确包含领域关键词, 直接返回, 跳过Ollama调用
    _KEYWORD_MAP = [
        ({"天气", "气温", "下雨", "温度", "几度", "穿什么"}, "amap-maps"),
        ({"地图", "导航", "附近", "我在哪", "路线", "怎么走", "到哪"}, "amap-maps"),
        ({"搜索", "搜一下", "查一下", "查查", "百度", "谷歌", "购物", "买东西"}, "baize-skills"),
        ({"提醒", "定时", "闹钟", "备忘", "日程", "日历", "待办", "记一下", "提醒我"}, "magic-reminder"),
        ({"笔记", "备忘录", "记下来", "记一下"}, "magic-notes"),
        ({"音量", "说慢", "说快", "语速", "大声", "小声"}, "magic-system"),
        ({"状态", "运行", "负载", "设备"}, "magic-system"),
        ({"新闻", "头条", "热点"}, "magic-info"),
        ({"时间", "几点", "日期", "星期"}, "magic-info"),
        ({"翻译", "翻成", "英语说", "怎么说"}, "magic-info"),
        ({"计算", "算一下", "换算", "等于多少"}, "magic-info"),
        ({"放歌", "播放", "听歌", "放周杰伦", "音乐", "歌单", "停止播放", "每日推荐", "随机"}, "magic-music"),
        ({"空调", "电视", "制冷", "制热", "风扇", "开灯", "关灯"}, "ac-control"),
        ({"文件", "读文件", "写文件", "笔记"}, "filesystem"),
        ({"外卖", "点餐", "吃饭", "购物", "商品", "查一下", "充电桩", "特斯拉", "出门"}, "magic-life"),
    ]
    for keywords, mcp_name in _KEYWORD_MAP:
        if any(kw in text for kw in keywords):
            log.info(f"[intent] 关键词命中: '{text[:30]}' → {mcp_name}")
            _intent_cache[text] = mcp_name
            return mcp_name
    prompt = (
        "任务: 判断用户输入需要哪个工具, 只回一个词\n"
        "选项: none | amap-maps | baize-skills | filesystem | magic-music | magic-reminder | magic-notes | magic-system | magic-info | magic-life | ac-control\n"
        "规则:\n"
        "- none = 闲聊/问候/常识问答(你好/谢谢/讲个笑话)\n"
        "- amap-maps = 天气/地图/导航/我在哪/附近/路线/温度/几度\n"
        "- baize-skills = 搜索互联网/查资料/购物/买东西\n"
        "- magic-music = 播放/歌单/音乐/随机播放\n"
        "- magic-reminder = 提醒/日程/定时器/日历\n"
        "- magic-notes = 备忘录/笔记\n"
        "- magic-system = 音量/语速/设备状态\n"
        "- magic-info = 时间/新闻/翻译/计算\n"
        "- magic-life = 外卖/充电桩/特斯拉/出门\n"
        "- ac-control = 空调/电视/制冷/制热/风扇/开关/频道/灯光\n"
        "- filesystem = 文件/读写文件/笔记\n"
        "示例:\n"
        "  你好→none\n"
        "  今天天气怎么样→amap-maps\n"
        "  北京天气→amap-maps\n"
        "  搜一下薛之谦→baize-skills\n"
        "  帮我点杯咖啡→baize-skills\n"
        "  设提醒→magic-reminder\n"
        "  放歌→magic-music\n"
        "  打开空调→ac-control\n"
        "  音量调大→magic-system\n"
        "  现在几点→magic-info\n"
        f"用户输入: {text[:100]}\n"
        "工具: →")
    try:
        r = _session.post("http://localhost:11434/api/chat",
            json={"model": "qwen3.5:2b",
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False, "think": False,
                  "options": {"num_predict": 10, "temperature": 0}},
            timeout=(3, 10))
        raw = r.json().get("message", {}).get("content", "").strip().lower()
        # 后处理: 模型可能回 "magic"/"baize" 等截断词, 统一映射
        if "amap" in raw or "map" in raw: mcp = "amap-maps"
        elif "baize" in raw or "search" in raw: mcp = "baize-skills"
        elif "music" in raw: mcp = "magic-music"
        elif "remind" in raw: mcp = "magic-reminder"
        elif "note" in raw: mcp = "magic-notes"
        elif "system" in raw: mcp = "magic-system"
        elif "info" in raw: mcp = "magic-info"
        elif "life" in raw: mcp = "magic-life"
        elif "magic" in raw: mcp = "magic-music"
        elif "ac" in raw or "air" in raw or "control" in raw: mcp = "ac-control"
        elif "file" in raw or "fs" in raw: mcp = "filesystem"
        else: mcp = "none"
        _intent_failures = 0
        _intent_disabled_until = 0.0
        _intent_cache[text] = mcp
        log.info(f"[intent] '{text[:30]}' → {mcp} ({raw[:15]})")
        return mcp
    except Exception as e:
        # 1次快速重试(避免偶发超时导致降级)
        if not isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            # 非超时/连接错误, 不重试
            _intent_failures += 1
            if _intent_failures >= INTENT_FAILURE_THRESHOLD:
                _intent_disabled_until = now + INTENT_FAILURE_COOLDOWN
                log.warning(f"[intent] 连续失败{_intent_failures}次，暂停本地分类{INTENT_FAILURE_COOLDOWN:g}秒")
            log.warning(f"[intent] 本地分类失败,默认none: text='{text[:20]}' err={e}")
            return "none"
        # 超时/连接错误: 重试1次
        try:
            r = _session.post("http://localhost:11434/api/chat",
                json={"model": "qwen3.5:2b",
                      "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "think": False,
                      "options": {"num_predict": 10, "temperature": 0}},
                timeout=(3, 15))
            raw = r.json().get("message", {}).get("content", "").strip().lower()
            if "amap" in raw or "map" in raw: mcp = "amap-maps"
            elif "baize" in raw or "search" in raw: mcp = "baize-skills"
            elif "magic" in raw or "remind" in raw: mcp = "magic-phone"
            elif "ac" in raw or "air" in raw or "control" in raw: mcp = "ac-control"
            elif "file" in raw or "fs" in raw: mcp = "filesystem"
            else: mcp = "none"
            _intent_failures = 0
            _intent_disabled_until = 0.0
            _cache_set("__intent__:" + text, mcp)
            log.info(f"[intent] '{text[:30]}' → {mcp} ({raw[:15]}) [重试成功]")
            return mcp
        except Exception as e2:
            _intent_failures += 1
            if _intent_failures >= INTENT_FAILURE_THRESHOLD:
                _intent_disabled_until = now + INTENT_FAILURE_COOLDOWN
                log.warning(f"[intent] 连续失败{_intent_failures}次，暂停本地分类{INTENT_FAILURE_COOLDOWN:g}秒")
            log.warning(f"[intent] 本地分类重试仍失败,默认none: text='{text[:20]}' err={e2}")
            return "none"

def _get_brain(mcp_set="none"):
    """获取或构建指定MCP组合的大脑(带缓存)。
    每次返回前刷新 system_message 中的时间/待办等动态信息。"""
    global _brain_build_time
    if mcp_set not in _brains:
        _brains[mcp_set] = _build_brain(mcp_set)
        if not _brain_build_time:
            _brain_build_time = time.time()
        log.info(f"[brain] 大脑构建完成: mcp={mcp_set}, 缓存总数={len(_brains)}")
    # 刷新 system_message(时间、待办等每30s变化一次)
    _brains[mcp_set].system_message = _build_system_msg(mcp_set)
    return _brains[mcp_set]

def _ensure_event_loop():
    """确保当前线程有event loop(Qwen-Agent MCP可能需要)"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

def _record_brain_failure(error: str = ""):
    """记录大脑失败, 连续失败超过阈值时自动重建"""
    global _brain_failures, _brain_total_failures, _brain_last_failure, _brains
    _brain_failures += 1
    _brain_total_failures += 1
    _brain_last_failure = time.time()
    # 429限流不应该清缓存(缓存重建代价大, 429是临时性的)
    is_429 = '429' in error or 'Too Many' in error or 'rate' in error.lower()
    log.error(f"[brain] 失败#{_brain_failures}(累计{_brain_total_failures}): {error}" +
              (" [429限流, 不清缓存]" if is_429 else ""))
    if not is_429 and _brain_failures >= _MAX_BRAIN_FAILURES:
        log.warning(f"[brain] 连续失败{_brain_failures}次(非限流), 清除所有缓存大脑...")
        for k, b in list(_brains.items()):
            _cleanup_brain_processes(b)
        _brains.clear()
        _brain_failures = 0
    elif is_429 and _brain_failures >= 10:
        # 429连续10次才清(可能模型确实挂了)
        log.warning(f"[brain] 429连续{ _brain_failures}次, 清除缓存大脑...")
        for k, b in list(_brains.items()):
            _cleanup_brain_processes(b)
        _brains.clear()
        _brain_failures = 0

def _record_brain_success():
    """记录大脑成功, 重置失败计数"""
    global _brain_failures, _brain_last_success
    _brain_failures = 0
    _brain_last_success = time.time()

def _cleanup_brain_processes(brain_instance):
    """清理大脑实例关联的MCP子进程(防止僵尸进程)"""
    if brain_instance is None:
        return
    try:
        # Qwen-Agent的MCP服务器通过subprocess管理
        # 清理function_list中的MCP server connections
        if hasattr(brain_instance, '_function_list'):
            for func in (brain_instance._function_list or []):
                if isinstance(func, dict) and 'mcpServers' in func:
                    for name, cfg in func['mcpServers'].items():
                        # 尝试关闭MCP client连接
                        try:
                            if hasattr(brain_instance, '_mcp_clients') and name in (brain_instance._mcp_clients or {}):
                                client = brain_instance._mcp_clients[name]
                                if hasattr(client, 'close'):
                                    client.close()
                                log.info(f"[brain] MCP客户端已关闭: {name}")
                        except Exception as e:
                            log.debug(f"[brain] 关闭MCP客户端 {name} 失败: {e}")
    except Exception as e:
        log.debug(f"[brain] MCP清理异常: {e}")

def restart_brain() -> str:
    """手动重启大脑(清除所有缓存大脑+MCP连接, 下次请求重建)"""
    global _brains, _brain_failures
    for k, b in list(_brains.items()):
        _cleanup_brain_processes(b)
    _brains.clear()
    _brain_failures = 0
    log.info("[brain] 手动重启, 所有缓存大脑已清除")
    return "大脑重启中, 下次请求将自动重建"

def brain_status() -> dict:
    """获取大脑健康状态"""
    import datetime as _dt
    return {
        "ready": len(_brains) > 0,
        "cached_brains": list(_brains.keys()),
        "consecutive_failures": _brain_failures,
        "total_failures": _brain_total_failures,
        "max_failures_before_rebuild": _MAX_BRAIN_FAILURES,
        "last_success": _dt.datetime.fromtimestamp(_brain_last_success).isoformat() if _brain_last_success else None,
        "last_failure": _dt.datetime.fromtimestamp(_brain_last_failure).isoformat() if _brain_last_failure else None,
        "uptime_since": _dt.datetime.fromtimestamp(_brain_build_time).isoformat() if _brain_build_time else None,
    }

def _ollama_fallback(text: str, messages: list) -> str:
    """Finna失败时, 用本地Ollama做离线降级对话。无MCP, 纯文本对话。"""
    try:
        import requests as _req
        # 用 qwen3.5:2b 做降级对话(和意图分类同一个模型, 已在Ollama上)
        # 取最近几轮历史 + 当前输入
        recent = messages[-6:] if len(messages) > 6 else messages
        ollama_msgs = [{"role": m["role"], "content": m["content"]} for m in recent]
        # 确保 system message 存在
        if not ollama_msgs or ollama_msgs[0]["role"] != "system":
            ollama_msgs.insert(0, {"role": "system", "content": "你是Charlie，用户的私人AI助理。回复极简，不寒暄。"})
        r = _req.post("http://localhost:11434/api/chat", json={
            "model": "qwen3.5:2b",
            "messages": ollama_msgs,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.7, "num_predict": 200}
        }, timeout=(2, 15))
        if r.status_code == 200:
            data = r.json()
            reply = data.get("message", {}).get("content", "").strip()
            if reply:
                return reply
        return ""
    except Exception as e:
        log.warning(f"[brain] Ollama降级也失败: {e}")
        return ""

_OLLAMA_SIMPLE_SYSTEM_MSG = (
    "你是Charlie，用户的私人AI助理。当前时间：{time}。"
    "用自然的中文回复，该长则长该短则短。"
    "不要自称AI助手，直接回答问题。"
    "不知道的就说不知道，别编造。"
)

def _ollama_simple_chat(text: str, session_id: str) -> str:
    """意图=none 时走本地 Ollama 简单对话，跳过 Finna。"""
    hist = _get_history(session_id)
    now = datetime.datetime.now()
    sys_msg = _OLLAMA_SIMPLE_SYSTEM_MSG.format(time=now.strftime('%Y-%m-%d %H:%M'))
    msgs = [{"role": "system", "content": sys_msg}]
    for m in hist[-6:]:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": text})
    try:
        r = _session.post("http://localhost:11434/api/chat", json={
            "model": "qwen3.5:2b",
            "messages": msgs,
            "stream": False, "think": False,
            "options": {"temperature": 0.7, "num_predict": 300}
        }, timeout=(2, 15))
        if r.status_code == 200:
            reply = r.json().get("message", {}).get("content", "").strip()
            if reply:
                return reply
    except Exception as e:
        log.warning(f"[ollama] 简单对话失败: {e}")
    return ""

def _direct_music_play(text: str) -> str:
    """音乐关键词命中时直接调用 ncm-cli，绕过 LLM，避免 429 限流和反复迭代。
    返回 __MUSIC__url__name__artist 或空字符串(失败时)。"""
    import subprocess, json as _json, random, re as _re
    NCM = os.path.expanduser("~/.local/bin/ncm")
    try:
        # 判断是随机播放还是指定歌曲
        is_random = any(kw in text for kw in ('随机', '随便', '来一首', '来首歌', '随便来', '随便播'))
        
        if is_random:
            # 随机播放: 从每日推荐或播放记录中随机选
            r = subprocess.run([NCM, "recommend", "songs", "--json"], capture_output=True, text=True, timeout=10)
            songs = []
            if r.returncode == 0 and r.stdout.strip():
                data = _json.loads(r.stdout)
                songs = data.get("recommend", []) or data.get("data", [])
            if not songs:
                r = subprocess.run([NCM, "record", "--json", "--limit", "30"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    data = _json.loads(r.stdout)
                    songs = data.get("data", []) or data.get("record", [])
            if not songs:
                return ""
            song = random.choice(songs)
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))
        else:
            # 指定歌曲: 从文本提取歌名/歌手名
            # 去掉常见前缀词
            keyword = text
            for prefix in ('播放', '放', '唱', '听', '我想听', '我要听', '来首', '点歌', '点一首'):
                keyword = keyword.replace(prefix, '')
            keyword = keyword.strip().rstrip('。.,，！!')
            if not keyword or len(keyword) < 1:
                return ""
            r = subprocess.run([NCM, "search", "song", keyword, "--limit", "1", "--json"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return ""
            data = _json.loads(r.stdout)
            songs = data.get("result", {}).get("songs", [])
            if not songs:
                return ""
            song_id = songs[0]["id"]
            song_name = songs[0]["name"]
            ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))
        
        # 获取播放地址
        r = subprocess.run([NCM, "url", str(song_id), "--level", "exhigh", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ""
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return ""
        # 强制 https
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        log.info(f"[music] ncm直连成功: {song_name} - {ars} ({len(url)}B URL)")
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        log.warning(f"[music] ncm直连异常: {e}")
        return ""

def brain(text: str, session_id: str = "default") -> str:
    """大脑推理: 意图=none走Ollama(本地,0.25s), ≠none走Finna+MCP(云,2s)"""
    # 时间类查询直接返回实时时间，绕过LLM(避免缓存/历史污染/限流)
    TIME_KEYWORDS = ('几点', '几点啦')
    if any(kw in text for kw in TIME_KEYWORDS):
        now = datetime.datetime.now()
        reply = f"现在{now.strftime('%H点%M分')}。"
        log.info(f"[time] 直接返回实时时间: {reply}")
        return reply
    # 音乐播放类查询直接路由到MCP，绕过LLM(避免反复迭代)
    MUSIC_KEYWORDS = ('播放音乐', '播放歌', '随机播放', '放歌', '唱首歌', '放音乐', '来首歌', '听歌', '点歌', '我想听', '我要听')
    if any(kw in text for kw in MUSIC_KEYWORDS):
        log.info(f"[music] 音乐关键词命中，直接调用ncm绕过LLM: {text[:20]}")
        music_reply = _direct_music_play(text)
        if music_reply:
            _append_history(_get_history(session_id), text, music_reply)
            return music_reply
        log.warning(f"[music] ncm直连失败，回退到brain")
    # 缓存命中
    cached = _cache_get(text)
    if cached is not None:
        log.info(f"[cache] 命中: {text[:20]}")
        return cached
    # 意图路由: 判断需要哪些MCP，全部走Finna deepseek-v4-flash + MCP
    mcp_set = _classify_intent(text)
    _ensure_event_loop()
    try:
        brain_instance = _get_brain(mcp_set)
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        return f"大脑启动失败，请稍后重试：{str(e)[:40]}"

    hist = _get_history(session_id)
    messages = [{'role': m['role'], 'content': m['content']} for m in hist] + [{'role': 'user', 'content': text}]
    try:
        final = None
        for rsp in brain_instance.run(messages):
            final = rsp
            # 检测到 __MUSIC__ 立即短路，避免 brain 反复迭代同一工具
            if isinstance(rsp, list):
                for m in rsp:
                    if isinstance(m, dict) and m.get('role') == 'function' and isinstance(m.get('content'), str) and m['content'].startswith('__MUSIC__'):
                        break
                else:
                    continue
                break
        _record_brain_success()
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        # 离线降级: Finna失败时 fallback 到本地 Ollama
        ollama_reply = _ollama_fallback(text, messages)
        if ollama_reply:
            log.info(f"[brain] Finna失败, Ollama降级成功: {ollama_reply[:30]}")
            _append_history(hist, text, ollama_reply)
            _cache_set(text, ollama_reply)
            return ollama_reply
        return f"抱歉，我处理时出错了：{str(e)[:50]}"

    reply = "我没听明白"
    if final and isinstance(final, list):
        # 收集所有 function 消息的工具返回内容
        func_replies = []
        for m in final:
            if m.get('role') == 'function' and m.get('content'):
                c = str(m.get('content', ''))
                if c.strip():
                    func_replies.append(c)
                    if c.startswith('__MUSIC__'):
                        reply = c
                        break
        # 如果找到 __MUSIC__ 直接返回
        if reply.startswith('__MUSIC__'):
            pass
        # 没找到 __MUSIC__ 但有其他工具返回内容, 使用第一个非空内容
        elif reply == "我没听明白" and func_replies:
            reply = func_replies[0][:200]
        # 如果没找到工具返回, 找 assistant 文字回复
        if reply == "我没听明白":
            for m in reversed(final):
                if m.get('role') == 'assistant':
                    c = m.get('content')
                    if isinstance(c, str) and c.strip():
                        reply = c
                        break
                    elif isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and part.get('text'):
                                reply = part['text']
                                break
                        if reply != "我没听明白":
                            break
        # 如果assistant content为空但有function_call(工具调用了但没生成文字)
        # 生成一个默认回复
        if reply == "我没听明白":
            has_tool_call = any(m.get('function_call') for m in final if isinstance(m, dict))
            has_music = any('__MUSIC__' in str(m.get('content','')) for m in final if isinstance(m, dict))
            if has_music:
                reply = "__MUSIC_PLAYING__"
            elif has_tool_call:
                reply = "好的，已执行。"

    # 更新历史+缓存
    _append_history(hist, text, reply)
    _cache_set(text, reply)
    return reply


# ===== 流式大脑: 逐句产出，支持TTS流水线 =====
import queue as _queue
_SENTENCE_END = re.compile(r'[。！？；\n]')
_COMMA_SOFT = re.compile(r'[，,]')
_TTS_BOLD_RE = re.compile(r'\*{1,3}')
_TTS_HEADER_RE = re.compile(r'^#{1,6}\s*')
_TTS_BLOCKQUOTE_RE = re.compile(r'^>\s*')
_TTS_TABLE_PIPE_RE = re.compile(r'\|')
_TTS_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```')
_TTS_INLINE_CODE_RE = re.compile(r'`[^`]*`')
_TTS_LIST_ITEM_RE = re.compile(r'^[-*+]\s+')
_TTS_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_TTS_WHITESPACE_RE = re.compile(r'\s{2,}')
_MIN_CHUNK = 15  # 逗号处至少积累15字才切割(降低首句延迟)
_MAX_CHUNK = 80  # 超过80字强制在下一个标点处切割

def _extract_assistant_text(rsp: Any) -> str:
    """从brain.run()的中间响应中提取assistant文本(累积)。
    也检查 function 角色的工具返回值(如 __MUSIC__ URL)。"""
    if not isinstance(rsp, list):
        return ""
    # 1. 优先检查 function 消息(工具返回值, 如 __MUSIC__ 音乐URL)
    for m in reversed(rsp):
        if not isinstance(m, dict) or m.get("role") != "function":
            continue
        c = m.get("content", "")
        if isinstance(c, str) and c.startswith("__MUSIC__"):
            return c
    # 2. 找 assistant 文字回复
    for m in reversed(rsp):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("text"):
                    return part["text"]
    return ""

def _clean_for_tts(text: str) -> str:
    """清理文本供TTS使用: 去除markdown符号、表格管道符、引用符等"""
    t = text
    t = _TTS_BOLD_RE.sub('', t)             # **粗体** *斜体*
    t = _TTS_HEADER_RE.sub('', t)           # # 标题
    t = _TTS_BLOCKQUOTE_RE.sub('', t)       # > 引用
    t = _TTS_TABLE_PIPE_RE.sub(' ', t)      # 表格管道符
    t = _TTS_CODE_BLOCK_RE.sub('', t)       # 代码块
    t = _TTS_INLINE_CODE_RE.sub('', t)      # 行内代码
    t = _TTS_LIST_ITEM_RE.sub('', t)        # 列表标记
    t = _TTS_MARKDOWN_LINK_RE.sub(r'\1', t) # [text](url) → text
    t = _TTS_WHITESPACE_RE.sub(' ', t)      # 多余空格
    return t.strip()

def _interrupted_context_message(interrupted_reply: str) -> dict:
    reply = (interrupted_reply or "").strip()[:200]
    return {
        "role": "system",
        "content": (
            "上一条助手回复被用户打断，只播报或显示了片段："
            f"“{reply}”。用户接下来的问题可能是在追问这段未完成内容；"
            "请自然衔接，但不要把这条系统说明原样说给用户。"
        ),
    }


def brain_stream_sentences(
    text: str,
    session_id: str = "default",
    interrupted_reply: str = "",
) -> Generator[Tuple[str, str], None, None]:
    """
    流式大脑: 逐句yield完整句子，供TTS流水线使用。
    brain.run()增量产出token → 检测句子边界 → yield完整句。
    最后更新对话历史+缓存。
    yield: (sentence:str, full_reply:str)
    最后一次yield后，full_reply是完整回复。
    """
    # 占用语过滤: LLM可能先输出"让我想想"等废话，这些话会被流式TTS立即合成播放
    # 但最终回复里又不包含，导致用户听到了但历史里看不到。在yield前直接过滤掉。
    _FILLER_WORDS = {
        "让我想想", "稍等一下", "稍等", "让我思考一下", "想一想",
        "让我看看", "我看看", "好的", "好", "嗯嗯", "嗯",
        "让我想一下", "我想想", "等一下", "稍等下",
    }
    def _is_filler(s: str) -> bool:
        """检查句子是否是纯占用语（去掉标点后完全匹配）"""
        cleaned = re.sub(r'[，。！？\s]', '', s).strip()
        is_filler = bool(cleaned) and len(cleaned) <= 6 and cleaned in _FILLER_WORDS
        log.info(f"[filler] input='{s}' cleaned='{cleaned}' is_filler={is_filler}")
        return is_filler

    # 时间类查询直接返回实时时间，绕过LLM(避免缓存/历史污染/限流)
    TIME_KEYWORDS = ('几点', '几点啦')
    if any(kw in text for kw in TIME_KEYWORDS):
        now = datetime.datetime.now()
        reply = f"现在{now.strftime('%H点%M分')}。"
        log.info(f"[time] 直接返回实时时间: {reply}")
        yield (reply, reply)
        return
    # 音乐播放类查询，直接调用ncm绕过LLM
    MUSIC_KEYWORDS = ('播放音乐', '播放歌', '随机播放', '放歌', '唱首歌', '放音乐', '来首歌', '听歌', '点歌', '我想听', '我要听')
    if any(kw in text for kw in MUSIC_KEYWORDS):
        log.info(f"[music] 音乐关键词命中(流式)，直接调用ncm绕过LLM: {text[:20]}")
        music_reply = _direct_music_play(text)
        if music_reply:
            hist = _get_history(session_id)
            _append_history(hist, text, music_reply)
            yield (music_reply, music_reply)
            return
        log.warning(f"[music] ncm直连失败(流式)，回退到brain")
    # 缓存命中
    cached = _cache_get_interrupted(text, interrupted_reply) if interrupted_reply.strip() else _cache_get(text)
    if cached is not None:
        yield (_clean_for_tts(cached), cached)
        return
    # 意图路由: 判断需要哪些MCP，全部走Finna deepseek-v4-flash + MCP
    mcp_set = _classify_intent(text)
    _ensure_event_loop()
    try:
        brain_instance = _get_brain(mcp_set)
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        message = f"大脑启动失败：{str(e)[:40]}"
        yield (message, message)
        return

    hist = _get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in hist] + [{"role": "user", "content": text}]
    if interrupted_reply.strip():
        messages.insert(-1, _interrupted_context_message(interrupted_reply))
    sent_len = 0       # 已yield的字符数
    full_reply = ""

    try:
        for rsp in brain_instance.run(messages):
            # 检测到 __MUSIC__ 立即短路，避免反复迭代
            if isinstance(rsp, list):
                _has_music = False
                for _m in rsp:
                    if isinstance(_m, dict) and _m.get('role') == 'function' and isinstance(_m.get('content'), str) and _m['content'].startswith('__MUSIC__'):
                        _has_music = True
                        break
                if _has_music:
                    t = _extract_assistant_text(rsp)
                    if t:
                        full_reply = t
                    break
            t = _extract_assistant_text(rsp)
            if not t or len(t) <= sent_len:
                continue
            full_reply = t
            unsent = full_reply[sent_len:]
            while True:
                # 优先在句末标点处切割
                m = _SENTENCE_END.search(unsent)
                if m:
                    sentence = unsent[:m.end()].strip()
                    unsent = unsent[m.end():]
                    if sentence:
                        sent_len = len(full_reply) - len(unsent)
                        cleaned_sentence = _clean_for_tts(sentence)
                        if not _is_filler(cleaned_sentence):
                            yield (cleaned_sentence, full_reply)
                    continue
                # 句末无标点但已积累较长 → 在逗号处软切割
                if len(unsent) >= _MIN_CHUNK:
                    cm = _COMMA_SOFT.search(unsent)
                    if cm:
                        sentence = unsent[:cm.end()].strip()
                        unsent = unsent[cm.end():]
                        if sentence:
                            sent_len = len(full_reply) - len(unsent)
                            cleaned_sentence = _clean_for_tts(sentence)
                            if not _is_filler(cleaned_sentence):
                                yield (cleaned_sentence, full_reply)
                        continue
                break
            sent_len = len(full_reply) - len(unsent)
        _record_brain_success()
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        if not full_reply:
            full_reply = f"抱歉，处理时出错了：{str(e)[:40]}"

    # 剩余文本作为最后一句
    if full_reply and len(full_reply) > sent_len:
        remaining = full_reply[sent_len:].strip()
        if remaining:
            cleaned_remaining = _clean_for_tts(remaining)
            if not _is_filler(cleaned_remaining):
                yield (cleaned_remaining, full_reply)
    elif not full_reply:
        full_reply = "我没听明白"
        yield (full_reply, full_reply)

    # 更新历史+缓存
    _append_history(hist, text, full_reply)
    if session_id != "default":
        _trim_history_tokens(hist, session_id=session_id)
    _cache_set(text, full_reply, interrupted_reply)

def _tts_cleaned_to_mp3(cleaned: str) -> bytes:
    """合成已清洗文本 → WAV转MP3 (32kbps)"""
    if not cleaned:
        return b""
    audio = tts(cleaned)
    if not audio or len(audio) < 100:
        return b""
    import subprocess
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-b:a", "32k", "-ac", "1", "-f", "mp3", "pipe:1"],
            input=audio, capture_output=True, timeout=10)
        result = r.stdout if r.stdout and len(r.stdout) > 100 else audio
        return result
    except Exception:
        return audio

def tts_to_mp3(text: str) -> bytes:
    """TTS生成 + WAV转MP3 (供流式端点使用), 管道方式省临时文件"""
    cleaned = _clean_for_tts(text).strip()
    if not cleaned or len(cleaned) < 2:
        return b""
    return _tts_cleaned_to_mp3(cleaned)

def stream_voice_pipeline(text: str) -> Generator[Tuple[str, str, bytes], None, None]:
    """
    流式语音流水线生成器: 大脑逐句产出 → TTS → yield (type, sentence, mp3)。
    type: "sentence"(文字+音频), "error"(错误信息), "done"(完成)
    大脑流式产出减少了整体等待时间。
    服务器端可在此基础上实现更细粒度的并行(见 voice_server SSE端点)。
    """
    try:
        for sentence, full_reply in brain_stream_sentences(text):
            mp3 = _tts_cleaned_to_mp3(sentence)
            yield ("sentence", sentence, mp3)
    except Exception as e:
        log.error(f"流式流水线异常: {e}")
        yield ("error", str(e)[:60], b"")
    yield ("done", None, b"")



# ===== 完整语音闭环 =====
def voice_loop(audio_in: bytes, fmt: str = "mp3") -> Tuple[str, str, bytes]:
    """语音进 → ASR → 大脑(含MCP) → TTS → 语音出"""
    text = asr(audio_in, fmt)
    if not text:
        return EMPTY_ASR_TEXT, EMPTY_ASR_REPLY, b""
    if is_low_intent_asr(text):
        return text, LOW_INTENT_ASR_REPLY, b""
    reply = brain(text)
    try:
        audio_out = tts(reply)
    except Exception as e:
        log.warning(f"voice_loop TTS降级为文字: {e}")
        audio_out = b""
    return text, reply, audio_out


def runtime_audio_path(filename: str) -> str:
    """Return an audio output path under the configured runtime data directory."""
    return os.path.join(DATA_DIR, filename)


def write_audio_file(path: str, audio: bytes) -> str:
    """Atomically write audio bytes so interrupted writes do not truncate an existing file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return path


def runtime_temp_audio_path() -> str:
    """Return a writable directory for short-lived generated audio files."""
    return DATA_DIR

# 预热: 在模块加载时构建一次 none 大脑实例(避免首请求等待 976ms)
try:
    _get_brain("none")
    log.info("[warmup] brain(none) 预热完成")
except Exception as e:
    log.warning(f"[warmup] brain(none) 预热失败: {e}")

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    q = sys.argv[1] if len(sys.argv) > 1 else "帮我搜下北京附近的充电桩"
    print(f"① TTS生成输入语音: {q}")
    audio_in = tts(q)
    print(f"   输入音频 {len(audio_in)}字节")
    print("② ASR识别 + ③大脑推理(调MCP) + ④TTS合成回复...")
    text, reply, audio_out = voice_loop(audio_in)
    print(f"\n  ASR识别: {text}")
    print(f"  大脑回复: {reply}")
    print(f"  回复音频: {len(audio_out)}字节")
    output_path = runtime_audio_path("voice_reply.wav")
    write_audio_file(output_path, audio_out)
    print(f"  已保存 {output_path}")
