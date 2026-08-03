"""
Charlie - 共享工具模块
提取重复逻辑: 时间解析、临时文件清理、错误脱敏
"""
import os, re, datetime, tempfile, glob, logging, fcntl
import json
from contextlib import contextmanager

log = logging.getLogger("magic")


@contextmanager
def _locked_file(path: str, shared: bool = False):
    with open(path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

def parse_time_str(s: str) -> str | None:
    """
    解析中文时间字符串为ISO格式。
    支持: "30分钟后" "2小时后" "3天后" "明天" "后天" "今天下午3点" "明天9点" 等
    被复用于: voice_server.py /api/reminders + baize_skills_mcp.py add_reminder
    """
    if not s:
        return None
    t = datetime.datetime.now()
    tg = t
    ok = False

    # 相对时间(分钟/小时/天后) 和 绝对日期(明天/后天) 互斥, 不叠加
    has_relative = False
    has_absolute_date = False
    # N分钟后
    mm = re.search(r"(\d+)\s*分(?:钟)?后", s)
    if mm:
        tg += datetime.timedelta(minutes=int(mm.group(1)))
        ok = True
        has_relative = True
    # N小时后
    hh = re.search(r"(\d+)\s*(?:小时|个小时)后", s)
    if hh:
        tg += datetime.timedelta(hours=int(hh.group(1)))
        ok = True
        has_relative = True
    # N天后
    dd = re.search(r"(\d+)\s*天后", s)
    if dd:
        tg += datetime.timedelta(days=int(dd.group(1)))
        ok = True
        has_relative = True
    # 相对日期: 大后天/后天/明天/今天 (如果已有相对时间, 跳过)
    for w, n in [("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)]:
        if w in s and not has_relative:
            tg += datetime.timedelta(days=n)
            ok = True
            has_absolute_date = True
            break
    # 具体时刻: 3点/3:30/下午3点/晚上9点半
    tm = re.search(r"(\d{1,2})\s*[点时:：]\s*(\d{0,2})", s)
    if tm:
        h = int(tm.group(1))
        mi = int(tm.group(2)) if tm.group(2) else (30 if "半" in s else 0)
        if ("下午" in s or "晚上" in s) and h < 12:
            h += 12
        tg = tg.replace(hour=h, minute=mi, second=0, microsecond=0)
        ok = True

    return tg.isoformat() if ok else None


def cleanup_temp_files(pattern: str = "/tmp/*_reply.wav", extra_dirs: list[str] | None = None) -> int:
    """
    清理临时音频文件(启动时+定期调用)。
    返回清理的文件数。
    """
    removed = 0
    files_to_remove = set(glob.glob(pattern))
    cleanup_dirs = ["/tmp", *(extra_dirs or [])]
    runtime_patterns = ["voice_reply.wav", "tts_test.wav", "asr_test.wav", "tmp*.mp3", "*_reply*.wav"]
    for directory in cleanup_dirs:
        for name in runtime_patterns:
            files_to_remove.update(glob.glob(os.path.join(directory, name)))
    for f in sorted(files_to_remove):
        if os.path.isdir(f):
            continue
        try:
            os.unlink(f)
            removed += 1
        except Exception:
            pass
    if removed > 0:
        log.info(f"[cleanup] 清理{removed}个临时文件")
    return removed


def truncate_history_file(path: str, max_entries: int = 100) -> None:
    """
    对话历史文件大小保护: 超过max_entries条自动截断保留最近的。
    """
    try:
        with _locked_file(f"{path}.lock"):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            truncated = False
            if isinstance(data, list):
                original_count = len(data)
                if original_count > max_entries:
                    data = data[-max_entries:]
                    truncated = True
            elif isinstance(data, dict):
                original_count = 0
                for session_id, messages in data.items():
                    if isinstance(messages, list):
                        original_count += len(messages)
                        if len(messages) > max_entries:
                            data[session_id] = messages[-max_entries:]
                            truncated = True
            else:
                raise ValueError(f"unsupported history type: {type(data).__name__}")

            if truncated:
                target_dir = os.path.dirname(os.path.abspath(path)) or "."
                fd, temp_path = tempfile.mkstemp(prefix=".history_truncate.", suffix=".tmp", dir=target_dir)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_path, path)
                    log.info(f"[history] 截断至{max_entries}条/会话(原{original_count}+)")
                except Exception:
                    try:
                        os.unlink(temp_path)
                    except FileNotFoundError:
                        pass
                    raise
    except Exception as e:
        log.warning(f"[history] 截断失败: {e}")


def sanitize_error(error_msg: str) -> str:
    """
    错误响应脱敏: 移除内部路径、密钥、堆栈等敏感信息。
    """
    # 移除文件路径
    s = re.sub(r"/[^\s:]+\.(py|json|env|sh)", "[文件]", error_msg)
    # 移除API密钥模式
    s = re.sub(r"(sk-|app-|tvly-)[a-zA-Z0-9-]+", "[密钥]", s)
    # 移除IP地址
    s = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "[IP]", s)
    # 截断过长错误
    if len(s) > 100:
        s = s[:100] + "..."
    return s
