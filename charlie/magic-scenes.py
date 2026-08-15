"""magic-scenes: 场景自动化 Protocol 引擎 v2

可配置的多步操作序列: 用户可通过对话教 Charlie 新场景。
内置: goodnight(晚安) / good_morning(早安) / movie_time(电影) / leaving_home(出门)

v2 改进:
- 新增步骤类型: wait(等待), if_condition(条件分支), llm(LLM生成)
- learn_protocol() 用 ARK LLM 解析自然语言步骤, 替代关键词匹配
- execute_protocol() 支持条件分支和步骤间延迟
"""
from mcp.server.fastmcp import FastMCP
import os, requests, datetime, subprocess, json as _json, threading, time, logging, re

log = logging.getLogger("magic")

mcp = FastMCP("magic-scenes")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
PROTOCOLS_FILE = os.path.join(DATA_DIR, "protocols.json")
_protocols_lock = threading.Lock()

# ===== 内置 Protocol 定义 =====
_BUILTIN_PROTOCOLS = {
    "goodnight": {
        "name": "晚安",
        "triggers": ["晚安", "睡觉", "好梦", "睡了", "我累了", "goodnight"],
        "auto_trigger": {"state": "home_sleeping"},
        "steps": [
            {"action": "ac_control", "params": {"action": "off"}},
            {"action": "tv_control", "params": {"action": "power_off"}},
            {"action": "reminder", "params": {"text": "起床", "time": "tomorrow 08:00"}},
            {"action": "tts", "params": {"template": "晚安，明天{weather}。"}},
        ],
    },
    "good_morning": {
        "name": "早安",
        "triggers": ["早上好", "早安", "起床了", "good morning", "上午好"],
        "auto_trigger": None,
        "steps": [
            {"action": "ac_control", "params": {"action": "cool"}},
            {"action": "tts", "params": {"template": "早上好！今天{weather}，{todo_count}项待办。"}},
        ],
    },
    "movie_time": {
        "name": "电影",
        "triggers": ["看电影", "电影模式", "追剧", "movie"],
        "auto_trigger": None,
        "steps": [
            {"action": "volume", "params": {"level": 30}},
            {"action": "tts", "params": {"template": "电影模式已启动，观影愉快。"}},
        ],
    },
    "leaving_home": {
        "name": "出门",
        "triggers": ["出门", "我走了", "上班了", "leaving"],
        "auto_trigger": {"state": "away"},
        "steps": [
            {"action": "ac_control", "params": {"action": "off"}},
            {"action": "tv_control", "params": {"action": "power_off"}},
            {"action": "tts", "params": {"template": "出门注意，{weather}。"}},
        ],
    },
}


_protocols_cache: dict | None = None
_protocols_cache_mtime: float = 0


def _load_protocols() -> dict:
    """加载所有 Protocol (内置 + 用户自定义) — 带文件 mtime 缓存"""
    global _protocols_cache, _protocols_cache_mtime
    try:
        mtime = os.path.getmtime(PROTOCOLS_FILE) if os.path.exists(PROTOCOLS_FILE) else 0
    except OSError:
        mtime = 0
    if _protocols_cache is not None and mtime == _protocols_cache_mtime:
        return _protocols_cache
    protocols = dict(_BUILTIN_PROTOCOLS)
    try:
        if os.path.exists(PROTOCOLS_FILE):
            with _protocols_lock:
                with open(PROTOCOLS_FILE, 'r', encoding='utf-8') as f:
                    custom = _json.load(f)
            if isinstance(custom, dict):
                protocols.update(custom)
    except Exception as e:
        log.debug(f"[scenes] 加载自定义协议失败: {e}")
    _protocols_cache = protocols
    _protocols_cache_mtime = mtime
    return protocols


def _save_custom_protocol(key: str, protocol: dict):
    """保存用户自定义 Protocol"""
    global _protocols_cache
    with _protocols_lock:
        custom = {}
        try:
            if os.path.exists(PROTOCOLS_FILE):
                with open(PROTOCOLS_FILE, 'r', encoding='utf-8') as f:
                    custom = _json.load(f)
        except Exception as e:
            log.debug(f"[scenes] 读取自定义协议失败: {e}")
        custom[key] = protocol
        try:
            with open(PROTOCOLS_FILE, 'w', encoding='utf-8') as f:
                _json.dump(custom, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"[scenes] 保存自定义协议失败: {e}")
    _protocols_cache = None  # 使缓存失效


