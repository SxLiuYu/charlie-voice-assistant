# Charlie 项目全景图

> 基线：`main` @ `710d2c4`。统计口径：`git ls-files` + `wc -l`。
> 规模：632 个受版本控制文件；Python 271 / TS 113 / Markdown 107 / JS 33 / HTML 20。
> `charlie/` 非测试 Python 约 42,579 行，测试约 33,362 行；`Baize/src` 36,455 行 TS。

---

## 0. 一句话结论

仓库里装的是**两套互不调用的系统**：

| 系统 | 语言 | 干什么 | 入口 |
|---|---|---|---|
| **charlie** | Python 3.12 / FastAPI | 语音助手：ESP32/浏览器收声 → ASR → LLM 大脑 → MCP 工具 → TTS，外加涂鸦红外、飞书、主动决策推送 | `charlie/voice_server.py` |
| **Baize（白泽 3.2）** | TypeScript / Node | 通用个人 Agent 运行时：技能插件、五类记忆、向量检索、Docker 沙箱、自进化提案 | `Baize/src/cli/index.ts` / `Baize/server.js` |

二者**没有任何代码调用关系**。`charlie/baize_skills_mcp.py` 只是 Charlie 自己的一个 MCP 搜索服务（FastMCP），`charlie/agent/roles.py` 里的 `"baize"` 只是一个人格角色。全仓搜索无 `node dist/cli`、无 `localhost:3000` 调用。

Baize 的实现与它自己的 README 差距较大，见 §11.6。

---

## 1. 仓库地形

```
charlie-voice-assistant/
├── charlie/                 # 主系统（Python）
│   ├── voice_server.py      # FastAPI 装配 + lifespan + 中间件      (421 行)
│   ├── voice_agent.py       # 大脑门面：FastPath + voice_loop       (581 行)
│   ├── charlie_main.py      # PyInstaller 入口 + MCP 子进程分派     (256 行)
│   ├── app/                 # 服务层：协议适配、路由、领域能力      (13,569 行)
│   ├── agent/               # 能力层：ASR/TTS、LLM、记忆、人设      (12,131 行)
│   ├── magic-*.py  ×24      # MCP 工具模块（136 个 @mcp.tool）
│   ├── web/  ×9             # 原生 JS 前端，无构建
│   ├── tests/ ×122          # pytest，约 1979 个 test 函数
│   ├── scripts/ ×12         # 证书/模型下载/评测/压测
│   └── skills/              # ClawHub 装的 2 个技能（运行时不用）
├── Baize/                   # 平行系统（TypeScript，36k 行）
├── skills/                  # 自媒体发布技能生态（SKILL.md 规范示例）
├── docs/ ×10                # 部署/ESP32/Demo/打包/技能文档
├── firmware/                # 只有 README，bin 走 Release 分发
├── OOTB/、src/              # 两个空的 gitlink 残留（mimo-vision 子模块）
├── .agent-teams/            # 多智能体协作的历史存档（一次性产物）
├── .scratch/productize/     # 产品化 Spec 拆出的 12 个工单
└── 根目录 8 个规划 md        # CHANGELOG/FIX_PLAN×4/OPTIMIZATION×2/评测/竞品
```

---

## 2. charlie 分层架构

```
┌─ 接入层 ─────────────────────────────────────────────────┐
│ ESP32 xiaozhi WS │ 浏览器 WS │ HTTP/SSE │ MQTT+UDP │ 飞书 │ 天猫精灵 │
└───────┬──────────────────────────────────────────────────┘
        ↓
┌─ 路由层 app/routes/*.py（10 个文件）+ app/xiaozhi_ws.py ─┐
└───────┬──────────────────────────────────────────────────┘
        ↓
┌─ 编排层 voice_agent.py :: voice_loop / FastPath ─────────┐
│           agent/llm.py :: brain_stream_sentences         │
└───────┬──────────────────────────────────────────────────┘
        ↓
┌─ 能力层 agent/* ─────────────────────────────────────────┐
│ asr_tts │ inproc_tools │ memory_store │ persona │ vision │
└───────┬──────────────────────────────────────────────────┘
        ↓
┌─ 工具层 magic-*.py（24 个模块 / 136 个工具）─────────────┐
└──────────────────────────────────────────────────────────┘
        ↓
┌─ 状态层 agent/state.py · llm_state.py · state_sync.py ───┐
│           app/state.py（指标/连接表/限流）               │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 语音全链路：四条通道

### 3.1 通道对照

| 通道 | 入口函数(文件:行) | 上行 | 下行 | 会话 ID |
|---|---|---|---|---|
| HTTP 一次性 | `voice_api` (`app/routes/conversation.py:255`) | 整段音频 | base64 MP3 | 请求体 `session_id` |
| HTTP 流式 | `chat_stream_api:285` / `voice_stream_api:307` | 音频或文本 | SSE：`asr/ack/text/audio/done` | 同上 |
| 浏览器 WS | `websocket_endpoint` (`app/routes/websocket.py:362`) | `text`/`audio_stream` | JSON 事件 | `_ws_join_session:116` |
| ESP32 小智 WS | `xiaozhi_websocket` (`app/xiaozhi_ws.py:552`) | Opus 60ms 帧 | Opus 帧 + JSON 控制 | 连接级 |
| ESP32 MQTT | `_process_utterance` (`app/mqtt_server.py:690`) | AES-CTR 加密 UDP | 同左 | `mqtt-{device_id}` |
| 飞书 | `_handle_feishu_message` (`app/routes/feishu.py:74`) | 文本/图片 | 文本 | `feishu_{open_id}` |

### 3.2 一次对话的调用顺序

```
ASR 阶段
  opus_decode / to_wav            app/audio.py, app/xiaozhi_codec.py
  likely_empty_audio              app/audio.py:26          # 静音短路
  asr_race                        agent/asr_tts.py:496     # SenseVoice/百度/StepFun/Vosk 竞速
  correct_asr_text                agent/asr_correction.py:181
  strip_wake_word                 agent/intent.py:67
  is_low_intent_asr / is_garbled  agent/intent.py:123/133

