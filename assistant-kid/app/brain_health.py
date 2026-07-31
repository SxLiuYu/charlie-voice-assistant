"""大脑健康与预热: 就绪检测 / 健康状态 / 后台预热

依赖 voice_agent(延迟import避免循环); 预热在子线程跑独立event loop, 首请求省~9秒.
"""
import logging, asyncio, threading
log = logging.getLogger("magic")

def _brain_is_warm():
    """检查大脑是否已预热"""
    try:
        from voice_agent import _brain
        return _brain is not None
    except Exception:
        return False

def _get_brain_health():
    """获取大脑健康状态(失败计数/上次成功/上次失败)"""
    try:
        from voice_agent import brain_status
        return brain_status()
    except Exception:
        return {"ready": False, "error": "无法获取"}

def _warmup_brain():
    """后台预启动大脑+6MCP，首请求省~9秒"""
    def _w():
        log.info("[warmup] 预启动大脑+6MCP...")
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from voice_agent import _build_brain
            import voice_agent
            voice_agent._brain = _build_brain()
            for rsp in voice_agent._brain.run([{'role': 'user', 'content': '你好'}]):
                pass
            log.info("[warmup] 大脑+6MCP预启动完成，首请求将更快")
        except Exception as e:
            log.warning(f"[warmup] 预热失败(不影响使用): {e}")
    threading.Thread(target=_w, daemon=True).start()
