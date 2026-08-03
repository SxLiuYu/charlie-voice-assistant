"""magic-system: 系统控制 (3个工具: 音量/语速/状态)"""
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("magic-system")


@mcp.tool()
def set_volume(level: int = -1) -> str:
    """控制系统音量。level=0-100(百分比), 不传或-1=当前音量

    例: set_volume(50) → 音量调到50%
        set_volume(0) → 静音
        set_volume(100) → 最大音量
    """
    import subprocess
    try:
        if level < 0:
            r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                             capture_output=True, text=True, timeout=5)
            return f"当前音量：{r.stdout.strip()}%"
        level = max(0, min(100, level))
        subprocess.run(["osascript", "-e", f"set volume output volume {level}"], timeout=5)
        return f"音量已调到{level}%"
    except Exception as e:
        return f"音量控制失败：{e}"


@mcp.tool()
def set_speech_speed(speed: str = "normal") -> str:
    """控制 Charlie 说话语速。speed: slow=慢, normal=正常, fast=快, 或直接传数字(0.5-2.0)

    例: set_speech_speed("slow") → 慢速说话
        set_speech_speed("1.0") → 正常语速
        set_speech_speed("0.8") → 稍慢
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
    """查询当前设备/服务器状态：CPU/内存/磁盘/运行时间"""
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
