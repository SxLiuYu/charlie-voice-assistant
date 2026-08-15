# -*- mode: python ; coding: utf-8 -*-
"""
Charlie 语音助手 — PyInstaller spec 文件

构建命令:
    # macOS (当前机器)
    pyinstaller charlie.spec

    # Windows (需在 Windows 机器上运行)
    pyinstaller charlie.spec

输出:
    dist/charlie/            # 目录模式 (启动快, 体积小)
    dist/charlie/charlie     # 可执行文件 (macOS)
    dist/charlie/charlie.exe # 可执行文件 (Windows)
"""

import os
import sys
import platform
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# 收集所有隐式依赖的 Python 包
hidden_imports = [
    # MCP 子进程模块 — 注意: 源码文件名带连字符(magic-info.py), Python 无法直接 import,
    # 运行时用 importlib.util.spec_from_file_location 按路径加载, 这里只放能直接 import 的
    'baize_skills_mcp', 'mcp_ir_control',
    'mcp_common', 'app.audio', 'app.brain_health', 'app.state',
    'app.reminders', 'app.config', 'app.env_catalog', 'app.preflight',
    'app.cert', 'app.mcp_gate', 'utils',
    # 拆分后的新模块 (voice_server.py 重构)
    'app.http_helpers', 'app.notifications', 'app.cors', 'app.schedulers',
    'app.routes', 'app.routes.system', 'app.routes.conversation',
    'app.routes.reminders', 'app.routes.websocket', 'app.routes.manage',
    # qwen_agent 隐式依赖
    'qwen_agent', 'qwen_agent.agents', 'qwen_agent.tools',
    'qwen_agent.tools.mcp_manager',
    # mcp SDK
    'mcp', 'mcp.server.fastmcp', 'mcp.client.stdio',
    'mcp.client.sse', 'mcp.client.streamable_http',
    'soundfile',
    # ASR/TTS
    'requests', 'urllib3',
    # 音频处理
    'audioop',
    # Web框架
    'uvicorn', 'fastapi', 'starlette', 'sse_starlette',
    'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
    'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan', 'uvicorn.lifespan.on',
    # 其他
    'psutil', 'dotenv', 'tiktoken',
    'numpy',
    # 原生桌面窗口 (pywebview + WebView2)
    'webview', 'webview.platforms.edgechromium', 'webview.platforms.winforms',
    'clr', 'pythonnet',
    # 跨平台系统控制/截图(可选，缺失自动降级)
    'mss', 'comtypes', 'pycaw', 'pycaw.pycaw',
    'fcntl_compat',
    # voice_agent 拆分的快路径模块
    'agent.music', 'agent.weather', 'agent.vision', 'agent.device_control',
    'agent.llm_state', 'agent.llm',
]

# 收集数据文件 (前端HTML, 模板, 配置)
datas = [
    ('web', 'web'),                    # 前端静态文件 (voice/setup/welcome/esp32_setup)
    ('app', 'app'),                    # app 模块数据
    ('scripts', 'scripts'),            # gen-cert.sh, download-models.sh
    # 注: skills/ 下的 u2-asr-pro/u2-tts-pro 是 ClawHub 技能，Charlie 不加载，不打包
    ('.env.example', '.'),             # 配置模板
    ('fcntl_compat.py', '.'),          # Windows fcntl 垫片
    # ESP32 firmware (only include if available — not in git, may be added locally)
    *([('../firmware/charlie-esp32-flash-16MB.bin', 'firmware')] if os.path.isfile('../firmware/charlie-esp32-flash-16MB.bin') else []),
    # MCP 源码文件 (文件名带连字符, 无法作为模块 import, 需作为数据文件打包, 运行时按路径加载)
    ('magic-info.py', '.'),
    ('magic-music.py', '.'),
    ('magic-reminder.py', '.'),
    ('magic-notes.py', '.'),
    ('magic-system.py', '.'),
    ('magic-life.py', '.'),
    ('magic-scenes.py', '.'),
    ('magic-evolution.py', '.'),
    ('magic-summary.py', '.'),
    ('magic-wardrobe.py', '.'),
    ('magic-browser.py', '.'),
    ('magic-apps.py', '.'),
    ('magic-feishu.py', '.'),
    ('magic-douyin.py', '.'),
    ('magic-taobao.py', '.'),
    ('magic-recipe.py', '.'),
    ('magic-decisions.py', '.'),       # 自主决策引擎(原遗漏)
    ('baize_skills_mcp.py', '.'),
    ('mcp_ir_control.py', '.'),
]

# 收集隐式依赖的包数据
for pkg in ['qwen_agent', 'mcp', 'fastapi', 'starlette', 'uvicorn', 'sse_starlette', 'webview', 'pythonnet',
            # esptool：应用内 ESP32 烧录向导需要（含 reedsolo/pyserial/bitstring 等子依赖）
            'esptool', 'reedsolo', 'serial', 'bitstring', 'intelhex']:
    pkg_data = collect_data_files(pkg)
    datas.extend(pkg_data)
    hidden_imports.extend(collect_submodules(pkg))

