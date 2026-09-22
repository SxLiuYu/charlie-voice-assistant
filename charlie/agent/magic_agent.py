"""magic-agent: 开放任务域 —— 固定工具域覆盖不了的请求交给 Hermes Agent CLI 后台执行。

背景（2026-09-20 用户需求）：charlie 的 28 个域全是固定工具域（天气/音乐/提醒…），
"清理磁盘""整理下载文件夹""排查为什么网速慢"这类开放任务没有域能接，用户只能听到
"这个我做不到"。Hermes（agent CLI，本机 ~/.local/bin/hermes）自带 bash/编辑/多步
推理能力，正是干这个的。

设计约束（用户原话："需要在完成任务和用户实时对话中拿到一个最优解，任务要完成，
但是实时对话要精简"）：
  - **实时对话精简**：分类到本域后立刻 yield 一句短确认（"好的，我去处理，完成后
    告诉你。"），零 LLM 往返、不等任务。实测真任务（建文件）耗时 1m51s——
    放进 brain_stream_sentences 会被 35s 大脑超时（agent/llm.py::_BRAIN_RUN_TIMEOUT）
    掐掉并回"抱歉，处理时出错了"。
  - **任务完成**：subprocess.Popen(start_new_session=True) 后台跑 hermes oneshot，
    双保险超时（hermes 自带 --run-budget 软预算 + 本模块硬超时 kill 进程组），
    完成后经 push_feishu_async 推结果（飞书是现成的主动推送通道）。

安全边界（刻意保留 agent 的执行力，只做边界约束）：
  - 任务文本走 `--query-file -`（stdin）：hermes 文档明示"nothing is shell-
    interpreted"，任意中文/引号/反引号/$() 原样传递，不经我们这层 shell。
  - 硬超时 kill 整个进程组（hermes 的工具子进程一并收掉），不残留僵尸。
  - 不设 --yolo：oneshot 实测会自动批准工具调用（建文件任务 4 次工具调用全部执行），
    任务要能真做成；不想给它自动批准权时设 AGENT_CLI_YOLO=0 的现场调法不提供，
    要收紧直接在 hermes 侧配。
  - 任务注册表只留最近 N 条（内存态），供"任务做完了吗"类后续查询扩展。
"""

import contextvars
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

log = logging.getLogger("magic")

# ===== 配置（env 可调，导入时读取——同 agent/asr_tts.py 惯例）=====
AGENT_CLI_ENABLED = os.getenv("AGENT_CLI_ENABLED", "1") == "1"
AGENT_CLI_BIN = os.getenv("AGENT_CLI_BIN", "hermes")
# hermes 的 agent 循环软预算（秒）：到点 hermes 自己收尾
AGENT_CLI_RUN_BUDGET = int(os.getenv("AGENT_CLI_RUN_BUDGET", "600"))
# 本模块硬超时（秒）：>run-budget，含模型冷启/工具开销余量；到点 kill 进程组
AGENT_CLI_TIMEOUT = int(os.getenv("AGENT_CLI_TIMEOUT", "900"))
# hermes --max-turns：多步任务上限（防失控循环）
AGENT_CLI_MAX_TURNS = int(os.getenv("AGENT_CLI_MAX_TURNS", "12"))
# hermes --in 工作目录（空=不传，用进程 cwd）
AGENT_CLI_WORKDIR = os.getenv("AGENT_CLI_WORKDIR", "").strip()
# 推送结果截断长度（飞书消息体，留余量给 hermes 的完整输出）
AGENT_CLI_PUSH_MAX_CHARS = int(os.getenv("AGENT_CLI_PUSH_MAX_CHARS", "800"))
# 任务注册表容量（内存态，超出丢最旧）
_TASK_REGISTRY_MAX = 50

# 语音侧秒回的短确认（用户要求"实时对话要精简"：一句，不解释过程）
ACK_TEXT = "好的，我去处理，完成后告诉你。"

