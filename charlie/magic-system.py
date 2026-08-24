"""magic-system: 系统控制 (3个工具: 音量/语速/状态)"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-system",
    "tier": "core",
    "required_env": [],
    "label": "系统控制(音量/语速/状态)"
}

from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")
mcp = FastMCP("magic-system")


@mcp.tool()
def set_volume(level: int = -1) -> str:
    log.info(f"[system] set_volume(level={level})")
    """控制系统音量。level=0-100(百分比), 不传或-1=当前音量

    例: set_volume(50) → 音量调到50%
        set_volume(0) → 静音
        set_volume(100) → 最大音量
    """
    import subprocess, platform
    try:
        _sys = platform.system()
        if level < 0:
            if _sys == "Darwin":
                r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                                 capture_output=True, text=True, timeout=5)
                return f"当前音量：{r.stdout.strip()}%"
            return "音量查询仅在macOS原生环境可用"
        level = max(0, min(100, level))
        if _sys == "Darwin":
            subprocess.run(["osascript", "-e", f"set volume output volume {level}"], timeout=5)
            return f"音量已调到{level}%"
        elif _sys == "Windows":
            try:
                import comtypes  # type: ignore
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
                from ctypes import cast, POINTER
                dev = AudioUtilities.GetSpeakers()
                iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
                vol = cast(iface, POINTER(IAudioEndpointVolume))
                vol.SetMasterVolumeLevelScalar(level / 100.0, None)
                if level == 0:
                    vol.SetMute(1, None)
                else:
                    vol.SetMute(0, None)
                return f"音量已调到{level}%"
            except Exception:
                return "Windows 音量控制需安装 pycaw（pip install pycaw comtypes）"
        else:  # Linux
            import shutil
            if shutil.which('amixer'):
                subprocess.run(['amixer', '-q', 'set', 'Master', f'{level}%'], timeout=5)
                return f"音量已调到{level}%"
            elif shutil.which('pactl'):
                subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{level}%'], timeout=5)
                return f"音量已调到{level}%"
            return "音量控制失败：未找到 amixer/pactl"
    except Exception as e:
        return f"音量控制失败：{e}"


@mcp.tool()
def set_speech_speed(speed: str = "normal") -> str:
    log.debug("[set_speech_speed] 被调用")
    """控制 Charlie 说话语速。speed: slow=慢, normal=正常, fast=快, 或直接传数字(0.5-2.0)

    例: set_speech_speed("slow") → 慢速说话
        set_speech_speed("1.0") → 正常语速
        set_speech_speed("0.8") → 稍慢
        set_speech_speed("快") → 快速说话
    """
    import voice_agent
    speed_map = {"slow": 0.8, "normal": 1.0, "fast": 1.2, "慢": 0.8, "正常": 1.0, "快": 1.2}
    try:
        if speed in speed_map:
            val = speed_map[speed]
        else:
            val = float(speed)
            val = max(0.5, min(2.0, val))
        voice_agent._tts_speed = val
        desc = "慢" if val < 1.0 else ("快" if val > 1.0 else "正常")
        return f"语速已调到{desc}({val:g}x)"
    except Exception as e:
        return f"语速控制失败：{e}"


@mcp.tool()
def system_status() -> str:
    log.debug("[system_status] 被调用")
    """查询当前设备/服务器状态：CPU/内存/磁盘/运行时间

    例: system_status() → 返回设备名称、CPU使用率、内存、磁盘、运行时间
    """
    import socket, platform, time as _t
    try:
        import psutil
        psutil.cpu_percent(interval=None)
    except ImportError:
        psutil = None
    _psutil = psutil if psutil is not None else __import__("psutil")
    vm = _psutil.virtual_memory()
    cpu_pct = _psutil.cpu_percent(interval=None)
    disk = _psutil.disk_usage('/')
    boot = _psutil.boot_time()
    up = int(_t.time() - boot)
    return (f"设备:{socket.gethostname()} | {platform.system()} {platform.release()}\n"
        f"CPU使用率:{cpu_pct}% | {_psutil.cpu_count()}核 | 内存:{(vm.total-vm.available)//1073741824:.1f}/{vm.total//1073741824:.1f}GB({vm.percent}%)\n"
        f"磁盘:{disk.used//1073741824:.0f}/{disk.total//1073741824:.0f}GB({disk.percent}%) | 运行:{up//86400}天{up%86400//3600}时{up%3600//60}分")


if __name__ == "__main__":
    mcp.run()
