# Charlie 大脑 & 硬件链路 — 深度评估与修复方案

> 评估时间：2026-08-26
> 基线：commit 9811aa4（工作区含未提交修改），测试基线全绿
> 方法：双路并行深度审计（大脑链路 / 硬件链路），逐条源码核实后生成本方案
> 前置报告：`EVALUATION_REPORT.md`（2026-08-25 通用审计，本报告聚焦两大链路的具体缺陷）

---

## 一、评估结论摘要

### 大脑链路（agent/ + llm_config）
整体设计成熟：5 级 provider 降级、意图熔断、LRU+TTL 缓存、流式分句管道齐全。
核心缺陷集中在**三类**：
1. **缓存/状态键缺少会话维度** —— 多设备/多用户场景下跨会话串台（B1/B2）；
2. **死代码/失效逻辑** —— 工作记忆快照恢复永不执行、context 失败退避永不生效（B3/C10）；
3. **热路径浪费** —— 非 GLM 429 误清全部 brain 缓存、barge-in 孤儿 worker 烧 token、缓存命中不入历史（B5/B8/B9）。

### 硬件链路（xiaozhi_ws / mqtt_server / device_control / tuya / asr_tts / tts_player）
WS 主路径功能完整（分句流水线、barge-in、自适应阈值），但存在：
1. **必现 bug** —— MQTT 下行采样率协商 16k 实编 24k（变速变调）、队列满 1 秒静默丢句子（H1/H2）；
2. **事件循环阻塞** —— Silero VAD torch 推理、整段 Opus 解码、ffmpeg 唤醒音合成都在主循环同步跑（H4/H5/H6）;
3. **设备控制假成功** —— 命令返回值被忽略、"不要开空调"会开空调（H3）。

### 测试基线
`pytest tests/ -q`：**100% 通过**（约 900+ 用例，少量 skip），无失败。

---

## 二、修复清单

### A. 大脑侧（9 项）

| # | 严重度 | 文件:位置 | 问题 | 修复 |
|---|--------|-----------|------|------|
| B1 | P1 | agent/cache.py:13,24,39 | 响应缓存 key 只含文本，"明天呢"等短句跨会话返回错误答案；角色切换不失效 | key 加入 session_id + 当前角色；调用点透传 session_id |
| B2 | P1 | agent/system_msg.py:192 | system msg 缓存无 user 维度，切换用户后 5 分钟内拿到他人偏好/记忆注入的 prompt | cache_key 加入 CHARLIE_USER_ID |
| B3 | P1 | agent/llm.py:1222/1462 | WM 快照恢复条件 `session_id != _wm_snapshot_session` 恒 False，多会话工作记忆永久互踩 | 入口记录前一 session id，finally 与之比较决定是否恢复 |
| B4 | P1 | agent/persona.py:_save | 每轮 2 次裸写 persona_state.json（非原子、无锁），并发可写坏文件致人设随机重置 | 模块级 Lock + tempfile + os.replace 原子写 |
| B5 | P1 | agent/llm.py:993-1002 | stepfun 等 non-GLM provider 429 也轮换 GLM 模型并清空全部 brains → 全量重建（秒级+MCP 子进程） | 仅当当前解析端点是 GLM 时才走 GLM 轮换+清缓存分支 |
| B6 | P2 | agent/history.py:242-260 | `_trim_history_tokens` 无锁 pop 共享列表，与并发 append 竞态 | 全程持有 _history_lock |
| B7 | P2 | agent/llm.py:283 | intent 审计 output_data 用了残留循环变量，恒记 "mimo-vision"，污染下游分析 | 改记实际命中域集合 |
| B8 | P2 | agent/llm.py:1389-1399 | MAX_CHUNK=80 定义未用，长句整段 yield → TTS 首包延迟线性膨胀 | 分句加硬上限切断 |
| B9 | P2 | agent/llm.py:1256-1259 | 缓存命中直接返回不入 history，下一轮指代失据 | 命中时同样 _append_history |

### B. 硬件侧（8 项）+ 启动优化（1 项）

| # | 严重度 | 文件:位置 | 问题 | 修复 |
|---|--------|-----------|------|------|
| H1 | P0 | app/xiaozhi_ws.py:569-595 | `_put_blocking` 队列满 ~1s 即丢句子，长回复尾部静默截断 | 改为绑定取消信号持续重试（连接断开/cancel 才放弃），上限放宽至分钟级 |
| H2 | P1 | app/mqtt_server.py:27/280 vs 652/758 | hello 协商 16kHz 但 mp3_to_opus_packets 默认按 24k 编码 → MQTT 下行语音变速变调 | 两处调用显式传 DOWNLINK_SAMPLE_RATE |
| H3 | P1 | agent/device_control.py:57-66,78,124-131,149 | ① 无否定词过滤："不要开空调"会开空调 ② ac_scenes_command 返回值被忽略，失败仍播报"已打开" | 否定词守卫 + 检查命令返回值并如实反馈 |
| H4 | P1 | app/xiaozhi_ws.py:1139/1168→212-227 | Silero VAD torch 前向每帧(~17fps)同步跑在事件循环里，占住主循环拖慢所有连接 | VAD 判定挪到线程池执行（asyncio.to_thread） |
| H5 | P2 | app/xiaozhi_ws.py:893-898 | 整段语音 opus 解码+静音扫描（最长36s音频）同步跑在事件循环，可阻塞 100ms+ | asyncio.to_thread 包裹 |
| H6 | P2 | app/xiaozhi_ws.py:254-259/1258 | 首次唤醒在事件循环内同步跑 ffmpeg 合成 beep（超时上限10s） | 启动预热线程顺带预热 beep；调用点改 to_thread |
| H7 | P2 | agent/tts_player.py:51-62 | 跨线程 put_nowait 不唤醒事件循环，结果延迟最高 1 个 timeout；队列无界 | 补 call_soon_threadsafe 唤醒 + 有界队列满丢最旧 |
| H8 | P2 | agent/asr_tts.py:493-526/597-600 | 百度 token 缓存 28 天，token 失效(110/111)类错误从不清除 → ASR 长期降级 | err_no∈{110,111,113} 时清空 token 强制重取 |
| W1 | P2 | voice_agent.py:401-406 | import 期同步预热 brain(none)，MCP 慢时进程启动卡最长 60s | 移入 daemon 后台线程 |

