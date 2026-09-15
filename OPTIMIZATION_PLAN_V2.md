# Charlie 竞品对比 + 优化方案 v2

> 基于对 9 款主流 AI 助手的横向调研，结合 Charlie 当前能力基线，制定新一轮优化方案。
> 日期：2026-09-06
> 前序方案：OPTIMIZATION_PLAN.md（P0-P2 已全部落地）

---

## 一、竞品能力全景图

### 国内 5 款

> 数据来源：2026.9 各厂商官网/开发者门户直接抓取验证。

| 维度 | 天猫精灵 (AliGenie) | 超级小爱 (小米) | 小度 (百度) | 小艺 (华为) | 豆包 (字节) |
|------|---------------------|-----------------|-------------|-------------|-------------|
| **底层 LLM** | 通义千问 (自研) | MiLM (自研) + DeepSeek | 文心一言 (自研) | 盘古系列 (自研，页面未标名) | Doubao-Seed (自研全家桶) |
| **免唤醒** | 20+ 场景快捷词 | ❌ | ✅ 全域免唤醒 (10m) | ❌ | ❌ (App 按住说话) |
| **方言** | — | — | — | ✅ 16 种方言 | — |
| **情感 TTS** | 名人音色包 (周深) | — | 个性化对话 | ✅ 11 情绪+100 场景 | ✅ 超拟人 RealtimeVoice |
| **多模态视觉** | GenieOS 7.0 多模态 | 部分 | ✅ 人脸 99.7% | ✅ 实时视觉+盲人辅助 | ✅ Doubao-Seed-Vision |
| **语音克隆** | ❌ (消费端) | ❌ (消费端) | ❌ (消费端) | ❌ (消费端) | ✅ 声音复刻 ICL-2.0 (8元/万字符) |
| **IoT 生态** | 1+3+N 全屋智能 (云云+芯片) | 米家 (最大) + 人车家全生态 | ✅ 闪连 400 设备/0.2s/本地控制 | 鸿蒙智家 | ❌ (仅 OEM SDK) |
| **电商** | ✅ 淘宝+支付宝小程序 | ❌ | ❌ | ❌ | ❌ |
| **开发者平台** | AliGenie 4 子平台 (webhook) | 小爱开放平台 + HyperOS | DuerOS 5200+ 技能 | HarmonyOS + HMAF | 火山方舟 API+CLI |
| **MCP 支持** | ❌ | ✅ **HyperOS Agent 平台显式支持 MCP** | ❌ | ❌ (推 A2A 协议) | ❌ |
| **Agent 协议** | webhook skills | MCP/Skill/Agent | skills | ✅ A2A (跨厂商) | Ark Agent |
| **儿童模式** | ✅ 儿童管控 | ✅ (HyperOS) | ✅ 教育/儿歌 | ✅ 深度解题 | ✅ 豆包爱学 (独立 App) |
| **主动服务** | AI 芯读习惯 | 小爱建议 | 10 亿用户画像 | 小艺建议+帮帮忙 | 定时任务 |
| **硬件** | 音箱/中控屏/闺蜜机 | 手机/音箱/TV/手表/SU7 车 | 音箱/中控屏/机器人/车 | 手机/PC/TV/手表/车/智家 | 无自有硬件 |
| **独特卖点** | GenieOS 7.0+全景声 | **唯一 MCP** + 人车家全生态 | 全域免唤醒+量化最强 | HMAF/A2A+16 方言+声音修复 | **唯一消费级语音克隆** + 全模态 |

### 国际 4 款

> 数据来源：2026.9 网络调研，反映最新状态。

