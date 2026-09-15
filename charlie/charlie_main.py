"""
Charlie 语音助手 — PyInstaller 入口点

用法:
    charlie                  # 启动服务 (默认)
    charlie --mcp <name>     # 以 MCP 子进程模式运行 (被主进程启动)

说明:
    PyInstaller 打包后，所有 Python 源码被冻结为单个可执行文件。
    MCP 子进程通过 subprocess 启动同一个可执行文件 + --mcp 参数，
    而非启动 Python 解释器 + .py 文件。
"""
import sys
import os
import shutil
import logging

log = logging.getLogger("magic")


# Windows 下 GUI 程序派生子进程(MCP/ffmpeg/视觉脚本)会弹 cmd 黑框一闪而过。
# 全局给 subprocess.Popen/Run 注入 CREATE_NO_WINDOW 标志，静默子进程。
# 必须在 qwen_agent/MCP 任何子进程启动前完成（模块导入即生效）。
if os.name == "nt":
    import subprocess as _sp
    _CREATE_NO_WINDOW = 0x08000000
    _OrigPopen = _sp.Popen

    class _NoWindowPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            cf = kwargs.get("creationflags", 0)
            if not (cf & _CREATE_NO_WINDOW):
                kwargs["creationflags"] = cf | _CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    _sp.Popen = _NoWindowPopen
    # 同时 patch asyncio 的 subprocess 创建器（mcp SDK 走 asyncio.create_subprocess_exec）
    try:
        import asyncio as _aio
        _orig_csp = _aio.create_subprocess_exec

        async def _csp_no_window(*args, **kwargs):
            if "creationflags" not in kwargs:
                # asyncio 在 Windows 把 creationflags 传给 CreateProcess；注入 CREATE_NO_WINDOW
                # 注意:ProactorEventLoop 下 async subprocess 支持该参数
                kwargs["creationflags"] = _CREATE_NO_WINDOW
            return await _orig_csp(*args, **kwargs)

        _aio.create_subprocess_exec = _csp_no_window
    except Exception as e:
        log.debug(f"[charlie_main] error at line 50: {e}")
        pass


def _ensure_first_run(base_dir: str) -> str:
    """首次启动检测：缺 .env 则从 .env.example 复制。

    返回首启目标路径（首次="/welcome"，已配置="/"）。
    """
    env_path = os.path.join(base_dir, ".env")
    first_run = not os.path.exists(env_path)
    if first_run:
        # 从 .env.example 复制空白模板
        example = os.path.join(base_dir, ".env.example")
        if os.path.exists(example):
            shutil.copy(example, env_path)
        else:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("# Charlie 语音助手配置\n# 请在应用内 /welcome 引导页配置\n")
    return "/welcome" if first_run else "/"


def _run_preflight():
    """运行外部二进制依赖检测（缺失只 warning）"""
    try:
        from app.preflight import run_preflight
        run_preflight()
    except Exception as e:
        log.warning(f"[preflight] 检测跳过: {e}")


def main():
    _first_path = "/"  # 默认进首页；frozen 模式首启会被覆盖为 /welcome
    # 确保工作目录是可执行文件所在目录
    if getattr(sys, 'frozen', False):
        _base = os.path.dirname(sys.executable)
        os.chdir(_base)
        # windowed 模式(console=False)下 sys.stdout/stderr 为 None，uvicorn StreamHandler
        # 会静默丢日志甚至触发 handleError 噪声。重定向到 devnull 兜底（文件 handler 仍落盘）。
        if not sys.stdout or not getattr(sys.stdout, 'write', None):
            sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='ignore')
        if not sys.stderr or not getattr(sys.stderr, 'write', None):
            sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='ignore')
        # 【关键】frozen 下把数据/日志目录重定向到 exe 同级可写目录，
        # 避免 PROJECT_DIR=dirname(__file__)=_internal/(只读 bundle) 导致
        # reminders.json/conversation_history.json 写入 bundle 目录，
        # 装到 Program Files 时还会因 os.makedirs 只读而启动崩溃。
        _data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR") or os.path.join(_base, "data")
        _log_dir = os.environ.get("ASSISTANT_KID_LOG_DIR") or os.path.join(_base, "logs")
        os.makedirs(_data_dir, exist_ok=True)
        os.makedirs(_log_dir, exist_ok=True)
        os.environ.setdefault("ASSISTANT_KID_DATA_DIR", _data_dir)
        os.environ.setdefault("ASSISTANT_KID_LOG_DIR", _log_dir)
        # 将 bin/ 目录加入 PATH (ffmpeg) — PyInstaller 可能放在 _base/bin 或 _base/_internal/bin
        for _bin_dir in [os.path.join(_base, 'bin'),
                         os.path.join(_base, '_internal', 'bin')]:
            if os.path.isdir(_bin_dir):
                os.environ['PATH'] = _bin_dir + os.pathsep + os.environ.get('PATH', '')
        # 创建运行时必需的空文件（落到 DATA_DIR，与 voice_server/voice_agent 读取路径一致）
        for f in ['conversation_history.json']:
            p = os.path.join(_data_dir, f)
            if not os.path.exists(p):
                try:
                    with open(p, 'w', encoding='utf-8') as fh:
                        fh.write('[]')
                except OSError:
                    pass  # 只读兜底，运行期若仍不可写会在写入时报错
        # T9: 首次启动检测 + preflight
        _first_path = _ensure_first_run(_base)
        _run_preflight()

    # MCP 子进程模式: 启动指定的 MCP server
    if len(sys.argv) >= 3 and sys.argv[1] == '--mcp':
        mcp_name = sys.argv[2]
        _run_mcp_server(mcp_name)
        return

    # 主服务模式: 启动 voice_server + 原生窗口
    _run_server(_first_path)