def _get_weather() -> str:
    """获取今天天气摘要"""
    AMAP = os.getenv("AMAP_KEY", "")
    try:
        r = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": "110000", "key": AMAP, "extensions": "all"}, timeout=10).json()
        casts = (r.get("forecasts") or [{}])[0].get("casts", [])
        if casts:
            today = casts[0]
            day_w = today.get("dayweather", "")
            night_w = today.get("nightweather", "")
            day_temp = today.get("daytemp", "")
            night_temp = today.get("nighttemp", "")
            weather_parts = []
            for w in (day_w, night_w):
                if w and w not in weather_parts:
                    weather_parts.append(w)
            weather = "转".join(weather_parts) if len(weather_parts) > 1 else (weather_parts[0] if weather_parts else "")
            return f"今天{weather}，{day_temp}到{night_temp}度"
    except Exception:
        pass
    return ""


def _get_today_todos() -> list:
    """获取今日待办"""
    try:
        from app.reminders import _load_reminders
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        rems = _load_reminders()
        return [r for r in rems if not r.get("done") and r.get("due", "").startswith(today_str)]
    except Exception:
        return []


def _ac_control(action: str) -> str:
    """控制空调(通过 Tuya 2B 红外云 API 触发红外发码)"""
    try:
        from tuya_api import TuyaCloudAPI
        infrared_id = os.getenv("TUYA_IR_DEVICE_ID", "")
        remote_id = os.getenv("TUYA_AC_DEVICE_ID", "")
        if not infrared_id or not remote_id:
            log.warning("[ac] 红外网关/空调设备ID未配置, TUYA_IR_DEVICE_ID/TUYA_AC_DEVICE_ID")
            return "空调控制失败(未配置TUYA_IR/AC_DEVICE_ID)"
        act = action.lower()
        mode_map = {"cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4}
        if act == "off":
            power, mode = 0, None
        else:
            power, mode = 1, mode_map.get(act)
        temp, wind = 26, 1
        log.info(f"[ac] scenes命令: power={power} mode={mode} temp={temp} wind={wind} (action={action!r})")
        api = TuyaCloudAPI()
        api.ac_scenes_command(infrared_id, remote_id, power=power, mode=mode, temp=temp, wind=wind)
        log.info(f"[ac] scenes指令发送成功: {action}")
        return f"空调已{action}"
    except Exception as e:
        log.warning(f"[ac] scenes控制失败(action={action!r}): {e}")
        return "空调控制失败"


def _tv_control(action: str) -> str:
    """控制电视 — 走 ESP32 红外网关（需在 .env 配置 ESP32_IP）。

    未配置 ESP32_IP 时直接返回明确提示，不再向硬编码的内网地址发请求造成 3 秒超时。
    （TODO: 后续可改用 Tuya 码库 key_code 参数，与空调 switch_power 抽象不同，待调研设备/码库 ID）
    """
    esp32_ip = os.getenv("ESP32_IP", "").strip()
    if not esp32_ip:
        log.info("[tv] 未配置 ESP32_IP，跳过电视红外控制")
        return "电视控制未配置（请在设置中填写 ESP32_IP）"
    try:
        log.info(f"[tv] 发送红外指令: action={action!r} target=http://{esp32_ip}/api/ir/send")
        resp = requests.post(f"http://{esp32_ip}/api/ir/send",
            json={"device": "tv", "action": action}, timeout=3)
        log.info(f"[tv] 指令响应: status={resp.status_code}")
        return f"电视已{action}"
    except Exception as e:
        log.warning(f"[tv] 控制失败(action={action!r}): {e}")
        return "电视控制失败（设备可能不在线）"


def _set_volume(level: int) -> str:
    """设置系统音量（跨平台）"""
    try:
        import platform as _pf
        _sys = _pf.system()
        if _sys == "Darwin":
            subprocess.run(["osascript", "-e", f"set volume output volume {level}"], timeout=5)
        elif _sys == "Windows":
            try:
                import comtypes  # type: ignore
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
                from ctypes import cast, POINTER
                dev = AudioUtilities.GetSpeakers()
                iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
                vol = cast(iface, POINTER(IAudioEndpointVolume))
                vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level / 100.0)), None)
            except Exception:
                return "Windows 音量调节需安装 pycaw（pip install pycaw）"
        else:  # Linux
            import shutil
            if shutil.which('amixer'):
                subprocess.run(['amixer', '-q', 'set', 'Master', f'{level}%'], timeout=5)
            elif shutil.which('pactl'):
                subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{level}%'], timeout=5)
            else:
                return "音量调节失败：未找到 amixer/pactl"
        return f"音量已调至{level}%"
    except Exception:
        return "音量调节失败"


