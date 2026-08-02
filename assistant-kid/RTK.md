# RTK 记录与项目交接

## RTK 基本信息

- 全局 RTK 配置：`/Users/sxliuyu/.claude/RTK.md`
- 当前二进制：`/opt/homebrew/bin/rtk`
- 当前版本：`rtk 0.35.0`
- 最近核验：`rtk gain` 可用；历史记录显示全局累计 3140 条命令、节省约 6.5M tokens、节省率 90.4%。
- 元命令直接运行：

```bash
rtk --version
rtk gain
rtk gain --history
rtk discover
rtk proxy <cmd>
```

普通命令由 hook 透明改写，例如 `git status` 会走 `rtk git status`。不要为了使用 RTK 手工包一层命令；需要绕过过滤时才用 `rtk proxy`。

## 本项目运行信息

- 项目目录：`/Users/sxliuyu/orca/projects/助手小子/assistant-kid`
- HTTP 服务：`0.0.0.0:8000`
- HTTPS 服务：`0.0.0.0:8443`
- 默认端口可通过环境变量覆盖：
  - `ASSISTANT_KID_HTTP_PORT`：HTTP 监听端口，默认 `8000`
  - `ASSISTANT_KID_HTTPS_PORT`：HTTPS 监听端口，默认 `8443`
- 已核验局域网 IP：`192.168.1.4`（`en1`）
- 局域网访问：
  - `http://192.168.1.4:8000`
  - `https://192.168.1.4:8443`
- 手机或其他设备必须和电脑在同一 Wi-Fi/局域网；若打不开，先确认设备没有走代理或 VPN。
- 如果本机 shell 启用了代理，本机 curl 访问局域网地址时加 `--noproxy '*'`。

## 文件与日志位置

- 主日志：`logs/app.log`
- 语音服务输出：`logs/voice_server.out`
- HTTPS 服务输出：`logs/https_server.out`
- 对话历史：`conversation_history.json`
- 提醒数据：`reminders.json`
- 偏好数据：`preferences.json`
- 运行时路径支持环境变量覆盖：
  - `ASSISTANT_KID_DATA_DIR`：历史、提醒、偏好目录
  - `ASSISTANT_KID_LOG_DIR`：日志目录
  - `LOG_FORMAT`：`text` 或 `json`

## 当前测试状态

运行命令：

```bash
.venv/bin/python -m pytest -q -rA
git diff --check
```

当前状态：

