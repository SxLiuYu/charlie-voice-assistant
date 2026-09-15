# Charlie 第三轮修复方案 —— 第二轮遗留四项

> 时间：2026-08-27
> 基线：两轮修复全部在位，pytest 939 collected（893 passed + 46 skipped）全绿
> 方法：analyzer 施工级设计（含固件源码核查），双 worker 并行实施，reviewer 验收

---

## 一、R3-A：system_msg 动静态拆分（C4）

### 设计结论
- 现状缺陷：`_build_system_msg` 整段按 TTL(300s) 缓存 → 时间/session_facts/偏好/记忆在 TTL 内冻结；且 session_facts 同时被 `_build_wm_anaphor_prompt` 每轮实时注入 messages[0]（llm.py:904-935），**重复注入**（一份缓存陈旧值 + 一份新鲜值）。
- 拆分边界：
  - **静态（进 TTL 缓存）**：角色 personality、通用回复规则(含 VOICE_RULE 占位)、demo banner、_MCP_SYSTEM_PROMPTS 工具文档、persona prompt；
  - **动态（每轮实时拼，不进缓存）**：_context_summaries 历史摘要、list_preferences、_build_user_profile、magic_memory 记忆注入、format_context_for_prompt(session_id)。
  - **删除**：system_msg 内的 working_memory session_facts 块（与 wm_prompt 注入重复，后者已是实时单一来源）。已核实 brain.system_message 无其他消费者依赖该块。
- 实施形态：改动仅限 `agent/system_msg.py` —— `_build_system_msg_impl` 改为「static 缓存段 + dynamic 实时后缀」，`_get_brain` 调用方零改动透明受益。允许顺带把 `_chat_lite_stream` 的 `_build_system_msg("none")` 补传 session_id（一处微调）。

### 测试设计（新文件 tests/test_round3_system_msg_split.py）
1. session_facts 设定后：system_msg 不含工作记忆块；`_build_wm_anaphor_prompt()` 含该 fact（单一来源验证）；
2. 缓存生效性：同参数两次调用 static 段一致（命中缓存）；改变偏好/摘要后再次调用 dynamic 段即时刷新（不冻结）；
3. NB6 兼容：摘要仍按 session_id 注入。

## 二、R3-B：MQTT 下行流式分句（NH4）

### 设计结论
- 现状：`_process_utterance` 整段 brain→整段 TTS→推送，首音延迟=完整推理+完整合成；`_handle_abort` 为 no-op。
- 重写为流式管道：`brain_stream_sentences(asr_text, session_id=f"mqtt-{device_id}", channel="voice", cancel_event=<abort_flag>)`（签名与 cancel_event 已由第二轮 C1 支持）逐句迭代，每句 `voice_agent.tts_to_mp3(sentence)` 后立即推送；WS 对齐的协议序列：**整段回复一次 tts start → 每句 sentence_start + opus 包 → 最后一次 tts stop**（保留 push_tts_single 给单段场景不动）。
- abort 机制：per-device threading.Event 字典；`_handle_abort` set 后循环 break 且传入 cancel_event 让 llm worker 协作退出；短延时自动 clear 复用。
- 异常路径：单句 TTS 失败跳过该句继续下句；brain 流式整体异常时若一句未推过则走既有"语音处理失败"文本兜底。
- 不改 agent/llm.py（仅消费生成器）。

### 测试设计（新文件 tests/test_round3_mqtt_streaming.py）
mock brain_stream_sentences yield 两句 + mock tts_to_mp3：断言 tts start×1 / sentence_start×2 / tts stop×1、跨句 UDP sendto 连续；abort 中途 set → 推送停止但 tts stop 已发；单句合成失败 → 该句跳过后续照常。
适配：NH1 用例改为 mock brain_stream_sentences/tts_to_mp3（原 mock voice_agent.brain/tts 不再被调用）。

## 三、R3-C：下行 seq 单调递增 + per-device 锁（C3）

### 设计结论
- 固件实证不可得：仓库 .ino 是遗留 HTTP 参考代码（头注释明确实际部署用预编译 bin），无 MQTT/UDP/seq 消费端源码。基于 RTP 标准语义推断：采用 **per-device 单调递增 uint32** 最安全。
- 实现：hello 建 session 时带 `"seq":0` 与 `"seq_lock":threading.Lock()`；所有下行音频发送路径（push_tts/_send_audio、push_tts_single/_send_audio、NH4 流式发送器统一走一个取号 helper）：锁内一次性领取连续号段 `(cur, cur+len(packets))`，`& 0xFFFFFFFF` 回绕；goodbye 随 session 清理；删除死代码 `self._local_seq`。
- 生命周期首帧从 0 起（兼容固件对首次 seq=0 的潜在期望），连接期内跨段/跨句连续。

### 测试设计（并入 R3-B 测试文件或独立）
1. 同设备两次连续推送：解包 seq 断言第二段首帧 = 第一段末帧 + 1；
2. Barrier 双线程并发推送同设备：seq 无重复（锁生效）；
3. hello→push→goodbye→hello：seq 从 0 重新计。

## 四、R3-D：AC 共享线程池

