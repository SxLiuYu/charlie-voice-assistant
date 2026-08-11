# RTK (Run-Time Knowledge) — Charlie 语音助手

> 最后更新: 2026-08-10 | 代码行: ~13,500 | 服务状态: 运行中 (PID 47112, watchdog 30253 托管) | 测试: 51 全绿 (43 question_paths + 8 xiaozhi_ws) | brain: 就绪 | ESP32 终端: 已接入 (xiaozhi 协议, 流式逐句流水线)

## 功能总览 (2026-08-07 全量梳理)

### 1. 语音交互
- 语音闭环 `/api/voice`：ASR→意图→大脑→TTS→音频返回 (10MB上限/60s超时降级)
- 流式语音 `/api/voice/stream`：asr→ack→逐句text→audio→done (首句立即播放, 其余末段批量)
- 流式文字 `/api/chat/stream` + 一次性 `/api/chat`
- ESP32 硬件终端 `/ws/xiaozhi`：手表/机器人接入，Opus 双向音频 + 流式逐句流水线
- WebSocket 双工：打断(barge-in)/位置上报/剪贴板/跨终端会话同步
- 短路优化：静音/空ASR/低意图语气词/乱码5规则 → 不消耗大脑
- 唤醒：浏览器 Web Speech API(前台) + 本地 Vosk(Silero VAD+TTS中断) + `/api/wakecheck`(ESP32)
- 音频兼容：webm/mp4/wav→ffmpeg 16k mono；输出 32k mp3

### 2. 大脑与智能
- 意图分类：25类关键词预判→ARK LLM→Ollama 降级, LRU缓存+熔断
- 19个 MCP 按意图路由 (Qwen-Agent 子进程 + system prompt 工具注入)
- 快路径绕过 LLM：时间直答/音乐直连(ncm)/视觉截屏/场景Protocol/智能命令(音量±/静音/睡眠)
- 熔断金字塔：ARK→Ollama qwen3.5:2b→固定文案; 大脑5次失败重建(429特判10次)
- OpenAI SDK 兼容层 (ARK私有参数 extra_body / tool_call_id 修补)
- 多用户隔离 `CHARLIE_USER_ID` + 会话隔离 `session_id`

### 3. 记忆与偏好
- 对话历史：20轮/会话/10会话/token裁剪(4000)/跨会话摘要/flock+原子写
- 偏好系统：CRUD+ETag 304+多进程锁+revision 冲突检测
- 叙事记忆：bigram TF-IDF 检索/修正/去重/7类事件提取(上限200)
- system prompt 实时注入：今日待办+摘要+偏好+进化画像+相关记忆
- 自进化：话题分布/活跃时段/回复风格/打断率调优/偏好建议

### 4. 主动服务
- 定时提醒：一次/每天/每周/工作日, 3次退避(60/180/600s)+崩溃恢复(900s)
- 提醒同步语音播报 (macOS afplay / Linux SSE)
- 主动建议8类：天气/睡眠/离家/回家/晨报/健康/下班/食物 (机器级锁防重复)
- 决策引擎7规则：晚睡/晨起/会议/离家/截止/晚间休息/午餐, 24h冷却+反馈闭环+60s确认窗口
- 场景Protocol：内置4+自定义(LLM解析步骤), 8种步骤执行器
- 每日/昨日/周摘要 (对话+决策+记忆聚合)

### 5. MCP 技能 (19个)
信息(时间/天气/新闻/翻译/安全计算/代码沙箱)、音乐(ncm+白噪音+闹钟)、提醒、备忘录/购物、系统控制、生活(外卖/充电桩)、浏览器(ego-browser)、13个App网页版、飞书、抖音、淘宝京东比价、搜索(Tavily/wigolo/Bing)、红外(Tuya)、穿搭、记忆/决策/场景/进化/摘要

### 6. 上下文感知
- 用户状态机6态 (home_awake/resting/sleeping/away/working): GPS+屏幕活跃+语音+ARP存在检测
- 打断上下文延续：被中断回复作为下轮上下文 (200字截断)

### 7. 安全与运维
- AUTH_TOKEN 认证(本地免token, 防XFF伪造) + WS query token
- 限流三层：IP 60/min + 语音10/min + 会话30/min (Retry-After)
- XSS清洗(全端点+WS) / 请求体15MB / 音频10MB
- 指标Metrics/遥测(轮询/打断) + ETag/304 + 弱ETag
- /dashboard HTML监控 + /api/status 综合状态 + /health
- 看门狗自动重启/日志轮转 + Cloudflare 隧道 + 结构化日志

### 8. 前端与部署
- voice.html(语音球/音量/打断/主题/导出) + manage.html 5面板 + setup + 测试台 + dashboard
- PWA：manifest + service worker + push 通知
- 对话导出 txt/md/json+日期过滤 / 搜索 / 分页
- Docker/Compose、PyInstaller(ffmpeg+ncm)、HTTPS自签、CI稳定集

## 项目身份

| 属性 | 值 |
|---|---|
| 项目名 | Charlie |
| 曾用名 | 助手小子 (assistant-kid), 魔幻手机 |
| 位置 | `/Users/sxliuyu/orca/projects/charlie/` |
| 代码目录 | `/Users/sxliuyu/orca/projects/charlie/charlie/` |
| 大脑模型 | deepseek-v4-flash (火山引擎 ARK) + Qwen-Agent + MCP 工具 (意图路由按需加载) |
| 语音引擎 | 百度 ASR (dev_pid=1537) + 百度 TTS (aue=6 MP3, per=3) |
| 本地降级 | Ollama qwen3.5:2b (对话/意图双降级) + Vosk (离线 ASR/唤醒词) |
| 前端 | 纯 HTML/JS (voice.html 690行 / manage.html 285行 / setup.html / voice_test.html), PWA, dashboard HTML |
| 容器 | Docker (Dockerfile + docker-compose.yml) |
| 桌面应用 | PyInstaller (charlie.spec + build.sh + charlie_main.py 入口) |
| 物联网 | Tuya 云 API: 2C 终端用户(TuyaAPI 设备列表/详情/属性) + 2B 开发者红外(TuyaCloudAPI 空调红外发码); tuya_api.py |
| 浏览器 | ego-browser CLI (magic-browser / magic-apps 复用登录态) |
| 衣橱 | `/Users/sxliuyu/wardrobe/` — 电子衣橱配套项目 |
| 版本 | 3.1.0 (by /api/version) |

## 架构

