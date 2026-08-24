"""local_wake: 本地唤醒词检测 — 不依赖浏览器前台

架构:
1. sounddevice 持续捕获麦克风音频 (16kHz mono, 16bit)
2. Vosk/openWakeWord/sherpa-onnx KeywordSpotter 检测唤醒词
3. 检测到唤醒词后, 播放提示音, VAD 录制命令音频直到静音
4. 命令音频 → voice_loop() (ASR→brain→TTS) → 扬声器播放

集成点:
- _start_wake_listener() 在 voice_server.py 启动时调用
- wake_callback 接收 WAV bytes, 触发 voice_loop → 播放 TTS
- /api/wake/toggle 端点控制启用/禁用
- audio_queue: 始终入队音频，支持 wake-during-TTS 打断
- _wake_audio_buffer: 环形缓冲区，保存唤醒前最近 N 秒音频（用于声纹校验）
"""
import os
import io
import json
import time
import wave
import queue
import threading
import logging
import numpy as np

log = logging.getLogger("magic")

# Always-listening audio queue (from agent/audio_queue.py)
try:
    from agent.audio_queue import get_audio_queue, _BUFFER_MAX_SECONDS
    _audio_queue = get_audio_queue()
    _AUDIO_QUEUE_AVAILABLE = True
except Exception:
    _audio_queue = None
    _AUDIO_QUEUE_AVAILABLE = False


class _WakeAudioBuffer:
    """环形缓冲区：保存唤醒词检测前最近 N 秒的原始 PCM 音频。

    用于声纹校验 / 事后分析，不参与实时推理。
    默认假设 16kHz / 16bit mono PCM。
    """
    def __init__(self, max_seconds: int = 3, rate: int = 16000):
        self._max_seconds = max_seconds
        self._rate = rate
        self._max_bytes = max_seconds * rate * 2  # 16-bit mono
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def push(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        with self._lock:
            self._buffer.extend(pcm_bytes)
            overflow = len(self._buffer) - self._max_bytes
            if overflow > 0:
                self._buffer = self._buffer[overflow:]

    def pop_recent(self, seconds: float = 3.0) -> bytes:
        with self._lock:
            nbytes = int(seconds * self._rate * 2)
            chunk = bytes(self._buffer[-nbytes:]) if nbytes > 0 else b""
            return chunk

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


_wake_audio_buffer = _WakeAudioBuffer(max_seconds=3)


# 音频参数
RATE = 16000  # 采样率
CHUNK = 4000  # 每次读取帧数 (250ms @ 16kHz)
CHANNELS = 1

# VAD 参数
VAD_SILENCE_FRAMES = 20      # 连续静音帧数 (250ms/帧, 即 5 秒)
VAD_MAX_RECORD_FRAMES = 60   # 最大录制帧数 (15 秒)
VAD_MIN_SPEECH_FRAMES = 3    # 最少说话帧数 (0.75 秒)
VAD_SPEECH_THRESHOLD = 0.5   # Silero VAD 语音概率阈值
VAD_CHUNK = 512              # Silero VAD 要求的帧大小 (16kHz)

# Silero VAD 模型 (懒加载)
_silero_model = None

def _load_silero_vad():
    global _silero_model
    if _silero_model is not None:
        return _silero_model
    try:
        from silero_vad import load_silero_vad
        _silero_model = load_silero_vad()
        log.info("[wake] Silero VAD 已加载")
        return _silero_model
    except Exception as e:
        log.warning(f"[wake] Silero VAD 加载失败, 降级到能量阈值: {e}")
        return None

def _is_speech(audio_chunk: np.ndarray) -> float:
    """用 Silero VAD 检测是否有语音, 返回语音概率 0~1"""
    model = _load_silero_vad()
    if model is None:
        # 降级: 能量阈值
        energy = np.abs(audio_chunk).mean()
        return 1.0 if energy > 300 else 0.0
    try:
        import torch
        if len(audio_chunk) < VAD_CHUNK:
            audio_chunk = np.pad(audio_chunk, (0, VAD_CHUNK - len(audio_chunk)))
        t = torch.from_numpy(audio_chunk[:VAD_CHUNK].astype(np.float32) / 32768.0)
        return model(t, RATE).item()
    except Exception:
        energy = np.abs(audio_chunk).mean()
        return 1.0 if energy > 300 else 0.0

# 唤醒词列表 (全部转小写匹配)
_WAKE_WORDS = [
    "charlie", "charley", "charls", "charles",
    "chali", "charli", "查理", "查里", "charlie",
    "jarvis", "贾维斯",
]

# 状态
_is_running = False
_is_enabled = True  # 可通过 /api/wake/toggle 切换
_wake_callback = None
_oww_model = None
_detector_thread = None


def _load_oww_model():
    """加载 openWakeWord 唤醒词模型（ONNX，低CPU）"""
    global _oww_model
    if _oww_model is not None:
        return _oww_model
    try:
        from openwakeword import Model
        # 使用内置的 "hey jarvis" 或自定义模型
        # 可以放自定义 .tflite 模型在 web/wake/ 目录
        model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "web", "wake")
        inference_framework = "onnx"
        if os.path.exists(model_path) and os.listdir(model_path):
            _oww_model = Model(
                wakeword_models=[os.path.join(model_path, f)
                                 for f in os.listdir(model_path)
                                 if f.endswith(('.tflite', '.onnx'))],
                inference_framework=inference_framework,
            )
            log.info("[wake] openWakeWord 自定义模型已加载: %s", model_path)
        else:
            # 使用内置模型 "hey_jarvis"（作为 charlie 的替代）
            _oww_model = Model(inference_framework=inference_framework)
            log.info("[wake] openWakeWord 内置模型已加载")
        return _oww_model
    except ImportError:
        log.warning("[wake] openwakeword 未安装，运行 pip install openwakeword")
        return None
    except Exception as e:
        log.warning(f"[wake] openWakeWord 加载失败: {e}")
        return None