# 排除 __pycache__ / .pyc，避免把已删除模块的陈旧字节码（如 nvs_patch）打进包
def _is_cache(src, dest):
    return '__pycache__' in src.replace('\\', '/').split('/') or src.endswith(('.pyc', '.pyo'))
datas = [(s, d) for (s, d) in datas if not _is_cache(s, d)]

# ffmpeg 二进制文件 (从系统中查找)
def _find_ffmpeg():
    """查找系统 ffmpeg 路径"""
    import shutil
    path = shutil.which('ffmpeg')
    if path:
        return path
    # macOS 常见路径 (homebrew Intel 和 M系列, 以及系统安装)
    for p in ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg',
              '/usr/bin/ffmpeg', '/opt/local/bin/ffmpeg']:
        if os.path.isfile(p):
            return p
    # Windows 常见路径
    if platform.system() == 'Windows':
        for p in [r'C:\ffmpeg\bin\ffmpeg.exe', r'C:\Program Files\ffmpeg\bin\ffmpeg.exe']:
            if os.path.isfile(p):
                return p
    return None

def _find_ncm():
    """查找系统的 ncm-cli 路径"""
    import shutil
    path = shutil.which('ncm')
    if path:
        return path
    # 常见路径
    for p in [os.path.expanduser('~/.local/bin/ncm'), '/usr/local/bin/ncm']:
        if os.path.isfile(p):
            return p
    return None

ffmpeg_path = _find_ffmpeg()
if ffmpeg_path:
    datas.append((ffmpeg_path, 'bin'))
    print(f"[spec] ffmpeg found: {ffmpeg_path}")
else:
    print("[spec] WARNING: ffmpeg not found, audio transcoding unavailable!")
    print("[spec]   macOS: brew install ffmpeg")
    print("[spec]   Windows: choco install ffmpeg or manual download")

ncm_path = _find_ncm()
if ncm_path:
    datas.append((ncm_path, 'bin'))
    print(f"[spec] ncm found: {ncm_path}")
else:
    print("[spec] WARNING: ncm-cli not found, music playback unavailable!")
    print("[spec]   macOS: pip install ncm-cli")
    print("[spec]   Windows: pip install ncm-cli")

# opus DLL (xiaozhi_codec/opuslib needs native libopus)
def _find_opus():
    """查找系统 opus 动态库完整路径"""
    import ctypes.util
    # Windows: find_library('opus') looks for opus.dll (returns full path on Win)
    if platform.system() == 'Windows':
        loc = ctypes.util.find_library('opus')
        if loc and os.path.isfile(loc):
            return loc
        for p in [os.path.join(os.getcwd(), '_internal', 'opus.dll'),
                  os.path.join(os.getcwd(), 'opus.dll'),
                  os.path.join(os.getcwd(), '_internal', 'libopus-0.dll')]:
            if os.path.isfile(p):
                return p
    # macOS common paths
    if platform.system() == 'Darwin':
        for p in ['/opt/homebrew/lib/libopus.dylib', '/usr/local/lib/libopus.dylib',
                  '/usr/lib/libopus.dylib', '/opt/local/lib/libopus.dylib']:
            if os.path.isfile(p):
                return p
    # Linux: try ldconfig for full path, then common paths
    if platform.system() == 'Linux':
        try:
            import subprocess
            r = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True, timeout=5)
            for line in r.stdout.split('\n'):
                if 'libopus.so' in line:
                    parts = line.strip().split('->')
                    if len(parts) == 2:
                        p = parts[1].strip()
                        if os.path.isfile(p):
                            return p
        except Exception:
            pass
        for p in ['/usr/lib/x86_64-linux-gnu/libopus.so.0', '/usr/lib/libopus.so.0',
                  '/usr/lib64/libopus.so.0', '/usr/lib/aarch64-linux-gnu/libopus.so.0']:
            if os.path.isfile(p):
                return p
    return None

opus_path = _find_opus()
if opus_path:
    print(f"[spec] opus found: {opus_path}")
else:
    print("[spec] WARNING: opus library not found, xiaozhi codec will be unavailable!")
    print("[spec]   macOS: brew install opus")
    print("[spec]   Linux: apt install libopus0")
    print("[spec]   Windows: download opus.dll to _internal/")

binaries = []
if ffmpeg_path:
    binaries.append((ffmpeg_path, 'bin'))
if ncm_path:
    binaries.append((ncm_path, 'bin'))
if opus_path:
    binaries.append((opus_path, '.'))
binaries = [(s, d) for (s, d) in binaries if not _is_cache(s, d)]

a = Analysis(
    ['charlie_main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'pandas',
        'scipy', 'cv2', 'torch', 'tensorflow',
        'tests', 'pytest', 'unittest', 'IPython',
        'jupyter', 'notebook', 'ipykernel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='charlie',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['*.pyd', 'pythonnet*', '*pythonnet*', '*WebView2*', '*clr*',
                 'pycaw*', '*pycaw*', 'comtypes*', '*comtypes*'],  # 压这些会崩
    console=False,  # 原生桌面窗口模式（不弹控制台黑框）
    disable_windowed_traceback=False,
    target_architecture=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='charlie.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['*.pyd', 'pythonnet*', '*WebView2*', 'pycaw*', 'comtypes*'],
    name='charlie',
)