```
浏览器 (voice.html / manage.html / dashboard / /test)
    │ HTTP POST(音频/文字) / WebSocket / SSE(EventSource)
    ▼
voice_server.py (FastAPI, port 8000, 2723 行)
    ├─ app/ — 子模块 (state/config/auth/audio/brain_health/reminders/routes/system)
    ├─ 中间件链: 限流+认证(DynamicCORS 内层→request_logger 外层)→body 大小(15MB)→全局异常
    │
    ├─ voice_agent.py — 核心引擎 (2589 行)   ← voice_server 及所有进程共用
    │   ├─ 输入过滤: is_low_intent_asr / is_garbled_asr (5条规则) 短路大脑
    │   ├─ 意图分类: 关键词表(25条) → ARK LLM → Ollama 降级, LRU+TTL 缓存+熔断
    │   ├─ Brain: Qwen-Agent Assistant + 19 MCP 子进程 (意图路由加载), 熔断/降级金字塔
    │   ├─ brain() 快路径: 智能命令→决策反馈→时间→音乐→视觉→缓存→意图→Protocol→LLM
    │   ├─ brain_stream_sentences(): 逐句产出(。！？；/逗号软切), 可注入被打断上下文
    │   ├─ tts: baidu MP3 + mp3缓存 + 熔断(TTSUnavailableError) + 降级文字
    │   ├─ asr: baidu → vosk 离线, 30s窗口>3次降级跳过
    │   ├─ 记忆注入: 待办/偏好/画像(evolution_data)/叙事记忆(magic-memory) → _build_system_msg
    │   └─ 用户状态机 _infer_user_state + 智能命令(_handle_smart_command)
    │
    ├─ 后台线程 (lifespan 启动, SKIP_BACKGROUND=1跳过):
    │   ├─ _reminder_scheduler      — 30s, 机器级锁, 同步播放提醒音频
    │   ├─ _proactive_suggestions   — 60s, 天气/睡眠/离家/晨报/健康/偏好 建议
    │   ├─ _evolve_loop             — 1800s, brain 学习偏好
    │   ├─ _decision_loop           — 120s, 自主决策引擎 evaluate
    │   ├─ _wake_listener           — 一次, 接入 local_wake (Vosk+Silero VAD)
    │   └─ _cleanup_loop            — 60s, 清理 stale WS 连接
    │
    └─ HTTPS 副本 (https_server.py, port 8443, SKIP_BACKGROUND=1, 证书自签)
```

## 数据流 (语音/文字闭环)

```
HTTP POST /api/voice(/stream) 或 WS audio
  → UploadFile/FormData (audio/webm 或 mp4)
  → app.audio.to_wav(ffmpeg 16k mono, 失败静默降级返回原始)
  → likely_empty_audio 静音判定(≥3s + active<0.25s + ratio<0.08 → 短路)
  → voice_agent.asr (iaidu→vosk; token 28天缓存)
     - 空ASR → (未识别到语音, 抱歉…, b"")
     - 低意图(纯语气词≤16字) → (text, "嗯嗯，我在。", b"")
     - 乱码 is_garbled_asr → 短路事件流
  → _classify_intent(mcp_set) → _get_brain(mcp_set)
     (快捷命令/决策反馈/时间/音乐/视觉 直连已在 brain() 前短路)
  → brain / brain_stream_sentences → 逐句文本
  → Text→_tts_cleaned_to_mp3(缓存→baidu→ffmpeg 32k mp3)
  → SSE / WS 事件流
WS /ws 额外插件: interrupt(barge-in)/location(状态机)/clipboard/跨终端广播
```

## WebSocket /ws 协议 (voice_server.py:1906-2128)

| 方向 | type | 说明 |
|---|---|---|
| C→S | `ping` | 回 `pong` |
| C→S | `location` | {lat,lng,accuracy}→`update_user_state`, 反地理编码, 回 `location_ack`+address |
| C→S | `interrupt` (with interrupted_reply) | 取消当前流 + InterruptTelemetry.record, 回 `interrupted` |
| C→S | `text` | ≤500字, 取打断上下文 record_follow_up, 建流任务 `_ws_stream_and_send` |
| C→S | `audio` | ≤10MB, 静音/空/低意图/乱码短路; 内联 ASR → `asr`+`ack`("嗯，让我想想") → 流式任务 |
| C→S | `discover` / `clipboard` | (仅日志/discover_ack) |
| S→C | `connect`/`asr`/`ack`/`text`/`audio`/`done`/`error`/`warning`/`pong`/`ping`/`interrupted`/`location_ack`/`discover_ack` | |
- **认证**: 仅当 `AUTH_TOKEN` 非空且对端非本地 (peer非127/::1) 才校验 `?token=`
- **串行流**: 每连接仅一个 stream_task; 新的 text/audio 先取消旧的 (interrupt flag + task.cancel())
- **同一 session 跨终端广播**: text/audio/done/error 广播给同 session 其它 ws_id (exclude_self)
- **stale 清理**: 300s 无活动 → 关闭; 空闲120s 发 ping 探活

## SSE 机制 (voice_server.py)

| 端点 | 说明 |
|---|---|
| `/api/chat/stream` | SSE: `text`* → `audio`*(首句立即flush, 其余累积成末段) → `done`; 心跳注释帧 |
| `/api/voice/stream` | 短路(静音/空/低意图/乱码) 事件流; 正常先 `asr`→`ack` 再如上 |
| `/api/events` | 通知 SSE(30s 心跳JSON帧), 提醒/天气/睡眠/离家/晨报/决策走 `_add_notification` |
| 常量 | `ACK_AFTER_ASR_MESSAGE="嗯，让我想想"` (L100), `_SSE_DONE_FRAME`, `_SSE_HEARTBEAT_FRAME` |

事件 `audio` 格式: `{"type":"audio","audio":<mp3-b64>}` (SSE) 或 `{"type":"audio","data":<b64>}` (WS)。

## ESP32 终端接入 (xiaozhi 协议, app/xiaozhi_ws.py + app/xiaozhi_codec.py)

```
SPI32手表/机器人 (xiaozhi-esp32 固件, LC-S3 1.54TFT 板型)
    │ GET /xiaozhi/ota (返回 ws://host:8000/ws/xiaozhi, 跳过激活)
    ▼
voice_server.py → register_xiaozhi_routes(app)
    ├─ /ws/xiaozhi (WebSocket): hello/listen/abort/ping/wake + Opus 音频
    ├─ 上行: 16kHz Opus → opus_decode_to_wav → 静音端点检测(滚动tail+自动断句) → 百度 ASR
    ├─ 下行: brain_stream_sentences 逐句 → finna TTS(SSE)→baidu fallback → mp3→24kHz Opus (60ms帧)
    │        → 按设备节奏播放(HEAD_START_FRAMES=6, 发送队列≤40包)
    └─ 音乐: __MUSIC__url__name__artist → url_to_opus_packets 下载真歌转Opus (每首≤30s/500帧)
```

- **流式逐句流水线** (2026-08-09): `stream_tts_reply` 取消 collect() 全量等待，
  改为 Qwen-Agent 后台线程逐句产出 → asyncio.Queue → 首句到达立即 tts-start+合成播放，边生成边播。
  首帧延迟从 ~4.6s → ~2.3s（多句回复「你好」实测 1.18s 首句产出 / 2.28s 开始播放）。
