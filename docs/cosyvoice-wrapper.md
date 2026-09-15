# CosyVoice Wrapper 服务接口约定

Charlie 的 `agent/cosyvoice_clone.py` 通过 HTTP 调用 CosyVoice 做语音克隆合成。
**官方 CosyVoice 是 Python 库（Gradio demo），不提供这套 HTTP API**，因此需要
自建一个薄 wrapper 服务把库能力暴露成下列三个接口。本文档即接口契约 + 最小实现示例。

## 启用方式

1. 部署 wrapper 服务（见下文示例），确认 `GET /health` 返回 200
2. `.env` 中设置：
   ```
   COSYVOICE_BASE=http://127.0.0.1:8080
   TTS_PRIORITY=cosyvoice,baidu,stepfun,finna   # 把 cosyvoice 加进链首
   ```
3. 重启 Charlie。未启用时 cosyvoice 不在默认链里，行为与之前完全一致。

## 接口契约

### 1. `GET /health` — 存活检查

返回 200 即在线。`is_available()` / MCP `voice_status` 用。

### 2. `POST /upload` — 上传参考音频，注册说话人

- 请求: `multipart/form-data`
  - `audio`: WAV/MP3 文件（建议 3-10 秒，单人、干净人声）
  - `name`: 说话人名称（如 "小明"）
- 响应: `{"speaker_id": "..."}`（200）；失败 4xx/5xx
- 用途: `create_voice_profile()` 创建克隆声时调用；上传失败不阻断本地建档
  （`speaker_id` 记空串，合成时若带 `reference_audio` 字段再走服务端注册）。

### 3. `POST /api/tts` — 合成

请求 JSON:

```json
{
  "text": "要合成的文本",
  "speaker": "中文女",
  "speaker_id": "之前 /upload 返回的 ID，可为空串",
  "speed": 1.0,
  "reference_audio": "可选；speaker_id 为空且服务与 Charlie 同机时的参考音频绝对路径"
}
```

响应（二选一，客户端都支持）:
- `Content-Type: audio/*`，body 为 WAV 字节（主流情形）
- `application/json`：`{"audio": "<base64 WAV>"}`

客户端（`synthesize()`）拿到 WAV 后用 ffmpeg 转 MP3 再进 TTS 管道。
**注意**: `reference_audio` 传的是本机绝对路径，要求 wrapper 与 Charlie 同机
部署；跨机部署请总是先走 `/upload` 拿 `speaker_id`。

## 最小 wrapper 示例（FastAPI）

```python
"""cosyvoice_wrapper.py — 最小 CosyVoice HTTP wrapper
pip install fastapi uvicorn python-multipart
python -m uvicorn cosyvoice_wrapper:app --port 8080
模型按官方 CosyVoice 仓库 README 下载（预训练 CosyVoice2-0.5B）。
"""
import io
import uuid
from pathlib import Path

import torchaudio
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response

from cosyvoice.cli.cosyvoice import CosyVoice2
from cosyvoice.utils.file_utils import load_wav

MODEL_DIR = "pretrained_models/CosyVoice2-0.5B"
app = FastAPI()
model = CosyVoice2(MODEL_DIR, load_jit=False, load_trt=False)
_speakers: dict[str, str] = {}  # name -> prompt_wav 路径


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/upload")
async def upload(audio: UploadFile = File(...), name: str = Form(...)):
    sid = str(uuid.uuid4())
    dest = Path(f"refs/{sid}.wav")
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(await audio.read())
    _speakers[sid] = str(dest)
    return {"speaker_id": sid}


@app.post("/api/tts")
def tts(body: dict):
    text, speed = body["text"], float(body.get("speed", 1.0))
    sid = body.get("speaker_id", "")
    ref = body.get("reference_audio", "")
    if not sid and ref and Path(ref).is_file():
        sid = str(uuid.uuid4())
        _speakers[sid] = ref
    if sid and sid in _speakers:
        prompt_wav = load_wav(_speakers[sid], 16000)
        result = next(model.inference_zero_shot(
            text, "", prompt_wav, stream=False, speed=speed))
    else:
        result = next(model.inference_sft(
            text, body.get("speaker", "中文女"), stream=False, speed=speed))
    wav = result["tts_speech"].squeeze(0).numpy()
    buf = io.BytesIO()
    torchaudio.save(buf, wav.unsqueeze(0), 24000, format="wav")
    return Response(content=buf.getvalue(), media_type="audio/wav")
```

> 示例仅表达契约：`inference_zero_shot` / `inference_sft` 的具体参数以你
> 部署的 CosyVoice 版本 API 为准。流式（`stream=True` + 分块返回）暂不在
> 契约内，需要时再扩展 `/api/tts/stream`。

## 克隆声与规范音色的关系

- 7 个规范音色（Ethan/Alex → 中文男，其余 → 中文女）走 `inference_sft` 内置说话人。
- 用户克隆声（`data/cosyvoice_voices/*.json` 里的 name）命中时优先走零样本
  `inference_zero-shot`，同名时克隆声覆盖内置声。
