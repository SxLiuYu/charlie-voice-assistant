"""
魔幻手机 - 语音Agent核心
语音闭环: ASR(qwen3-asr) → 大脑(GLM-5.2+Qwen-Agent+MCP) → TTS(qwen3-tts)
连接韧性: Session复用 + 自动重试 + 异常降级
对话记忆: 跨请求保留历史上下文，支持多轮连续对话，持久化到磁盘
"""
import os, json, base64, requests, datetime, time, logging, asyncio, re, threading
from typing import Optional, Generator, Tuple, List, Dict, Any
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

log = logging.getLogger("magic")

FINNA = os.getenv("FINNA_BASE", "https://www.finna.com.cn/v1")
TTS_KEY = os.getenv("TTS_KEY", "REDACTED")
ASR_KEY = os.getenv("ASR_KEY", "REDACTED")
GLM_KEY = os.getenv("GLM_KEY", "app-Egtyx0Fytauhxkr6rWBLZyZl")
TTS_VOICE = os.getenv("TTS_VOICE", "Cherry")
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 3, 5]  # 秒，逐次递增

# ===== 连接池复用 =====
_session = requests.Session()
_session.headers.update({"Connection": "keep-alive"})

# ===== 响应缓存(60秒TTL, 减少重复GLM调用) =====
_cache = {}
_CACHE_TTL = 60  # 秒
_CACHE_MAX = 50

def _cache_get(text: str) -> Optional[str]:
    """获取缓存响应(60秒内有效)"""
    key = text.strip().lower()
    if key in _cache:
        reply, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return reply
        del _cache[key]
    return None

def _cache_set(text: str, reply: str) -> None:
    """设置缓存响应"""
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))  # 移除最旧的
    _cache[text.strip().lower()] = (reply, time.time())

# ===== 对话历史(多会话, 跨请求持久化) =====
_history = []  # 默认会话历史(向后兼容)
_sessions = {"default": _history}  # 多会话: {session_id: [history]}
MAX_HISTORY = 20  # 每个会话保留最近20轮对话(40条消息)
MAX_SESSIONS = 10  # 最多10个并发会话
_history_lock = threading.Lock()  # 防止多线程同时修改对话历史
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation_history.json")

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

