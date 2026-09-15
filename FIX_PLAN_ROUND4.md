# Charlie 第四轮评估与修复方案 —— 服务层与运维角落

> 时间：2026-08-27
> 基线：三轮修复全部在位（12 个跨轮抽查点全部 ✅），pytest 968 全绿
> 结论先行：无 P0/P1 回归；历轮 C 表遗留项全部销项或有量化依据维持延期
> 本轮方向：前三轮未覆盖的记忆脚本族 / 外部 IO 服务层 / 调度后台 / 运维部署角落

---

## 一、新发现与处置总表

| # | 严重度 | 文件 | 问题 | 处置 |
|---|--------|------|------|------|
| NA1 | P1 | magic-evolution.py:78-105 | 进化数据 open('w') 非原子写 + 子进程/主进程双路径仅线程锁（跨进程无效），并发写坏后 json.load 异常被静默吞 → 学习成果丢失 | **修** |
| NA2 | P2 | magic-habits.py:23-87 | read-modify-write 全程无锁，并发打卡丢记录；HABITS_FILE 无用户隔离 | **修** |
| NA3 | P2 | magic-notes.py:80-134 | 购物清单 RMW 无锁非原子；clear 截断写可留半文件 | **修** |
| NA4 | P2 | app/news.py:48-118 | feedparser.parse 无超时；as_completed(timeout) 挡不住 shutdown(wait=True)，慢源冻结主动建议线程分钟级 | **修** |
| NA5 | P2 | app/schedulers.py 多处 :413/:418/:440/:465/:505/:546 | proactive 单线程循环内同步调 play_reminder_audio（TTS HTTP + WS future.result(10s)），单轮累计可阻塞 30s+ | **修**（线程化派发 + 推送串行锁） |
| NH8 | P2 | app/mqtt_server.py:369-378,695 | abort 后 2s 自动 clear 与新一轮语音/再次 abort 竞态，第二次 abort 可被旧清除线程吞没 | **修**（世代计数器） |
| NH11 | P2 | app/routes/manage.py:548-553 | .env 写入 open('w') 即截断非原子；flock 失败仅告警继续写 | **修**（tempfile+fsync+os.replace；锁失败告警后仍完整原子替换=最后完成者整版生效） |
| NA7 | P3 | app/schedulers.py:182-195 | 主动建议天气硬编码北京 110000 / "北京"，不读用户城市偏好 | **修** |
| NA11 | P3 | app/briefing.py:22-25,71-74 | 早间简报节日字段空占位，app/holiday 已有实现未接线 | **修** |
| NA8 | P3 | app/notifications.py:235-249 | streaming 守卫 import/调用异常时 pass 继续直推，违反设计意图 | **修**（异常改保守返回 enqueued） |
| NA9 | P3 | app/audit_log.py:44-51 | 轮转 open('w') 截断窗口丢日志 | **修**（tempfile+os.replace） |
| NA6 | P3 | baize_skills_mcp.py:170-201 | deep_research 提前 return 后 27 行死代码误导维护 | **修**（删除死代码块） |
| NH10 | P3 | app/routes/manage.py:5 | 硬 import fcntl，Windows 整服启动崩（项目有 fcntl_compat 垫片被绕过） | **修** |
| NH14 | P3 | watchdog.sh:152 | `stat -f%z` 仅 BSD/macOS 语法，Linux 日志轮转永不触发 | **修**（BSD/GNU 兼容回退） |
| NH15 | P3 | docker-compose.yml / Dockerfile | 缺 8443 映射、EXPOSE 不全、MQTT broker 假设外部无说明 | **修**（补映射/EXPOSE/注释说明） |
| — | — | tests/test_voice_agent.py::test_clean_for_tts_reuses_compiled_patterns | 第一轮记录的 junitxml INTERNALERROR 兼容问题（全局 monkeypatch re.sub） | **尝试修复**（timebox：若根因过深则文档化退回延期） |

### 记录在案、本轮不修（C 表续）

| # | 问题 | 不修理由 |
|---|------|----------|
| NH7 | mqtt UDP 收包循环同步 Silero VAD（~5-15ms/帧，WS 路径已异步化不一致点） | 量化终审：≤5 设备时帧余量充足不构成瓶颈；已给最小改造设计备查，多设备扩展时再动 |
| NH9 | _process_utterance 每段裸线程无并发上限 | 当前 utterance_active 同设备互斥 + 设备量小，排队属合理背压；扩展期再约束 |
| NH12 | local_wake 唤醒后开第二个 InputStream 与主循环争用设备 | 低端 USB 驱动才触发；重构为主循环单流切换工作量大 |
| NH13 | local_wake _play_audio 扬声器回声可能自我打断 | 加回声保护窗口属体验微调，需真机听感验证参数 |
| NA10 | schedulers 两模块级字典名义上无锁 | 实际均单线程独占访问，现状无竞态；加锁收益为零 |

