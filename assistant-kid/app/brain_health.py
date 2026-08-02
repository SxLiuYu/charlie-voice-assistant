"""大脑健康与预热: 就绪检测 / 健康状态 / 后台预热

依赖 voice_agent(延迟import避免循环); 预热在子线程跑独立event loop, 首请求省~9秒.
"""
import logging, asyncio, threading
log = logging.getLogger("magic")

def _brain_is_warm():
    """检查大脑是否已预热(任意缓存大脑就绪即可)"""
    try:
        from voice_agent import _brains
        return len(_brains) > 0
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
    """后台预启动 none 大脑(0 MCP, 日常对话省~3秒), 首请求直接命中缓存.
    同时预热 Ollama 意图分类模型(qwen3.5:2b), 省冷启动~3秒.
    同时预缓存 ASR 确认音("收到，我在听"), ASR完成后0ms推送."""
    def _w():
        log.info("[warmup] 预启动 none 大脑(0 MCP)...")
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from voice_agent import _get_brain, _classify_intent, tts_to_mp3, _tts_cache_set
            # 0. 预缓存 ASR 过渡语 + 常见短回复(避免每次走 Finna 700ms 往返)
            _WARMUP_PHRASES = [
                "嗯，让我想想",      # 过渡语 (brain 首 token 窗口填充)
                "好的",              # 常见确认
                "好的，马上",        # 常见确认
                "好的，马上办",      # 常见确认
                "收到",              # 常见确认
                "明白",              # 常见确认
                "嗯",                # 常见语气
                "哦",                # 常见语气
                "了解",              # 常见确认
            ]
            cached_count = 0
            for phrase in _WARMUP_PHRASES:
                try:
                    audio = tts_to_mp3(phrase)
                    if audio and len(audio) > 100:
                        _tts_cache_set(phrase, audio, cleaned=True)
                        cached_count += 1
                    else:
                        log.warning(f"[warmup] 短回复TTS返回空或过小: '{phrase}' ({len(audio) if audio else 0}B)")
                except Exception as e:
                    log.warning(f"[warmup] 预缓存'{phrase}'失败(不影响使用): {e}")
            log.info(f"[warmup] 短回复预缓存完成: {cached_count}/{len(_WARMUP_PHRASES)}")
            # 1. 预热 Ollama 意图分类(冷启动~3s → 热启动~300ms)
            try:
                _classify_intent("你好")
                log.info("[warmup] Ollama 意图分类预热完成")
            except Exception as e:
                log.warning(f"[warmup] Ollama 预热失败(不影响使用): {e}")
            # 2. 预热 brain(含 Assistant 构造 + 首次 LLM 调用)
            brain = _get_brain("none")
            for rsp in brain.run([{'role': 'user', 'content': '你好'}]):
                pass
            log.info("[warmup] none 大脑预启动完成, 首请求将更快")
            # 注: 不预启动 MCP brain (amap-maps/magic-phone/baize-skills),
            # 因为 4 个 MCP 进程 + ASR 模型 892MB + TTS 模型 1GB 会撑爆 16GB 内存,
            # 导致系统 swap → 所有本地推理变慢 10-20 倍。
            # MCP 进程按需启动, 首次调用慢 ~2s 但不会拖垮全局。
        except Exception as e:
            log.warning(f"[warmup] 预热失败(不影响使用): {e}")
    threading.Thread(target=_w, daemon=True).start()