| 维度 | Alexa+ (2026.2 全美开放) | Gemini (2026.9 起唯一) | Siri AI (WWDC26 发布) | Bixby (2026.4 重构) |
|------|--------|--------|---------------------------|-------|
| **底层 LLM** | 模型无关架构 (Amazon FM + 第三方按任务路由) | Gemini 2.x (Search Grounding) | Apple 自研 + Google Gemini 定制合作 | Galaxy AI (agentic 重构, 与 Gemini 共存) |
| **多模态视觉** | Visual ID 识别家庭成员 | ✅ 最强 (Live 视频+屏幕共享+Project Astra) | 相机 Siri 模式 (菜品识别+营养) | Bixby Vision |
| **语音克隆** | Voice ID 识别 | ❌ (预设表达力音色) | ✅ Personal Voice (无障碍) | ✅ 自定义/名人音色 |
| **情感 TTS** | 自然对话音色 | Gemini Live 30+ 音色 | 更自然 Siri 音色 | 对话式 AI 音色 |
| **IoT 生态** | Matter/Zigbee (最大, 数亿设备) | Matter/Thread/Nest | Matter/HomeKit | SmartThings + 家电 |
| **开发者平台** | Alexa Skills Kit + 第三方 agent 接入 | Gemini Extensions (Actions 已下线) | App Intents + Shortcuts (iOS 27 跨 App) | Bixby (agentic, 向第三方开放中) |
| **儿童模式** | Amazon Kids+ | Family Link | Screen Time | Samsung Kids |
| **主动服务** | ✅ 购物代理 (盯降价+自动下单) + 多 agent 编排 | Gemini 记忆 + 跨设备流转 | Personal Context (跨邮件/信息/照片搜索) | SmartThings 联动 |
| **Agentic 任务** | ✅ 最强 (Uber/OpenTable/Ticketmaster/Expedia 自主预订) | 搜索+Shopping Graph agent | 行程规划+跨 App 执行 | 多代理编排 |
| **硬件** | Echo 全系/Auto/Buds/Fire TV | Nest/Pixel/Auto/Wear OS/XR 眼镜 | HomePod/iPhone/CarPlay/Watch/Vision Pro | Galaxy 全系/家电/TV |
| **定价** | Prime 免费 / 非 Prime $19.99/月 | AI Ultra 订阅 | 免费 (随 iOS 27) | 免费 (随 One UI) |
| **独特卖点** | 购物代理 + 多模型路由 | 全设备统一 AI + 搜索知识 | 隐私 + 个人上下文 + 免费 | 家电 + 与 Gemini 共存 |

---

## 二、Charlie 当前能力基线（v2 盘点）

### 已落地能力（25 项，含本轮新增）

| # | 能力 | 竞品对标 |
|---|------|----------|
| 1 | 语音对话闭环 (ASR→LLM→TTS) | 全员标配 |
| 2 | 天气 + 穿衣建议 | 全员标配 |
| 3 | 提醒/闹钟 (持久化+重试+过期清理) | 全员标配，Charlie 投递状态机更精细 |
| 4 | 菜谱推荐 (9 工具) | 小度有，其他有限 |
| 5 | 音乐播放 (8 工具) | 全员标配 |
| 6 | 事实记忆 (SQLite 持久化) | Gemini/Siri 有记忆，音箱类无 |
| 7 | 偏好记忆 (持久化) | 同上 |
| 8 | 角色切换 | 竞品无 |
| 9 | 场景自动化 (4 内置+用户学习) | Alexa Routines、小爱场景 |
| 10 | **飞书集成** (消息/文档/日历/双向WS) | **竞品均无** |
| 11 | **摄像头视觉 (Agnes ARK API)** ★新增 | 对标 Gemini/Echo Show |
| 12 | 个人化推送 | 对标小爱建议 |
| 13 | DLNA 电视控制 | 竞品通过 IoT 间接实现 |
| 14 | 早报/晚报 | 对标 Flash Briefing |
| 15 | MQTT 设备 (ESP32+Opus) | 独有硬件方案 |
| 16 | 代码解释器 | **竞品均无** (除 Alexa+ agentic) |
| 17 | 浏览器工具 | **竞品均无** |
| 18 | 知识问答 (汇率/节日/新闻/翻译/空气) | 全员标配 |
| 19 | 意图分类 (关键词+LLM 三级) | 全员有意图理解 |
| 20 | 进程内 MCP (零子进程开销) | **竞品均无 MCP** |
| 21 | **语音克隆 (CosyVoice)** ★新增 | 对标 Apple Personal Voice / Bixby |
| 22 | **6 级 TTS 降级链** ★增强 | 优于多数竞品的 TTS 容错 |
| 23 | **Provider 自动轮换** ★增强 | 对标 Alexa+ Bedrock 多模型 |
| 24 | **提醒入队重试上限** ★修复 | 工程质量提升 |
| 25 | **飞书 WS watchdog 区分启动/退出** ★修复 | 工程质量提升 |

### 竞品有而 Charlie 没有的（差距清单）

