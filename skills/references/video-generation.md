# 抖音/短视频制作流程（PIL + ffmpeg）

用 Python PIL 生成文字幻灯片图片帧 → ffmpeg 合成视频 → 混入 TTS 音频。
用户要求视频质量：文字清晰无乱码、语音清楚、音色好。

## 为什么不用 ffmpeg drawtext

macOS Homebrew 安装的 ffmpeg **没有 drawtext 滤镜**（未编译 `--enable-libfreetype`）。
```
[AVFilterGraph] No such filter: 'drawtext'
```
必须用 Python PIL (Pillow) 生成带文字的图片，再用 ffmpeg 把图片转视频。

## PIL 生成幻灯片（正确方式）

### 字体选择（关键坑）
- `PingFang.ttc` **路径不对**：`/System/Library/Fonts/PingFang.ttc` 实际不存在（macOS 26+）
- 用 `fc-list :lang=zh` 查可用中文字体
- **可用字体**：`/System/Library/Fonts/STHeiti Medium.ttc`（黑体）
- **不可用**：`/System/Library/Fonts/PingFang.ttc`（文件不存在，PIL 报 `OSError: cannot open resource`）

### 代码模板
```python
from PIL import Image, ImageDraw, ImageFont

font_path = "/System/Library/Fonts/STHeiti Medium.ttc"
pages = [
    {"bg": (26, 26, 46), "title": "标题文字", "subtitle": "副标题第一行\n副标题第二行",
     "title_color": (79,195,247), "sub_color": (255,255,255)},
    # ... 更多页
]
for i, page in enumerate(pages):
    img = Image.new("RGB", (1080, 1920), page["bg"])
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype(font_path, 72)
    sub_font = ImageFont.truetype(font_path, 52)
    # 标题居中
    bbox = draw.textbbox((0,0), page["title"], font=title_font)
    x = (1080 - (bbox[2]-bbox[0])) // 2
    draw.text((x, 600), page["title"], fill=page["title_color"], font=title_font)
    # 副标题多行
    sub_y = 850
    for line in page["subtitle"].split('\n'):
        bbox = draw.textbbox((0,0), line, font=sub_font)
        x = (1080 - (bbox[2]-bbox[0])) // 2
        draw.text((x, sub_y), line, fill=page["sub_color"], font=sub_font)
        sub_y += 100
    img.save(f"/tmp/slide_{i}.png")
```

### 图片转视频片段
```bash
ffmpeg -y -loop 1 -i /tmp/slide_0.png -c:v libx264 -t 4 -pix_fmt yuv420p -vf scale=1080:1920 -r 30 /tmp/slide_0.mp4
```

### 合并片段 + 混入音频
```bash
# 合并视频片段
echo "file '/tmp/slide_0.mp4'" > /tmp/slides_list.txt
echo "file '/tmp/slide_1.mp4'" >> /tmp/slides_list.txt
ffmpeg -y -f concat -safe 0 -i /tmp/slides_list.txt -c copy /tmp/video_noaudio.mp4

# 混入音频
ffmpeg -y -i /tmp/video_noaudio.mp4 -i /tmp/voice.wav \
  -c:v copy -c:a aac -shortest -map 0:v:0 -map 1:a:0 /tmp/final.mp4

# 验证有 audio + video 流
ffprobe -v error -show_streams /tmp/final.mp4 | grep codec_type
```

## TTS 语音生成

用户明确要求：**音色要好**。本地 Qwen3-TTS-0.6B 音色差，用户不满意。

### Edge TTS（Hermes text_to_speech 工具，推荐用于短视频配音）
Hermes 内置的 `text_to_speech` 工具使用 Edge TTS 引擎，音色自然、清晰度高，适合短视频配音。2026-08-04 实测成功用于抖音视频。
```
// 在 Hermes 工具中调用
text_to_speech(text="需要朗读的文案")
// 返回 mp3 文件路径，如 <home>/.hermes/cache/audio/tts_20260804_064919.mp3
```
- **优势**：音色自然（优于本地 Qwen3-TTS-0.6B）、无需额外配置、调用简单
- **语速**：偏快，130-150字中文文案约生成16.9秒音频
- **输出格式**：mp3（不是 wav），ffmpeg 混合时直接用即可

**时长匹配策略**（重要）：
先生成语音 → 用 `ffprobe` 获取音频时长 → 根据音频时长调整幻灯片每页持续时间。
- 不要预设视频时长再去找匹配的音频——语音时长由文案长度决定，很难精确控制
- 正确顺序：文案 → text_to_speech → ffprobe 获取时长 → 调整幻灯片 durations → ffmpeg 合成
- 6张幻灯片匹配16.9秒音频的 durations 示例：[3, 2.5, 3, 2.5, 3, 1.9] = 15.9s
- 用 `ffmpeg -shortest` 截断到较短时长

### 百度 TTS（项目方案，适用于语音助手本身）
项目 `voice_agent.py` 的 `_tts_baidu()` 用百度在线 TTS，per=3 度逍遥（成熟男声）。
```python
# 直接调用项目的 TTS
import sys; sys.path.insert(0, "/path/to/assistant-kid")
from voice_agent import tts
wav_bytes = tts("要朗读的文字")
with open("/tmp/voice.wav", "wb") as f: f.write(wav_bytes)
```

### Finna qwen3-tts-flash（不推荐 — 实测返回空音频）
Finna 的 `/audio/speech` 端点（model=qwen3-tts-flash）实测返回空音频：
- stream=True：SSE 只发 `speech.audio.done` 事件，无音频数据
- stream=False：返回 44 字节空 WAV（只有 header，data 长度=0）
- 所有 voice 参数（alloy/echo/onyx 等）都返回空
- 可能是配额或 API 未正确实现，不要依赖

**Finna API key 与模型绑定**（2026-08-04 确认）：
Finna 的每个 API key 绑定特定模型，不能混用：
- `GLM_KEY` → 只能调 `deepseek-v4-flash`（chat completions）
- `TTS_KEY` → 只能调 `qwen3-tts-flash`（audio/speech）但返回空
- `ASR_KEY` → 只能调 `qwen3-asr-flash`（audio/transcriptions）
- 用错 key+model 组合返回 400: `{"code":"invalid_param","message":"this key must use the X model"}`
- `tts-1`/`tts-1-hd` 等标准 OpenAI TTS 模型名会返回 429（限流或不支持）

## 视频规格

- 抖音竖版：1080x1920 (9:16)
- 帧率：30fps
- 编码：H.264 (libx264)
- 像素格式：yuv420p
- 时长：每页 4-8 秒，总时长匹配语音时长

## 用户反馈记录

- "文本有问题，文字乱码" → ffmpeg drawtext 不可用，必须用 PIL
- "语音没说清楚" → TTS 语音质量
- "音色也不好" → 本地 Qwen3-TTS 音色差，用百度 per=3 度逍遥
- "用finna的tts生成声音" → Finna TTS 实测返回空音频，需用百度替代
- "抖音视频也用同样的思路重新发"（2026-08-04）→ 用 Edge TTS + PIL 幻灯片重做视频，效果用户满意
