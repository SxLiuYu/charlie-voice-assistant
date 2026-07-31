"""
助手小子 - 共享工具模块
提取重复逻辑: 时间解析、临时文件清理、错误脱敏
"""
import os, re, datetime, tempfile, glob, logging

log = logging.getLogger("magic")

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

    # N分钟后
    mm = re.search(r"(\d+)\s*分(?:钟)?后", s)
    if mm:
        tg += datetime.timedelta(minutes=int(mm.group(1)))
        ok = True
    # N小时后
    hh = re.search(r"(\d+)\s*(?:小时|个小时)后", s)
    if hh:
        tg += datetime.timedelta(hours=int(hh.group(1)))
        ok = True
    # N天后
    dd = re.search(r"(\d+)\s*天后", s)
    if dd:
        tg += datetime.timedelta(days=int(dd.group(1)))
        ok = True
    # 相对日期: 大后天/后天/明天/今天
    for w, n in [("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)]:
        if w in s:
            tg += datetime.timedelta(days=n)
            ok = True
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


def cleanup_temp_files(pattern: str = "/tmp/*_reply.wav") -> int:
    """
    清理临时音频文件(启动时+定期调用)。
    返回清理的文件数。
    """
    removed = 0
    for f in glob.glob(pattern):
        try:
            os.unlink(f)
            removed += 1
        except Exception:
            pass
    # 也清理 voice_reply.wav 等测试文件
    for name in ["voice_reply.wav", "tts_test.wav", "asr_test.wav"]:
        p = os.path.join("/tmp", name)
        if os.path.exists(p):
            try:
                os.unlink(p)
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
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if len(data) > max_entries:
            data = data[-max_entries:]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.info(f"[history] 截断至{max_entries}条(原{len(data)}+)")
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