# 常见开放任务关键词（高精度集合，命中即走本域，省 0.8-1.1s LLM 分类）。
# ⚠️ 保持小而准：模糊词（"帮我""弄一下"）会误吞普通对话，一律不在此列，
# 长尾交给 LLM 分类器（prompt 里有 magic-agent 选项）。
AGENT_TASK_KEYWORDS = (
    "清理磁盘", "清一下磁盘", "清下磁盘", "磁盘清理", "释放磁盘", "释放空间",
    "清理空间", "磁盘满了", "磁盘满了", "整理下载", "整理文件夹", "清理缓存",
    "清理垃圾", "排查网络", "修复网络", "网速慢", "为什么这么卡", "系统卡顿",
    "重建索引", "清理日志", "备份一下", "归档一下",
)

# ===== 任务注册表 =====
_tasks: dict = {}
_tasks_lock = threading.Lock()


def agent_cli_available() -> bool:
    """本域是否可用：开关 + hermes 二进制存在。不可用时分类结果照常产生，
    但早退分支不触发，请求落回大脑（大脑用通用工具尽力回答，行为不变）。"""
    if not AGENT_CLI_ENABLED:
        return False
    return shutil.which(AGENT_CLI_BIN) is not None


def _extract_answer(raw: str) -> str:
    """从 hermes oneshot 输出里抠出答案正文。

    实测输出形态（2026-09-20）：
        Query: ...
        Initializing agent...
        ╭─ ☤ Hermes ────...╮
        <答案正文，可多行>
        ╰──────...╯

        Resume this session with: ...
        Session: ...
    只取框内正文；无框时退化为剥掉已知元数据行。
    """
    if not raw:
        return ""
    lines = raw.splitlines()
    # 找框线：第一个 ╭ 开头的行到第一个 ╰ 开头的行之间
    start = end = None
    for i, ln in enumerate(lines):
        if start is None and ln.lstrip().startswith("╭"):
            start = i + 1
        elif start is not None and ln.lstrip().startswith("╰"):
            end = i
            break
    if start is not None and end is not None and end > start:
        inner = [ln for ln in lines[start:end] if ln.strip()]
        # 去掉框内可能残留的 │ 边线（hermes 某些宽度会画）
        cleaned = [re.sub(r"^[│|]\s?", "", ln).rstrip() for ln in inner]
        return "\n".join(cleaned).strip()
    # 无框：剥元数据行
    _META_PREFIXES = ("Query:", "Initializing", "Resume this session",
                      "Session:", "Title:", "Duration:", "Messages:", "hermes ")
    kept = [ln for ln in lines
            if ln.strip() and not ln.startswith(_META_PREFIXES)]
    return "\n".join(kept).strip()


def run_agent_task(task: str) -> tuple:
    """同步跑一次 hermes oneshot（供后台线程调用）。返回 (status, answer)。

    status: "ok" | "empty" | "timeout" | "error" | "no_binary"
    硬超时 kill 整个进程组（start_new_session=True 使 hermes 自成进程组，
    它的 bash 工具子进程也在这个组里，一并收掉）。
    """
    if not agent_cli_available():
        return "no_binary", f"agent CLI ({AGENT_CLI_BIN}) 不可用"
    cmd = [AGENT_CLI_BIN, "chat", "--query-file", "-", "--oneshot",
           "--format", "text", "--run-budget", str(AGENT_CLI_RUN_BUDGET),
           "--max-turns", str(AGENT_CLI_MAX_TURNS)]
    if AGENT_CLI_WORKDIR:
        cmd += ["--in", AGENT_CLI_WORKDIR]
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        out, err = proc.communicate(input=task, timeout=AGENT_CLI_TIMEOUT)
        if proc.returncode != 0:
            return "error", (err or out or "hermes 非零退出").strip()[:500]
        answer = _extract_answer(out)
        if not answer:
            return "empty", "agent 没有产出文本"
        return "ok", answer
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return "timeout", f"超过硬超时 {AGENT_CLI_TIMEOUT}s，已终止"
    except Exception as e:
        if proc is not None:
            _kill_group(proc)
        return "error", str(e)[:500]


def _kill_group(proc) -> None:
    """杀整个进程组（hermes + 它的工具子进程），尽力而为不抛。"""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def _register(task_id: str, task: str) -> None:
    with _tasks_lock:
        if len(_tasks) >= _TASK_REGISTRY_MAX:
            # 丢最旧（按 started 排序的第一个）
            oldest = min(_tasks, key=lambda k: _tasks[k]["started"])
            _tasks.pop(oldest, None)
        _tasks[task_id] = {"task": task, "status": "running",
                           "started": time.time(), "finished": None,
                           "answer": "", "error": ""}


