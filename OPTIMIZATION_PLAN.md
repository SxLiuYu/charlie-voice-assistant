# Charlie 真实优化方案

> 原则: 不能做的事不做，生态不允许的不做，想做但不可持续的不做
> 日期: 2026-09-06
> 视觉模型: 使用 Agnes (ARK API)

---

## 约束声明

### 电商 — 不能做

| 想做 | 为什么不能做 |
|------|-------------|
| 淘宝/京东商品搜索 | 无公开API，HTML scraping 反爬极严，`g_page_config` 正则新版已不存在 |
| 商品比价 | 同上，数据源不可靠 |
| 自动下单 | 需要用户登录态 + 支付凭证 |
| 物流查询 | 快递100/阿里物流API 非公开申请制 |
| 优惠券搜索 | 无聚合API，scraping 不可持续 |

**结论**: 电商闭环在当前生态下不可做。`magic-taobao.py` 三个工具标注为「实验性/已失效」。

### 设备同步 — 有限地做

- **能做**: 跨设备状态共享（SQLite + Feishu/MQTT 广播），用户在任意设备说"今天有什么安排"能同步
- **不能做**: 直接接入小米米家/涂鸦/BroadLink（无公开API文档）
- **能做**: 通过 MQTT 协议（Tasmota/Sonoff/Shelly/ESP32）+ Home Assistant REST API 间接控制

### 视觉模型 — 使用 Agnes

接入 ARK API 的视觉能力（Agnes 多模态），不需要额外模型服务。
- 场景：拍屏幕上文字、识别物品、读文档、看门口摄像头
- 新增 `analyze_image(image_bytes, question)` MCP 工具

---

## 一、Charlie 当前能力（真实盘点）

### 已验证可用的（20项）

1. **语音对话** — ASR(百度) → LLM(ARK/Agnes/Ollama) → TTS(百度)，完整闭环
2. **天气 + 穿衣建议** — 高德API + magic-wardrobe
3. **提醒/闹钟** — magic-reminder，持久化 + 推送 + 过期清理
4. **菜谱推荐** — magic-recipe，9个工具
5. **音乐播放** — magic-music，8个工具
6. **事实记忆** — magic-memory，5个工具
7. **偏好记忆** — magic-preferences，持久化
8. **角色切换** — magic-roles + magic-jarvis
9. **场景自动化** — magic-scenes，4个内置 + 用户可学习
10. **飞书集成** — 消息/文档/日历/双向WebSocket
11. **摄像头视觉** — 萤石云RTSP + 屏幕截图（基础模型）
12. **个人化推送** — 基于用户兴趣的主动推荐
13. **DLNA电视控制** — 音量/静音/播放
14. **早报/晚报** — morning_briefing / evening_wrapup
15. **MQTT设备** — ESP32语音设备，Opus音频双向
16. **代码解释器** — 自主Python执行
17. **浏览器工具** — 网页浏览/搜索/截图
18. **知识问答** — 汇率/节日/新闻/翻译/计算/空气质量
19. **意图分类** — 关键词+LLM三级管线，100%准确率
20. **进程内MCP** — 零子进程开销，0.01s工具调用

### 已失效/实验性的（4项）

| 工具 | 状态 | 原因 |
|------|------|------|
| `magic-taobao.search_products` | **已失效** | 淘宝HTML反爬，新版页面结构变化 |
| `magic-taobao.search_jd` | **已失效** | JD搜索页结构变化 |
| `magic-taobao.compare_price` | **已失效** | 依赖前两项 |
| `magic-dev` 改代码 | 可选 | 开发调试用 |

---

## 二、优化方案

### P0 — 立即可做（1-2周）

#### P0-1: 提醒/推送稳定性 ★★★★

**问题**: MQTT推送无确认、飞书无guaranteed delivery、进程重启丢失未触发的提醒。

**方案**:
1. **进程重启恢复** — `schedulers.py` 启动时从 `reminders.json` 恢复所有未完成且未过期的提醒
2. **发送失败重试** — `push_tts_to_mqtt` 失败时写入pending队列，下次推送时重发
3. **推送确认** — ESP32 收到TTS后回发ack，服务器确认ACK才标记为已送达
4. **静默时段** — 基于用户偏好 `quiet_hours_start`/`quiet_hours_end` 调整提醒时间

**效果**: 提醒送达率 85% → 99%
**工作量**: 2天
**修改文件**: `app/schedulers.py`, `app/mqtt_server.py`, `app/routes/reminders.py`

#### P0-2: 场景条件自动触发 ★★★

**问题**: 场景必须用户说"晚安"才触发，睡着了忘了说就不会执行。

**方案**:
- 新增 `charlie/agent/scene_triggers.py`：定时轮询引擎（默认5分钟间隔）
- 支持条件类型：`time`(固定时间)、`weather_contains`(天气)、`routine`(复合)
- 内置默认触发器：22:30→晚安、07:00→早安
- 触发后通过 Feishu/MQTT 推送通知，不自动执行有副作用的操作（如关空调）

**效果**: 从纯手动到半自动
**工作量**: 2天
**新建文件**: `charlie/agent/scene_triggers.py`

---

### P1 — 短期（1-2月）