# ── sherpa-onnx KeywordSpotter ──

_sherpa_kws = None
_sherpa_kws_config = None


def _load_sherpa_kws():
    """尝试加载 sherpa-onnx KeywordSpotter（需额外模型文件）"""
    global _sherpa_kws, _sherpa_kws_config
    if _sherpa_kws is not None:
        return _sherpa_kws
    try:
        import sherpa_onnx
    except ImportError:
        log.debug("[wake] sherpa-onnx 未安装，跳过 KeywordSpotter")
        return None
    try:
        # 配置路径：可通过环境变量覆盖
        # KWS_MODEL=模型目录或文件，KWS_TOKENS=tokens.txt，KWS_KEYWORDS=keywords.txt
        model_dir = os.getenv("KWS_MODEL", "")
        tokens_path = os.getenv("KWS_TOKENS", "")
        keywords_path = os.getenv("KWS_KEYWORDS", "")

        if not model_dir:
            log.debug("[wake] 未设置 KWS_MODEL，跳过 sherpa KeywordSpotter")
            return None

        # 自动补齐 tokens/keywords
        if not tokens_path:
            tokens_path = os.path.join(model_dir, "tokens.txt")
        if not keywords_path:
            keywords_path = os.path.join(model_dir, "keywords.txt")

        if not os.path.exists(tokens_path) or not os.path.exists(keywords_path):
            log.warning("[wake] KWS tokens/keywords 文件缺失: %s, %s", tokens_path, keywords_path)
            return None

        _sherpa_kws_config = sherpa_onnx.KeywordSpotterConfig(
            model=sherpa_onnx.KeywordSpotterModelConfig(
                encoder=os.path.join(model_dir, "encoder.onnx") if os.path.exists(os.path.join(model_dir, "encoder.onnx")) else "",
                decoder=os.path.join(model_dir, "decoder.onnx") if os.path.exists(os.path.join(model_dir, "decoder.onnx")) else "",
                joiner=os.path.join(model_dir, "joiner.onnx") if os.path.exists(os.path.join(model_dir, "joiner.onnx")) else "",
                tokens=tokens_path,
                keywords=keywords_path,
            ),
            blank_penalty=0.0,
        )
        _sherpa_kws = sherpa_onnx.KeywordSpotter(_sherpa_kws_config)
        log.info("[wake] sherpa-onnx KeywordSpotter 已加载: %s", model_dir)
        return _sherpa_kws
    except Exception as e:
        log.warning(f"[wake] sherpa-onnx KeywordSpotter 加载失败: {e}")
        return None