def _save_history() -> None:
    with _history_lock:
        try:
            # 保存所有会话(格式: {"default": [...], "session_xxx": [...]})
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(_sessions, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def _load_history() -> None:
    global _history, _sessions
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _sessions = data
            _history = _sessions.get("default", [])
            _sessions["default"] = _history
        elif isinstance(data, list):
            _history = data
            _sessions = {"default": _history}
    except Exception:
        _history = []
        _sessions = {"default": _history}

_load_history()

# ===== 对话上下文管理(token感知截断) =====
MAX_CONTEXT_TOKENS = 4000  # 历史对话token预算(留空间给系统提示+新消息+回复)

def _estimate_tokens(text: str) -> int:
    """粗略估算token数: 中文1字≈1.5token, 英文1词≈1.3token, 符号1个≈1token"""
    if not text:
        return 0
    # 中文字符数
    cn = len(re.findall(r'[\u4e00-\u9fff]', text))
    # 英文单词数
    en = len(re.findall(r'[a-zA-Z]+', text))
    # 其他字符(符号/数字)
    other = len(text) - cn - sum(len(w) for w in re.findall(r'[a-zA-Z]+', text))
    return int(cn * 1.5 + en * 1.3 + other * 0.5)

def _estimate_msg_tokens(msg: dict) -> int:
    """估算单条消息的token数(含role开销)"""
    content = msg.get("content", "")
    return _estimate_tokens(content) + 4  # role标记约4token

def _trim_history_tokens(hist: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> None:
    """原地截断对话历史, 保持在token预算内。
    策略: 从最新的开始保留, 如果超限则移除最旧的。
    总是保留最近2轮(4条消息)作为即时上下文。
    """
    if len(hist) <= 4:
        return  # 不超过4条不截断
    total = sum(_estimate_msg_tokens(m) for m in hist)
    if total <= max_tokens:
        return  # 未超限
    # 从最旧的开始移除, 但保留最近4条
    min_keep = 4
    while len(hist) > min_keep:
        total -= _estimate_msg_tokens(hist[0])
        del hist[0]
        if total <= max_tokens:
            break
    log.debug(f"[context] 截断至{len(hist)}条({total}tok, 预算{max_tokens})")

# 替换原有的简单截断为token感知截断 -> None:
    global _history, _sessions
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # 新格式: 多会话
            _sessions = data
            _history = _sessions.get("default", [])
            _sessions["default"] = _history
        elif isinstance(data, list):
            # 旧格式: 单一会话列表(向后兼容)
            _history = data
            _sessions = {"default": _history}
    except Exception:
        _history = []
        _sessions = {"default": _history}

def reset_history(session_id: str = "default") -> None:
    """重置指定会话的历史(原地清空, 保持引用有效)"""
    with _history_lock:
        hist = _sessions.get(session_id)
        if hist is not None:
            hist.clear()  # 原地清空, 不重新赋值
        else:
            _sessions[session_id] = []
    _save_history()

_load_history()

def _build_system_msg() -> str:
    now = datetime.datetime.now()
    weekdays = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"]
    h = now.hour
    if 5 <= h < 9: period = "清晨"
    elif 9 <= h < 12: period = "上午"
    elif 12 <= h < 14: period = "中午"
    elif 14 <= h < 18: period = "下午"
    elif 18 <= h < 23: period = "晚上"
    else: period = "深夜"
    # 动态加载今日待办数量
    try:
        rf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
        with open(rf, encoding="utf-8") as f: rems = json.load(f)
        today = now.strftime("%Y-%m-%d")
        today_rems = [r for r in rems if not r.get("done") and r.get("due","").startswith(today)]
        todo_ctx = f"今日有{len(today_rems)}项待办" if today_rems else "今日无待办"
    except Exception:
        todo_ctx = ""
    ctx = (f"当前时间：{now.strftime('%Y年%m月%d日')} {weekdays[now.weekday()]} "
           f"{period} {now.strftime('%H:%M')}。{todo_ctx}。用户在Mac Mini上。")
    return (f"你是魔幻手机，中国版贾维斯——用户的私人AI助理。{ctx}\n"
            "你的性格：高效、主动、偶尔幽默，像老朋友一样亲切。\n"
            "你有多个MCP工具：高德地图(天气/POI/路线)、充电桩搜索、购物推荐、翻译、"
            "提醒管理(add_reminder可设提醒,list_reminders可查待办)、文件读写等。\n"
            "行为准则：\n"
            "1. 回复简洁口语化，适合语音播报，通常不超过3句\n"
            "2. 能用工具就用工具，给真实数据而非编造\n"
            "3. 用户提到时间/待办时，主动调add_reminder设提醒\n"
            "4. 记住用户偏好(用Memory MCP)，下次主动应用\n"
            "5. 涉及敏感操作(支付/车辆控制)需先确认\n"
            "6. 如果用户说'你刚才说的'之类，回顾对话历史回答\n"
            "7. 用户问'今天有什么安排'时，如果有提醒会主动列出")

# ===== 带重试的请求封装 =====
def _retry(fn, name: str = "请求") -> Any:
    """带重试的函数调用封装"""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except requests.exceptions.Timeout:
            last_err = f"{name}超时"
            log.warning(f"{name}第{attempt+1}次超时，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
        except requests.exceptions.ConnectionError:
            last_err = f"{name}连接失败"
            log.warning(f"{name}第{attempt+1}次连接失败，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
        except Exception as e:
            last_err = f"{name}异常: {e}"
            log.warning(f"{name}第{attempt+1}次异常: {e}，{'重试...' if attempt < MAX_RETRIES-1 else '放弃'}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF[attempt])
    raise Exception(last_err or f"{name}失败")

# ===== TTS: 文字 → 音频bytes =====
def tts(text: str) -> bytes:
    """文字→语音(WAV bytes), 带重试"""
    def _do():
        r = _session.post(f"{FINNA}/audio/speech",
            headers={"Authorization": f"Bearer {TTS_KEY}", "Content-Type": "application/json"},
            json={"model": "qwen3-tts-flash", "input": text, "voice": TTS_VOICE},
            stream=True, timeout=(10, 60))
        r.raise_for_status()
        audio = b""
        for line in r.iter_lines():
            if not line: continue
            line = line.decode('utf-8', 'ignore')
            if line.startswith("data:"):
                try: d = json.loads(line[5:].strip())
                except: continue
                if "delta" in d.get("type", "") and d.get("audio"):
                    audio += base64.b64decode(d["audio"])
        return audio
    try:
        return _retry(_do, "TTS")
    except Exception as e:
        log.error(f"TTS最终失败: {e}")
        return b""

# ===== ASR: 音频bytes → 文字 =====
def asr(audio_bytes: bytes, fmt: str = "mp3") -> str:
    def _do():
        r = _session.post(f"{FINNA}/audio/transcriptions",
            headers={"Authorization": f"Bearer {ASR_KEY}"},
            files={"file": (f"input.{fmt}", audio_bytes, f"audio/{fmt}")},
            data={"model": "qwen3-asr-flash"}, stream=True, timeout=(10, 60))
        r.raise_for_status()
        text = ""
        for line in r.iter_lines():
            if not line: continue
            line = line.decode('utf-8', 'ignore')
            if line.startswith("data:"):
                try: d = json.loads(line[5:].strip())
                except: continue
                if d.get("type") == "transcript.text.done":
                    text = d.get("text", "")
                elif d.get("type") == "transcript.text.delta" and not text:
                    text = d.get("delta", "")
        return text
    try:
        return _retry(_do, "ASR")
    except Exception as e:
        log.error(f"ASR最终失败: {e}")
        return ""

# ===== 大脑: GLM-5.2 + Qwen-Agent + MCP =====
def _build_brain():
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
        'model': os.getenv('GLM_MODEL', 'glm-5.2'), 'model_type': 'oai',
        'api_base': FINNA, 'api_key': GLM_KEY,
        'generate_cfg': {'use_raw_api': True},
    }
    # MCP服务器配置(可通过MCP_SERVERS环境变量控制启用哪些, 逗号分隔)
    all_mcp = {
        "amap-maps": {"command": "npx", "args": ["-y", "@amap/amap-maps-mcp-server"],
            "env": {"AMAP_MAPS_API_KEY": os.getenv("AMAP_KEY", "REDACTED")}},
        "magic-phone": {"command": os.path.join(os.getcwd(), ".venv/bin/python"),
            "args": ["mcp_server.py"], "cwd": os.getcwd()},
        "baize-skills": {"command": os.path.join(os.getcwd(), ".venv/bin/python"),
            "args": ["baize_skills_mcp.py"], "cwd": os.getcwd(),
            "env": {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY", ""),
                    "ALIYUN_API_KEY": os.getenv("ALIYUN_API_KEY", "")}},
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", os.getenv("FS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))]},
        "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
        "sequential-thinking": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]},
    }
    # 默认启用4个核心服务器(省内存); 可通过.env的MCP_SERVERS覆盖
    enabled = os.getenv("MCP_SERVERS", "amap-maps,magic-phone,baize-skills,filesystem").split(",")
    enabled = [s.strip() for s in enabled if s.strip()]
    mcp_servers = {k: v for k, v in all_mcp.items() if k in enabled}
    log.info(f"[brain] 启用{len(mcp_servers)}个MCP: {list(mcp_servers.keys())}")
    tools = [{"mcpServers": mcp_servers}]
    return Assistant(llm=llm_cfg, name='魔幻手机',
        system_message=_build_system_msg(),
        function_list=tools)

