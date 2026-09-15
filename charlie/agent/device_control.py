"""Device control fast paths: AC (Tuya IR), volume, mute, display sleep.

Extracted from voice_agent.py. Handles smart home and system control
keywords, bypassing LLM for sub-second response.
"""
import atexit
import os
import sys
import re
import platform
import subprocess
import logging
import concurrent.futures
import threading
from typing import Optional

log = logging.getLogger("magic")

# AC 共享线程池（惰性单例，atexit 自动清理）
_ac_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_ac_pool_lock = threading.Lock()
_ac_pool_atexit_registered = False


def _get_ac_pool() -> concurrent.futures.ThreadPoolExecutor:
    """获取 AC 共享线程池（惰性初始化，双检锁）。"""
    global _ac_pool, _ac_pool_atexit_registered
    if _ac_pool is None:
        with _ac_pool_lock:
            if _ac_pool is None:
                max_workers = int(os.getenv("AC_POOL_SIZE", "2"))
                _ac_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="ac-tuya",
                )
                if not _ac_pool_atexit_registered:
                    atexit.register(_shutdown_ac_pool)
                    _ac_pool_atexit_registered = True
    return _ac_pool


def _shutdown_ac_pool():
    """atexit 回调：关闭共享池。"""
    global _ac_pool
    pool = _ac_pool
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            log.debug(f"[ac] 共享池关闭失败: {e}")
        finally:
            _ac_pool = None


def _run_ac_with_timeout(fn, timeout: float = 8.0, timeout_msg: str = "空调指令发送超时，请稍后重试"):
    """Run an AC API call in the shared thread pool with a hard timeout budget.

    已知限制：超时返回后 worker 线程至底层 HTTP 自然结束才释放；AC 调用低频，
    池大小可调（AC_POOL_SIZE 环境变量）兜底。
    """
    executor = _get_ac_pool()
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        log.warning("[ac] API 调用超时(%.1fs)，返回超时文案", timeout)
        return False, timeout_msg