| 差距 | 竞品来源 | Charlie 能否做 | 原因 |
|------|----------|---------------|------|
| 电商下单 | 天猫精灵/Alexa+ | ❌ 不能 | 无淘宝/京东公开 API |
| 米家/涂鸦设备直控 | 小爱/小度 | ❌ 不能 | 无公开 SDK |
| 全双工免唤醒连续对话 | 小度(全域)/天猫精灵/豆包/Gemini Live | ⚠️ 有限 | 需 VAD 连续监听改造，ESP32 端需固件配合 |
| 情感 TTS (多情绪切换) | 华为(11情绪)/豆包/Gemini Live | ⚠️ 可做 | 需接入支持 emotion 标签的 TTS 或 CosyVoice 情绪模型 |
| 手机 App | 全员 | ⚠️ 有限 | 飞书可作为入口，但完整 App 工作量大 |
| 车机集成 | 小爱/华为/Alexa/Bixby | ❌ 不能 | 需车厂合作 |
| 可穿戴 | 华为/Apple/Bixby | ❌ 不能 | 需硬件 |
| 端到端实时语音 (低延迟) | 豆包 RealtimeVoice | ⚠️ 探索 | 当前 ASR→LLM→TTS 管道延迟 2-5s，端到端需模型重构 |
| 健康问诊 | 小度 | ✅ 可做 | 可通过 LLM + 医疗知识库实现，但需免责声明 |
| 多语言 ASR/TTS | Siri/Gemini/Bixby/华为(20 语言) | ✅ 可做 | 百度 ASR/TTS 支持多语种，需配置 |
| 智能体/Agent 任务编排 | Alexa+(最强)/扣子/超级小爱/Bixby | ✅ 已有基础 | Charlie 已有 MCP + 代码执行，可增强多步任务 |
| 摄像头视频通话 | 豆包/Echo Show/Gemini Live | ✅ 可做 | 已有萤石 RTSP + app/camera.py，可加实时视频流分析 |
| 跨设备状态同步 | 华为/小米/Gemini | ✅ 可做 | SQLite + state_sync.py + Feishu/MQTT 总线已就绪 |
| 个人上下文 (跨邮件/信息搜索) | Siri AI Personal Context | ⚠️ 有限 | Charlie 有记忆系统但无邮件/短信接入，飞书消息可部分替代 |
| 购物代理 (盯降价+自动下单) | Alexa+ | ❌ 不能 | 依赖电商 API，生态壁垒 |
| 跨设备统一 AI (手机→音箱→眼镜) | Gemini (全设备) | ❌ 不能 | 需多端硬件 |
| 方言识别 | 华为 (16 种) | ✅ 可做 | 百度 ASR 支持部分方言，需配置 |
| 声纹识别 (多用户区分) | 天猫精灵/小度 | ✅ 可做 | 需接入声纹模型或百度声纹 API |
| 声音修复 (无障碍) | 华为小艺 | ⚠️ 探索 | 需语音修复模型，非 Charlie 核心场景 |

---

## 三、竞争定位

```
                    封闭生态 ←──────────────────→ 开放能力
                    ┌──────────────────────────────────┐
    硬件销售驱动    │  天猫精灵  小爱  小度  华为       │
                    │  Alexa     Bixby                  │
                    ├──────────────────────────────────┤
    个人代理驱动    │                    Siri (Apple)   │
                    │                    Gemini (Google)│
                    │                    豆包 (字节)     │
                    ├──────────────────────────────────┤
    开发者/DIY驱动  │                        Charlie ★  │
                    │                    Home Assistant │
                    └──────────────────────────────────┘
```

**Charlie 的竞争坐标**：开放能力 + 个人代理 + 开发者/DIY 驱动

**不可追的差距**（生态壁垒，非技术壁垒）：
- 电商闭环（淘宝/京东 API 封闭）
- 米家/涂鸦设备直控（SDK 封闭）
- 车机/穿戴（需硬件合作）
- 端到端实时语音（需自研声学模型）

**可追的差距**（技术可实现）：
- 情感 TTS
- 儿童模式
- 名人音色库
- 全双工连续对话（固件配合）
- 多语言
- 多步 Agent 任务编排
- 跨设备同步
- 健康问诊
- 视频通话/实时视觉

