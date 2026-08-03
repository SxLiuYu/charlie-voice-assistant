"""提醒持久化I/O: 带文件锁的加载/保存/自动清理

REMINDERS_FILE 路径基于项目根(app的上级目录). 7天前已完成的提醒自动清理.
"""
import os, json, fcntl, logging, copy, tempfile, datetime as dt
from contextlib import contextmanager
log = logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", PROJECT_DIR)

# 项目根 = app/ 的上级目录(与voice_server.py同层)；测试可通过 ASSISTANT_KID_DATA_DIR 隔离。
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")
REMINDERS_LOCK_FILE = REMINDERS_FILE + ".lock"
SCHEDULER_LOCK_FILE = REMINDERS_FILE + ".scheduler.lock"
SUGGESTIONS_STATE_FILE = os.path.join(DATA_DIR, "suggestions_state.json")
PROACTIVE_LOCK_FILE = SUGGESTIONS_STATE_FILE + ".runner.lock"
DELIVERY_RETRY_DELAYS = [60, 180, 600]
DELIVERY_CLAIM_TIMEOUT = 900  # 秒；播报线程崩溃后允许重新申领

os.makedirs(DATA_DIR, exist_ok=True)


@contextmanager
def _locked_reminders(shared: bool = False):
    """串行化提醒文件读写；只读路径用共享锁，允许多个读者并发读取。"""
    with open(REMINDERS_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _acquire_file_lock(lock_path: str, log_prefix: str):
    """非阻塞获取机器级 flock；失败返回 None，不等待其他进程释放。"""
    lock_file = None
    try:
        lock_file = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        return lock_file
    except BlockingIOError:
        if lock_file is not None:
            lock_file.close()
        return None
    except OSError as e:
        if lock_file is not None:
            lock_file.close()
        log.warning(f"{log_prefix} 获取运行锁失败: {e}")
        return None


def acquire_scheduler_lock():
    """非阻塞获取机器级提醒调度锁；失败返回 None，由其他进程继续待命。"""
    return _acquire_file_lock(SCHEDULER_LOCK_FILE, "[reminders]")


def acquire_proactive_lock():
    """非阻塞获取机器级主动建议运行锁；失败返回 None。"""
    return _acquire_file_lock(PROACTIVE_LOCK_FILE, "[suggest]")


def _lock_status(lock_path: str) -> dict:
    """读取 flock 状态；只做非阻塞探测，不创建文件也不影响持有者。"""
    owner_pid = None
    held_by_this_process = False
    probe = None
    try:
        try:
            probe = open(lock_path, "r", encoding="utf-8")
        except FileNotFoundError:
            probe = None
            locked = False
        else:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = False
            except BlockingIOError:
                locked = True
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)

        if locked:
            probe.seek(0)
            for line in probe.read().splitlines():
                if line.startswith("pid="):
                    try:
                        owner_pid = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        owner_pid = None
                    break
        held_by_this_process = bool(locked and owner_pid == os.getpid())
    except OSError:
        locked = False
    finally:
        if probe is not None:
            probe.close()

    return {
        "locked": locked,
        "held_by_this_process": held_by_this_process,
        "owner_pid": owner_pid,
        "lock_file": lock_path,
    }

def scheduler_lock_status() -> dict:
    """读取调度器锁的当前状态；不抢锁，不影响真实持有者。"""
    return _lock_status(SCHEDULER_LOCK_FILE)


def proactive_lock_status() -> dict:
    """读取主动建议运行锁的当前状态；不抢锁，不影响真实持有者。"""
    return _lock_status(PROACTIVE_LOCK_FILE)

def _coerce_reminders(raw) -> list:
    """把磁盘中的提醒数据收敛为 dict 列表，避免畸形记录拖垮只读和调度路径。"""
    if not isinstance(raw, list):
        log.warning("[reminders] 提醒数据格式异常，期望 list，实际为 %s", type(raw).__name__)
        return []

    reminders = []
    malformed = 0
    for item in raw:
        if not isinstance(item, dict):
            malformed += 1
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            malformed += 1
            continue
        reminders.append(item)

    if malformed:
        log.warning("[reminders] 忽略 %d 条格式异常的提醒记录", malformed)
    return reminders

def _read_locked_reminders():
    """在已持有提醒文件锁时读取并收敛提醒列表。"""
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            return _coerce_reminders(json.load(f))
    except Exception:
        return []