- 2026-08-02 03:19 CST 收口前端异步音频竞态、降级播放器资源释放和 barge 定时器清理：`web/voice.html` 新增 `audioPlaybackGeneration`，`playNextAudio()` 为每次待播放 chunk 捕获 `playbackGeneration`，`decodeAudioData()` 成功回调、解码失败降级回调、WebAudio `onended`、HTMLAudio `onended/onerror` 和 `play().catch` 都在推进队列前检查代际和对象身份；`stopAllAudio()` 递增代际并 `clearTimeout(bargeTimer)`，使手动打断或自动 barge-in 后迟到的解码/播放回调不能再启动已取消的 TTS，也不留下待触发的自动打断计时器。自动 barge-in 起播保护保持为连续播放会话开始时记录一次 `speakingStartedAt`，同队列后续 chunk 不重置，必须同时满足音量 `>0.45`、连续 650ms、距离起播至少 900ms；手动打断仍即时停止音频、abort SSE/fetch 并发送 interrupt。HTMLAudio 降级路径现在保存 `audioUrl`，在正常结束、错误、播放失败和 `stopAllAudio()` 暂停当前音频时通过幂等 `releaseHtmlAudioUrl()` 调用 `URL.revokeObjectURL()`，避免长时间语音对话累积 Blob URL。新增/修正 `test_home_stale_webaudio_decode_callback_does_not_start_after_interrupt`、`test_home_stale_decode_error_does_not_fallback_after_interrupt`、`test_home_html_audio_fallback_releases_object_url`，其中第一个用例从过严的 `letaudioPlaybackGeneration=0` 改为匹配真实声明 `currentHtmlAudio=null,audioPlaybackGeneration=0`；`test_home_barge_in_guard_starts_when_playback_session_begins` 额外覆盖停止时清空 barge 定时器。定向 barge-in/interrupt/资源释放 8 项通过；新鲜全量 `.venv/bin/python -m pytest -q` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 7+5+40+29+28+97+203=409，`.venv/bin/python -m py_compile ...` 与 `git diff --check` 均退出码 0。`logs/app.log` 仍为 19580 行，真实运行尾部停在 `2026-08-02 01:01:37`，03:11、03:16 和 03:19 只有测试运行，没有新的真实用户日志。pytest 输出中的 429、超时、密钥故障转移、mock build failed、`/tmp/nonexistent_test_file.json`、malformed reminders、播放失败、空 ASR 和限流日志来自韧性测试，属于预期覆盖。
- 2026-08-02 03:03 CST 继续收紧自动 barge-in 的起播误打断：复核 `logs/app.log` 共 19580 行，尾部真实运行记录停在 `2026-08-02 01:01:37`；`rg -n "2026-08-02 0[2-9]:|2026-08-02 1[0-9]:" logs/app.log` 无匹配，因此 02:28、02:56 之后没有新的真实用户运行日志，只有测试和开发记录。上一轮把自动打断阈值提高到 `BARGE_THRESHOLD=0.45`、连续命中 `BARGE_DELAY=650ms` 后，仍有一个边角：连续播报刚开始时的手机外放冲击声或回声可能正好落入 650ms 窗口。本轮先新增 `test_home_automatic_barge_in_uses_stricter_threshold_than_wake_word` 的起播保护断言和 `test_home_barge_in_guard_starts_when_playback_session_begins`，RED 确认页面缺少 `BARGE_START_GUARD_MS` 和播放开始时间；实现后一次连续 TTS 播放会话从 idle 进入 `playNextAudio()` 时记录 `speakingStartedAt=Date.now()`，同一队列里的后续 chunk 不重置保护时间，避免短句连续播报时用户迟迟不能打断。自动 barge-in 必须同时满足音量超过 `0.45`、连续 650ms、且距离本次连续播报开始至少 `BARGE_START_GUARD_MS=900ms` 才会停止播报并开始录音。队列播完或 `stopAllAudio()` 会清零该时间，手动打断按钮仍即时 `stopAllAudio()`、`abortActiveStream()` 并发送 `interrupt`，不经过自动保护。定向 `test_home_automatic_barge_in_uses_stricter_threshold_than_wake_word`、`test_home_barge_in_guard_starts_when_playback_session_begins`、`test_home_manual_interrupt_aborts_sse_fallback`、`test_home_manual_interrupt_reports_last_reply` 为 4 项通过；新鲜全量验证结果以本条之后的最终收口记录为准。pytest 输出中的 429、超时、密钥故障转移、mock build failed、`/tmp/nonexistent_test_file.json`、malformed reminders、播放失败、空 ASR 和限流日志来自韧性测试，属于预期覆盖。
- 2026-08-02 02:56 CST 继续按真实用户反馈收口自动 barge-in 误打断：`logs/app.log` 的真实运行尾部停在 01:01:37，之后主要是测试污染，不能当作新回归；历史反馈中“回复的时候打断……就会出现这种情况”对应前端 `updateVolume()` 在 TTS 播放中使用与普通唤醒相同的低阈值 `0.2` 和 300ms 连续窗口，手机外放、洗澡环境噪声或 TTS 回声都可能触发误打断。本轮先新增 `test_home_automatic_barge_in_uses_stricter_threshold_than_wake_word`，RED 确认页面缺少独立 barge-in guard；实现后将普通唤醒阈值保持为 `SOUND_THRESHOLD=0.2/300ms`，自动打断改为 `BARGE_THRESHOLD=0.45` 且连续命中 `BARGE_DELAY=650ms` 才调用 `bargeIn()`，短暂冲击声、点击声和回声不会立即停止 TTS。手动打断按钮保持即时，仍直接 `stopAllAudio()`、`abortActiveStream()`、发送 `interrupt` payload，不经过音量延迟。定向 `test_home_automatic_barge_in_uses_stricter_threshold_than_wake_word`、`test_home_manual_interrupt_reports_last_reply`、`test_home_manual_interrupt_aborts_sse_fallback` 为 3 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 7+5+40+29+28+97+199=405，`.venv/bin/python -m py_compile voice_server.py voice_agent.py app/audio.py app/config.py app/reminders.py app/routes/system.py app/state.py app/auth.py tests/test_voice_server.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、密钥故障转移、mock build failed、`/tmp/nonexistent_test_file.json`、malformed reminders、播放失败、空 ASR 和限流日志来自韧性测试，属于预期覆盖。
- 2026-08-02 02:28 CST 继续收口主动建议去重和晨报天气获取：真实日志线索显示天气小时检查与 8-10 点晨报可能在同一轮主动循环中重复调用高德天气，而偏好类建议的持久化申领使用截断 `pkey[:10]`，长 key 前 10 个字符相同会互相碰撞，同日声明也缺少完整 value 指纹和稳定日期语义。本轮先新增 `test_morning_report_reuses_current_weather_fetch`、`test_morning_report_fetches_weather_once_when_hourly_check_is_skipped`、`test_morning_report_retries_weather_after_empty_hourly_fetch`、`test_preference_suggestions_keep_distinct_state_for_similar_keys`、`test_same_day_preference_suggestion_survives_restart`，RED 分别确认同一轮成功获取天气后晨报再次 `_get_weather()`、相似偏好 key 状态串扰、重启后同日偏好建议会重复出现。实现后 `_proactive_suggestions()` 在单轮循环内复用 `casts` / `weather_loaded`；小时天气检查成功时晨报直接复用结果，若小时检查返回空结果则晨报允许重试一次；偏好状态 key 改为 `_preference_state_key(pkey, pval)`，使用 `sha256(f"{pkey}\\0{pval}")[:16]`，持久化 claim 形如 `YYYY-MM-DD_pref_<fingerprint>`，避免不同 key/value 的同日建议互相遮蔽。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders tests/test_runtime_resilience.py -q -rA` 为 74 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 6+5+40+29+28+93+194=395，`.venv/bin/python -m py_compile voice_server.py tests/test_voice_server.py tests/test_runtime_resilience.py app/reminders.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-02 02:16 CST 继续根据真实日志收口主动天气提醒：核查 `logs/app.log:19483` 的 00:15 “雷阵雨 雷阵雨”和 `logs/app.log:19510` 的 00:54 重复播报，中间 `logs/app.log:19496-19499` 显示 00:20 服务重启；这些日志仍来自天气文案去重和建议状态持久化之前的运行版本。当前代码已通过 `suggestions_state.json` 原子申领保存 `last_weather_check` 和 `last_rain_suggest`，因此新增 `test_proactive_weather_claim_survives_restart_within_same_day`，模拟 00:15 播报后清空模块内存状态、00:54 再轮询，确认只调用一次天气 API、只通知和播放一次，且磁盘状态保留 `last_rain_suggest=2026-08-02`。RED 还发现当前天气循环会遍历高德多日预报，若第一条是明天的雨也会误播成“今天”；新增 `test_proactive_weather_uses_today_forecast_not_tomorrow` 后，`voice_server.py` 增加 `_forecast_for_date(casts, today)`，天气提醒和 8-10 点晨报都只读取当天预报，缺少 `date` 的旧测试/API 返回继续兼容第一条。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders::test_proactive_weather_suggestion_deduplicates_day_and_night_weather tests/test_voice_server.py::TestReminders::test_proactive_weather_claim_survives_restart_within_same_day tests/test_voice_server.py::TestReminders::test_proactive_weather_uses_today_forecast_not_tomorrow tests/test_runtime_resilience.py -q -rA` 为 43 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 6+5+40+29+28+93+189=390，`.venv/bin/python -m py_compile voice_server.py tests/test_voice_server.py tests/test_runtime_resilience.py app/reminders.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-02 02:03 CST 继续收口本地静音预检：`app/audio.py` 新增 `likely_empty_audio(data)`，用标准库 `wave` 解析 16-bit PCM WAV，并以 20ms 分块和 `audioop.rms` 阈值 120 保守识别 3 秒以上长静音；短录音、非 16-bit、解析失败或任一分块有足够能量都 fail-open 继续交给 ASR，避免误杀真实小声说话。`/api/voice`、`/api/voice/stream`、`/ws` 和 `/api/asr` 现在都在远端 ASR 前执行该检查；长静音路径不调用远端 ASR、大脑或 TTS，非流式 `/api/voice` 返回 `EMPTY_ASR_TEXT`、`EMPTY_ASR_REPLY`、空音频和 `degraded=true`，流式 SSE/WebSocket 返回既有空 ASR 事件，独立 `/api/asr` 返回 `{"text": ""}`。修复上一轮未完成补丁误把 `/api/voice` TTS 降级测试嵌进 `TestAsrAPI` 的结构问题，恢复 `test_voice_tts_unavailable_returns_text_without_audio`，新增 `_tone_wav()` 覆盖非静音音频确实到达远端 ASR。定向 `.venv/bin/python -m pytest tests/test_audio_activity.py tests/test_voice_server.py::TestVoiceAPI tests/test_voice_server.py::TestAsrAPI tests/test_voice_server.py::TestChat tests/test_voice_server.py::TestWebSocket -q -rA` 为 46 项通过；`.venv/bin/python -m py_compile app/audio.py voice_server.py tests/test_audio_activity.py tests/test_voice_server.py` 通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 6+5+40+29+28+93+187=388，`git diff --check` 通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-02 00:47 CST 继续补齐语音 ASR 即时回执体验：真实日志 `logs/app.log:19403` 记录用户反馈“你跟他说完话之后到他回答你中间有一小段的空窗”，`logs/app.log:19417` 与 `logs/app.log:19422` 也明确提出“以后先回复你，再去思考”“收到或嗯”。本轮新增事件 `{"type":"ack","message":"收到，我在听"}`，SSE 和 WebSocket 语音路径均在非空 ASR 后、大脑/TTS 工作前发送给当前说话端；空 ASR 不发送 ack，文字聊天不发送 ack，ack 不写历史、不触发 TTS、不产生模型调用。补充 `test_ws_voice_ack_is_not_broadcast_before_stream_task` 先验证 RED：旧 WebSocket 路径会额外调用 `_ws_broadcast_to_session(... {'type':'asr','text':'你好'}, exclude_self=True)`；GREEN 删除该预广播，保持 ack 仅为当前终端的瞬时 UI 回执，后续脑回复仍由流式任务按既有跨终端逻辑广播。定向 ack 相关 6 项测试通过；语音相关 `TestChat`、`TestWebSocket`、`TestStreamingResilience`、`TestHealthAndStatus` 共 82 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 5+32+29+28+85+170=349，`.venv/bin/python -m py_compile voice_server.py tests/test_voice_server.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 17:57 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试为 238 个用例全部通过；`pytest --collect-only -q` 统计为 2+10+28+21+68+109。
- 2026-08-01 17:57 CST `.venv/bin/python -m pytest tests/test_voice_server.py::TestConditionalGet -q` 为 11 个用例全部通过；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/state.py app/reminders.py tests/test_voice_server.py` 通过；`git diff --check` 通过。
- 2026-08-01 18:03 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试为 239 个用例全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 2+10+28+21+68+110。
- 2026-08-01 18:03 CST `.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py tests/test_voice_server.py` 通过；`git diff --check` 通过。新增的 `test_status_and_dashboard_use_nonblocking_cpu_sample` 已覆盖 `/api/status` 和 `/dashboard` 不再以 `interval=0.5` 阻塞采样 CPU。
- 2026-08-01 18:20 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试为 245 个用例全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 2+11+28+21+68+115。
- 2026-08-01 18:20 CST `.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py tests/test_runtime_resilience.py tests/test_voice_server.py` 通过；`git diff --check` 通过。
- 2026-08-01 18:35 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试退出码为 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+28+21+68+116=246 一致；`git diff --check` 通过。
- 2026-08-01 18:35 CST `.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py tests/test_runtime_resilience.py tests/test_voice_server.py` 通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 2+11+28+21+68+116。
- 2026-08-01 18:44 CST 继续做高频状态路径降载：新增 `test_status_and_dashboard_scan_reminders_once` 和 `test_status_and_dashboard_cache_host_metadata`，先验证旧实现会对提醒列表扫描两次、主机名/OS 元数据会重复读取；实现后 `/api/status` 和 `/dashboard` 都只做一次提醒汇总，并缓存进程级稳定的 hostname/OS 字符串，输出字段和 dashboard 文案保持不变。
- 2026-08-01 18:44 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试退出码为 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+28+21+68+118=248 一致；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py tests/test_runtime_resilience.py tests/test_voice_server.py` 通过；`git diff --check` 通过。
- 2026-08-01 18:52 CST 继续收敛 dashboard 请求路径：新增 `test_preference_count_without_copying` 和 `test_dashboard_uses_preference_count_without_snapshot`，先验证旧实现缺少锁内计数 API，且 dashboard 会通过 `list_preferences()` 复制整份偏好 dict 只为计算长度；实现后新增 `voice_agent.preference_count()`，在 `_prefs_lock` 内直接返回 `len(_preferences)`，`_pref_count()` 改用该 API。完整偏好快照仍保留给 `/api/preferences`、`/api/context` 等确实需要内容的接口。
- 2026-08-01 18:52 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试退出码为 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+28+21+69+119=250 一致；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py` 通过；`git diff --check` 通过。
- 2026-08-01 18:56 CST 记录 dashboard metrics summary 降载：新增 `Metrics.summary(..., include_endpoints=True)`，`/api/metrics` 和 `/api/status` 继续保留每接口耗时/状态码明细，但 dashboard 只读取自身需要的总览指标，明确跳过 per-endpoint breakdown。新增 `test_summary_can_skip_endpoint_breakdown` 与 `test_dashboard_metrics_summary_skips_endpoint_breakdown` 覆盖该行为；该优化后的新鲜全量测试为 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 2+11+28+21+69+121=252，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py` 与 `git diff --check` 均通过。
- 2026-08-01 19:00 CST 继续优化输入清洗热路径：先新增 `test_sanitize_reuses_compiled_patterns`，确认旧实现中 `app.auth` 没有模块级预编译正则且 `_sanitize_text()` 每次调用都会走 `re.sub()` 即时编译；红灯后新增 `_HTML_TAG_RE`、`_JAVASCRIPT_SCHEME_RE`、`_EVENT_HANDLER_RE`、`_CONTROL_CHAR_RE` 四个模块级 `re.Pattern`，函数内改用 `Pattern.sub()`。聊天、提醒和 TTS 请求路径的清洗语义保持不变。定向 `.venv/bin/python -m pytest tests/test_security_fixes.py tests/test_voice_server.py::TestInputSanitization -q -rA` 为 35 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 2+11+29+21+69+121=253，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。
- 2026-08-01 19:21 CST 继续优化 TTS 清洗热路径：先新增 `test_clean_for_tts_reuses_compiled_patterns`，确认旧 `_clean_for_tts()` 每次调用都通过 `re.sub()` 即时编译 Markdown 清洗规则；红灯后新增 `_TTS_BOLD_RE`、`_TTS_HEADER_RE`、`_TTS_BLOCKQUOTE_RE`、`_TTS_TABLE_PIPE_RE`、`_TTS_CODE_BLOCK_RE`、`_TTS_INLINE_CODE_RE`、`_TTS_LIST_ITEM_RE`、`_TTS_MARKDOWN_LINK_RE`、`_TTS_WHITESPACE_RE` 九个模块级 `re.Pattern`，函数内保持原替换顺序并改用 `Pattern.sub()`。粗体、标题、引用、表格、代码块、列表项、Markdown 链接和空白压缩的 TTS 清洗语义不变。
- 2026-08-01 19:25 CST 继续优化 token 估算：先新增 `test_estimate_tokens_reuses_compiled_patterns_once`，确认旧 `_estimate_tokens()` 每次即时编译中英文正则，并且英文词会在同一轮估算中扫描两次；红灯后新增 `_TOKEN_CHINESE_RE`、`_TOKEN_ENGLISH_RE`，函数复用 `findall()` 结果计算中文和英文 token。历史裁剪、上下文限制和缓存键相关行为不变。
- 2026-08-01 19:29 CST 继续优化历史裁剪：先新增 `test_trim_estimates_each_message_once`，确认旧 `_trim_history_tokens()` 在每轮循环中从 `hist[0]` 重新估算全部剩余消息，8 条消息会产生 12 次估算，其中已删除消息还会被重复计算；实现后一次性构建 `token_costs`，按旧到新顺序计算需要删除的前缀长度，再批量 `del hist[:remove_count]`。`min_keep = 4`、旧消息优先删除、删除话题摘要和默认会话 system-message 缓存失效语义保持不变。定向 `.venv/bin/python -m pytest tests/test_voice_agent.py::TestContextManagement -q -rA` 为 9 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`.venv/bin/python -m pytest --collect-only -q` 统计为 2+11+29+21+72+122=257，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、 malformed reminders、播放失败和空 ASR 相关日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 19:42 CST 继续收敛流式 TTS 清洗：先新增 `test_sse_stream_does_not_reclean_brain_sentences`、`test_ws_stream_does_not_reclean_brain_sentences`、`test_public_tts_to_mp3_cleans_markdown_before_synthesis`，确认 SSE/WebSocket 会对 `brain_stream_sentences()` 已清洗句子重复清洗，而公开 `tts_to_mp3()` 在旧路径下会把 Markdown 原文先传给 TTS 再清洗；红灯后新增 `_tts_cleaned_to_mp3()` 供已持有清洗文本的流式路径调用，`_tts_cache_get()`/`_tts_cache_set()` 增加仅关键字的 `cleaned=False` 以避免缓存路径重复清洗，SSE/WebSocket 的 `tts_buffer` 直接追加已清洗句子，公开 `tts_to_mp3()` 仍保持一次自清洗后委托内部 helper。缓存命中的流式回复也统一返回清洗后的文本。定向 `.venv/bin/python -m pytest tests/test_voice_agent.py::TestTTSCache tests/test_voice_server.py::TestStreamingResilience tests/test_voice_server.py::TestReminders tests/test_voice_server.py::TestWebSocket -q -rA` 通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，共 261 项通过，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。
- 2026-08-01 19:45 CST 继续优化 SSE 热路径：先新增 `test_sse_event_frame_uses_compact_json` 和 `test_sse_fixed_frames_are_reused`，确认旧流式路径每个事件通过内联 f-string 调 `json.dumps()`，且 done/heartbeat 等固定帧反复构造；红灯后新增 `_sse_event()` 统一使用紧凑 JSON separators，并新增 `_SSE_DONE_FRAME`、`_SSE_HEARTBEAT_FRAME` 模块级常量，`_stream_brain_tts()` 和空 ASR SSE 降级路径统一复用。事件类型、字段名、文本内容和前端解析语义不变。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestStreamingResilience -q -rA` 为 8 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，共 263 项通过，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。
- 2026-08-01 19:50 CST 统一流式语音超限响应：新增 `test_voice_stream_oversized_audio_matches_voice_error_shape`，先确认旧 `/api/voice/stream` 在 413 时返回 `{"error":"音频过大","status_code":413}`，既泄漏 HTTP 状态字段，也缺少非流式 `/api/voice` 已有的实际大小和上限文案；红灯后改为只返回 `{"error":"音频过大(...KB), 上限...MB"}`，并记录 warning 日志。超限时继续在转码、ASR、大脑和 TTS 前短路返回。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestChat::test_voice_stream_empty_asr_short_circuits_brain tests/test_voice_server.py::TestChat::test_voice_stream_oversized_audio_matches_voice_error_shape -q -rA` 为 2 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，共 264 项通过，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。当前重新核验局域网 IP：`en1=192.168.1.4`，局域网访问仍为 `http://192.168.1.4:8000` 和 `https://192.168.1.4:8443`。
- 2026-08-01 20:02 CST 继续收口 `/api/events` SSE 帧构造：新增 `test_sse_event_heartbeat_frame_uses_compact_json` 与 `test_sse_events_uses_compact_connect_and_heartbeat_frames`，先确认旧 `/api/events` 缺少模块级事件型心跳固定帧，并在函数内重复导入 `StreamingResponse`；红灯后新增 `_SSE_EVENT_HEARTBEAT_FRAME`，connect、通知和 heartbeat 帧统一走 `_sse_event()`，同时移除函数内重复导入。同步修正测试替身：旧 monkeypatch 直接抛 `asyncio.TimeoutError` 会留下 `Queue.get()` 未 await 的 `RuntimeWarning`；新增断言先确认 RED，再把替身改为委托真实 `asyncio.wait_for(..., timeout=0)`，让内部协程按真实取消路径关闭。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestStreamingResilience -q -rA` 为 10 项通过且无 Queue.get warning；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+129=266 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。
- 2026-08-01 20:05 CST 继续优化普通 JSON 响应路径：新增 `test_json_response_without_etag_token_serializes_once`，先确认 `_json_response(..., etag_token=None)` 会对同一 payload 调两次 `json.dumps()`，一次用于内容 ETag，一次用于响应体；红灯后先构造一次 UTF-8 compact JSON body，再直接对 body 字节计算弱 ETag，避免第二次序列化和 `body.decode("utf-8")`。带显式 `etag_token` 的轮询接口仍在命中 304 时不解析 payload；未命中时也只序列化一次。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestConditionalGet -q -rA` 为 13 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+130=267 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。
- 2026-08-01 20:13 CST 继续收敛通知 SSE fan-out：先把 `test_add_notification_reuses_payload_for_polling_and_sse` 改为 `test_add_notification_serializes_sse_frame_once_for_all_clients`，使用两个 SSE 队列确认旧实现会把同一个原始通知 dict 分别放进每个队列，后续每个连接再各自 `_sse_event()` 序列化；RED 明确失败在队列值仍是 dict 而非 SSE frame。实现后 `_add_notification()` 继续把原始 dict 放入轮询缓冲区供 `/api/notifications` 返回，但在存在 SSE 客户端时只调用一次 `_sse_event(notification)`，`_push_notification_to_sse()` 把同一个 frame 字符串调度给所有连接，`/api/events` 消费队列时直接 yield frame。多个手机/网页同时监听提醒时，同一条通知只做一次 JSON 序列化，事件字段和前端解析语义不变。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders::test_add_notification_serializes_sse_frame_once_for_all_clients tests/test_voice_server.py::TestStreamingResilience::test_sse_events_uses_compact_connect_and_heartbeat_frames -q -rA` 为 2 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+131=268 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 20:21 CST 继续收口 SSE 连接集合的跨线程访问：先新增 `test_sse_client_registry_tracks_queues_with_snapshot_copy` 与 `test_push_scheduling_failure_does_not_mutate_sse_clients_from_caller`，确认旧实现只有裸 `_sse_clients` list，`/api/events` 在 asyncio 线程 append/remove，提醒播报线程通过 `list(_sse_clients)` 快照并在调度异常时直接 remove，无法统一验证注册、注销、快照复制和“外部调度失败不由调用线程改集合”的行为；RED 明确失败在缺少 registry helper。实现后在 `app.state` 新增 `register_sse_client()`、`unregister_sse_client()`、`snapshot_sse_clients()`、`sse_client_count()`，使用同一把 `threading.Lock` 保护连接列表；`/api/events`、`/health`、dashboard 和 `_push_notification_to_sse()` 全部走这些入口。后台线程只取快照并调度 `_put_sse_event_nowait()`，真正队列满或队列异常时的注销回到 event loop 回调里执行，避免把连接集合修改散落在多个线程。SSE connect/heartbeat、通知 fan-out 和状态页连接数字段语义不变。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders::test_sse_client_registry_tracks_queues_with_snapshot_copy tests/test_voice_server.py::TestReminders::test_push_scheduling_failure_does_not_mutate_sse_clients_from_caller tests/test_voice_server.py::TestReminders::test_add_notification_serializes_sse_frame_once_for_all_clients tests/test_voice_server.py::TestStreamingResilience::test_sse_events_uses_compact_connect_and_heartbeat_frames -q -rA` 为 4 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+133=270 一致，`.venv/bin/python -m py_compile app/state.py app/routes/system.py voice_server.py tests/test_voice_server.py` 与 `git diff --check` 均通过。
- 2026-08-01 20:38 CST 继续收口 `/api/notifications` 缓冲：旧实现使用裸 list 和调用方各自执行 `list(_notifications); _notifications.clear()`，提醒线程若在两步之间追加通知，可能被随后的 `clear()` 清掉。本轮把缓冲区改为 `collections.deque(maxlen=MAX_NOTIFICATIONS)`，新增 `_append_notification()`、`_drain_notifications()` 和 `_notifications_lock`，写入和“复制并清空”都在同一把锁内完成；通知超过 20 条时由 deque 自动淘汰最旧项。回归测试包含容量裁剪，以及使用 `ThreadPoolExecutor` 与事件钩子确定 append 在 drain 持锁期间阻塞，验证这次 append 不会被清空并会留给下一次轮询；另用一次性脚本模拟旧的无锁 `list()` 后 `clear()` 语义，确认旧逻辑会丢掉“同时到达”通知。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders -q -rA` 为 20 项通过；`TestStreamingResilience` 为 10 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+135=272 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 20:47 CST 继续优化静态 HTML 读取路径：先新增 `test_html_routes_reuse_cached_file_contents` 和 `test_cached_text_file_rereads_when_file_token_changes`，确认旧实现中 `/` 和 `/test` 每次 GET 都直接 `open(...).read()`，且缺少可复用的 `_read_cached_text()` 缓存入口；RED 明确失败在 `voice_server` 没有 `_open_text_file`/缓存读取路径。实现后新增 `_read_cached_text()`，以 `mtime_ns + size + ino` 作为缓存 token，未变化时直接返回内存中的 HTML 文本；读取后会再 stat 一次，若文件在读取过程中变化则重试并避免缓存不一致内容。`/` 和 `/test` 都改为走该 helper，开发时修改 HTML 后会因元数据变化重新读盘。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestHealthAndStatus -q -rA` 为 36 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+138=275 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 20:52 CST 继续给静态 HTML 条件请求降载：先将 `test_home_supports_head` 扩展为参数化的 `test_html_routes_support_etag_304_without_reading_file`，确认旧 `/` 和 `/test` 响应没有 `ETag`，浏览器重复访问无法走 304。实现后新增 `_html_response()`，`/` 与 `/test` 统一支持 GET/HEAD、弱 ETag、`Cache-Control: no-cache`、`Vary: Accept-Encoding`、`Content-Length` 和 `If-None-Match`；命中 304 时先通过文件元数据校验并直接返回空 body，不打开 HTML、不重复传页面内容。底层文件仍沿用 `_read_cached_text()` 的读取前后 stat 校验，避免把过期内容对应的 ETag 返回给客户端。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestHealthAndStatus -q -rA` 为 38 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+140=277 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 20:57 CST 继续优化 PWA manifest：新增 `test_manifest_reuses_cached_json_and_supports_head_304`，先确认旧 `/manifest.json` 只有 GET、响应缺少 `Cache-Control`/ETag，手机安装页重复访问时会反复构造 dict 和 JSON body。实现后抽出 `_build_manifest_payload()`，新增 `_manifest_response()` 和 `_MANIFEST_BODY` 进程内缓存；首次访问生成紧凑 JSON body 与弱 ETag，后续 GET/HEAD 直接复用同一份字节和头，`If-None-Match` 命中时返回 304 且不重新构造 payload。manifest 字段值和 JSON 内容保持不变。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestHealthAndStatus::test_manifest_reuses_cached_json_and_supports_head_304 tests/test_voice_server.py::TestHealthAndStatus -q -rA` 为 39 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+141=278 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py tests/test_config.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 20:59 CST 继续优化移动图标条件请求：新增参数化 `test_mobile_icons_support_etag_304` 覆盖 `/favicon.ico`、`/apple-touch-icon.png`、`/apple-touch-icon-precomposed.png`，先确认旧响应缺少 ETag，浏览器缓存验证时仍重复返回相同 PNG。实现时首次 GREEN 用通用 `_not_modified_response()` 暴露出一个语义问题：304 会带 `Cache-Control: no-cache`，覆盖图标原本的 `public, max-age=86400`；根因是动态内容 helper 不适用于固定静态图标，因此改为图标专用 304 头，保留 24 小时缓存、PNG content-type、HEAD 和 `Content-Length`。固定 PNG 的弱 ETag 在导入时计算一次，304 不返回 body。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestHealthAndStatus -q -rA` 为 42 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+144=281 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py tests/test_config.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 2026-08-01 21:13 CST 继续优化 `/api/search` 历史读取：新增 `test_search_uses_memory_history_without_reading_file_when_caught_up`、`test_search_reads_longer_external_history_once_until_file_changes` 和 `test_search_rereads_external_history_after_file_changes`，先确认旧搜索路由每次请求都会 `open(HISTORY_FILE)` 并 `json.load()`，即使内存历史已是保存后的最新状态；外部文件更长时虽然能兼容读取，但未变化文件也会连续重复解析。RED 后在 `voice_agent.py` 新增带 `_history_lock` 的 `_searchable_history()`、文件签名探测和按 `(dev, ino, size, mtime_ns, ctime_ns)` 失效的文件快照缓存；内存历史已追平或更长时直接复制内存列表，文件确实更长时才读取并复用同一份未变化快照，文件 mtime/size 等变化后会重新读取。`_save_history()` 成功后同步更新快照缓存并只保存 list 类型会话，`_load_history()` 复用同一解析路径并继续兼容旧 list 格式和默认会话。`/api/search` 不再直接打开历史文件，相关性评分、高亮、分页、多会话和外部更长历史兼容语义不变。定向 `.venv/bin/python -m pytest tests/test_voice_server.py::TestSearch -q -rA` 为 8 项通过；历史相关回归 `.venv/bin/python -m pytest tests/test_voice_agent.py::TestHistory tests/test_voice_agent.py::TestMultiUserSessions tests/test_voice_server.py::TestSearch tests/test_voice_server.py::TestConversation -q -rA` 为 16 项通过；新鲜全量 `.venv/bin/python -m pytest -q -rA` 退出码 0，`-rA` 列出的 PASSED 项与 `.venv/bin/python -m pytest --collect-only -q` 的 2+11+29+21+74+147=284 一致，`.venv/bin/python -m py_compile voice_agent.py voice_server.py tests/test_voice_server.py` 与 `git diff --check` 均通过。pytest 输出中的 429、超时、OOM guard、malformed reminders、播放失败、空 ASR 和限流日志来自对应韧性测试，属于预期覆盖。
- 请求开始日志从 INFO 降为 DEBUG；完成日志仍包含 request id、method、path、status 和耗时。常规 `304 Not Modified` 完成日志也降为 DEBUG，非 304 响应和错误仍为 INFO。指标仍统计每一次 304；后续 INFO 日志扫描不能再依赖 INFO 级 `→ 304` 行，应读取 `/api/metrics` 或开启 DEBUG 日志。
- 提醒读取 `_load_reminders()` 改为共享文件锁；写入仍使用排他锁，允许并发读取并继续阻止写读竞争。
- `Metrics.response_times` 使用 `deque(maxlen=100)`，请求完成路径保持 O(1) 追加；只有新样本到达后的首次 `/api/status`、`/dashboard` 或 `/api/metrics` 汇总才惰性计算平均值和 P95，重复读取复用缓存，平均值和 P95 口径保持不变。
- `/api/status` 和 `/dashboard` 的局域网诊断增加 2 秒 TTL 缓存，并把 HTTP/HTTPS 端口和 `AUTH_TOKEN` 开关纳入缓存键；连续状态刷新不再重复枚举网卡，端口或认证配置变化会立即失效缓存。
- `/api/status` 和 `/dashboard` 都复用一次 `brain_status()` 快照：状态接口的 `brain_ready`、`brain_health`、`brain_status` 来自同一 dict，dashboard 的预热状态和大脑累计失败也不再重复读取大脑状态。
- 2026-08-01 17:25 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试为 231 个用例全部通过；`pytest --collect-only -q` 统计为 2+10+28+21+67+103。
- 2026-08-01 17:25 CST `.venv/bin/python -m py_compile voice_agent.py voice_server.py app/state.py tests/test_voice_agent.py tests/test_voice_server.py` 通过；`git diff --check` 通过。
- 2026-08-01 17:08 CST 使用项目虚拟环境 `.venv/bin/python -m pytest -q -rA` 新鲜全量测试为 229 个用例全部通过；`pytest --collect-only -q` 统计为 2+10+28+21+66+102。
- 2026-08-01 17:08 CST `git diff --check` 通过；`.venv/bin/python -m py_compile tests/test_voice_server.py tests/test_voice_agent.py app/state.py voice_server.py` 通过。
- 旧摘要提到“先带回复打断、随后无回复打断，旧待关联片段残留”已在当前工作区验证：`InterruptTelemetry.record()` 在空回复打断时会 `pop(ws_id)`，对应 `test_ws_second_interrupt_without_reply_clears_pending_context` 回归用例通过，后续文字不会误关联到旧片段。
- 2026-08-01 16:56 CST 新鲜全量测试为 229 个用例全部通过；`pytest --collect-only -q` 统计为 2+10+28+21+66+102。
- 2026-08-01 16:48 CST 新鲜全量测试为 226 个用例全部通过；`pytest --collect-only -q` 统计为 2+10+28+21+66+99。
- 2026-08-01 16:34 CST 新鲜全量测试为 217 个用例全部通过；`pytest --collect-only -q` 统计为 2+10+28+21+66+90。
- 全量验证优先使用项目虚拟环境：`.venv/bin/python -m pytest ...`。全局 `/opt/homebrew/bin/pytest` 在当前机器上绑定 Python 3.12，但部分 pytest 插件装在其他 Python site-packages，会出现 `async def functions are not natively supported` 或 `Unknown config option: asyncio_mode` 这类环境误报。
- 异步测试统一使用当前项目已安装的 `anyio` 插件：`@pytest.mark.anyio`，不再依赖 `pytest-asyncio` 的 `asyncio_mode = auto`。
- `TestOpenAICompat.test_build_brain_installs_compat_wrapper` 改为向 `sys.modules` 注入假的 `qwen_agent.agents.Assistant`，用于验证兼容 wrapper；测试不再要求宿主环境真实安装 qwen-agent，避免可选依赖缺失导致全量测试失败。
- 2026-08-01 14:34 复跑时发现 `TestOpenAICompat.test_build_brain_installs_compat_wrapper` 会读取宿主真实内存并在内存压力超过 90% 时失败；已将该测试改为固定虚拟内存，避免宿主瞬时内存占用影响测试结论。
- `git diff --check` 通过。
- 2026-08-01 16:56 CST 额外通过 `py_compile`：`voice_server.py`、`tests/test_voice_server.py`。此前 16:48 CST 已通过 `app/state.py`、`app/routes/system.py`、`voice_server.py`、`tests/test_voice_server.py`。
- 旧摘要提到的 `/api/tts` 缺少 `TTSUnavailableError` 局部导入问题已在当前工作区确认不存在：`voice_server.py` 的 `/api/tts` 已同时导入 `tts_to_mp3, TTSUnavailableError`，对应 503 回归测试通过。
- 旧记录中的“114 个测试中 10 个失败”已过时，不能再作为当前结论。
- 旧记录中的“pytest 会污染真实运行数据”已过时。当前 `tests/conftest.py` 会设置独立的 `ASSISTANT_KID_DATA_DIR` 和 `ASSISTANT_KID_LOG_DIR` 临时目录，`tests/test_runtime_resilience.py` 覆盖了历史、提醒、偏好、日志、MCP 子进程和日志目录子进程路径隔离。

## 本轮已完成优化

1. 高频轮询降载
   - `/api/reminders`、`/api/preferences`、`/api/tunnel` 支持弱 ETag 和 `304 Not Modified`。
   - 命中有效 `If-None-Match` 时，`/api/reminders` 不加载提醒文件、`/api/preferences` 不复制偏好字典、`/api/tunnel` 不读取隧道文件；若底层数据在读取前后发生变化，会回退为内容或快照对应的 ETag，避免把过期 304 返回给客户端。
   - 前端轮询携带 `If-None-Match`；304 时不重绘。
   - 页面隐藏时暂停轮询，恢复可见时立即补刷并重启调度。
   - 轮询周期从较频繁的固定间隔调整为提醒 60 秒、偏好/隧道 120 秒，并在失败时指数退避。

2. 上游大脑限流的前端体验
   - SSE `/api/chat/stream`、`/api/voice/stream` 和 WebSocket 流式大脑共用的错误出口会识别 `429`、`Too many requests`、`rate limit`、`限流`。
   - 命中限流时向前端发送稳定中文文案：“大脑服务繁忙，请稍后再试。”，并保证事件流以 `done` 结束。
   - 原始上游错误继续写入服务端日志，便于排查；不再直接把原始 `Too many requests` 抛给用户。

3. TTS 限流韧性和提醒幂等
   - `voice_agent.tts_to_mp3()` 对短句 MP3 做短期缓存，失败结果不入缓存；缓存按音色和模型隔离。
   - TTS 最终失败后进入短冷却，避免在限流窗口继续打 Finna TTS。
   - 重试逻辑尊重 `Retry-After`，并对永久 4xx 停止重试。
   - `/api/tts`、非流式语音、SSE 流式语音和 WebSocket 语音在 TTS 不可用时保留文字结果或返回稳定中文提示。
   - 提醒加载、保存和到期申领使用 `reminders.json.lock` 文件锁串行化；同一条到期提醒在多进程/多线程下只会被申领一次。
   - 空 ASR 会短路大脑和 TTS，直接返回“抱歉，我没听清，请再说一遍。”，避免噪声请求消耗上游。
   - `enable_thinking` 等 SDK 不兼容参数会自动移入 `extra_body`，避免模型 SDK/API 参数差异导致预热失败。

4. 安全和运行态隔离
   - 默认不信任 `X-Forwarded-For`/`X-Real-IP`；只有显式设置 `ASSISTANT_KID_TRUST_PROXY_HEADERS=1` 且服务位于可信反向代理后才读取代理头，避免外部伪造本地 IP 绕过 `AUTH_TOKEN`。
   - `suggestions_state.json` 跟随 `reminders.json` 所在目录，因此会遵守 `ASSISTANT_KID_DATA_DIR`，测试环境和真实运行数据不会互相污染。
   - 提醒播报临时 MP3 在 `finally` 中删除；提醒被申领后先进入 `delivery_state="delivering"`，只有 TTS 和 `afplay` 都成功才标记 `delivered`。
   - 提醒播报失败按 60 秒、180 秒、600 秒退避重试，超过次数标记 `failed`；调度器不再用进程内去重挡住已释放为 `retry` 的提醒。
   - 提醒调度器新增机器级 `flock` 单实例锁，锁文件为 `reminders.json.scheduler.lock`，已通过跨进程测试验证同一时刻只有一个进程扫描和申领提醒。
   - 调度锁状态读取只做非阻塞探测：缺失锁文件时不创建运行态文件，锁释放后不报告旧 pid，也不会释放当前进程已持有的锁。

5. 异常可观测性和本地分类降级
   - `voice_agent._retry()` 对空字符串异常会记录异常类型名，超时和连接失败也保留原始异常信息，避免日志出现 `HTTP第1次异常: ，重试...` 这类无法排查的空错误。
   - 本地 Ollama 意图分类连续失败两次后会短熔断 30 秒，冷却期内直接降级为 `none`；分类成功后自动重置失败状态。这样在 `localhost:11434` 超时时，不会每个大脑请求都额外等待 5 秒。
   - `/api/status` 新增只读运行态字段：`tts.active/remaining_seconds/cooldown_seconds`、`intent_classifier.circuit_open/remaining_seconds/consecutive_failures/failure_threshold/cooldown_seconds`、`scheduler.locked/held_by_this_process/owner_pid/lock_file`。
   - `/dashboard` 新增“运行健康”卡片，展示 TTS 冷却、本地意图分类熔断和提醒调度锁占用状态；页面仍保持 10 秒自动刷新，锁文件路径做了 HTML 转义和窄屏换行。
   - `.env.example` 已补充 `ESP32_IP`，用于 ESP32 WiFi 定位地址。

6. 状态页、局域网和指标口径收口
   - 新增 `app/config.py`，统一由 `http_port()`/`https_port()` 读取 `ASSISTANT_KID_HTTP_PORT` 和 `ASSISTANT_KID_HTTPS_PORT`；非法值或越界端口回退默认值。
   - `voice_server.py`、`https_server.py`、CORS localhost 来源和 `/api/status`、`/dashboard` 的局域网诊断统一使用同一端口配置，避免展示地址与实际监听端口不一致。
   - 修复 `voice_agent.tts_status()` 引用不存在的模块级变量 `_tts` 导致 `/api/status` 和 `/dashboard` 返回 500 的问题；TTS 状态只返回真实存在的冷却、剩余时间、失败计数和阈值。
   - `/api/metrics` 使用弱 ETag 和 `304 Not Modified`，并用 `exclude_endpoint="/api/metrics"` 排除指标接口自身轮询。
   - `/api/status` 和 `/dashboard` 内嵌指标也改为 `_metrics.summary(exclude_endpoint="/api/metrics")`，避免高频轮询 `/api/metrics` 污染状态页里的请求统计。
   - `/api/status` 新增 `reminder_delivery` 只读统计，汇总 `delivering`、`retry`、`failed` 和活跃投递数；dashboard 的运行健康卡片展示“重试中/失败”，便于观察提醒是否正在退避重试。
   - 新增内存态前端轮询遥测：页面隐藏时记录每个轮询任务的 `paused`，恢复可见时记录 `resumed`，轮询失败时同步记录 `errors` 和 `backoff`。该数据通过 `/api/status` 的 `polling` 字段只读展示，并在 dashboard 运行健康卡片显示暂停、恢复、退避、失败计数。
   - 新增 `POST /api/polling-telemetry`，使用 Pydantic `Literal` 校验事件名和任务名；除 `resumed` 外必须带 `reminders`、`preferences` 或 `tunnel` 任务名。该接口不写磁盘、不新增运行时文件，沿用现有认证和限流中间件。
   - 新增 `tests/test_config.py` 覆盖端口环境变量和非法值回退；新增状态页和 dashboard 的指标口径回归测试。
   - 局域网诊断实测：`hostname=anonymous`、`interface=en1`、`lan_ip=192.168.1.4`、`http_url=http://192.168.1.4:8000`、`https_url=https://192.168.1.4:8443`、`auth_required=False`。该探针使用临时 `ASSISTANT_KID_LOG_DIR`，未写入真实日志。