**Charlie 的独有优势**（竞品做不到或不做的）：
- 飞书深度集成（唯一，双向 WS + MCP + 路由三通道）
- 代码执行能力（`magic-info.run_code` 当前已禁用，但 `magic-dev` 可用，且 `magic-browser` 可执行网页操作——仅 Alexa+ agentic 有类似能力）
- 浏览器工具（唯一，6 工具：浏览/搜索/阅读/点击/截图/填表）
- 自托管隐私（仅 Home Assistant 对标；竞品均为云端）
- 多 LLM provider 热切换（5 provider 可轮换，竞品均绑定自研大模型）
- 摄像头视觉 + RTSP 流（已有 `app/camera.py` + 萤石云，竞品仅带屏设备有限支持）

**Charlie 的共有优势**（竞品部分有但 Charlie 做得更开放）：
- 进程内 MCP（小米 HyperOS 也支持 MCP，但 Charlie 是进程内零子进程调用，更轻量）
- CosyVoice 语音克隆（豆包有消费级声音复刻 ICL-2.0，但 Charlie 自托管无按量计费）
- 自定义场景扩展（竞品为审核制技能/有限 Routines，Charlie 支持代码级 + `custom_tools/` JSON 配置）

---

## 四、优化方案 v2

> 原则：只做竞品做不到但 Charlie 能做的，不做生态壁垒内的事。
> 优先级：P0 (1-2 周) → P1 (1-2 月) → P2 (2-4 月) → P3 (不做)

### P0 — 立即可做（1-2 周）

#### P0-1: 情感 TTS 音色切换 ★★★★

**竞品对标**：豆包端到端多情绪、Gemini Live 30+ 音色、Alexa+ 表达力提升

**问题**：Charlie 当前 TTS 只有中性语气，无法根据对话内容自动切换情感（开心/悲伤/严肃/温柔）。已有 `agent/persona.py` 做动态人格状态机（按情绪+交互模式调整回复风格），但 TTS 层未联动。

**方案**：
1. `agent/asr_tts.py` 新增 `emotion` 参数，映射到 TTS provider 的 emotion/style 标签
2. `agent/persona.py` 输出当前情感状态 → `agent/llm.py` brain stream 时传递给 TTS 层
3. `agent/system_msg.py` 引导 LLM 在句首注入情感标签 `[emotion:happy]`，TTS 层解析后切换音色参数（兜底：persona 状态推断）
4. CosyVoice 已支持 `emotion` 参数（happy/sad/angry/surprised/default），百度 TTS 可通过 speed/pitch 调整近似
5. 新增 3 个情感音色预设：`warm`（温暖，默认对话）、`energetic`（活泼，音乐/天气）、`calm`（平静，睡前/新闻）

**工作量**：3 天
**修改文件**：`agent/asr_tts.py`, `agent/llm.py`, `agent/persona.py`, `agent/system_msg.py`

#### P0-2: 儿童模式 ★★★

**竞品对标**：天猫/小爱/小度/Alexa Kids+ 均有

**问题**：Charlie 无内容过滤和儿童友好的交互模式。

**方案**：
1. 新增 `charlie/agent/kid_mode.py`
2. 用户说"开启儿童模式"或通过飞书命令切换
3. 效果：
   - 系统提示词切换为儿童版（更简单语言、鼓励性回复、安全过滤）
   - LLM temperature 略提高 (0.7→0.8) 增加趣味性
   - 过滤不适宜话题（暴力/成人内容），返回"这个话题我们换个聊吧"
   - TTS 自动切换为童声音色（百度 TTS 有 `kid` 发音人）
   - 提醒/推送在儿童模式下使用更温和的措辞
4. 状态持久化到 `preferences.json`，跨会话生效

**工作量**：2 天
**新建文件**：`charlie/agent/kid_mode.py`
**修改文件**：`agent/system_msg.py`, `agent/preferences.py`, `agent/asr_tts.py`

#### P0-3: 多步 Agent 任务编排 ★★★★

**竞品对标**：Alexa+ agentic 任务执行、扣子智能体、超级小爱智能体对话

**问题**：Charlie 当前工具调用是单步的（一次调一个工具），无法处理"帮我查明天天气，如果下雨就提醒我带伞"这类条件多步任务。brain 路径已支持 function calling 但缺少多轮 tool-use 循环。

**方案**：
1. `agent/llm.py` 的 brain 路径已支持 function calling，增强为多轮 tool-use 循环（当前是单轮调用后直接回复）
2. 新增 `agent/task_planner.py`：对复杂请求先做任务分解（LLM 生成步骤列表），再逐步执行
3. 每步执行结果注入上下文，下一步可引用上一步结果
4. 设置最大步数（默认 5 步）防止无限循环
5. 通过 `magic-jarvis.py` 注册 `plan_and_execute(task)` MCP 工具，LLM 可自主调用

