# Charlie 大脑 & 硬件链路 — 第二轮评估与修复方案

> 评估时间：2026-08-27
> 基线：工作区含第一轮未提交修复（FIX_PLAN.md 中 B1-B9/H1-H8/W1 已核实全部在位）
> 方法：双路并行深度审计（大脑链路 / 硬件链路），逐条源码核实后生成本方案
> 前置：`FIX_PLAN.md`（2026-08-26 第一轮）

---

## 一、本轮结论摘要

第一轮 18 项修复经逐行抽查全部在位且有效（含 reviewer 分诊结论复核）。
本轮新发现问题集中在四类：

1. **多用户数据隔离失效** —— 历史持久化的用户分流函数是死代码（NB1，P0）；
2. **共享可变状态竞态** —— 共享 brain 实例的 system_message 被临时替换/恢复（NB2）、GLM 模型轮换无锁（NB7）；
3. **键维度不一致串台** —— 上下文缓存用 default 会话（NB5）、裁剪摘要写读 key 不匹配（NB6）、切用户不清缓存（NB8）；
4. **热路径浪费与无界阻塞** —— MQTT 双重 ASR（NH1）、每帧新建解码器/WAV头污染VAD（NH2/NH3）、Tuya 最坏 ~485s 阻塞（NH6/C5）、barge-in 孤儿推理（C1 转正）、context 退避死代码（NB4/C6 转正）。

## 二、修复清单

### A. 大脑侧（8 项）

| # | 严重度 | 文件:位置 | 问题 | 修复 |
|---|--------|-----------|------|------|
| NB1 | P0 | agent/history.py:24,26-30,44,68,170,178 | `_get_history_file()` 按 CHARLIE_USER_ID 分流的函数从未被调用，所有读写仍走常量 HISTORY_FILE → 多用户历史混写入同一文件互相覆盖 | 所有 I/O 点改为动态调用 `_get_history_file()`；LOCK 文件路径同步动态化；签名缓存按 uid 分键 |
| NB2 | P1 | agent/llm.py:636,1351-1355,1470-1477 | feishu 流临时替换共享 brain 实例的 system_message，与并发 `_get_brain()` 刷新互踩；finally 恢复可能回写过时值 | 取消实例属性替换，channel 规则作为额外 system message 插入 messages 列表头部 |
| NB3 | P1 | agent/llm.py:670-675,1425-1427 | `_record_brain_failure` 连续 10 次 429 无 provider 守卫清空全部 brains，非 GLM provider 限流时死循环式全量重建 | 加 `active_is_glm()` 守卫，非 GLM 只走 provider 轮换 |
| NB4 | P1 | agent/context.py:27-110,128 | 外层 try 被 7 个内层吞异常 try 包裹，退避计数永不生效（C6 转正） | 网络/易失败字段异常冒泡到外层触发退避；纯内存字段保持独立 try |
| NB5 | P1 | agent/context.py:72,164-167 | recent_conversation 恒取 default 会话历史，设备会话注入他人对话 | get_context/_refresh_context_cache 增加 session_id 参数并透传调用链 |
| NB6 | P1 | agent/history.py:266 ↔ system_msg.py:309-310 | `_context_summaries` 写入 key=session_id、读取 key=user_id，只有双方都是 default 才命中 | 统一以 session_id 为 key，构建链路透传 session_id |
| NB7 | P2 | app/llm_config.py:375-386 | rotate_glm_model 全局索引无锁，并发 429 轮换索引竞态 | 模块级 Lock 保护 |
| NB8 | P2 | agent/llm.py:723-726 | set_current_user 不失效响应缓存，同 session_id 不同用户可命中前任缓存回复 | 切换时清 response cache 并失效上下文缓存 |

### B. 硬件侧（6 项）

| # | 严重度 | 文件:位置 | 问题 | 修复 |
|---|--------|-----------|------|------|
| NH1 | P1 | app/mqtt_server.py:598,620-624 | `_process_utterance` 先自己 ASR 过滤乱码，又把同一 WAV 整个喂给 `voice_loop` 再 ASR 一遍 → 每句双重识别，延迟翻倍烧配额，两次结果可能不一致 | 直接用已识别的 asr_text 进 brain + TTS 推送，绕过重复 ASR（注意核对 voice_loop 其余必要步骤不丢失） |
| C1 | P2 | agent/llm.py:570,1197 + xiaozhi_ws 调用点 | barge-in 打断后孤儿 worker 跑完整个 LLM 流（烧 token/MCP 副作用）（第一轮延期项转正） | `_brain_run_iter._worker` 与 `brain_stream_sentences` 支持 cancel_event，逐 token 检查提前退出；xiaozhi_ws 调用点传入流取消事件；保持既有 sentinel 结束语义不被破坏 |
| NH2 | P2 | app/mqtt_server.py:494-495,588,593 | UDP 收包循环每帧新建 opuslib.Decoder（~17fps），对比 WS 路径连接级复用 | per-device Decoder 缓存复用 |
| NH3 | P2 | app/mqtt_server.py:495-502 | RMS/VAD 把 `opus_decode_to_wav` 返回值连 44 字节 WAV 头一起当 PCM 计算，污染 VAD 输入 | 解码改产裸 PCM 直接喂 RMS/VAD；送 ASR 时再封装 WAV（与 NH2 同一改动区合并实施） |
| NH5 | P2 | app/xiaozhi_ws.py:1078-1080 | 收包循环每帧 `import app.schedulers` + `_touch_esp32()`，17fps 高频冗余 | import 上提；调用降频到每 100 帧 |
| NH6 | P2 | tuya_api.py:619-649,112-119 + agent/device_control.py:167 | AC 快路径 4 条命令串行，单请求最坏 ~97s，合计最坏 ~485s 占住 brain worker（C5 转正，风险高于原记录） | direct_ac_control 包整体超时预算（~15s），单请求 timeout 30s→8s；超时如实播报 |

