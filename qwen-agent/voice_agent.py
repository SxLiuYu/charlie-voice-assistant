"""
魔幻手机 - 语音Agent核心
语音闭环: ASR(qwen3-asr) → 大脑(GLM-5.2+Qwen-Agent+MCP) → TTS(qwen3-tts)
连接韧性: Session复用 + 自动重试 + 异常降级
对话记忆: 跨请求保留历史上下文，支持多轮连续对话，持久化到磁盘
"""
import os, json, base64, requests, datetime, time, logging
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

# ===== 对话历史(跨请求持久化) =====
_history = []
MAX_HISTORY = 20  # 保留最近20轮对话(40条消息)
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation_history.json")

def _save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _load_history():
    global _history
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            _history = json.load(f)
    except Exception:
        _history = []

def reset_history():
    global _history
    _history = []
    _save_history()

_load_history()

def _build_system_msg():
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
        rems = json.load(open(rf, encoding="utf-8"))
        today = now.strftime("%Y-%m-%d")
        today_rems = [r for r in rems if not r.get("done") and r.get("due","").startswith(today)]
        todo_ctx = f"今日有{len(today_rems)}项待办" if today_rems else "今日无待办"
    except Exception:
        todo_ctx = ""
    ctx = (f"当前时间：{now.strftime('%Y年%m月%d日')} {weekdays[now.weekday()]} "
           f"{period} {now.strftime('%H:%M')}。{todo_ctx}。用户在Mac Mini上。")
    return (f"你是魔幻手机，中国版贾维斯——用户的私人AI助理。{ctx}\n"
            "你的性格：高效、主动、偶尔幽默，像老朋友一样亲切。\n"
            "你有6个MCP工具：高德地图(天气/POI/路线)、充电桩搜索、购物推荐、翻译、"
            "提醒管理(add_reminder可设提醒,list_reminders可查待办)、文件读写、知识图谱记忆。\n"
            "行为准则：\n"
            "1. 回复简洁口语化，适合语音播报，通常不超过3句\n"
            "2. 能用工具就用工具，给真实数据而非编造\n"
            "3. 用户提到时间/待办时，主动调add_reminder设提醒\n"
            "4. 记住用户偏好(用Memory MCP)，下次主动应用\n"
            "5. 涉及敏感操作(支付/车辆控制)需先确认\n"
            "6. 如果用户说'你刚才说的'之类，回顾对话历史回答\n"
            "7. 用户问'今天有什么安排'时，如果有提醒会主动列出")

# ===== 带重试的请求封装 =====
def _retry(fn, name="请求"):
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
def asr(audio_bytes: bytes, fmt="mp3") -> str:
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
    from qwen_agent.agents import Assistant
    llm_cfg = {
        'model': os.getenv('GLM_MODEL', 'glm-5.2'), 'model_type': 'oai',
        'api_base': FINNA, 'api_key': GLM_KEY,
        'generate_cfg': {'use_raw_api': True},
    }
    tools = [{"mcpServers": {
        "amap-maps": {"command": "npx", "args": ["-y", "@amap/amap-maps-mcp-server"],
            "env": {"AMAP_MAPS_API_KEY": os.getenv("AMAP_KEY", "REDACTED")}},
        "magic-phone": {"command": os.path.join(os.getcwd(), ".venv/bin/python"),
            "args": ["mcp_server.py"], "cwd": os.getcwd()},
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/sxliuyu/orca/projects/傻妞"]},
        "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
        "baize-skills": {"command": os.path.join(os.getcwd(), ".venv/bin/python"),
            "args": ["baize_skills_mcp.py"], "cwd": os.getcwd(),
            "env": {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY", ""),
                    "ALIYUN_API_KEY": os.getenv("ALIYUN_API_KEY", "")}},
        "sequential-thinking": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]}
    }}]
    return Assistant(llm=llm_cfg, name='魔幻手机',
        system_message=_build_system_msg(),
        function_list=tools)

_brain = None

def brain(text: str) -> str:
    global _brain, _history
    if _brain is None:
        try:
            _brain = _build_brain()
        except Exception as e:
            log.error(f"大脑构建失败: {e}")
            return "大脑启动失败，请稍后重试"

    messages = list(_history) + [{'role': 'user', 'content': text}]
    try:
        final = None
        for rsp in _brain.run(messages):
            final = rsp
    except Exception as e:
        log.error(f"大脑推理异常: {e}")
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

    _history.append({'role': 'user', 'content': text})
    _history.append({'role': 'assistant', 'content': reply})
    if len(_history) > MAX_HISTORY * 2:
        _history = _history[-(MAX_HISTORY * 2):]
    _save_history()
    return reply

# ===== 完整语音闭环 =====
def voice_loop(audio_in: bytes, fmt="mp3") -> tuple:
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
    open("/tmp/voice_reply.wav", "wb").write(audio_out)
    print(f"  已保存 /tmp/voice_reply.wav")