7. 语音打断上下文标记
   - WebSocket 收到 `interrupt` 时新增内存态打断遥测：记录打断总数、带被打断回复的次数、最近被打断回复、连接 id 和时间戳；回复在服务端统一截断为 200 字，避免前端截断绕过和状态页承载过长文本。
   - 当同一 WebSocket 在打断后继续发送文字消息或语音 ASR 结果时，记录最近一次后续意图，保留“被打断回复 → 文字/语音后续意图”的上下文片段；后续意图同样服务端截断为 200 字。连接断开或后续收到不带回复的打断时丢弃未完成的待关联片段，避免跨连接或跨轮次串扰。
   - 前端手动打断按钮和语音 barge-in 共用同一 payload 构造逻辑；如果当前正在播报或显示回复，会随 `interrupt` 发送服务端需要的 `interrupted_reply`，避免只有自动 barge-in 携带片段、手动按钮不携带的行为不一致。
   - WebSocket 后续文字和语音 ASR 会把消费到的被打断回复透传给 `brain_stream_sentences()`，并以临时 system 上下文插入到历史消息之后、当前用户问题之前。该说明只参与本轮模型调用，不写入持久对话历史。
   - 大脑响应缓存按上下文隔离：普通查询使用 `text\x00...`，带打断片段的查询使用 `interrupted\x00<被打断回复>\x00<追问>`，避免同一句追问在不同被打断上下文下错误复用旧回复。
   - `/api/status` 新增 `interrupts` 只读字段，dashboard “运行健康”卡片展示打断总数、带回复数，以及最近一次被打断回复和后续意图。
   - 新增 10 个 WebSocket/状态页/首页回归用例，覆盖带回复打断、无回复打断、长回复截断、后续文字意图、后续语音意图、无前置打断不误关联、空回复二次打断清理待关联片段、后续意图截断、dashboard 展示和首页手动打断 payload。
   - `test_interrupted_reply_is_added_to_prompt_but_not_history` 覆盖临时上下文不写入历史；`test_ws_interrupt_records_following_text_intent` 与 `test_ws_interrupt_records_following_voice_intent` 同时断言文字/语音路径都会把 `interrupted_reply` 透传给流式大脑。

