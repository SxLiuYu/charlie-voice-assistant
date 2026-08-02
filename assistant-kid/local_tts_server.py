#!/usr/bin/env python3
"""本地 TTS 微服务 — Mac Mini M4 MLX 加速
TTS: Qwen3-TTS-0.6B-4bit (mlx-audio) — 短文本热运行 0.65s
独立进程, 避免与 ASR 争抢 GPU 内存
模型预加载后复用, 避免每次请求重新加载
"""
import os, time, io, json, tempfile, logging, traceback

from flask import Flask, request, jsonify, Response

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger("local-tts")

app = Flask(__name__)

_tts_generate = None  # 预加载的 generate_audio 函数
_tts_model_instance = None  # 预加载的模型实例

_LOCAL_TTS_MODEL_PATH = "/Users/sxliuyu/.local/share/models/Qwen3-TTS-12Hz-0.6B-Base-4bit-mlx"

def _load_tts():
    global _tts_generate, _tts_model_instance
    if _tts_model_instance is not None:
        return _tts_generate, _tts_model_instance
    from mlx_audio.tts.generate import generate_audio
    from mlx_audio.tts.utils import load_model

    log.info("加载 Qwen3-TTS-0.6B-4bit (本地 MLX)...")
    t0 = time.time()

    # 预加载模型实例 (避免每次请求重新加载)
    _tts_model_instance = load_model(model_path=_LOCAL_TTS_MODEL_PATH)
    _tts_generate = generate_audio

    elapsed = time.time() - t0
    log.info(f"TTS 模型加载完成: {elapsed:.1f}s")

    # 预热: 首次生成会初始化推理引擎
    log.info("TTS 预热中...")
    tmpdir = tempfile.mkdtemp()
    try:
        generate_audio(
            text="预热",
            model=_tts_model_instance,  # 传入已加载的模型实例
            lang_code="zh",
            file_prefix=f"{tmpdir}/warmup",
            join_audio=True,
            verbose=False,
        )
        elapsed = time.time() - t0
        log.info(f"TTS 预热完成: {elapsed:.1f}s")
    except Exception as e:
        elapsed = time.time() - t0
        log.warning(f"TTS 预热失败 ({elapsed:.1f}s): {e}")
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return _tts_generate, _tts_model_instance

@app.route('/tts', methods=['POST'])
def tts():
    """文本 → WAV 音频文件"""
    t0 = time.time()

    data = request.get_json(silent=True) or {}
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "no text"}), 400

    try:
        generate_fn, model_instance = _load_tts()
        tmpdir = tempfile.mkdtemp()
        output_path = os.path.join(tmpdir, "output")

        generate_fn(
            text=text,
            model=model_instance,  # 复用预加载的模型实例
            lang_code="zh",
            file_prefix=output_path,
            join_audio=True,
            verbose=False,
        )

        # mlx-audio join_audio=True 生成 <file_prefix>.wav
        wav_path = output_path + ".wav"
        if not os.path.exists(wav_path):
            wav_path = os.path.join(tmpdir, "audio_000.wav")
        if not os.path.exists(wav_path):
            return jsonify({"error": "TTS生成失败: 文件不存在"}), 500

        with open(wav_path, 'rb') as f:
            wav_bytes = f.read()

        try:
            os.unlink(wav_path)
            os.rmdir(tmpdir)
        except Exception:
            pass

        elapsed = time.time() - t0
        log.info(f"TTS: \"{text[:30]}\" ({elapsed:.3f}s, {len(wav_bytes)}B)")

        return Response(wav_bytes, mimetype='audio/wav',
                       headers={'X-Latency': f'{elapsed:.3f}',
                                'X-Audio-Size': str(len(wav_bytes))})

    except Exception as e:
        log.error(f"TTS 错误: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "local-tts", "tts_loaded": _tts_model_instance is not None})

if __name__ == '__main__':
    log.info("启动本地 TTS 微服务 (端口 8767)...")
    _load_tts()
    app.run(host='127.0.0.1', port=8767, threaded=True)