def _write_locked_reminders(reminders: list) -> None:
    """在已持有提醒文件锁时原子写入提醒文件，避免半写 JSON 破坏原文件。"""
    directory = os.path.dirname(REMINDERS_FILE)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".reminders.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(reminders, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, REMINDERS_FILE)
    except Exception:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise

def _cleanup_old_reminders(reminders: list) -> tuple:
    """清理已完成的旧提醒(7天前完成的), 返回(清理后列表, 删除数)"""
    cutoff = (dt.datetime.now() - dt.timedelta(days=7)).isoformat()
    kept = []
    removed = 0
    for r in reminders:
        completed_at = r.get("completed_at") or r.get("triggered_at") or ""
        if r.get("done") and completed_at < cutoff:
            removed += 1
        else:
            kept.append(r)
    if removed > 0:
        log.info(f"[reminders] 自动清理{removed}条已完成旧提醒")
    return kept, removed

def _load_reminders():
    """带文件锁的提醒加载(自动清理7天前已完成)"""
    with _locked_reminders(shared=True):
        return _read_locked_reminders()


def reminder_delivery_status(reminders: list) -> dict:
    """汇总提醒投递状态，供状态接口和监控面板只读展示。"""
    status = {"active": 0, "delivering": 0, "retry": 0, "failed": 0}
    for reminder in reminders:
        state = reminder.get("delivery_state")
        if state in ("delivering", "retry"):
            status[state] += 1
            status["active"] += 1
        elif state == "failed":
            status["failed"] += 1
    return status


def _save_reminders(data):
    """带文件锁的提醒保存(保存前自动清理7天前已完成)"""
    with _locked_reminders():
        data = _coerce_reminders(data)
        data, removed = _cleanup_old_reminders(data)
        if removed > 0:
            log.info(f"[reminders] 保存时清理{removed}条旧提醒")
        _write_locked_reminders(data)


def append_reminder(text: str, time_str: str = "", due: str | None = None, repeat: str = "") -> dict:
    """在排他锁内完成新增提醒的读改写，返回写入后的提醒副本。

    repeat: ""=一次性, "daily"=每天, "weekly"=每周, "weekdays"=工作日
    """
    now = dt.datetime.now()
    with _locked_reminders():
        reminders = _read_locked_reminders()
        next_id = int(now.timestamp())
        for reminder in reminders:
            try:
                next_id = max(next_id, int(reminder.get("id", 0)))
            except (TypeError, ValueError):
                continue
        next_id = max(next_id + 1, int(now.timestamp()))
        item = {
            "id": next_id,
            "text": text,
            "time": time_str,
            "due": due,
            "done": False,
            "repeat": repeat if repeat in ("daily", "weekly", "weekdays") else "",
        }
        reminders.append(item)
        reminders, removed = _cleanup_old_reminders(reminders)
        if removed > 0:
            log.info(f"[reminders] 新增时清理{removed}条旧提醒")
        _write_locked_reminders(reminders)
        return copy.deepcopy(item)


def complete_reminder(reminder_id: int) -> bool:
    """在排他锁内把提醒标记完成；找不到时返回 False，且不写文件。"""
    now = dt.datetime.now().isoformat()
    with _locked_reminders():
        reminders = _read_locked_reminders()
        for reminder in reminders:
            if reminder.get("id") != reminder_id:
                continue
            reminder["done"] = True
            reminder["completed_at"] = now
            _write_locked_reminders(reminders)
            return True
        return False


def _retry_delay(attempt_count: int):
    """返回第 attempt_count 次失败后的退避秒数；超过上限则 None。"""
    if attempt_count < 1 or attempt_count > len(DELIVERY_RETRY_DELAYS):
        return None
    return dt.timedelta(seconds=DELIVERY_RETRY_DELAYS[attempt_count - 1])


def release_failed_reminder(reminder_id: int, claimed_at, error: str = ""):
    """把播报失败的提醒恢复为待重试；超过重试上限则保留完成状态。"""
    now = dt.datetime.now()
    if isinstance(claimed_at, str):
        try:
            claimed_at = dt.datetime.fromisoformat(claimed_at)
        except Exception:
            claimed_at = now
    with _locked_reminders():
        reminders = _read_locked_reminders()

        changed = False
        for reminder in reminders:
            if reminder.get("id") != reminder_id:
                continue
            attempt_count = int(reminder.get("attempt_count", 1) or 1)
            delay = _retry_delay(attempt_count)
            reminder["last_delivery_error"] = error[:200]
            if delay is None:
                reminder["done"] = True
                reminder["delivery_state"] = "failed"
                reminder["completed_at"] = now.isoformat()
            else:
                retry_after = claimed_at + delay
                reminder["done"] = False
                reminder["delivery_state"] = "retry"
                reminder["attempt_count"] = attempt_count
                reminder["retry_after"] = retry_after.isoformat()
                reminder.pop("triggered_at", None)
                log.warning(
                    f"[reminders] 提醒{reminder_id}播报失败，{int(delay.total_seconds())}秒后重试: {error}"
                )
            changed = True
            break

        if changed:
            _write_locked_reminders(reminders)