8. 对话导出筛选
   - `/api/export` 新增 `from_date` 和 `to_date` 查询参数，按消息时间戳做日期范围过滤；非法日期或开始日期晚于结束日期返回 400。
   - 前端语音页新增“对话导出”操作区，提供开始日期、结束日期、TXT/Markdown/JSON 格式选择和下载按钮，默认导出当前共享会话，日期留空表示不限制。
   - 新增 3 个导出回归测试：日期范围排除范围外消息、非法日期返回 400、首页包含导出筛选控件。

9. 状态页非阻塞 CPU 采样
   - 模块导入时先调用一次 `psutil.cpu_percent(interval=None)` 建立基线，后续 `/api/status` 和 `/dashboard` 都读取自上次采样以来的非阻塞 CPU 值。
   - 历史日志中的状态类请求曾出现约 500ms 到 945ms 的固定阻塞；修复后隔离 `TestClient` 探针显示 `/api/status` 约 4.5ms/1.1ms/1.0ms，`/dashboard` 约 1.6ms/1.1ms/0.9ms。
   - 该探针使用独立 `ASSISTANT_KID_DATA_DIR`、`ASSISTANT_KID_LOG_DIR` 和 `SKIP_BACKGROUND=1`，不写入真实运行数据。

10. 高频状态和日志路径继续降载
    - 请求开始日志和常规 304 完成日志从 INFO 降为 DEBUG，保留非 304 完成、错误和限流告警在 INFO/WARNING，减少真实 `app.log` 在高频轮询下的噪声。
    - 提醒读取使用共享锁；写操作仍保持排他锁，降低后台提醒扫描、API 列表和 dashboard 同时读取时的互相阻塞。
    - `Metrics` 的最近 100 条响应耗时改为 O(1) 追加；状态页、dashboard 和指标页首次汇总后缓存平均值/P95，直到下一个延迟样本到达才重新计算。
    - 局域网地址枚举增加 2 秒 TTL 缓存，缓存键包含 HTTP 端口、HTTPS 端口和认证开关；缓存返回副本，避免调用方意外修改共享状态。
    - `/api/status` 和 `/dashboard` 复用同一次大脑状态快照，避免高频刷新重复读取 `_brains` 和重复生成时间戳字段。
    - `/api/status` 和 `/dashboard` 的提醒待办数与投递状态改为一次遍历汇总；状态接口只计数，dashboard 才保留前 5 条待办用于渲染。hostname 和 OS 字符串在进程内缓存一次，避免每次 10 秒刷新都重复调用系统 API。
    - 新增 `TestMetrics` 覆盖 100 条样本的平均值/P95 口径和惰性缓存；新增局域网缓存 TTL 测试和状态/dashboard 单次大脑快照回归测试，并在状态测试间自动清理缓存避免顺序污染。

