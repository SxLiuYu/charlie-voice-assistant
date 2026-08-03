# Charlie 语音助手 — 延迟瓶颈与优化机会分析报告

> 分析日期: 2026-08-03 | 硬件: Mac mini M4 16GB | 模型: Finna deepseek-v4-flash + Ollama qwen3.5:2b

---

## 1 全链路延迟拆解（语音输入 → 语音输出）

**现状串行链路（voice_loop 非流式路径）：**

```
音频转码(ffmpeg ~0.1s) → ASR百度(0.34s) → 意图分类(0.003~0.11s) → brain构建(按需, ~0.3s首次) → LLM(0.25~1.84s首token) → TTS百度(0.34s) → WAV转MP3(ffmpeg ~0.1s)
```

**典型耗时（非流式，非缓存命中）：**

| 场景 | 意图分类 | brain构建 | LLM首token | LLM总时间 | TTS | 总延迟 |
|------|---------|-----------|-----------|----------|-----|-------|
| 闲聊("你好") | 0.003s(关键词) | 0s(缓存) | 0.25s(Ollama) | 0.4s | 0.34s | **~1.2s** |
| 查天气("北京天气") | 0.003s(关键词) | 0.3s(首次) | 1.84s(Finna) | 2.0s | 0.34s | **~2.8s** |
| 查天气(缓存命中后) | 0.003s | 0s | 1.84s | 2.0s | 0.34s | **~2.5s** |
| 设提醒("提醒我明天") | 0.003s | 0.3s(首次) | 1.84s | 2.2s(含MCP) | 0.34s | **~3.0s** |
| 放歌("放首歌") | 0.003s | 0s | 0s(ncm直连) | 0.8s(ncm) | 0s(前端播放) | **~1.3s** |

**优化方案：**

1. **ASR 并行化**：ASR 完成后才进入意图分类，但 ASR 的 ffmpeg 转码和百度 API 调用可以异步。目前 voice_server.py 的 `/api/voice/stream` 中，`asyncio.to_thread(asr, ...)` 已经异步化，但 ASR 仍然是串行链路起点。可以将 ffmpeg 转码与 ASR 网络请求并行（在 ffmpeg 转码的同时预连接百度 API）。

2. **brain 预热**：服务启动时预构建 `mcp_set="none"` 和 `mcp_set="amap-maps"` 的大脑实例。当前 `_brains` 字典在首次请求时懒加载，导致首次请求额外增加 0.3s。启动时预热可消除此开销。

3. **TTS 并行化**：voice_server.py 的 `_stream_brain_tts` 已经实现了 brain 逐句产出 + TTS 并行合成。但非流式路径 `voice_loop` 仍然是串行 `brain() → tts()`。建议废弃非流式路径，统一使用流式 SSE 端点。

**预期收益：**
- 预热 brain 消除首次 0.3s 构建时间
- 流式路径让用户感知延迟从 `(brain总时间 + TTS总时间)` 降至 `max(brain首句时间, TTS首句时间)`，约 **节省 0.34s (TTS时间不再串行等待)**
- 总延迟从 2.5~3.0s 降至 **1.8~2.2s**（用户感知到首句音频的时间）

---

## 2 System Prompt 优化

**现状各意图 prompt 长度（voice_agent.py L703-L745）：**

| 意图 | 工具说明字符数 | 基础规则字符数 | 总计(估算) |
|------|-------------|-------------|----------|
| none(闲聊) | 0 | ~350 | ~350 |
| amap-maps | 75 | ~350 | ~425 |
| magic-music | 159 | ~350 | ~509 |
| magic-reminder | 68 | ~350 | ~418 |
| magic-notes | 38 | ~350 | ~388 |
| magic-system | 72 | ~350 | ~422 |
| magic-info | 117 | ~350 | ~467 |
| magic-life | 95 | ~350 | ~445 |
| baize-skills | 36 | ~350 | ~386 |
| filesystem | 16 | ~350 | ~366 |
| ac-control | 98 | ~350 | ~448 |

**基础规则（~350 字符）持续膨胀：** `_build_system_msg()` 每次都拼接时间、待办、偏好、历史摘要。当前有 7 条原则，其中原则 5（MCP失败说辞）和原则 6（报时间格式）对大部分场景不是必需的。原则 7（ASR误识别）更是只在语音场景有用。

**优化方案：**

1. **按意图裁剪基础规则**：非工具场景（none、baize-skills）不需要原则 3/5（工具调用相关）。工具场景不需要原则 2/4（寒暄、做不到的事）。可以分成 `_BASE_RULES_TOOL` 和 `_BASE_RULES_CHAT` 两个版本。