def _set_reminder(text: str, time_str: str = "") -> str:
    """设置提醒"""
    try:
        from app.reminders import append_reminder
        from utils import parse_time_str
        due = parse_time_str(time_str) if time_str else None
        append_reminder(text, time_str or "", due or "")
        return f"已设提醒：{text}"
    except Exception as e:
        return f"提醒设置失败: {e}"


def _fill_template(template: str) -> str:
    """填充模板变量: {weather} {todo_count} {date}"""
    result = template
    result = result.replace("{weather}", _get_weather() or "天气未知")
    todos = _get_today_todos()
    result = result.replace("{todo_count}", f"{len(todos)}" if todos else "没有")
    result = result.replace("{date}", datetime.datetime.now().strftime("%Y年%m月%d日"))
    return result


# ===== v2 新增步骤执行器 =====

def _wait_step(params: dict) -> str:
    """等待指定秒数"""
    seconds = int(params.get("seconds", 5))
    time.sleep(min(seconds, 30))  # 上限30秒, 防止长时间阻塞
    return f"等待{seconds}秒"


def _evaluate_condition(condition: str) -> bool:
    """评估条件表达式, 返回 True/False
    支持的条件:
    - weather_contains=X: 天气中包含X (如 weather_contains=雨)
    - hour_after=H: 当前小时 >= H (如 hour_after=18)
    - hour_before=H: 当前小时 < H (如 hour_before=9)
    - weekday: 今天是工作日
    - weekend: 今天是周末
    """
    try:
        if condition.startswith("weather_contains="):
            keyword = condition.split("=", 1)[1]
            weather = _get_weather()
            return keyword in weather
        elif condition.startswith("hour_after="):
            h = int(condition.split("=", 1)[1])
            return datetime.datetime.now().hour >= h
        elif condition.startswith("hour_before="):
            h = int(condition.split("=", 1)[1])
            return datetime.datetime.now().hour < h
        elif condition == "weekday":
            return datetime.datetime.now().weekday() < 5
        elif condition == "weekend":
            return datetime.datetime.now().weekday() >= 5
    except Exception:
        pass
    return False


def _if_condition_step(params: dict) -> str:
    """条件分支: 满足条件执行 then_steps, 否则执行 else_steps
    params:
    - condition: 条件表达式 (如 "weather_contains=雨")
    - then: 子步骤列表 [{action, params}, ...]
    - else: 子步骤列表 (可选)
    """
    condition = params.get("condition", "")
    is_met = _evaluate_condition(condition)
    steps = params.get("then" if is_met else "else", [])
    if not steps:
        return f"条件{'满足' if is_met else '不满足'}, 无步骤"
    results = []
    for step in steps:
        results.append(_execute_step(step))
    return f"条件{'满足' if is_met else '不满足'}: " + "；".join(results)


def _llm_step(params: dict) -> str:
    """用 LLM 生成回复 (通过 ARK API)
    params:
    - prompt: 提示词 (如 "根据当前时间说一句早安问候")
    - max_tokens: 最大token数 (默认100)
    """
    prompt = params.get("prompt", "说一句话。")
    max_tokens = int(params.get("max_tokens", 100))
    ark_key = os.getenv("ARK_KEY", "")
    ark_base = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
    ark_model = os.getenv("ARK_MODEL", "ark-code-latest")
    if not ark_key:
        return _fill_template(params.get("template", "无法生成回复。"))
    try:
        r = requests.post(f"{ark_base}/chat/completions",
            headers={"Authorization": f"Bearer {ark_key}", "Content-Type": "application/json"},
            json={
                "model": ark_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            }, timeout=10)
        reply = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if reply:
            return reply
        return "LLM生成失败"
    except Exception:
        return _fill_template(params.get("template", "生成失败。"))


# ===== 步骤执行器 =====
_STEP_EXECUTORS = {
    "ac_control": lambda params: _ac_control(params.get("action", "off")),
    "tv_control": lambda params: _tv_control(params.get("action", "power_off")),
    "volume": lambda params: _set_volume(int(params.get("level", 50))),
    "reminder": lambda params: _set_reminder(params.get("text", ""), params.get("time", "")),
    "tts": lambda params: _fill_template(params.get("template", "已执行。")),
    "wait": _wait_step,
    "if_condition": _if_condition_step,
    "llm": _llm_step,
}


