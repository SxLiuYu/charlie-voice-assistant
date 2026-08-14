"""Device control fast paths: AC (Tuya IR), volume, mute, display sleep.

Extracted from voice_agent.py. Handles smart home and system control
keywords, bypassing LLM for sub-second response.
"""
import os, sys, re, platform, subprocess, logging

log = logging.getLogger("magic")


def direct_ac_control(text: str) -> str:
    """空调控制关键词命中时直接调用 Tuya 2B 红外云 API,绕过 LLM+MCP(2-4s→~0.2s)。
    解析: 开/关 + 模式(制冷/制热/除湿/送风/自动) + 温度(X度) + 风速(高/中/低)。
    返回自然语言回复,无明确指令或调用失败时返回空串(回退 brain)。
    """
    try:
        from tuya_api import TuyaCloudAPI
    except Exception as e:
        log.warning(f"[ac] tuya_api 导入失败: {e}")
        return ""
    infrared_id = os.getenv("TUYA_IR_DEVICE_ID", "")
    remote_id = os.getenv("TUYA_AC_DEVICE_ID", "")
    if not infrared_id or not remote_id:
        return ""
    try:
        api = TuyaCloudAPI()
    except ValueError as e:
        log.warning(f"[ac] 2B 凭证未配置: {e}")
        return ""
    t = text.strip()

    if (re.search(r"关(?:闭|掉|上)?\s*空调|空调\s*关|把空调关|关了", t) or
            (("空调" in t or "电扇" in t) and "关" in t and "开" not in t)):
        try:
            log.info(f"[ac] 查询当前状态: infrared={infrared_id[:8]}... remote={remote_id[:8]}...")
            cur = api.ac_status(infrared_id, remote_id)
            log.info(f"[ac] 操作前状态: power={cur.get('power')} mode={cur.get('mode')} temp={cur.get('temp')}°C wind={cur.get('wind')}")
            api.ac_scenes_command(infrared_id, remote_id, power=0)
            log.info(f"[ac] 直连关机: ASR={t[:20]} → power=0")
            return "好的，空调已关闭。"
        except Exception as e:
            log.warning(f"[ac] 关机失败: {e}")
            return ""

    power = False
    mode = None
    temp = None
    fan = None

    MODE_2B = {"cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4}
    if any(k in t for k in ("制冷", "制冷模式", "冷风", "降温")):
        mode = MODE_2B["cool"]
    elif any(k in t for k in ("制热", "制热模式", "暖风", "加热", "升温")):
        mode = MODE_2B["heat"]
    elif any(k in t for k in ("除湿", "抽湿")):
        mode = MODE_2B["dry"]
    elif any(k in t for k in ("送风", "自然风", "风机模式")):
        mode = MODE_2B["fan"]
    elif any(k in t for k in ("自动模式", "全自动")) or ("自动" in t and "模式" in t):
        mode = MODE_2B["auto"]
    if "高风" in t or "最大风" in t:
        fan = 3
    elif "中风" in t or "中等风" in t:
        fan = 2
    elif "低风" in t or "小风" in t or "微风" in t:
        fan = 1
    m = re.search(r"(\d{1,3})\s*(?:度|℃|°C|°C|摄氏度)", t)
    if not m:
        m = re.search(r"(?:调到|设成|调到|调至|开?\s*到)\s*(\d{1,3})", t)
    if m:
        try:
            temp = int(m.group(1))
        except ValueError:
            temp = None

    if any(k in t for k in ("开空调", "打开空调", "把空调", "开机制冷", "开机", "打开制冷", "开启空调")):
        power = True
    elif mode is not None or temp is not None or fan is not None:
        power = True
    elif any(k in t for k in ("开",)) and any(k in t for k in ("空调", "制冷", "制热", "除湿")):
        power = True
    else:
        power = False

    if mode is None and temp is None and fan is None and not power:
        return ""

    if power:
        eff_temp = max(16, min(30, temp if temp is not None else 26))
        try:
            log.info(f"[ac] 查询当前状态: infrared={infrared_id[:8]}... remote={remote_id[:8]}...")
            cur = api.ac_status(infrared_id, remote_id)
            log.info(f"[ac] 操作前状态: power={cur.get('power')} mode={cur.get('mode')} temp={cur.get('temp')}°C wind={cur.get('wind')}")
            log.info(f"[ac] 发送指令: power=1 mode={mode} temp={eff_temp} wind={fan} (ASR={t[:30]})")
            api.ac_scenes_command(infrared_id, remote_id, power=1, mode=mode, temp=eff_temp, wind=fan)
            parts = ["空调已打开"]
            if mode is not None:
                parts.append({0: "制冷", 1: "制热", 2: "自动", 3: "送风", 4: "除湿"}[mode])
            parts.append(f"{eff_temp}度")
            if fan is not None:
                parts.append({1: "低风", 2: "中风", 3: "高风"}.get(fan, ""))
            reply = "，".join(p for p in parts if p) + "。"
            log.info(f"[ac] 直连开机成功: {reply}")
            return reply
        except Exception as e:
            log.warning(f"[ac] 开机失败: {e}")
            return ""
    return ""


def set_volume(delta_pct: int) -> bool:
    """跨平台系统音量增减(delta_pct 正=调大/负=调小)。返回是否成功。"""
    _sys = platform.system()
    try:
        if _sys == "Darwin":
            op = "+" if delta_pct >= 0 else ""
            subprocess.run(['osascript', '-e', f'set volume output volume (output volume of (get volume settings) {op}{delta_pct})'], timeout=3)
        elif _sys == "Windows":
            try:
                import comtypes
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
                from ctypes import cast, POINTER
            except Exception:
                return False
            dev = AudioUtilities.GetSpeakers()
            iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            vol = cast(iface, POINTER(IAudioEndpointVolume))
            cur = vol.GetMasterVolumeLevelScalar()
            vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, cur + delta_pct / 100.0)), None)
        else:
            import shutil
            if shutil.which('amixer'):
                subprocess.run(['amixer', '-q', 'set', 'Master', f'{delta_pct}%+'], timeout=3)
            elif shutil.which('pactl'):
                subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{delta_pct}%+'], timeout=3)
            else:
                return False
        return True
    except Exception:
        return False


