"""提醒持久化I/O: 带文件锁的加载/保存/自动清理

REMINDERS_FILE 路径基于项目根(app的上级目录). 7天前已完成的提醒自动清理.
"""
import os, json, logging, copy, tempfile, datetime as dt
try:
    import fcntl
except ImportError:  # Windows 无 fcntl
    import fcntl_compat as fcntl
from contextlib import contextmanager
log = logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _get_data_dir() -> str:
    """动态读取 DATA_DIR，避免模块级冻结导致测试隔离失败"""
    return os.environ.get("ASSISTANT_KID_DATA_DIR", PROJECT_DIR)

def _get_reminders_file() -> str:
    return os.path.join(_get_data_dir(), "reminders.json")

def _get_suggestions_state_file() -> str:
    return os.path.join(_get_data_dir(), "suggestions_state.json")

# 项目根 = app/ 的上级目录(与voice_server.py同层)；测试可通过 ASSISTANT_KID_DATA_DIR 隔离。
REMINDERS_FILE = _get_reminders_file()
REMINDERS_LOCK_FILE = REMINDERS_FILE + ".lock"
SCHEDULER_LOCK_FILE = REMINDERS_FILE + ".scheduler.lock"
SUGGESTIONS_STATE_FILE = _get_suggestions_state_file()
PROACTIVE_LOCK_FILE = SUGGESTIONS_STATE_FILE + ".runner.lock"
DECISION_LOCK_FILE = os.path.join(_get_data_dir(), "decision_engine.runner.lock")
DELIVERY_RETRY_DELAYS = [60, 180, 600]
DELIVERY_CLAIM_TIMEOUT = 900  # 秒；播报线程崩溃后允许重新申领
EXPIRED_AFTER_HOURS = int(os.getenv("REMINDER_EXPIRED_AFTER_HOURS", "2"))  # 一次性提醒过期小时数
# enqueued（ESP32 离线等重连）重试次数上限：5 分钟间隔 × 6 次 ≈ 30 分钟。
# 超过后停止重新入队——飞书兜底已送达，设备端 pending 队列 TTL 也只有 5 分钟，
# 继续重试只是浪费 TTS 配额并往调度日志灌重复推送（真实事故：起床提醒循环 64 次）。
ENQUEUED_MAX_ATTEMPTS = int(os.getenv("REMINDER_ENQUEUED_MAX_ATTEMPTS", "6"))

os.makedirs(_get_data_dir(), exist_ok=True)


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
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
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


def acquire_decision_lock():
    """非阻塞获取机器级决策引擎运行锁；失败返回 None。"""
    return _acquire_file_lock(DECISION_LOCK_FILE, "[decision]")


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
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
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


def list_reminders(include_completed: bool = False) -> list:
    """返回提醒列表，按 due 升序。

    include_completed=False 时过滤已完成提醒。
    """
    reminders = _load_reminders()
    if not include_completed:
        reminders = [r for r in reminders if not r.get("done")]
    reminders = sorted(reminders, key=lambda r: r.get("due", "") or "")
    return reminders


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
        # 失效上下文缓存和 system_msg 缓存，确保下次看到最新待办
        try:
            from agent.context import invalidate_context_cache
            invalidate_context_cache()
        except Exception as e:
            log.debug(f"[app.reminders] error at line 254: {e}")
            pass
        try:
            from agent.system_msg import invalidate_system_msg_cache
            invalidate_system_msg_cache()
        except Exception as e:
            log.debug(f"[app.reminders] error at line 259: {e}")
            pass


