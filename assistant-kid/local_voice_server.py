#!/usr/bin/env python3
"""本地 ASR + TTS 微服务 — Mac Mini M4 MLX 加速
ASR: SenseVoiceSmall (FunASR)   — 0.49s, 90%+ 准确率
TTS: Qwen3-TTS-0.6B-4bit (MLX)   — 短文本 0.65s, 长文本 ~8字/s
"""
import os, time, io, json, tempfile, logging, traceback

os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
os.environ['HF_HOME'] = '/Users/sxliuyu/.local/share/hf-models'
os.environ['MODELSCOPE_CACHE'] = '/Users/sxliuyu/.cache/modelscope'

from flask import Flask, request, jsonify, Response
import soundfile as sf
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger("local-voice")

app = Flask(__name__)

# ===== 全局模型实例 =====
_asr_model = None
_tts_ready = False
_TTS_MODEL_PATH = '/Users/sxliuyu/.local/share/models/Qwen3-TTS-0.6B-4bit'

def _load_asr():
    global _asr_model
    if _asr_model is not None:
        return _asr_model
    from funasr import AutoModel
    log.info("加载 SenseVoiceSmall...")
    t0 = time.time()
    _asr_model = AutoModel(model='iic/SenseVoiceSmall', disable_update=True)
    log.info(f"ASR 模型加载完成: {time.time()-t0:.1f}s")
    return _asr_model

def _load_tts():
    global _tts_ready
    if _tts_ready:
        return
    from mlx_audio.tts.generate import generate_audio
    log.info("加载 Qwen3-TTS 模型...")
    t0 = time.time()
    # 预热
    generate_audio(text='预热', model=_TTS_MODEL_PATH, voice='default', lang_code='zh')
    _tts_ready = True
    log.info(f"TTS 模型加载+预热完成: {time.time()-t0:.1f}s")

# ===== ASR 端点 =====
@app.route('/asr', methods=['POST'])
def asr():
    """音频文件 → 文字"""
    t0 = time.time()
    
    audio_file = request.files.get('file')
    if not audio_file:
        return jsonify({"error": "no file"}), 400
    
    # 保存临时文件
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name
    
    try:
        model = _load_asr()
        
        # 读取音频
        wav, sr = sf.read(tmp_path)
        if len(wav.shape) > 1:
            wav = wav[:, 0]  # 取左声道
        
        # SenseVoice 需要 16kHz
        if sr != 16000:
            import librosa
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
            sr = 16000
        
        # 确保是 float32
        if wav.dtype != np.float32:
            wav = wav.astype(np.float32)
        
        # 归一化
        if np.abs(wav).max() > 1.0:
            wav = wav / 32768.0
        
        result = model.generate(input=wav, language='zh', use_itn=True)
        
        # 清理特殊标记 — 用正则一次清理所有 <|xxx|> 标签
        import re
        text = ""
        if result:
            raw = result[0].get('text', '')
            text = re.sub(r'<\|[^|]+\|>', '', raw).strip()
        
        elapsed = time.time() - t0
        log.info(f"ASR: \"{text}\" ({elapsed:.3f}s)")
        return jsonify({"text": text, "latency": round(elapsed, 3)})
    
    except Exception as e:
        log.error(f"ASR 错误: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp_path)

# ===== TTS 端点 =====
@app.route('/tts', methods=['POST'])
def tts():
    """文字 → WAV 音频"""
    t0 = time.time()
    data = request.get_json()
    text = data.get('text', '').strip()
    
    if not text or len(text) < 2:
        return jsonify({"error": "text too short"}), 400
    
    try:
        _load_tts()
        from mlx_audio.tts.generate import generate_audio
        
        # 生成音频
        generate_audio(text=text, model=_TTS_MODEL_PATH, voice='default', lang_code='zh')
        
        # 找生成的音频文件
        wav_path = '/Users/sxliuyu/audio_000.wav'
        if not os.path.exists(wav_path):
            return jsonify({"error": "no audio generated"}), 500
        
        # 读取音频
        with open(wav_path, 'rb') as f:
            audio_bytes = f.read()
        
        # 清理
        os.unlink(wav_path)
        
        elapsed = time.time() - t0
        log.info(f"TTS: \"{text[:30]}\" ({elapsed:.3f}s, {len(audio_bytes)}B)")
        
        from flask import Response
        return Response(audio_bytes, mimetype='audio/wav',
                       headers={'X-Latency': f'{elapsed:.3f}'})
    
    except Exception as e:
        log.error(f"TTS 错误: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

# ===== 健康检查 =====
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "ok": True,
        "service": "local-voice",
        "asr_loaded": _asr_model is not None,
        "tts_loaded": _tts_ready,
    })

if __name__ == '__main__':
    log.info("启动本地 ASR/TTS 微服务...")
    # 预加载 ASR 模型
    _load_asr()
    # 预加载 TTS 模型
    _load_tts()
    app.run(host='127.0.0.1', port=8766, threaded=True)
