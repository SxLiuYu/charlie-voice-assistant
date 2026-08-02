# Charlie 语音助手 — 部署指南

## 架构概览

```
用户浏览器 (voice.html)
    ↓ HTTP/WebSocket
voice_server.py (FastAPI, port 8000)
    ├─ ASR: 自托管 local_asr_server.py (port 8766) / Finna ASR
    ├─ 大脑: deepseek-v4-flash (Finna API)
    ├─ TTS: qwen3-tts-flash (Finna API)
    ├─ MCP工具: mcp_ir_control.py → ESP32 HTTP API
    └─ Cloudflare Tunnel: 公网访问
```

## 前置条件

- Python 3.12+
- ESP32 开发板（IR 控制用，可选）
- Finna API Key（大脑+TTS+ASR）

## 1. 克隆仓库

```bash
git clone https://github.com/SxLiuYu/charlie-voice-assistant.git
cd charlie-voice-assistant
```

## 2. 安装依赖

```bash
cd assistant-kid
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 API Key
```

### 必需的 Key

| 变量 | 用途 | 获取方式 |
|------|------|----------|
| `GLM_KEY` | Finna 大脑 (deepseek-v4-flash) | Finna 平台 |
| `TTS_KEY` | TTS 语音合成 (qwen3-tts-flash) | Finna 平台 |
| `ASR_KEY` | ASR 语音识别 | Finna 平台 |
| `AMAP_KEY` | 高德地图/天气 | 高德开放平台 |

### 可选 Key

| 变量 | 用途 |
|------|------|
| `OLLAMA_BASE_URL` | 本地意图分类 (默认 http://localhost:11434) |
| `ESP32_IP` | IR 控制 (默认 192.168.1.7) |

## 4. 启动服务

```bash
# 启动 ASR 服务 (可选，不启动则用 Finna ASR)
python local_asr_server.py &

# 启动 Charlie 主服务
python voice_server.py
```

访问 http://localhost:8000 即可使用。

## 5. 公网访问 (Cloudflare Tunnel)

```bash
# 安装 cloudflared
brew install cloudflared  # macOS
# 或 apt install cloudflared  # Linux

# 启动隧道
cloudflared tunnel --url http://localhost:8000
```

会输出类似 `https://xxx-xxx.trycloudflare.com` 的公网地址。

## 6. ESP32 IR 控制 (可选)

### 硬件

- ESP32-S3 开发板
- 38kHz IR LED (GPIO 39)
- 杜邦线连接（无需焊接）

### 固件

ESP32 固件在 `esp32-smart-home` 项目中，支持：
- NEC 协议 IR 发送
- HTTP API: `POST /api/ir/send {"device":"tv","action":"power"}`
- MQTT 桥接 Home Assistant

### 支持的设备

| 设备 | 协议 | 地址码 | 命令码 |
|------|------|--------|--------|
| 小米电视 | NEC | 0x20DF | power: 0x10EF, vol+: 0x40BF, vol-: 0xC03F |
| 三菱空调 | NEC | 0xCB32 | 0x48B7 (toggle) |

### Tuya 万能红外 (推荐替代方案)

ESP32 IR LED 功率有限，推荐使用 Tuya 万能红外遥控器：
1. 买 Tuya 万能红外遥控器（~30元）
2. 用 Tuya App 学习所有遥控器码
3. 注册 Tuya 开发者平台获取 API 凭证
4. 修改 `mcp_ir_control.py` 对接 Tuya 云 API

## 7. Baize 目录说明

`Baize/` 是 [BaiZeAgent/Baize](https://github.com/BaiZeAgent/Baize) 项目的本地副本，作为 Charlie 的参考实现。

### 本地运行 Baize

```bash
cd Baize
npm install
cp .env.example .env  # 填入 API Key
npm start
```

详见 `Baize/README.md` 和 `Baize/docs/` 目录。

## 故障排查

| 问题 | 解决方案 |
|------|----------|
| 页面打开但语音不工作 | 检查浏览器是否 Chrome、是否授权麦克风、是否 HTTPS |
| ASR 无结果 | 检查 ASR_KEY 或 local_asr_server.py 是否运行 |
| TTS 无声音 | 检查 TTS_KEY，Finna 429 限流时等 30s 重试 |
| IR 控制无效 | 检查 ESP32 IP、IR LED 方向、设备协议匹配 |
| 意图分类错误 | 检查 Ollama 是否运行，或查看 logs/app.log |
| 公网无法访问 | 检查 cloudflared 进程是否存活 |

## 日志

```bash
# 实时查看日志
tail -f assistant-kid/logs/app.log

# 查看意图分类
grep "\[intent\]" logs/app.log | tail -20

# 查看错误
grep -i "error\|fail\|warn" logs/app.log | tail -20
```