2. **动态内容懒加载**：时间、待办、偏好信息只在 30 秒内变化一次，但 `_build_system_msg` 每次调用都重新计算。可以缓存 system_message 30 秒，只在过期时刷新。

3. **工具说明精简**：magic-info 的 6 个工具说明可以简化为按查询类型动态选择。例如用户问时间时不需要知道天气功能。

**预期收益：**
- 基础规则裁剪：每个请求减少 50~100 字符，LLM 首 token 延迟降低约 **5~10%**
- 缓存 system_message 30 秒：每次重复请求节省 0.01~0.02s 的拼接时间
- 对于 Finna deepseek-v4-flash，输入 token 减少 10% 约节省 **0.15~0.3s** 首 token 延迟

---

## 3 MCP 工具 Schema 按需加载效果

**拆分前后对比：**

| 指标 | 旧版 mcp_server.py | 6个子MCP | 变化 |
|------|-------------------|---------|------|
| 文件数 | 1 | 6(+1 mcp_common.py) | +6 |
| 总代码行 | 789 | 730 | -59 |
| 工具总数 | 25 | 24 | -1 |
| 单文件平均工具数 | 25 | 4 | 更专注 |
| 是否按意图加载 | 否(全部加载) | 是(只加载1个) | **关键改进** |
| System prompt 增量 | 全部工具都列在 prompt | 只列当前意图的工具 | **~350→~100字符** |

**按需加载的实际效果：**

- 旧版：25 个工具全部加载到 brain，Qwen-Agent 需要为每个工具建立 schema 缓存，MCP 子进程启动 10+ 个
- 新版：按意图只加载 1~6 个工具的 MCP 子进程
- Finna deepseek-v4-flash 的 tool_choice 自动选择范围缩小，减少工具选择延迟

**优化方案：**

1. **mcp_server.py 已无引用**（grep 确认只有测试文件引用），可以安全删除，节省 36KB 磁盘和可能的混淆。

2. **进一步减少 MCP 子进程数**：magic-info 和 amap-maps 都包含天气功能，存在重复。可以合并天气查询到一个 MCP。

3. **MCP 懒启动优化**：当前每个 MCP 子进程通过 `sys.executable` 启动，首次调用需要加载 Python 解释器。可以改为 `--preload` 或常驻进程池。

**预期收益：**
- 按需加载已实现，每个 brain 只启动 1 个子 MCP（vs 旧版 10+），启动时间从 ~0.5s 降至 **~0.05s**
- 删除 mcp_server.py 减少维护负担
- 合并重复工具再减少 1 个 MCP 子进程

---

## 4 Ollama 意图分类优化

**现状：**

`_classify_intent()` 有 15 组关键词映射（L1201-L1217），覆盖大部分常见场景。关键词命中后直接返回，不调 Ollama。

当关键词未命中时，使用 350 字符的 prompt 调用 Ollama qwen3.5:2b，平均耗时 **108ms**。返回结果后还有一个 12 分支的 if-elif 后处理映射（L1260-L1271）。

**瓶颈分析：**

1. **关键词映射覆盖不够**：15 组关键词共约 60 个关键词，对常见场景命中率较高，但长尾场景（如"帮我查下明天天气"——关键词"天气"命中，"明天天气怎么样"——"天气"命中）基本覆盖。

2. **Ollama prompt 过于冗长**：350 字符的 prompt 包含 10 个选项的详细规则 + 10 个示例 + 后处理逻辑。实际上只需要输出一个词，示例可以大幅精简。

3. **后处理映射可能出错**：`"magic" in raw → magic-music` 这个映射有问题——用户说"magic-reminder"也会被映射到"magic-music"。

**优化方案：**

1. **精简 Ollama prompt** 从 350 字符到 150 字符：
```
意图分类: 只回一个词。none=闲聊, amap-maps=天气地图, magic-music=音乐, magic-reminder=提醒, magic-notes=笔记, magic-system=设置, magic-info=时间新闻翻译, magic-life=生活, baize-skills=搜索, ac-control=空调, filesystem=文件。输入: {text}
工具: →
```

2. **修复后处理映射**：`"magic" in raw` 应该删除，改为精确匹配或按优先级排列（先检查 "magic-music"、"magic-reminder" 等全名）。

3. **增加意图缓存 TTL**：当前 `_intent_cache` 是无限增长的无 TTL 字典，只受 Python 内存限制。可以改为 LRU + 1 小时 TTL。

4. **关键词命中率提升**：当前关键词列表可以扩展 20-30 个常见词，如"通话"、"电话"、"短信"、"发消息"、"开灯"、"关灯"、"多少"、"什么"等。