def _execute_step(step: dict) -> str:
    """执行单个步骤, 返回结果文字"""
    action = step.get("action", "")
    params = step.get("params", {})
    executor = _STEP_EXECUTORS.get(action)
    if executor:
        try:
            return executor(params)
        except Exception as e:
            return f"{action}执行失败: {e}"
    return f"未知操作: {action}"


def match_protocol(text: str) -> str | None:
    log.debug(f"[scenes] match_protocol(text={text[:30]})")
    """检查文本是否匹配某个 Protocol 的触发词, 返回 protocol key 或 None"""
    protocols = _load_protocols()
    for key, proto in protocols.items():
        for trigger in proto.get("triggers", []):
            if trigger in text:
                return key
    return None


def execute_protocol(key: str) -> str:
    log.info(f"[scenes] execute_protocol(key={key})")
    """执行指定 Protocol, 返回结果摘要"""
    protocols = _load_protocols()
    proto = protocols.get(key)
    if not proto:
        return f"未找到场景: {key}"
    results = []
    for step in proto.get("steps", []):
        result = _execute_step(step)
        results.append(result)
    name = proto.get("name", key)
    return f"{name}场景已执行：" + "；".join(results) + "。"


@mcp.tool()
def goodnight() -> str:
    """晚安模式: 关空调+关电视+播明天天气+设起床提醒

    例: goodnight() → 关空调、关电视、设明天8点起床提醒、播报天气
    """
    return execute_protocol("goodnight")


@mcp.tool()
def good_morning() -> str:
    """早安模式: 开空调+播报今天天气+今日待办

    例: good_morning() → 开空调、播报天气、列出今日待办
    """
    return execute_protocol("good_morning")


@mcp.tool()
def movie_time() -> str:
    """电影模式: 调低音量+播报观影提示

    例: movie_time() → 调低音量、播报观影提示
    """
    return execute_protocol("movie_time")


@mcp.tool()
def leaving_home() -> str:
    """出门模式: 关空调+关电视+播报天气

    例: leaving_home() → 关空调、关电视、播报天气
    """
    return execute_protocol("leaving_home")


# ===== v2: LLM 辅助的步骤解析 =====

def _parse_steps_with_llm(steps_description: str) -> list:
    """用 ARK LLM 将自然语言步骤描述转为结构化步骤列表"""
    ark_key = os.getenv("ARK_KEY", "")
    ark_base = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
    ark_model = os.getenv("ARK_MODEL", "ark-code-latest")
    if not ark_key:
        return _parse_steps_keyword(steps_description)

    system_prompt = """你是一个场景步骤解析器。将用户的自然语言步骤描述转为JSON数组。
每个步骤是一个对象: {"action": "类型", "params": {...}}
支持的action类型:
- ac_control: 空调控制, params: {"action": "on/off/cool/heat"}
- tv_control: 电视控制, params: {"action": "power_off"}
- volume: 音量, params: {"level": 数字}
- reminder: 提醒, params: {"text": "内容", "time": "时间"}
- tts: 语音播报, params: {"template": "模板文字"}
- wait: 等待, params: {"seconds": 数字}
- if_condition: 条件分支, params: {"condition": "条件", "then": [步骤], "else": [步骤]}
- llm: LLM生成, params: {"prompt": "提示词"}
只输出JSON数组, 不要其他文字。"""

    try:
        r = requests.post(f"{ark_base}/chat/completions",
            headers={"Authorization": f"Bearer {ark_key}", "Content-Type": "application/json"},
            json={
                "model": ark_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"步骤描述: {steps_description}"},
                ],
                "max_tokens": 500,
            }, timeout=15)
        reply = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # 提取JSON数组
        if reply.startswith("["):
            steps = _json.loads(reply)
            if isinstance(steps, list) and steps:
                return steps
        # 尝试从markdown代码块中提取
        import re
        match = re.search(r'\[.*\]', reply, re.DOTALL)
        if match:
            steps = _json.loads(match.group())
            if isinstance(steps, list) and steps:
                return steps
    except Exception:
        pass
    return _parse_steps_keyword(steps_description)