### C. 记录在案、继续延期

| # | 位置 | 问题 | 不修原因 |
|---|------|------|----------|
| C4 | system_msg TTL | 时间/WM 事实冻结进 prompt 最长 5 分钟 | 动静态拆分影响 prompt 内容组织，牵涉面广；NB5/NB6 修复已消除其中跨会话危害，纯新鲜度问题留待单独迭代 |
| NH4 | mqtt 下行 | MQTT 路径非流式分句，首音延迟显著劣于 WS | 需重构 _process_utterance 为流式管道，单独迭代 |
| C3 | mqtt 下行 seq | seq 每次推送归零、无 per-device 锁 | 需固件联调确认 seq 语义后再动 |
| C7 | xiaozhi_codec | mp3_to_ogg_opus 死代码、伪 LRU | 低危不影响功能 |

## 三、实施顺序

1. 大脑侧 NB1→NB8（worker-A，测试文件 `tests/test_round2_brain_fixes.py`）
2. 硬件侧 NH1→NH6、C1（worker-B，测试文件 `tests/test_round2_hw_fixes.py`）
3. 受影响的既有测试同步适配（最小改动）
4. @reviewer 审查验收 + 全量 pytest 回归

## 四、验收标准

- A/B 组修复全部落地且可在 diff 中逐条对应；
- 新增回归测试覆盖：NB1 多用户文件隔离、NB2 并发通道隔离、NB3 非 GLM 429 不清缓存、NB4 退避生效、NB5 会话隔离、NH1 单次 ASR、NH2 解码器复用、NH3 裸 PCM、NH6 超时预算、C1 打断终止消费；
- `pytest tests/ -q` 全绿（不引入新失败）；
- 延期项在 C 表中保留明确理由。

---

## 五、代码审查结果（reviewer 意见分诊）

总体结论：**修后合入**。14 项逐条核销全部确认落地（见审查报告核销表），P1 修复完成后最终全量回归通过。

| 审查意见 | 分诊 | 处理 |
|----------|------|------|
| P1-1 `HISTORY_FILE/HISTORY_LOCK_FILE` 静态导出为 import 期快照 | **采纳** | voice_agent.py 移除 7 个快照别名导入（经 grep 确认无外部消费者），新增模块级 `__getattr__` 把 `_history/_sessions/MAX_HISTORY/MAX_SESSIONS/_history_lock/HISTORY_FILE/HISTORY_LOCK_FILE` 动态委托到 agent.history 当前值——读永远活值，兼容既有以属性方式访问的测试 |
| P1-2 MQTT `_process_utterance` brain() 硬编码 session_id="default" | **采纳** | 改为 `session_id=f"mqtt-{device_id}"` 按设备隔离；NH1 测试同步断言该参数 |
| P2-1 设备离线不清 Decoder 缓存 | **采纳** | `_handle_goodbye` 增加 `self._opus_decoders.pop(device_id, None)`（持锁） |
| P2-2 `_run_ac_with_timeout` 每次新建线程池 | 接受为已知限制 | 数 ms 级开销、AC 操作低频，记入下轮候选 |
| P2-3 NB4 测试 mock 函数命名可读性 | 驳回（本轮） | 功能正确，纯命名问题不值得再动测试 |
| 「待验证」active_is_glm 是否感知 GLM_MODEL 热更新 | **已核实、销项** | 实现为运行时调 `active_chat_endpoint()` 解析端点并与 glm provider base 对比，与模型轮换无关，无快照问题 |

实施过程中额外修复：

1. NB4 测试非确定性：`locate_text()`（真实网络调用，排在天气字段之前）在慢网时令刷新线程卡住、失败计数达不到 3 → 两个 NB4 用例把 locate_text 一并 mock 为快速失败；
2. 测试隔离加固：round2 大脑测试的 set_current_user 会改写 history 静态导出与 llm_state，仅 pop 环境变量会污染后续测试文件（test_voice_agent 锁测试 / test_runtime_resilience 断言被残留 uid 影响）→ 新增模块级 autouse 夹具对 CHARLIE_USER_ID、current_user_id、HISTORY_FILE/LOCK 导出、per-uid 签名缓存做快照恢复。

## 六、最终验证证据

```
$ cd charlie && python3 -m pytest tests/ --tb=no -p no:warnings -q
939 collected = 893 passed + 46 skipped, 0 failed    PYTEST_EXIT=0
```

新增回归测试：
- `tests/test_round2_brain_fixes.py`（20 用例）：NB1 多用户历史文件隔离/NB2 通道规则注入且 brain.system_message 不被改写/NB3 非 GLM 429 不清 brains/NB4 连续网络失败触发退避且退避期不起新线程/NB5 会话级 recent_conversation/NB6 摘要按 session 读写/NB7 并发轮换/NB8 切用户清缓存
- `tests/test_round2_hw_fixes.py`（6 用例）：NH1 单次 ASR+设备会话+TTS 来自 brain 回复/C1 cancel_event 及时终止消费且队列干净收尾/NH2 per-device Decoder 复用/NH3 RMS/VAD 吃裸 PCM 且送审 WAV 带 RIFF 头/NH5 _touch_esp32 每 100 帧一次/NH6 AC 整体超时返回如实文案

遗留事项（下一轮候选）：C4（system_msg 动静态拆分）、NH4（MQTT 下行流式分句）、C3（下行 seq 需固件联调）、C7（xiaozhi_codec 死代码）、reviewer P2-2（AC 超时的共享线程池）。