def _finish(task_id: str, status: str, answer: str, error: str) -> None:
    with _tasks_lock:
        _t = _tasks.get(task_id)
        if _t is not None:
            _t.update(status=status, finished=time.time(),
                      answer=answer, error=error)


def _push_result(task: str, status: str, answer: str, error: str) -> None:
    """完成后推飞书（现成的主动推送通道，FEISHU_PUSH_ENABLED 门控）。"""
    try:
        from app.notifications import push_feishu_async
        short = task if len(task) <= 40 else task[:37] + "…"
        if status == "ok":
            body = answer
            if len(body) > AGENT_CLI_PUSH_MAX_CHARS:
                body = body[:AGENT_CLI_PUSH_MAX_CHARS] + f"\n…（已截断，完整 {len(answer)} 字）"
            text = f"🤖 任务完成：{short}\n{body}"
        else:
            text = f"🤖 任务未完成：{short}\n{error}"
        push_feishu_async(text)
    except Exception as e:
        log.warning("[magic-agent] 结果推送失败: %s", e)


def _refresh_ack_cache() -> None:
    """保温即时 ACK 的 TTS 缓存（派发一次就把下一次的合成预算先付掉）。

    TTS 缓存 TTL 1h 且只在写入时刷新：服务空闲 >1h 后再次派发，ACK 要重新走
    StepFun 合成（实测 ~1.9s），700ms 的 filler 竞速（"好的，我想一下。"）会抢
    走首句，用户听到双重应答。启动预热（app/xiaozhi_ws.py::_prewarm_hints）+
    本保温 = 除服务启动后 >1h 的第一次派发外，ACK 恒为缓存命中 ~50ms。
    失败静默：保温只是优化，合成失败下次照常走网络。
    """
    try:
        from app.xiaozhi_ws import _synth_mp3
        _synth_mp3(ACK_TEXT)
    except Exception as e:
        log.debug("[magic-agent] ACK 保温失败(不影响功能): %s", e)


def _run_and_push(task_id: str, task: str) -> None:
    """后台线程体：跑 hermes → 落注册表 → 推结果。任何异常不外抛（守护线程）。"""
    try:
        _refresh_ack_cache()
        status, payload = run_agent_task(task)
        answer, error = (payload, "") if status == "ok" else ("", payload)
        _finish(task_id, status, answer, error)
        log.info("[magic-agent] 任务结束 task=%s status=%s 产出=%d字",
                 task_id, status, len(answer))
        _push_result(task, status, answer, error)
    except Exception as e:
        log.warning("[magic-agent] 后台任务异常 task=%s: %s", task_id, e)
        _finish(task_id, "error", "", str(e)[:500])


def dispatch_task(task: str, session_id: str = "default") -> str:
    """语音链路入口：登记任务 → 起后台线程 → 立刻返回短确认语。

    调用方（brain_stream_sentences 的早退分支）拿到返回值就 yield，
    不等任务——这是"实时对话要精简"的落点。返回 ACK_TEXT；
    hermes 不可用时返回 ""（调用方据此落回大脑路径，行为不变）。

    ⚠️ 后台线程必须 copy_context()：入口层 bind_user 的 ContextVar 不跨线程
    继承（agent/llm.py:1008-1014 的教训——裸线程里 current_binding() 恒 None，
    多用户部署下归属会丢）。
    """
    if not agent_cli_available():
        return ""
    task_id = f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    _register(task_id, task)
    ctx = contextvars.copy_context()
    threading.Thread(
        target=lambda: ctx.run(_run_and_push, task_id, task),
        daemon=True, name=f"agent-task-{task_id}").start()
    log.info("[magic-agent] 派发任务 task=%s session=%s: %s",
             task_id, session_id, task[:60])
    return ACK_TEXT


def agent_task_status(task_id: str) -> dict:
    """查任务状态（注册表快照；未知 id 返回 {}）。"""
    with _tasks_lock:
        _t = _tasks.get(task_id)
        return dict(_t) if _t else {}