def complete_reminder_delivery(reminder_id: int):
    """播报成功后标记完成；如果是循环提醒，生成下一次到期。"""
    now = dt.datetime.now()
    with _locked_reminders():
        reminders = _read_locked_reminders()

        changed = False
        for reminder in reminders:
            if reminder.get("id") != reminder_id:
                continue
            reminder["done"] = True
            reminder["delivery_state"] = "delivered"
            reminder["completed_at"] = now.isoformat()
            reminder.pop("retry_after", None)
            reminder.pop("last_delivery_error", None)
            changed = True

            # 循环提醒：生成下一次到期时间
            repeat = reminder.get("repeat", "")
            due_str = reminder.get("due", "")
            if repeat and due_str:
                try:
                    old_due = dt.datetime.fromisoformat(due_str)
                    if repeat == "daily":
                        next_due = old_due + dt.timedelta(days=1)
                    elif repeat == "weekly":
                        next_due = old_due + dt.timedelta(weeks=1)
                    elif repeat == "weekdays":
                        next_due = old_due + dt.timedelta(days=1)
                        while next_due.weekday() >= 5:
                            next_due += dt.timedelta(days=1)
                    else:
                        next_due = None
                    if next_due:
                        next_id = int(now.timestamp())
                        for r in reminders:
                            try:
                                next_id = max(next_id, int(r.get("id", 0)))
                            except (TypeError, ValueError):
                                continue
                        next_id += 1
                        new_item = {
                            "id": next_id,
                            "text": reminder["text"],
                            "time": reminder.get("time", ""),
                            "due": next_due.isoformat(),
                            "done": False,
                            "repeat": repeat,
                        }
                        reminders.append(new_item)
                        log.info(f"[reminders] 循环提醒{reminder_id}已生成下次: id={next_id} due={next_due.isoformat()}")
                except Exception as e:
                    log.warning(f"[reminders] 循环提醒{reminder_id}生成下次失败: {e}")
            break

        if changed:
            _write_locked_reminders(reminders)


def claim_due_reminders(now=None):
    """原子申领所有已到期提醒；跨进程/多线程下每条只返回一次。"""
    now = now or dt.datetime.now()
    claimed = []
    with _locked_reminders():
        reminders = _read_locked_reminders()

        changed = False
        for reminder in reminders:
            if reminder.get("done"):
                continue
            if reminder.get("delivery_state") == "delivering":
                claim_started = reminder.get("claim_started_at")
                try:
                    claim_started = dt.datetime.fromisoformat(claim_started)
                except Exception:
                    claim_started = now - dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT + 1)
                if now - claim_started < dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT):
                    continue
                # 播报线程可能已崩溃；超过申领超时后允许重新申领同一条提醒。
            retry_after = reminder.get("retry_after")
            if retry_after:
                try:
                    if now < dt.datetime.fromisoformat(retry_after):
                        continue
                except Exception:
                    pass
            due_str = reminder.get("due", "")
            if not due_str:
                continue
            try:
                due = dt.datetime.fromisoformat(due_str)
            except Exception:
                continue
            if now >= due:
                # 只有非 delivering 状态才申领；delivering 且超时的会在上面 fall-through 到这里
                reminder["delivery_state"] = "delivering"
                reminder["claim_started_at"] = now.isoformat()
                reminder["attempt_count"] = int(reminder.get("attempt_count", 0) or 0) + 1
                reminder.pop("retry_after", None)
                reminder.pop("last_delivery_error", None)
                claimed.append(copy.deepcopy(reminder))
                changed = True

        if changed:
            reminders, removed = _cleanup_old_reminders(reminders)
            if removed > 0:
                log.info(f"[reminders] 申领时清理{removed}条旧提醒")
            _write_locked_reminders(reminders)

    return claimed
