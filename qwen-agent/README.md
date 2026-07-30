# 魔幻手机 (Magic Phone) · Qwen-Agent 语音大脑

中国版贾维斯：GLM-5.2 大脑 + 原生 MCP + ASR/TTS 语音闭环，一句话跨多个 MCP 工具完成任务。

## 架构
```
    ┌──────────────────────────────────────────────┐
    │  流式语音闭环 (SSE)                            │
    │  音频 → ASR → 大脑逐句产出 → TTS批量 → 逐句播报 │
    └──────────────────────────────────────────────┘
                      ┌──────────────────────────────┐
语音/文字输入          │  ASR(qwen3-asr-flash)         │
        └─────────────►│  → GLM-5.2+6MCP大脑(带记忆)   │──┐
                      │  → TTS(qwen3-tts-flash)       │  │
                      └──────────────────────────────┘  │
                      ↑ 流式: 逐句产出+TTS批量(50字/块)  │ 回复
后台调度器(30s) ──► 检查reminders.json ──► 到期? ──► TTS+afplay自动播报到AirPods
```
- **大脑**: Qwen-Agent + GLM-5.2(finna中转, use_raw_api走原生tool_calls)
- **对话记忆**: 跨请求保留多轮对话历史(最多20轮)，支持追问和上下文引用
- **语音**: qwen3-asr-flash / qwen3-tts-flash (finna, voice=Cherry)
- **协议**: 原生 MCP (标准 mcpServers 配置即插即用)
- **主动服务**: 后台线程每30s检查到期提醒，自动TTS合成语音+afplay播报到AirPods/扬声器
- **预热**: 服务启动后台预热大脑+6MCP(asyncio子线程修复)，首请求从12s→1.5s
- **连接韧性**: Session复用+3次重试+异常降级，API暂时不可用时自动恢复
- **双进程安全**: HTTP进程管理后台线程，HTTPS进程SKIP_BACKGROUND跳过(防重复播报)
- **文件锁**: reminders.json使用fcntl文件锁防竞态
- **CORS**: 允许跨域访问，手机/其他设备可直接调用API
- **请求日志**: 每个请求分配唯一ID+耗时统计

- **流式语音闭环(v3.0)**: 大脑逐句产出→检测句子边界→TTS批量推送(50字/块)→客户端逐句播报，首文本延迟从5s降至1.7s，首音频从8s降至3.5s
- **逗号软分割**: 句末标点优先切割，长句超35字在逗号处软切割，避免TTS碎片化
- **Markdown清理**: TTS前自动去除**粗体**、#标题、|表格、>引用等格式符号

## 6 个 MCP 工具
| MCP | 能力 | 数据 |
|-----|------|------|
| 高德地图 (amap-maps) | 天气/POI/路线/路况 | 真实 |
| baize-skills | 购物/翻译/提醒/系统监控/计算/生活服务deeplink | 真实 |
| Filesystem | 读写本地文件 | 真实 |
| Memory | 知识图谱记忆 | 真实 |
| Sequential-Thinking | 复杂任务拆解规划 | 真实 |
| magic-phone | 时间/充电桩(高德POI)/特斯拉空调(模拟) | 充电桩真实/特斯拉模拟 |

## 配置
所有API密钥集中管理在 `.env` 文件（已被 .gitignore 保护，不会提交到git）：
```env
GLM_KEY=app-xxx          # GLM-5.2大脑
TTS_KEY=app-xxx          # qwen3-tts-flash
ASR_KEY=app-xxx          # qwen3-asr-flash
AMAP_KEY=xxx              # 高德地图
TAVILY_API_KEY=tvly-xxx   # Tavily搜索
ALIYUN_API_KEY=sk-xxx    # 阿里云(购物分析)
FINNA_BASE=https://...   # finna API基址
GLM_MODEL=glm-5.2        # 模型名
TTS_VOICE=Cherry         # TTS音色
```
所有Python文件启动时自动 `load_dotenv()`，无需手动 export。

## 使用
```bash
cd qwen-agent && source .venv/bin/activate

# 1. 交互式语音CLI(绕过浏览器权限)
python magic_phone_cli.py

# 2. HTTP服务(Mac本机)
python voice_server.py           # http://localhost:8000

# 3. HTTPS服务(手机同WiFi)
python https_server.py           # https://sxliuyudeMac-mini.local:8443

# 重启
pkill -9 -f "voice_server\|https_server"; sleep 2
screen -dmS voice bash -c 'source .venv/bin/activate && python voice_server.py'
screen -dmS voice-https bash -c 'source .venv/bin/activate && python https_server.py'

# 启动看门狗(60秒健康检查+自动重启+日志轮转)
screen -dmS watchdog bash watchdog.sh
```

## API 端点 (12个, Swagger文档: /docs)
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web客户端(语音VAD+文字+提醒+历史+新对话) |
| POST | `/api/voice` | 音频进→ASR→大脑→TTS→JSON |
| POST | `/api/chat` | 纯文字对话(带记忆) |
| POST | `/api/tts` | 文字→WAV语音(独立TTS) |
| POST | `/api/asr` | 音频→文字(独立ASR) |
| POST | `/api/reset` | 清空对话历史 |
| GET | `/api/conversation` | 获取对话历史 |
| GET | `/api/status` | 系统状态(CPU/内存/磁盘/提醒) |
| GET | `/api/version` | 版本信息+功能列表 |
| GET | `/api/export` | 导出对话历史(文本) |
| GET | `/api/notifications` | 获取并清空通知队列(Web轮询用) |
| GET | `/api/search?q=xxx` | 搜索对话历史 |
| GET | `/manifest.json` | PWA移动端manifest |
| GET | `/api/reminders` | 提醒列表 |
| POST | `/api/reminders` | 添加提醒 |
| DELETE | `/api/reminders/{id}` | 标记提醒完成 |
| GET | `/dashboard` | 监控面板(CPU/内存/提醒/大脑状态) |
| GET | `/health` | 健康检查 |