def _sherpa_kws_detect(kws, audio_float: np.ndarray) -> tuple[bool, str | None]:
    """运行 sherpa-onnx KeywordSpotter，返回 (是否触发, 关键词)"""
    try:
        result = kws.forward(audio_float)
        if result and len(result.keywords) > 0:
            keyword = result.keywords[0].keyword
            log.info(f"[wake][sherpa] 唤醒词: {keyword} (score={result.keywords[0].score:.2f})")
            return True, keyword
    except Exception as e:
        log.debug(f"[wake][sherpa] 检测异常: {e}")
    return False, None


# ── Speaker Verification (sherpa-onnx) ──

_speaker_extractor = None
_speaker_manager = None
_speaker_threshold = 0.7  # 余弦相似度阈值，高于此值视为同一人


def _load_speaker_extractor():
    """加载 sherpa-onnx SpeakerEmbeddingExtractor（需额外模型文件）"""
    global _speaker_extractor
    if _speaker_extractor is not None:
        return _speaker_extractor
    try:
        import sherpa_onnx
    except ImportError:
        log.debug("[wake] sherpa-onnx 未安装，跳过 speaker verification")
        return None
    try:
        model_dir = os.getenv("SPEAKER_MODEL_DIR", "")
        if not model_dir or not os.path.exists(model_dir):
            log.debug("[wake] 未设置 SPEAKER_MODEL_DIR，跳过 speaker verification")
            return None

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=os.path.join(model_dir, "model.onnx"),
            # 部分模型需要额外配置
        )
        _speaker_extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        log.info("[wake] speaker embedding extractor 已加载: %s", model_dir)
        return _speaker_extractor
    except Exception as e:
        log.warning(f"[wake] speaker embedding extractor 加载失败: {e}")
        return None


def extract_speaker_embedding(audio_bytes: bytes) -> np.ndarray | None:
    """从 WAV/PCM bytes 提取 speaker embedding（用于注册/校验）"""
    extractor = _load_speaker_extractor()
    if extractor is None:
        return None
    try:
        # 确保是 16kHz 单声道 int16 PCM
        if audio_bytes.startswith(b'RIFF'):
            import soundfile as sf
            data, sr = sf.read(io.BytesIO(audio_bytes))
            if data.ndim > 1:
                data = data[:, 0]
            audio_float = data.astype(np.float32)
        else:
            audio_float = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        if len(audio_float) < 1600:  # 至少 0.1s
            return None

        stream = extractor.create_stream()
        stream.accept_waveform(RATE, audio_float)
        embedding = stream.compute_speaker_embedding()
        return np.array(embedding) if embedding is not None else None
    except Exception as e:
        log.debug(f"[wake] speaker embedding 提取失败: {e}")
        return None


def verify_speaker(audio_bytes: bytes, reference_embedding: np.ndarray, threshold: float | None = None) -> bool:
    """校验音频是否与 reference_embedding 属于同一说话人

    threshold: 余弦相似度阈值（默认 _speaker_threshold）
    Returns: True=通过验证，False=未通过
    """
    if reference_embedding is None:
        return False
    embedding = extract_speaker_embedding(audio_bytes)
    if embedding is None:
        return False
    try:
        # 余弦相似度
        sim = np.dot(embedding, reference_embedding) / (
            np.linalg.norm(embedding) * np.linalg.norm(reference_embedding)
        )
        th = threshold if threshold is not None else _speaker_threshold
        passed = bool(sim > th)
        log.info(f"[wake] speaker verification: similarity={sim:.3f}, threshold={th:.2f}, passed={passed}")
        return passed
    except Exception as e:
        log.debug(f"[wake] speaker verification 异常: {e}")
        return False


def register_speaker(audio_bytes: bytes) -> np.ndarray | None:
    """注册用户的声纹特征（提取 embedding 并保存到 preferences）

    调用后返回 embedding，可后续用于 verify_speaker。
    """
    embedding = extract_speaker_embedding(audio_bytes)
    if embedding is None:
        return None
    try:
        from agent.preferences import set_preference
        set_preference("speaker_embedding", embedding.tolist())
        log.info("[wake] speaker embedding 已注册并保存到 preferences")
        return embedding
    except Exception as e:
        log.warning(f"[wake] speaker embedding 注册保存失败: {e}")
        return embedding


def _play_beep():
    """播放短促提示音 (800Hz 正弦波, 100ms)"""
    try:
        import sounddevice as sd
        duration = 0.1
        t = np.linspace(0, duration, int(RATE * duration), False)
        beep = np.sin(2 * np.pi * 800 * t) * 0.3
        sd.play(beep, RATE)
        sd.wait()
    except Exception:
        pass  # 提示音失败不影响核心功能