**示例**：
```
用户："帮我查明天天气，如果下雨就提醒我带伞"
步骤1: get_weather("明天") → "明天小雨，18-22°C"
步骤2: add_reminder("明天早上8点", "带伞出门") → "已设置"
回复："明天有小雨，我已经设了早上8点的提醒叫你带伞"
```

**工作量**：4 天
**新建文件**：`charlie/agent/task_planner.py`
**修改文件**：`agent/llm.py`, `magic-jarvis.py`

---

### P1 — 短期（1-2 月）

#### P1-1: 全双工连续对话 ★★★★

**竞品对标**：天猫精灵 x6、豆包、Gemini Live 均支持免唤醒连续对话

**问题**：Charlie 每轮对话都需唤醒（或按按钮），无法自然连续对话。

**方案**：
1. ESP32 固件侧：VAD 检测到用户说话结束后，不立即切到 SPEAKING 状态，而是保持 LISTENING 状态 3 秒（可配置），如检测到新的语音输入则继续聆听
2. 服务端 `app/xiaozhi_ws.py`：在 TTS 播放期间持续接收音频，检测到用户打断时立即停止 TTS
3. 新增 `CONTINUOUS_DIALOG` 环境变量，默认开启，可通过语音"结束对话"关闭
4. 连续对话超过 5 轮无响应自动退出连续模式

**限制**：需 ESP32 固件配合改造 VAD 状态机
**工作量**：5 天（服务端 3 天 + 固件 2 天）
**修改文件**：`app/xiaozhi_ws.py`, `agent/state.py`

#### P1-2: 名人/自定义音色库 ★★★

**竞品对标**：Bixby 名人音色 (BTS)、Alexa 名人音色 (Samuel L. Jackson)、Apple Personal Voice

**问题**：Charlie 已有 CosyVoice 克隆能力，但用户需要自己录制样本，没有预置音色可选。

**方案**：
1. 预制 5-8 个高质量音色样本（男女各半，含温暖/活泼/沉稳风格），存放在 `assets/voice_samples/`
2. 用户可语音切换"换成温柔女声"/"换成沉稳男声"
3. 新增 `list_preset_voices()` 和 `switch_voice(name)` MCP 工具
4. 支持用户通过飞书上传 10s 语音样本自定义音色（已有 CosyVoice 基础）

**工作量**：3 天
**修改文件**：`agent/cosyvoice_clone.py`, `agent/asr_tts.py`, `magic-jarvis.py`

#### P1-3: 跨设备状态同步增强 ★★★☆

**竞品对标**：华为跨设备流转、小米人车家全场景

**问题**：已有 `agent/state_sync.py`（共享文件 + fcntl 锁 + MQTT topic `charlie/state/{user_id}`）和 `agent/memory_store.py`（SQLite 持久化），但同步仍不完整——手机飞书上设的提醒，回家问 ESP32 音箱可能不知道。

**方案**：
1. SQLite 作为唯一状态源（memory/preferences/reminders 统一存储，`memory_store.py` 已就绪）
2. 在现有 `state_sync.py` 基础上增强：任何状态变更后通过 Feishu WS + MQTT 广播增量事件
3. 设备上线时拉取 24h 增量（需新增增量拉取接口）
4. 完善 `reminders.py` 状态变更 → `state_sync.py` 广播的钩子

**工作量**：3 天（在现有 state_sync.py 基础上增强）
**修改文件**：`agent/state_sync.py`, `app/schedulers.py`, `app/reminders.py`

#### P1-4: 健康问诊 ★★★

**竞品对标**：小度健康问诊

**问题**：Charlie 无法回答健康类问题，用户问"我头疼怎么办"只能泛泛而谈。

**方案**：
1. 新增 `charlie/magic-health.py` MCP 模块
2. 工具：
   - `symptom_check(symptoms)` — 基于医学知识库的初步建议（LLM + 系统提示词约束）
   - `medicine_info(name)` — 常见药品查询（开放医学 API 或内置知识库）
   - `health_reminder(medicine, schedule)` — 服药提醒（复用 reminder 系统）
3. 系统提示词严格约束：始终附加"建议就医"免责声明，不做确诊
4. 服药提醒复用现有 `app/reminders.py`，类型标记为 `medication`

