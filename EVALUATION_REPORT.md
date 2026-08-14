# Charlie 语音助手 — 代码评估报告

## 1. 项目结构

### 1.1 God-File 反模式（严重）

| 文件 | 行数 | 函数数 | 问题 |
|------|------|--------|------|
| `voice_server.py` | 3,415 | 155 | 路由 + 业务逻辑 + 调度器 + SSE + CORS + 飞书推送 + 天气 + 提醒 全在一个文件 |
| `voice_agent.py` | 1,440 | ~40 | LLM 调用 + 缓存 + 音乐 + 天气 + 提醒 全在一个文件 |
| `app/xiaozhi_ws.py` | 915 | ~30 | WebSocket + 音频编解码 + VAD + 唤醒 全在一个文件 |

**建议拆分方案：**
- `voice_server.py` → `app/routes/` (按功能分组: chat, voice, reminders, setup, mcp, sse)
- `voice_agent.py` → `agent/llm.py`, `agent/music.py`, `agent/weather.py`
- `app/xiaozhi_ws.py` → `app/xiaozhi/connection.py`, `app/xiaozhi/audio.py`, `app/xiaozhi/vad.py`

### 1.2 模块组织

优点：
- `app/` 模块化较好（env_catalog, llm_config, cert, preflight 等职责清晰）
- `agent/` 子模块化合理（cache, history, intent, preferences, retry, asr_tts）
- 20+ `magic-*.py` MCP 工具通过文件名连字符 + importlib 按路径加载，设计合理

缺点：
- `voice_server.py` 和 `voice_agent.py` 之间有循环引用风险（`from voice_agent import runtime_temp_audio_path`）
- 大量全局变量和模块级状态（`_scheduler_lock_handle`, `_suggest_state` 等）

## 2. 性能问题

### 2.1 阻塞 I/O 在异步上下文中（中等）

`voice_server.py` 在 FastAPI 异步路由中直接调用 `requests.get/post`（同步阻塞）：
- Line 671: `requests.post("https://open.feishu.cn/...")` — 飞书推送
- Line 855: `requests.post(...)` — TTS 合成
- Line 1020: `requests.get("https://restapi.amap.com/...")` — 天气查询
- Line 3366: `requests.get(f"http://{lan_ip}:...")` — OTA 检查

这些调用在 asyncio 事件循环中会阻塞整个服务器。

**建议：** 改用 `httpx.AsyncClient` 或 `aiohttp`。

### 2.2 time.sleep 在后台线程中（低）

22 处 `while True: time.sleep()` 轮询循环，虽然不在事件循环中，但：
- 唤醒检测 `local_wake.py:270` — `time.sleep(0.5)` 轮询
- 提醒调度 `voice_server.py:877` — `time.sleep(30)` 轮询
- 主动推送 `personalized_push.py:216` — `time.sleep(600)` 轮询

**建议：** 改用 `asyncio.Event` 或 `threading.Event` 实现可取消等待。

### 2.3 无界缓存（低）

`agent/cache.py` 的 `_cache` 字典使用 LRU（已限制 50 条），合理。
但 `voice_server.py` 的 SSE 客户端队列 `_client_queues` 无上限检查。

## 3. 代码质量

### 3.1 异常吞没（严重 — 184 处）

全项目 **184 处** `except Exception:` 或 `except Exception: pass`，大多数不记录日志。

示例（`voice_agent.py:939`）:
```python
except Exception:
    pass  # 静默吞没错误
```

**影响：**
- 生产环境出现错误时无法定位
- 状态不一致但程序继续运行
- 安全漏洞可能被掩盖

**建议：** 至少添加 `log.warning(f"...: {e}")` 或 `log.debug(...)`。

### 3.2 测试覆盖

- 12 个核心测试文件，112 个测试用例全部通过 ✅
- 但 `test_voice_server.py` (172KB) 和 `test_voice_agent.py` (60KB) 存在但 CI 不运行（太大/太慢）
- 4 个测试文件因缺少 opus 库在 import 时崩溃（已修复）

### 3.3 安全

优点：
- `test_security_fixes.py` 主动检查凭证泄露和死代码
- `.env.example` 不包含真实凭证
- `auth.py` 有 token 验证
- `DynamicCORSMiddleware` 动态 CORS

缺点：
- `except Exception: pass` 可能掩盖安全相关错误
- `tuya_api.py` 硬编码了大量设备控制逻辑

## 4. 已修复问题

| 修复 | 文件 | 影响 |
|------|------|------|
| UTF-8 编码 | `tests/test_security_fixes.py` | Windows GBK 环境下 2 个测试崩溃 |
| opus 库查找 | `charlie.spec` | Linux CI 因 `find_library` 返回相对路径失败 |
| 空默认值 | `app/env_catalog.py` | 4 个 env var 空值导致 `int("")`/`float("")` 崩溃 |
| pywebview 依赖 | `requirements.txt` | CI 构建缺少原生 GUI 窗口 |

## 5. 改进优先级

| 优先级 | 改进项 | 预期收益 |
|--------|--------|----------|
| P0 | 拆分 `voice_server.py` (3415行/155函数) | 可维护性大幅提升 |
| P0 | 给 184 处 `except Exception:` 加日志 | 生产可观测性 |
| P1 | 异步路由中的 `requests.*` → `httpx` | 并发性能提升 |
| P1 | 消除 voice_server ↔ voice_agent 循环引用 | 架构清晰 |
| P2 | 后台线程轮询 → Event 驱动 | 资源占用降低 |
| P2 | SSE 客户端队列上限 | 防内存泄漏 |