def _run_mcp_server(name: str):
    """启动 MCP 子进程 (magic-* / baize-skills / ...)"""
    import importlib.util, os, sys
    # 逻辑名 → 源码文件名 (带连字符的模块名 Python 无法直接 import)
    mcp_files = {
        "magic-info": "magic-info.py",
        "magic-music": "magic-music.py",
        "magic-reminder": "magic-reminder.py",
        "magic-notes": "magic-notes.py",
        "magic-system": "magic-system.py",
        "magic-life": "magic-life.py",
        "magic-scenes": "magic-scenes.py",
        "magic-evolution": "magic-evolution.py",
        "magic-summary": "magic-summary.py",
        "magic-wardrobe": "magic-wardrobe.py",
        "magic-browser": "magic-browser.py",
        "baize-skills": "baize_skills_mcp.py",
        "filesystem": "magic-notes.py",  # 使用备忘录工具（文件读写）
    }
    filename = mcp_files.get(name)
    if not filename:
        log.error(f"未知 MCP: {name}")
        sys.exit(1)
    # 按文件路径加载 (PyInstaller frozen 模式下 __file__ 在 _MEIPASS)
    if getattr(sys, 'frozen', False):
        _base = os.path.join(os.path.dirname(sys.executable), '_internal')
    else:
        _base = os.getcwd()
    filepath = os.path.join(_base, filename)
    if not os.path.exists(filepath):
        # 尝试直接从源码目录
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(filepath):
        log.error(f"MCP {name} 文件不存在: {filepath}")
        sys.exit(1)
    try:
        mod_name = name.replace('-', '_') + '_mcp'
        spec = importlib.util.spec_from_file_location(mod_name, filepath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.mcp.run()
    except Exception as e:
        log.error(f"MCP {name} 启动失败: {e}")
        sys.exit(1)

def _run_server(first_path: str = "/"):
    """启动 voice_server (FastAPI+Uvicorn, 后台线程) + 原生桌面窗口 (pywebview/WebView2)

    双击 charlie.exe → 弹原生窗口内嵌 Web UI，不再开浏览器、不弹控制台。
    窗口关闭即退出。
    """
    import threading, time, socket
    import uvicorn
    from voice_server import app
    from app.config import http_port
    port = http_port()
    # 后台线程跑 Uvicorn（host 0.0.0.0 保留局域网/ESP32 接入能力）
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    _srv_thread = threading.Thread(target=server.run, daemon=True)
    _srv_thread.start()
    # 等端口就绪（最多 ~10s）；超时则报错退出，避免带死 URL 进窗口
    _ready = False
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                _ready = True
                break
        except OSError:
            time.sleep(0.2)
    if not _ready:
        log.error(f"[gui] voice_server 未在 10s 内就绪(端口 {port})，退出")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Charlie 服务启动失败（端口 {port} 被占或依赖缺失），详情见 logs/app.log", "Charlie 启动失败", 0x10)
        except Exception as e:
            log.debug(f"[charlie_main] error at line 206: {e}")
            pass
        sys.exit(1)
    url = f"http://127.0.0.1:{port}{first_path}"
    log.info(f"[gui] 启动原生窗口: {url}")
    try:
        import webview
        webview.create_window("Charlie 语音助手", url,
                              width=440, height=760,
                              min_size=(360, 600),
                              text_select=False)
        webview.start()  # 阻塞主线程，窗口关闭后返回
    except Exception as e:
        # 退路：pywebview/WebView2 不可用时回退到系统浏览器
        log.warning(f"[gui] pywebview 不可用({e})，回退浏览器")
        import webbrowser
        webbrowser.open(url)
        try:
            _srv_thread.join()
        except (KeyboardInterrupt, SystemExit):
            pass
        return
    # 优雅关闭：通知 uvicorn 退出，等调度器收尾
    server.should_exit = True
    _srv_thread.join(timeout=3)
    _cleanup_mcp_subprocesses()


def _cleanup_mcp_subprocesses():
    """窗口关闭时清理 qwen_agent 启动的 stdio MCP 子进程（防孤儿 charlie.exe --mcp）。"""
    try:
        import psutil
        me = psutil.Process()
        for child in me.children(recursive=True):
            try:
                # 只杀本程序自己 --mcp 派生的同名子进程，避免误杀无关进程
                if child.name() and child.name().lower().startswith('charlie'):
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except Exception:
                        child.kill()
            except Exception as e:
                log.debug(f"[charlie_main] error at line 248: {e}")
                pass
    except Exception as e:
        log.debug(f"[charlie_main] error at line 250: {e}")
        pass

if __name__ == "__main__":
    main()