**工作量**：3 天
**新建文件**：`charlie/magic-health.py`

#### P1-5: 对话摘要 + 行动项提取 ★★★

**竞品对标**：Galaxy AI Summarize、超级小爱 AI 写作摘要

**问题**：长对话结束后没有自动摘要，用户回顾困难。

**方案**：
1. 扩展 `magic-summary.py`
2. 新增 `summarize_conversation(n)` — LLM 总结最近 N 轮对话的待办/决策/事实
3. 新增 `extract_actions(text)` — 从文字中提取行动项（时间+内容），可一键转提醒
4. 对话超过 10 轮时自动追加摘要到 sessions 表
5. 用户说"总结一下刚才说的"触发摘要

**工作量**：3 天
**修改文件**：`charlie/magic-summary.py`, `agent/history.py`

---

### P2 — 中期（2-4 月）

#### P2-1: 实时视频通话/视觉问答 ★★★☆

**竞品对标**：豆包视频通话+屏幕共享、Echo Show 摄像头、Gemini Live 视频

**问题**：Charlie 已有 `app/camera.py`（旧手机做 IP 摄像头 + RTSP 抓取 + MJPEG 流 + 视觉分析）和 `agent/vision.py`（`direct_camera_vision_analyze` 摄像头快路径），但只能按需截图分析，无法持续视频流监控。

**方案**：
1. 在现有 `app/camera.py` 基础上增加持续流模式：RTSP 流 → 逐帧截图（1fps）→ Agnes 分析 → 语音回复
2. 用户说"看看门口"→ 已有 `try_camera_by_source_name` 路由 → 拉取萤石/摄像头最新帧 → Agnes 分析 → "门口有一个快递员"
3. 新增"持续看"模式：每 30s 分析一帧，检测到变化时主动语音通知
4. 场景：看门口、看婴儿房、看宠物、读信件

**工作量**：4 天（在现有 camera.py + vision.py 基础上增强）
**修改文件**：`app/camera.py`, `agent/vision.py`, `agent/agnes_vision.py`, `magic-jarvis.py`

#### P2-2: 多语言支持 ★★☆

**竞品对标**：Siri/Gemini/Bixby 均支持多语言

**方案**：
1. ASR 切换：百度 ASR 支持英/日/韩等语种，通过 `ASR_LANGUAGE` 环境变量切换
2. TTS 切换：百度 TTS 有多语种发音人
3. 系统提示词多语言模板
4. 用户可语音切换"切换英文模式"

**工作量**：3 天
**修改文件**：`agent/asr_tts.py`, `agent/system_msg.py`, `app/env_catalog.py`

#### P2-3: 自定义工具配置化完善 ★★☆

**现状**：`agent/custom_tools/` 目录已存在（用户可放 JSON 定义自定义 MCP 工具，`mcp_registry.py` 已支持扫描），但缺少文档和示例，非技术用户难以使用。

**方案**：
1. 完善 `custom_tools/` 机制：提供 3 个示例工具 JSON 模板（查询快递、记账、番茄钟）
2. 格式：`{ "name": "my_tool", "description": "...", "command": "python script.py", "params": [...] }`
3. 新增 `docs/CUSTOM_TOOLS.md` 使用指南
4. 飞书命令 `/add_tool` 交互式引导用户创建工具

**工作量**：2 天
**修改文件**：`app/mcp_registry.py` (完善扫描逻辑)
**新建文件**：`docs/CUSTOM_TOOLS.md`, `agent/custom_tools/example_*.json`

#### P2-4: 主动智能体 — 定时思考 ★★★

**竞品对标**：Alexa+ agentic、超级小爱 AI 记忆

**问题**：Charlie 的主动推送是基于规则的（定时+天气），不够智能。

**方案**：
1. 新增 `agent/proactive_brain.py`：每小时一次"思考"周期
2. 输入：近期对话摘要 + 日历 + 天气 + 用户偏好 + 未完成待办
3. LLM 生成 0-2 条主动建议（不强制推送，有门槛过滤）
4. 推送门槛：重要提醒（已实现）、天气突变、用户偏好匹配的新内容
5. 用户可配置"安静时段"不推送

**工作量**：4 天
**新建文件**：`charlie/agent/proactive_brain.py`
**修改文件**：`app/schedulers.py`

---

### P3 — 不做