def append_reminder(text: str, time_str: str = "", due: str | None = None, repeat: str = "") -> dict:
    """在排他锁内完成新增提醒的读改写，返回写入后的提醒副本。

    repeat: ""=一次性, "daily"=每天, "weekly"=每周, "weekdays"=工作日

    去重：已存在相同 text + repeat + due(±60s) 的未完成提醒时，复用已有项，
    防止晚安场景重复触发导致同内容多份提醒堆积。
    """
    now = dt.datetime.now()
    with _locked_reminders():
        reminders = _read_locked_reminders()

        # 去重检查：同 text + repeat + due(±60s) 且未完成
        norm_due = due or ""
        if norm_due:
            try:
                due_dt = dt.datetime.fromisoformat(norm_due)
            except Exception:
                due_dt = None
        else:
            due_dt = None
        for existing in reminders:
            if existing.get("done"):
                continue
            if existing.get("text", "").strip() != text.strip():
                continue
            if existing.get("repeat", "") != repeat:
                continue
            ex_due = existing.get("due", "")
            if due_dt and ex_due:
                try:
                    if abs((dt.datetime.fromisoformat(ex_due) - due_dt).total_seconds()) <= 60:
                        log.info(f"[reminders] 跳过重复提醒: {text} (due={norm_due})")
                        return copy.deepcopy(existing)
                except Exception as e:
                    log.debug(f"[app.reminders] error at line 297: {e}")
                    pass
            elif not norm_due and not ex_due:
                log.info(f"[reminders] 跳过重复提醒: {text}")
                return copy.deepcopy(existing)

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
        # 失效上下文缓存和 system_msg 缓存，确保下次看到最新待办
        try:
            from agent.context import invalidate_context_cache
            invalidate_context_cache()
        except Exception as e:
            log.debug(f"[app.reminders] error at line 327: {e}")
            pass
        try:
            from agent.system_msg import invalidate_system_msg_cache
            invalidate_system_msg_cache()
        except Exception as e:
            log.debug(f"[app.reminders] error at line 332: {e}")
            pass
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


def update_reminder_delivery_state(reminder_id: int, state: str):
    """更新提醒投递状态（不标记 done），用于 enqueued 等中间状态。"""
    with _locked_reminders():
        reminders = _read_locked_reminders()
        changed = False
        for reminder in reminders:
            if reminder.get("id") != reminder_id:
                continue
            reminder["delivery_state"] = state
            changed = True
            break
        if changed:
            _write_locked_reminders(reminders)

def mark_reminder_enqueued(reminder_id: int):
    """标记提醒为 enqueued 状态（ESP32 离线等待推送），设置 retry_after=5分钟后重试。

    调用方：push_tts_to_xiaozhi 返回 "enqueued" 时，由 play_reminder_audio 调用。
    """
    now = dt.datetime.now()
    retry_after = now + dt.timedelta(minutes=5)
    with _locked_reminders():
        reminders = _read_locked_reminders()
        changed = False
        for reminder in reminders:
            if reminder.get("id") != reminder_id:
                continue
            reminder["delivery_state"] = "enqueued"
            reminder["retry_after"] = retry_after.isoformat()
            reminder.pop("claim_started_at", None)
            changed = True
            # 循环提醒：飞书兜底已送达，立即滚动生成下一次，
            # 避免 ESP32 长期离线时后续日程整段丢失（补播 direct 成功时不再重复生成）
            if _generate_next_occurrence_locked(reminder, reminders, now):
                changed = True
            log.info(f"[reminders] 提醒{reminder_id}入队(enqueued)，{int((retry_after - now).total_seconds())}秒后可重试")
            break
        if changed:
            _write_locked_reminders(reminders)

def set_reminder_retry_after(reminder_id: int, retry_after: dt.datetime):
    """设置提醒的重试时间（用于静音时段补播等场景）。"""
    with _locked_reminders():
        reminders = _read_locked_reminders()
        changed = False
        for reminder in reminders:
            if reminder.get("id") != reminder_id:
                continue
            reminder["retry_after"] = retry_after.isoformat()
            reminder.pop("claim_started_at", None)
            changed = True
            # 静音时段与 enqueued 同语义：飞书已兜底送达，循环提醒立即滚动
            if _generate_next_occurrence_locked(reminder, reminders, dt.datetime.now()):
                changed = True
            log.info(f"[reminders] 提醒{reminder_id}设置retry_after={retry_after.isoformat()}")
            break
        if changed:
            _write_locked_reminders(reminders)