## 真实日志统计

统计窗口：`2026-08-01 00:00:00` 到 `2026-08-01 15:00:00` 左右，以日志尾部最新时间为准。

过滤关键词：

- `testclient`
- `mock build failed`
- `/tmp/nonexistent_test_file.json`
- `test第`
- `密钥故障转移`
- `pytest`
- 测试生成的 `Connection error.`

统计结果：

- 当前原始日志约 15855 行。
- 有完成日志的 HTTP 请求：6431 条。
- 状态码：`200` 6196 条，`304` 129 条，`404` 97 条，`405` 3 条，`422` 6 条。
- 14:23 之后主要是轮询接口的 `304 Not Modified`，说明前端 ETag、页面隐藏暂停和降频轮询持续生效。
- `[ERROR]`/`[WARNING]` 合计 325 行，主要集中在早间 TTS/大脑 429、本地意图分类超时和两次空异常文案。
- 429/限流相关仍主要来自 Finna TTS 和 Finna 聊天上游；当前已做中文降级、TTS 冷却和缓存，但上游账户/接口配额本身无法在本地消除。
- 空 ASR：9 次
- TTS 最终失败：多次集中在 Finna TTS 429；新增冷却和缓存用于降低重复打击。

高频接口：

| 接口 | 次数 | 平均耗时 | 最大耗时 |
| --- | ---: | ---: | ---: |
| `GET /api/reminders` | 2214 | - | - |
| `GET /api/preferences` | 1167 | - | - |
| `GET /api/tunnel` | 1159 | - | - |
| `GET /api/status` | 1072 | - | - |
| `GET /api/events` | 141 | - | - |
| `POST /api/voice/stream` | 131 | - | - |
| `GET /` | 113 | - | - |
| `GET /api/conversation` | 76 | - | - |
| `GET /manifest.json` | 73 | - | - |
| `GET /ws` | 66 | - | - |

注意：`POST /api/chat/stream` 的完成日志只表示 SSE 响应已建立，不代表大脑完整回复耗时；真实回复耗时主要发生在流式生成阶段。

### 2026-08-01 15:00 后观察

统计窗口：`2026-08-01 15:00:00` 到 `2026-08-01 16:13:32` 左右，日志文件约 16539 行。

统计口径：

- 只统计包含 `→ <状态码>` 的 HTTP 完成日志行，避免把请求开始行和 `[intent] '...' → baize-skills` 这类路由日志算成接口请求。
- 继续过滤 `testclient`、`test第`、`密钥故障转移`、`/tmp/nonexistent_test_file`、`tests/test_`、`pytest` 和测试生成的 `Connection error.`。

统计结果：

- HTTP 完成请求：299 条。
- 状态码：`200` 7 条，`304` 292 条。
- 高频请求：`GET /api/reminders` 144 条，`GET /api/tunnel` 74 条，`GET /api/preferences` 74 条，`GET /health` 2 条，`GET /dashboard` 2 条，`GET /api/export` 2 条，`POST /api/chat` 1 条。
- 15:00 后过滤测试噪声后，没有新的 `[ERROR]`/`[WARNING]`、TTS 429、TTS 最终失败、空 ASR 或本地意图分类超时记录。
- 绝大多数完成请求返回 `304`，说明 reminders/preferences/tunnel 的 ETag 轮询降载持续生效；15:05 的 `[intent] '今天有什么科技新闻' → baize-skills` 是意图路由日志，不是接口完成日志，也不是异常。

历史异常和体验问题（早间观察，15:00 后未复现）：

- 早间出现 `No module named 'qwen_agent'`，随后重启环境恢复；当前测试导入正常。
- 出现过一次 `Completions.create() got an unexpected keyword argument 'enable_thinking'`；已通过兼容 wrapper 自动迁移不兼容参数。
- 大脑和意图分类多次遇到上游 429；已统一用户可见中文文案、重试和熔断逻辑。
- TTS 在 11:04 两次最终失败；已增加缓存、冷却、`Retry-After`、文字降级，并继续观察真实配额。
- 08:00 的早间提醒曾疑似重复播报；已通过提醒申领锁、调度器单实例锁和 delivered/delivering/retry/failed 状态收敛。
- 12:43 和 12:47 出现过 `HTTP第1次异常: ，重试...`；已补测试并改为记录异常类型和原始异常信息。
- 12:26 和 14:23 出现 `localhost:11434` 本地意图分类读超时；已新增连续失败后的 30 秒短熔断，避免持续拖慢请求。

## 从历史日志提炼的功能建议

P0：

- 上游 429 的全链路友好降级：已覆盖大脑、SSE/WebSocket、TTS 和提醒语音的稳定中文文案；后续仍需观察真实上游配额恢复情况。
- 提醒触发幂等：已通过文件锁申领、调度器单实例锁和 delivered/delivering/retry/failed 状态收敛多进程重复播报；后续仍需结合真实提醒日志观察。
- 空 ASR 反馈优化：已短路大脑和 TTS，并返回“抱歉，我没听清，请再说一遍。”；后续可继续按真实噪声样本调阈值或前端录音体验。

P1：

- 模型和 SDK 兼容探测：已将 `enable_thinking` 等不兼容参数自动移入 `extra_body`；后续可增加启动期兼容探测和更细的模型能力缓存。
- TTS 限流退避和缓存：已实现短句缓存、失败冷却、`Retry-After` 尊重和文字降级；仍需观察真实 TTS 配额是否恢复。
- 局域网访问诊断：`/api/status` 和 `/dashboard` 已展示本机 IP、HTTP/HTTPS 地址、端口和同网段访问提示。
- 前端流式阶段状态：SSE 和 WebSocket 已区分“聆听中”“正在思考”“正在合成语音”，减少用户重复提交。
- 轮询和运行态指标：`/api/metrics` 已支持 304 命中率，状态页和 dashboard 已展示 TTS、意图分类、调度器健康、提醒重试/失败计数、前端轮询暂停/恢复/退避/失败计数和排除自身后的指标。

P2：

- 语音打断后的上下文标记：已在 WebSocket 流式大脑路径把被打断回复作为临时 system 上下文接入多轮提示词，并保证不写入持久历史、不跨连接串扰；SSE `/api/chat/stream`、`/api/voice/stream` 和非流式 `/api/chat` 当前没有打断事件和待关联片段遥测，因此不涉及该上下文注入。
- 轮询指标面板：304 命中、页面隐藏暂停、恢复和失败退避计数已经进入状态接口和 dashboard；后续可在真实手机浏览器上观察暂停/退避计数是否符合用户使用路径。
- 对话导出按会话/时间筛选的前端入口已补齐；后续可观察手机窄屏下日期选择和下载行为。
- 真实设备定位和跨终端常驻仍需 ESP32/终端客户端、权限申请、状态同步通道和安全边界，不能只靠当前后端模拟完成。

## 当前实现不了或只能模拟的能力

分析用户“能不能做”的需求时，必须区分“可以新增”和“当前实现不了/只是模拟”：

- 意图分类只路由到：`none`、`amap-maps`、`baize-skills`、`filesystem`、`magic-phone`。
- `baize-skills.shopping_search()` 是购物推荐/评测，不是通用互联网搜索。
- `/api/search` 只搜对话历史，不搜互联网。
- `control_tesla_ac()` 只返回模拟成功文案，没有连接真实车控 API。
- `open_lifestyle_app()` 只生成外卖/购物等 deeplink，不完成真实下单或支付。
- 跨终端常驻、Wi-Fi 定位、自动感知用户所在位置目前没有配套设备注册、定位权限、状态同步通道或终端客户端。
- 自修改/自扩展目前没有安全执行边界；可做诊断、生成补丁、人工 review、测试和回滚，不能宣称可自动自我优化。
- 真车控、支付、通用网页搜索、跨设备同步、后台定位都需要新增外部 API、权限申请、设备端协议和安全审计，不能只靠提示词或模拟函数完成。

## 继续工作时的注意事项

- 优先运行全量测试，不要只凭定向用例判断收口。
- 真实日志分析必须重新按时间窗口和过滤关键词统计；历史摘要只能作为线索。
- 当前工作区存在多处未提交改动，继续优化时不要回滚他人改动。
- 若新增运行时文件路径，必须同时支持 `ASSISTANT_KID_DATA_DIR` 或 `ASSISTANT_KID_LOG_DIR`，并补 `tests/test_runtime_resilience.py` 隔离断言。
- 新增用户可见错误时，服务端保留原始错误，客户端只显示稳定、可理解的中文文案。
- 涉及重试或超时时，日志不能只记录 `str(e)`；异常为空时至少记录异常类型，避免后续再次出现空错误。