| 项 | 原因 |
|----|------|
| 电商下单 | 无公开 API，生态壁垒 |
| 米家/涂鸦直控 | 无公开 SDK |
| 车机集成 | 需车厂合作 |
| 可穿戴硬件 | 需硬件研发 |
| 端到端实时语音模型 | 需自研声学模型，投入产出比低 |
| 离线全链路 | 设备通常在线，ASR 仍是瓶颈 |
| 自研 IoT 网关 | 硬件+协议+固件 = IoT 公司做的事 |
| 手机原生 App | 飞书已作为入口，完整 App 工作量过大 |

---

## 五、实施路线图

```
Week 1-2 (P0):
  ├── Day 1-3: 情感 TTS 音色切换
  ├── Day 4-5: 儿童模式
  └── Day 6-9: 多步 Agent 任务编排

Week 3:
  └── 全量回归测试 + 真实场景复测

Week 4-5 (P1):
  ├── 全双工连续对话 (服务端+固件)
  └── 名人/自定义音色库

Week 6-7 (P1):
  ├── 跨设备状态同步
  └── 健康问诊

Week 8 (P1):
  └── 对话摘要 + 行动项提取

Week 9-12 (P2):
  ├── 实时视频通话/视觉问答
  ├── 多语言支持
  ├── 自定义工具配置化
  └── 主动智能体 — 定时思考
```

---

## 六、格局判断（不变）

Charlie 不走「对标小爱同学/天猫精灵」路线。商业音箱做的是封闭生态+硬件销售，Charlie 做的是**开放能力+个人代理**。

| 维度 | 商业音箱 | Charlie |
|------|----------|---------|
| 卖点 | 硬件 + 内容 + 生态闭环 | 工具深度 + 个性化 + 自主性 |
| 扩展 | 审核制技能商店 | 代码级 MCP + 自定义场景 + JSON 配置工具 |
| 用户 | 泛家庭 | 技术用户 / DIY 爱好者 |
| 设备 | 自有协议 + 封闭生态 | MQTT/HA 开放协议 + ESP32 |
| 数据 | 云端 | 自托管 |
| LLM | 绑定厂商大模型 | 5 provider 可热切换 (ARK/Agnes/GLM/StepFun/Ollama) |
| 视觉 | 带屏设备限定 | Agnes API + 摄像头 RTSP 随时调用 |
| 语音克隆 | 豆包有 (按量计费) / Apple/Bixby (受限) | CosyVoice 零样本自托管 (无按量计费) |
| 工具调用 | 单步 intent / 有限 agent | 多步 Agent (v2) + 代码执行 + 浏览器 + 飞书 |
| MCP | 仅小米 HyperOS 支持 | 进程内零子进程 MCP (更轻量) |
| Agentic | Alexa+ 最强 (订餐/购物/票务) | MCP + 代码执行已有基础，v2 增强多步编排 |

**v2 新增竞争力**：

| 能力 | 竞品状态 | Charlie 方案 |
|------|----------|-------------|
| 情感 TTS | 华为(11情绪)/豆包/Gemini Live | CosyVoice emotion + 百度 pitch 调整 + persona 联动 |
| 儿童模式 | 全员标配 | 软件层面快速实现 |
| 多步 Agent | Alexa+(最强)/扣子/小爱 MCP | MCP + 代码执行已有基础，增强循环 |
| 音色库 | Bixby/Alexa 名人音色/豆包 ICL | CosyVoice 预制 + 用户自定义 (自托管无计费) |
| 主动智能 | Alexa+ agentic/小艺建议 | LLM 定时思考 + 门槛过滤 |
| 实时视频 | 豆包/Echo Show/Gemini Live | 萤石 RTSP + Agnes 逐帧 (已有 camera.py 基础) |
| 多语言 | 华为(20语言)/Siri/Gemini/Bixby | 百度 ASR/TTS 多语种 |
| 方言 | 华为 (16 种) | 百度 ASR 支持部分方言，需配置 |

Charlie 的核心优势仍然是商业音箱做不到的：**真实工具调用、代码执行、持久记忆、飞书集成、浏览器工具、自托管隐私**。在 MCP 方面，小米 HyperOS 虽已支持，但 Charlie 的进程内零子进程方案更轻量灵活。在语音克隆方面，豆包虽有消费级 ICL-2.0，但 Charlie 自托管 CosyVoice 无按量计费限制。v2 方案在保持这些优势的基础上，补齐了情感表达、儿童友好、多步任务、音色多样性等用户体验层面的短板。