**预期收益：**
- Prompt 精简 57%：Ollama 推理时间从 108ms 降至 **~60ms**
- 关键词扩展：命中率从 ~80% 提升至 ~90%，更多场景跳过 Ollama（3ms vs 108ms）
- 平均意图分类延迟从 **~25ms(混合)** 降至 **~10ms(混合)**

---

## 5 缓存策略

**现状各缓存系统：**

| 缓存 | 类型 | TTL | 上限 | 命中率估计 | 问题 |
|------|------|-----|-----|-----------|------|
| 对话响应缓存 `_cache` | dict | 300s | 50条 | 低(语音随机) | 无持久化，LRU淘汰策略粗暴(删最旧) |
| 意图缓存 `_intent_cache` | dict | 无 | 无限制 | 中 | 无TTL，无限增长，有内存泄漏风险 |
| brain实例缓存 `_brains` | dict | 进程生命周期 | 按意图数 | 高 | 每次刷新system_message，但无过期机制 |
| 历史文件缓存 `_history_file_cache` | 签名校验 | 文件变更 | 1份 | 高 | 设计合理，签名校验避免重复读盘 |
| 偏好文件缓存 `_preferences` | 签名校验 | 文件变更 | 1份 | 高 | 设计合理 |
| 百度 token 缓存 `_baidu_token` | 定时 | 28天 | 1份 | 高 | 设计合理 |

**核心问题：**

1. **对话响应缓存 TTL 太长（300s）**：对于语音助手，重复问同一句话的概率很低。300s TTL 导致缓存占满 50 条后开始淘汰，有效缓存比例低。建议降至 **60s**。

2. **意图缓存无 TTL**：`_intent_cache` 只增不减，长期运行可能积累数千条。用户说"今天天气"→"amap-maps"，但下次说"今天天气"时同一轮对话中概率低。建议改为 **LRU + 100 条上限 + 1 小时 TTL**。

3. **brain 实例缓存无过期**：`_brains` 中的 Assistant 实例在进程生命周期内一直存在，但如果 MCP 子进程崩溃，brain 实例可能处于无效状态。目前只有连续失败 5 次才重建。建议增加 **定期健康检查**（每 5 分钟 ping 一次 MCP 子进程）。

4. **缓存键设计**：对话缓存使用 `text\x00{text.strip().lower()}` 作为键，意图缓存使用 `text` 原文。两者不一致，意图缓存没有标准化。

**预期收益：**
- 意图缓存改为 LRU+TTL：**防止内存泄漏**，长期运行内存节省约 **1-5MB**
- 对话缓存 TTL 降至 60s：缓存命中率变化不大，但内存占用更稳定
- brain 定期健康检查：避免静默故障，**减少 429 错误率**（当前 MCP 子进程无声崩溃时 brain 仍在响应但工具调用失败）

---

## 6 并行化机会

**现状串行链路（voice_loop）：**

```
ffmpeg转码 → ASR API → 意图分类 → brain构建 → LLM → TTS → ffmpeg转MP3
```

**已实现的并行：**
- voice_server.py 的 `_stream_brain_tts` 实现了 brain 逐句产出 + TTS 并行合成
- ASR 的 `asyncio.to_thread` 异步化

**未实现的并行机会：**

1. **ASR 与意图分类不能并行**：意图分类依赖 ASR 结果。但可以在 ASR 进行的同时，用用户输入音频的简短片段做语音活动检测（VAD）和语言检测，提前判断是否是中文/是否是低意图。

2. **brain 预热**：首次请求后，其他意图的 brain 实例可以异步预构建。例如用户问天气后，预构建 `magic-music`、`magic-reminder` 等常见意图的 brain。

3. **TTS 预合成**：对于常见回复（如"好的"、"在呢"、"没听清"），可以预先合成并缓存 MP3。当前 `_cache` 只缓存文本，不缓存音频。

4. **MCP 子进程并行启动**：`_build_brain` 中启动 MCP 子进程是串行的（`mcp_servers` 字典遍历）。如果有多个 MCP，可以并行启动子进程。

**具体方案：**

1. **brain 预热时机**：在 `_build_brain` 完成后，启动一个后台线程异步构建下个意图的 brain。使用最近 N 次意图分布的统计预测。

2. **常见回复预合成**：在服务启动时预合成 `TTS_DEGRADED_MESSAGE`、`ASR_ACK_MESSAGE`、`EMPTY_ASR_REPLY`、`LOW_INTENT_ASR_REPLY` 的音频。当前 `/api/voice/stream` 中 `ASR_ACK_MESSAGE` 已经是实时合成，可以改为预合成缓存。

