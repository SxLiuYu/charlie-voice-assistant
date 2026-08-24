# Charlie 语音助手

> 本地运行的私人 AI 语音助手。ASR→大脑→TTS 语音闭环，支持 ESP32 开发板终端、浏览器、飞书推送。

## 5 分钟快速上手

### 方式一：Windows 便携版（推荐，面向普通用户）

1. 到 [Release](https://github.com/SxLiuYu/charlie-voice-assistant/releases/latest) 下载 `Charlie-Portable.zip`
2. 解压到任意目录，双击 `charlie.exe`（原生窗口，无需装 Python）
3. 欢迎向导里填入免费的智谱 GLM Key（[注册即送，glm-4.7-flash 永久免费](https://open.bigmodel.cn)），保存即时生效
4. 说「几点了」→ Charlie 报时间

> 语音对话再填百度语音 Key（有免费额度），不填也能用文字对话。

### 方式二：Python 开发（面向开发者）

```bash
git clone <repo>
cd charlie/charlie
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-core.txt
# macOS: brew install ffmpeg   Linux: apt install ffmpeg   Windows: winget install ffmpeg
cp .env.example .env  # 填入你的 key；不填则 Demo 模式兜底
python voice_server.py
# 打开 http://localhost:8000
```

### 方式三：Docker

```bash
cp .env.example .env  # 填 key
docker compose up -d
```

## Demo 规则模式（零配置可用）

不填任何 key 也能和 Charlie 对话：

- ✅ 「几点了」→ 报时间
- ✅ 「晚安」→ 触发 goodnight 场景
- ✅ 「看看屏幕」→ 截屏分析
- ✅ 智能命令 / 场景 Protocol

无法完成的（需 key）：天气/翻译/记忆/飞书/空调/音乐。引导页选「完整模式」填 key 解锁。

## 密钥获取

| Key | 用途 | 获取地址 |
|---|---|---|
| GLM_KEY | 大脑 LLM（智谱 GLM-4.7-Flash，永久免费） | https://open.bigmodel.cn |
| BAIDU_APP_ID/API_KEY/SECRET_KEY | ASR + TTS（有免费额度） | https://console.bce.baidu.com/ai/#/ai/speech/overview/index |
| AMAP_KEY | 天气（可选） | https://console.amap.com |

可选：ARK_KEY（火山方舟，备选大脑）、FEISHU_APP_ID/SECRET（飞书推送）、TUYA_CLIENT_ID/ACCESS_KEY（空调）、TAVILY_API_KEY（搜索）、ALIYUN_API_KEY（购物分析）

启动后访问 `http://localhost:8000/setup` 用网页填写，按分组展示每个 key 状态。

## ESP32 开发板终端

Charlie 支持 ESP32 LC-S3 1.54 寸 TFT WiFi 开发板（xiaozhi 协议）：

1. 开发板插 USB 连电脑
2. 打开 `http://localhost:8000/esp32-setup`，选择串口烧录干净固件
3. 手机连设备热点 `lc-s3-wifi-1.54tft-XXXX`，访问 `http://192.168.4.1`
4. 选家里 WiFi、填入 OTA 地址（页面提供复制），保存后设备自动连 Charlie

> 烧录的是干净固件，WiFi/服务器地址由设备自带的 AP 热点配网门户写入，不重新编译固件。

## HTTPS 手机访问

```bash
python https_server.py
# 自动生成自签证书（首次）
# 手机同 WiFi 访问 https://<电脑-IP>:8443，首次需信任证书
```

## 配置项

所有 90 个环境变量在 `.env.example` 里，按分组注释。配置注册表在 `app/env_catalog.py`（单一来源）。

关键配置：
- `MCP_PROFILE=core`（默认 12 个核心 MCP）/ `all`（19 个）/ `custom`（读 MCP_SERVERS）
- `ASSISTANT_KID_HTTP_PORT=8000`（HTTP 端口）

## 项目结构

```
charlie/
├── voice_server.py      # FastAPI 主服务 (HTTP/WebSocket/SSE)
├── voice_agent.py       # 大脑引擎 (意图→LLM→MCP→TTS)
├── charlie_main.py      # PyInstaller 入口 (首次启动引导/原生窗口)
├── https_server.py      # HTTPS 副本 (手机访问)
├── app/
│   ├── env_catalog.py   # 环境变量注册表（单一来源）
│   ├── config.py        # 端口/LAN IP/CORS
│   ├── preflight.py     # 外部二进制检测
│   ├── mcp_gate.py      # MCP 分层 + key 缺失过滤
│   ├── cert.py          # HTTPS 证书自动生成
│   └── ...
├── web/                 # 前端 (voice/setup/welcome/esp32_setup)
├── scripts/             # gen-cert.sh, download-models.sh, check-leaks.sh
├── docs/                # 文档 (SPEC, DEPLOYMENT, DEMO_MODE)
└── tests/               # pytest 测试套件
```

> ESP32 固件 bin（16MB）位于仓库根目录 `firmware/`，打包时随便携版分发。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

产品化测试（TDD 覆盖）：
- `test_demo_rule_mode.py` — Demo 规则模式
- `test_lan_info.py` — LAN info API + OTA 动态端口
- `test_preflight.py` — 外部二进制检测
- `test_mcp_gate.py` — MCP 分层 + key 过滤
- `test_cert.py` — HTTPS 证书自动生成
- `test_model_download.py` — SenseVoice 模型下载
- `test_setup_api.py` — setup mcp-status API
- `test_welcome.py` — /welcome 引导页
- `test_esp32_wizard.py` — 烧录向导
- `test_charlie_main.py` — 首次启动检测
- `test_question_paths.py` — 智能命令/场景分支

## 许可证

MIT 开源，保留作者署名。