def mute_volume() -> bool:
    """跨平台静音。"""
    _sys = platform.system()
    try:
        if _sys == "Darwin":
            subprocess.run(['osascript', '-e', 'set volume output muted true'], timeout=3)
        elif _sys == "Windows":
            import comtypes
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from ctypes import cast, POINTER
            dev = AudioUtilities.GetSpeakers()
            iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            vol = cast(iface, POINTER(IAudioEndpointVolume))
            vol.SetMute(1, None)
        else:
            import shutil
            if shutil.which('amixer'):
                subprocess.run(['amixer', '-q', 'set', 'Master', 'mute'], timeout=3)
            elif shutil.which('pactl'):
                subprocess.run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', 'toggle'], timeout=3)
            else:
                return False
        return True
    except Exception:
        return False


def sleep_display() -> bool:
    """跨平台让显示器进入睡眠。"""
    _sys = platform.system()
    try:
        if _sys == "Darwin":
            subprocess.run(['pmset', 'displaysleepnow'], timeout=3)
        elif _sys == "Windows":
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            '(Add-Type "[DllImport(\"user32.dll\")]public static extern int SendMessage(int h,int m,int w,int l);" -Name Win -Namespace Win); [Win]::SendMessage(-1,0x0112,0xF170,2) | Out-Null'],
                           timeout=5)
        else:
            subprocess.run(['xdg-screensaver', 'activate'], timeout=3)
        return True
    except Exception:
        return False


def handle_smart_command(text: str) -> str | None:
    """智能语音快捷命令: 不走 brain, 直接执行系统操作
    支持: 停止/暂停、音量控制、静音、睡眠模式
    返回: 回复文本(已处理) 或 None(不匹配, 交给 brain)
    """
    text_lower = text.strip().lower()
    if any(kw in text for kw in ['音量大', '大声点', '大一点', 'volume up', '音量加']):
        return '音量已调大。' if set_volume(20) else '系统音量控制不可用，请手动调节。'
    if any(kw in text for kw in ['音量小', '小声点', '小一点', 'volume down', '音量减']):
        return '音量已调小。' if set_volume(-20) else '系统音量控制不可用，请手动调节。'
    if any(kw in text for kw in ['静音', 'mute', '消音']):
        return '已静音。' if mute_volume() else '系统音量控制不可用，请手动静音。'
    if text_lower in ('停止', '暂停', '停', 'stop', 'pause', '闭嘴'):
        return '好的，我停。'
    if any(kw in text for kw in ['睡眠', '休眠', 'sleep', '显示器关闭']):
        return '已进入睡眠模式。' if sleep_display() else '显示器控制不可用。'
    return None
