"""Charlie - 应用模块包"""
import importlib.util as _iu
import os as _os
import time as _time

# 模块缓存：{name: (module, mtime)} — 避免每 120s 重载导致冷却状态被重置
_magic_module_cache: dict = {}


def load_magic_module(name: str, filename: str = None):
    """加载 magic-*.py 模块（统一路径策略，#3 修复的跨文件版本）

    带文件 mtime 缓存：文件未修改时返回缓存的模块对象，
    避免决策循环每 120s 重载 magic-decisions 导致模块级冷却状态（_last_sedentary_alert 等）被重置。

    Args:
        name: 逻辑模块名（如 "magic_scenes"）
        filename: 文件名（如 "magic-scenes.py"）；默认 name + ".py"
    """
    fname = filename or (name.replace("_", "-") + ".py")
    # 尝试两种路径：PROJECT_DIR（voice_agent 传 cwd）和上级 charlie/ 目录
    candidates = [
        _os.path.join(_os.getcwd(), fname),
        _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), fname),
    ]
    for path in candidates:
        if not _os.path.exists(path):
            continue
        mtime = _os.path.getmtime(path)
        cached = _magic_module_cache.get(name)
        if cached and cached[1] == mtime:
            return cached[0]
        _spec = _iu.spec_from_file_location(name, path)
        if _spec and _spec.loader:
            _mod = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _magic_module_cache[name] = (_mod, mtime)
            return _mod
    return None