_brain = None
_brain_failures = 0          # 连续失败计数
_brain_last_failure = 0       # 上次失败时间戳
_brain_last_success = 0       # 上次成功时间戳
_MAX_BRAIN_FAILURES = 3       # 连续失败3次后自动重建大脑
_brain_build_time = 0         # 大脑构建时间

def _ensure_event_loop():
    """确保当前线程有event loop(Qwen-Agent MCP可能需要)"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

def _record_brain_failure(error: str = ""):
    """记录大脑失败, 连续失败超过阈值时自动重建"""
    global _brain_failures, _brain_last_failure, _brain
    _brain_failures += 1
    _brain_last_failure = time.time()
    log.error(f"[brain] 失败#{_brain_failures}: {error}")
    if _brain_failures >= _MAX_BRAIN_FAILURES:
        log.warning(f"[brain] 连续失败{_brain_failures}次, 重建大脑+MCP...")
        _brain = None  # 清除旧大脑, 下次请求自动重建
        _brain_failures = 0

def _record_brain_success():
    """记录大脑成功, 重置失败计数"""
    global _brain_failures, _brain_last_success
    _brain_failures = 0
    _brain_last_success = time.time()

def restart_brain() -> str:
    """手动重启大脑(清除旧实例+MCP连接, 下次请求重建)"""
    global _brain, _brain_failures
    _brain = None
    _brain_failures = 0
    log.info("[brain] 手动重启请求, 大脑将在下次请求时重建")
    return "大脑重启中, 下次请求将自动重建"

def brain_status() -> dict:
    """获取大脑健康状态"""
    import datetime as _dt
    return {
        "ready": _brain is not None,
        "consecutive_failures": _brain_failures,
        "max_failures_before_rebuild": _MAX_BRAIN_FAILURES,
        "last_success": _dt.datetime.fromtimestamp(_brain_last_success).isoformat() if _brain_last_success else None,
        "last_failure": _dt.datetime.fromtimestamp(_brain_last_failure).isoformat() if _brain_last_failure else None,
        "uptime_since": _dt.datetime.fromtimestamp(_brain_build_time).isoformat() if _brain_build_time else None,
    }
    """确保当前线程有event loop(Qwen-Agent MCP可能需要)"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