def _generate_next_occurrence_locked(reminder: dict, reminders: list, now: dt.datetime) -> bool:
    """为循环提醒生成下一次到期条目（须在 _locked_reminders 锁内调用）。

    已生成过（next_generated 标记）或非循环提醒返回 False。
    标记防止重复：enqueued 时生成过一次后，补播 direct 成功不再生成。
    """
    if reminder.get("next_generated"):
        return False
    repeat = reminder.get("repeat", "")
    due_str = reminder.get("due", "")
    if not repeat or not due_str:
        return False
    try:
        old_due = dt.datetime.fromisoformat(due_str)
        if repeat == "daily":
            next_due = old_due + dt.timedelta(days=1)
            while next_due < now:
                next_due += dt.timedelta(days=1)
        elif repeat == "weekly":
            next_due = old_due + dt.timedelta(weeks=1)
            while next_due < now:
                next_due += dt.timedelta(weeks=1)
        elif repeat == "weekdays":
            next_due = old_due + dt.timedelta(days=1)
            # 1. 先跳过所有已过期的循环点（补触发过去的）
            while next_due < now:
                next_due += dt.timedelta(days=1)
            # 2. 再跳过周末，但保留 >= now 的工作日
            while next_due.weekday() >= 5:
                next_due += dt.timedelta(days=1)
        else:
            return False
        next_id = int(now.timestamp())
        for r in reminders:
            try:
                next_id = max(next_id, int(r.get("id", 0)))
            except (TypeError, ValueError):
                continue
        next_id += 1
        reminders.append({
            "id": next_id,
            "text": reminder["text"],
            "time": reminder.get("time", ""),
            "due": next_due.isoformat(),
            "done": False,
            "repeat": repeat,
        })
        reminder["next_generated"] = True
        log.info(f"[reminders] 循环提醒{reminder.get('id')}已生成下次: id={next_id} due={next_due.isoformat()}")
        return True
    except Exception as e:
        log.warning(f"[reminders] 循环提醒{reminder.get('id')}生成下次失败: {e}")
        return False


