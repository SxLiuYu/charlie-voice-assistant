# ego-lite 浏览器集成 · 接力文档

## 完成状态

### 已完成
- [x] ego-lite app 安装 (`/Applications/ego lite.app`)
- [x] `ego-browser` CLI symlink (`~/.local/bin/ego-browser`)
- [x] onboarding 完成（手动导入 Chrome 数据）
- [x] `skills/browser-automation/main.js` — ego-browser 驱动版 (310行)
- [x] `skills/browser-automation/SKILL.md` — v2.1.0 文档
- [x] `skills/browser-automation/main.js.bak.playwright` — 原版备份
- [x] `dist/tools/browser.js` — Baize BrowserTool 已改为调用 main.js 后端
- [x] 端到端验证: navigate ✅ extract ✅ screenshot ✅ close ✅

### 验证结果 (2026-07-31)
| 操作 | 目标 | 结果 | 耗时 |
|------|------|------|------|
| navigate | https://www.baidu.com | ✅ URL+标题 | 4.2s |
| extract | 百度搜索北京天气 | ✅ 语义快照 | 0.4s |
| screenshot | 当前页面 | ✅ PNG文件路径 | 0.3s |
| close | 关闭浏览器 | ✅ | 1ms |

## 架构
```
Baize LLM → BrowserTool (dist/tools/browser.js)
                    ↓ spawnSync
        skills/browser-automation/main.js
                    ↓ stdin pipe
            ego-browser nodejs
                    ↓ Mojo IPC
            ego lite app (--headless)
                    ↓ Chromium
                真实网页 (继承登录态)
```

## 已知坑点（4个）
1. **`ego.helpers.wait()` 在 macOS 26 挂起** → 用 `new Promise(r => setTimeout(r, ms))`
2. **console.log 输出到 stderr** → `runEgoBrowser()` 读 `result.stderr`
3. **每次 `ego-browser nodejs` 是独立无状态会话** → list+create+use+execute 必须在同一次调用
4. **ego lite 以 `--headless --no-startup-window` 启动** → `ensureEgoRunning()` 自动处理

## 前置条件（勿动）
- `/Applications/ego lite.app` — 已安装
- `~/.local/bin/ego-browser` — symlink 有效
- `~/.local/bin` 在 PATH 中
- onboarding 已完成

## 待办（接力）
- [ ] 在白泽运行环境中测试 `auto` 模式（需 LLM 配置 ALIYUN_API_KEY）
- [ ] 替换 `src/tools/browser.ts` 源码（当前只改了 dist 编译版）
- [ ] 跑 `npm run build` 确认 TypeScript 编译不覆盖 dist
- [ ] 在白泽 Web UI 中测试端到端浏览器操作
- [ ] 可选: kill 旧 Playwright/Puppeteer 进程释放资源