## 二、实施分组

| worker | 范围 | 文件 |
|--------|------|------|
| worker-A（持久化与服务层） | NA1/NA2/NA3/NA4/NA5/NA6/NA7/NA8/NA9/NA11 | magic-evolution.py、magic-habits.py、magic-notes.py、app/news.py、app/schedulers.py、app/notifications.py、app/audit_log.py、app/briefing.py、baize_skills_mcp.py、新建 tests/test_round4_persistence_services.py |
| worker-B（通道与运维） | NH8/NH11/NH10/NH14/NH15 + junitxml 尝试 | app/mqtt_server.py、app/routes/manage.py、watchdog.sh、docker-compose.yml、Dockerfile、tests/test_voice_agent.py(junitxml)、新建 tests/test_round4_channels_ops.py |

并行无文件交集。原子写统一模式：参照 agent/preferences.py 黄金标准（flock 跨进程锁可选 + tempfile.mkstemp + fsync + os.replace）；utils._locked_file 若可用则复用。

## 三、验收标准

- 上表 15 项落地且 diff 可逐条对应；新增回归测试覆盖各自动作点（并发无丢失、崩溃安全语义以 mock 表达、超时窗口生效）；
- `pytest tests/ -q` 保持全绿（基线 968）；--junitxml 试跑结果如实记录（修复成功或文档化延期）；
- C 表续 5 项保留明确理由。

---

## 四、代码审查结果与实施后修正

总体结论：**修后合入 → 全部核销 ✅**。16 项 + junitxml 逐条核销通过；中断残留专项排查确认首次失败的 worker-A 未在任何 NA 文件留下半成品（"已合规"结论与源码一致）。

审查意见处置：

| 级别 | 意见 | 处置 |
|------|------|------|
| P1-1 | round4 测试 teardown 直接 pop 掉 conftest 设置的 ASSISTANT_KID_DATA_DIR，顺序依赖破坏下游用例 | **已修**：4 处 pop 改为模块级保存/恢复 |
| P2-1 | magic-evolution 双锁设计意图应在注释中固化 | **已修**：加双锁说明注释 |
| P2-2 | audit_log rotate filesize 读取存在微小竞争窗口 | 接受为可接受风险（需求即"过大截断保留尾段"，最终内容语义正确）|
| P2-3 | watchdog tail>tmp mv 非原子，mv 失败残留 .tmp | 记录为已知限制（mv 失败仅磁盘异常场景） |
| P3-1/2/3 | news 解析层极端大 XML、天气区级名未命中映射表回退北京、env flock 失败继续写的权衡注释 | 前两项记入下轮候选；第三项补注释 |

### 实施过程中发现并修复的额外问题

1. **junitxml 兼容问题销项**（第一轮遗留）：根因是 junitxml 插件自身的 `re.sub` 调用被测试的全局 monkeypatch 误伤；改为带调用栈检查的放行版后 `--junitxml` 正常工作，原断言语义不变。
2. **test_persistence_race.py 收集期环境污染**（陈年地雷）：该文件在 pytest 收集导入阶段改写全局 ASSISTANT_KID_DATA_DIR 且长期持有——历史上所有读取方同为静态快照故侥幸一致；本轮引入规范的 teardown 恢复动作后错位引爆三个下游用例。修复：环境翻转严格限定在两个隔离模块 `_load_module` 加载瞬间（try/finally 即时恢复），teardown 仅清理临时目录。
3. **voice_agent.HISTORY_FILE/HISTORY_LOCK_FILE 升级为实时计算**：委托层从"读 history 模块的滞后常量"改为调用 `_get_history_file()`/_get_history_lock_file()` 动态取值，从构造上消除"env 直改后路径陈旧"这一类问题（与 NB1 动态化哲学对齐）。

## 五、最终验证证据

```
$ cd charlie && python3 -m pytest tests/ --tb=short -p no:warnings -q
991 collected, EXIT=0（0 failed / 0 error）
定向：tests/test_round4_persistence_services.py(13) + test_round4_channels_ops.py +
      test_round3_mqtt_streaming.py + test_manage_routes.py + test_setup_api.py +
      test_voice_agent.py(--junitxml 双跑) → 全绿
worker-A 定向 40 passed / 1 skipped（既有环境相关 skip）
```

测试规模：968 → 991（+23：round4 两文件 30 例中含替换/合并）。