## 2026-08-01 19:14 CST

- `/api/metrics` 条件请求继续降载：新增 `test_metrics_304_skips_summary_build`，先确认 RED：重复携带 `If-None-Match` 时旧代码仍在 `voice_server.metrics()` 中立即调用 `_metrics.summary(...)`，测试抛错路径明确指向 `voice_server.py:1592`。
- `Metrics` 增加 `revision`、`include_in_metrics` 和 `token(exclude_endpoint=...)`。`token()` 的计数口径与 `summary(exclude_endpoint="/api/metrics")` 对齐，避免 metrics 自身轮询改变 ETag；请求中间件继续记录 `/api/metrics` 的全局 total/304 计数用于观测，但不把它纳入会改变 payload/token 的指标修订。
- `_json_response()` 支持传入 callable payload；当调用方提供 `etag_token` 且请求命中 `If-None-Match` 时，直接返回 304，不再构造 summary、不序列化 JSON。未提供 token 的旧调用方仍按 body 派生弱 ETag，行为保持兼容。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_server.py::TestMetrics -q -rA` 退出码 0，9 个 metrics 相关用例全部通过。
- 全量验证：`.venv/bin/python -m pytest -q -rA` 退出码 0；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 11`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 69`、`tests/test_voice_server.py: 122`，合计 254；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 退出码 0；`git diff --check` 退出码 0。

## 2026-08-01 19:21 CST

- TTS Markdown 清理热路径预编译：新增 `TestCleanForTTS::test_clean_for_tts_reuses_compiled_patterns`。RED 已确认：当前 `voice_agent` 没有 `_TTS_*_RE` 模块级编译正则，定向用例退出码 1，失败在 `assert len(compiled_patterns) >= 9`；继续执行还会拦截 `re.sub()`，证明旧实现每个流式片段重复编译 9 个模式。
- 将 `_clean_for_tts()` 中粗体、标题、引用、表格管道、代码块、行内代码、列表、Markdown 链接和空白压缩 9 个正则提升为模块级 `Pattern`，替换顺序、匹配表达式和替换内容保持不变。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestCleanForTTS -q -rA` 退出码 0，6 个清理相关用例全部通过。
- 全量验证：`.venv/bin/python -m pytest -q -rA` 退出码 0；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 11`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 70`、`tests/test_voice_server.py: 122`，合计 255；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 退出码 0；`git diff --check` 退出码 0。

## 2026-08-01 19:25 CST

- 对话上下文 token 估算预编译：新增 `TestContextManagement::test_estimate_tokens_reuses_compiled_patterns_once`。RED 已确认：旧实现没有 `_TOKEN_CHINESE_RE`/`_TOKEN_ENGLISH_RE`，定向用例退出码 1，失败在 `isinstance(None, re.Pattern)`；旧实现还会对英文单词执行两次 `re.findall()`，一次计词数、一次计字母数。
- 将中文字符和英文单词匹配提升为模块级 `Pattern`，`_estimate_tokens()` 复用同一次英文匹配结果计算单词数和字母数，中文 1 字约 1.5 token、英文 1 词约 1.3 token、其他字符约 0.5 token 的粗略口径保持不变。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestContextManagement -q -rA` 退出码 0，8 个上下文管理用例全部通过。
- 全量验证：`.venv/bin/python -m pytest -q -rA` 退出码 0；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 11`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 71`、`tests/test_voice_server.py: 122`，合计 256；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/routes/system.py app/state.py app/reminders.py app/auth.py tests/test_runtime_resilience.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_security_fixes.py` 退出码 0；`git diff --check` 退出码 0。

## 2026-08-01 21:22 CST

- 历史读取并发收口：新增 `TestHistory::test_history_snapshot_is_stable_after_append`、`TestHistory::test_session_summaries_are_copied_under_lock`、`TestConversation::test_conversation_uses_locked_history_snapshot`、`TestExport::test_export_uses_locked_history_snapshot`、`TestContextEndpoint::test_context_uses_locked_history_snapshot`、`TestSessions::test_list_sessions_uses_locked_summaries`。RED 已确认：旧代码缺少 `_history_snapshot()` 和 `_session_summaries()`，失败均为 `AttributeError`，能证明 API 线程此前直接遍历或序列化活动历史列表引用。
- `voice_agent` 新增 `_history_snapshot()`：在 `_history_lock` 内确保会话存在、执行最大会话数淘汰，并返回稳定 list 副本；新增 `_session_summaries()`：同一把锁内构造 `/api/sessions` 所需的 `session_id/message_count/last_message` 摘要，避免外部迭代活动 `_sessions`。
- `/api/conversation`、`/api/export`、`/api/context` 改用 `_history_snapshot()`；`/api/sessions` 改用 `_session_summaries()`。导出路径删除 `_history`/`_get_history` 分支差异，统一按请求会话读取快照。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestHistory tests/test_voice_server.py::TestConversation tests/test_voice_server.py::TestExport tests/test_voice_server.py::TestContextEndpoint tests/test_voice_server.py::TestSessions -q -rA` 退出码 0，20 个相关用例全部通过。
- 新鲜验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py tests/test_voice_agent.py tests/test_voice_server.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，290 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 11`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 76`、`tests/test_voice_server.py: 151`，合计 290；`git diff --check` 退出码 0。

## 2026-08-01 21:40 CST

- 历史文件 I/O 移出 `_history_lock`：新增 `TestHistory::test_save_history_does_not_hold_history_lock_during_file_write`、`TestHistory::test_stale_history_save_does_not_replace_newer_snapshot`、`TestHistory::test_searchable_history_reads_file_without_holding_history_lock`。三个 RED 已确认：保存打开 `HISTORY_FILE` 时 `_history_lock.locked()` 仍为 `True`；旧保存序号未检查会执行第二次 `os.replace` 覆盖新快照；搜索读取历史文件时也持有 `_history_lock`。第三个用例最初 patch `voice_agent.open` 未拦住内置 `open`，改为 patch `builtins.open` 后失败原因准确。
- `voice_agent` 新增 `tempfile` 导入；`_history_save_lock` 收口为统一文件锁 `_history_file_lock`，保护文件签名/缓存和保存串行化；`_read_history_file_unlocked()` 改为 `_read_history_file_locked()`，并新增锁外入口 `_read_history_file()`。
- `_searchable_history()` 先在 `_history_lock` 内复制内存历史，再在锁外读取历史文件；`_save_history()` 在 `_history_lock` 内只复制快照和递增 `_history_save_seq`，锁外写临时文件、`flush`、`fsync`、`os.replace` 原子落盘；旧保存若发现已有更新保存排队，不再替换目标文件；`_load_history()` 改为锁外读文件，再在内存锁内应用状态，避免锁顺序交叉。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestHistory -q -rA` 退出码 0，7 个历史相关用例全部通过。
- 新鲜验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py tests/test_voice_agent.py tests/test_voice_server.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，293 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 11`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 79`、`tests/test_voice_server.py: 151`，合计 293；`git diff --check` 退出码 0；`rg -n "_read_history_file_unlocked|_history_save_lock" voice_agent.py tests` 无旧符号残留。

## 2026-08-01 21:48 CST

- 偏好保存并发收口：新增 `TestPreferences::test_save_preferences_does_not_hold_prefs_lock_during_file_write` 和 `TestPreferences::test_stale_preferences_save_does_not_replace_newer_snapshot`。RED 已确认：旧代码在 `json.dump()` 期间持有 `_prefs_lock`，另一个线程 0.2 秒无法获取偏好锁；旧代码没有 `_preferences_save_seq`，第二个保存序号停在 0，无法阻止旧快照覆盖新快照。第一个用例曾因 RLock 同线程语义和旧代码吞异常导致误判，改为在后台线程保存、主线程尝试获取锁后准确失败。
- `voice_agent` 新增 `_preferences_file_lock` 和 `_preferences_save_seq`。`_save_preferences()` 现在只在 `_prefs_lock` 内复制偏好快照并递增保存序号；文件写入移到锁外，使用同目录临时文件、`flush`、`fsync`、`os.replace` 原子落盘；文件锁内若发现保存序号已更新，则丢弃旧保存，避免旧快照覆盖新快照。`get_preference()` 同步纳入 `_prefs_lock`，匹配既有偏好修订语义。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestPreferences tests/test_runtime_resilience.py tests/test_voice_server.py::TestConditionalGet::test_preferences_supports_etag_304 tests/test_voice_server.py::TestConditionalGet::test_preferences_304_skips_preferences_read tests/test_voice_server.py::TestConditionalGet::test_preferences_etag_changes_after_mutation tests/test_voice_server.py::TestConditionalGet::test_preferences_etag_changes_after_delete -q -rA` 退出码 0，24 个相关用例全部通过。
- 新鲜验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py tests/test_voice_agent.py tests/test_voice_server.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，295 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 11`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 81`、`tests/test_voice_server.py: 151`，合计 295；`git diff --check` 退出码 0。

## 2026-08-01 21:53 CST

- 系统提示词热路径复用提醒加载器：新增 `TestBuildSystemMsg::test_today_reminders_use_locked_reminder_loader`。RED 已确认：旧代码在 `_build_system_msg()` 中直接 `open(REMINDERS_FILE)`，没有调用 `app.reminders._load_reminders()`，测试桩计数为 `{'load': 0, 'open': 1}`，失败原因准确指向绕过共享提醒锁和畸形记录过滤。
- `_build_system_msg()` 现在从 `app.reminders` 调用 `_load_reminders()` 后统计未完成今日待办；读取异常时保持原有降级，不让系统提示词构建失败。偏好部分同步改为通过 `list_preferences()` 取锁内快照后展示前 10 项，避免无锁遍历 `_preferences`。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestBuildSystemMsg -q -rA` 退出码 0，4 个系统提示词用例全部通过。
- 相关验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py tests/test_voice_agent.py tests/test_voice_server.py` 退出码 0；`.venv/bin/python -m pytest tests/test_voice_agent.py::TestBuildSystemMsg tests/test_voice_agent.py::TestPreferences tests/test_runtime_resilience.py tests/test_voice_server.py::TestHealthAndStatus -q -rA` 退出码 0，66 个相关用例全部通过；日志中的 429 重试和 malformed reminders 属于韧性用例预期输出。

## 2026-08-01 22:00 CST

- 提醒文件原子写入：新增 `tests/test_runtime_resilience.py::test_reminder_writes_are_atomic` 和 `test_reminder_write_failure_keeps_existing_file_intact`。RED 已确认：`_save_reminders()`、`claim_due_reminders()`、`complete_reminder_delivery()`、`release_failed_reminder()` 旧代码都直接以 `"w"` 打开 `REMINDERS_FILE`；第二个用例证明 `json.dump()` 失败前目标文件已被截断为空串。
- `app.reminders` 新增 `_write_locked_reminders()`：在调用方已持有提醒文件锁时，写同目录隐藏临时文件、`flush`、`fsync`，再 `os.replace()` 原子替换；异常路径清理临时文件并向上抛出，避免写一半破坏原 JSON。上述四个写路径统一改用该辅助函数。
- 定向验证：`.venv/bin/python -m pytest tests/test_runtime_resilience.py::test_reminder_writes_are_atomic tests/test_runtime_resilience.py::test_reminder_write_failure_keeps_existing_file_intact -q -rA` 退出码 0，2 个新增用例全部通过；`.venv/bin/python -m py_compile app/reminders.py tests/test_runtime_resilience.py` 退出码 0。
- 相关验证：`.venv/bin/python -m pytest tests/test_runtime_resilience.py tests/test_voice_server.py::TestHealthAndStatus tests/test_voice_server.py::TestReminders tests/test_voice_server.py::TestConditionalGet -q -rA` 退出码 0，84 个相关用例全部通过；日志中的 429、播放失败、重试和 malformed reminders 属于韧性用例预期输出。
- 新鲜验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/reminders.py tests/test_voice_agent.py tests/test_voice_server.py tests/test_runtime_resilience.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，298 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 13`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 82`、`tests/test_voice_server.py: 151`，合计 298；`git diff --check` 退出码 0。

## 2026-08-01 22:09 CST

- MCP 提醒持久化并发收口：上轮已新增并确认 RED 的 `test_baize_mcp_add_reminder_uses_shared_reminder_lock`、`test_baize_mcp_reminder_write_is_atomic`、`test_baize_mcp_add_reminder_holds_exclusive_lock_for_read_modify_write` 分别覆盖共用 `reminders.json.lock`、临时文件加 `os.replace()` 原子替换、以及添加提醒读改写必须处于同一个排他事务。
- `baize_skills_mcp.py` 新增 `STORE_LOCK_FILE`、`_locked_store(shared=False)`、`_read_store()`、`_write_store_locked()`；`_load()` 使用共享锁读取，`_save()` 使用排他锁原子写入，`add_reminder()` 在同一个排他锁内完成读取、追加和落盘，`list_reminders()` 继续走共享锁读取。
- 偏好文件 `_save_prefs()` 仍直接写 `preferences.json`；主 `voice_agent` 进程又有长期内存偏好缓存，所以这里不能只靠文件锁宣称跨进程偏好一致。后续若处理，需要同时设计锁内读改写和缓存失效或重载策略。
- 定向验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/reminders.py baize_skills_mcp.py tests/test_voice_agent.py tests/test_voice_server.py tests/test_runtime_resilience.py` 退出码 0；`.venv/bin/python -m pytest tests/test_runtime_resilience.py tests/test_voice_server.py::TestHealthAndStatus tests/test_voice_server.py::TestReminders -q -rA` 退出码 0，78 个相关用例全部通过。
- 新鲜验证：`.venv/bin/python -m pytest -q -rA` 退出码 0，301 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 16`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 82`、`tests/test_voice_server.py: 151`，合计 301；`git diff --check` 退出码 0。