- **端点检测**: `SILENCE_RMS=150` / `SILENCE_FRAMES=30`(1.8s) / `MIN_SPEECH_FRAMES=18` /
  `MAX_UTTERANCE_FRAMES=600`(36s); 断连清理。
  - 2026-08-09 加固: `NOISE_DROP_FRAMES=200`(12s) 未达MIN_SPEECH即静默丢弃环境噪音,
    不再白等36s; 端点日志区分 `capped/noise_drop` 双因。真机验证: 唤醒后静默态 rms≈30(<150) 无循环。
  - 2026-08-09 二次**静默策略**: 没人说话就闭嘴 —— 空/乱码 ASR、无足够 speech 的端点
    统一静默丢弃,移除「我没听清」提示与 HINT_COOLDOWN; listen stop 同用 MIN_SPEECH 判定。
    真机验证 57帧「帮我报个时间」→ 1.32s 首句。集成测试改为断言静默。
- **ASR 过滤** (2026-08-09, agent/intent.py + voice_agent.py): 纯语气词(嗯/哦/huh/啊) →
  回「嗯嗯，我在」(不触发大脑); 乱码/无有效指令 → 回「我没听清楚，能再说一遍吗?」;
  英文垃圾正则 `h+m+ u+h+ a+h+ h+e+v?y*o* y+e+a+hn?`。修复 voice_agent 本地重复
  `is_garbled_asr` 引用未定义常量 `_VALID_SHORT_TEXTS` 的 NameError (统一走 agent.intent)。
  实测 12/12: 6 垃圾词全拦截 + 6 合法指令零误杀; 真机触发 `no clear speech → hint`。
- **TTS 男声 + 缓存** (2026-08-09): 全链路由女声 Cherry → Finna `Ethan`(晨煦,中文男声),
  `TTS_VOICE` env 可覆盖; xiaozhi 下行与 asr_tts 统一同一常量。`_synth_mp3` 结果写入
  共享 TTS mp3 缓存(命中跳过网络往返); opus 转码按 sha256(mp3) 缓存(命中跳过 ffmpeg+opus);
  开机预热「你好」「嗯嗯我在」两常用短语。
- 测试: tests/test_xiaozhi_ws.py (集成, 需真机/模拟WS); 模拟客户端 /tmp/xiaozhi_sim2.py。
- 固件细节见 esp32-charlie-terminal 仓库 PROJECT_NOTES.md。

## 意图分类 `_classify_intent` (voice_agent.py:1529)

1. **LRU+TTL 缓存** (100条/3600s)
2. **熔断**: 失败≥2次 → 冷却30s
3. **闲聊短句** (≤6字且不含领域词 → "none", 跳过LLM)
4. **25条关键词表**: amap-maps×2 / magic-info×4 / magic-system×2 / magic-scenes×3 + music/reminder/notes/ac-control/filesystem/life/evolution/taobao/browser/apps/feishu/douyin/baize-skills/mimo-vision
5. **ARK LLM** (max_tokens=10, temp=0, no thinking, timeout=(3,10))
6. **后处理映射链** → 兜底 "none"
7. **降级** → Ollama qwen3.5:2b (简化映射) → "none"

## TTS / ASR 健康与熔断

- `tts_status()`: 缓存命中/熔断状态/剩余冷却秒 (挂在 /api/status 与 dashboard)
- TTS失败TaCooldown 120s (fail-closed, 首次失败即断), 成功清零
- 上游降级: voice_loop 捕获 TTSUnavailableError → audio_out=b"" 显示文字
- ASR: 百度失败(30s窗口>3次) → 跳过 → Vosk 离线 (模型 web/vosk/vosk-model-small-en-us-0.15)
- 百度 token 缓存磁盘 `.baidu_token.json` + 28天 TTL

## 记忆/缓存/文件体系

| 名称 | key | TTL/上限 | 锁 | 路径 |
|---|---|---|---|---|
| 对话缓存 | text 或 `interrupted:` 前缀 | 60s / 50条 FIFO | 内存锁 | 内存 |
| 意图缓存 | 原始文本 | 3600s / 100 | 锁 | 内存 |
| TTS mp3 缓存 | (text,voice,model) | 3600s / ≤20字 · 50条 | GIL | 内存 |
| 历史 | {session:[msgs]} | 40条/会话 | _history_lock + fcntl flock(SH/EX) + save_seq + 签名 | `DATA_DIR/conversation_history[_uid].json` |
| 偏好 | dict | — | _prefs_lock(RLock) + flock + revision + 签名 | `DATA_DIR/preferences.json` |
| 百度 token | {token,at} | 28d | 锁 | `DATA_DIR/.baidu_token.json` |
| 叙事记忆 | 每条约 {timestamp,tags,bigrams,summary} | 200条 | threading.Lock | `episodic_memories[_uid].json` |
| 提醒 | list | 7d清理 | _locked_reminders flock | `reminders.json` |

磁盘 JSON 一律 `NamedTemporaryFile → fsync → os.replace` 原子替换。

### 原子/跨进程设计
- 写路径均持排他 fcntl.flock (跨进程/线程) + 内存锁 (同进程), 临时文件+fsync+os.replace 保证崩溃恢复
- `_searchable_history` 磁盘回读: 内存快照为空(重启后) 时才读盘, 不持 `_history_lock` (避免阻塞写入)
- 偏好: `_save_preferences` 磁盘为主+内存覆盖合并; `_commit_preferences` 排他锁 mutate (MCP 工具写路径)

## 并发/线程安全 (voice_agent 全量锁清单)

带锁: `_baidu_token_lock` `_cache_lock` `_history_lock` `_history_file_lock` `_prefs_lock`(RLock, 防重入) `_preferences_file_lock` `_intent_cache_lock` `_user_state_lock` — 全局状态另 fcntl flock 跨进程。
无锁(GIL only): `_tts_cache` `_tts_failures/_unavailable_until` `_brains`/`_brain_failures` `_intent_failures` `_asr_fallback_times` `_current_user_input` 等 — **潜在并发区注意**
后台: `_remember_conversation_async`（daemon）写记忆; Qwen-Agent 管理 MCP 子进程生命周期。

## 用户状态机 (voice_agent.py:1442-1525)

状态: unknown / home_awake / home_resting / home_sleeping / away / working
判定: GPS(0.01°≈1km内) + presence.detect_devices + 屏幕/语音活动/时段:
- 无位置 → 在家默认; 距家>1km → away
- 深夜(22-6)+30min无语音 → home_resting (0.6); +60min → sleeping (0.7)
- 白天+屏幕<5min+语音<10min → working (0.7); 无语音30min → home_resting (0.5)
Presence: ARP + Ping(30s缓存) 置信度 confidence>0.5 判在家。

## 决策引擎 (magic-decisions.py:472行) + 场景 Protocol (magic-scenes.py:520行)

| 规则 | 优先级 | 触发 | 动作 |
|---|---|---|---|
| late_night_sleep | 90 | 晚睡状态+22-6 | goodnight Protocol |
| morning_wakeup | 80 | 早醒+7-9 | good_morning Protocol |
| meeting_reminder | 75 | any+任意 | 飞书日历核查→TTS提醒 |
| leaving_reminder | 70 | away+6-23 | leaving_home Protocol |
| deadline_reminder | 60 | any | 记忆系统截止→TTS |
| evening_wind_down | 50 | 21-23 | TTS提醒休息(需确认) |
| lunch_reminder | 40 | 11-13 | TTS提醒吃饭(需确认) |