理解阶段
  FastPath                        voice_agent.py:415       # 本地直出，绕 LLM
  handle_smart_command            agent/device_control.py:348
  _classify_intent                agent/llm.py:331         # 关键词→LLM 分类，产出 mcp_set
  brain_stream_sentences          agent/llm.py:1637        # 工具循环 + 逐句 yield
    └ _brain_run_iter             agent/llm.py:860
    └ 工具分发                    agent/inproc_tools.py:270

输出阶段
  _synth_mp3                      app/xiaozhi_ws.py:287     # 百度→Finna→StepFun→say 四级降级
  TTSParallelPlayer               agent/tts_player.py:22    # 并行合成 + 有序播放
  mp3_to_opus_packets             app/xiaozhi_codec.py:184
  60ms 节奏下发                    app/xiaozhi_ws.py:974
```

### 3.3 ESP32 协议状态机（`app/xiaozhi_ws.py`）

| 阶段 | 函数:行 | 要点 |
|---|---|---|
| 鉴权 | `552-563` | query `token` 比对 `AUTH_TOKEN`/`XIAOZHI_DEVICE_TOKEN`，失败 1008 |
| hello | `1417` | 回 `opus/24000/1ch/60ms` |
| 上行帧 | 主循环 `1219-1404` | `_pcm_rms:95` + `_is_speech_vad_async:247` 双判 |
| 端点检测 | 参数 `55-73` | `SILENCE_FRAMES=20`、`MIN_SPEECH_FRAMES=12`、`MAX_UTTERANCE_FRAMES=600` |
| 打断 | `1318-1346` | `BARGE_IN_FRAMES=3`；10s 内 3 次空打断 → 风暴守卫 |
| 续说 | `1122-1139` | resume 关键词 |
| 单句处理 | `process_utterance:1052` | → `_decode_check_asr:449` → `stream_tts_reply:661` |
| 首句超时 | `_FIRST_SENTENCE_TIMEOUT=92` | 8s → 兜底语 + `rebuild_brain_pool:173` |
| 控制消息 | `1417-1489` | `listen/detect|start|stop`、`abort`、`ping` |

---

## 4. 大脑：意图与 LLM

### 4.1 六条意图路径（按执行顺序）

| # | 路径 | 入口 | 说明 |
|---|---|---|---|
| 1 | 唤醒词/低意图/乱码拦截 | `agent/intent.py:67/123/133` | 正则 + 质量判定 |
| 2 | **FastPath** 本地直出 | `voice_agent.py:415`，`FAST_PATHS:495` | 时间/场景/角色等；无 LLM |
| 3 | 设备直控正则 | `agent/device_control.py:348` | 空调/音量/音乐/天气/视觉 |
| 4 | Laya 工具快通道 | `voice_agent.py:271` | flag 门控，默认关 |
| 5 | **LLM 分类** | `agent/llm.py:331`，`_KEYWORD_MAP:124` | 关键词表 → 模型 → 缓存 → 熔断 |
| 6 | decider-first | `agent/decide_intent.py:294` | **无生产调用方**（2026-09-20 起停用） |

### 4.2 多提供商降级（`app/llm_config.py` + `agent/llm.py`）

- Provider 链：`LLM_PRIORITY`（`llm_config.py:124`）默认 `stepfun,agnes,sagnes,glm,ark`，`resolve():264` 取首个已配置者。
- 模型级轮换：GLM 429 → `rotate_glm_model():377`。
- Key 级轮换：StepFun 多 Key `agent/stepfun_keys.py:64 next_key`。
- Brain 级：`_build_brain:777`（LRU 上限 12）、`_try_rotate_provider_on_failure:1136`（阈值 2 次）、`_record_brain_failure:964`（熔断上限 5）。
- 兼容补丁：`_install_openai_compat:528`、`_install_mcp_timeout_patch:561`、`_install_mcp_fault_tolerance_patch:625`。
- HTTP 重试：`agent/retry.py:43`（3 次，退避 1/3/10s）。

### 4.3 流式与缓存

- `brain_stream_sentences:1637`：工具循环（`_MAX_TOOL_STEPS=5`）+ 分句（`MIN_CHUNK=15`/`MAX_CHUNK=80`，`agent/llm_state.py:46`）。
- 闲聊快速通道 `_chat_lite_stream:1473`，独立超时预算。
- 回答缓存 `agent/cache.py`（TTL 300s / 500 条），含"被中断回答复用" `_cache_get_interrupted:41`。

---

## 5. 记忆系统：六层

| 层 | 介质 | 写 | 读 |
|---|---|---|---|
| 短期历史 | `history.json`（flock + 原子替换，30 条/10 会话） | `history.py:209 _append_history` | `:116 _get_history` |
| 工作记忆 | 进程内存（thread-local，16 会话上限） | `working_memory.py:83 update` | `:69 get` |
| 长期结构化 | SQLite `memory[_uid].db`（facts/events/sessions/corrections/semantic） | `memory_store.py:215 upsert_fact`、`:387 add_event` | `:250 get_fact`、`:601 format_memories_for_prompt` |
| 情景叙事 | `episodic_memories[_uid].json`（bigram 相似度 + 时间衰减） | `magic-memory.py:216 remember_conversation` | `:258 get_relevant_memories`、`:634 recall_hybrid` |
| 用户画像 | `user_profile[_uid].json` | `user_profile.py:315 record_explicit` | `:181 get_user_profile`、`:405 format_profile_for_prompt` |
| 人设/偏好 | `persona[_uid].json`、`preferences.json` | `persona.py:128 update`、`preferences.py:168 set_preference` | `:186 get_persona_prompt`、`:183 list_preferences` |

多用户：`agent/user_context.py` 用 `ContextVar` 把 session → uid → 分片文件，`set_session_user:63` / `resolve_user:100`。
提示词装配：`agent/system_msg.py:224 _build_system_msg`（动静分离 + 300s 缓存）。

---

## 6. 工具层：136 个 MCP 工具

### 6.1 注册机制

```
magic-*.py
  ├ __mcp_meta__ = {name, tier, required_env}     ← mcp_registry 用 ast 解析，不执行
  └ mcp = FastMCP(name) + @mcp.tool()             ← schema = 签名 + docstring

