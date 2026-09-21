# Changelog

本项目版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。每个版本的下载见
[GitHub Releases](https://github.com/SxLiuYu/charlie-voice-assistant/releases)。

## [未发布]

### 变更
- **新增用户画像统一门面 agent/user_profile.py（2026-09-21）**。

  用户状态早已存在但散在 6 个模块、6 种 schema（preferences / memory_store / evolution_data / persona / context / history），没有一个统一的"画像"对象可读可注入。新增 `get_user_profile(session_id)` 把各源聚合成一个规范画像对象（identity/location/preferences/interests/reply_style/persona_state/conversation_background/meta），**只读不改行为、不迁移数据**；同时给原先无主的"用户显式回复风格"（reply_style）一个持久新家 `user_profile_{uid}.json`。写路径双通道：`record_explicit`（"我喜欢X/我住在X/说简短点/记住X"确定性路由）+ `apply_extracted`（复用记忆抽取 + 敏感信息过滤：身份证/银行卡/手机号/病历/薪资一律不自动入库）。全程按 uid 分片（FR-14），跨用户零泄漏。

  逐级灰度、默认全关（见 .env.example）：`USER_PROFILE_ENABLED`（P1 只读聚合+审计）、`USER_PROFILE_AUTOSTORE`（P3 自动沉淀）、`USER_PROFILE_INJECT`（P4 system prompt 注入，只放稳定字段、情绪人设仍走 persona 通道避免打架）。默认关时注入路径不执行、输出逐字节不变。

  本轮补齐两处**之前只建了读路径、没接进实时链路**的缺口：

  - **画像捕获接入实时对话（brain 落库钩子）**：原先 `record_explicit` / `apply_extracted` 只在单测里被调用，真实对话不会写画像。新增 `voice_agent._capture_profile(text, session_id, reply)`，在 `brain()` 的两大返回路径（fast_path 命中、`_brain_llm` 结果）后台守护线程异步落库——`USER_PROFILE_ENABLED=1` 时解析显式指令（`record_explicit`），`USER_PROFILE_AUTOSTORE=1` 时每轮抽取（`apply_extracted`，含敏感闸）。纯后台、零延迟影响、任何异常静默吞掉；两个开关都关时函数第一行直返、原句/返回值完全不变。**这才让"用户偏好/喜好/位置/喜欢什么回复"真正被存储**。
  - **多轮指代消解 agent/coreference.py（`MULTITURN_COREF_ENABLED`，默认关）**：Laya 是无状态决策模型、遇到"把它关了/那首歌再放一遍/再看一眼院子"这种跨轮代词会判 out-of-scope 或抽错槽。新增就近消解——从历史（assistant 确认优先）抽设备/歌曲/地点/提醒/城市实体，把当前句的代词/零指代/指示词还原成字面实体，在 `brain()` 推理前折叠进当前句，喂给 Laya 快路径 + LLM。纯函数、无副作用、不落库；近因优先、候选缺失即保留原词交 LLM 兜底，绝不编造实体；任意异常降级透传。**这才让"多轮指代"有解法落地**（之前只有方案、没实现）。

  单测：`tests/test_user_profile.py`（13 例，含 `_capture_profile` 门控开关两测）、`tests/test_coreference.py`（10 例，歌曲/设备/提醒/地点就近还原、零指代、空历史透传、不编造、异常兜底、会话封装）。默认关时两套代码皆不进入生产路径、输出逐字节不变。

- **新增候选对话模型评估台架 scripts/bench_model_toolcall.py（2026-09-21）**。

  「工具调用换个模型、日常对话可行吗」这类问题需要可复测的答案。台架把问题拆成两个维度，在**真实 brain**（同 function_list、同系统提示、同 fncall 循环）上跑：A. 工具调用——5 个只读/可逆场景（天气/时间/提醒列表/股票行情/角色切换）记录是否发起调用、调了哪个、端到端延迟、最终答复；B. 日常对话——走生产闲聊通道（brain_stream_sentences → lite 流式）记录首包延迟与回复文本。候选用 env 覆盖切换（`--cand "标签:K=V,..."`），needle3 这类新模型到手后一条命令即可入测。

  首轮四候选实测（每格 1 样本 + 闲聊补测）：**工具调用全部 5/5 命中**；延迟差异巨大——step-5-preview（现役）工具延迟中位 4.11s、闲聊首包中位 1.67s 且出现过 **11.22s 首包/14s 总**的长尾；step-3.7-flash 工具 2.22s、闲聊首包 **0.31s**（约 5 倍差距），回复质量在线（人物一致、数据准确、无模板标记泄漏——基线在提醒场景把 `[状况/结果]` 提示标记泄漏进了语音回复）；step-3.5-flash-2603 接近（2.54s/0.38s）；agnes-3.0-flash 有中文场景回英文笑话的语种泄露与 11.66s 长尾，不推荐。**未切换现役模型**（等用户决定），台架复跑命令：`.venv/bin/python scripts/bench_model_toolcall.py`。

  台架现场还修掉一个自身缺陷：角色场景的"现场恢复"原先靠再问一次模型"切换回默认"——实测模型对该句式非确定性（四候选全部只回不调，live 角色被留在 jarvis），已改为直接调 `agent.roles.switch_role`，现场清理不依赖被测对象。

- **模型→工具契合度收尾：jarvis 按域收敛 + 提示清单从真实 function_list 派生（2026-09-20）**。
- **模型→工具契合度收尾：jarvis 按域收敛 + 提示清单从真实 function_list 派生（2026-09-20）**。

  上一轮"未做"两项落地，都在“模型真正看到的工具面”这条线上：

  ① **jarvis 15 工具按域收敛**（`agent/inproc_tools.py:312-322`、`agent/llm.py:1152-1158` stdio 同规则）：旧规则“非 none brain 全注入 jarvis”让天气/提醒等每个 brain 都背着 5 个 5-15s 的外部数据工具（股票/大盘/空气质量/地震/赛事），LLM 误调一次就是 10-40s 卡死。改为只注入 `mcp_set in ("all", "magic-jarvis")`。收敛不断路：`_KEYWORD_MAP` 新增 jarvis 词集（股票/行情/空气质量/地震/球赛…，与 amap 天气词集**零重叠**，有测试钉），分类器补 `magic-jarvis` 规则行。实测 amap-maps brain 从 31 把刀降到 16 把（jarvis 5 把慢刀全移除），all/magic-jarvis 不变。

  ② **修一条错位映射 + 让 magic-roles 可达**：`_KEYWORD_MAP` 里“切换到/换角色”原指向 **magic-jarvis**——jarvis 15 个工具里没有一个能换角色（`switch_role` 在 magic-roles 域），而 magic-roles 此前**无任何路由可达**（关键词表、分类器选项都没有它），“切换到贾维斯”实际会建一个只有股票工具的 brain。改指 magic-roles，分类器补选项与规则（口头禅“变成贾维斯/回到默认”由规则兜）。`_normalize_intent` 两域均已在 ALL_MCP，原样接纳。

  ③ **提示工具清单从真实 function_list 派生**（`agent/system_msg.py`）：手写清单与真实工具面反复漂移——amap 广告 5 个而真实 12 个（`calculate`/`get_exchange_rate`/`get_holiday_info`/`on_this_day` 模型根本不知道存在），filesystem 广告“读写文件”而真实域是 magic-notes 的备忘录工具，ac-control 干脆一个工具都不列（真实有 3 个设备工具）。改造：`_MCP_SYSTEM_PROMPTS`（手写清单）→ `_DOMAIN_NOTES`（只留行为规则），新增 `_derive_tools_line(mcp_set)` 从 `resolve_inproc_tools` 的 `.parameters` 实时派生“可用工具：name(参数)…”行，禁用桩 `run_code`/`code_interpreter` 进 `_HIDDEN_TOOLS` 黑名单。派生是 mcp_set 纯函数，静态提示缓存键 `(mcp_set, role)` 不变；失败返回空串优雅降级。装配后实测：amap 13 个真实工具全广告、none brain 也拿到真实可见的 4 个偏好工具、magic-roles/jarvis 新域带行为注释。

  测试：新增 `tests/test_jarvis_scoping.py` 27 个（注入收敛 7 参数化、路由 12、词集卫生 4、分类器 prompt 与两路径规则一致）；`test_toolcall_perf.py` 卫生三测改钉装配后文档 + 新增“广告的工具必须真实存在”零漂移断言与“注释里不许再出现手写清单”结构守卫；两处 decider 域清单相等断言改为方向性不变量（prompt ⊇ decider DOMAINS，decider 已停用、prompt 是唯一真源）。全量回归 **2564 passed / 0 failed / 0 errors / 49 skipped**。

- **openai 客户端连接池：复用 HTTP 连接，消除每次 LLM 调用的 TLS 握手（2026-09-20）**。
- **意图分类切专用快端点 step-3.7-flash + decider 停用：对话首包从 12~16s 降到 1~2s（2026-09-20）**。

  「对话太慢了」→ 搜 `charlie/logs/app.log` 定位：慢的全在**生成之前的意图路由层**，模型本身首包只要 0.3~0.7s。三句话的现场证据：

  | utterance | 路由 prologue | 路由层发生了什么 |
  |---|---|---|
  | 07:08「你帮我清理一下。」 | 13.3s | 分类跑在主对话的 step-5-preview 上，思维链 1842 字吃光 `max_tokens=512` ⇒ 正文恒空 ⇒ 放大预算重试一次 |
  | 21:46「12day12day。」 | 12.2s | 本地 decider context 注册打满 10s 读超时（14h 空闲后首次） |
  | 21:46「查lie，我说查lie。」 | 12.1s | decider 弃权后云端分类又打满 10s 读超时 |

  两个根因、两个修复：

  ① **分类专用端点**（`app/llm_config.py::intent_classifier_endpoint`）：默认 `step_plan/v1 + step-3.7-flash`，key 复用 StepFun 多 key 轮询路由器（与主链路同源；**不**原样读 `STEPFUN_KEY`——那是逗号分隔多把，整串当 Bearer 会 401，现场踩过）。env `INTENT_CLASSIFIER_BASE/MODEL/KEY` 可覆盖；解析不到 key 或 base 置空时自动回落活动端点（旧行为，不配也能跑）。`agent/llm.py` 的 `_post_intent` 改经这一收口解析。**预算策略/饿死检测/缓存纪律全部保留**——`_intent_request_params` 的 512+minimal、饿死重试 2048、饿死不写缓存，对任何 reasoning 端点都是安全网。`tests/test_intent_reasoning_starvation.py` 的旧守卫「分类必须留在活动端点」是相反决策，随现场证据作废并重写为「必须经 `intent_classifier_endpoint` 单点解析」（该结论成立于活动模型还是 step-3.5-flash 的年代，那时分类 max 1.08s）。

  ② **decider 停用**（`.env` `DECIDE_ENABLED=0`）：它的故障形态（10s 注册超时 / 2.5s decide 超时）比它省的 0.8~1.1s 更贵。停用后 `decide_intent` 在 `enabled()` 处直接返回 None，**零 HTTP**（测试用会炸的 `_get_session` 桩钉住）。服务可留着不占热路径，想恢复改回 1 即可。

  **真实验证**（live .env + 真实分类 prompt，7 个代表性输入）：走 LLM 的 5 个全部 0.6~2.3s 返回且答案正确——「你帮我清理一下。」从 13.3s 的错答 `none` 变成 **0.79s 的正确 `magic-agent`**；关键词命中的仍 0ms。全量回归 **2535 passed / 0 failed / 0 errors / 49 skipped**；新增 `tests/test_intent_classifier_endpoint.py` 15 个用例（默认值/router 取 key/轮询推进/env 覆盖/三条回落路径/与 env_catalog、.env.example 的三层一致/decider 停用零网络）。env_catalog 与 .env.example 同步加条目。已重启 live（voice_server PID 88352：新代码 + 新 .env，HTTP :8000 / MQTT / UDP :8888 全部就绪）。

  ⚠️ **重启时误杀了一个进程**：8100 端口的 `uvicorn app.main:app` 被我当作本项目管理 API 重启，实际它是旧入口 `app/main.py`（未追踪文件、已不在工作区）拉起的冗余实例——同一套路由 voice_server 全都 include（`voice_server.py:195-208`），管理面在 :8000 的 `/manage` 正常（200）。该实例无法原样恢复（代码文件已不存在），但其功能无损失；如果 8100 上还挂着别的东西，需要用户重新拉起。

- **openai 客户端连接池：复用 HTTP 连接，消除每次 LLM 调用的 TLS 握手（2026-09-20）**。

  qwen_agent 的 `TextChatAtOAI`（`.venv/.../qwen_agent/llm/oai.py:78/92`）每次 `_chat_complete_create`/`_complete_create` 都 `openai.OpenAI(**api_kwargs)` 建新客户端——新客户端 = 新 httpx 连接池 = 每次 LLM 调用重做 TCP/TLS 握手（热路径每工具轮 ≥2 次）。`api_kwargs`(base_url/api_key) 对单个 brain 固定，故按配置复用客户端即复用连接，省掉每调一次的握手。

  改法（`agent/llm.py`）：`_install_openai_client_pool()`（brain 构建时经 `_install_openai_compat` 调，幂等、进程内一次）把 `openai.OpenAI` 换成记忆化工厂，按 `(base_url, api_key, …简单 kwarg)` 复用客户端。安全前提都已验证：全仓无 `isinstance(..., openai.OpenAI)`（grep 实证），闭包在**调用时**查模块属性故自动受益；httpx 连接池对失效 keep-alive 自动丢弃重连，跨服务端超时复用无恙；持锁构造消掉"两线程同配置各建一个"的重复；`_oai_pool_key` 遇任一不可归一 kwarg（如 `http_client`/`default_headers` 实例）或位置参数 → 整体放弃缓存直接新建，绝不把不同配置混成一个客户端；有界缓存（默认 16，`OPENAI_CLIENT_CACHE_MAX`）FIFO 淘汰。

  测试 `tests/test_openai_client_pool.py` 8 个：同配置复用 / 异配置隔离 / 不可哈希 kwarg 绕过 / 位置参数绕过 / 幂等安装 / 有界淘汰 / 未安装不破坏，+ 集成用例「`TextChatAtOAI` 连打两次 `_chat_complete_create` 只建一次客户端」钉住热路径真复用（含全局态快照/还原 fixture，防补丁泄漏到其它用例）。全量回归 **2522 passed / 0 failed / 0 errors**。

- **模型→MCP 工具调用优化：性能去冗余 + 契合度补描述（2026-09-20）**。

  「模型调用mcp，模型调用工具的调用方式需要优化，提高性能，提高模型和工具的契合度」→ 沿用并行子智能体调研后实施，命中 qwen_agent `Assistant`（进程内 MCP、原生 function-calling）fncall 路径的两类浪费与一组模型可见性缺陷。三处改动全部加回归守卫，全量 **2514 passed / 0 failed**。

  **性能（不降准确换效率，Pareto）**：
  ① **静态系统提示去重**：`brain_stream_sentences`（`agent/llm.py:2498-2527`）把拼好的 system 放进 `messages[0]`，qwen_agent `Assistant.run()` 又 prepend 一份 `brain.system_message`——每次工具轮把几 KB 静态 persona/工具提示**发两遍**，且首份还漏出未解析的 `<<<VOICE_RULE>>>` 占位符。改为每次运行用浅拷贝 brain（`_make_single_system_brain`，`llm.py:749`）置空其 `system_message` 关掉重复 prepend，静态提示经 `_render_static_system_prompt`（`llm.py:726`，通道规则渲染 + 占位符兜底）后**只发一遍**，动态内容（通道/偏好/WM 回指/记忆召回）全保留、无共享态竞争（拷贝，不改缓存 brain）。探针实测每请求静态提示出现次数 **2→1**（`[2,2]`→`[1,1]`），占位符不再泄漏；非流式 `_brain_llm` 同路径一并修。
  ② **静态 system msg 走 TTL 缓存**：`_get_brain` 原先每轮 `_build_system_msg_static(mcp_set)` 重建并回写共享 brain；改走新增 `_build_system_msg_static_cached`（`agent/system_msg.py:206`），3 次调用重建 **3→1**，命中返回同一字符串对象（测试钉 `is` 同一性）。
  ③ **顺手清两处提示卫生**：`system_msg.py:29` 字面 `"\\n..."` 改成真换行；`system_msg.py:48-52` 删掉对**已禁用** `run_code` 工具的广告（`magic-info.py:346` 为永久禁用的 RCE 桩，模型照着调必然失败）。

  **契合度（模型真正看到的工具面）**：
  ④ **schema "launder hole"**：`agent/inproc_tools.py:86-97` 原先给缺描述的参数**用参数名回填 description**（模型看到 `"keyword":{"description":"keyword"}` 纯噪声还带偏）；改为缺描述就省略该键（真实描述原样透传，`Annotated[..., Field(description=...)]` 不改）。9 个 fit 测试。
  ⑤ **46 个"死文档字符串"工具复活**：一批 `magic-*.py`/`baize_skills_mcp.py` 工具把 docstring 字面量写在 `log.*(...)` **之后**——Python 只认函数体第一条为 `__doc__`，FastMCP 从 `__doc__` 取 description，于是**模型看到 27+ 个工具的空白描述**（含天气主力 `get_detailed_weather`/`get_current_time`、全部 recipe/reminder/feishu/system/notes/baize web_search 等）。修复=纯换位（字面量提为第一条语句，日志与逻辑不动、零行为变更）。AST 扫描 140 个 `@mcp.tool` 全有 `__doc__`、运行时经真实 loader 描述全非空；`tests/test_tool_descriptions_present.py` 7 个用例三层钉死（AST 不变量/真实 loader description/46 函数白名单/近邻"description 退化成同名"防护）。

  （当时未做、留待下轮的两项——jarvis 按域收敛、提示清单对齐——已于同日“模型→工具契合度收尾”一条落地，见下。）

- **magic-agent 全流程用户测试闭环 + 三项 ACK/路由优化（2026-09-20）**。

  「做全流程用户测试闭环」→ 搭了台架 `scripts/` 外的一支黑盒驱动（真语音进 live 服务：`say` 合成「排查一下网络」→ 16kHz opus 打 `ws://127.0.0.1:8000/ws/xiaozhi` → SenseVoice ASR → 意图路由 → 秒回 ACK → hermes 后台执行 → 飞书推送），把「语音进→路由→派发→执行→推送」整条链路在真服务上跑通并钉成可复跑的断言。**绿**：STT `'排查一下网络。'`（100%）→ 即时 ACK **t+0.82s 缓存命中、回复仅一句**（不阻塞在任务上）→ hermes `status=ok 产出=51字`（~5min）→ 推送尝试（本机未配 `FEISHU_APP_ID/SECRET`，走凭证门跳过——配置状态非代码问题，推送行为由单测覆盖）。闭环同时暴露并修掉三处：

  ① **消歧规则去 STRONG 漂移（`agent/llm.py`）**：原消歧依赖 `_AGENT_TASK_STRONG` 强标记表，与关键词表各自演进会漂移——"排查网络"在表里却匹配不上口语句 "排查一下网络"，共域时落 decider（360ms）而非即时。删除该表，规则简化为「magic-agent 词表全是任务型短语（无裸词），短句（≤15字/非复合）命中即真意图，搜索守卫（真搜索如"查一下怎么清理磁盘"）与复合句留给分类器」。"排查一下网络"回到 0ms 消歧。这一类"两份清单漂移"的 bug 靠合并成单一真源根治。

  ② **ACK 预热 + 保温 + 校验重试（`app/xiaozhi_ws.py` / `agent/magic_agent.py`）**：秒回 ACK 文案固定，首派发不预热要付 ~1.9s StepFun 合成，700ms 的 filler（"好的，我想一下。"）竞速会抢走首句→用户一次任务听双重应答。三招：启动预热 ACK（`_prewarm_hints`）；每次派发保温（`_refresh_ack_cache`，覆盖空闲 >1h 缓存过期）；**预热校验重试**——StepFun 启动瞬间偶发空响应会静默走 macOS `say` 兜底（兜底音频不写缓存=白预热），此前约 1/3 启动因此丢缓存、首派发被 filler 抢先；现预热后校验 ACK 真入缓存、未入则重试至多 3 次。

  ③ **缓存未命中之谜定位（非 bug，是兜底路径）**：插桩 `_synth_mp3` 抓到命中/未命中两侧的完整键——**键恒稳定**（`role=charlie voice=Ethan speed=1.0 model=qwen3-tts-flash emo=''`，情感/角色/语速均不漂移），未命中不是键不匹配，而是缓存条目在 producer GET 时不在（预热走了 say 兜底没 PUT，或 FIFO 淘汰）。所以 filler 抢先是"缓存真没热"时的合法兜底而非缺陷。闭环测试随之改为稳健判定：ACK 在回复里即算秒认领（允许 filler 在前），>预算或 >2 句（任务被同步化成长播报）才算失败。

  **闭环测试自身的两处效率修正**：后台轮询上限 300s→**950s**——hermes 墙钟极不稳定（"排查网络"实测 3min/`status=ok` ~ 15min/`status=timeout`，`--run-budget 600` 是算力预算非墙钟上限，`AGENT_CLI_TIMEOUT=900` 才是硬 kill），300s 会在任务跑完前误判超时；950s>900 保证无论 ok/timeout "任务结束"行都落窗口内，闭环断言确定性通过。轮询改「任务结束+任一推送行」即退，不再空等满窗。

  测试：`tests/test_magic_agent.py` 增至 35 个（新增 ACK 保温付费下次预算、派发前先保温后跑任务两条）；`test_magic_agent.py`+`test_xiaozhi_ws.py` 54 passed。全量回归待随本轮改动一并复跑。

- **本地 ASR 真正上线：SenseVoice 竞速生效 + 服务回归 .venv（部署修复，2026-09-19）**。

  「asr 本地模型的准确率怎么样，准确率高效率高就选用」→ 实测两者都高，已选用并落地。

  ① **准确率实测**（`say` 合成 10 句已知文本，.venv 里 sherpa_onnx 1.13.4 按项目同参数解码）：平均 **CER 3.3%**，语义 10/10 全对；3 个非逐字全对里，一个是标点差异（呢。vs 呢？）、一个是 `use_itn=True` 的数字转换（九点四十五分→9点45分，下游意图解析兼容）、只有一个是真同音字错误（介绍一下→以下介绍）。速度 **24–85ms/句**。同语句 Baidu live 实测 151–352ms（restart9.log）且丢过字（明天→天）。干净音频下本地质量不输云端、速度快 5 倍+，符合"准确率高效率高就选用"。

  ② **为什么一直没生效**：`models/sense-voice/` 模型文件齐全（228MB int8），但 `sherpa_onnx` 只装在 **.venv（python3.12）**；服务此前跑在系统 python3.14 上——每句话静默回退百度（日志 16 次 "No module named 'sherpa_onnx'"），16 轮 ASR 全是 provider=legacy。README 本来就规定 `.venv` 启动（`source .venv/bin/activate` → `python voice_server.py`），是中途重启用了裸 `python3` 偏离了文档部署。已切回 .venv（PID 88487），启动日志出现「SenseVoice 加载完成」+ warmup 已有本地 ASR 预热（首句不付 ~500ms 模型加载）。**这是本机部署约定：服务必须从 .venv 启，否则本地 ASR 静默降级、日志里没有加载行就是征兆**（.env.example 已写明）。

  ③ **竞速行为符合设计**：live 实测每句 `[asr-race] 本地=32-73ms 云端=timeout 选择=local 原因=本地干净+云端超宽限`——StepFun ASR 800ms+ 永远赶不上 50ms 宽限，本地全胜；本地乱码/为空时仍等满 1.2s deadline 落云端（`is_garbled_asr` 闸门保留）。云端 future 每句照发不撤回——这是刻意的乱码安全网（本地结果出来前无法预知质量），代价是每句多一次被丢弃的 StepFun 调用。

  ④ **验证**：live 闭环 33 PASS / 1 FAIL——唯一 FAIL 与切换前基线**逐条一致**（decider 对「太阳系里最大的行星是什么」判 magic-info conf=0.88 并正确答木星，测试期望弃权，属校准观点非回归）；7 轮 ASR overlap 全 100%、provider=sensevoice、T6 barge-in 0ms。全量回归 **venv 下 2363 passed / 49 skipped / 0 failed（exit=0）**——比系统 python 还多 252 个用例（sherpa/vosk/edge-tts 等 skipif 守卫的测试在 venv 里真正执行），两个运行时各自全绿。venv 缺 onnxruntime/chromadb/openwakeword 均不影响（前两者 app 无引用、sherpa 自带 runtime；后者有 try/except 降级，local_wake 本就不可用）。

- **意图分类接入本地 Decider-2B 决策头（decider-first，`agent/decide_intent.py` 新模块，老于拍板）**。

  「在 charlie 里能不能用 decide」→ 实测能用且正对痛点。`_classify_intent` 的 LLM 兜底环节此前 0.82–1.08s/句（stepfun），reasoning 模型思维链饿死过 P0（`_intent_request_params` 整个函数为它而存在）；Decider-2B（MLX 8bit 本地决策头，问题+选项→校准概率）是同一形状。**真机实测（28 域、charlie 真实语料）**：裸 context 53% 弃权不可用；charlie 域说明做 context 后 **25/28 全对、0 错判**，3 个弃权全是该弃的（ASR 乱码句 conf=0.25 正确拒答）；context 缓存路径中位 **253ms**。28 个选项超 26 字母 token 无碍。

  接入形状：decider-first——`DECIDE_ENABLED=1` 时每句话先问本地 decider（`context_id` 走 state_first 缓存），conf ≥ `DECIDE_INTENT_ABSTAIN`（默认 0.5）直接用（省云端 1s）；弃权/未启用/服务不可用 → None → 落回既有 LLM 分类，行为不变。热路径安全：模块**永不抛异常**；网络失败计连续次数（3 次冷却 300s，服务挂了不每句吃连接超时）；HTTP 400 unknown context 自愈重注册（decide_reuse 语义）；越界选项拒收。两处红线已钉死：context 非空（服务端 400）；只走全量/state_first 标定（T=1.30），不碰 schema 温度。

  已知代价：decide 服务进程常驻 ~4.6GB（16GB Mac 上与 charlie 同住）；253ms 不是文档的 44ms（28 选项长清单自身也要 prefill）。它只接意图分类一处——大脑回复/提醒解析/记忆提取等生成环节仍是 LLM 的活。

  配置：`DECIDE_ENABLED`（默认 0，.env 已开）/ `DECIDE_BASE` / `DECIDE_INTENT_ABSTAIN` 进 env_catalog 与 .env.example。测试：`tests/test_decide_intent.py` 14 个（热路径安全/弃权回落/冷却/自愈/越界拒收/与 LLM prompt 域集合一致性钉）；**conftest 强制 `DECIDE_ENABLED=0`**——.env 的开关会随 dotenv 泄进测试进程，让意图测试隐式依赖本机 decide 服务（真实外呼 + 非确定），decider 用例在各自文件显式开。

- **StepFun 多 Key 轮询路由（`agent/stepfun_keys.py` 新模块，老于拍板）**。

  「stepfun 有十个 key，可以做轮询」。`STEPFUN_KEY` 从此支持逗号分隔多把（单把写法行为不变），全部 StepFun 调用（TTS 批量/流式、ASR、LLM 建脑、realtime WS）共用**一个**本地轮询路由器：每次请求取下一把，单 key 配额 × N；撞 429 该 key 冷却 60s（`STEPFUN_KEY_COOLDOWN` 可调）自动滑到下一把，401/403 按 30 分钟隔离（欠费/被禁不反复撞墙），全部 key 都在冷却时兜底用当前指针那把、不阻塞不吞错（上游报错 → 既有熔断/降级链照常工作）。管理页 Key 校验改为只打第一把（否则整串会被当 key 发出去）。LLM 大脑客户端建脑时固化 key，其 429 分支做近似打标（标记最近发出的 key，低 QPS 下足够，标错也只是健康 key 冷却 60s 无害）。`keys_status()` 只回传 key 尾 4 位，不泄明文。

  测试：`tests/test_stepfun_keys.py` 新增 14 个（解析/轮询顺序/冷却跳 key/过期回归/全冷却兜底/状态不泄 key/TTS·LLM·校验·realtime 四路接线）；conftest 快照列表补路由器状态防跨测试污染。

- **TTS 云端只留 StepFun：撤掉百度与 Finna（`app/xiaozhi_ws.py` / `agent/asr_tts.py`，老于拍板）**。

  「不用百度的 TTS，只用 stepfun plan 的 tts」。三条链路逐一处理：

  ① **设备语音批量回落 `_synth_mp3`**（唯一的硬编码多 provider 链）：删掉 Baidu 首块和 Finna SSE 块，StepFun（`step_plan` 路径 `stepaudio-2.5-tts`）成为唯一云端 provider，macOS `say` 保留为离线兜底。连带清掉 `_finna_cooldown_until` 全局与 `FINNA_API_KEY` 读取；
  ② **推送/提醒链路 `tts_to_mp3` → `tts()`**：走 `TTS_PRIORITY`，默认链从 `baidu,stepfun,finna` 改为 `stepfun`（4 处默认同步：`agent/asr_tts.py` 两处、`app/runtime_settings.py`、`app/env_catalog.py`，外加 `.env` / `.env.example`）；
  ③ **设备语音主链路 `_open_pcm_stream`**：本来就是 StepFun SSE 流式（`XIAOZHI_TTS_STREAM` 默认开），不受影响，仍是三条里最快的。

  **实测依据（真 Key，2026-09-19）**：流式 SSE 首块 **554ms（23 字）/ 687ms（39 字）**；整句批量 1897ms / 3117ms；`step_plan/v1/models` 下只有 `stepaudio-2.5-tts` 一个 TTS 模型，**没有更快的替代**。对比上周的 realtime 实测 576–769ms —— realtime 相对现用流式是**平手不是提速**，所以本轮不动发声模型，只拔掉慢且多余的百度层。

  **⚠️ 冗余代价（必须知道）**：百度/Finna 撤出后，云端只剩单点 StepFun；它 429/超时/欠费时设备语音只剩 macOS `say`（机械音、中文质量差），不再有第二家云端接管。刻意接回只用一行：`TTS_PRIORITY=baidu,stepfun`（provider 代码保留未删）。

  ④ **启动预热 `app/brain_health.py`**：预热 TTS 从 `_tts_baidu` 换成 `_tts_stepfun`（百度 ASR token 预热保留，`ASR_PRIORITY` 仍含 baidu）。**顺带修掉一个潜伏 bug**：预热写缓存用的是裸字符串键 + 裸 bytes 值（`_tts_cache[reply_text] = audio`），而 `_tts_cache_get` 的键是 `(text, voice, speed, model, emotion)` 元组、值是 `(audio, ts)` —— 缓存里存的 4 条常见短回复**一条都读不到**，预热等于白做，每次还是现合成。改为走 `_tts_cache_put`。

  测试：`tests/test_synth_mp3_chain.py` 与 `tests/test_round8_infra.py` 改写钉新链（含"百度/Finna 零参与"断言），`tests/test_asr_tts_priority.py` 从隐式依赖默认链改为显式给定优先级 + 新增子进程验证真实默认值，`tests/test_round9_blindspots.py` 新增预热走 StepFun、不走百度 + 预热缓存条目必须能被读到两条，`tests/conftest.py` 移除已不存在快照键。另外 `tests/test_round10_tts_asr.py`、`tests/test_voice_agent.py` 里两个隐式依赖旧链长度（3 个 provider）的用例改为显式给定链。

- **Realtime 发声通道切到 step_plan 路径的 `stepaudio-2.5-realtime`（老于拍板）**。

  按"realtime 用 step_plan 2.5 的"（确认说的是 StepFun 那条通道，不是面壁 MiniCPM 原型），把默认端点从 v1 路径的 `stepaudio-3-realtime-preview`（限免预览版）换成订阅路径 `wss://api.stepfun.com/step_plan/v1/realtime` + `stepaudio-2.5-realtime`，与 LLM/TTS 共用同一条订阅计费通道。

  **真 Key 实测（2026-09-19，`scripts/stepaudio3_probe.py` 同协议，17 个样本）**：端点连通、默认音色 `jingdiannvsheng` 可用、采样率 24000Hz（24k 假设下 5.0 字/秒）、取消残留 0 字节（`status=incomplete`、无 `response.cancelled` 事件）、174 字长文本逐字一致、多轮连接无指令污染。首音频 **576–769ms**（preview 版 239–305ms；对比生产在用的 stepaudio-2.5-tts 1733ms 仍快 3 倍）。

  **实测同时发现两条 2.5 上的新协议事实（已写进 `realtime_tts.py` 文档头与 PLAN_FR13 §2.1）**：
  ① **照读不可靠**——17 个样本约 30–40% 被改写（助手口吻陈述句大多逐字一致，指令式/用户口吻句子失手率接近 100%，同一句重复请求结果不一致 ⇒ 偶发非确定）。instructions 是必要不充分条件，接线时必须比对 `response.audio_transcript.done` 与源文本、不一致丢弃回落常规 TTS（接线本身仍按 FR-13 决策等 ESP32 上线再做）；
  ② **429 限流**——连续请求直接 HTTP 429 拒连（15s 间隔仍被限 ⇒ 持续性配额），外加连接空闲超时（不发操作被服务端主动断）。生产"每轮一个短连接"的节奏不撞配额，`synthesize_streaming` 不抛异常、返回 ok=False 已保证失败回落。

  **改动面**：`agent/realtime_tts.py`（`DEFAULT_URL`/`DEFAULT_MODEL` + 文档头测量数据；环境变量沿用 `STEPAUDIO3_*` 前缀——改名会让存量 .env 静默失效）；`app/env_catalog.py`（三个 realtime 条目的默认值与计费说明：2.5 是计费模型 70 元/1M tokens，非限免，`ENABLED` 默认 0 控制成本）；`charlie/.env.example`（realtime 段 + TTS base/model 一并对齐 step_plan + 2.5-tts——此前 example 写 v1 + step-tts-2 与代码兜底、实际 .env 都不一致）；`scripts/stepaudio3_probe.py` 默认指向新端点；`docs/PLAN_FR13_REALTIME_INTERRUPT.md`（§2.1 标注切换与两代测量数据，风险/决策表更新计费口径，延迟对比改用生产模型 1733ms）。

  **回归**：`tests/test_step_plan_defaults.py`（新增 6 个）钉住三层默认值不漂移——模块常量、env_catalog 条目、`.env.example` 模板，外加从 asr_tts.py 源码解析兜底值对比（防"只改 base 没改 model"的半截切换，混用会 404）；`tests/test_realtime_tts.py` 更新 2 处断言并新增 2 个默认值用例。全量 **2375 用例（0 failed / 0 error）**，0 回归（基线 2367 + 新增 8）。

- **五轮全流程用户闭环测试（Rounds 36-40）：按用户入口逐条切链路跑闭环，发现并修复 15 个 bug**。

  前四轮闭环盯的是大脑链路内部（事故链回归、正常使用、飞书文本清洗），这五轮把**用户入口**逐条切开各跑一轮完整闭环：语音文本 → 真实意图分类 → 大脑/工具 → 外部系统（提醒文件、MQTT broker、飞书事件订阅、记忆存储）→ 落盘/投递/播报。每轮只 mock 外部世界（LLM HTTP、paho client、飞书 API、TTS 合成、时钟），链路代码全部真实执行，断言落在外系统真实状态上（reminders.json、episodic_memories + SQLite events、MQTT topic/payload、飞书 API 调用序列）。

  **Round 36 提醒链路**（`tests/test_round36_closed_loop.py`，21 个，修 4 个）：
  ① 循环提醒投递放弃后**永久失联**（`app/reminders.py`：重试上限标 failed 时不再为 repeat 提醒装填下一次——TTS 挂 15 分钟后「每天8点吃药」再也不会响）；
  ② `schedule_task` 缺过去时间守卫且不推导重复（`magic-reminder.py`：「刚才8点」先回"已创建"再回"过期未送达"，docstring 承诺的"每天22点→每天重复"实现只建一次性任务）；
  ③ 重启恢复被静音延后的提醒时 KeyError 中断**整个**恢复循环（`app/reminders.py`：`set_reminder_retry_after` pop 掉 `claim_started_at` 但保留 delivering 状态，restore 无默认值 pop 崩掉，排在后面的提醒全轮不到恢复）；
  ④ 循环提醒的"（每天重复）"确认语被 `_clean_for_tts` 括号规则整段剥掉——用户永远听不出这是每天重复的。

  **Round 37 设备控制链路**（`tests/test_round37_closed_loop.py`，28 个，修 3 个）：
  ac-control 域历史上**零工具**（`_SERVER_FILE_MAP` 缺该键，且 `@mcp.tool()` 注册卡在 `create_mcp_tools()` 函数体里无人触发——「打开空调」在工具层空转）；
  Tuya `get_ac_status` 缺失导致 ImportError 被静默吞掉；
  MQTT 入口 `session_id="mqtt-"+device_id` 不被 `device_key_from_session` 识别（只认 `xiaozhi-` 前缀），房间上下文静默丢失。同时钉住 token 安全：OTA 公开端点只下发设备令牌，管理面强随机 AUTH_TOKEN 绝不经公开端点下发。

  **Round 38 飞书入口**（`tests/test_round38_closed_loop.py`，16 个，修 4 个）：
  webhook 路径漏了长连接路径验证过的全部机制——无消息去重（事件订阅至少一次投递，重推 → 用户问一句收两条回复）、无 per-chat 串行化（并发事件乱序写同一会话历史）、长回复不分段（>3500 字符直接发送失败，token 被挤掉不刷新重试）、大脑失败回"（无回复）"（无"正在思考…"占位也无失败删除）。

  **Round 39 打断与异常降级**（`tests/test_round39_closed_loop.py`，20 个，修 2 个）：
  ① **TTS 熔断阈值形同虚设**（`agent/asr_tts.py`）：一次全 provider 瞬时失败就武装 120 秒全局冷却——`TTS_FAILURE_THRESHOLD`（默认 3）此前只出现在 `tts_status()` 里，用户遇到一次 baidu 偶发 500 后两分钟内所有语音回复都合成不出音频。改为连续失败达到阈值才熔断，未达阈值照常抛本次异常、但不毒化后续请求；
  ② **工作记忆轮次漏计**（`agent/llm.py`）：`_already_incremented` 的猜测式计数对"闲聊通道无产出回退大脑"和"无关键词但 LLM 分类到工具域"两条路径都漏（turn 增量实际发生在 `_brain_llm`，inline 大脑路径不经过它）——turn_count 驱动指代消解提示的展示阈值，纯闲聊会话里轮次永远停在低位。改为显式 `_wm_incremented` 跟踪。

  **Round 40 记忆召回**（`tests/test_round40_closed_loop.py`，9 个，修 1 个）：
  `forget()`/`correct_memory()` 只清理 JSON、不动 SQLite——用户说"忘掉我喜欢吃辣"后，下一轮系统提示仍从 SQLite 近期事件把旧记忆召回进来（嘴上说忘了，提示里还是旧的）。已在 magic-memory 补双写同步（memory_store 新增 `delete_events_matching`/`update_events_matching`）。

  **跨轮同源 bug（Round 36 B1 与 Round 40 scope 外发现的同一个根因，统一修复，`agent/llm.py`）**：
  语音派发链路的后台线程全部没做 `contextvars.copy_context()`——`_brain_run_iter` 的 worker 线程和 7 处记忆提取裸线程里 `current_binding()` 恒为 None，多用户部署（FR-14）下**所有用户的提醒 owner、对话记忆都落进 default 分片**（提醒广播到全屋设备）。入口层 `app/xiaozhi_ws.py` 的 brain 派发早已复制上下文，但裸 `threading.Thread` 不跨线程继承 ContextVar，更下层的工具执行与记忆写入全丢用户身份。已给 worker 线程包 `copy_context()`，记忆提取统一走新 helper `_spawn_memory_thread()`。

  五轮合计新增闭环用例 **94 个**（21+28+16+20+9）。全量：**2367 用例（2318 passed / 49 skipped / 0 failed / 0 error）**，0 回归。

  **闭环报出的三个 scope 外项：决策并修复**（`tests/test_scope_out_fixes.py`，6 个）：
  ① 「打开客厅灯」这类最自然的说法关键词表不命中（ac-control 只认"开灯/关灯"子串，自然说法全漏、只能掉 LLM 兜底——而 round34 的事故链正是意图分类偶发失败让句子掉出工具域）。**决策：补词**——关键词集加裸词"灯"（与"空调/电视/风扇"同为设备名词，家庭场景里"灯"基本等于智能灯），快路径覆盖全部自然说法；含「和」的复合句仍降级 LLM（一句话控制多设备）。
  ② `_load_history()` 在生产启动路径无调用点——shutdown 处 `_save_history` 对称地存了却从不读：重启后 `_sessions` 全空、`_get_history` 对缺失会话只建空列表，大脑 prompt 一条上文都不带。**决策：补调用**——`voice_server` lifespan 启动段恢复（只恢复主用户分片：`_sessions` 以 session_id 为键不区分 uid，多分片逐个加载会互相覆盖，属 file-per-uid 设计层面的重构）。
  ③ `_build_system_msg_dynamic` 调 `get_recent_user_message()` 恒取 default 会话——MQTT/飞书/多会话入口的记忆检索 query 是另一个会话的话题，召回全偏。**决策：传参 + 回落**——两个注入点（magic-memory、SQLite）都传 session_id + uid；函数内存为空（进程刚重启、会话未恢复）时回落该用户自己的磁盘分片，显式 uid 优先于绑定解析（丢 ContextVar 的链路里绑定可能不是本人，FR-14 隔离）。

- **修复：真机日志三连——方括号标记漏进播报（P0）、"极简"风格不生效、意图分类被思维链饿死（P1）**。

  **背景**（2026-09-19 真机物理验证后的 `logs/app.log`，19:20–06:56 共 67 轮
  对话）：三处问题叠加在同一条链路上——用户说"打开莲花弄下来。"（ASR 把
  "导航"听成"莲花弄"类口误），意图分类因为思维链把 max_tokens 吃光而静默
  变 none，句子失去全部 MCP 工具，lite 闲聊通道兜底时把 system prompt 的
  汇报模板方括号当内容念了出来，设备开口："[导航已启动] 目的地：莲花弄。"

  **修复 1（P0，双层防御纵深）**：`agent/asr_tts.py` 的 `_clean_for_tts`
  新增 `_TTS_BRACKET_RE`，剥离 `[...]`/`【...】` 状态标记（必须在链接/图片
  规则**之后**执行，否则 `[文字](url)` 的文字段先被吃掉）；顺带发现并修复
  图片规则排序 bug——`_TTS_IMAGE_RE` 原在链接规则之后，`![alt](url)` 先被
  链接规则简化成 `!alt` 漏进播报。`agent/system_msg.py` 的
  `_replace_system_msg_for_channel` 语音约束新增"禁止任何方括号标记"，
  lite 通道与主通道的 `<<<VOICE_RULE>>>` 替换都覆盖。

  **修复 2（P1）**：`agent/llm.py` 的 `_get_max_sentences` 在
  `max_sentences` 缺省时按 `response_style` 兜底（极简/简洁→2、精简→3）。
  真机复现：用户 19:35 设了极简，之后几轮回复仍是 4~6 句——set_pref 只写
  response_style、不写 max_sentences，旧行为返回 0（无限），提示词层的
  "最多2句"模型并不严格遵守，必须服务端硬截断。

  **修复 2 补丁（闭环测试发现的两个漏洞，句数上限原本形同虚设）**：
  ① 大脑路径的句数上限达成后 break 出句子循环，但循环外的**尾部 flush**
  会把剩余文本合成一段继续播（闭环复现：极简下仍念完 5 句——前 2 句单独
  播、后 3 句合并播）。现在上限达成时跳过 flush。
  ② **lite 闲聊通道根本没有句数上限**——只在大脑路径实现了硬截断，
  用户走闲聊通道时上限就失效（真机 4~6 句的回复很可能正是从这条路漏的）。
  现在两个通道共用 `_get_max_sentences()`，上限达成即停止读流 + 跳过
  flush。

  **修复 3（P1）**：`_classify_intent` 在"content 空 + finish_reason=length"
  饿死检测触发时，放大预算（512→`_INTENT_MAX_TOKENS_ON_STARVATION`=2048）
  重试一次。现场复现：长句思维链跑到 930 字把 512 吃光，已经花掉 ~4s，
  重试一次比直接判 none 便宜得多——none 会让关键词没命中的句子全丢 MCP
  工具；两次都饿死仍返回 none 且不写缓存（污染 TTL 会持续生效）。
  重试只发生在故障路径，正常分类仍是一次请求。

  **回归测试**（`tests/test_round34_log_driven.py`，19 个 + `test_round23_ux`
  更新 1 个/新增 1 个）：方括号剥离/链接保留/图片删除、纯标记清洗为空、
  风格兜底上限（极简=2/精简=3/显式值优先/未知风格=0）、饿死重试（预算
  放大、成功写缓存、双饿死不写缓存、非空截断不重试、正常路径只发一次）、
  语音约束对方括号的禁止、lite 系统消息带 VOICE_RULE 占位符。

  **用户闭环测试**（`tests/test_round34_closed_loop.py`，11 个）：跑真实
  用户链路（ASR 文本 → 真 `_classify_intent` → lite/大脑 → 切句 → 清洗 →
  播报句子），外部世界（LLM HTTP、brain 实例、evolution 文件）全 mock：
  事故链端到端（饿死→重试→amap-maps→大脑有工具→播报无方括号、语音约束
  进系统提示、关键词短路不被破坏）、极简上限端到端（大脑+闲聊两个通道
  各一正一反对照）、双饿死优雅降级回 lite 且不污染缓存、lite 播报清洗 +
  提示词层方括号禁令。本文件的用例**发现并钉住了**修复 2 的两个漏洞。
  全量：2256 passed / 49 skipped（基线 2225）。

- **第二轮用户闭环测试（`tests/test_round35_closed_loop.py`，11 个）：飞书文本通道泄漏方括号模板标记（P0，已修复）**。

  第一轮闭环跑的是事故链（ASR 口误 → 意图饿死 → lite 兜底念出方括号），
  本轮盯**正常使用**链路：多轮指代、偏好设置后生效、打断回复、会话隔离、
  飞书文本通道、完整语音链路。与第一轮互补：历史/工作记忆/动态 prompt/
  句数上限全部用真实代码，只 mock 外部世界（LLM HTTP、天气上下文、缓存）。

  **发现并修复**：飞书通道为了保留 markdown 排版整段跳过 `_clean_for_tts`，
  于是 system prompt 汇报模板的 `[状况/结果]` 噪音原样发给飞书用户——
  语音链路有修复 1 的 `_TTS_BRACKET_RE`，飞书链路之前完全没管。现在飞书
  通道只剥方括号（markdown 照留），与大脑路径 `_maybe_clean` 同一约定。

  **钉住的正常链路**（11 个全绿）：多轮（第二轮带第一轮上下文、工作记忆
  指代提示真注入）、偏好闭环（`learn_from_history` 真执行 → evolution 落盘
  → 下一轮 5 句截 2 句 → 风格进系统提示）、打断回复（进提示不进历史）、
  会话隔离（两会话互不串）、飞书（markdown 保留 + 方括号剥离 + lite 通道
  同约定）、语音链路（清洗后文本真到 TTS）。

  另修两处测试脚手架 bug（都是"单独跑过、接龙跑挂"的跨文件污染）：
  ① `agent/system_msg.py` 的 `DATA_DIR` 是 import 时捕获的（与 persona/
  preferences 同族），fixture 的 `monkeypatch.setenv` 改不到它，"风格进
  系统提示"的断言永远看不到极简——按仓库惯例直接 `setattr` 模块属性。
  ② 历史隔离原来用 `_sessions[sid] = []` 重新绑定，会静默断开
  `agent/history.py` 模块级 `_history` 与 `_sessions["default"]` 的
  **同一列表对象**别名（voice_agent._history 经 __getattr__ 动态映射），
  接在后面的 test_voice_agent TestHistory 三个用例全红——改为原地
  `.clear()` + teardown 恢复快照内容。

  全量：2267 passed / 49 skipped（基线 2256；新增 11 个闭环用例，0 回归）。

  全量回归说明：`tests/test_round6_coverage.py` 因 site-packages 里
  `tests` 包（某 ML 依赖 8 月装的）遮蔽了 charlie/tests 命名空间包，
  `from tests.test_xiaozhi_ws import ...` 在**本机环境**必然 ImportError
  （该文件未被本轮改动，属环境既有问题），本轮回归用 `--ignore` 跳过。

- **修复：2.2.0 升级后黑屏 + 死机——手工 lc-s3 板引脚全错（P0）**。

  **现象**（真机复现 2026-09-18）：设备 OTA 到 2.2.0 后黑屏、串口看门狗
  每 10 秒踢一次，audio_detection 卡死在 WakeNet9 卷积里。

  **根因**：手工建的 lc-s3 板型是照 genjutech-s3-1.54tft / esp-sparkbot 抄的，
  **引脚全错**——I2S 在 GPIO 10-14（实际 1-5）、I2C 在 8/9（实际 6/7）、
  PA 在 21（实际 8）、显示屏在 2-7（实际 DC=10/CS=13/CLK=14/MOSI=15/RST=16）。
  错误引脚上初始化音频 → ES8311 在 I2C 0x30 上 NACK（`Open fail`）→
  编解码器死掉；错误引脚上初始化 SPI 屏 → 黑屏；喂不进去音频又触发
  WakeNet9 卷积自旋 → 看门狗。另有 SparkBot 编解码器类（延迟关功放）和
  power_manager 的 ADC 配置也是抄错的。

  **出厂固件源码溯源**：出场 2.1.0 固件（2026-01-30 编译，ESP-IDF v5.5.1）
  的板型源码 = GitHub PR #1360（hairdawngd/xiaozhi-esp32 分支
  feature/add-lc-s3-154-board，ec1a6975，2025-11-21 最后提交）。出厂二进制
  里的 `self.light.*` 工具、`ADC value: %d average...` 日志、
  `battery_check_timer` 字符串与下载的源码逐一比对吻合；PR #1930
  （aredridel，2026-04-12）晚于出厂编译日期，排除。

  **修复**（固件 `main/boards/lc-s3-wifi-1.54tft/`，三文件整包替换为出厂版
  源码并适配 v2.1.0 API）：`config.h` 改用出厂引脚（I2S 1-5、I2C 6/7、
  PA 8、屏 10/13/14/15/16、背光 21、继电器灯 17）；`.cc` 改用
  `Es8311AudioCodec` + SPI mode 0 / 40MHz + 继电器灯 MCP 工具；启动键
  用 v2.1.0 的 `EnterWifiConfigMode()`；`power_manager.h` 改用出厂
  ADC1_CH8 电量映射。`main/Kconfig.projbuild` 的 `CONFIG_OTA_URL` 默认值
  改为 `http://192.168.1.12:8000/xiaozhi/ota`——此前默认指向官方
  api.tenclass.net，设备会拉官方 OTA 响应把 NVS mqtt 配置污染成官方服务器。
  固件重建为 2.2.0（`dist/firmware/xiaozhi-lc-s3-2.2.0.bin`，2818496 字节，
  替换旧坏固件，同版本号不会被服务端重复 OTA）。

  **验证**（USB 写 ota_1 @ 0x410000，保留 nvs/phy_init/assets/ota_0）：
  `Es8311AudioCodec initialized` + `Open codec device OK`、LVGL 任务启动、
  背光 70、电量 100%；OTA 拉 Charlie（`Current is the latest version`）、
  MQTT 连 `192.168.1.12:1883` 并 `Subscribed to topic:
  charlie/esp32/esp32-default/down`；`Auto listen on boot: opening audio
  channel` → 会话建立 → UDP 音频帧持续流入服务端（3 分钟稳定，
  帧 #2910，无看门狗/panic/重启）。

- **修复：设备连 MQTT 永远"等待响应超时"——固件 MQTT 只发不收（P0）**。

  **现象**（真机复现 2026-09-18 21:14）：设备重启后屏幕停在"等待响应超时"。
  日志链：OTA 轮询正常（服务端下发了 mqtt 段）→ 设备按 2.1.0 的
  `InitializeProtocol` 选了 MqttProtocol → hello 发出后 10 秒超时
  （`OpenAudioChannel` 等 `MQTT_PROTOCOL_SERVER_HELLO_EVENT` 不到位，
  `SetError(SERVER_TIMEOUT)`，即 lang_config.h 里的"等待响应超时"）→ 掉线。

  **根因**：全固件源码 grep 证实——`MqttProtocol` **从未订阅任何 topic**：
  `mqtt_protocol.h` 只有 `publish_topic_` 成员，`EspMqtt::Subscribe` 存在但
  零调用。设备把 hello 发到 up topic 后，服务端的响应发在 down topic，设备
  谁都没订阅，永远收不到。（服务端此前"固件只监听 publish_topic"的注释是
  错误假设——它谁都不听。）

  **修复**（固件 `main/protocols/mqtt_protocol.{h,cc}`）：新增
  `subscribe_topic_` 成员，`StartMqttClient` 从 NVS "mqtt" 命名空间读
  `subscribe_topic`（OTA 响应本来就下发该字段），在 `OnConnected` 回调里
  以 QoS 1 订阅。放在 `OnConnected` 而非 Connect 之后：MQTT 断连后订阅关系
  失效，每次（重）连接都必须重新订阅，`OnConnected` 在每次
  断开→连上跳变都会触发，同时覆盖内部自动重连与 60 秒定时重连两条路径。
  固件重建为 2.2.0（`dist/firmware/xiaozhi-lc-s3-2.2.0.bin`，2817920 字节）。

- **修复：设备 OTA 自升级被 401 挡死——固件下载路径未入免鉴权清单 + OTA URL 未带设备令牌（P0）**。

  **现象**（同上真机日志）：设备 21:13:55 轮询 OTA 拿到 firmware 段（2.2.0）
  后 `GET /xiaozhi/firmware/latest` 返回 401，升级链断在第一步，只能退回
  2.1.0 的 MQTT 路径，撞上上面的订阅 bug。

  **根因**：`/xiaozhi/firmware/latest` 不在 `_DEVICE_OPEN_PATHS` 里，被全局
  Bearer 中间件 401（设备不会带 Authorization header）；且 OTA 响应里的
  `firmware.url` 没有带 query token，路径即使豁免也会被 handler 拒。

  **修复**（`voice_server.py` + `app/routes/reminders.py`）：路径加入
  `_DEVICE_OPEN_PATHS`；`xiaozhi_firmware_latest` handler 内按
  `/ws/xiaozhi` 同策略校验（本机直连放行，否则 hmac 比对
  AUTH_TOKEN 或 XIAOZHI_DEVICE_TOKEN，失败打脱敏 token 警告日志）；
  `_ota_firmware_offer` 在 AUTH_TOKEN 非空时给 URL 追加 `?token={XIAOZHI_DEVICE_TOKEN}`。

  **回归测试**（`tests/test_firmware_ota_auth.py`，12 个）：路径豁免、
  非本机带设备令牌 200、带管理面令牌 200、无令牌/错令牌 401、本机免令牌、
  OTA 关闭 404、镜像缺失 404、OTA URL 内嵌/不内嵌令牌、OTA 完整响应链路。
  另用 LAN IP 实机路径验证：无 token 401 / `?token=device` 200（2817920 字节）
  / 错 token 401。

- **OTA 服务器地址改为设置页可配（`device.ota_url`，消灭设备侧最后一个编译期硬编码）**。

  **背景**：设备轮询 OTA 的地址此前只有编译期 `CONFIG_OTA_URL` 一个来源，
  换服务器（Charlie 换机器、换端口）必须重编译 + 重刷固件；本仓库没有写
  NVS "wifi" ota_url 的通道，固件实际永远走编译默认值。

  **实现**（服务端 + 固件两侧）：`app/runtime_settings.py` 新增 `device.ota_url`
  参数（str，默认空 = 沿用设备已保存的地址），设置页 `/settings` 直接呈现；
  `POST /api/settings` 落盘前校验 http(s) 开头（与 `tts.voice` 白名单同款
  前置校验——畸形 URL 会让设备激活直接失败且报错难定位）；`_ota_device_offer`
  把非空值塞进 OTA 响应 device 段（空串不下发，避免盖掉设备原有值）；固件
  `ota.cc` 的 `GetCheckVersionUrl()` 改为三级优先级：NVS "device" ota_url
  （本特性下发）> NVS "wifi" ota_url（兼容旧版写入）> `CONFIG_OTA_URL`，
  空串一律按未设置处理。设置页改一次、设备重启即生效，不用重刷。

  **注意（鸡生蛋）**：服务器搬家必须在设备还能连上旧服务器时提前下发新
  ota_url；服务器已经搬走的话设备拿不到新地址，只能重新烧录——已写进参数
  help 文案，避免搬家时踩坑。

  **回归测试**（`tests/test_runtime_settings.py`，11 个）：参数表登记与
  `GET /api/settings` schema 暴露、http/https 合法值接受、非法 scheme
  落盘前被拒（不写 overrides）、空串接受、下发段空值省略/非空带值/
  关闭返回 None。

- **修复：MQTT+UDP 音频链路两侧加密 nonce 对不上 ⇒ 设备升级 2.2.0 后上行全丢、下行出噪声（P0）**。

  **现象**（首次真机验证 MQTT 路径前排查发现）：固件 `mqtt_protocol.cc` 与
  `mqtt_server.py` 的 UDP 音频包加解密约定不一致，双向都不通：

  **下行（服务端→设备）**：固件 `OnMessage` 解密时**直接拿包头前 16 字节当
  AES-CTR nonce**（`auto nonce = (uint8_t*)data.data()`，并检查
  `data[0] == 0x01`）；而服务端 `_build_audio_packet` 用 base nonce 打补丁
  （`[2:4]=payload_len, [8:12]=ts, [12:16]=seq`）派生 nonce 加密——base nonce
  的 `[0:2]/[4:8]` 是随机字节，和包头头的 `0x01/0x00/ssrc` 不一致 ⇒ 设备解出
  噪声，TTS 播出沙沙声。

  **上行（设备→服务端）**：固件 `SendAudio` 的上行包格式是
  `[nonce(16)][密文]`，nonce 首字节是 base nonce 的随机字节；而服务端
  `_udp_recv_loop` 两处（自动匹配分支 + 已知设备分支）都要求
  `data[0] == 0x01`（按下行包头格式校验）⇒ 设备每包都被判非法丢弃，
  自动匹配永远不触发，ASR 收不到任何语音。

  **修复**（`app/mqtt_server.py`）：
  1. `_build_audio_packet` 去掉无用的 `aes_nonce` 参数，加密 nonce 改为
     **包头本身**（先构造 16 字节 header，再用 `bytes(header)` 作 nonce 加密），
     与固件 `data[0:16]` 解密完全对齐；`_send_audio_packets` 及三个调用点
     同步简化。
  2. 删除 `_udp_recv_loop` 里两处 `data[0] != 0x01` 校验（固件上行首字节是
     随机 nonce，长度校验 `len(data)==16+plen` 已挡住随机噪声，opus 试解码
     仍是真实验证）。

  **回归测试**（`tests/test_mqtt_server.py`）：新增 `TestFirmwareUdpRoundTrip`
  按固件 C 代码逐行模拟双向往返（下行：`data[0]==0x01` 检查 + 包头当 nonce
  解密；上行：nonce 打补丁开头加密 + 服务端重建 nonce 解密），并保留
  `test_old_base_nonce_derivation_fails` 防止退回旧加密方式。原有测试把
  `_build_audio_packet`（下行构造器）误用于构造上行包，已改为固件格式
  （首字节非 0x01）。**Live 验证**：固件格式上行包（首字节 0xd9）在真服务
  上完成 hello 握手 → 自动匹配 → 解密 → opus 验证全链路。

- **修复：意图分类被 reasoning 思维链"饿死" ⇒ 所有 LLM 兜底分类静默变 `none`、工具全废（P0，比 lite 那条更严重）**。

  **现象**：`logs/app.log` 里 `[intent] LLM分类完成: … → none (raw)` 的 raw 那一格，
  在 **2026-09-17 20:42:43 服务重启**之后**一直是空的**（重启前是 `(none)`）：

  ```
  2026-09-17 20:37:41 [intent] LLM分类完成: 1.266s | '这看我了。' → none (none)   ← 重启前
  2026-09-17 20:42:45 [intent] LLM分类完成: 1.492s | '你好'      → none ()       ← 重启后
  2026-09-18 07:45:23 [intent] LLM分类完成: 0.708s | '帽子他まで。' → none ()
  ```

  那一刻正是 `.env` 把 `LLM_PRIORITY` 改成 `stepfun,agnes,sagnes` 生效的时刻 ——
  而启动日志自己就印着「StepFun…注意: **LLM 的 reasoning 无法禁用**」。

  **根因**：分类请求只要一个类别名，`max_tokens=4`；而 `step-3.5-flash-2603` 的
  **思维链与正文共享这份预算** ⇒ 思维链 7 个字就把 4 个 token 吃光 ⇒ `content` 恒为空
  （实测 **5/5**，`finish_reason=length`）⇒ `_normalize_intent("")` 走 `else` 兜底成
  `"none"`，**并且这个错的 `none` 还会被写进意图缓存**（TTL 内一直生效）。

  **后果**：关键词预判没命中、需要 LLM 兜底分类的句子**全部静默失去工具能力**
  （`none` ⇒ 走 lite 闲聊通道 ⇒ 零 MCP）。实测修复前 `帮我点杯咖啡` /
  `帮我查一下去西二旗怎么坐地铁` / `明天上午提醒我交电费` 全被判成 `none`；
  修复后分别是 `baize-skills` / `amap-maps` / `magic-reminder`。

  **修复**（`agent/llm.py`）：新增 `_intent_request_params(base)` —— 按端点给出
  `(max_tokens, reasoning_effort)`：reasoning 端点（现清单 `stepfun`）拿
  `512 + reasoning_effort=minimal`，其余端点仍是 `4`。

  - **饿死检测**：`content` 空且 `finish_reason=length` ⇒ 打明确告警，且**不写缓存**
    （否则错的 `none` 在 TTL 内持续生效，端点修好了也一样）
  - 删掉死变量 `_max_tokens = 2048`（只赋值从不读取，是本次误判的帮凶）

  ⚠️ **中途走过一个弯路，记录在此以免重犯**：第一版把分类**切到 agnes**
  （非 reasoning 端点），依据是"stepfun 5/5 空正文"。但那个对比**只测了
  `max_tokens=4` 的 stepfun**，没测给足预算的 stepfun。补齐同机各 12 次实测后：

  | 方案 | 中位 | p90 | **max** | 空产出 | 一致性 |
  |---|---|---|---|---|---|
  | agnes(mt=4) | 0.23s | 0.48s | **4.90s**（另一次 7.74s） | 0/12 | 7 个输入里 2 个三次不同答案 |
  | stepfun(mt=512, minimal) | 0.82s | 0.89s | **1.08s** | 0/12 | 7/7 一致 |

  分类在每句话的热路径上、而端到端首句预算只有 8s ⇒ 选型要看**上界**不是中位。
  agnes 那 4.9–7.7s 的长尾会**再造一次死链**。所以最终方案是**留在活动端点上
  给它够用的预算**，不换端点。真机复核（真实 `.env`）准确率也更好：
  `帮我点杯咖啡 → baize-skills`（agnes 三次给了 `magic-recipe`/`magic-reminder`/
  `magic-browser` 三个不同答案）。

  **验证**：新增 `tests/test_intent_reasoning_starvation.py`（**13 用例**），
  **变异 10/10 KILLED**；`TestDecisionGuard` 专门钉住"不许把分类切走换端点"这个
  决策（附实测表格），防止有人照着被否掉的方案改回去。
  全量回归 **2,195 通过 / 0 失败 / 0 错误 / 49 跳过**（258s）

- **修复：ASR 兜底纠错 `_llm_correction` 被 reasoning 思维链"饿死" ⇒ 开启后静默零产出（同根因，休眠态已修）**。

  **根因**：`agent/asr_correction.py::_llm_correction` 走 `active_chat_endpoint()`（= stepfun =
  reasoning 模型）且写死 `max_tokens=64`。reasoning 模型的**思维链与正文共享预算**，长句实测
  `max_tokens=64/256/512` **全部空产出**（思维链随预算增长 99→382→767 字），
  `finish_reason=length`、正文 0 字 ⇒ 空 content 被静默兜底成"返回原文、不报错"
  ⇒ `ASR_LLM_CORRECTION=1` 开启后纠错功能等于没开。

  **与意图分类那条的区别**：本条**默认关闭**（`ASR_LLM_CORRECTION` 默认 `0`），当前零影响；
  且"返回原文"是安全兜底（不会像意图分类那样静默禁用工具）。属同根因、但休眠中的第三处。

  **修复**（`agent/asr_correction.py`）：`max_tokens` 64→512（非 reasoning 端点也无害）；
  stepfun 端点额外加 `reasoning_effort="minimal"` 压思维链（agnes 不认该字段、带上去 400，
  故只按 `base` 条件加，与意图分类同一守卫模式）。

  **验证**：新增 `tests/test_asr_correction_reasoning_starvation.py`（**4 用例**：stepfun 端点
  的 payload 断言 / 非 stepfun 端点不带该字段 / 空产出优雅回退原文 / 静态守卫 `max_tokens=64`
  不再出现）。**本轮只读扫查 + 此修复，全量回归仍为 2,195 / 0 / 0 / 49**。

- **修复：闲聊快通道被 reasoning 思维链"饿死" ⇒ 端到端死链（2026-09-18 现场真 bug）**。

  现场日志（07:45，一条 ASR 听错的输入 `帽子他まで。`）：

  ```
  22.551 [intent] LLM分类开始: text='帽子他まで。'
  23.258 [intent] LLM分类完成: 0.708s → none
  23.270 [lite] LITE_PROVIDER=stepfun 覆盖闲聊通道端点: model=step-3.5-flash-2603
  26.566 [lite] 闲聊通道无产出，回退完整大脑        ← 白转 3.3s
  30.533 [xiaozhi] 首句超时(>8s)，视为死链          ← 8.0s 到点
  ```

  **根因链条**（已离线复现，3/3）：
  1. `.env` 的 `LITE_PROVIDER=stepfun` 把闲聊快通道指向 `step-3.5-flash-2603` —— **reasoning 模型**
  2. reasoning 模型的**思维链与正文共享 `max_tokens`**（lite 是 300）。遇到"听不懂"的输入，
     思维链把预算吃光 ⇒ `finish_reason=length`、正文 **0 字**、耗时 3.64s
  3. lite 静默 yield 空 ⇒ 回退完整大脑；但**回退预算已被吃光**：`_brain_t0` 在生成器首次
     `next` **之前**设置（`app/xiaozhi_ws.py:862`），8s 预算里已包含「分类 0.7s + lite
     白转 3.6s = 4.3s」⇒ 只剩 3.7s，不够出首句 ⇒ 死链

  ⚠️ **"首字节"是误导性的选型指标**：当初选 stepfun 的依据是"首字节 0.37–0.69s"，但
  reasoning 模型的**第一个 SSE chunk 是思维链而不是正文** —— 首字节快 ≠ 首 content 快。

  修复：
  - `charlie/.env`：去掉 `LITE_PROVIDER=stepfun`（留空 = 走代码默认的 agnes），就地写明原因
  - `agent/llm.py`：
    - 无产出时**按 `finish_reason` 分开记录**成因 —— `=length` 报"思维链吃光 max_tokens"
      并打出 reasoning 字数，否则报"对端结束但零正文"。旧实现只有一句"闲聊通道无产出"，
      现场根本看不出是"对端没回话"还是"预算被思维链吃光"，而后者**必然**死链
    - delta 解析同时统计 `reasoning_content` 长度；**思维链绝不进正文**（否则会把思考过程念出来）
    - `LITE_MAX_TOKENS` 开放为 env（默认 300 不变）
    - stepfun 端点补 `reasoning_effort=minimal`（reasoning 466→280 字，正文 2.30s 出且
      `finish_reason=stop`）。⚠️ 这是"万一被指回来"的兜底，**不是推荐配置** ——
      实测 `thinking:disabled` / `enable_thinking:False` / `chat_template_kwargs` 全都无效
    - **修掉一处反向建议**：超时倒挂提示原写着"闲聊通道走首字节 <1s 的 provider，如
      stepfun"，下一个人照做就会复现本次故障；改为"非 reasoning 且首 content 快（如 agnes）"
    - 明确 `_LITE_FIRST_CONTENT_TIMEOUT` **不要**为"给回退留余量"而收紧到 3–4s（8.0 是有意
      从 4.5 放宽的，钉在 `tests/test_round10_llm.py`；真正该修的是"别让 lite 白转"）

  实测对比（同机各 3 次）：

  | provider | `帽子他まで。` | `你好` | `那个东西放哪儿了来着` |
  |---|---|---|---|
  | agnes-2.5-flash（代码默认） | 0.55s · **0/3 空** | 0.37s · 0/3 | 0.31s · 0/3 |
  | stepfun 3.5-flash（`.env` 覆盖） | **3/3 全空** | 0.85s | 1.34s |

  **补测（2026-09-18 稍后，修正上面对比的方法论缺陷）**：上面的 stepfun 是在
  `max_tokens=300` **被饿死**的状态下测的。改成"两边都给正常工作配置"重测
  （流式计时到首 content，各 10 次），**结论不变、理由更硬**：

  | 配置 | 首 content 中位 | p90 | max | 空产出 |
  |---|---|---|---|---|
  | agnes(mt=300) | 0.29s | 0.50s | **0.50s** | 0/10 |
  | stepfun(mt=512, reasoning_effort=minimal) | 1.03s | 3.96s | **3.96s** | 0/10 |

  生成式任务上 reasoning 必须先跑完思维链（实测 68–543 字 = 0.7–3.7s 静默）才开始
  吐字，天然不适合"首字要快"的闲聊通道。⚠️ **这不能推广到约束式任务** ——
  意图分类（只输出一个类别名）实测反而是 stepfun+minimal 的上界更小（见下一条）。
  **任务类型决定选型**，已写进 `_lite_endpoint()` / `_intent_request_params()` 的 docstring。

  验证：新增 `tests/test_lite_reasoning_starvation.py`（8 用例：无产出诊断 4 / payload 2 /
  配置守卫 2），**变异 8/8 KILLED**；其中一条是 `.env` 守卫 —— 一旦有人把闲聊快通道指回
  reasoning provider 就直接失败（当前按设计跳过，因为配置正确）。
  全量回归 **2,182 通过 / 0 失败 / 0 错误 / 49 跳过**（258s）

  行为变更：`.env` 一行（闲聊快通道回到 agnes 默认）；代码侧只加日志与诊断字段，无逻辑分支变化

- **FR-13 方案已确认（A′），并登记 Realtime 开关**。老于 2026-09-18 拍板三件事：
  ① 走 **A′**（自有 brain 负责理解与动作，StepFun Realtime 只当**发声通道**；
  取消边界只画在"发声"，绝不画在"动作执行" —— MCP 有不可回滚副作用）；
  ② **`step-tts-2` 保留为默认，Realtime 作为可选加速项**，用独立开关
  `STEPAUDIO3_RT_ENABLED`（默认 `0`）按需打开，**不挂 `TTS_PRIORITY`**；
  ③ 第 2 步剩下的"接线"**等 ESP32 上线时再动**（手感必须真机验证）。

  决策 ② 的落地：`STEPAUDIO3_*` 此前只在 `agent/realtime_tts.py` 里被 `os.getenv`
  读到，**既没进 `app/env_catalog.py`（管理页看不到、界面上开不了），也没进
  `charlie/.env.example`（新部署的人不知道有这东西）**。现已补齐：

  - `app/env_catalog.py`：新增分组 **`tts_realtime`**（"实时语音（Realtime 发声通道，
    可选加速）"，排在 `asr_local` 之后）+ 6 个 `EnvEntry`
    （`STEPAUDIO3_RT_ENABLED` / `RT_URL` / `RT_MODEL` / `VOICE` / `SAMPLE_RATE` /
    `READ_INSTRUCTION`）。全量 `EnvEntry` **99 → 105**，无重名
  - `charlie/.env.example`：新增「StepFun Realtime 发声通道」段，`ENABLED=0` 起步，
    并就地注明两条硬约束（采样率必须 24k、限免到期后如不接受计费请保持关闭）
  - **行为变更**：**零变更**（只是把已有的 env 登记进元数据；`ENABLED` 默认仍是 `0`）
  - **验证**：全量回归 **2,174 通过 / 0 失败 / 0 错误 / 48 跳过**（260s），与改动前一致

- **FR-13 第 2 步地基：pcm16 → Opus 编码通路（不依赖真机）**。
  第 2 步的价值是"**去掉一次 mp3 解码**"（现在 `mp3 → ffmpeg → pcm → Opus`，
  Realtime 直接给 pcm16 24k）。本轮把它的地基做完并验证到位，等设备上线只剩接线。

  - **下行编码收敛到唯一实现**：改造前 `mp3_to_opus_packets` 与 `url_to_opus_packets`
    **各自内联了一份逐字节相同的编码循环**（帧长换算 / offset 步进 / 异常吞掉）。
    再加第三份（给 pcm 用）就是又一次"同一份规则写两遍"——改一处漏一处会产生
    "某条通路帧长不一致"这种极难查的音频问题。
    - 新增 `app/xiaozhi_codec.py::pcm_to_opus_packets()` —— 唯一的批处理实现
    - 新增 `PcmOpusStreamEncoder` —— 流式编码器：喂任意长度分片（不必按帧对齐）、
      凑满即吐、`flush()` 补静音成整帧
    - 两条老通路改为委托；**AST 守卫**钉住"全模块只有两处 `opuslib.Encoder`
      创建点（批处理 + 流式编码器）"
    - `pad_tail` 取舍：批处理默认**丢尾巴**（与改造前逐字节一致，帧数被别的测试
      断言着）；流式 `flush()` **必须补** —— 那是真实音频，丢掉就是最多 60ms 的截断
  - **新增 `agent/realtime_tts.py::stream_sentence_opus()`** —— 把"Realtime 的流式 pcm"
    与"下行 Opus 帧"接起来，调用方只需决定"怎么发帧"。⚠️ 不能"收齐 pcm 再编码"：
    Realtime 的全部价值就是首音频 305ms，等整句收齐就退化成 `305ms + 整句生成时间`
    （长句十几秒），等于白接。两个刻意设计：
    - **采样率不匹配直接拒绝**（`ok=False`）。源 24k / 下行 24k ⇒ 不需要重采样；
      一旦 `STEPAUDIO3_SAMPLE_RATE` 被改错，宁可降级到 mp3 ——
      发错采样率的音频设备会变调/变速，而且**不报任何错**
    - **被取消时不补尾巴** —— 用户已经在说话了，多吐 60ms 只会推迟"停"
    - `on_opus` 必须快速返回（在 Realtime 的 recv 循环里同步调用），
      真实链路应把帧丢进队列由独立 pacing 任务发送
  - **首音频打点**：`[xiaozhi] latency: tts_first_audio=…ms 端到端=…ms` ——
    拆成"句首→首帧"（本地转码开销）与"端到端"（含 ASR/brain/TTS 合成），
    退化时能直接定位是"模型慢了"还是"本地转码慢了"
  - **验证**：新增 `tests/test_fr13_pcm_opus.py`（**18 用例**：批处理 4 /
    流式编码器 7 / 唯一实现守卫 2 / 适配层 5），含真 opuslib 往返（能量偏离 < 30%）；
    **变异 8/8 KILLED**；全量回归 **2,174 通过 / 0 失败 / 0 错误 / 48 跳过**
  - **行为变更**：**零变更**。`pcm_to_opus_packets` / `PcmOpusStreamEncoder` /
    `stream_sentence_opus` 都是新增导出，尚未接线；两条老通路的输出与改造前逐字节一致。
  - **⚠️ 剩下的"接线"不是改一个函数，是加第二个消费者**：现有队列的形状是
    `(句子, 完整 mp3)`（生产者线程在消费者播上一句时把整句合成好，掩盖句间欠载），
    而 Realtime 必须边收 pcm 边发帧，装不进这个形状。要做的是：队列载荷打标签 +
    生产者侧 opt-in 时跳过 `_synth_mp3` + 消费者侧新增"边收边发"分支。
    **这段的手感（pacing / 缓冲 / 停顿时长）必须连真 ESP32 才能验**，
    设备不在线时改它等于盲改，改坏了是"音频断断续续"这种本地抓不到的症状。

- **FR-13 实时打断：修掉「打断掐不断」的两个真 bug（不需要接 Realtime 即生效）**。
  老于的诉求原文是「**任何**播报过程中开口都能打断」。把这句话拆成两条通路，各有一个
  真 bug；两条都在现有链路上，与"用哪个 TTS"无关。

  - **真 bug ①：唤醒词打断时设备收不到 `tts/stop`**。ESP32 侧已经收到并解码进本地
    队列的 Opus 帧会继续播几十到几百毫秒 —— 用户听到的就是"掐不断 / 反应特别慢"。
    根因是「取消 = 通知设备停播」这条不变量是**散的**：11 个 `cancel_stream()` 调用点
    里只有 RMS barge-in 与 `abort` 两处手写了 `tts/stop`，而
    **`listen/detect`（喊"你好小智"，最常用的打断方式）漏了**。
    修法：收进 `cancel_stream()` 统一保证（加 `_was_active` 守卫，无活跃流时不发噪声）；
    顺序关键 —— 先 `await` 完被取消的 `stream_task`，再发
    `{"type":"tts","state":"stop"}`，反过来的话设备会先清缓冲、随后又收到残余帧。
    顺带删掉 barge-in 路径里重复的那条 `tts/stop`。
  - **真 bug ②：提醒 / 主动推送的播报完全在 `cancel_stream()` 管辖之外**。
    待推送播报是 `app/state.py::_do_flush_xiaozhi_pending` 里**另一个 asyncio 任务**，
    而 `cancel_stream()` 只认 `stream_tts_reply` 的 `stream_task` ⇒ 提醒念到一半用户
    开口**掐不掉**，同时有对话流时还会两路音频交错；RMS barge-in 的触发条件也只看
    `stream_task`，所以"提醒在播 + 用户直接开口"根本不触发打断。
    修法：
    - `app/state.py` 新增播报登记表 `_xiaozhi_push_active`（按 `session_id`）+
      `cancel_xiaozhi_push()` / `push_playback_active()`；所有播报启动一律走
      `_spawn_push_playback()`（登记过才掐得掉）
    - `cancel_stream()` 同时掐它，并 `await` 其**正常退出**后才发 `tts/stop`
    - 取消检查落在**逐帧循环内部**（只在条目之间检查的话，一条 15 秒的提醒要念完才停）
    - RMS barge-in 触发条件加上 `or push_playback_active(session_id)`
    - ⚠️ **取消是"置标志"而不是 `task.cancel()`**：待推送项已被
      `_take_pending_for_flush()` 从队列取走，硬取消会让"还没开口"的那几条
      **永久丢失**（既不播也不补发，日志里只有一句"被打断"）。置标志让任务走正常
      退出路径，由它自己决定哪些项可重试
    - ⚠️ 实现过程中测试抓出**第三个**真 bug：打断时 `remaining`（还没碰过的剩余项）
      没有放回队列 ⇒ 用户一开口，后面几条提醒永久消失
  - **顺带清掉一份死代码**：`app/xiaozhi_ws.py` 里还留着一份本地 `_flush()` 待推送
    兜底，它靠 `register_xiaozhi_client()` 返回待推送列表触发，而该函数自 FR-14 改造后
    **恒返回 `[]`**（补发逻辑已内聚到 `state.py`）。它是唯一一条不受 `cancel_stream()`
    管辖的播报通路，留着只会误导后来者。已删除，并加 AST 守卫防止复活。
  - **日志**：新增 `[xiaozhi] 打断耗时: 服务端停手→tts/stop=…ms (对话流=… 推送=…)` 与
    `[xiaozhi] barge-in latency: 检测→tts/stop=…ms` —— 拆成两段是为了让退化可定位
    （停流卡住 vs 停播卡住，修法完全不同）。
  - **验证**：新增 `tests/test_fr13_push_interrupt.py`（**13 用例**：取消语义 3 /
    登记表 4 / AST 结构守卫 6）；`tests/test_xiaozhi_ws.py` 新增端到端 1 例
    （唤醒词掐断提醒播报 —— 用"补发帧数 200×60ms=12s 远超 8s collect 窗口"保证
    坏代码**不会**因"播报自然念完发的 stop"而假通过）；**变异 8/8 全杀**
    （含"回到修复前"、去掉逐帧检查、改用硬取消、绕过登记表、只看对话流）。
  - **顺带修掉一个既有 flaky**：`tests/test_xiaozhi_ws.py::_hello()` 把**单次** 2s
    recv 超时当致命错误，而本进程**第一个**连接要在处理循环里惰性加载 Silero VAD
    （冷 page cache 下实测 3s）⇒ 该用例在"自己是本进程第一个连接"时随机失败。
    改成在 10s 总预算内重试，断言强度不变。
  - **行为变更**：默认零变更（无待推送项时登记表为空、不发任何额外消息）。
    `_do_flush_xiaozhi_pending()` 的 `cancel_evt` 是可选关键字参数，老调用方行为不变。

- **FR-14 多人场景 · 批次 6：`device_key` 清洗一致性（真 bug）+ 房间名映射 + 管理页 UI**。
  至此 FR-14 的批次 1/2/3A/3B/4a/4b/5/6 **全部完成**。

  - **真 bug：入口与映射表的 `device_key` 清洗规则不一致 ⇒ 绑定静默失效**。
    `app/xiaozhi_ws.py` 的 hello 解析自带一份正则
    `re.sub(r"[^0-9A-Za-z_-]", "", cand)[:32]`，而映射表的写入侧
    （`agent/device_users.py::_sanitize_device_key`）用的是
    `re.sub(r"[^0-9A-Za-z_:-]", "", key)[:64]`。实测：
    ```
    aa:bb:cc:dd:ee:ff                           入口 'aabbccddeeff'         映射表 'aa:bb:cc:dd:ee:ff'  ❌
    LC-S3-WiFi-1.54TFT-A1B2C3D4E5F6A7B8C9D0E1F2  入口 '…A1B2C3D4E5F6A7'(32) 映射表 '…A1B2C3D4E5F6A7B8C9D0E1'(64) ❌
    ```
    **MAC 带冒号、长 `client_id` 的设备永远查不到映射** ⇒ 回落 `default`，
    而日志只说"设备未绑定用户"，完全看不出是规则漂移。这与批次 4a 的
    `bound_device_user()`（"未绑定 ⇒ 不 bind"）设计直接冲突 —— 用户以为绑好了。
    修法：入口复用 `_sanitize_device_key`（单一事实来源）+ AST 结构守卫
    （入口不得再出现自带正则、必须从 `agent.device_users` 导入该函数）。
  - **房间名映射（方案 §6 遗留）**：`device_users.json` 从一张表扩到三张 ——
    `devices`（设备 → 用户）、`rooms`（设备 → 房间）、`room_users`（房间 → 用户兜底）。
    解析优先级：`devices[device_key]` → `room_users[rooms[device_key]]` → `None`。
    **为什么值得做**：设备标识是固件层面的（重刷固件 / 换板子 / OTA 改名都会变），
    而"这台在客厅"是物理事实。房间绑过人之后，新设备指认到同一房间即可继承身份，
    不必重建整张表。另外 `rooms` 还让管理页把 `aa:bb:cc:dd:ee:ff` 显示成「客厅」，
    并把「当前设备位置：客厅」注入 dynamic system prompt（LLM 才能区分"这个灯"和"客厅的灯"）。
    - ⚠️ **房间名用独立的 `sanitize_room`**：`_sanitize_device_key` 按
      `[^0-9A-Za-z_:-]` 清洗会把「客厅」整串吃掉变成空字符串（静默丢数据）。
    - ⚠️ **`devices_for_user()` 与 `get_device_user()` 严格互逆**：候选集取
      `devices ∪ rooms` 的 key 并集再逐个判定归属。若只按 `rooms` 反查，
      "设备直接绑 A、其房间绑 B"时 A 会同时出现在两个人的设备集合里 ⇒ 提醒推给错的人。
    - ⚠️ **`remove_device_user()` 连房间标记一起清**：只清 `devices` 而留着 `rooms`，
      设备会继续通过房间兜底拿到身份 —— 用户看到的是"删了还在"。
    - 新增 env `CHARLIE_DEVICE_ROOMS`（支持 `{dev: room}` 扁平式与
      `{"rooms":…,"room_users":…}` 结构化两种写法）。
  - **管理页 UI**：`web/manage.html` 新增「设备与用户」tab —— 在线设备（带
    `direct`/`room`/`none` 来源徽标）、已绑定设备、房间兜底绑定、绑定/解绑表单
    （支持 `clear_room`）。动态数据一律走 `data-*` + 事件委托，不拼 `onclick` 字符串。
    顺带把 `showTab` 的按钮高亮从 `textContent.includes(...)` 改为按 `TAB_LABELS`
    **精确匹配**（原来靠子串匹配，多一个 tab 就可能误点亮）。
  - **API**：`GET /api/device-users` 返回三张表 + `source` 徽标；
    `POST /api/device-users` 新增 `room` / `clear_room`；新增 `POST /api/room-users`。
  - **验证**：新增 `tests/test_fr14_batch6_device_room.py`（**101 用例 / 9 类**）；
    **变异 13/13 全杀**（清洗回退、截断 32、房间清洗吃中文、丢房间兜底、房间优先于设备、
    反查漏房间、解绑不清房间、session 前缀不校验、房间段写进 static、写表丢其他表 ×2、
    API 忽略 `clear_room`、前端漏登记 `TAB_LABELS`）。全量回归
    **2,140 通过 / 0 失败 / 0 错误 / 48 跳过**（= 上一档 2,039 + 101，243s）。
  - **行为变更**：**默认零变更**（没有 `device_users.json` ⇒ 三张表全空 ⇒ 无房间段、
    无兜底解析）。只有显式绑定后才有变化。

- **FR-14 多人场景 · 批次 5：角色/音色按 uid 隔离 + 线程池 ContextVar + 会话级切换兼容层**。
  批次 3A 立了一条不变量「static 段与 user 无关」，但**有两处违反了它**，且都是静默串台
  （不报错，只是把 A 的东西给了 B）。本批次把这条不变量真正落地。

  - **真 bug ①：角色/音色跨用户串台**。`agent/roles.py` 把"当前角色"缓存成**进程级单例**
    `_current_role`，而 `preferences_{uid}.json` 自批次 2 起**已按 uid 分片** ——
    进程级缓存把分片又抹平了。实测两个方向的串台：
    ```
    laoyu   -> jarvis  {'voice': 'Nova',  'speed': 0.92}
    jiaren1 -> jarvis  {'voice': 'Nova',  'speed': 0.92}   ← 期望 charlie / Ethan
    ── 进程重启后 ──
    jiaren1 先说话 -> charlie
    laoyu   后说话 -> charlie                              ← 期望 jarvis
    ```
    第二种更阴：**谁先说话谁定全家的角色**，老于自己的设置反而失效。
    修法：`_current_role` 降级为"显式进程级覆盖"（非 None 才优先，保留管理台强制切换的
    后门）；真正的状态改为 `_current_role_by_uid: dict[uid, role]`。
  - **真 bug ②：`set_role_tts_voice()` 改的是共享的 `_ROLES[role_id]["tts_voice"]`** ——
    `_ROLES` 是进程级唯一的一份内置角色表，老于把 charlie 音色改成 Alex，妈妈那边跟着变。
    改为 `_voice_overrides: dict[(uid, role_id), voice]` 分键，内置表永不被用户动作污染
    （用例 `test_roles_table_not_polluted_by_user_actions` 锁死）。
  - **真 bug ③：`_load_persisted_voice_overrides()` 在模块加载时被调用一次** —— 那一刻
    没有任何会话绑定，uid 恒为 env/default，等于把"某个特定用户"的音色覆盖**烤进进程
    状态**。改为每次 `_resolve_role_voice()` 按当前 uid 惰性读取并分键缓存。
  - **真 bug ④：`persona` 人格段拼在 static 段里**。`get_persona_prompt()` /
    `contextual_response_style()` 按 uid 读 `persona_state_{uid}.json`（mood / emotion /
    relationship_level）—— 是**用户态**，却被 `(mcp_set, role)` 缓存（`_SYSTEM_MSG_TTL=300`）
    并写进 `brain.system_message`（brain 本身按 mcp_set 缓存）。于是"谁先说话谁的人格被
    烤进共享 brain"，5 分钟内妈妈拿到老于的心情与亲密度。移到
    `_build_system_msg_dynamic(uid)`（顺带修掉 persona 变化后最长 5 分钟不生效的陈旧问题）。
  - **真 bug ⑤：上下文里的「今日待办」不过滤 uid**。`agent/context.py` 的 60 秒共享缓存
    `_context_cache` 由**后台线程** `_refresh_context_cache()` 填充，而它调的是**不过滤**的
    `_load_reminders()`（全局视图）—— 算出来的 `todos` 是"某一个人"的待办，再被注入到
    **所有**会话的 system message。改为 `todos` 不进共享缓存，由 `_override_todos()` 在
    **读取时**按会话 uid 现算（走用户面入口 `list_reminders_for(uid)`），与既有的
    `_override_recent()` 同一套路。
  - **`ThreadPoolExecutor` / `run_in_executor` 丢 ContextVar**（潜伏陷阱 + 一处真实缺口）：
    - `agent/inproc_tools.py` 的 `is_async` 分支把工具丢进线程池 → 丢身份 → `resolve_user()`
      静默回落到 env/default。⚠️ 当前**没有任何** magic-* 工具是 `async def`，所以这条
      分支是死路 —— 但谁哪天把一个读用户数据的工具改成 async 就会踩中。显式
      `contextvars.copy_context()` 把路封死
    - `app/xiaozhi_ws.py` 新增 `_ctx_run_in_executor()`：消费侧的兜底语 / 罐头语
      （"抱歉，反应慢了"、"抱歉，音乐加载失败"）在 `_synth_mp3` 里合成，走裸
      `run_in_executor` 会丢掉用户身份 ⇒ **妈妈听到老于的音色**。3 处调用点改用它
    - 主回复路径已由 `_collect_ctx.run(collect)` 覆盖；`asyncio.to_thread` **会**传播上下文
      （CPython ≥3.9 内部 `copy_context`），所以 `conversation.py` / `websocket.py` /
      `feishu.py` 的既有依赖是成立的 —— 这一点本批次做了审计确认
  - **会话级切换兼容层**：`set_current_user(user_id, session_id=None)` 新增可选
    `session_id`。传了 ⇒ 只绑定该会话（`user_context.set_session_user`），**不写
    `os.environ`、不清全局缓存**（响应缓存 key 含 `session_id` + `role`，会话身份变了 key
    自然变；清全局缓存会误伤其他用户）。不传 ⇒ 保持改造前的进程级行为，单用户部署与既有
    API 零改动。`/api/user/switch` 同步支持 `?session_id=`，返回体加 `scope`
    （`session` / `process`），进程级路径会在日志里**告警**。
  - 新增 `charlie/tests/test_fr14_batch5_role_identity.py`（**51 用例**），
    **变异 24/24 全杀**，全量回归 **2,039 通过 / 0 失败 / 0 错误 / 48 跳过**（零回归）。
  - 测试基础设施：`tests/conftest.py` 状态快照新增 `agent.roles._current_role_by_uid` /
    `_voice_overrides` / `agent.persona._states`。⚠️ 不拍快照会表现为「单独跑通过、全量跑
    失败」——persona 段移入 dynamic 后，某个用例改过的亲密度会留给后续用例，让
    `test_fr14_batch3a` 的"动态段为空"用例凭空多出 `🎭 人设风格：…`。
  - ⚠️ **变异测试的两个坑（本批次实测踩到）**：
    1. `_current_role = role_id` 写在函数里**没加 `global`** ⇒ 只是创建**局部变量**，
       对模块全局毫无影响 —— 一个意外等价变异，报 SURVIVED 但代码其实没坏。
       报 SURVIVED 时先问"语义真的不同吗"，不要直接补测试。
    2. `qwen_agent.tools.base.is_tool_schema` 要求 `parameters` 的键集合**恰好**是
       `{type, properties, required}` 且 `required` 是 list —— 测试里构造工具类时传
       `{}` 或只写 `type`+`properties` 都会在实例化时抛 `ValueError`。

- **FR-14 多人场景 · 批次 3B：工具层与数据层的 uid 归属**。
  4a 让入口绑定生效后，批次 2 的文件分片与 3A 的内容隔离才真正跑起来，但**工具层**
  仍按进程级身份找数据 —— 妈妈用语音说"记一下我明天要买牛奶"，落进的是老于的
  `episodic_memories.json`。
  - `magic-memory.py` / `magic-evolution.py` / `magic-habits.py` / `magic-summary.py`：
    uid 统一走 `agent.user_context.resolve_user()`（ContextVar → session 注册表 → env
    兜底），新增 `_user_file()` 按 uid 分片且 `default` 保留原名（**老数据零迁移**）
  - `MEMORY_FILE` / `EVOLUTION_FILE` / `HABITS_FILE` 从**导入期快照**改为 PEP 562
    模块级 `__getattr__`，每次读取按当前 uid 解析。⚠️ 快照会把"谁在用"冻结成进程
    启动那一刻的身份，之后所有用户读写同一个文件
  - `magic-memory.py`：semantic 记忆路径缓存 `_SEMANTIC_FILE = None` **单例** →
    `_SEMANTIC_FILES: dict[uid, path]` 按 uid 分键。单例更隐蔽：第一次调用（进程早期、
    uid 恒为 `default`）就把路径固化，即使 uid 解析改对了隔离照样失效
    - 顺带修掉一个潜在死锁：`_load/_save_semantic_memories` 都在**持有**
      `_semantic_lock` 时调 `_get_semantic_file()`，那里必须用独立锁
      （`_semantic_path_lock`），复用同一把非重入锁会直接自锁
  - `magic-summary.py::_get_daily_memories`：硬编码的 `episodic_memories.json` →
    `_user_file("episodic_memories")`（多用户上线后 `default` 通常是空的，
    表现为"昨日回顾永远是空的"）
  - `agent/llm.py::_get_max_sentences`：`os.environ` → `resolve_user()`，读当前 uid 的
    `evolution_data_{uid}.json`（旧写法下妈妈设了简洁模式，老于的回复也一起变短）
  - `personalized_push.py`：**只加注释**，显式标注"仍是进程级身份（后台广播通道）"。
    它是无会话/无设备的定时广播，拿不到"这次推给谁"，属已知功能缺口（批次 5/6）
  - **顺带修掉的真 bug**：`app/routes/manage.py` 的数据目录写成
    `os.path.join(PROJECT_DIR, "data", ...)`，而 `PROJECT_DIR` 是 `charlie/` **子目录**，
    真实数据在仓库根的 `ASSISTANT_KID_DATA_DIR`。实测 `charlie/data/` 下**一个 JSON
    都没有** ⇒ `/api/habits` 永远返回"暂无习惯数据"，`/api/behaviors` 与
    `/api/decisions/config` 的反馈读写**全部落空**（静默，不报错）。新增
    `_data_dir()` / `_data_file()` 统一解析
  - `/api/habits` 重写：支持 `?user=<uid>`，不传是**全局合并视图**（与 `/api/reminders`
    同一套语义 —— 管理台请求上没有用户身份，默认按 env 过滤会让它看不到家人打的卡）；
    键名**只在真的同名冲突时**加 `{owner}·` 前缀，每条恒带 `owner` 字段。
    顺带修 3 个健壮性缺陷：`week_days` 里裸调 `fromisoformat` 遇脏数据直接 **500**；
    非法日期项用 `break` 会**把连续记录清零**（改 `continue`）；`total_days`/`week_days`
    计重复项（改按去重后的天数）
  - **方案修正**：原计划的"用户域 Brain 按 `(mcp_set, uid)` 分键"**被否决** ——
    工具是经 `agent/inproc_tools.py` 在 Brain 线程内**进程内**调用的，决定数据落盘位置
    的是数据层怎么解析 uid，不是 Brain 的缓存粒度；分键只会把进程数从 ~7 拉到 ≤48，
    却修不掉任何一个具体串台点。改为直接修数据层与工具层，**零进程增长**
  - 新增 `charlie/tests/test_fr14_batch3b_tool_data_scope.py`（**54 用例**），
    **变异 14/14 全杀**，全量回归 **1,988 通过 / 0 失败 / 0 错误 / 48 跳过**
    （基线 1,934 + 54，零回归）。⚠️ 变异判定一律看子进程 `returncode`，不要用
    `re.search(r"(\d+) failed", stdout)`（`pytest -q` 的统计行不在捕获窗口里，
    曾把 6 个变异全部误判成 SURVIVED）

- **修复：设备上线重复播报「已通过飞书送达」的消息（跨通道去重）**。
  现象：ESP32 开机后把之前积压的消息全部播报一遍。服务端日志实测（2026-09-17）：

  ```
  20:21:48 [xiaozhi-push] ESP32未连接，入队(1): 磁盘空间不足：已使用94%…
  20:21:50 [feishu] 推送成功: 磁盘空间不足：已使用94%…      ← 用户手机已收到
  …
  20:27:02 [xiaozhi] 补发推送: 磁盘空间不足：已使用97%…      ← 设备上线又播一遍
  20:27:07 [xiaozhi] 补发推送: 磁盘空间不足：已使用100%…
  20:27:13 [xiaozhi] 补发推送: 磁盘空间不足：已使用93%…
  ```

  设备离线时消息被 `enqueue_xiaozhi_pending` 排进待推送队列，**同时**走飞书兜底
  送达；设备上线后 `flush_xiaozhi_pending` 又把队列内容逐条补发 —— 同一条消息
  用户被通知两次。
  - `app/state.py` 新增 `drop_xiaozhi_pending(text)`：按文本撤掉队列项。
    ⚠️ 空文本是"无操作"哨兵，不能匹配任何项（否则会清空整个队列）
  - `enqueue_xiaozhi_pending` 对**同文本 + 同目标**的项不重复入队，只刷新
    `ts`/`mp3`。否则同一条消息在离线期间被反复触发会堆出多份副本，上线后连播 N 遍
  - `app/notifications.py`：新增 `_feishu_send_once()`（同步，返回是否确认送达）
    与 `_feishu_send_async()`（开关 + 线程包装）。**撤销只在确认送达之后发生** ——
    飞书失败时队列必须留着，否则消息彻底丢失
  - 顺带修正飞书返回码判定：飞书 OpenAPI 无论成败都返回 HTTP 200，旧实现只判断
    "没抛异常"，会把 `code != 0`（token 失效 / open_id 无效）也记成"推送成功"
  - `play_reminder_audio` 的 `_feishu_done=True` 分支（`add_notification` 已推过飞书）
    显式撤销队列项，但先用 `_has_fallback_channel()` 确认确实有兜底通道
  - 新增 `charlie/tests/test_fr14_cross_channel_dedup.py`（41 用例）。
    变异测试 4/4 全部被抓住（撤销早于送达确认 / 去掉空文本守卫 / 去掉入队去重 /
    去掉兜底通道守卫）

- **修复：LLM 端点选择导致的首句死链（`首句超时(>8s)，视为死链`）**。
  现象：ESP32 偶发"反应特别慢 / 不回话"，随后播兜底语"抱歉，反应慢了，请再说一遍"。
  实测（2026-09-17 同机 6 次采样）：

  | provider | 首字节延迟 | 稳定性 |
  |---|---|---|
  | agnes | 1.10 – 3.31s | 尾部抖动可达 7.9s，历史多次 `api.agnes-ai.cn Read timed out`(15s) |
  | **stepfun** | **0.37 – 0.69s** | 稳定 |

  而 `LLM_PRIORITY=agnes,stepfun,sagnes` 把**最慢的放在第一位**，`_lite_endpoint()`
  又硬编码优先 Agnes —— **两条 LLM 路径都首选抖动最大的那家**。端到端首句预算只有
  8s（`XIAOZHI_FIRST_SENTENCE_TIMEOUT`），agnes 一抖动就吃光预算 → 死链 → 再回退
  完整大脑（35s 超时），用户侧表现为长时间无响应。
  - `.env`：`LLM_PRIORITY` 改为 `stepfun,agnes,sagnes`；新增 `LITE_PROVIDER=stepfun`
  - `agent/llm.py`：`_lite_endpoint()` 支持 `LITE_PROVIDER` 覆盖（**默认行为不变**，
    留空仍是 Agnes 优先，便于随时回退）；`_LITE_FIRST_CONTENT_TIMEOUT` 开放为
    env 可配（默认仍 8.0）。⚠️ 该值必须**明显小于**端到端首句超时 —— 两者相等
    （都是 8.0）正是本次死链的直接成因

- **修复：lite 通道的「首包超时」形同虚设 + 完全没有取消能力（`不回话` 的第二层根因）**。
  上一轮把端点换成 stepfun 只解决了「模型慢」，翻 20:33–20:37 那段会话日志后
  发现还有两个独立缺陷：

  ```
  20:37:25,591 [lite] 首token=0.71s
  20:37:26,082 barge-in, cancelling current TTS
  20:37:28,084 [xiaozhi] producer未在2s内结束，放弃等待
  20:37:28,084 [xiaozhi] brain_pool 重建(累计死链2次)
  20:37:31,637 [xiaozhi] brain_pool 重建过于频繁，跳过(距上次3.6s)
  20:37:44,720 [lite] 首content超时(>8.0s, elapsed=14.6s, model=agnes-2.5-flash)无content，中断流
  ```

  1. **首包超时实际不生效**：检查写在 `for line in r.iter_lines()` 的**循环体内**，
     对端一个字节都不发时循环体根本不执行 ⇒ 真实上界退化成 `_LITE_TOTAL_TIMEOUT`
     （12s）的 socket close，而它解开阻塞又慢 ~2.6s（实测 14.6s）。远超消费者侧
     8s 首句预算 ⇒ 必然判死链。
  2. **lite 完全没有取消能力**：`cancel_event` 根本没传进来，barge-in 之后生成器
     仍卡在阻塞读上，`app/xiaozhi_ws.py` 的 collect 线程因此长期不退出，被上层
     误判成死链并反复重建线程池。而 `old.shutdown(wait=False)` **并不会杀掉正在
     运行的线程**，被 `_REBUILD_MIN_INTERVAL`（10s）跳过的那次更会让卡住的 worker
     留在**当前**池里 —— 8 个 worker 逐步被吃光 ⇒ 后续请求排队 ⇒ "不回话"。

  - `agent/llm.py::_chat_lite_stream`：新增 `cancel_event` 参数（默认 `None`，向后兼容），
    并起一个 `lite-watchdog` 守护线程，在「取消置位」或「首包超时」时直接
    `resp.close()` 解开阻塞中的 `iter_lines()`
  - ⚠️ 取消监听必须**贯穿整条流**，不能因为"首包已到"就收工：实测 20:37 的 4 次
    barge-in 有 2 次发生在首 token 之后。早退会让生成器重新卡住
  - 看门狗主动关流引起的读异常是**预期内**的，不再记成"流中断"告警（否则现场会
    被误导成网络抖动）
  - `brain_stream_sentences` 的两处 lite 调用点都透传 `cancel_event`
  - `app/xiaozhi_ws.py`：`producer未在2s内结束` 分支加取消守卫 —— `cancel_evt` 已
    set 时按"延迟退出"处理（`log.info`），**不重建线程池**；未取消（真死链）仍保留
    原有自愈能力
  - 新增"超时倒挂"提示：只在**真的发生首包超时**时打（不是每次启动刷屏），
    带上可执行的调整建议
  - 新增 `charlie/tests/test_lite_cancel_watchdog.py`（17 用例）。变异测试
    **6/6 全部被抓住**（看门狗不启动 / 首包后停止监听取消 / 调用点不透传
    `cancel_event` / 去掉倒挂提示 / 一律重建线程池 / 不抑制预期读异常）
- **FR-14 多人场景 · 批次 3A：用户相关内容移出共享的 `brain.system_message`**。
  Brain 只按 `mcp_set` 缓存，而 `_get_brain()` 每次把 `_build_system_msg(mcp_set, session_id)`
  （静态段 + 动态段）写进这个**共享**对象 —— A 会话的动态内容（用户偏好、长期记忆、
  会话摘要、实时上下文）会留在共享对象上被 B 会话读到，即「内容串台」。
  现在共享对象只保留**静态段**（`_build_system_msg_static`：角色/人格/工具文档，
  与 session / user 无关）；动态段由每次调用注入 `messages[0]`，`uid` 走
  `agent/user_context.resolve_user(session_id)` 按会话解析。
  qwen_agent 会把 `brain.system_message` 前置合并进已是 system 的 `messages[0]`
  （`qwen_agent/agent.py:110-121`），所以最终仍是**单条** system 消息 ——
  双 system 会触发 400（历史 P0：`ACCEPTANCE_REPORT` bug 14）。
  - 覆盖路径：非流式 `_brain_llm`、流式 `brain_stream_sentences`（含 `feishu_text` 通道）
  - lite 闲聊快通道**刻意不注入**动态段（它从不读共享对象，串台风险为零；而记忆检索 ×2 +
    上下文构建 3 次 IO 会直接落在首包延迟上）—— 见 `agent/llm.py` 内注释
  - 收益：内容串台消除，**零 Brain / 零 MCP 子进程增长**
- **FR-14 多人场景 · 批次 2 补齐：数据层 uid 分片**（此前有代码、零测试）。
  `preferences` / `persona` / `history` / `memory_store` / `state_sync` 统一走
  `agent/user_context.resolve_user()` 解析 uid，`default` 保留改造前的原文件名
  （**老数据不迁移**），其他 uid 加 `_{uid}` 后缀。本轮补上：
  - `agent/state_sync.py` 补齐遗漏的 3 处 `os.environ["CHARLIE_USER_ID"]` 直读，
    4 个函数（`_acquire_file_lock*` / `read_shared_state` / `write_shared_state` /
    `_publish_mqtt` / `setup_mqtt_listener`）加 `uid` 参数并透传
  - 新增 `charlie/tests/test_fr14_batch2_uid_sharding.py`（28 用例，路径层 + 往返层）。
    在旧代码上实测 **5 例失败且全部落在行为断言**上（非 `AttributeError`）
  - ⚠️ **重要事实**：`bind_user()` / `set_session_user()` 目前只被测试调用，
    生产代码里没有任何地方绑定用户 —— 因此批次 2 的隔离**在生产中尚未生效**
    （`resolve_user()` 一律回落 env/`default`）。这同时说明批次 2 是**零行为变更**。
    真正生效要等批次 4/5 在入口用 `bind_user()` 包住整条处理链
- **FR-14 多人场景 · 批次 4a：设备 → 用户映射 + ESP32 入口绑定**。
  批次 2/3A 把数据层与 system prompt 改成按 uid 隔离，但**没有任何地方绑定用户**，
  隔离形同虚设。本批把它接到 ESP32 入口，让前两批真正生效：
  - 新增 `agent/device_users.py`：持久化的「设备 → 用户」映射
    （`device_users.json`，支持 `CHARLIE_DEVICE_USERS` 环境变量覆盖）。
    按 **`device_key`**（hello 载荷里的 `client_id`/`device_id`/`mac`）索引 ——
    不能用 `session_id`，后者是 `uuid4()[:16]`，**每次重连都会变**。
  - `agent/user_context.py` 新增 `bind_session_user(session_id)` 上下文管理器
  - `app/xiaozhi_ws.py`：hello 握手时解析设备用户并 `set_session_user()`；
    新增 `_process_utterance_bound()`，在 `create_task` 前用 `bind_user()` 包住整轮
    ASR→brain→TTS（两处调用点：wake 自动收音、push-to-talk 的 listen/stop）
  - `app/state.py` 新增 `set_xiaozhi_client_device()`：把稳定 `device_key` 记进连接表，
    否则管理页只能看到一串随机 uuid，无法回答"这台设备绑的是谁"
  - 新增管理 API：`GET/POST/DELETE /api/device-users`
  - **默认空映射 ⇒ 零行为变更**：未绑定设备一律回落原身份，所有数据仍写改造前的原文件名
  - ⚠️ **未绑定设备刻意「不 bind」**：`bind_user("default")` 会把
    `os.environ["CHARLIE_USER_ID"]` 的兜底**盖掉** —— 单用户老配置（env=laoyu）
    一旦被盖，表现就是"记忆、偏好、历史全部凭空消失"。故新增
    `bound_device_user()`（未绑定返回 `None`）供入口层判断
- **FR-14 多人场景 · 批次 4b：主动推送（提醒）按用户隔离**。
  批次 4a 只覆盖了**设备发起的对话**（ESP32 → ASR → brain → TTS）；
  提醒的投递链（`schedulers._play_async` → `notifications.play_reminder_audio`
  → `push_tts_to_xiaozhi` → 待推送队列 → ESP32）仍走进程级身份，
  于是妈妈设的「8 点吃药」会放到老于房间，老于问「我有哪些提醒」会听到妈妈的。
  本批把它接通：
  - `app/reminders.py`：每条提醒新增 `owner` 字段；`list_reminders_for(uid)` 作为
    **用户面读取入口**（`list_reminders()` 保留为调度器/晨报用的全局视图）；
    新增 `reminder_owner()` 读归属（旧数据无该字段 ⇒ `"default"`）
  - **写路径不看 env 兜底**（`_owner_for_new_reminder()`）：只在入口层显式
    `bind_user()` 时才定向，否则一律 `"default"`（广播）。读路径
    （`list_reminders_for`）**保留** env 兜底 —— 这条不对称是刻意的：
    若写路径按 env 解析，老配置（`CHARLIE_USER_ID=laoyu`、无任何设备映射）下
    每条新提醒都会带 `owner=laoyu`，投递层会认为"目标用户没有设备"，
    既不推也不广播 ⇒ 提醒静默消失（用户只看到"已添加提醒"）
  - `app/notifications.py`：`play_reminder_audio(owner=)` 用 `bind_user(owner)` 包住
    TTS 合成（称呼/人格取对人）+ 把 owner 作为 `target_uids` 传给投递层；
    `push_tts_to_xiaozhi(target_uids=)` 按设备归属过滤，
    且**定向推送不走 MQTT**（MQTT 是广播通道，会把妈妈的提醒放到老于房间）。
    新增 `_any_device_bound()`：目标 uid **压根没有设备映射**时降级为广播 ——
    区分"设备已映射但离线"（保持定向排队等上线）与"uid 没有任何设备"（配置错误，
    定向毫无意义，排队后 `_take_pending_for_flush(uid)` 也取不出来）
  - `app/state.py`：待推送队列支持 `target_uids` 定向取件；
    **不匹配的项留在队列**，不能被"别人的设备"静默吃掉；
    `enqueue/flush/register` 三个入口加 `uid` 参数
  - `app/xiaozhi_ws.py`：hello 解析出设备用户后**定向补发**一次；
    post-stream flush（`stream_tts_reply` 的 try/finally 两处）也带上设备归属
  - 用户面读取按用户过滤：`/api/reminders` 新增可选 `?user=` 过滤
    （**默认仍是全局管理视图** —— 管理台请求上没有用户身份，按 env 过滤会让它
    看不到家人通过语音设的提醒）；`magic-reminder` 的
    `list_reminders` / `delete_reminder` / `list_alarms` 改为按当前用户过滤
  - `notifications._reminder_prefix(owner)`：播报称呼按用户可配
    （偏好键 `reminder_prefix`），未配置 ⇒ 回落改造前的硬编码 `"主人，提醒您："`，
    **默认行为零变化**
  - **默认零行为变更**：无设备映射 ⇒ 所有提醒 owner 都是 `"default"` ⇒ 广播，
    与改造前完全一致
  - ⚠️ **`"default"` 在投递层是广播**，不是"只发给未绑定设备"。生产现状是所有提醒
    都是 default；若当成定向，一旦老于开始绑设备，这些提醒会从"全屋都响"变成
    "只在没绑的房间响" —— 属于没人预期的行为突变
  - 新增 `tests/test_fr14_batch4b_reminder_routing.py`（**60 用例**，含 3 个端到端
    "妈妈在自己房间说提醒 → 只在她房间响"）。变异测试 6/6 全部被用例抓住

- **全流程闭环测试 Campaign A–D（真实语音 + 故障注入 + 自动化回归）**。

  四轮闭环全部对着**运行中的真实服务**（voice_server :8000 + decide_server :8080）：

  ① **Campaign A 真实语音闭环**（`/tmp/live_loop_test.py`，macOS `say` 真发声 → Opus → WS → 真 ASR/decider/LLM/TTS 回放）：7 轮对话覆盖 decider 命中（conf 0.60–0.96，250–650ms，state_first_cached）、弃权回落、intent 缓存短路、关键词预匹配、多轮指代、打断（350–390ms）；ASR 链 sensevoice→baidu 回落实测生效；TTS 整链含离线 `say` 最后兜底（一轮 StepFun 400 后兜底照常播音）。服务端统计：7 轮 ASR 全对、打断 3 次、错误 0。
  ② **Campaign B 故障注入**（`/tmp/live_loop_test_b.py`）：`pkill` 杀 decider → 3 轮全部经 LLM 兜底正常出话；重启后 HTTP 400 unknown context **自愈重注册**自动恢复命中（加分验证）；天气修复 live 验证通过。
  ③ **Campaign C 自动化回归**（`tests/test_closed_loop_decide.py` 新增 6 用例，全绿）：WS 协议栈 + `brain_stream_sentences`/`_classify_intent` **真身**，只 mock 网络边界（ASR 文本 / chat/completions / StepFun TTS HTTP）——decider 命中时 LLM 分类**零调用**（`_session.post` 挂了"触之即炸"）、弃权时恰好一次回落、`_ask` 全 None 模拟服务挂 3 轮后**真实冷却**生效（fail_streak≥3、cooldown>0）、key 路由闭环（整句 TTS 真链路，Authorization 带的就是轮询 key）、天气预报明天段两用例。
  ④ **Campaign D 全量回归**：全量（除既有跳过的 `test_round6_coverage.py`）**exit=0、零失败**，新文件与全部既有用例同会话共存无污染（重复跑两遍确认）。

  **方法论沉淀（两个真踩过的坑，已钉进测试文件防再犯）**：
  - **测试文本必须过关键词守卫**：分类链第二环是关键词预匹配，撞了关键词（实测"海獭睡觉会牵手吗"的"牵手"撞 magic-scenes）decider 路径根本不会被走到，用例会以完全不同的路径"假通过"。`_assert_keyword_clean()` 钉死。
  - **session fixture 跨文件不能导入**：`from test_xiaozhi_ws import xiaozhi_server` 会让 fixture 在本模块被 pytest 重新注册成独立 FixtureDef，session 缓存失效、两个 uvicorn 都绑 8001——第二个 bind 失败走 lifespan shutdown，把**共享模块级 `_brain_pool`/`_io_pool` 一起关掉**（实测：同会话后续所有 WS 用例 ASR 全挂 "cannot schedule new futures after shutdown"）。新文件改用自带独立端口（8002，`XIAOZHI_CLOSED_LOOP_PORT` 可覆盖）的 session fixture。

- **慢路径开场 filler 移到 WS 消费者竞速计时器 + 天气双路并行 + barge-in 停止不再等 producer（性能续轮，2026-09-19 live 埋点驱动）**。

  ① **filler 所有权从生成器移到 WS 消费者（`app/xiaozhi_ws.py`，本轮核心）**：上轮把「好的，我想一下。」放进 `brain_stream_sentences` 生成器开场，但 live 闭环发现它只覆盖**完整大脑**路径——decider 弃权后的 LLM 分类兜底（live 实测 3.1–5.0s）、lite 闲聊首 token（2.2–6.9s）都发生在生成器里那句 filler **之前**（`_classify_intent` 在生成器内部调用），用户依旧干等。改为 **WS 消费者首句竞速**（`_race_first_sentence`）：同时等"真首句"（`q.get()`）与"预合成 filler"（`XIAOZHI_FILLER_DELAY=0.7s` 定时器，TTS 缓存命中 ~1ms 出声），谁先到播谁。三条设计约束：filler 先赢时那个挂起的 `q.get()` future **原样留给下一轮复用**（取消它会让 pending getter 丢队首项——`asyncio.Queue` 取消竞态）；**真首句的继续等待仍受本轮首句预算约束**（filler 先赢后 8s 内真首句不到 → 走同一个死链兜底，死链检测不被 filler 吃掉）；filler 只武装"本轮真生成"（`prebuilt is None and not interrupted_reply.strip()`——打断续播秒回、刚打断的追问要答案，都不需要「我想一下」）。live 实测（restart9）：各轮首音频全部 **701–703ms**——此前 decider 命中 300–600ms 不变，lite 2.2s+ / 弃权分类 3.1s+ / 完整大脑 2.4s+ 全部被 700ms 覆盖；快路径不会被抢。

  ② **filler 暴露并修掉一个 FR-13 回归（`cancel_stream`）**：filler 现在会在分类窗口里播出，barge-in（喊唤醒词）落在该窗口时，`cancel_stream` 原先 `await` 被取消的流——流的 finally 里有"等 producer 退出"（≤2s，producer 常卡在 LLM 分类/工具往返里退不出），设备因此**晚 2s 才收到 tts/stop**、继续播放本地解码缓冲里的残余帧（live 实测 2003ms，"打断掐不断"）。改为：consumer 硬取消后**立即发 stop**，流的收尾（producer 等待、flush）交后台（`_track_task`）。传输层保序保证不会有新帧跟在 stop 后面（在飞的帧要么排在 stop 前到达、要么根本不到）。live 实测打断耗时 **0ms**。

  ③ **天气两路 amap 调用并行（`magic-info.py`）**：`get_detailed_weather` 的预报（extensions=all）与实时（extensions=base）互不依赖，从两段串行（实测 ~148ms）改为 `ThreadPoolExecutor(2)` 并发（~69ms）；实时解析失败时保持旧语义（`realtime=""`，不出"实时："段）。

  ④ **全域预热无价值，量完即弃**：`_get_brain` cold 首域 699ms（一次性），其后每新域 34ms、热路径 0ms——预热省不了什么，本轮不做。

  ⑤ **测试基建：session 绑定失联坑钉死**：`test_closed_loop_decide.py` 的 session mock 改用 `_session_post_patches()` 同时 patch `voice_agent._session` 与 `agent.llm._session` 两个绑定——`test_voice_agent.py` 的 `importlib.reload(agent.llm_state)` 用例会造出**新的** session 对象，使两个模块级绑定失联，只 patch 一边时另一边的真实请求会漏到外网（被测试网络护栏报错，表现为"分类没打"）。同会话不同文件顺序下都会炸，故两绑定一起打。

  测试：12 个曾按"生成器 filler"改过的用例全部回退（filler 回到消费者层后生成器序列不再有 filler）；`test_closed_loop_decide.py::test_full_brain_voice_turn_leads_with_filler` 改写为**慢大脑**（FakeBrain 首句 1.5s，越过 700ms 阈值）断言消费者竞速播出 filler、真答案随后到达；`test_brain_deadlink_sends_slow_reply_fallback` 钉死"filler 先赢后真首句超时仍走死链兜底"（`_FIRST_SENTENCE_TIMEOUT=3` 下 filler 702ms 播出 → 真首句超时 → 兜底语照旧）。全量回归 exit=0（2111 passed / 49 skipped；`test_round6_coverage.py` 为**既有**收集失败——它的 `from tests.test_xiaozhi_ws import` 被 site-packages 里 ultralytics 的 `tests` 包遮蔽，与本轮无关）。live 闭环 33 PASS / 1 FAIL（唯一 FAIL 是测试预期问题：decider 对「太阳系里最大的行星是什么」判 magic-info conf=0.88 命中并正确作答，测试期望它弃权走 LLM 兜底——行为正确，弃权路径由 mock 用例确定性覆盖）。

- **闲聊快通道（lite）死链修复：读超时 15s→2s + 主节点无产出切备用 provider 重试（并重轮核心，2026-09-19 00:36 事故驱动）**。

  **死链机制**（现场 + 本地复现，不是猜的）：agnes 在 POST **头阶段**挂住——连接建立、请求已发出、对端一个字节都不回。旧实现把它读超时（`_LITE_TIMEOUT` 15s）当唯一防线，而端到端首句预算只有 8s（`XIAOZHI_FIRST_SENTENCE_TIMEOUT`）⇒ WS 消费者判死链、发兜底话术、**整轮 utterance 被丢弃**，用户必须重说。本地复现：自建 HTTP server 接受连接后 `sleep 3600s`（`/tmp/deadlink_live_test.py` 场景 C，与现场日志形态逐条一致）+ 黑洞地址 `10.255.255.1:1`（场景 B 连接超时）。

  **为什么是"备用重试"而不是另两条路**（三个方案都否过）：耐心等主节点——agnes 尾部抖动最高 7.9s，超预算减意图/TTS 的余量，等就是死链；直接回退大脑——大脑是 24–31 工具 fncall 负载，首句 1.27–6.17s（冷启 11–12s），放不进剩余预算，同样死链；并行双发——正常路径（agnes 首 content 中位 0.29s）token 花费翻倍，违背效率并重。⇒ **主节点快速失败，切备用 provider（大脑 provider = stepfun）答一次**：挂住/空产出时用户仍能听到回复，正常路径零额外开销。

  四处改动（`agent/llm.py`）：
  ① **读超时 (2,2)**：15s→2s。头阶段挂住的真实上界就是读超时，主节点失败得越快，留给备用的预算越多。
  ② **主节点首 content 8.0s→3.0s**（`LITE_FIRST_CONTENT_TIMEOUT`）：旧值与端到端预算相等是死链的直接成因——lite 放弃的瞬间端到端也到点，回退大脑的预算为 0。3.0s 对 agnes 够宽（实测首 content 中位 0.29s、p90 0.50s、10/10 有产出，4x 余量）。
  ③ **备用重试编排**（`_lite_endpoint_candidates()` + `_chat_lite_stream` 编排器 + `_lite_stream_attempt` 单次尝试）：候选最多 2 个——主 `_lite_endpoint()`（默认 Agnes）+ 备 `active_chat_endpoint()`（大脑 provider，保证已配置、自带轮换），**按 base 去重**（主备同端点时重试毫无意义，同机同病）。备用更耐心（`LITE_FALLBACK_FIRST_CONTENT_TIMEOUT`=5.0s，stepfun 是 reasoning 模型要先跑完思维链才吐字）、更大预算（`LITE_FALLBACK_MAX_TOKENS`=512 + `reasoning_effort=minimal`——mt=300 时思维链吃光配额，实测 3/3 零正文）。历史/缓存/记忆只在最终有产出时由**编排层**写一次；`cancel_event` 在两次尝试之间检查——barge-in/死链取消后绝不再重试备用（用户已经不想听了）。
  ④ **失败归因修正**（`_provider_name_for_base` + `_note_provider_failure(name)`）：lite 失败此前记在大脑 provider（stepfun）账上，会把 `_PROVIDER_ROTATE_THRESHOLD=2` 的轮换计数打满，可能把大脑轮到一个挂着的节点——失败现在记在**实际尝试的端点**（agnes）账上。

  **预算账**：0.3s（意图）+ 2s（主读超时失败）+ 5.0s（备首句）+ 0.3s（TTS）= **7.6s < 8s ✓**。仍超时的（备用思维链 >5s 尾巴或 reasoning 饿死）落回大脑，与修复前同一条兜底，**不算回归**。边界场景（主节点"连上但无正文"磨到 3.0s 才失败）合计 8.6s 超预算——超时倒挂警告会在真超时打出可执行建议。

  **实测（真 Key，live）**：正常路径不受影响——首 token 0.19–0.89s、首句（含 TTS）1.4–2.2s，全部远小于预算；挂住场景 C：**2/4 样本被备用救回**（6.05s/6.18s 出首句），其余 2 个落大脑，与修复前逐条一致；barge-in 取消 lite 流干净退出（elapsed 1.5–2.5s）。备选节点 **sagnes 测过否掉**：首 content 3.15–5.18s 且 2/5 超时，比 stepfun（直连 1.39–2.07s、5/5；hang 路径 2.80–5.53s）差。

  测试：新增 `tests/test_lite_fallback.py`（17 个：候选去重/上限/坏端点跳过/主 POST 超时触发备用/主空产出触发备用/主有产出不重试/取消后不重试/主 mid-stream 部分产出不重试/备用首包更耐心/备用 mt 更大且带 reasoning_effort/历史只写一次/provider 名解析/超时记在实际尝试的 provider/非超时不记）；`test_round10_llm.py`、`test_round12_llm_feishu.py` 的钉死断言 8.0→3.0 更新；`test_log_driven_fixes.py` 同时打主备两端点（去重后单候选，避免双尝试把断言打两次）。全量 **2445 passed / 49 skipped / 0 failed**。live 验证：重启服务跑三轮 WS 闭环，热运行全过（残留 FAIL 是"缓存命中"意图缓存产物——测试为全新服务写的，重复语句命中缓存后日志窗口无新 decider 标记，非回归）。

- **ASR 退化音频审计（并重轮）：本地 SenseVoice 在 SNR 5dB/8k 带宽/1.15x 语速下准确率无损失，维持本地优先**。

  「准确率高效率高就选用」已定 SenseVoice（CER 3.3%、24–85ms），本轮补**退化鲁棒性**这一维：3 说话人（Tingting/Meijia/Sinji）× 7 条件（clean / 白噪 20/15/10/5dB / 8k 电话带宽 / 1.15x 语速）× 10 句 charlie 真实语料 = 210 样本，43s 跑完（`/tmp/asr_robust_audit.py`），每例同时过项目真实的 `is_garbled_asr` 闸门判定。

  结果：Tingting/Meijia 从 clean 到 SNR 5dB、8k 带宽、1.15x 语速 **CER 逐档持平 3.3%**——噪声与带宽对已上线的模型无额外伤害；Sinji 只在 5dB 劣化（6.7%：行星→恒星、那后天呢→晚后天啊，两例同音错）。**结论：维持 SenseVoice 本地优先，无代码改动**。

  审计同时量到一个**既有风险**（无需本轮修，但要知道）：乱码闸门对所有错误样本**零拦截**——错误全是"看起来干净"的同音替换与 ITN 数字转换（九点四十五分→9点45分，下游意图解析兼容）。即竞速规则"本地干净+云端超宽限→用本地"只防乱码、不防语义错；这是本地 ASR 方案的固有代价，天气/飞书等关键域仍有 decider + 关键词两道后闸兜底。

- **意图分类 decider context v4 校准（并重轮）：67 句语料实错判 3→1，阈值维持 0.5（帕累托最优）**。

  67 句语料 6 变体对比（阈值 0.5 下的实错判数，史见 `agent/decide_intent.py` 注释）：v1 原版 3 错（电视音量→ac-control / 太阳系→magic-info / 晚安→scenes）→ v2 2 错（magic-info 负向说明修掉前两个，但"音箱等本机设备"把风扇带到 magic-system）→ v5 3 错（magic-system 描述里提"空调/灯/风扇"字样**反而吸走**"电视音量"——描述里提电器名会反噬）→ **v4 1 错**（magic-system 只讲播放参数不提电器名，风扇回 ac-control；仅剩晚安→scenes，说晚安进夜间模式属可辩护的产品行为）。

  **阈值维持 0.5**：抬到 0.6 只能多挡风扇、却让 4 个正确快命中回落 LLM——帕累托不划算。**结论：准确率修复走 context 描述而非抬阈值**（抬阈值对全部查询收效率税，描述修复免费）。context_id 随内容变更换为 `charlie-intent-v4`（同 id = 同文本，服务端缓存）。

- **`.env.example` 过期条目修正 + lite 旋钮补全**：`XIAOZHI_FIRST_SENTENCE_TIMEOUT=30  # 代码默认: 30s` 三处都错——值是 30（代码默认 8.0，测试钉死 ≤10s；新装机复制 .env.example 会把死链预算放大 4 倍）、描述写反（它是**大脑首句死链预算**，不是"等待用户说第一句话"——那个是 `XIAOZHI_ARM_WINDOW`）、注释默认值不符。已改为 8 并修正描述；补 `LITE_FIRST_CONTENT_TIMEOUT`/`LITE_FALLBACK_FIRST_CONTENT_TIMEOUT`/`LITE_FALLBACK_MAX_TOKENS`/`LITE_MAX_TOKENS`/`LITE_TOTAL_TIMEOUT`/`LITE_PROVIDER` 六个旋钮（含"备用是 reasoning 模型""LITE_PROVIDER 只能用非 reasoning provider"两个坑的现场提示），`app/env_catalog.py` 同步补条目（这些是导入时读取的模块常量，如实标 `tunable=False`——改后需重启，不标 tunable 防止设置页给人"改了即时生效"的错觉）。

- **开放任务域 magic-agent：接 Hermes Agent CLI 后台执行（新功能）**。

  「现在输出的音色还可以，不过很多开放性问题做不了，比如我让他清理磁盘就做不了，这个可以做的，后端接入一个agent cli就可以了」——28 个固定工具域（天气/音乐/提醒…）覆盖不了"清理磁盘/排查网络/备份归档"这类需要多步操作的系统维护请求，用户只能听到"这个我做不到"。约束（用户原话）：**任务要完成，实时对话要精简**。

  ① **架构（并重的落点）**：分类到 `magic-agent` 后 `brain_stream_sentences` **早退分支**秒回一句确认（"好的，我去处理，完成后告诉你。"，零 LLM 往返、不等任务），任务由 `agent/magic_agent.py` 起后台守护线程跑 hermes oneshot（`Popen(start_new_session=True)`，上下文 `copy_context()` 继承用户绑定），完成/失败经 `push_feishu_async` 推飞书。实测真任务（建文件）hermes 跑 **1m51s**——放进 brain 必然被 35s 大脑超时（`_BRAIN_RUN_TIMEOUT`）掐掉并回"抱歉出错"，所以后台执行不是优化是必需。

  ② **安全边界**：任务文本走 `--query-file -`（stdin，hermes 文档明示不经 shell 解释，任意中文/引号/`$()` 原样传递）；双保险超时（hermes 自带 `--run-budget` 软预算 + 本模块硬超时 kill **整个进程组**，hermes 的工具子进程一并收掉）；不设 `--yolo`（oneshot 实测会自动批准工具调用，任务要能真做成）。ack **不写缓存**——缓存会让重复请求静默丢任务。

  ③ **路由（5 处联动 + 1 条消歧规则）**：`_KEYWORD_MAP` 高精度复合词（含"清理一下磁盘"这类动补变体——只列主干词会漏掉最自然的口语句式）；`_normalize_intent` 显式分支（**必须在 "magic" 泛匹配之前**——magic-agent 不在 ALL_MCP，泛匹配会查 ALL_MCP 落空兜底成 magic-music）；LLM 分类 prompt 选项/规则/示例三处；decider DOMAINS + context（`charlie-intent-v5`）。**确定性消歧**：强任务标记（磁盘/网速…）共域时压过子串误命中——"网速很慢排查一下"里 baize 的"查一下"是"排查一下"的子串、"释放点空间"里 music 的"放点"是子串；搜索守卫（"搜索备份软件"→baize）与复合句（>15字/双意图）留给分类器。**decider v5 重校准（74 句）**：8 句 magic-agent 语料全对（conf 0.78–0.96）、负向全对（"搜索备份软件"→baize 0.63、"整理一下笔记"→notes 0.99）；阈值维持 0.5（0.45 多 1 错、0.55 多挡 3 个正确快命中，帕累托不划算）。

  ④ **降级**：hermes 不可用（`AGENT_CLI_ENABLED=0` 或二进制不在 PATH）→ 早退分支不触发，请求落回大脑通用工具路径，行为与接入前一致。

  ⑤ **配置**：`AGENT_CLI_ENABLED`/`AGENT_CLI_BIN`/`AGENT_CLI_RUN_BUDGET`/`AGENT_CLI_TIMEOUT`/`AGENT_CLI_MAX_TURNS`/`AGENT_CLI_WORKDIR`/`AGENT_CLI_PUSH_MAX_CHARS` 进 .env.example 与 env_catalog（默认开，bin 可换 opencode/pi/dsh）。

  ⑥ **验证**：新增 `tests/test_magic_agent.py` 33 个（命令构建/stdin 不经 argv/box 边框抠取/超时 kill 进程组/非零退出/空产出/无二进制/秒回不阻塞/注册表容量淘汰/推送成功失败截断/早退分支不碰大脑/不可用落回大脑/ack 不入缓存）；**live 实测**：真 hermes 跑"在 /tmp 建文件 charlie_live_verify.txt"任务，STATUS=ok，文件真建成（17 字节、内容正确，hermes 还自行 wc/cat 复核）；全量回归 **2535 passed / 264 skipped / 0 failed（exit=0）**。顺带修了两个被 StepFun-only 迁移 + say 文件化改造拖旧的测试（`test_jarvis_features` 的缓存隔离用例改 mock `_tts_stepfun` 并把假音频加长到 >100 字节——43 字节过不了长度检查会静默走兜底；`test_round8_infra` 的 say 兜底用例改用 `_say_side_effect` 文件式 mock）。

### 修复
- **天气预报不给明天：`get_detailed_weather` 从未用过 `casts[1]`**（真 bug，闭环测试 Campaign A 发现）：

  高德 `extensions=all` 固定回 4 天 casts，但实现只取 `casts[0]`（今天）——问"明天天气"这个关键词表里的高频查询，brain 拿到工具结果里**一个字明天数据都没有**，只能答"暂未获取到"。真机 live 复现（"明天北京天气怎么样"）确认；amap 本身健康（真 Key 实测 4 天 casts 齐全）。修复：取 `casts[1]` 拼明天段（`{城市}明天{白天转夜间}，{夜温}到{日温}度。`，白天/夜间天气去重后"转"连接，温度缺失或为 0 时整段不出），拼进结果串。live 验证："明天杭州多云转晴，23到三十度"正常播报。测试：`tests/test_closed_loop_decide.py` 两个天气用例（含明天段 / 单 cast 不出明天段）。

  顺带闭环中观察到的**既有韧性路径全部按设计工作**（记录备查，未改）：brain 首句 >8s 死链 → "抱歉，反应慢了，请再说一遍"兜底 + 线程池重建（~20 轮中 2 次）；brain 空句 → TTS 400 → 离线 `say` 兜底照常出声。
- **循环提醒生成下一次时丢失 `owner`**（FR-14 批次 4b，真 bug）：
  `_generate_next_occurrence_locked()` 新建条目时没带 `owner`，而它跑在调度器后台
  线程里、**没有任何身份绑定** —— 于是「妈妈的每日 8 点吃药」**第一天**是对的，
  从**第二天**起 `reminder_owner()` 回落 `"default"`，变成全屋广播。
  只有第一条对、之后全错，是最难被发现的一类跨用户串台。改为从源提醒继承 `owner`。
- **3 个既有 flaky 用例：用固定 `sleep` 等子进程就绪**（与 FR-14 无关，顺手修）：
  ① `test_utils.py::test_truncate_waits_for_exclusive_history_lock`、
  ② `test_voice_agent.py::test_save_history_waits_for_shared_history_file_lock`
  在 `subprocess.Popen` 后 `time.sleep(0.1)` 就假设子进程已启动 Python 并抢到文件锁 ——
  机器有负载时（全量回归同时跑 torch / silero）冷启动经常超过 100ms，父进程于是在
  **无人持锁**时调用，立即返回，`assert elapsed >= 0.35` 偶发失败；
  ③ `test_runtime_resilience.py::test_watchdog_uses_configured_log_dir` 在
  `sleep(1.5)` 后读看门狗写的 `commands.log`，脚本冷启动超过 1.5s 时文件还不存在
  → `FileNotFoundError`。
  统一改为**子进程做完第一步后写标记文件，父进程轮询它**（20s 上限），
  修后连跑 5 轮稳定通过。
  > 归因手法（值得复用）：`pytest tests/ --deselect <本批新增测试文件>` 再跑一遍 ——
  > 总数正好回到上一档基线（1,813）且这几例仍以**完全相同**的形态失败
  > ⇒ 判定为既有问题，与本次改动无关。
- **记忆热缓存跨用户串台**（FR-14 批次 2 遗漏，真 bug）：`_hot_fact_cache` 只按
  **fact key** 索引，没按 uid 分键。A 用户写入 `city=北京` 后，B 用户 `get_fact("city")`
  会**命中 A 的缓存条目**直接返回北京 —— cache hit 根本不查 SQLite，整条分片链路被绕过。
  `delete_fact` / `get_fact` 的可见性判断同样失真。
  现在缓存键改为 `(uid, fact_key)` 复合键；`_cache_hot_facts()` 只清当前 uid 的条目
  （旧实现整体 `clear()`，给 A 预热会把 B 已缓存的条目一并打回 SQLite）；
  `get_memory_stats()["cache_size"]` 改为**当前用户**计数，另加 `cache_size_all`
  用于观察 500 条全局 LRU 的挤占情况。
- **按 uid 分片后的 `memory_{uid}.db` 全是空库**（FR-14 批次 2 遗漏，真 bug）：
  `_init_db()` 只在模块导入时调用一次，那时 uid 恒为 `default` —— 于是除 `memory.db`
  外的所有分片文件都没建表，`upsert_fact` 以 `no such table: facts` **静默失败**
  （只打一条 WARNING），读取一律返回空。批次 4/5 一接上 `bind_user()`，
  每个家人用户的记忆写入都会无声丢失。
  现在改为按 DB 路径幂等（`_db_ready_paths` 集合），且 `_get_conn()` 每次先确保建表。
  另修一处同源缺陷：该集合命中时还需确认**文件仍在**，否则「删库后重建」
  （测试的 `_fresh_db()`、运维轮转或误删后重启）会跳过建表并永久失效。
- **`agent/state_sync.py` 根本无法被导入**（真 bug）：`get_synced_state()` 的注解写成
  `-> dict | any` —— `any` 是内置函数而非类型，`dict | any` 在 **def 期**求值即抛
  `TypeError: unsupported operand type(s) for |`。该文件此前无任何导入方（是死代码），
  所以一直没暴露；谁 import 它谁让应用在启动期崩。改为 `-> Any`。
- **lite 闲聊通道裁剪历史时会把 system 提示词一起丢掉**：`_chat_lite_stream` 在
  `total_chars > 4000` 时执行 `messages = messages[-4:]`，保留的是**最后 4 条** ——
  历史稍长就必然把 system 消息裁掉，模型随即丢失人格与规则，开始自报 provider 身份
  （"我是 Agnes"）。改为只裁历史、保留全部 system（`system + 最近 1 轮`）。
- **待推送提醒会被短暂连接静默吃掉**：`app/state.py::register_xiaozhi_client` 在设备连上时
  无条件 `list(...)` + `clear()` 再发送，两个后果 ——
  ① 没有 `_PENDING_TTL` 过滤，陈旧推送也会被补发（与「5 分钟内才补发」的语义不符）；
  ② **发送失败即永久丢失**，一次短暂连接（重连 / 探活 / 自检脚本）就能吃掉队列里的提醒
  （实测 2026-09-17 00:34 一次探活连接吃掉了 2 条待推送）。
  现在统一走 `flush_xiaozhi_pending`：TTL 内的项交给补发，**发送失败的项放回队列**待下次补发
  （仍受 TTL 约束，不迟到播报）；已开口播放到一半的失败不重放，避免半句 + 整句串音；
  过期项直接丢弃，不再一直占着队列。
- **天气城市误判（北京被播报成香港）**：文本里没有城市时（例如系统提示词里的天气摘要
  `direct_weather_play("")`），原本**直接信任出口 IP 定位**。代理 / VPN / 沙箱 / 云主机
  环境下出口 IP 与用户真实所在地不符 —— 实测 2026-09-16 20:23–23:59 连续 4 小时每 30 分钟
  把北京用户的天气播报成「香港今天毛毛雨」。
  城市解析优先级改为 **文本显式城市 > 用户偏好（`weather_city` / `city` / `location`）
  > IP 定位 > 北京**，城市来源写入日志；新增 `WEATHER_IP_LOCATE=0` 可彻底关闭 IP 定位兜底。
  顺带恢复了高德快路径（北京 73ms 命中高德；此前因城市名为「香港」而一直降级 Open-Meteo）。
- `agent/context.py` 的「当前位置」同样改为偏好优先，不再被出口 IP 定位覆盖
- `app/schedulers.py` / `app/briefing.py` 的城市来源补齐 `location` 偏好，晨报不再硬编码北京
- `_estimate_tokens` 兜底估算：tiktoken 不可用时（离线且无 BPE 缓存）改为中日韩/西文分档。
  旧实现 `len(text)//2` 中文低估 2.5 倍（「你好世界」算 2，实际 5）、英文高估 2.5 倍
  （「hello world」算 5，实际 2），低估会让历史裁剪少裁、撑爆模型上下文窗口
- **离线 TTS 兜底（macOS `say`）从来没有真正工作过，且用的是无音色的系统默认音**（用户实测反馈「本地合成语音效果很差，没有音色」）：

  ① **音色**：旧代码 `say` 不带 `-v`，用的是系统默认音——实测本机默认音 3.07s/句，与三个中文音 Tingting/Meijia/Sinji 的 3.43/3.61/3.75s 全不同，是 macOS 26 的 Siri 类系统音，读中文出来没有音色、像念字。改为显式 `-v Tingting`（zh_CN 标准音，与系统语言一致；`OFFLINE_TTS_VOICE` 环境变量可换，音色不存在时自动退裸 say——Linux/容器换不了音色的机器不受影响）。**用户复验：「现在输出的音色还可以」**。

  ② **管道模式在 macOS 26 上是坏的**（旧兜底因此从来没生效过）：`say -o -` 管道输出实测 rc=1 "Opening output file failed: fmt?" 或 rc=0 但 0 字节；`-o file.pcm --data-format=LEI16@24000` 也失败（扩展名相关）。只有写 `.wav` 文件模式正常（asr_robust_audit 的 210 条合成用的就是它）。改为 `tempfile.NamedTemporaryFile(suffix=".wav")` → `say -o tmp.wav` → `ffmpeg` 转 mp3，用完删临时文件。

  ③ **缓存投毒修复**：say 兜底音频原来与云端音频**共用缓存键**（文本, 角色音色, 模型）——断网期间把 Tingting 机械音缓存进去，网络恢复后会命中旧键继续播，云端音色要等 TTL(1h)/缓存淘汰才回来，等于"云好了还在用坏音色"。改为**离线音频不写缓存**（断网本就降级，say 是本地 CPU 合成 1–3s，在 8s 首句预算内，不缓存没有不可接受的代价）。

  ④ **验证**：live 实测 0.59s 出有效 3.672s mp3、临时文件清理、不污染缓存；`tests/test_synth_mp3_chain.py` 12 个 say 相关用例（指定音色/写临时 wav 而非管道/不缓存/音色缺失退裸 say/全 TTS 挂 RuntimeError）全过；`.env.example` 与 `env_catalog.py` 补 `OFFLINE_TTS_VOICE` 旋钮。

### 已知问题（本轮排查中发现，未修，待决策）
- **提醒已按用户隔离，但「系统级主动建议」仍是广播**（FR-14 批次 4b 的边界）。
  批次 4b 只给**真提醒**（`reminders.json` 里带 `reminder_id` 的那些）打了 `owner`
  并按人路由。`app/schedulers.py` 里其余 15 处 `_play_async(...)`（晨报、天气、健康告警、
  决策询问、主动建议）与 `app/briefing.py` 都**不传 owner** ⇒ 依旧广播给所有设备。
  这是刻意的保守选择：这些建议由**全屋共享的后台任务**发起，没有"该发给谁"的答案，
  硬猜一个人反而会把全屋提醒变成只响一个房间。要按人分发，需要先决定
  「主动建议归谁」的语义（按设备在线情况？按角色？按房间？）。
- **`/api/user/switch` 仍是进程级切换**（写 `os.environ["CHARLIE_USER_ID"]`），
  会改掉**所有并发会话**的身份 —— 这正是 FR-14 要消除的。本轮只加了警告日志与
  返回值提示，未改行为（怕破坏既有单用户用法）。多人请改用 `/api/device-users`。
- **流式路径把整段静态 system 提示词发送两遍**（语音主链路，每轮都发生）。
  `brain_stream_sentences` 为了渲染 `<<<VOICE_RULE>>>` 占位符，会把静态段注入
  `messages[0]`；而 qwen_agent 的 `Assistant.run()` 又会把 `brain.system_message`
  前置合并进去（`qwen_agent/agent.py:110-121`），于是静态段在最终 system 里出现两次。
  实测（2026-09-17）：`messages[0]` 里静态段 1 次 → 合并后 2 次。静态段约 2000+ 字符
  （人格 + 行为准则 + 通用规则 + 工具文档），相当于每轮流式请求多付一倍的 system prefill。
  修法需要动一个**被测试钉住的设计**（`tests/test_round3_system_msg_split.py:245/259`
  明确断言静态段保留占位符、由通道层替换），且会牵连 `brain.system_message` 的语义，
  故单独评估。

## [3.2.0] - 2026-08-13

开箱即用版本的稳定化迭代：修复用户可感知的缺陷、移除死代码、同步文档、清理技术债。

### 修复
- 电视/空调控制不再硬编码内网 IP `192.168.1.7`；未配置 `ESP32_IP` 时给出明确提示而非静默失败
- 系统音量调节在控制不可用时返回清晰反馈，不再假装成功
- “停止 / 暂停 / 闭嘴”仅做应答，不再误操作系统静音
- ESP32 烧录在 PyInstaller 窗口模式下改为进程内调用 `esptool.main()`，修复 frozen 环境无法烧录的问题
- frozen 环境下 esptool 依赖探测误报“未安装”

### 变更
- ESP32 配网全面切换为设备内置 AP 热点门户（`http://192.168.4.1`），移除脆弱的 NVS 字符串 patch 方案
  - 删除 `app/nvs_patch.py` 及其测试，从 spec 中移除引用
- 打包清单移除未使用的 u2 技能目录，便携包体积更小
- 百度 OAuth token 临时文件纳入 `.gitignore`（含 access_token，绝不入库）
- 新增环境变量 `MQTT_ENABLE_OTA`、`INTERNAL_API_TOKEN`，并登记到 `env_catalog`

### 文档
- 重写 `docs/ESP32.md`、`firmware/README.md`：AP 热点配网原理与步骤
- 更新根 `README.md`、`charlie/README.md`、`docs/DEPLOYMENT.md`、`docs/DEMO_MODE.md`、`docs/WINDOWS_BUILD.md`
  - 主路径改为 Windows 便携版 `charlie.exe` + 免费智谱 GLM
  - 移除 `nvs_patch`、`charlie-mac.zip`、`patch NVS`、`brew`/`sudo esptool` 等过时表述

### 清理
- 删除过时文件：根 `DEPLOYMENT.md`（Finna/deepseek 旧架构）、两个已合并的 `.patch`、
  旧打包脚手架 `assistant-kid/`、社交媒体草稿 `charlie/charlie_post.json`
- 修复 `.gitignore` 中损坏的字面换行
- `app/cert.py` 的 `datetime.utcnow()` 迁移为 `datetime.now(datetime.UTC)`

## [3.1.0] - 2026-08-13

首个开箱即用版本：双击 exe 即可在原生桌面窗口运行，免费 GLM 大脑 + 百度语音，无需安装 Python。

### 新增
- Windows 便携版 `Charlie-Portable.zip`：解压双击 `charlie.exe` 即运行，内置 ESP32 固件与 esptool
- 跨平台原生窗口（pywebview / WebView2）
- ESP32 AP 热点配网：烧录干净固件 + 手机连 `lc-s3-wifi-1.54tft-XXXX` 配网，支持任意长度 WiFi 密码
- 应用内烧录向导，进程内调用 esptool，免装 Python 环境
- 配置热重载：向导保存 Key 后立即生效，无需重启；保存时实时验证 GLM/百度 Key 有效性

### 修复
- 流式对话“大脑启动失败”
- 启动时显示“⏰ undefined”
- “语音服务繁忙”误导提示（未配置时准确引导）
- 移除天气回复中的 Open-Meteo 归属文本

### 资产
- `charlie-esp32-flash-16MB.bin`：ESP32 16MB 全量固件镜像（xiaozhi v2.1.0，LC-S3 1.54寸 TFT），已擦除 NVS，不含任何 WiFi/服务器信息

## [1.0.0]

早期版本（已被后续版本取代，无对应 tag）。