def _parse_steps_keyword(steps_description: str) -> list:
    """关键词匹配解析步骤 (LLM不可用时的降级方案)"""
    step_descs = [s.strip() for s in steps_description.split(",") if s.strip()]
    steps = []
    for desc in step_descs:
        desc_lower = desc.lower()
        if "空调" in desc and "开" in desc:
            steps.append({"action": "ac_control", "params": {"action": "on"}})
        elif "空调" in desc and ("关" in desc or "off" in desc_lower):
            steps.append({"action": "ac_control", "params": {"action": "off"}})
        elif "空调" in desc and "制冷" in desc:
            steps.append({"action": "ac_control", "params": {"action": "cool"}})
        elif "空调" in desc and "制热" in desc:
            steps.append({"action": "ac_control", "params": {"action": "heat"}})
        elif "电视" in desc and "关" in desc:
            steps.append({"action": "tv_control", "params": {"action": "power_off"}})
        elif "音量" in desc or "调音" in desc:
            level = 50
            nums = re.findall(r'\d+', desc)
            if nums:
                level = int(nums[0])
            steps.append({"action": "volume", "params": {"level": level}})
        elif "提醒" in desc:
            steps.append({"action": "reminder", "params": {"text": desc, "time": ""}})
        elif "等待" in desc or "等" in desc:
            seconds = 5
            nums = re.findall(r'\d+', desc)
            if nums:
                seconds = int(nums[0])
            steps.append({"action": "wait", "params": {"seconds": seconds}})
        elif "如果" in desc or "要是" in desc:
            steps.append({"action": "if_condition", "params": {"condition": "weather_contains=雨", "then": [{"action": "tts", "params": {"template": desc}}]}})
        else:
            steps.append({"action": "tts", "params": {"template": desc}})
    return steps


@mcp.tool()
def learn_protocol(name: str, trigger_words: str, steps_description: str) -> str:
    """学习新的场景 Protocol。用户通过对话教 Charlie 新场景。

    参数:
    - name: 场景名称(如"开工了")
    - trigger_words: 触发词列表, 逗号分隔(如"开工了,开始工作,干活")
    - steps_description: 步骤描述, 自然语言(如"打开空调, 开灯, 播放专注音乐")

    v2: 优先用LLM解析步骤, 失败时降级为关键词匹配。
    支持新步骤类型: wait(等待N秒), if_condition(条件分支), llm(LLM生成)

    例: learn_protocol("开工了", "开工了,开始工作", "打开空调, 等待3秒, 播放专注音乐")
        learn_protocol("雨天提醒", "下雨了", "如果下雨就提醒带伞, 否则说天气不错")
    """
    triggers = [t.strip() for t in trigger_words.split(",") if t.strip()]
    if not triggers:
        return "需要至少一个触发词"
    # v2: 优先用LLM解析, 失败降级为关键词匹配
    steps = _parse_steps_with_llm(steps_description)
    if not steps:
        return "未识别到可执行的步骤"
    key = name.replace(" ", "_").lower()
    protocol = {
        "name": name,
        "triggers": triggers,
        "auto_trigger": None,
        "steps": steps,
    }
    _save_custom_protocol(key, protocol)
    step_summary = "；".join(f"{s.get('action', '?')}" for s in steps)
    return f"已学习场景「{name}」，以后说「{triggers[0]}」就会执行({len(steps)}步): {step_summary}"


@mcp.tool()
def list_protocols() -> str:
    """列出所有可用场景 Protocol

    例: list_protocols() → 列出所有内置和自定义场景
    """
    protocols = _load_protocols()
    lines = []
    for key, proto in protocols.items():
        triggers = "、".join(proto.get("triggers", []))
        step_count = len(proto.get("steps", []))
        is_builtin = key in _BUILTIN_PROTOCOLS
        lines.append(f"[{'内置' if is_builtin else '自定义'}] {proto.get('name', key)}: 触发词「{triggers}」, {step_count}步")
    return "\n".join(lines) if lines else "暂无场景"


@mcp.tool()
def execute_scene(name: str) -> str:
    """手动执行指定场景

    参数:
    - name: 场景名称或触发词(如"晚安"或"goodnight")

    例: execute_scene("晚安") → 执行晚安场景
        execute_scene("goodnight") → 执行晚安场景
    """
    # 先精确匹配 key
    protocols = _load_protocols()
    if name in protocols:
        return execute_protocol(name)
    # 再匹配触发词
    for key, proto in protocols.items():
        if name in proto.get("triggers", []):
            return execute_protocol(key)
        if name == proto.get("name", ""):
            return execute_protocol(key)
    return f"未找到场景「{name}」，可用场景: {', '.join(protocols.keys())}"


if __name__ == "__main__":
    mcp.run()