### C. 记录在案、暂不修复（风险/工作量原因，留待后续迭代）

| # | 位置 | 问题 | 不修原因 |
|---|------|------|----------|
| C1 | xiaozhi_ws barge-in | 打断后孤儿 worker 跑完整个推理（烧 token/MCP 副作用） | 需要给 qwen_agent 流加协作式取消 Event，涉及核心库语义，单独迭代 |
| C2 | mqtt_server UDP 收包 | WAV 头当 PCM 喂 RMS/VAD、每包新建 Decoder、单线程串行 | MQTT 是备用路径，重构解码管道工作量大 |
| C3 | mqtt_server seq | 下行 seq 每次推送归零、无 per-device 锁 | 同上，需固件联调验证 |
| C4 | system_msg TTL | 时间/WM 事实冻结进 prompt 最长 5 分钟 | 动静态拆分属结构性改动 |
| C5 | tuya_api | 快路径无整体超时预算（最坏数百秒） | 需要压测确定合理预算值 |
| C6 | context.py | 失败退避死代码（内层吞异常致 fail-count 恒 0） | 低危（仅多余刷新线程），随 C4 一起做 |
| C7 | tts_proxy/xiaozhi_codec | 死代码、伪 LRU 缓存淘汰 | 低危 |

---

## 三、实施顺序

1. 大脑侧 B1→B9（互相独立，逐项小步提交粒度）
2. 硬件侧 H1→H8、W1
3. 新增回归测试覆盖关键修复（缓存隔离、否定词、采样率参数）
4. 全量 pytest 回归 + code review

## 四、验收标准

- 全部 A/B 组修复落地且逐条可在 diff 中对应；
- 新增测试覆盖 B1/B3/H3/H2 等可离线验证项；
- `pytest tests/ -q` 保持 100% 通过（不引入新失败）；
- 未修复项在上方 C 表中明确记录理由。

---

## 五、代码审查结果（reviewer 意见分诊）

审查结论：**修复后合入**。逐条分诊如下：

| 审查意见 | 分诊 | 处理 |
|----------|------|------|
| P1-2 `_put_blocking` RuntimeError 会吞 sentinel | **驳回** | 已核实 `collect()` 的 finally 直接经 `run_coroutine_threadsafe(q.put((None, _SENTINEL)), loop)` 投递结束标记，不经过 `_put_blocking`，无吞没路径 |
| P1-1 history append/trim 非原子窗口 | **评估为良性** | trim 与 append 在不同入口，锁内"成对 pop 2 个/成对 append"保持配对不变式；窗口内最坏情况是多一轮 trim，无数据损坏 |
| P2-7 快路径双重入历史 | **误读** | fast-path 与 brain 路径互斥入口，各自只 append 一次 |
| P1-4 llm.py 注释提及已删除的 mcp_name 变量 | **采纳** | 注释已改写为描述"审计记实际命中域集合" |
| P2-5/6/8/9 | **接受为既有问题** | 均为本次改动之前即存在的风格/健壮性问题，记入 C 表范围，不在本迭代扩大改动面 |
| P1-3 | **延期** | 与 C 组同因：需要结构性改动，单独迭代 |

另发现测试基建已知限制一条：`tests/test_voice_agent.py::test_clean_for_tts_reuses_compiled_patterns` 用 monkeypatch 全局替换 `re.sub`，与 pytest `--junitxml`（junitxml 插件在报告阶段内部调用 `re.sub`）不兼容，会触发 INTERNALERROR。普通运行不受影响；如需接入 junitxml 报告需先改造该用例。

## 六、最终验证证据

```
$ python3 -m pytest tests/
867 passed, 46 skipped, 0 failed, 0 errors in 142.75s (0:02:22)
PYTEST_EXIT=0        （913 collected = 867 passed + 46 skipped）
```

新增回归测试：`tests/test_eval_fixes.py`（20 个用例），覆盖：
- B1 缓存会话隔离 / 角色维度 / default 桶向后兼容
- B3 working_memory.switch_session 切换清空、切回恢复、LRU 上限
- B4 persona 并发写（8 线程 × update 后 interaction_count==8，原子替换无损坏）
- H3 空调否定词守卫（"不要开空调"/"我不想开"/"我卧室太热了帮关掉"等不触发；祈使句仍触发）+ 失败如实反馈
- H8 百度 token 110 类错误强制失效

修复过程中同步解决的测试问题：
- `tests/test_device_control.py` mock 占位返回值 `{}` 与新的返回值检查冲突 → 改 `True`
- `tests/test_core.py` teardown 对后台线程原子替换的临时文件 listdir/remove 竞态 → OSError 容忍
- `tests/test_voice_agent.py::TestCache` 两个用例适配新 key 格式（过期用例改用 `_cache_key()` 计算真实 key；max_size 用例保存/恢复真实默认上限而非硬编码 50）
- voice_agent import 期预热改为后台线程后受 `SKIP_BACKGROUND=1` 门控，与 conftest 测试环境约定一致

遗留事项：C1–C7 见第二节 C 表；junitxml 兼容性见第五节。
