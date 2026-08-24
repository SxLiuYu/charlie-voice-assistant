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
    同时预热 ARK 意图分类 + 百度ASR/TTS + 常见短回复 TTS 预合成."""
    def _w():
        log.info("[warmup] 预启动 none 大脑(0 MCP)...")
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from voice_agent import _get_brain, _classify_intent, _baidu_get_token, _tts_baidu
            # 0. 预热 SenseVoice 本地 ASR（首次冷启动 ~528ms，预热后首请求 0ms）
            try:
                from agent.asr_tts import _load_sense_voice
                _load_sense_voice()
                log.info("[warmup] SenseVoice 本地ASR预热完成")
            except Exception as e:
                log.warning(f"[warmup] SenseVoice预热失败(不影响使用): {e}")
            # 1. 预热 ARK 意图分类(prefix-cache初次构建)
            try:
                _classify_intent("你好")
                log.info("[warmup] ARK 意图分类预热完成")
            except Exception as e:
                log.warning(f"[warmup] 意图分类预热失败(不影响使用): {e}")
            # 2. 预热百度ASR token + ffmpeg (首次冷启动1.8s → 热启动0.37s)
            try:
                _baidu_get_token()
                log.info("[warmup] 百度ASR token预热完成")
            except Exception as e:
                log.warning(f"[warmup] 百度token预热失败(不影响使用): {e}")
            # 3. 预热百度TTS (首次ffmpeg冷启动) + 常见短回复预合成
            try:
                _tts_baidu("预热")
                log.info("[warmup] 百度TTS预热完成")
                # 预合成常见短回复, 存入全局缓存(省0.34s/次)
                from voice_agent import _tts_cache, tts
                common_replies = ["在呢，说。", "好的。", "嗯嗯。", "抱歉，我没听清，请再说一遍。"]
                for reply_text in common_replies:
                    try:
                        audio = _tts_baidu(reply_text)
                        _tts_cache[reply_text] = audio
                    except Exception:
                        pass
                log.info(f"[warmup] 常见回复TTS预合成完成 ({len(_tts_cache)}条)")
            except Exception as e:
                log.warning(f"[warmup] 百度TTS预热失败(不影响使用): {e}")
            # 4. 预热 brain(含 Assistant 构造 + 首次 LLM 调用)
            brain = _get_brain("none")
            for rsp in brain.run([{'role': 'user', 'content': '你好'}]):
                pass
            log.info("[warmup] none 大脑预启动完成, 首请求将更快")
            # 5. 预热 amap-maps brain(省首次天气查询1秒MCP初始化)
            try:
                _get_brain("amap-maps")
                log.info("[warmup] amap-maps 大脑预启动完成, 首次天气查询将更快")
            except Exception as e:
                log.warning(f"[warmup] amap-maps预热失败(不影响使用): {e}")
        except Exception as e:
            log.warning(f"[warmup] 预热失败(不影响使用): {e}")
    threading.Thread(target=_w, daemon=True).start()