主路径（默认，MCP_INPROC=1，进程内直调）
  agent/inproc_tools.py:270 resolve_inproc_tools
    → :24 _load_mcp_module（spec_from_file_location）
    → :60 _extract_tools_from_mcp（读 mcp._tool_manager._tools）
    → :107 _make_tool_class（动态生成 BaseTool 子类）
  挂载点：agent/llm.py:823 _build_brain_inner

回退路径（MCP_INPROC=0，stdio 子进程）
  app/mcp_registry.py:55 _build_all_mcp → charlie_main.py:131 _run_mcp_server → mod.mcp.run()
  工具名带前缀 "<server>-<tool>"
```

门控：`app/mcp_gate.py:50 resolve_mcp_profile`，`MCP_PROFILE=core|all|custom`。
层级：`tier=core` 8 个模块（info/reminder/notes/system/scenes/evolution/summary/memory），`optional` 其余。

### 6.2 工具清单（按模块）

| 模块 | 工具数 | 主要工具 |
|---|---|---|
| `magic-info.py` | 11 | `get_current_time`、`get_detailed_weather`、`get_news`、`get_location`、`translate`、`calculate`、`get_holiday_info`、`get_exchange_rate`、`on_this_day`、`get_my_location`、`run_code`（禁用桩） |
| `magic-jarvis.py` | 15 | 股票/大盘/AQI/地震/赛事 + `proactive_greeting`、`suggest_action`、`morning_briefing`、`evening_report` + 3 个克隆音色工具 + 2 个服务状态 |
| `magic-apps.py` | 13 | `open_wechat`、`open_alipay`、`browse_{douyin,toutiao,meituan,taobao,jd,pdd,dianping,maoyan,damai,xianyu,feishu}`（ego-browser） |
| `magic-recipe.py` | 9 | `search_recipe`、`get_recipe`、`recommend_daily`、`recommend_by_context`、`generate_recipe` … |
| `magic-music.py` | 8 | `search_music`、`play_music`、`play_random_music`、`stop_music`、`list_playlists`、`play_playlist`、`play_white_noise`、`set_alarm` |
| `magic-reminder.py` | 7 | `add_reminder`、`list_reminders`、`delete_reminder`、`set_timer`、`get_calendar_today`、`schedule_task`、`list_alarms` |
| `magic-scenes.py` | 7 | `goodnight`、`good_morning`、`movie_time`、`leaving_home`、`learn_protocol`、`list_protocols`、`execute_scene` |
| `magic-tv.py` | 6 | DLNA：`get_tv_status`、`set_tv_volume`、`set_tv_mute`、`tv_play_pause`、`tv_stop`、`cast_to_tv` |
| `magic-browser.py` | 6 | `browse_page`、`search_web`、`read_page`、`click_element`、`screenshot`、`fill_form` |
| `magic-notes.py` | 5 | `save_note`、`list_notes`、`add_shopping_item`、`list_shopping_items`、`clear_shopping_list` |
| `magic-memory.py` | 5 | `recall`、`memory_status`、`correct`、`dedup`、`forget` |
| `magic-wardrobe.py` | 5 | `outfit_recommend`、`wardrobe_list`、`add_clothing`、`remove_clothing`、`_get_default_city`（误挂） |
| `magic-evolution.py` | 4 | `learn_from_history`、`self_optimize`、`suggest_preferences`、`evolution_status` |
| `magic-summary.py` | 4 | `daily_summary`、`yesterday_summary`、`weekly_summary`、`summary_stats` |
| `magic-preferences.py` | 4 | `set_pref`、`get_pref`、`forget_pref`、`list_prefs` |
| `magic-habits.py` | 4 | `log_habit`、`habit_streak`、`habit_today`、`habit_week` |
| `magic-life.py` | 4 | `open_lifestyle_app`、`search_charging_stations`、`control_tesla_ac`（纯文案桩）、`leaving_home` |
| `magic-feishu.py` | 4 | `search_docs`、`send_message`、`list_messages`、`get_calendar` |
| `baize_skills_mcp.py` | 4 | `web_search_free`、`web_search`、`shopping_search`、`deep_research` |
| `magic-system.py` | 3 | `set_volume`、`set_speech_speed`、`system_status` |
| `magic-roles.py` | 3 | `switch_role`、`list_roles`、`get_role_info` |
| `magic-douyin.py` | 3 | `search_videos`、`get_trending`、`get_video_info` |
| `magic-taobao.py` | 3 | `search_products`、`search_jd`、`compare_price` |
| `magic-dev.py` | 3 | `list_project_files`、`read_file`、`edit_file`（改动需飞书回"确认"） |
| `magic-decisions.py` | **0** | 纯库，被调度器 import，不暴露工具 |

---

## 7. IoT 与外部集成

### 7.1 涂鸦红外（`tuya_api.py`，946 行）

两套凭证、两套 API：

| 类 | 凭证 | 用途 |
|---|---|---|
| `TuyaAPI`（2C 用户侧）`:88` | `TUYA_API_KEY` Bearer | 家庭/房间/设备、属性下发（仅影子，不发红外）、IPC 抓拍 |
| `TuyaCloudAPI`（2B 云侧）`:510` | `TUYA_CLIENT_ID`+`TUYA_ACCESS_KEY`，HMAC-SHA256 | **红外发码**、遥控器列表、空调状态 |

发码顺序（`ac_scenes_command:621`）：关机只发 `power=0`；开机按 `power → mode → temp → wind` 逐条 HTTP。
调用方：`agent/device_control.py:70 direct_ac_control`（含否定守卫、疑问句排除、8s 超时）。
`HTTP /api/tuya/ac/control` 已硬禁用（403），场景里的 `ac_control` 步骤也换成占位串——只有语音快路径能控空调。

### 7.2 飞书双通道

| 通道 | 入口 | 说明 |
|---|---|---|
| WebSocket 长连接 | `feishu_ws.py:383 start`（lifespan 启动 + 60s watchdog） | 无需公网，SDK 自动重连 |
| HTTP Webhook | `app/routes/feishu.py:24` | 校验 `FEISHU_VERIFICATION_TOKEN`，未配置则 503 |

收消息：`_on_message_receive:165`（去重 LRU 300s/200 条）→ `_process_message_sync:251`（先 `check_feedback` 识别确认/取消，再走 brain）。
token 全局单例：`feishu_token.py`（7000s 刷新，400/401 时 `invalidate()` 强刷）。

### 7.3 MQTT 常驻通道（`app/mqtt_server.py`）

`MqttXiaozhiServer:116`：paho 连 broker + 绑 UDP 8888，AES-CTR 加密音频，订阅 `charlie/esp32/+/up`。
`hello` 生成 AES key 回下发；`listen/goodbye/abort` 走 MQTT JSON；音频走 UDP。
推送：`push_tts:418`、`push_notification:466`、`push_tts_to_mqtt:934`。

---

## 8. 主动能力（无用户输入也会说话）

### 8.1 决策引擎（`magic-decisions.py`，1150 行）

- 12 条规则（`_DECISION_RULES:142`）：`late_night_sleep`(90) / `weather_alert`(85) / `morning_wakeup`(80) / `meeting_reminder`(75) / `leaving_reminder`(70) / `arrive_home`(65) / `deadline_reminder`(60) / `evening_wind_down`(50) / `evening_wrapup`(45) / `lunch_reminder`(40) / `sedentary_break`(35) / `casual_checkin`(10)。
- 评估 `evaluate:751`：状态匹配 → 时间窗（支持 callable）→ 冷却 → extra_check → 按优先级排序。
- 三层防重：规则冷却 12h（`mark_triggered:102`）、confirm 询问节流（1h/8h + 每日上限 2 次）、机器级 flock（`reminders.py:88`）。
- 确认闭环：`set_pending_confirmation` → 飞书回"确认" → `check_feedback:1017` → `execute_decision:888`。
- 反馈调权：`decision_feedback.json`，得分 <0.4 直接跳过该规则。

### 8.2 场景引擎（`magic-scenes.py`，694 行）

内置 4 个（晚安/早安/电影/出门），自定义存 `protocols.json`（mtime 缓存）。
步骤执行器 `_STEP_EXECUTORS:432`：`ac_control`（禁用）/`ac_sleep`/`tv_control`/`volume`/`reminder`/`tts`/`wait`/`if_condition`/`llm`。
学习 `learn_protocol:621`：LLM 出 JSON 步骤，失败降级关键词解析。
去重三件套：`protocols.json`（定义）、`scene_trigger_dedup.json`（时间/天气触发每日一次）、`suggestions_state.json`（auto_trigger 每日一次，跨重启有效）。

### 8.3 自进化（`magic-evolution.py`，520 行）

`learn_from_history:302`：扫全量历史 → 话题分布/高频问/活跃时段 → 平均句长决定回复风格（极简/平衡/详细）→ 写 `evolution_data[_uid].json`。
`self_optimize:371`：按打断率调风格与最大句数。
消费端：`system_msg._build_user_profile` 拼进 system prompt；`personalized_push` 做兴趣画像。
定时：启动一次 + 每 1800s（`schedulers.py:686`）。

### 8.4 个性化推送（`personalized_push.py`）

兴趣画像 → 抖音热搜 → 7 天去重 → LLM 选 3 条（只许匹配原始热点）→ 飞书富文本。默认 1h，静音 22:00–07:00。

### 8.5 提醒系统（`app/reminders.py`，751 行）

状态机 `pending → delivering → delivered / retry / failed / expired / expired_enqueued`。
关键：`claim_due_reminders:646`（原子申领，`DELIVERY_CLAIM_TIMEOUT=900s`）、`DELIVERY_RETRY_DELAYS=[60,180,600]`、`EXPIRED_AFTER_HOURS=2`（锚定 `due`）、`restore_after_restart:522`、循环生成 `_generate_next_occurrence_locked:467`。

### 8.6 调度器（`app/schedulers.py`，828 行）

| Job | 频率 | 做什么 |
|---|---|---|
| 提醒 | 30s | `claim_due_reminders` + 投递 |
| 主动建议 | 60s | 场景触发、用户状态、天气、晚安/早安、系统告警、飞书 watchdog |
| 自进化 | 1800s | `learn_from_history()` |
| 决策 | 120s | `evaluate` + 待确认处理 |
| 本地唤醒 | 启动 20s 后 | `local_wake.start_wake_detector` |
| WS 清理 | 60s | 过期连接/会话桶 |

---

## 9. HTTP API 全景

中间件链（`voice_server.py`）：15MB 请求体限制 → `DynamicCORSMiddleware:249`（CORS + IP 限流 60/min、语音 10/min）→ `request_logger:268`（鉴权 + 会话限流 + 指标 + `X-Request-ID`）。
豁免鉴权：`/xiaozhi/ota`、`/ws/xiaozhi`、`/api/feishu/webhook`、`/aligenie*`。

### 9.1 页面与静态

| 方法 | 路径 | 文件 |
|---|---|---|
| GET | `/` | `web/voice.html` |
| GET | `/manage` | `web/manage.html` |
| GET | `/setup` `/welcome` `/test` `/esp32-setup` | 对应页面 |
| GET | `/dashboard` | 内联 HTML 监控面板 |
| GET | `/manifest.json` `/service-worker.js` `/icon.svg` `/favicon.ico` | PWA |

### 9.2 核心接口

| 方法 | 路径 | 处理函数 | 用途 |
|---|---|---|---|
| POST | `/api/voice` | `conversation.py:254` | 音频→ASR→brain→TTS |
| POST | `/api/voice/stream` | `:306` | 同上，SSE |
| POST | `/api/chat` | `:341` | 纯文本 |
| POST | `/api/chat/stream` | `:284` | 文本流 |
| POST | `/api/asr` `/api/tts` | `:411` / `:385` | 单点能力 |
| POST | `/api/reset` | `:367` | 清历史 |
| GET | `/api/conversation` `/api/search` `/api/export` | `:376`/`:563`/`:508` | 历史读/搜/导出 |
| POST | `/api/brain/restart` | `:598` | 重建 brain |
| WS | `/ws` | `websocket.py:362` | 浏览器双向 |
| WS | `/ws/xiaozhi` | `xiaozhi_ws.py:552` | ESP32 |
| GET/POST | `/xiaozhi/ota` | `reminders.py:128` | OTA 配置下发 |

### 9.3 管理与配置

| 方法 | 路径 | 处理函数 |
|---|---|---|
| GET | `/health` `/api/status` `/api/version` `/api/metrics` `/api/audit` | `manage.py:218` / `system.py:199` / `:173` / `manage.py:143` / `:148` |
| GET/POST/DELETE | `/api/preferences` | `manage.py:247/255/263` |
| GET/POST | `/api/roles` `/api/roles/switch` | `:271` / `:295` |
| GET/POST | `/api/setup`、`/api/setup/verify`、`/api/setup/mcp-status`、`/api/setup/llm-priority` | `:651/664/685/726`、`:155/174` |
| GET/POST | `/api/mcp/servers` `/api/mcp/toggle` | `:996` / `:1005` |
| GET/DELETE | `/api/memory` | `:453` / `:473` |
| GET | `/api/decisions` `/api/protocols` `/api/evolution` `/api/behaviors` `/api/habits` | `:326` / `:856` / `:882` / `:380` / `:424` |
| GET/POST | `/api/wake/status` `/api/wake/toggle` | `:918` / `:910` |
| GET/POST | `/api/user/current` `/api/user/switch` | `:931` / `:923` |
| GET/POST | `/api/tts/voices` `/api/tts/voice` | `:960` / `:970` |
| GET | `/api/logs` `/api/devices` `/api/context` `/api/sessions` `/api/tunnel` `/api/lan-info` | 各自 |
| POST | `/api/esp32/flash` `/api/esp32/detect-port` `/api/setup/download-model` | 后台任务 |
| GET | `/api/events` | SSE 通知流（30s 心跳） |

### 9.4 第三方回调

| 路径 | 处理 |
|---|---|
| `POST /api/feishu/webhook` | 飞书事件（token 校验，未配置 503） |
| `POST /aligenie` | 天猫精灵云技能（open_id 白名单） |
| `GET /api/tuya/devices` 等 | 涂鸦 2C 只读 + 属性下发 |

---

## 10. 前端（`charlie/web/`，原生 JS，无构建）

| 文件 | 行数 | 做什么 | 关键技术点 |
|---|---|---|---|
| `voice.html` | 728 | 主界面：麦克风球、流式对话、侧栏（提醒/偏好/音色/导出） | **双通道**：WS + 手写 fetch-SSE 解析器；`MediaRecorder`+`AudioContext` 做 VAD；`SpeechRecognition` 浏览器端唤醒；MP3 队列播放；Service Worker |
| `voice_test.html` | 299 | 语音诊断台：ASR 原文、延迟指标、事件日志、设备枚举 | 纯 fetch-SSE，不用 WS |
| `manage.html` | 487 | 9 个运维 tab（状态/记忆/决策/协议/进化/日志/MCP/音色/延迟） | 懒加载 + 模板字符串 |
| `setup.html` | 275 | 全量环境变量配置，按 `env_catalog` 分组，LLM 优先级拖拽 | HTML5 draggable |
| `welcome.html` | 213 | 首启三步引导（百度语音 → GLM → 完成） | 按 `/api/welcome/status` 自动定位 |
| `esp32_setup.html` | 180 | 检测串口 → 烧录 → AP 配网 | `setTimeout` 轮询 |
| `service-worker.js` | 45 | 离线缓存 + push 通知 | Cache API |

---

## 11. Baize（TypeScript 平行系统）

### 11.1 主链路（V3）

```
CLI cli/index.ts:145 handleInput
  → core/brain-v3/index.ts:178 processStream
    → core/executor/UnifiedExecutor.ts:248 executeStream
      ├ quickClassify:406  规则 → LLM 分类
      ├ 简单 → generateDirectResponse:822
      └ 复杂 → goalDefiner.analyzeTask → selectStrategy:490
              → generatePlan:523（skills+tools 清单进 prompt，LLM 出 {tool,params}）
              → executePlan:639 → skill.run / tool.safeExecute
              → resultValidator.validate:372
              → experienceStore.save:377
              → generateResponse:786