### 设计结论
- 模块级惰性单例 `ThreadPoolExecutor(max_workers=int(os.getenv("AC_POOL_SIZE","2")), thread_name_prefix="ac-tuya")`，atexit 注册 `shutdown(wait=False)`；
- `_run_ac_with_timeout` 改为 `executor.submit(fn)` + `future.result(timeout)`，TimeoutError 返回 `(False, timeout_msg)` 行为不变；不再每次新建/销毁池；
- 已知限制注释：超时返回后 worker 至底层 HTTP 自然结束才释放；AC 低频 + 池大小可调兜底。

### 测试设计（tests/test_round3_ac_pool.py）
单例一致性；并发多请求全部完成且受 max_workers 约束排队；超时后池仍可用；shutdown 后重建。NH6 既有用例 sleep(10) 改 sleep(0.5) 减少泄漏 worker 占用共享池时长。

## 五、施工顺序与验收

| 序 | 内容 | 文件 |
|----|------|------|
| worker-1 | R3-A | agent/system_msg.py（+可选 llm.py 一处微调）、tests/test_round3_system_msg_split.py |
| worker-2 | R3-B → R3-C → R3-D（同 worker 串行，避免 mqtt_server/共享测试文件冲突） | app/mqtt_server.py、agent/device_control.py、tests/test_round3_mqtt_streaming.py、tests/test_round3_ac_pool.py、tests/test_round2_hw_fixes.py(NH1/NH6 用例适配) |
| reviewer | 对照本方案逐项核销 + 回归风险清单 | 只读 |
| 主代理 | 全量 pytest 终验 | — |

约束：两 worker 各自只跑定向测试（新增文件+受影响文件），全量回归由主代理在合流后统一执行，避免并行跑套互相干扰。

---

## 六、代码审查结果与实施后修正

总体结论：**合入**。四项逐条核销通过（R3-A×3 ✅ / R3-B abort 竞态与互斥性 ✅ / R3-C 并发安全+回绕+WS 未波及 ✅ / R3-D 幂等保护 ✅）。实施后由主代理落实的审查意见与本轮新发现：

| # | 来源 | 内容 | 处理 |
|---|------|------|------|
| F1 | 实施发现 | device_control.py `_ac_pool: Optional[...]` 注解缺 import——本机 Py3.14 PEP649 惰性注解掩盖，Py3.12 直接 NameError | 补 `from typing import Optional` |
| F2 | review/实施 | 下行发包只保证号段分配原子，实际发送线程可交错乱序（并发测试捕获 [0,3]）；方案书设计意图为"per-device 锁串行推送"未实现完整 | 重写 `_send_audio_packets`：领号+整段发包全程持 per-device seq_lock；删除独立 `_alloc_seq`（所有调用点已统一）|
| F3 | review/测试 | brain 流启动失败时 `_ensure_tts_stop` 会补发无 start 配对的孤立 tts stop | 加 `tts_start_sent` 守卫：从未 start 就不补 stop，兜底走 _push_text_tts 完整序列 |
| F4 | 测试修正 | worker 自写用例缺陷：字符串解包断言('o' vs 'ok')、Barrier(4) 撞 max_workers=2 池死锁、假 MP3 喂真实 ffmpeg、sendto mock 类属性隐式绑定多传 self、seq 计数先污染后断言、固定 sleep 时序脆弱 | 全部改为正确 mock（staticmethod/xc.mp3_to_opus_packets patch）、先装收集器再推送、轮询 deadline 替代固定 sleep |
| F5 | review 待验证→销项 | MQTT tts start 多 text/voice 字段 vs WS 无——系 MQTT 既有协议（push_tts_single 原有字段），对现网设备已验证兼容，不随本轮变更 |
| F6 | 全量回归发现 | 会话 LRU 上限可被超限高度自我维持：reset_history 直建路径不驱逐 + 单次驱逐跟不上 churn，全量下 test_max_sessions_limit 暴露 | 新增 `_evict_overflow_sessions_locked()`（while 收敛到硬上限），_get_history 与 reset_history 两创建路径共用；属真实生产健壮性修复 |

### 过程事故与恢复记录

实施后主代理误执行 `git checkout agent/history.py` 将该文件回退到 HEAD（丢失两轮未提交修复）。恢复路径：从 ZCode 会话数据库（~/.zcode/cli/db/db.sqlite part 表）提取 reviewer 对最终版文件的 Read 输出，剥离行号前缀还原 9825 字节/286 行完整源码；以 `test_round2_brain_fixes.py`(20) + `test_voice_agent.py` 全类回归验证行为一致。事故原因（把清理探针的 sed 写成了 checkout 探测）已记录防再犯。

## 七、最终验证证据

```
$ cd charlie && python3 -m pytest tests/ --tb=short -p no:warnings -q
968 collected, EXIT=0（0 failed / 0 error）
定向套件：
  tests/test_round3_mqtt_streaming.py + test_round3_ac_pool.py +
  test_round2_hw_fixes.py + test_mqtt_server.py + test_device_control.py → 86 passed
  tests/test_round2_brain_fixes.py(20) + test_voice_agent.py + test_round3_system_msg_split.py(14) → 全绿
```

遗留至下一轮：`_process_utterance` ~120 行可拆分子方法（reviewer Minor）；MQTT seq 固件语义待真机联调确认；NB4 之外的 pytest junitxml 兼容问题沿用第一轮记录。