- 24h 冷却 + 负面反馈>60%跳过 + pending_confirmation.json 60s窗口
- **Protocol 步骤执行器 8种**: ac_control/tv_control/volume/reminder/tts/wait≤30s/if_condition/llm
- 内置场景: goodnight/good_morning/movie_time/leaving_home; 自定义 protocols.json (learn_protocol 支持 LLM 解析步骤)

## MCP 工具集(19个) — 实测注册方式(与直觉不同)

**全部是官方 FastMCP + `@mcp.tool()` + `mcp.run()`(stdio)**, 由 voice_agent 子进程拉起:
```
Qwen-Agent Assistant(llm=ARK, function_list=[{"mcpServers":{...}}])
  → 每服务器 {"command": sys.executable, "args": ["magic-xxx.py"], "cwd": 项目目录}
  → mcp_set 根据意图路由只加载对应 MCP (默认17个, env MCP_SERVERS 覆盖)
  → 非 frozen: 直接 python magic-xxx.py; frozen 用 --mcp name (charlie_main.py)
```
MCP 特例: `amap-maps` 与 `magic-info`同文件 magic-info.py; `filesystem` 指向 magic-notes.py; `ac-control` = mcp_ir_control.py (默认不启用列表内).
**特殊**: magic-memory / magic-decisions 不走 stdio — 被 voice_agent 直接 `importlib` 加载为内嵌库 (format_memories_for_prompt / evaluate)。