## 2026-08-01 22:18 CST

- HTTP 提醒新增/完成读改写并发收口：新增 `tests/test_runtime_resilience.py::test_append_reminder_holds_single_exclusive_transaction`、`test_append_reminder_is_cross_process_safe`、`test_complete_reminder_missing_does_not_write`，以及 `tests/test_voice_server.py::TestReminders::test_add_reminder_uses_atomic_reminder_transaction`、`test_delete_reminder_uses_complete_transaction`、`test_delete_missing_reminder_returns_404_without_save`。RED 已确认：旧 HTTP 路由自行执行 `_load_reminders()` 后再 `_save_reminders()`，两次文件锁之间存在并发覆盖窗口；旧代码也缺少 `app.reminders.append_reminder()` 和 `complete_reminder()` 事务入口。
- `app.reminders` 新增 `append_reminder(text, time_str="", due=None)`：在同一个排他锁内读取当前提醒、计算下一个 id、追加记录、清理旧提醒并原子写入，返回新记录深拷贝；新增 `complete_reminder(reminder_id)`：在同一个排他锁内查找并标记 `done=True`/`completed_at`，找不到时返回 `False` 且不写文件。
- `voice_server.py` 的 `POST /api/reminders` 不再自行读改写，只负责输入清洗、时间解析和响应文案；`DELETE /api/reminders/{rid}` 改为调用 `complete_reminder(rid)`，缺失时返回 404。删除路由中已无用的 `_save_reminders`、`_cleanup_old_reminders` 导入。
- 定向验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/reminders.py baize_skills_mcp.py tests/test_voice_agent.py tests/test_voice_server.py tests/test_runtime_resilience.py` 退出码 0；新增提醒相关用例在实现后通过。
- 新鲜验证：`.venv/bin/python -m pytest -q -rA` 退出码 0，307 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 19`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 82`、`tests/test_voice_server.py: 154`，合计 307；`git diff --check` 退出码 0。pytest 输出中的 429、TTS 失败、afplay 超时、malformed reminders、限流、空 ASR、熔断等均为韧性测试预期输出。

## 2026-08-01 22:46 CST

- 偏好跨进程一致性收口：上轮新增并确认 RED 的 `test_baize_mcp_preference_write_is_atomic`、`test_baize_mcp_set_preference_holds_exclusive_lock_for_read_modify_write`、`test_baize_mcp_set_preference_waits_for_shared_preference_lock`、`test_voice_agent_set_preference_waits_for_shared_preference_lock`、`test_preference_writers_from_separate_processes_do_not_lose_keys`、`TestPreferences::test_refresh_preferences_reloads_external_changes_and_bumps_etag`、`TestConditionalGet::test_preferences_api_reloads_external_changes` 分别覆盖 MCP 原子写、同锁读改写、主/MCP 共用锁等待、外部变更刷新和跨进程 15+15 key 不丢失。
- 根因是 MCP `_save_prefs()` 直接以 `"w"` 截断写 `preferences.json`，MCP set 偏好的读取和写入不在同一个排他事务；主 `voice_agent` 又长期持有内存偏好快照，外部 MCP 写入后 `/api/preferences` 可能错误返回 304，旧内存快照也可能覆盖新 key。
- `baize_skills_mcp.py` 新增 `PREFS_LOCK_FILE = PREFS_FILE + ".lock"`、`_locked_prefs(shared=False)`、`_read_locked_prefs()`、`_write_locked_prefs()`；`_load_prefs()` 走共享锁，`set_preference()` 在同一排他锁内读、改、写临时文件、`fsync`、`os.replace()`。
- `voice_agent.py` 新增主进程侧 `PREFS_LOCK_FILE`、偏好文件签名和锁内读写/提交路径。主进程和 MCP 共用 `preferences.json.lock`；读取路径按 `(dev, ino, mtime_ns, size)` 检测外部文件变更，发现后 reload 内存快照、bump revision/etag，并失效系统提示词缓存。写入路径在排他锁内重读磁盘、合并内存偏好并原子替换，避免旧缓存覆盖 MCP 刚写入的 key；`set_preference()` 和 `del_preference()` 改走统一 `_commit_preferences()`。
- 定向验证：`.venv/bin/python -m pytest tests/test_runtime_resilience.py -q -k 'preference or preferences' tests/test_voice_agent.py::TestPreferences -q tests/test_voice_server.py::TestConditionalGet::test_preferences_api_reloads_external_changes -q` 退出码 0；受影响的系统提示词锁加载用例同步通过。
- 新鲜验证：`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/reminders.py baize_skills_mcp.py tests/test_voice_agent.py tests/test_voice_server.py tests/test_runtime_resilience.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，314 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 24`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 83`、`tests/test_voice_server.py: 155`，合计 314；`git diff --check` 退出码 0。pytest 输出中的 429、TTS 失败、afplay 超时、malformed reminders、限流、空 ASR、熔断等均为韧性测试预期输出。

## 2026-08-01 22:58 CST

- 建议状态跨进程一致性收口：上轮新增并确认 RED 的 `tests/test_runtime_resilience.py::test_suggest_state_write_is_atomic`、`test_update_suggest_state_holds_exclusive_lock_for_read_modify_write`、`test_suggest_state_writers_from_separate_processes_do_not_lose_keys` 分别覆盖 `suggestions_state.json` 原子写、锁内读改写和跨进程并发写不丢 key。
- 根因是 `voice_server._save_suggest_state()` 直接以 `"w"` 截断写状态文件，后台天气、雨雪、晨报/晚安、健康告警和偏好建议在多进程下可能重复申领或互相覆盖 key。
- `voice_server.py` 新增 `SUGGEST_STATE_LOCK_FILE = SUGGEST_STATE_FILE + ".lock"` 以及 `_locked_suggest_state()`、`_read_locked_suggest_state()`、`_write_locked_suggest_state()`、`_refresh_suggestions_state()`、`_suggest_state_snapshot()`、`_update_suggest_state()`、`_claim_suggest_state()`。状态写入走同目录临时文件、`flush`、`fsync`、`os.replace()`；后台建议去重标记统一走 `_claim_suggest_state()` 原子申领。
- 定向验证：`.venv/bin/python -m pytest tests/test_runtime_resilience.py -q -k 'suggest_state' -rA` 退出码 0，3 个用例通过；`.venv/bin/python -m py_compile voice_server.py tests/test_runtime_resilience.py` 退出码 0。
- 新鲜验证：`.venv/bin/python -m pytest tests/test_runtime_resilience.py -q -rA` 退出码 0，27 个用例通过；`.venv/bin/python -m pytest -q -rA` 退出码 0，317 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 27`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 21`、`tests/test_voice_agent.py: 83`、`tests/test_voice_server.py: 155`，合计 317；`.venv/bin/python -m py_compile voice_agent.py voice_server.py app/reminders.py baize_skills_mcp.py tests/test_voice_agent.py tests/test_voice_server.py tests/test_runtime_resilience.py` 与 `git diff --check` 均退出码 0。pytest 输出中的 429、播报失败、限流等为韧性测试预期输出。

## 2026-08-01 23:11 CST

- 对话历史截断和跨进程文件锁收口：先补写并确认 RED。`tests/test_utils.py` 覆盖 dict 多会话历史截断、原子 `os.replace()`、写失败保留原文件、畸形/不支持 JSON 不抛异常、等待 `conversation_history.json.lock`；`tests/test_voice_agent.py::TestHistory::test_save_history_waits_for_shared_history_file_lock` 覆盖主历史保存也等待同一历史文件锁。旧实现明确表现为 dict 历史未按会话截断、`truncate_history_file()` 直接 `"w"` 写目标文件、截断和保存互不等待共享锁。
- `utils.py` 新增 `_locked_file()`；`truncate_history_file()` 在排他锁内读改写，同时兼容旧 list 和当前 dict `{session_id: [messages]}` 格式。dict 格式按会话保留最近 `max_entries` 条；非 list session 值原样保留；写入走同目录临时文件、`flush`、`fsync`、`os.replace()`，写失败清理临时文件且不破坏原 JSON。
- `voice_agent.py` 新增 `HISTORY_LOCK_FILE` 和 `_locked_history_file()`；`_read_history_file()` 使用共享锁，`_save_history()` 使用排他锁，与后台 `utils.truncate_history_file()` 共用 `conversation_history.json.lock`。`.gitignore` 同步忽略 `conversation_history.json.lock`、`suggestions_state.json.lock`、`preferences.json.lock`。
- 定向验证：`.venv/bin/python -m pytest tests/test_utils.py tests/test_voice_agent.py::TestHistory tests/test_voice_server.py::TestSearch tests/test_voice_server.py::TestConversation tests/test_voice_server.py::TestExport tests/test_voice_server.py::TestContextEndpoint tests/test_voice_server.py::TestSessions -q -rA` 退出码 0，58 个相关用例通过。
- 新鲜验证：`.venv/bin/python -m py_compile utils.py voice_agent.py voice_server.py tests/test_utils.py tests/test_voice_agent.py tests/test_runtime_resilience.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，323 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 27`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 27`、`tests/test_voice_agent.py: 84`、`tests/test_voice_server.py: 155`，合计 323；`git diff --check` 退出码 0。pytest 输出中的 429、TTS 失败、afplay 超时、malformed reminders、限流、空 ASR、熔断等为韧性测试预期输出。

## 2026-08-01 23:27 CST

- 运行态音频路径继续收口：先新增 `TestVoiceLoop::test_runtime_audio_path_uses_configured_data_dir`、`test_magic_phone_cli_audio_paths_use_configured_data_dir` 和 `test_magic_phone_cli_record_default_output_uses_cli_audio_path`。RED 已确认：`voice_agent` 缺少 `runtime_audio_path()`，`magic_phone_cli` 也没有 `DATA_DIR`/`cli_audio_path()`，CLI 录音默认参数在导入时固定到 `/tmp/cli_mic.wav`，即使后续设置 `ASSISTANT_KID_DATA_DIR` 也不会跟随。
- `voice_agent.py` 新增 `runtime_audio_path(filename)`；命令行 demo 的 `voice_reply.wav` 现在写入 `DATA_DIR`，打印路径也使用实际输出位置。`magic_phone_cli.py` 从 `voice_agent` 复用 `DATA_DIR`，新增 `cli_audio_path(filename)`，`record()` 的默认输出在调用时动态计算，主循环的录音读取和回复播放也统一走该 helper。这样测试隔离目录和真实运行数据目录都不会再把交互式 CLI 音频散落到固定 `/tmp`。
- 同步收口上一轮未完成的 tunnel CORS 刷新：只修改 `app.user_middleware` 不会影响已构建的 `CORSMiddleware` 实例，且默认 `"*"` 加 credentials 时动态追加来源没有响应级证据。新增 `test_dynamic_cors_middleware_reloads_origin_flags`，先用独立 Starlette app 确认旧实例会对新 origin 预检返回 400；实现 `DynamicCORSMiddleware` 后，每次 HTTP 请求从 `_cors_origins` 重新刷新 `allow_origins`、`allow_all_origins`、`preflight_explicit_allow_origin` 和简单/预检响应头，`/api/tunnel` 读到新 tunnel 文件后后续预检会实际使用新来源。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_agent.py::TestVoiceLoop tests/test_runtime_resilience.py::test_magic_phone_cli_audio_paths_use_configured_data_dir tests/test_runtime_resilience.py::test_magic_phone_cli_record_default_output_uses_cli_audio_path tests/test_voice_server.py::TestHealthAndStatus -q -rA` 退出码 0，50 个相关用例通过；`.venv/bin/python -m py_compile voice_server.py voice_agent.py magic_phone_cli.py tests/test_voice_server.py tests/test_voice_agent.py tests/test_runtime_resilience.py` 退出码 0。
- 新鲜验证：`.venv/bin/python -m pytest -q -rA` 退出码 0，329 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 2`、`tests/test_runtime_resilience.py: 29`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 26`、`tests/test_voice_agent.py: 85`、`tests/test_voice_server.py: 158`，合计 329；`git diff --check` 退出码 0。pytest 输出中的 429、TTS 失败、afplay 超时、malformed reminders、限流、空 ASR、熔断等为韧性测试预期输出。