def _play_audio(audio_bytes: bytes, interruptible: bool = True) -> bool:
    """播放音频到扬声器, 支持中断检测
    interruptible: 是否允许用户通过说话打断播放
    Returns: True=播放完成, False=被中断
    """
    try:
        import sounddevice as sd
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(audio_bytes))
        if data.ndim > 1:
            data = data[:, 0]
        
        if not interruptible:
            sd.play(data, sr)
            sd.wait()
            log.info(f"[wake] 播放完成: {len(audio_bytes)} 字节")
            return True
        
        # 中断检测: 边播放边监听麦克风
        chunk_size = int(sr * 0.2)  # 200ms 检测间隔
        played = 0
        speech_frames = 0
        
        while played < len(data):
            end = min(played + chunk_size, len(data))
            chunk = data[played:end]
            sd.play(chunk, sr, blocking=False)
            
            # 同时捕获麦克风音频 (检测是否有人在说话)
            try:
                mic_data = sd.rec(chunk_size, samplerate=RATE, channels=1, dtype='int16')
                sd.wait()  # 等待捕获完成
                if mic_data.ndim > 1:
                    mic_data = mic_data[:, 0]
                speech_prob = _is_speech(mic_data)
                if speech_prob > VAD_SPEECH_THRESHOLD:
                    speech_frames += 1
                    if speech_frames >= 3:  # 连续 3 帧 (600ms) 有语音
                        sd.stop()  # 停止播放
                        log.info("[wake] TTS 被用户语音打断 (Silero prob={:.2f})".format(speech_prob))
                        _play_beep()  # 确认听见了
                        return False
                else:
                    speech_frames = 0
            except Exception:
                pass
            
            played += chunk_size
        
        log.info(f"[wake] 播放完成: {len(audio_bytes)} 字节")
        return True
    except Exception as e:
        log.warning(f"[wake] 音频播放失败: {e}")
        return True  # 播放失败视为完成


def _record_command() -> bytes | None:
    """录制命令音频: Silero VAD 检测语音边界, 静音后停止, 返回 WAV bytes"""
    try:
        import sounddevice as sd
    except Exception:
        return None

    frames = []
    speech_count = 0
    silence_count = 0

    log.info("[wake] 开始录制命令 (Silero VAD 监听...)")

    with sd.InputStream(samplerate=RATE, channels=CHANNELS, dtype='int16',
                        blocksize=CHUNK) as stream:
        while True:
            data, _ = stream.read(CHUNK)
            if data.ndim > 1:
                data = data[:, 0]
            data_bytes = data.tobytes()
            frames.append(data_bytes)

            # Silero VAD 语音检测
            speech_prob = _is_speech(data)

            if speech_prob > VAD_SPEECH_THRESHOLD:
                silence_count = 0
                speech_count += 1
            else:
                silence_count += 1

            # 停止条件: 说话后静音超过阈值, 或超时
            if silence_count >= VAD_SILENCE_FRAMES and speech_count >= VAD_MIN_SPEECH_FRAMES:
                break
            if len(frames) >= VAD_MAX_RECORD_FRAMES:
                break

    if speech_count < VAD_MIN_SPEECH_FRAMES:
        log.info("[wake] 未检测到有效语音, 忽略")
        return None

    # 合并为 WAV
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))

    wav_bytes = wav_buffer.getvalue()
    log.info(f"[wake] 命令录制完成: {len(wav_bytes)} 字节, {speech_count} 语音帧")
    return wav_bytes