| MCP | 文件/行 | 工具 | 依赖 |
|---|---|---|---|
| magic-memory | 373 | recall/memory_status/correct/dedup/forget + remember_conversation(内嵌) | 无, 本地 bigram TF-IDF |
| magic-decisions | 472 | evaluate/execute_decision/decisions_summary(非工具) | 飞书日历(可选) |
| magic-scenes | 520 | goodnight/good_morning/movie_time/leaving_home/learn_protocol/list/execute_scene | ESP32(/api/ir/send)+高德+ARK |
| magic-evolution | 372 | learn_from_history/self_optimize/suggest_preferences/evolution_status | 本地读取历史 |
| magic-summary | 211 | daily_summary/yesterday_summary/weekly_summary | ARK LLM |
| magic-wardrobe | 305 | outfit_recommend/wardrobe_list/add_clothing/remove_clothing | 高德/wttr.in/ARK; 外部 /Users/sxliuyu/wardrobe |
| magic-info | 235 | time/weather/news(Bing爬取)/location(ESP32)/translate/calculate/run_code | 高德+Bing+DashScope+ESP32 |
| magic-music | 256 | search/play/play_random/playlist/play_playlist/white_noise/set_alarm | ncm CLI (`~/.local/bin/ncm`) |
| magic-reminder | 149 | add_reminder/set_timer/schedule_task/list_alarms | app.reminders + Calendar(可选) |
| magic-notes | 124 | save_note/list_notes/shopping_items | 纯本地 notes/*.md |
| magic-system | 79 | set_volume/set_speech_speed/system_status | osascript/psutil |
| magic-life | 134 | open_lifestyle_app/search_charging_stations/control_tesla(class pseudo)/leaving_home | 高德+ESP32+app |
| magic-browser | 201 | browse/read/click/fill/screenshot/search — ego-browser nodejs CLI | 用户登录态 |
| magic-apps | 239 | wechat/alipay/douyin/taobao/jd/pdd/maoyan/damai/xianyu/feishu — ego-browser | 同上 |
| magic-feishu | 160 | search_docs/send_message/list_messages/get_calendar | 飞书开放平台 |
| magic-douyin | 118 | search_videos/get_trending/get_video_info | 抖音网页API |
| magic-taobao | 134 | search_products/search_jd/compare_price (HTML正则) | 无API |
| baize-skills | 184 | web_search_free / web_search / shopping_search / deep_research(死代码) | Tavily+DashScope+wigolo+Bing |
| ac-control | 100 | ac_control/ac_status | Tuya 2B 红外云 API (TuyaCloudAPI.ac_scenes_command 单键端点分步发码 + ac_status 查询) |

**协议字符串**: magic-music 返回 `__MUSIC__{url}__{name}__{artist}` / `__MUSIC_STOP__` 由 voice_agent 解析 → 前端 playMusicUrl 播放。

## 后端常规工具 (非 MCP)

| 文件 | 行 | 职责 |
|---|---|---|
| tuya_api.py | 920 | TuyaAPI(2C 终端用户: 设备/属性下发/天气/IPC缩略) + TuyaCloudAPI(2B 开发者: HMAC-SHA256 签名 sign含body SHA256 + 红外空调 ac_scenes_command/ac_status/ac_remote) + CLI `python tuya_api.py control <dev> <props>` |
| tuya_proxy.py | 69 | `/api/tuya` FastAPI 5端点 (devices/ac/control) |
| utils.py | 160 | parse_time_str(中文时间)/cleanup_temp_files/truncate_history_file/sanitize_error |
| magic_phone_cli.py | 144 | 录音 CLI → voice_loop → afplay (麦克风/airpods自动探测) |
| mcp_common.py | 66 | 共享: dotenv/aliyun_chat/_safe_math_eval(AT安全AST)/_ensure_https |

## API 端点全表 (voice_server 主 app)

| 端点 | 方法 | 行号 | 说明 |
|---|---|---|---|
| `/api/voice` | POST | 1141 | 音频闭环 (10MB→413, 内部 voice_loop to_thread 60s→504) |
| `/api/chat/stream` | POST | 1380 | SSE 文字流 |
| `/api/voice/stream` | POST | 1389 | SSE 音频流, asr→ack→text→audio→done |
| `/api/chat` | POST | 1437 | 一次性文字, 60s→504, 失败降级(reply+degraded) |
| `/api/reset` | POST | 1455 | 清会话历史 |
| `/api/reminders` | GET/POST | 1463 | 列表/添加 (weak ETag 304) |
| `/api/reminders/{id}` | DELETE | 1498 | 完成 |
| `/api/conversation` | GET | 1504 | 分页历史 |
| `/api/tts` / `/api/asr` | POST | 1525/1540 | 文字→MP3 / 音频→文字 |
| `/api/wakecheck` | POST | 1576 | **Vosk 唤醒词检测(ESP32等外部设备用, Vosk识别)`** |
| `/api/export` | GET | 1631 | 导出 txt/md/json + 日期过滤 |
| `/api/notifications` | GET | 1702 | 轮询通知(读后清) |
| `/api/events` | GET | 1708 | SSE 通知流 |
| `/api/search` | GET | 2199 | 历史搜索(评分+高亮) |
| `/manifest.json` `/service-worker.js` `/icon.svg` `/favicon.ico` | GET | 2249-2526 | PWA 静态 (ETag+304+HEAD) |
| `/api/brain/restart` | POST | 2279 | 重启大脑(MCP连接) |
| `/api/metrics` | GET | 2288 | 指标+ETag (排除自身) |
| `/api/preferences` | GET/POST/DELETE | 2304-2330 | 偏好 CRUD (条件ETag 304) |
| `/api/sessions` | GET | 2334 | 会话摘要 |
| `/api/context` | GET | 2341 | 上下文token预算+摘要 |
| `/api/decisions` `/api/memory` `/api/tunnel` | GET | 2360/2386/2400 | 决策/记忆/隧道状态 |
| `/health` | GET | 2427 | 健康检查 |
| `/` `/manage` `/test` `/setup` | GET | 2482-2573 | 页面 |
| `/api/setup` | GET/POST | 2579-2611 | 读/写 .env (白名单键) |
| `/api/protocols` `/api/protocols/learn` | GET/POST | 2613/2637 | 场景协议 |
| `/api/evolution` `/api/evolution/learn` | GET/POST | 2656/2681 | 进化状态 |
| `/api/wake/toggle` `/api/wake/status` | POST/GET | 2694/2701 | 唤醒开关 |
| `/api/user/switch` `/api/user/current` | POST/GET | 2707/2715 | 多用户 |
| **system_router**: `/api/status` `/api/version` `/api/polling-telemetry`(202) `/dashboard` | | | 综合状态/版本/轮询遥测/HTML监控 |
| **tuya_router**: `/api/tuya/devices[/{id}]` `/api/tuya/devices/{id}/control` `/api/tuya/ac[/control]` | | | 设备控制 |
| **xiaozhi_router** (app/xiaozhi_ws.py): `/ws/xiaozhi`(WS) `/xiaozhi/ota`(GET/POST) | | 1929/2892 | ESP32 手表/机器人接入 (Opus 音频) |

## 安全 / 限流 / CORS

- **认证** (app/auth.py): AUTH_TOKEN 空→放行; 非空时仅对远程请求(本地 peer 且无代理头可免)校验 Bearer token `hmac.compare_digest`. `ASSISTANT_KID_TRUST_PROXY_HEADERS` 控制 X-Forwarded-For; **带代理头的请求一律按远程处理**(防伪造绕过). WS 走 query token 独立认证.
- **限流**: 中间件 request_logger, IP 滑窗(60s): general 60/min + voice 10/min(命中 /api/voice,/api/tts,/api/asr—注意 /api/voice/stream 也命中); 429 带 Retry-After; 会话桶(30/min) 预留(代码存在未接线). 请求体上限 15MB (python中间件), 音频 10MB.
- **清洗**: `_sanitize_text` 去 HTML 标签+js协议+XSS事件+控制字符, 截断; chat/reminders/tts/preferences 都过. **注意 WS text/audio 只做长度校验未走清洗.**
- **CORS**: DynamicCORSMiddleware 动态来源 (TTL 2s): localhost_origins + lan_origins(psutil 私有IPv4)+tunnel_url.txt + ASSISTANT_KID_CORS_ORIGINS; allow_credentials=True; `/api/setup` 只写白名单键, 必需项缺失拒绝.
- **ETag/条件请求**: / + /manage + /manifest + /icon + /api/tunnel + /api/metrics + /api/conversation(分页) 用文件 mtime/inode 弱 ETag +306.

## 已知问题 (2026-08-07 修复后)

| 问题 | 严重度 | 说明 |
|---|---|---|
| ~~venv 缺 qwen_agent~~ ✅已修 | - | 根因: screen 会话环境污染 `VIRTUAL_ENV` 指向旧路径 `助手小子/assistant-kid/.venv`, `source .venv/bin/activate` 被覆盖, python 解析到 hermes venv → warmup 一直失败。修复: `pip install -r requirements.txt` 补齐依赖 + watchdog.sh 改用绝对路径 `.venv/bin/python` 启动 (不再依赖 source activate) |
| ~~WS 文本未走 _sanitize_text~~ ✅已修 | - | WS text 消息现经 `_sanitize_text` 清洗后进大脑 (voice_server.py:2007) |
| ~~会话级限流未接线~~ ✅已修 | - | `_check_session_rate` 补 `_RATE_LOCK` 并接入 /api/chat /api/chat/stream + WS text/audio (非default会话 30/min→429) |
| ~~_REMINDER_AUDIO/SCHEDULER_LOCK 死代码~~ ✅已修 | - | 已删除 (voice_server.py:672-674) |
| ~~无锁全局竞态~~ ✅已修 | - | 新增 `_tts_lock`、`_brain_lock`、`_asr_lock` 保护 TTS缓存/熔断、大脑状态/failure计数、ASR降级限流；`_intent_cache_lock` 扩展覆盖 `_intent_failures`/`_intent_disabled_until` 的读写 |
| ~~_session_buckets 无限增长~~ ✅已修 | - | `_cleanup_loop` 每60秒清理过期(>2×RATE_WINDOW)会话桶 |
| ~~Metrics.token not_modified 重复缺陷~~ ✅已修 | - | app/state.py:77 第二个 `not_modified` → `self.cache_hits` |
| 前端唤醒词用浏览器 Web Speech API(word页前台), `/api/wakecheck` 是外部设备(ESP32)用的 | 低 | 文档已更正 |
| 公网隧道不稳定 | 中 | cloudflared 2026.7.3 --loglevel debug |
| Vosk 模型 macOS 需本地麦克风; 唤醒词依赖浏览器前台 | 中 |
| tts() docstring 称 WAV 实际 aue=6 返回 MP3 | 低 | 文档性 |
| `_MAX_CHUNK=80` (L2248) 声明未使用 / cache_key (L1718) 死代码 | 低 | 清理项 |
| **红外空调 2C API 无法发码** | 高 | 空调设备 `TUYA_AC_DEVICE_ID` 实测为涂鸦万能红外遥控器(category=qt, 非直连WiFi空调)。原 `tuya_api.py` 用 2C 终端用户 API(Bearer `sk-AY` key)调 `shadow/properties/issue` 下发 `switch_power/mode/temperature/fan` + `control=send_ir`，API 返回 success 但**只更新云端影子、不触发红外码发送**，空调纹丝不动；日志长期"直连开机成功"全是 API 返回值的误判。涂鸦 App 能控制是因为内部走 2B 开发者红外云 API。**2026-08-11 已修复**: `tuya_api.py` 新增 `TuyaCloudAPI` 类(2B HMAC-SHA256 签名, sign 含 body SHA256), 走 `/v2.0/infrareds/{infrared_id}/air-conditioners/{remote_id}/command` 单键端点分步下发(power/mode/temp/wind 各一次红外发码)。infrared_id=`TUYA_IR_DEVICE_ID`(红外网关 6c2347eb...), remote_id=`TUYA_AC_DEVICE_ID`(空调遥控器 6c7347f1..., 三菱电机·客厅)。4 处调用点(voice_agent 快路径/mcp_ir_control/tuya_proxy/magic-scenes)全改用 `TuyaCloudAPI.ac_scenes_command`。**关键教训**: (1) scenes/command 多键端点对三菱空调报 `30100 没有查询到码库`, 改用 command 单键端点分步下发才稳定; (2) 2C shadow 下发 send_ir 永远不发码, 是架构限制非参数问题。凭证: TUYA_CLIENT_ID/TUYA_ACCESS_KEY(需涂鸦云项目+实名认证+订阅万能红外服务+关联App账号)。验证脚本 `tuya_ir_verify.py`。`magic-scenes._tv_control` 仍走废弃 ESP32 路径, 待迁移。 |

## 环境变量

**必需**: `ARK_KEY/ARK_BASE/ARK_MODEL`(大脑) · `BAIDU_API_KEY/BAIDU_SECRET_KEY`(ASR/TTS; APP_ID 未用) · `AMAP_KEY`(高德天气/地图) · `TUYA_API_KEY`(2C 设备读取) · `TUYA_CLIENT_ID`/`TUYA_ACCESS_KEY`(2B 红外空调发码, 需云项目+实名认证+订阅万能红外+关联App) · `TUYA_IR_DEVICE_ID`(红外网关)/`TUYA_AC_DEVICE_ID`(空调遥控器 remote_id) 运行时实际在用: ARK_KEY/BAIDU_API_KEY/BAIDU_SECRET_KEY/AMAP_KEY/ALIYUN_API_KEY/TAVILY_API_KEY/TUYA_API_KEY/TUYA_CLIENT_ID/TUYA_ACCESS_KEY

**可选**: 数据/日志目录(DATA_DIR/LOG_DIR) `CHARLIE_USER_ID`(多用户隔离) `MCP_SERVERS`(启用MCP列表) `TTS_VOICE/TTS_CACHE_MAX_CHARS/TTS_FAILURE_THRESHOLD/TTS_FAILURE_COOLDOWN/LOCAL_TTS_ENABLED` `ASSISTANT_KID_RETRY_AFTER_CAP` `KNOWN_DEVICES`(presence `name=...;mac=...;ip=...`) `DEFAULT_CITY` `SKIP_BACKGROUND` `LOG_FORMAT` `ASSISTANT_KID_CORS_ORIGINS` `ASSISTANT_KID_TRUST_PROXY_HEADERS` `AUTH_TOKEN` `FEISHU_APP_ID/APP_SECRET` `ESP32_IP`(默认192.168.1.7) `FINNA_BASE` `GLM_KEY`

## 运行时数据文件 (data/ 下, 默认项目目录, ASSISTANT_KID_DATA_DIR 可覆盖)

episodic_memories[_uid].json / decision_history.json / decision_feedback.json / pending_confirmation.json / protocols.json / conversation_history[_uid].json / preferences.json / evolution_data[_uid].json / reminders.json / suggestions_state.json / .baidu_token.json / logs/ 下 voice_srv/https/watchdog/app logs

## 前端 (web/)

- **voice.html (690行)**: 语音球/音量条/消息流/面板(提醒/偏好/导出)/唤醒词按钮; MediaRecorder(webm/mp4, Safari降级) + AnalyserNode 音量检测(SOUND 0.2 静音 0.3 THRESHOLD); barge-in (BARGE_THRESHOLD=0.45, BARGE_DELAY=650, BARGE_GUARD_MS=900, 打断只由用户发起, 附最后回复200字); WebAudio+generation 计数器防失效回床; 10s/story 状态栏轮询, reminders 60s/preferences 120s/tunnel 120s (ETag 条件); 剪贴板; `__MUSIC__` 播放; 通知
- **manage.html (285行)**: 5 Tab (系统状态/记忆/决策/协议/进化)
- **manifest.json**: name "Charlie - AI语音助理", standalone, 竖屏, 主题 #e94560, SVG 图标
- **service-worker.js (45行)**: 预缓存 + 网络优先缓存后备 + push 通知
- **icon.svg**: 512×512 渐变 C
- **vosk**: vosk-model-small-en-us-0.15 (68MB) — ESP32 唤醒 / web/voice.html(WebSpeech 其实不用) / local_wake / voice_agent _vosk_asr

## 测试体系 (tests/, pytest.ini)

conftest: sys.path + 无密钥默认环境 + 独立临时 DATA_DIR(tmpdir)

| 文件 | 行 | 测试数 | 覆盖 |
|---|---|---|---|
| test_voice_server.py | 3926 | 178 | HTML/ETag/health/dashboard/reminders/SE/WS 全套(P./location/interrupt)/stream/tts/asr/export/清洗/index/导入 |
| test_voice_agent.py | 1321 | 86 | Cache/History_Files/clean/SystemMsg/brain_stream/TTS熔断/偏好/API-key轮换/多用户 |
| test_runtime_resilience.py | 1410 | 40 | 数据目录/原子写/跨进程锁(提醒/偏好/suggest)/调度/Retry-After/机器级锁 |
| test_security_fixes.py | 310 | 29 | 安全 eval/进程清理/请求体/伪造报头/清洗/env.example 完整性 |
| test_utils.py | 331 | 28 | parse_time_str/oso/清理/截断 |
| test_core.py | 233 | 30 | magic-memory/decisions/scenes/evolution |
| test_config.py | 91 | 5 | 端口/CORS/LAN/TTL |
| test_audio_activity.py | 72 | 7 | 静音/短音/咔哒声 |
| **CI 只跑稳定队**: test_core+test_utils+test_audio_activity+test_runtime_resilience (~95个, 约9s) | | | |
| tests/old/ | gitignore(含硬编码密钥) | | 手工旧冒烟 |

## 部署

- **本地**: `screen -dmS watchdog bash watchdog.sh` → 监控/重启(uvicorn 8000/8443) + 日志轮转
- **Docker**: docker-compose.yml(8000, 卷 charlie-data/logs/notes, restart除非停止, /health 健康检查)
- **PyInstaller**: charlie.spec (hiddenimports 全 SDK, datas 含 6 magic-*.py, bin/ 搜 ffmpeg/ncm, COLLECT) → build.sh
- **CI**: .github/workflows/ci.yml (python 3.12, requirements, 稳定集 + compileall)
- **隧道**: start_tunnel.sh (cloudflared) → tunnel_url.txt → 前端/API/tunnel

## 当前状态 (2026-08-07)

### 已重做/新实(正在维护)

- 全测试绿: 431 passed / 10 skipped (含 WS 协议/记忆/熔断等)
- **（之前的）功能**端口见上节。 额外 8/7 新增:
  1. AI 穿搭推荐 magic-wardrobe.py (天气+ARK+衣橱3套)
  2. Silero VAD 集成 local_wake.py (神经网络边界)
  3. 唤醒修复 (venv python3.12)
  4. 空调控制强化 / 音乐关键词修正 / 乱码阈值6→10
  5. 管理面板 + 5端点 + 10s 监控
  6. **第一轮**: system msg注入(待办/摘要/偏好), 意图分类/脑熔断完善, ack消息(barge-in), 提醒同步播放, CORS, manifest "Charlie", 前端 barge-in参数, .env TTS 全量文档, RTK更新
  7. **第二轮**: 全局并发竞态加锁(`_tts_lock`/`_brain_lock`/`_asr_lock`+`_intent_cache_lock`), `_session_buckets`周期清理(防内存泄漏), `Metrics.token`缺陷修复, WS文本清洗, 会话限流接线, CI全量测试, venv修复(hermes污染→绝对路径venv), watchdog.sh重写, 服务重启(PID 30319)

### 8/9 新增（唤醒体验 + 空调快路径）

1. **严格唤醒（回复后必须再唤醒）**: `app/xiaozhi_ws.py` — 流式回复播完(`tts state=stop` 且 started)后**重新打开 30s 连续对话窗口**(`armed_until=now+ARM_WINDOW`, 不是清零), 日志 `reply finished, re-armed for 30s follow-up`; 30s 内可连续追问, 窗口过期才需重新唤醒。同时重置 `noise_floor=0` 防 TTS 余响抬高阈值。
2. **空闲 goodbye (修复"只能对话一轮")**: 固件 v2.1 在 WS 连接期间**关闭本地 KWS**, 必须服务端在 arm 窗口过期且空闲时主动发 `{"type":"goodbye"}` 并 `ws.close()`, 设备才断开重回本地唤醒词监听。否则设备空流音频永不重连, 第二轮无法唤醒。每 100 帧检查一次。真机日志验证: goodbye→cleaned up→设备重连+wake 成功。
3. **短句 endpoint 修复**: `MIN_SPEECH_FRAMES 18→12` — "现在几点了"仅~0.9s(13热帧), 调 18 时永不 endpoint 直到 goodbye, 短命令(开灯/几点)全丢。降到 12(0.72s)；环境噪声仍由 NOISE_DROP_FRAMES(12s 内不足 12 热帧则丢弃) 过滤。直接注入 WS 多轮测试 PASS(第一轮天气+第二轮不唤醒追问几点)。
4. **唤醒即时反馈音**: 唤醒(detect)瞬间播放 880Hz/150ms 预合成提示音(`_synth_wake_beep` 模块级缓存) + 设 `wake_echo_until=+0.5s` 冷却窗(覆盖提示音回授；注: 2s 会吞掉用户唤醒后立即说话，已改 0.5s)。
5. **唤醒词剥离**: `agent/intent.py::strip_wake_word` 去 ASR 开头"你好小智/charlie/查理…+标点"；`process_utterance` 若 ASR 只有唤醒词则静默忽略。
6. **BARGE_IN_FRAMES 2→3**（120→180ms，对齐打断检测 ~200ms 底线）。
7. **空调直连快路径**: `voice_agent._direct_ac_control` 解析 开关/制冷热除湿送风自动/温度(X度)/风速(高低中)→ `TuyaAPI.issue_properties` 直连（绕过 LLM+MCP 的 2-4s）；在 `brain()` 与 `brain_stream_sentences()` 的 TIME/WEATHER 快路径旁插入 AC 分支，含『天气』的问句不拦截；失败回退 brain。实测 Tuya 读接口 255ms。
8. **测试**: `tests/test_question_paths.py` TestAcFastPath 5 例 + `tests/test_xiaozhi_ws.py` 连续对话 1 例(test_one_wake_allows_multiple_followups) + goodbye 空闲断开 1 例 + 短句 endpoint 1 例(test_short_utterance_reaches_asr)。全绿 51 (43 question_paths + 8 xiaozhi_ws)。

### 8/10 会话补充（多轮对话验证 + 延迟数据）

- **多轮对话端到端验证**: 直接用 WS 注入高质量音频（Finna合成，绕开扬声器→空气→麦克风声学衰减）验证完整周期:
  `唤醒+天气→re-arm→不唤醒追问几点→回复`, 连续多次 PASS。日志时序: 第二问 speech start→endpoint(53帧)→ASR→first sentence→TTS 全链路 ~6s。
- **真机 goodbye 生命周期**: 09:00:37 唤醒+connect → 09:01:11 `idle past arm window, sending goodbye` → `session cleaned up` → 09:01:15 设备再次被唤醒重连。证实固件 v2.1 WS 连接期间关闭本地 KWS、收到 goodbye 断开后重回唤醒态。
- **延迟数据（用户感知）**: 用户说完→设备出声 ≈ **2.4–3.1s**。
  - 天气快路径(第一轮): endpoint→首句 0.55s, 总 ~2.4s
  - 普通路径(第二问"几点"): endpoint→ASR 0.45s + TTS 首句 0.98s, 总 ~3.1s
  - **大头 = SILENCE_FRAMES=30 固定 1.8s 尾静音判定**, 占感知延迟 60%。可优化方向: SILENCE_FRAMES 30→24(省~0.36s)。
- **真机测试通道限制（非代码 bug）**: Mac 扬声器→空气→ESP32 麦克风路径信号衰减大, 命令音频被判 `noise_drop`(热帧不足), Finna TTS 合成音尤其。故多轮逻辑用直接 WS 注入验证; 真人近距离对着设备说话应无此问题。
- 相关文件: `app/xiaozhi_ws.py`(MIN_SPEECH_FRAMES=12 于 L49, re-arm 于 ~L488, goodbye 于 ~L668), `agent/intent.py::strip_wake_word`, `voice_agent.py::_direct_ac_control`, `tests/test_xiaozhi_ws.py`(+2), `tests/test_question_paths.py`(+5)

### 8/11 空调控制修复：2C→2B 红外云 API（彻底打通）

- **根因**: 空调设备 `TUYA_AC_DEVICE_ID`(6c7347f1e4f47ddb07hejn) 实测为涂鸦万能红外遥控器(category=qt, 非直连 WiFi 空调, 三菱电机·客厅)。原代码用 2C 终端用户 API(`sk-AY` Bearer key)调 `shadow/properties/issue` 下发 `switch_power/mode/temperature/fan` + `control=send_ir`，API 返回 success 但**只更新云端影子、不触发红外发码**(2C 架构限制, 非参数问题)。日志长期"直连开机成功"全是 API 返回值误判, 空调从未真正被控。
- **2B 链路**: 涂鸦 App 走 2B 开发者红外云 API。`tuya_api.py` 新增 `TuyaCloudAPI` 类(2B HMAC-SHA256 签名, **sign 包含 body SHA256**, token 2h 自动刷新)。infrared_id=`TUYA_IR_DEVICE_ID`(红外网关 6c2347eb1be34657f3ggsd), remote_id=`TUYA_AC_DEVICE_ID`(空调遥控器)。
- **端点选型教训**: `scenes/command` 多键端点对三菱空调报 `30100 没有查询到码库`; 改用 `command` 单键端点分步下发(power/mode/temp/wind 各发一次红外码)才稳定。`ac_scenes_command` 内部循环单键。
- **平台配置**(ego-browser 辅助完成): 涂鸦云项目 charlie(p17859320009453j3wd8) → 实名认证 → 订阅「万能红外开放能力」→ 关联 App 账号授权设备到项目。凭证 `TUYA_CLIENT_ID=e78jgtaru59pyhjww7sr` + `TUYA_ACCESS_KEY` 写入 .env。
- **4 处调用点全改 2B**: `voice_agent._direct_ac_control`(mode 映射改 2B: 0冷/1热/2自动/3送风/4除湿, 与 2C 不同)、`mcp_ir_control.ac_control/ac_status`、`tuya_proxy./ac`(devices 用 2C, ac 用 2B)、`magic-scenes._ac_control`。
- **验证**: `_direct_ac_control("打开空调制冷26度")` → "空调已打开,制冷,26度。", `ac_status` 返回 power=1/mode=0/temp=26/wind=1 一致; 关机同样生效。5 例 AcFastPath 回归全绿。验证脚本 `tuya_ir_verify.py`(status/on/off)。**用户真机确认空调物理响应成功(开机/制冷)**, 至此 2B 红外链路完整闭环(API True + status 一致 + 物理响应)。
- 相关文件: `tuya_api.py`(+TuyaCloudAPI/130行)、`voice_agent.py::_direct_ac_control`、`mcp_ir_control.py`、`tuya_proxy.py`、`magic-scenes.py::_ac_control`、`tuya_ir_verify.py`(新)、`.env.example`(+TUYA_CLIENT_ID/ACCESS_KEY)。
- **遗留**: `magic-scenes._tv_control` 仍走废弃 ESP32 红外路径(电视用码库 key_code 参数, 机制不同), 加 TODO 待迁移。

### 8/11 项目评估与优化（基于互联网最佳实践）

**评估基线**: 74 文件/25345 行(RTK 旧记 13500 已过时, 含 agent/ 包重构 1256 行)。CI 稳定集(test_core+utils+audio_activity+runtime_resilience+question_paths)全绿。test_voice_agent.py 78 passed/14 failed/1 error(退化)。退化根因两类: (1) agent/ 包重构后, 测试 `patch.object(voice_agent, ...)` 未同步改 `patch.object(agent.asr_tts/history/preferences/...)` — 因函数/常量已迁入 agent.* 模块, patch voice_agent 命名空间不影响模块内调用; (2) 重构改变行为, 测试期望未对齐(如 `_estimate_msg_tokens` 去掉 role 开销, 测试仍 assert >4)。

**已修复**: TestTTSCache patch 路径 12 处 `voice_agent.tts`→`agent.asr_tts.tts` + TTS 常量(TTS_CACHE_TTL/MAX/VOICE/MODEL/FAILURE_COOLDOWN)→`agent.asr_tts.*` + setup 末尾清缓存。TestTTSCache 从 7 失败降到 2(剩余 2 是 _retry/tts mock 时序深层问题)。

**最佳实践调研**(一手来源, agent 核验): Top10 优先清单 —
1. 流式 VAD 替代固定 1.8s 尾静音(Silero/py-webrtcvad) — 砍 ~1.3-1.5s 首句延迟(最大杠杆)
2. 百度 ASR/TTS 阻塞 requests 用 run_in_threadpool + 专用有界线程池 — 修事件循环阻塞
3. 测试回归: httpx.MockTransport + dependency_overrides, setup 去真实 I/O
4. 流式 TTS: 按句合成音频块即产即发(参考 Pipecat 帧式管线)
5. ARK LLM 流式 token + 首句即启 TTS, ASR/LLM/TTS asyncio Task+Queue 重叠
6. lifespan 托管共享资源(httpx.AsyncClient/MCP子进程/Tuya client), request.state 共享, 优雅关闭
7. Tuya token 提前 60s 后台刷新 + Lock 防雪崩(**已实施**: TuyaCloudAPI._refresh_token 加 _token_lock + double-check)
8. 固定提示词 TTS 预合成 LRU + 外部服务长连接复用
9. 拆分 voice_server.py(2723行)/voice_agent.py(2589行) 为 routers/services + DI 接缝
10. WebSocket/ASR 限流 + 有界队列背压

**已实施优化**: (a) Tuya 2B 红外链路完整打通(见上节); (b) TuyaCloudAPI token 并发锁(double-check 命中 0ms, 复用 token ac_status 157ms vs 首次 409ms); (c) 测试 patch 路径修复(voice_agent→agent.*, 14→10 failed, 已修 DATA_DIR/_clean_for_tts/PREFS_FILE/TTS常量 + 恢复_estimate_msg_tokens role开销+4); (d) 验证 Tuya 2B 签名与官方 SDK 源码一致; (e) **项3 专用有界线程池**: voice_server `_io_pool`(ThreadPoolExecutor max_workers=8, thread_name_prefix=charlie-io) + `loop.set_default_executor` 让 asyncio.to_thread 自动用, ASR/TTS/brain 阻塞 I/O 隔离不占满默认 executor, lifespan shutdown 清理。CI 稳定集全绿无回归。

**剩余优化清单**(逐项状态):
- 项1 测试退化: ✅ 已全部清零! 88 passed/0 failed/5 skipped (从 78p/14f/1err → 88/0/5)。修复明细: (1)patch 路径同步 agent 重构(TTS/MODE/FAILURE_COOLDOWN/_retry/json/os/PREFS_FILE/DATA_DIR/_clean_for_tts 共 20+ 处 voice_agent→agent.*); (2)恢复 `_estimate_msg_tokens` role 开销 +4; (3)`_load_history` 改原地 clear+extend 保持 `_sessions`↔`_history` 引用一致; (4)`_trim_history_tokens` 下限 2→4 (保最后2轮); (5)`_save_history` 恢复并发 snapshot 保护(save_seq 检查)+ temp+os.replace 原子写(重构丢失); (6)TTS cooldown 测试清 setup 留下熔断; (7)5 个过时测试 skip(_TOKEN_RE 废弃/trim 不存摘要)。
- 项2 流式 VAD: SILENCE_FRAMES 已 20(1.2s, 非旧记30), local_wake.py Silero VAD(`_is_speech` L54-69)可复用, 但集成到 xiaozhi_ws 逐帧循环 + 调参需真机验证(误端点影响体验), 待真机环境专项。
- 项4 大文件拆分: voice_server.py(2723行)/voice_agent.py(2589行) 超最佳实践~500行护栏, 拆分是高风险大重构(移动函数+更新import+重跑全测试), 建议专门 PR。

### 遗留 v2 决策
- (无未决决策)

## 隧道/进程

- tunnel: `https://dogs-respected-campaign-tier.trycloudflare.com` (cloudflared 2026.7.3)
- voice_server: 47112 (screen `voice`, 端口8000, 项目venv绝对路径, 8/10 载入goodbye+连续对话+MIN_SPEECH_FRAMES=12)
- https_server: 无 (watchdog 按需; 当前未起)
- watchdog: 30253 (screen watchdog, 用绝对路径 `.venv/bin/python` 启动, 避免 hermes venv 污染)