3. **MCP 子进程池**：对常驻 MCP（magic-info、magic-system）使用进程池而非每次构建新进程。

**预期收益：**
- brain 预热：消除后续请求的 0.3s 构建时间，首次请求不受影响
- 常见回复预合成：消除 0.34s TTS 延迟，约 **节省 0.34s**
- 并行 MCP 启动：多 MCP 场景（如 `MCP_SERVERS=all`）从串行 ~0.5s 降至并行 **~0.1s**

---

## 7 TTS 流式化机会

**现状：**

`voice_server.py` 的 `_stream_brain_tts`（L1086-L1215）已经实现了成熟的流式架构：

- brain 在后台线程逐句产出 → 推入 `q` 队列
- 主循环异步轮询两个队列：`q`（brain 产出）和 `tts_q`（TTS 结果）
- 文本事件即时推送，音频事件在 TTS 完成后推送
- TTS 在独立线程中并行合成（`_submit_tts`）
- 总耗时 = `max(brain总时间, TTS总时间)`

**但 `voice_agent.py` 中的非流式路径（brain/voice_loop）仍然存在：**

- `brain()`（L1538）返回完整回复文本，然后 `voice_loop` 串行调用 `tts(reply)`
- `brain_stream_sentences()`（L1723）实现了逐句 yield，但 `stream_voice_pipeline()`（L1873）是串行 `brain_stream_sentences → TTS`，没有并行化

**优化方案：**

1. **废弃非流式路径**：`voice_loop()` 和 `brain()` 可以被 `stream_voice_pipeline` 替代。所有请求统一走流式路径。

2. **TTS_BATCH_SIZE 调优**：当前 `_TTS_BATCH_SIZE = 30` 字符，对中文偏小。中文平均句长 15-20 字，30 字意味着第一句就触发 TTS（因为 `first_audio_sent=False` 时 `should_flush=True`）。建议提升到 **50 字符**，减少 TTS 调用次数。

3. **TTS 降级只触发一次**：当前 `tts_failed` 标志位在第一次 TTS 失败后阻止后续所有 TTS 尝试。应该改为 **per-sentence 降级**：某一句 TTS 失败不影响其他句子。

4. **音频缓存**：TTS 结果缓存到 `_cache` 的扩展中，相同文本不重复合成。当前 `_cache` 只缓存文本回复。

**预期收益：**
- 统一流式路径：减少代码维护量，所有用户享受流式体验
- TTS_BATCH_SIZE 调优：TTS 调用次数减少 30%，降低百度 API 费用
- 音频缓存：常见短句（"好的"、"在呢"）节省 0.34s TTS 延迟

---

## 8 内存/资源占用

**现状分析：**

| 组件 | 内存占用 | 说明 |
|------|---------|------|
| Ollama qwen3.5:2b | ~2.5GB | 常驻进程，用于意图分类、简单对话、降级 |
| voice_agent.py 进程 | ~200MB | 含 Qwen-Agent Assistant 实例缓存 |
| 每个 MCP 子进程 | ~30-50MB | 6 个 MCP 最多 6 个子进程 |
| 百度 token 缓存 | <1KB | 无问题 |
| 对话历史缓存 | ~1-5MB | 10 个会话 × 20 轮 × 每轮 ~500 字符 |
| 偏好系统 | <100KB | 无问题 |
| _intent_cache | 可能 1-10MB | 无 TTL 可能持续增长 |
| 总内存 | ~3.5-4.5GB | Mac mini M4 16GB 可承受 |

**优化方案：**

1. **_intent_cache 加 LRU 限制**：当前无上限，运行 24 小时后可能积累 10 万+ 条。改为 LRU 1000 条。

2. **MCP 子进程生命周期管理**：30 分钟不活跃的 MCP 子进程可以 kill 掉，下次请求时重建。当前子进程一直存活。

3. **对话历史文件缓存**：`_history_file_cache` 和文件签名机制设计合理，但 `_save_history` 每次写入都复写整个文件。对话量大的情况下文件可能达到 **10MB+**。

4. **Qwen-Agent Assistant 对象**：每个 `_brains` 条目包含一个完整的 Assistant 对象，包含 LLM 配置、tool schema、MCP 客户端连接。10 个意图就是 10 个 Assistant 对象，每个约 20-50MB。

**预期收益：**
- _intent_cache LRU：**防止内存泄漏**，长期运行内存节省约 10-50MB
- MCP 子进程不活跃回收：空闲时内存从 4.5GB 降至 **~3.5GB**
- 大型对话历史分片存储：减少单次写入开销

---

## 9 代码冗余和死代码