def restore_after_restart():
    """进程重启恢复：重置卡在中间状态的提醒，使其可被重新申领。

    场景：
    - 进程崩溃时提醒处于 "delivering" 状态（播报线程中断，claim_started_at 过期）
    - 进程崩溃时提醒处于 "enqueued" 状态（retry_after 可能已过期）
    恢复策略：
    1. 过滤掉已过期的一性提醒（超过 EXPIRED_AFTER_HOURS）
    2. 把 "delivering" 过一次申领超时的状态降级为 "pending" + 重试
    3. 把 "enqueued" + retry_after 已过的状态降级为 "pending"
    4. 把其他中间状态（无 delivery_state 但有 claim_started_at）降级为 "pending"
    """
    now = dt.datetime.now()
    restored = 0
    with _locked_reminders():
        reminders = _read_locked_reminders()
        changed = False
        for r in reminders:
            if r.get("done"):
                continue
            repeat = r.get("repeat", "")
            due_str = r.get("due", "")
            if not due_str:
                continue

            # 一次性过期提醒保持 expired（锚定 due，不用 retry_after——会被重试推后续命）
            if not repeat:
                try:
                    due = dt.datetime.fromisoformat(due_str)
                    if now - due > dt.timedelta(hours=EXPIRED_AFTER_HOURS):
                        continue  # 已过期，保持当前状态
                except (ValueError, TypeError):
                    continue

            delivery_state = r.get("delivery_state", "")
            claim_started = r.get("claim_started_at", "")
            retry_after_str = r.get("retry_after", "")
            needs_restore = False
            new_state = ""

            if delivery_state == "delivering":
                # delivering 状态：检查 claim 是否已超时（超过 DELIVERY_CLAIM_TIMEOUT）
                try:
                    claim_dt = dt.datetime.fromisoformat(claim_started)
                except (ValueError, TypeError):
                    claim_dt = now - dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT + 1)
                if now - claim_dt >= dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT):
                    needs_restore = True
                    new_state = "retry"
                    r["delivery_state"] = "retry"
                    r.pop("claim_started_at")
                    r["attempt_count"] = int(r.get("attempt_count", 1) or 1) + 1
            elif delivery_state == "enqueued":
                # enqueued 状态：检查 retry_after 是否已过
                if retry_after_str:
                    try:
                        if now >= dt.datetime.fromisoformat(retry_after_str):
                            needs_restore = True
                            new_state = "pending"
                            r["delivery_state"] = ""
                            r.pop("retry_after")
                    except (ValueError, TypeError):
                        pass
            elif delivery_state == "" and claim_started:
                # 无 delivery_state 但有 claim_started_at（旧版格式残留）
                try:
                    claim_dt = dt.datetime.fromisoformat(claim_started)
                    if now - claim_dt >= dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT):
                        needs_restore = True
                        new_state = "retry"
                        r["delivery_state"] = "retry"
                        r.pop("claim_started_at")
                except (ValueError, TypeError):
                    pass

            if needs_restore:
                changed = True
                restored += 1
                log.info(f"[reminders] 重启恢复提醒{r.get('id')}: {delivery_state or 'pending'} → {new_state}")

        if changed:
            _write_locked_reminders(reminders)
    if restored:
        log.info(f"[reminders] 重启恢复完成，共恢复 {restored} 条提醒")
    return restored


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

            # 循环提醒：生成下一次到期时间（enqueued 时可能已生成过）
            _generate_next_occurrence_locked(reminder, reminders, now)
            break

        if changed:
            _write_locked_reminders(reminders)
            try:
                from agent.context import invalidate_context_cache
                invalidate_context_cache()
            except Exception as e:
                log.debug(f"[app.reminders] error at line 480: {e}")
                pass
            try:
                from agent.system_msg import invalidate_system_msg_cache
                invalidate_system_msg_cache()
            except Exception as e:
                log.debug(f"[app.reminders] error at line 485: {e}")
                pass


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

            # 一次性提醒过期太久（超过 EXPIRED_AFTER_HOURS）直接标记 expired，不再播报。
            # 移到状态检查之前，避免 delivering/enqueued 状态绕过过期判定（claim 续命死循环）。
            # 过期锚定 due 而非 retry_after：重试会不断推后 retry_after，
            # 以它为基准等于给提醒无限续命（真实事故：起床提醒 5 分钟一次循环 64 次）。
            repeat = reminder.get("repeat", "")
            due_str = reminder.get("due", "")
            if not repeat and due_str:
                try:
                    due = dt.datetime.fromisoformat(due_str)
                    if now - due > dt.timedelta(hours=EXPIRED_AFTER_HOURS):
                        old_delivery_state = reminder.get("delivery_state")
                        reminder["done"] = True
                        reminder["delivery_state"] = "expired"
                        reminder["completed_at"] = now.isoformat()
                        reminder.pop("retry_after", None)
                        reminder.pop("last_delivery_error", None)
                        changed = True
                        log.info(f"[reminders] 一次性提醒{reminder.get('id')}过期{int((now - due).total_seconds() / 3600)}小时，标记expired")
                        # 未成功送达的提醒过期时推送飞书通知，避免用户无感知
                        if old_delivery_state in ("retry", None, "", "pending", "enqueued"):
                            try:
                                from app.notifications import push_feishu_async
                                push_feishu_async(f"⏰ 提醒已过期未送达：{reminder.get('text', '')}")
                            except Exception as e:
                                log.debug(f"[app.reminders] push_feishu_async failed: {e}")
                        continue
                except Exception:
                    pass

            # enqueued 状态：retry_after 未到则跳过，避免 5 分钟内重复申领；
            # 超过 ENQUEUED_MAX_ATTEMPTS 后停止重投（飞书兜底已送达，
            # 设备 pending 队列 TTL 5 分钟，继续入队只是重复 TTS + 推送轰炸）
            if reminder.get("delivery_state") == "enqueued":
                attempts = int(reminder.get("attempt_count", 0) or 0)
                if attempts >= ENQUEUED_MAX_ATTEMPTS:
                    reminder["done"] = True
                    reminder["delivery_state"] = "expired_enqueued"
                    reminder["completed_at"] = now.isoformat()
                    reminder.pop("retry_after", None)
                    changed = True
                    log.info(f"[reminders] 提醒{reminder.get('id')}入队重试{attempts}次达上限"
                             f"（设备持续离线），标记expired_enqueued停止重投")
                    continue
                retry_after = reminder.get("retry_after")
                if retry_after:
                    try:
                        if now < dt.datetime.fromisoformat(retry_after):
                            continue
                    except Exception:
                        pass

            # delivering 状态：检查 claim 是否超时
            if reminder.get("delivery_state") == "delivering":
                claim_started = reminder.get("claim_started_at")
                try:
                    claim_started = dt.datetime.fromisoformat(claim_started)
                except Exception:
                    claim_started = now - dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT + 1)
                if now - claim_started < dt.timedelta(seconds=DELIVERY_CLAIM_TIMEOUT):
                    continue
                # 播报线程可能已崩溃；超过申领超时后允许重新申领同一条提醒。

            # 通用 retry_after 检查（非 enqueued 状态）
            retry_after = reminder.get("retry_after")
            if retry_after:
                try:
                    if now < dt.datetime.fromisoformat(retry_after):
                        continue
                except Exception:
                    pass

            if not due_str:
                continue
            try:
                due = dt.datetime.fromisoformat(due_str)
            except Exception:
                continue

            if now >= due:
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