def brain(text: str, session_id: str = "default") -> str:
    """大脑推理: 文字→GLM-5.2+MCP→回复文字"""
    global _brain
    # 缓存命中(60秒内相同查询直接返回)
    cached = _cache_get(text)
    if cached is not None:
        log.info(f"[cache] 命中: {text[:20]}")
        return cached
    _ensure_event_loop()
    if _brain is None:
        try:
            _brain = _build_brain()
            global _brain_build_time
            _brain_build_time = time.time()
            log.info(f"[brain] 大脑构建完成, {len(messages) if 'messages' in dir() else '?'}消息")
        except Exception as e:
            log.error(f"大脑构建失败: {e}")
            return "大脑启动失败，请稍后重试"

    hist = _get_history(session_id)
    messages = list(hist) + [{'role': 'user', 'content': text}]
    try:
        final = None
        for rsp in _brain.run(messages):
            final = rsp
        _record_brain_success()
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        return f"抱歉，我处理时出错了：{str(e)[:50]}"

    reply = "我没听明白"
    if final and isinstance(final, list):
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

    with _history_lock:
        hist.append({'role': 'user', 'content': text})
        hist.append({'role': 'assistant', 'content': reply})
        if len(hist) > MAX_HISTORY * 2:
            # 原地截断(保持引用有效)
            del hist[:len(hist) - (MAX_HISTORY * 2)]
    _save_history()
    _cache_set(text, reply)
    return reply


# ===== 流式大脑: 逐句产出，支持TTS流水线 =====
import queue as _queue
_SENTENCE_END = re.compile(r'[。！？；\n]')
_COMMA_SOFT = re.compile(r'[，,]')
_MIN_CHUNK = 35  # 逗号处至少积累35字才切割(避免过短TTS碎片)
_MAX_CHUNK = 80  # 超过80字强制在下一个标点处切割

def _extract_assistant_text(rsp: Any) -> str:
    """从brain.run()的中间响应中提取assistant文本(累积)"""
    if not isinstance(rsp, list):
        return ""
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
    import re as _re
    t = text
    t = _re.sub(r'\*{1,3}', '', t)          # **粗体** *斜体*
    t = _re.sub(r'^#{1,6}\s*', '', t)        # # 标题
    t = _re.sub(r'^>\s*', '', t)             # > 引用
    t = _re.sub(r'\|', ' ', t)              # 表格管道符
    t = _re.sub(r'```[\s\S]*?```', '', t)  # 代码块
    t = _re.sub(r'`[^`]*`', '', t)           # 行内代码
    t = _re.sub(r'^[-*+]\s+', '', t)        # 列表标记
    t = _re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)  # [text](url) → text
    t = _re.sub(r'\s{2,}', ' ', t)          # 多余空格
    return t.strip()