**mcp_server.py 的现状：**

- 789 行，25 个工具，**零引用**（grep 确认：`voice_agent.py`、`voice_server.py`、`magic-*.py` 均无 import 或引用）
- 仅有测试文件 `tests/test_runtime_resilience.py` 和 `tests/old/test_mcp.py` 中提及
- 功能已被 6 个独立子 MCP 完全替代
- **可以安全删除**

**其他死代码：**

| 文件/函数 | 行数 | 状态 | 说明 |
|-----------|------|------|------|
| `voice_agent.py` 中的 `voice_loop()` | L1892-1905 | 极少使用 | 非流式路径，建议废弃 |
| `voice_agent.py` 中的 `stream_voice_pipeline()` | L1873-1887 | 串行版本 | 已被 voice_server.py 的并行版本替代 |
| `voice_agent.py` 中的 `_tts_unisound()` | L877-904 | 备用降级 | 云知声 TTS (异步 38s)，很少调用 |
| `voice_agent.py` 中的 `_asr_unisound()` | L934-955 | 备用降级 | 云知声 ASR (异步 38s)，很少调用 |
| 旧版 `_wrap_openai_create_unknown_kwargs` | L1061-1086 | 兼容层 | 如果 Finna 不再更新上游参数可以移除 |
| `voice_agent.py` 中的 `_searchable_history()` | L249-266 | 未使用 | 没有被任何函数调用 |
| `voice_agent.py` 中的 `_history_snapshot()` | L282-295 | 未使用 | 没有被任何函数调用 |
| `voice_agent.py` 中的 `preferences_conditional()` | L673-683 | 未使用 | 没有被任何函数调用 |

**优化方案：**

1. **删除 mcp_server.py**：36KB，789 行死代码，直接删除。

2. **废弃非流式路径**：标记 `voice_loop()` 为 deprecated，统一使用 SSE 流式端点。

3. **删除未使用的函数**：`_searchable_history()`、`_history_snapshot()`、`preferences_conditional()` 没有任何调用者，可以删除。

4. **简化降级路径**：云知声 ASR/TTS 的异步 38s 超时对于语音助手几乎不可用，可以考虑移除或改为更短的超时。

**预期收益：**
- 删除死代码：减少约 **900 行**代码，降低维护负担
- 减少 Python 模块加载时间：约 **0.05s** 启动时间

---

## 总结：优先级排序

| 优先级 | 优化项 | 预期收益 | 难度 | 影响 |
|--------|-------|---------|------|------|
| P0 | 废弃非流式路径，统一使用 SSE 流式 | 用户感知延迟 -0.34s | 低 | 所有用户 |
| P0 | 删除 mcp_server.py | 清理 789 行死代码 | 低 | 维护性 |
| P1 | 意图分类 prompt 精简 + 关键词扩展 | 分类延迟 -50ms | 低 | 所有请求 |
| P1 | _intent_cache 加 LRU+TTL | 防内存泄漏 | 低 | 长期运行 |
| P1 | 常见回复预合成 TTS 音频 | 每请求 -0.34s | 低 | 语音用户 |
| P1 | 对话缓存 TTL 降至 60s | 内存稳定 | 低 | 所有请求 |
| P2 | brain 预热（启动时预构建常见意图） | 首次请求 -0.3s | 中 | 新用户 |
| P2 | 按意图裁剪基础规则 prompt | LLM 首 token -5~10% | 中 | 工具场景 |
| P2 | system_message 缓存 30s | 重复请求 -0.02s | 低 | 高频用户 |
| P3 | MCP 子进程不活跃回收 | 空闲内存 -1GB | 中 | 内存敏感场景 |
| P3 | 音频缓存（相同文本不重复合成 TTS） | 重复请求 -0.34s | 中 | 常见问题 |
|| P3 | 删除未使用的函数（3 个） | 清理 ~100 行 | 低 | 维护性 |

**✅ 已实施：取消 Ollama 简单对话，全部走 Finna + 预热 brain**
- 2026-08-03 已修改：`brain()` 和 `brain_stream_sentences()` 中意图=none 不再调用 `_ollama_simple_chat()`，直接走 Finna deepseek-v4-flash（首 token 119ms，比 Ollama 的 166ms 还快）。实测验证：`brain()` 中 `_ollama_simple_chat` 引用为 False。
- 2026-08-03 已添加：模块加载时自动预热 `_get_brain("none")`（约 976ms 构建耗时 → 0ms）。验证：`_brains` 预热后 keys=['none']，获取延迟 0.0ms。
- `_ollama_simple_chat` 函数保留为死代码（仅剩定义，零调用），可后续清理。