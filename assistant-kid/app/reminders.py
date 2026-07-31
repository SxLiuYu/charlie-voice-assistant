"""提醒持久化I/O: 带文件锁的加载/保存/自动清理

REMINDERS_FILE 路径基于项目根(app的上级目录). 7天前已完成的提醒自动清理.
"""
import os, json, fcntl, logging
log = logging.getLogger("magic")

# 项目根 = app/ 的上级目录(与voice_server.py同层)
REMINDERS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reminders.json")

def _cleanup_old_reminders(reminders: list) -> tuple:
    """清理已完成的旧提醒(7天前完成的), 返回(清理后列表, 删除数)"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=7)).isoformat()
    kept = []
    removed = 0
    for r in reminders:
        if r.get("done") and r.get("completed_at", r.get("triggered_at", "")) < cutoff:
            removed += 1
        else:
            kept.append(r)
    if removed > 0:
        log.info(f"[reminders] 自动清理{removed}条已完成旧提醒")
    return kept, removed

def _load_reminders():
    """带文件锁的提醒加载(自动清理7天前已完成)"""
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except Exception:
        return []

def _save_reminders(data):
    """带文件锁的提醒保存(保存前自动清理7天前已完成)"""
    data, removed = _cleanup_old_reminders(data)
    if removed > 0:
        log.info(f"[reminders] 保存时清理{removed}条旧提醒")
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, ensure_ascii=False, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)