def brain_stream_sentences(text: str, session_id: str = "default") -> Generator[Tuple[str, str], None, None]:
    """
    流式大脑: 逐句yield完整句子，供TTS流水线使用。
    brain.run()增量产出token → 检测句子边界 → yield完整句。
    最后更新对话历史+缓存。
    yield: (sentence:str, full_reply:str)
    最后一次yield后，full_reply是完整回复。
    """
    global _brain
    # 缓存命中直接返回
    cached = _cache_get(text)
    if cached is not None:
        yield (cached, cached)
        return
    _ensure_event_loop()
    if _brain is None:
        try:
            _brain = _build_brain()
        except Exception as e:
            log.error(f"流式大脑构建失败: {e}")
            yield (f"大脑启动失败: {str(e)[:40]}", f"大脑启动失败: {str(e)[:40]}")
            return

    hist = _get_history(session_id)
    messages = list(hist) + [{"role": "user", "content": text}]
    sent_len = 0       # 已yield的字符数
    full_reply = ""

    try:
        for rsp in _brain.run(messages):
            t = _extract_assistant_text(rsp)
            if not t or len(t) <= sent_len:
                continue
            full_reply = t
            _record_brain_success()
            unsent = full_reply[sent_len:]
            while True:
                # 优先在句末标点处切割
                m = _SENTENCE_END.search(unsent)
                if m:
                    sentence = unsent[:m.end()].strip()
                    unsent = unsent[m.end():]
                    if sentence:
                        sent_len = len(full_reply) - len(unsent)
                        yield (_clean_for_tts(sentence), full_reply)
                    continue
                # 句末无标点但已积累较长 → 在逗号处软切割
                if len(unsent) >= _MIN_CHUNK:
                    cm = _COMMA_SOFT.search(unsent)
                    if cm:
                        sentence = unsent[:cm.end()].strip()
                        unsent = unsent[cm.end():]
                        if sentence:
                            sent_len = len(full_reply) - len(unsent)
                            yield (_clean_for_tts(sentence), full_reply)
                        continue
                break
            sent_len = len(full_reply) - len(unsent)
    except Exception as e:
        _record_brain_failure(str(e)[:60])
        if not full_reply:
            full_reply = f"抱歉，处理时出错了：{str(e)[:40]}"

    # 剩余文本作为最后一句
    if full_reply and len(full_reply) > sent_len:
        remaining = full_reply[sent_len:].strip()
        if remaining:
            yield (_clean_for_tts(remaining), full_reply)
    elif not full_reply:
        full_reply = "我没听明白"
        yield (full_reply, full_reply)

    # 更新历史+缓存(与brain()保持一致)
    with _history_lock:
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": full_reply})
        if len(hist) > MAX_HISTORY * 2:
            del hist[:len(hist) - (MAX_HISTORY * 2)]
        _trim_history_tokens(hist)  # token感知截断
    _save_history()
    _cache_set(text, full_reply)

def tts_to_mp3(text: str) -> bytes:
    """TTS生成 + WAV转MP3 (供流式端点使用)"""
    audio = tts(text)
    if not audio or len(audio) < 100:
        return b""
    # 内联wav_to_mp3逻辑(避免循环导入voice_server)
    import subprocess, tempfile, os as _os
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio)
        inp = f.name
    out = inp.replace(".wav", ".mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", inp, "-b:a", "32k", "-ac", "1", out],
                       capture_output=True, timeout=10)
        with open(out, "rb") as f:
            return f.read()
    except Exception:
        return audio  # 失败返回原始WAV
    finally:
        for p in (inp, out):
            try:
                _os.unlink(p)
            except Exception:
                pass

def stream_voice_pipeline(text: str) -> Generator[Tuple[str, str, bytes], None, None]:
    """
    流式语音流水线生成器: 大脑逐句产出 → TTS → yield (type, sentence, mp3)。
    type: "sentence"(文字+音频), "error"(错误信息), "done"(完成)
    大脑流式产出减少了整体等待时间。
    服务器端可在此基础上实现更细粒度的并行(见 voice_server SSE端点)。
    """
    try:
        for sentence, full_reply in brain_stream_sentences(text):
            mp3 = tts_to_mp3(sentence)
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
        text = "(未识别到语音)"
    reply = brain(text)
    audio_out = tts(reply)
    return text, reply, audio_out

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
    with open("/tmp/voice_reply.wav", "wb") as f: f.write(audio_out)
    print(f"  已保存 /tmp/voice_reply.wav")