```

### 11.2 模块

| 目录 | 行数 | 职责 |
|---|---|---|
| `core/` | 10,396 | V1/V2/V3 三代大脑、路由、规划、状态、经验库 |
| `executor/` | 4,674 | `Executor`（唯一的 hooks/policy/approval/sandbox 编排点）、ReAct V2、并行执行器、子 Agent |
| `skills/` | 3,325 | `SkillRegistry` + `SkillLoader`（SKILL.md → 动态 Skill）+ ClawHub 市场客户端 |
| `tools/` | 2,723 | `ToolRegistry` + 10 个内置工具 |
| `memory/` | 2,566 | sql.js 数据库 + v1/v2/v3 三代记忆 + 向量检索 |
| `evolution/` | 1,109 | 四角色评审 → 权限 → 审批 → 改自己代码 |
| `sandbox/` `approval/` `policy/` `hooks/` `recovery/` `security/` | 合计 ~2,800 | 安全执行链（**在 V3 链路上不生效**，见 11.6） |

### 11.3 记忆与向量

- 存储：sql.js（纯 JS SQLite），`memory/database.ts:188` 每次写都全量 export 落盘。
- 三代并存：`MemorySystem`(v1) / `MemorySystemV2`(v2, 无人引用) / `EnhancedMemory`(v3, 在用)。
- 向量：`memory/vector.ts:118 embed` 先试 `llm.embed`，失败走 `hashEmbed`（字符散列，非语义）；检索是全量暴力余弦，无 ANN。
- 两个 LLM provider（OpenAI 兼容 / Ollama）**都没实现 `embed()`**，所以实际永远是哈希向量。

### 11.4 CLI 与 HTTP

- CLI：`start`（默认交互）/ `chat <msg>` / `test` / `skill` / `help`。
- HTTP：`server.js` 起 Express（3000）+ 静态服务器（8080）。路由 `/health`、`/api/chat`、`/api/chat/stream`（SSE）、`/api/skills`、`/api/memory/*`、`/api/cost/*`、`/api/reminders/stream`。

### 11.5 前端

`Baize/web/`：原生 JS 五页签（chat/skills/memory/cost/config），`api.js` 自动探测端口 3000/3001/3002/8080。

### 11.6 偏差清单（读代码得出，未运行验证）

1. **安全链未接入**：V3 的 `executePlan:639` 直接 `skill.run`/`tool.safeExecute`，绕过 hooks/policy/approval/sandbox/recovery/security。这六个模块只在遗留 `Executor` 里闭环。
2. **审批无法完成**：`ApprovalManager` 挂起等用户，CLI/API 都没 approve 入口，只能等 300s 超时。
3. **沙箱非强制**：无 Docker 时 `createFallbackSandbox:150` 直接在主机 `spawn bash`。
4. **文档型技能 = 命令执行**：`loader.ts:541 executeShellCommand` 从 SKILL.md 抓 ```bash``` 块直接 `exec`。
5. **向量检索是假语义**：见 11.3。
6. **README 与实现不符**：`baize web`/`baize api`/`/api/skills/execute`/`/api/skills/market/*` 都不存在。
7. **四代循环并存**：V1/V2/V3 + 两套并行执行器，大量模块（`sandbox/manager.ts`、`memory/v2.ts`、`knowledge/`、`plugins/`、`scheduler/`、`watcher/`、`security/`、`context/`）仅 barrel 导出，无人调用。
8. **`docker/Dockerfile` 不存在**，`docker compose up` 会失败。

---

## 12. 测试（`charlie/tests/`，122 文件 / ~1979 测试）

| 主题 | 文件数 | 覆盖 | 代表文件 |
|---|---|---|---|
| 历轮回归 `test_roundN_*` | 52 | 每轮修一个 P0 并留回归线 | `test_round10_llm.py`、`test_round33_log_driven.py` |
| FR-14 多用户隔离 | 8 | uid 分片、会话内容出共享 brain、设备→用户绑定、提醒路由、音色隔离 | `test_fr14_batch3b_tool_data_scope.py`(776L) |
| ASR/TTS/音频 | 19 | provider 优先级、SenseVoice 竞速、StepFun 多 Key、并行 TTS、克隆音色、本地唤醒 | `test_asr_tts_priority.py`、`test_local_wake.py` |
| 意图/路由/指代 | 5 | 意图头弃权回落、关键词→LLM 路由、多轮指代 | `test_decide_intent.py`、`test_coreference.py` |
| 提醒/推送 | 5 | 重复投递、无限重试上限、静默时段、离线兜底、事故时序回放 | `test_incident_replay.py` |
| MCP/MQTT/ESP32/编解码 | 6 | MQTT AES、xiaozhi WS（真起 uvicorn）、Opus 编解码、烧录向导 | `test_mqtt_server.py`(558L) |
| 安全 | 2 | 安全求值、请求体限制、XFF 伪造、AST 静态扫描 | `test_security_fixes.py`、`test_uncovered_security.py` |
| 持久化/并发 | 6 | 原子写+flock、200 并发读写、半截文件降级 | `test_persistence_race.py`、`test_runtime_resilience.py`(1413L) |
| 记忆/画像/上下文 | 5 | SQLite 记忆、画像聚合、ContextVar 优先级 | `test_memory_store.py` |
| HTTP/页面/产品化 | 7 | health/status/metrics、manage 路由、setup、Demo 模式 | `test_voice_server.py` |
| magic 工具域 | 7 | bigram 相似度、决策冷却、场景、进化、AST 覆盖率 | `test_magic_coverage.py` |
| 基础设施 | 8 | 证书、preflight、MCP 分层、天气降级、LLM 注册表 | `test_mcp_gate.py` |

CI（`.github/workflows/ci.yml`）只跑 12 个白名单测试文件 + `compileall`。

---

## 13. 工程与发布

| 项 | 状态 |
|---|---|
| `requirements-core.txt` | ~75 个全钉版本包，含平台条件依赖 |
| `requirements-optional.txt` | silero-vad / sounddevice / vosk / opuslib / sherpa_onnx / paho-mqtt |
| `Dockerfile` | **已失效**：`COPY mcp_ir_control.py`（不存在）、漏 `COPY agent/`、漏 `COPY static/` |
| `release.yml` | test → 三平台 PyInstaller → upload-artifact，**没有发布 job** |
| 版本 | CHANGELOG 的 `[未发布]` 约 1180 行，远大于已发布段 → 自 3.2.0 后长期未发版 |
| `env_catalog.py` | 60+ 环境变量单一来源，`setup.html` 按它渲染 |
| `runtime_settings.py` | 5 个热更新键（device.* / tts.*），但 `device.*` 未接入 `/xiaozhi/ota` |

---

## 14. 已确认的缺陷（读代码 + grep 验证）

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| 1 | `app/schedulers.py:558` | `minute` 未定义（该作用域只有 `hour`，L403） | 早间 7:30 简报分支 `NameError`，被外层 `except` 吞掉 → **永不执行** |
| 2 | `magic-scenes.py:117` | 用 `tempfile.mkstemp` 但未 `import tempfile`（L19-21 无） | 首次保存自定义场景 `NameError`，被 `except` 吞 → "学了但没存" |
| 3 | `agent/system_msg.py:375/408/417` | `get_persona_prompt(uid)` / `list_preferences(uid)` / `get_current_mood(uid)` 与实际签名不符 | 画像/偏好静默缺失 |
| 4 | `agent/user_profile.py:396` | 导入不存在的 `_spawn_memory_thread` | 异步记忆写入分支不生效 |
| 5 | `magic-wardrobe.py:250` | `_get_default_city` 误挂 `@mcp.tool` | 内部函数暴露成工具，写进系统提示 |
| 6 | `magic-dev.py` | 无 `if __name__ == "__main__": mcp.run()` | stdio 模式下该 server 立即退出，只能用 inproc |
| 7 | `baize_skills_mcp.py:167-171` | `mcp.run()` 在 `deep_research` 定义之前 | 直接跑脚本时少注册一个工具 |
| 8 | `app/xiaozhi_ws.py:1094` | 引用未定义 `DATA_DIR` | `ASR_DUMP_WAV=1` 时抛错（被吞） |
| 9 | `fcntl_compat.py` | Windows 无 fcntl 时 `flock` 退化为 no-op | **Windows 上"机器级锁"实际不互斥** |
| 10 | `tuya_proxy.py` | `/api/tuya/ac/control` 首行 `raise 403` | 后续逻辑全是死代码（有意为之的安全边界） |
| 11 | `agent/decide_intent.py:294`、`agent/magic_agent.py:240` | 无生产调用方 | 死代码，但测试仍在跑 |
| 12 | `charlie/static/` 2 个 PNG | 全仓无引用，未进 `charlie.spec`/Dockerfile | 孤立资产 |
| 13 | `OOTB/charlie/skills/mimo-vision`、`src/skills/mimo-vision` | 空 gitlink，无 `.gitmodules` | `feishu_ws.py` 指向的 `vision.py` 缺失 → 飞书图片消息断链 |
| 14 | `docs/LOGGING.md` | `.env.example` 引用但文件不存在 | 断链 |

---

## 15. 跨文档汇总的未完成项

1. 聊天主模型仍是待替换的 `step-5-preview`（CHANGELOG 记载"等用户决定"）。
2. 全双工连续对话未做（ESP32 侧仍是半双工端点检测）。
3. MQTT 下行 seq 语义需固件真机联调。
4. Release 流水线缺最后发布 job。
5. `mimo-vision` 子模块空壳（同上 #13）。
6. 社媒自动化发布已弃用（封号风险），但文档/脚本仍残留且互相漂移（`cover_geometric.py` 有 3 份不同副本）。
7. `[未发布]` 长期未打 tag，版本停在 3.2.0。

---

## 附：三个最容易上手的入口

| 想做什么 | 从哪里读 |
|---|---|
| 改语音链路 | `charlie/app/xiaozhi_ws.py:1052 process_utterance` → `agent/llm.py:1637 brain_stream_sentences` |
| 加一个工具 | 新建 `charlie/magic-xxx.py`，`FastMCP` + `@mcp.tool()`，填 `__mcp_meta__`，在 `app/mcp_gate.py` 归到 core/optional |
| 查某句话为什么这么回 | `charlie/app/manage.py:936 /api/logs` 读 `logs/app.log`；`/api/audit` 看功能调用；`test_round33_log_driven.py` 是照着日志修 bug 的范例 |