## 2026-08-01 23:56 CST

- 继续以项目虚拟环境复跑基线：`.venv/bin/python -m pytest -q -rA` 退出码 0，340 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 5`、`tests/test_runtime_resilience.py: 31`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 28`、`tests/test_voice_agent.py: 85`、`tests/test_voice_server.py: 162`，合计 340。pytest 输出中的 429、超时、malformed reminders、afplay 失败、熔断和限流日志来自韧性测试，属于预期覆盖。
- CORS 收口已在当前工作树验证：默认不再返回 `*`，本地来源、局域网 IPv4 来源、cloudflared tunnel URL 和 `ASSISTANT_KID_CORS_ORIGINS` 显式来源组成 allowlist；局域网来源带 2 秒 TTL 缓存，`/api/tunnel` 仍会强制刷新。相关测试集中在 `tests/test_config.py` 和 `tests/test_voice_server.py::TestHealthAndStatus`。
- 运行态隔离继续保持：提醒播报临时 MP3 和 CLI 录音输出跟随 `ASSISTANT_KID_DATA_DIR`；`watchdog.sh`、`start_tunnel.sh` 和服务日志路径跟随 `ASSISTANT_KID_LOG_DIR`；测试通过 `tests/conftest.py` 隔离数据和日志目录。
- 从真实 `logs/app.log` 尾部继续观察：Finna TTS 在 21:50:07 仍有一次 429，现有中文降级、TTS 冷却和短句缓存只能降低重复打击，不能消除上游配额/限流；21:46 左右本地 Ollama 意图分类仍有读超时，现有 30 秒短熔断用于避免每个请求额外等待。
- 真实日志暴露一个新的可优化点：23:00:01 和 23:00:51 两次播报同一条主动休息建议。当前代码的晚安/晨报分别用 `last_time_suggest=<date>_late` 和 `<date>_morning` 去重，建议状态文件也已有跨进程文件锁和 `_claim_suggest_state()`；下一步要补并发/多进程回归测试，确认主动建议后台循环在多线程或重复启动下只会申领并播放一次。

## 2026-08-02 00:15 CST

- 主动建议运行锁可观测性收口：先运行聚焦 RED，确认 `/api/status` 缺少 `proactive_suggestions` 字段，dashboard 也没有“主动建议锁已占用”文案。实现后，`app.reminders` 新增 `SUGGESTIONS_STATE_FILE`、`PROACTIVE_LOCK_FILE`、`acquire_proactive_lock()` 和 `proactive_lock_status()`，与提醒调度锁共用同一套非阻塞 `flock` 状态探测逻辑；`voice_server.py` 改为导入低层常量和 helper，不再自己维护抢锁实现，`app/routes/system.py` 继续避免导入 `voice_server`，防止循环导入。
- `/api/status` 现在返回 `proactive_suggestions.locked/held_by_this_process/owner_pid/lock_file`；dashboard “运行健康”卡片新增“主动建议”状态、持有进程 PID 和建议锁文件路径，文案为“主动建议锁已占用”或“主动建议待命”。对应测试改为 patch 生产真实调用点：状态接口 patch `app.reminders.PROACTIVE_LOCK_FILE`，dashboard patch `app.routes.system.proactive_lock_status`。
- 继续优化后台主动建议健康检查：新增 `TestReminders::test_proactive_health_check_uses_nonblocking_cpu_sample`，RED 已确认旧代码每分钟调用 `psutil.cpu_percent(interval=0.5)`，会固定阻塞后台线程 500ms；实现改为 `psutil.cpu_percent(interval=None)`，复用 psutil 自上次采样以来的非阻塞 CPU 值。CPU/内存阈值、每小时健康告警去重和播报逻辑不变。
- 定向验证：`.venv/bin/python -m pytest tests/test_voice_server.py::TestHealthAndStatus::test_status_exposes_runtime_health tests/test_voice_server.py::TestHealthAndStatus::test_dashboard_exposes_runtime_health tests/test_runtime_resilience.py::test_proactive_runner_lock_is_cross_process_exclusive tests/test_runtime_resilience.py::test_runtime_files_use_configured_data_dir -q -rA` 退出码 0，4 项通过；`.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders::test_proactive_health_check_uses_nonblocking_cpu_sample tests/test_voice_server.py::TestReminders::test_proactive_loop_skips_work_when_machine_lock_is_unavailable tests/test_voice_server.py::TestHealthAndStatus -q -rA` 退出码 0，50 项通过。
- 新鲜验证：`.venv/bin/python -m py_compile app/reminders.py app/routes/system.py voice_server.py tests/test_voice_server.py tests/test_runtime_resilience.py` 退出码 0；`.venv/bin/python -m pytest -q -rA` 退出码 0，343 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 5`、`tests/test_runtime_resilience.py: 32`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 28`、`tests/test_voice_agent.py: 85`、`tests/test_voice_server.py: 164`，合计 343；`git diff --check` 退出码 0。pytest 输出中的 429、超时、malformed reminders、afplay 失败、熔断和限流日志来自韧性测试，属于预期覆盖。

## 2026-08-02 00:27 CST

- 真实日志继续暴露两个主动建议问题：`logs/app.log` 在 23:00:01 和 23:00:51 两次播报同一条“已经23点了，该休息了”；00:15:57 的天气建议播报为“雷阵雨 雷阵雨”。前者说明跨进程 `flock` 和建议状态申领不能覆盖同进程内重复 worker，后者来自白天天气和夜间天气直接拼接且未去重。
- 根因确认：`flock` 锁是进程内文件描述符作用域，同一进程内多个主动建议线程可以共享同一个锁句柄并同时进入循环；旧 `_start_proactive()` 每次调用都会新建 daemon 线程，没有存活线程保护。天气文本旧逻辑直接拼接 `dayweather + " " + nightweather`，当两个字段都是“雷阵雨”时会生成重复短语。
- 先写 RED：新增 `TestReminders::test_start_proactive_does_not_start_duplicate_worker_thread`，旧实现会启动两个 `threading.Thread`；新增 `TestReminders::test_proactive_weather_suggestion_deduplicates_day_and_night_weather`，旧实现通知文本为“雷阵雨 雷阵雨”。两个失败原因都准确指向目标行为。
- 实现收口：`voice_server.py` 新增模块级 `_proactive_thread`，`_start_proactive()` 在线程已存活时直接返回，否则保存并启动唯一 daemon 线程。天气建议改为按顺序收集、trim 并去重白天/夜间天气，雨雪判断逐个检查 weather part；不同天气如“阴 雷阵雨”仍保留，相同天气只播报一次。
- 定向 GREEN：`.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders::test_start_proactive_does_not_start_duplicate_worker_thread tests/test_voice_server.py::TestReminders::test_proactive_weather_suggestion_deduplicates_day_and_night_weather -q -rA` 退出码 0，2 项通过；`.venv/bin/python -m py_compile voice_server.py tests/test_voice_server.py` 退出码 0。
- 扩大回归：`.venv/bin/python -m pytest tests/test_voice_server.py::TestReminders tests/test_voice_server.py::TestHealthAndStatus -q -rA` 退出码 0，76 项通过。输出中的 TTS 429、afplay 超时、malformed reminders 和 304 DEBUG 日志来自韧性/状态路径测试，属于预期覆盖。
- 新鲜验证：`.venv/bin/python -m pytest -q -rA` 退出码 0，345 项全部通过；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 5`、`tests/test_runtime_resilience.py: 32`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 28`、`tests/test_voice_agent.py: 85`、`tests/test_voice_server.py: 166`，合计 345；`.venv/bin/python -m py_compile app/reminders.py app/routes/system.py voice_server.py tests/test_voice_server.py tests/test_runtime_resilience.py` 与 `git diff --check` 均退出码 0。pytest 输出中的 429、超时、malformed reminders、afplay 失败、熔断和限流日志来自韧性测试，属于预期覆盖。

## 2026-08-02 00:53 CST

- 语音 ASR 即时回执后的 WebSocket 会话组继续收口：新增 `tests/test_voice_server.py::TestWebSocket::test_ws_voice_registers_session_before_stream_task`。RED 已确认：语音连接创建后台流式任务前没有注册 `session_id`，测试内读取到 `client_session is None`、任务参数却为 `test_voice_session_group`，后续 `_ws_broadcast_to_session()` 会按默认会话找 peer，导致跨终端语音回复无法可靠同步。
- `voice_server.py` 新增 `_ws_join_session(ws_id, session_id)`，统一写入 `_ws_clients[ws_id]["session_id"]` 并维护 `_ws_session_groups`；文字路径和语音路径都在取消旧流、启动新流式任务前加入目标 session。连接断开清理继续按保存的 `session_id` 从会话组移除。
- 行为边界保持不变：语音 ASR 完成后当前说话终端仍先收到本地 `{"type":"asr","text":...}` 和 `{"type":"ack","message":"收到，我在听"}`，`ack` 只用于 UI 即时反馈，不写历史、不触发 TTS、不广播。
- 聚焦验证：`.venv/bin/python -m pytest tests/test_voice_server.py::TestWebSocket::test_ws_voice_registers_session_before_stream_task tests/test_voice_server.py::TestWebSocket::test_ws_voice_sends_ack_after_asr_before_stream_task tests/test_voice_server.py::TestWebSocket::test_ws_voice_ack_is_not_broadcast_before_stream_task -q -rA` 退出码 0，3 项通过。
- 新鲜验证：`.venv/bin/python -m py_compile voice_server.py tests/test_voice_server.py && .venv/bin/python -m pytest -q -rA && .venv/bin/python -m pytest --collect-only -q && git diff --check` 退出码 0；`.venv/bin/python -m pytest --collect-only -q` 统计为 `tests/test_config.py: 5`、`tests/test_runtime_resilience.py: 32`、`tests/test_security_fixes.py: 29`、`tests/test_utils.py: 28`、`tests/test_voice_agent.py: 85`、`tests/test_voice_server.py: 171`，合计 350。pytest 输出中的 429、超时、malformed reminders、afplay 失败、熔断和限流日志来自韧性测试，属于预期覆盖。