def _listen_loop():
    """持续监听唤醒词的主循环 — openWakeWord (ONNX, 低CPU) / sherpa-onnx KeywordSpotter

    结合 agent/audio_queue.py 实现 always-listening：
    - 所有音频始终入队到 audio_queue（即使 TTS 播放中也不阻塞）
    - 唤醒时从队列取最近 3 秒音频作为 pre-wake context
    - 可选：_wake_audio_buffer 保存最近 3 秒 PCM（用于声纹校验）
    """
    global _is_running

    oww_model = _load_oww_model()
    kws_model = _load_sherpa_kws()
    use_kws = kws_model is not None

    if not oww_model and not use_kws:
        log.warning("[wake] 无可用唤醒词模型 (openWakeWord/sherpa)，本地唤醒词不可用")
        return

    import sounddevice as sd
    import numpy as np

    # openWakeWord 需要 16kHz, 单声道, int16
    oww_chunk = 1280  # 80ms at 16kHz

    detector_label = "sherpa-onnx KeywordSpotter" if use_kws else "openWakeWord"
    log.info("[wake] 本地唤醒词监听已启动 (%s)", detector_label)

    with sd.InputStream(samplerate=RATE, channels=CHANNELS, dtype='int16',
                        blocksize=oww_chunk) as stream:
        _is_running = True
        cooldown_until = 0
        while _is_running:
            try:
                data, _ = stream.read(oww_chunk)
                if data.ndim > 1:
                    data = data[:, 0]
                data_bytes = data.tobytes()

                # Always-listening: 始终入队音频（即使 TTS 播放中）
                if _AUDIO_QUEUE_AVAILABLE and _audio_queue is not None and _audio_queue.is_enabled():
                    _audio_queue.push(data_bytes)

                # 保存到 _wake_audio_buffer（用于声纹校验 / 事后分析）
                _wake_audio_buffer.push(data_bytes)

                if not _is_enabled:
                    time.sleep(0.5)
                    continue

                # 冷却期（避免重复触发）
                if time.time() < cooldown_until:
                    continue

                triggered = False
                wake_keyword = None

                if use_kws:
                    # sherpa-onnx KeywordSpotter（低延迟，专用模型）
                    audio_float = data.astype(np.float32) / 32768.0
                    triggered, wake_keyword = _sherpa_kws_detect(kws_model, audio_float)
                elif oww_model is not None:
                    # openWakeWord 预测
                    audio_float = data.astype(np.float32) / 32768.0
                    prediction = oww_model.predict(audio_float)

                    # 检查任意唤醒词分数 > 0.5
                    for wake_name, score in prediction.items():
                        if score > 0.5:
                            log.info(f"[wake] 唤醒词: {wake_name} (score={score:.2f})")
                            triggered = True
                            wake_keyword = wake_name
                            break

                if triggered:
                    cooldown_until = time.time() + 3  # 3秒冷却
                    _play_beep()
                    # 从 audio_queue 取 pre-wake 音频（最近 3 秒）
                    pre_wake_audio = b""
                    if _AUDIO_QUEUE_AVAILABLE and _audio_queue is not None:
                        pre_wake_audio = _audio_queue.pop_recent(_BUFFER_MAX_SECONDS)
                    wav = _record_command()
                    if wav and _wake_callback:
                        try:
                            # 合并 pre-wake 音频 + 命令音频
                            combined = pre_wake_audio + wav if pre_wake_audio else wav
                            _wake_callback(combined, keyword=wake_keyword)
                        except TypeError:
                            # 兼容旧版回调（不接受 keyword 参数）
                            try:
                                _wake_callback(combined)
                            except Exception as e:
                                log.warning(f"[wake] 回调失败: {e}")
                        except Exception as e:
                            log.warning(f"[wake] 回调失败: {e}")
            except Exception as e:
                log.debug(f"[wake] 监听异常: {e}")
                time.sleep(0.1)

    log.info("[wake] 本地唤醒词监听已停止")


def start_wake_detector(callback):
    """启动唤醒词检测器 (后台线程)
    
    callback: 接收 wav_bytes 参数, 异步处理命令
    """
    global _wake_callback, _detector_thread, _is_running
    _wake_callback = callback
    _detector_thread = threading.Thread(target=_listen_loop, daemon=True)
    _detector_thread.start()
    log.info("[wake] 唤醒词检测器启动")


def stop_wake_detector():
    """停止唤醒词检测器"""
    global _is_running
    _is_running = False
    log.info("[wake] 唤醒词检测器已停止")


def toggle_wake(enabled: bool = None) -> bool:
    """启用/禁用唤醒词检测, 返回当前状态"""
    global _is_enabled
    if enabled is not None:
        _is_enabled = enabled
        log.info(f"[wake] 唤醒词检测 {'启用' if enabled else '禁用'}")
    return _is_enabled


def is_listening() -> bool:
    """返回是否正在监听"""
    return _is_running and _is_enabled


def wake_status() -> dict:
    """返回唤醒词检测器状态"""
    return {
        "running": _is_running,
        "enabled": _is_enabled,
        "model_loaded": _oww_model is not None or _sherpa_kws is not None,
        "silero_vad": _silero_model is not None,
        "wake_words": _WAKE_WORDS,
        "vad_threshold": VAD_SPEECH_THRESHOLD,
    }
