#!/usr/bin/env python3
"""本地 ASR 微服务 — Mac Mini M4 MLX 加速
ASR: SenseVoiceSmall (FunASR) — 0.49s, 90%+ 准确率
"""
import os, time, io, json, tempfile, logging, traceback

os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
os.environ['HF_HOME'] = '/Users/sxliuyu/.local/share/hf-models'
os.environ['MODELSCOPE_CACHE'] = '/Users/sxliuyu/.cache/modelscope'

from flask import Flask, request, jsonify
import soundfile as sf
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger("local-asr")

app = Flask(__name__)

_asr_model = None
_asr_lock = threading.Lock()

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

@app.route('/asr', methods=['POST'])
def asr():
    """音频文件 → 文字"""
    t0 = time.time()
    
    audio_file = request.files.get('file')
    if not audio_file:
        return jsonify({"error": "no file"}), 400
    
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
        
        if wav.dtype != np.float32:
            wav = wav.astype(np.float32)
        
        if np.abs(wav).max() > 1.0:
            wav = wav / 32768.0
        
        # 推理锁(防止多线程并发调用模型)
        with _asr_lock:
            result = model.generate(input=wav, language="zh", use_itn=True)
        
        # 清理特殊标记
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

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "local-asr", "asr_loaded": _asr_model is not None})

if __name__ == '__main__':
    log.info("启动本地 ASR 微服务...")
    _load_asr()
    app.run(host='127.0.0.1', port=8766, threaded=True)