#### P1-1: 长期记忆 SQLite 持久化 ★★★★

**问题**: memory/preferences 是 JSON 文件 + 内存缓存，跨会话弱，重启后搜索效率低。

**方案**:
- 新增 `charlie/agent/memory_store.py`：SQLite 存储
- 表：`facts(user_id, key, value, source, created_at, last_accessed, confidence)`
- `magic-memory.recall()` 优先查 SQLite → 内存 → LLM
- 启动时加载高频 facts 到 LRU 缓存

**效果**: "你还记得我上次说..." 跨会话准确率 30% → 90%+
**工作量**: 4天

#### P1-2: 跨设备状态同步 ★★★☆

**问题**: 手机上设的提醒，回家问音箱不知道。

**方案**:
- SQLite 作为唯一状态源
- Feishu WebSocket + MQTT 天然是消息总线
- 任何交互后写SQLite → 通过 Feishu/MQTT 广播增量事件
- 设备上线时拉取24h增量

**限制**: 手机端只有飞书消息入口，不是完整App，同步是状态共享。
**工作量**: 5天

#### P1-3: 知识管理增强 ★★★

**方案**: 扩展 `magic-summary`：
- `summarize_conversation(n)` — LLM总结最近N轮对话的待办/决策/事实
- `extract_actions(text)` — 从文字中提取行动项（时间+内容）
- 对话结束时自动追加 summary 到 sessions 表

**工作量**: 3天

#### P1-4: MQTT 统一设备控制 ★★★

**方案**: 新建 `charlie/magic-mqtt-devices.py`：
- 统一的 `device_control(device_id, action, value)` 接口
- 覆盖 Tasmota/Sonoff/Shelly/ESP32/Zigbee2MQTT（通过 MQTT）
- 通过 HA REST API 间接控制飞利浦Hue/IKEA Tradfri
- 自动发现：scan MQTT `homeassistant/+/+/config` topic

**不能做的**：小米米家/涂鸦/BroadLink（无公开Python SDK）
**工作量**: 5天

---

### P2 — 中期（2-4月）

#### P2-1: 自定义 MCP 工具配置 ★★☆

**方案**: 用户通过 JSON 配置文件添加自定义工具：
- `custom_tools/` 目录，每个 JSON 文件定义一个工具
- 格式：`{ "name": "my_tool", "prompt": "...", "command": "python script.py", "params": [...] }`
- `mcp_registry.py` 增加扫描该目录

**工作量**: 3天

#### P2-2: 视觉模型接入 (Agnes) ★★★

**方案**:
- 通过 ARK API 的 Agnes 多模态能力，识别图片/屏幕/摄像头内容
- 新增 `analyze_image(image_bytes, question)` MCP 工具
- 摄像头/屏幕截图 → base64 → Agnes API → 结构化回复
- 支持场景：屏幕文字识别、物品识别、合同/文档阅读

**工作量**: 2天

#### P2-3: 语音克隆 ★★★☆

**方案**: 集成 CosyVoice（阿里开源）
- 用户录制 10s 语音样本 → 零样本克隆训练（2-5分钟）
- 生成 .wav 后替换 TTS 管道
- 可选功能，默认使用系统音色

**工作量**: 5天

---

### P3 — 不做

| 项 | 原因 |
|----|------|
| 离线降级 | 设备通常在线，投入产出比低 |
| 电商全链路 | 淘宝/京东无公开API，scraping已失效 |
| 美团外卖下单 | 无开放API |
| 机票/火车票 | 12306/航司无开放API |
| 本地模型降级 | 设备跑量化模型吃力，离线ASR仍是瓶颈 |
| IoT网关自研 | 硬件+协议+固件 = IoT公司做的事 |

---

## 三、实施路线图

```
Week 1-2 (P0):
  ├── Day 1-2: 提醒稳定性（重启恢复 + 推送确认 + 静默时段）
  └── Day 3-5: 场景条件自动触发引擎

Week 3:
  ├── 全量回归测试
  └── 真实场景复测

Week 4-5 (P1):
  ├── memory_store.py (SQLite持久化)

Week 6-7 (P1):
  ├── 跨设备状态同步
  └── magic-summary 增强

Week 8-9 (P1):
  ├── magic-mqtt-devices.py
  └── 自定义 MCP 工具配置

Week 10-11 (P2):
  ├── 视觉模型接入 (Agnes)
  └── 语音克隆 (CosyVoice)
```

---

## 四、格局判断

Charlie 不走「对标小爱同学/天猫精灵」路线。商业音箱做的是封闭生态+硬件销售，Charlie 做的是**开放能力+个人代理**。

| 维度 | 商业音箱 | Charlie |
|------|----------|---------|
| 卖点 | 硬件 + 内容 | 工具深度 + 个性化 + 自主性 |
| 扩展 | 审核制技能商店 | 代码级 MCP + 自定义场景 |
| 用户 | 泛家庭 | 技术用户 / DIY爱好者 |
| 设备 | 自有协议 | MQTT/HA 开放协议 |
| 数据 | 云端 | 自托管 |

Charlie 的优势是商业音箱做不到的：真实工具调用、代码执行、持久记忆。商业音箱的电商闭环/米家生态是商业壁垒不是技术壁垒，不追。