def direct_ac_control(text: str) -> str:
    """空调控制关键词命中时直接调用 Tuya 2B 红外云 API,绕过 LLM+MCP(2-4s→~0.2s)。
    解析: 开/关 + 模式(制冷/制热/除湿/送风/自动) + 温度(X度) + 风速(高/中/低)。
    返回自然语言回复,无明确指令或调用失败时返回空串(回退 brain)。

    AC_DRY_RUN=1 时模拟执行（不调真实设备），用于测试。
    """
    import time as _t
    _ac_start = _t.time()
    _dry_run = os.getenv("AC_DRY_RUN", "0") == "1"
    try:
        from app.audit_log import audit_log
        audit_log("ac_control", input_data=text, action="voice_command",
                  session_id="voice", source="user")
    except Exception as e:
        log.debug(f"[agent.device_control] error at line 25: {e}")
        pass
    if _dry_run:
        log.warning(f"[ac] 测试模式(AC_DRY_RUN=1)，模拟执行: {text[:30]}")
    try:
        from tuya_api import TuyaCloudAPI
    except Exception as e:
        log.warning(f"[ac] tuya_api 导入失败: {e}")
        try:
            from app.audit_log import audit_log
            audit_log("ac_control", input_data=text, success=False, error=str(e),
                      action="import_failed", session_id="voice", source="user",
                      duration_ms=(_t.time()-_ac_start)*1000)
        except Exception as e:
            log.debug(f"[agent.device_control] error at line 38: {e}")
            pass
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

    # 否定守卫："不要开空调"/"别开空调" 会被下面的子串匹配误判成开机指令，
    # 真实操作电器前必须放弃快路径，交给 brain 结合上下文判断
    if re.search(r"不要|不用|不想|无需|不必|别开|别关|别再", t):
        log.info(f"[ac] 否定语句跳过快路径: {t[:30]}")
        return ""

    # 关机判断：只匹配明确的祈使指令（"关空调"/"关闭空调"/"把空调关掉"等），
    # 不匹配疑问句/质问句（"你是不是把空调关了"/"空调是不是关了"等）。
    # 历史误触发案例：ASR识别出"你是不是把客厅空调给关了"→ 旧逻辑把空调关了。
    _is_question = any(k in t for k in ("是不是", "了吗", "了没", "有没有", "刚才"))
    _power_off_cmd = bool(re.search(
        r"^(?:帮我把?)?(?:关(?:闭|掉|上)?)(?:一下)?\s*(?:客厅)?(?:空调|电扇)"
        r"|(?:帮我把?)?(?:客厅)?(?:空调|电扇)\s*关(?:闭|掉|上)?"
        r"|关了空调$"
        r"|把空调关了"
        r"|把空调关掉"
        r"|空调关掉"
        r"|空调关了吧",
        t))
    if _power_off_cmd and not _is_question:
        # 陈述句防误触："我不小心把空调关了" 是陈述不是指令。
        # 祈使特征：带 帮/请/麻烦，或非第一人称开头（"我要关空调"不匹配关机模式）
        if re.match(r"^\s*我", t) and not any(k in t for k in ("帮", "请", "麻烦")) \
                and not any(k in t for k in ("我要", "我想")):
            log.info(f"[ac] 陈述句跳过关机快路径: {t[:30]}")
            return ""
        try:
            if _dry_run:
                log.warning(f"[ac] 测试模式: 模拟关机 → power=0")
                return "好的，空调已关闭（测试模式）。"
            log.info(f"[ac] 查询当前状态: infrared={infrared_id[:8]}... remote={remote_id[:8]}...")
            cur = None
            try:
                cur = api.ac_status(infrared_id, remote_id)
                log.info(f"[ac] 操作前状态: power={cur.get('power')} mode={cur.get('mode')} temp={cur.get('temp')}°C wind={cur.get('wind')}")
            except Exception as e:
                log.warning(f"[ac] 状态查询失败，仍尝试关机: {e}")
            _timeout_msg = "空调指令发送超时，请稍后重试"

            def _do_power_off():
                return api.ac_scenes_command(infrared_id, remote_id, power=0)

            _ok = _run_ac_with_timeout(_do_power_off, timeout=8.0, timeout_msg=_timeout_msg)
            if isinstance(_ok, tuple):
                _ok = _ok[0]
            try:
                from app.audit_log import audit_log
                audit_log("ac_control", input_data=t,
                          output_data="power=0" if _ok else "power=0 FAILED",
                          success=_ok, action="power_off", session_id="voice",
                          source="user",
                          duration_ms=(_t.time()-_ac_start)*1000)
            except Exception as e:
                log.debug(f"[agent.device_control] error at line 83: {e}")
                pass
            if not _ok:
                # 部分命令失败时如实反馈，不能播报假成功让用户以为已关闭
                log.warning(f"[ac] 关机指令未全部生效: ASR={t[:20]}")
                return "空调指令可能没生效，请稍后再说一次。"
            log.info(f"[ac] 直连关机: ASR={t[:20]} → power=0")
            return "好的，空调已关闭。"
        except Exception as e:
            log.warning(f"[ac] 关机失败: {e}")
            return "空调指令发送失败，请稍后重试。"

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
    m = re.search(r"(\d{1,3})\s*(?:度|℃|°C|摄氏度)", t)
    if not m:
        m = re.search(r"(?:调到|设成|调至|开?\s*到)\s*(\d{1,3})", t)
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
            if _dry_run:
                mode_str = {0: "制冷", 1: "制热", 2: "自动", 3: "送风", 4: "除湿"}.get(mode, "")
                fan_str = {1: "低风", 2: "中风", 3: "高风"}.get(fan, "")
                reply = f"空调已打开，{mode_str}，{eff_temp}度{f'，{fan_str}' if fan_str else ''}（测试模式）。"
                log.warning(f"[ac] 测试模式: 模拟开机 → {reply}")
                return reply
            log.info(f"[ac] 查询当前状态: infrared={infrared_id[:8]}... remote={remote_id[:8]}...")
            cur = api.ac_status(infrared_id, remote_id)
            log.info(f"[ac] 操作前状态: power={cur.get('power')} mode={cur.get('mode')} temp={cur.get('temp')}°C wind={cur.get('wind')}")
            log.info(f"[ac] 发送指令: power=1 mode={mode} temp={eff_temp} wind={fan} (ASR={t[:30]})")
            _timeout_msg = "空调指令发送超时，请稍后重试"

            def _do_power_on():
                return api.ac_scenes_command(infrared_id, remote_id, power=1, mode=mode, temp=eff_temp, wind=fan)

            _ok = _run_ac_with_timeout(_do_power_on, timeout=8.0, timeout_msg=_timeout_msg)
            if isinstance(_ok, tuple):
                _ok = _ok[0]
            try:
                from app.audit_log import audit_log
                audit_log("ac_control", input_data=t,
                          output_data=f"power=1 mode={mode} temp={eff_temp} wind={fan}" + ("" if _ok else " FAILED"),
                          success=_ok, action="power_on", session_id="voice",
                          source="user",
                          duration_ms=(_t.time()-_ac_start)*1000)
            except Exception as e:
                log.debug(f"[agent.device_control] error at line 152: {e}")
                pass
            if not _ok:
                # 设备可能处于半配置状态（如开机成功但温度未下发），如实反馈
                log.warning(f"[ac] 开机指令未全部生效: ASR={t[:30]}")
                return "空调指令可能没生效，请稍后再说一次。"
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
            return "空调指令发送失败，请稍后重试。"
    return ""


def set_volume(delta_pct: int) -> bool:
    """跨平台系统音量增减(delta_pct 正=调大/负=调小)。返回是否成功。"""
    delta_pct = max(-100, min(100, delta_pct))  # clamp 到 [-100,100]
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