## 自测
```bash
python test_system.py          # 15项测试, 45秒完成(含35s通知队列测试)
python test_system.py https://sxliuyudeMac-mini.local:8443  # 测HTTPS
python test_system.py https://sxliuyudeMac-mini.local:8443  # 测HTTPS
```

## 主动提醒
- 后台调度器: 服务启动自动启动守护线程，每30s检查 `reminders.json`
- 到期播报: 到期提醒自动调TTS生成"主人，提醒您：{内容}"→afplay播报到AirPods
- 语音添加: 对话说"提醒我明天10点开会"→大脑调add_reminder MCP→自动存入→到期播报
- Web管理: 浏览器面板可直接添加/查看/完成提醒

## 主动建议系统（贾维斯灵魂）
- **天气感知**: 每小时检查高德天气API，下雨/下雪自动播报"主人，今天有雨，记得带伞"
- **每日晨报**: 每天8:00自动播报天气+今日待办清单
- **作息提醒**: 23:00自动播报"主人，该休息了"
- **系统监控**: CPU>90%或内存>95%自动告警"系统资源紧张，建议关闭程序"
- 所有建议通过TTS合成语音+afplay播报到AirPods/扬声器，不打扰当前对话

## 已验证场景
- ✅ "北京天气" → 高德真实天气
- ✅ "搜北京充电桩" → 高德真实POI(开迈斯/国家电网)
- ✅ 多轮对话记忆: "我叫刘宇"→"我叫什么?"→"帮我提醒遛猫"→"我刚才让你做什么?" 正确回忆
- ✅ 主动提醒: 插入过期提醒→30s内自动TTS+afplay播报到AirPods
- ✅ Warmup: 预热后首请求1.5s(vs未预热12s)
- ✅ system_status: 真实CPU/内存/磁盘(psutil)
- ✅ /api/status: 实时系统状态
- ✅ Web: ASR识别文本显示+新对话按钮+提醒面板
- ✅ 对话持久化: 重启后保留对话历史(conversation_history.json)
- ✅ 主动天气建议: 启动即检测到雷阵雨→自动TTS播报"记得带伞"
- ✅ 每日晨报: 8:00天气+待办清单
- ✅ system_status(通过MCP): 大脑调system_status返回真实CPU/内存/运行时间
- ✅ 连接韧性: 3次连续请求全部成功(5.3s→4.2s→2.4s递减)
- ✅ 双进程修复: HTTP=1调度器, HTTPS=0调度器(不再重复播报)
- ✅ TTS/ASR独立端点: /api/tts + /api/asr, 往返测试"你好世界"→正确识别
- ✅ CORS: access-control-allow-origin: * 
- ✅ 请求日志: [request_id] METHOD /path → status duration_ms
- ✅ 系统自测: 10/10全通过(test_system.py, 13秒)
- ✅ .env集中管理密钥: 8个密钥全部正确加载，.gitignore保护
- ✅ MCP子进程.env加载: baize-skills calculate/system_status 通过管道验证
- ✅ 无DeprecationWarning: on_event→lifespan上下文管理器
- ✅ Swagger文档: /docs 自动生成, 15个端点
- ✅ 监控面板: /dashboard 10秒自动刷新
- ✅ 对话导出: /api/export 下载对话历史
- ✅ Jarvis人格: 增强系统提示词(主动设提醒+上下文感知)
- ✅ 线程池: asyncio.to_thread不阻塞事件循环, 支持并发请求
- ✅ 超时保护: chat30s/voice60s/tts30s/asr30s, 超时返回504
- ✅ MP3压缩: WAV→MP3(32kbps), 8-10x更小, 减少网络传输
- ✅ 通知队列: 主动通知(提醒/天气/晨报)存入队列, Web客户端15s轮询获取
- ✅ 动态上下文: 系统提示词含今日待办数, 大脑知道用户日程
- ✅ 响应缓存: 60s TTL, 重复查询170x加速(2.6s→0.015s)
- ✅ 自动重启: 看门狗60s检查, 服务挂了自动重启
- ✅ 日志轮转: >10MB自动截断5000行, 防止无限增长

## 关键文件
| 文件 | 说明 |
|------|------|
| `voice_agent.py` | 语音闭环(tts/asr/brain/voice_loop)+对话记忆+预热 |
| `voice_server.py` | FastAPI服务+主动提醒调度器+预热+系统状态+提醒API |
| `https_server.py` | HTTPS服务(手机用) |
| `magic_phone_cli.py` | 交互式语音CLI |
| `baize_skills_mcp.py` | 白泽MCP(7工具:购物/翻译/提醒/系统监控/计算/deeplink) |
| `mcp_server.py` | magic-phone MCP(时间/充电桩真实/特斯拉模拟) |
| `reminders.json` | 提醒数据 |
| `cert/` | HTTPS自签证书 |
| `web/voice.html` | Web客户端(语音VAD+文字+TTS+提醒+历史+新对话) |
| `test_system.py` | 系统自测脚本(10项验证, 13秒) |
| `conversation_history.json` | 对话历史持久化 |
| `suggestions_state.json` | 主动建议状态(防重复播报) |

## 待办
- [ ] 特斯拉真实控制(需Tesla Developer授权)
- [ ] 流式响应降延迟(当前同步ASR→大脑→TTS约5-15s，可改并行)
- [ ] 开发板多端(树莓派直接跑/ESP32瘦客户端)
- [ ] 外卖购物商品搜索(京东union API需授权认证)
