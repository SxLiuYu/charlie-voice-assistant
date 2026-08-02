---
name: browser-automation
description: "浏览器自动化操作 - 基于 ego-browser，继承 Chrome 登录态，支持打开网页、截图、提取内容、填表、搜索等"
version: "2.1.0"
author: "Baize Team"
capabilities:
  - browser
  - web-automation
  - screenshot
  - form-filling
  - content-extraction
  - login-state-inherited
  - headless
risk_level: "medium"
when_to_use: "当用户需要打开网页、浏览网站、提取网页内容、自动填表、网页截图、操作需登录的网站等浏览器操作时使用"
input_schema:
  type: object
  properties:
    action:
      type: string
      description: "操作类型：auto(默认,LLM生成脚本) | navigate | screenshot | extract | click | fill | script"
      enum: ["auto", "navigate", "screenshot", "extract", "click", "fill", "script"]
    task:
      type: string
      description: "自然语言任务描述（action=auto 时必需，LLM 会生成 ego-browser JS 脚本执行）"
    url:
      type: string
      description: "网页URL（navigate/screenshot/extract/click/fill 操作可选）"
    selector:
      type: string
      description: "CSS选择器（click/fill 操作使用，extract 可选用于精确定位）"
    value:
      type: string
      description: "填写内容（fill 操作使用）"
    script:
      type: string
      description: "自定义 ego-browser JS 脚本（action=script 时使用，直接执行）"
    taskSpaceName:
      type: string
      description: "task space 名称，默认 'baize'。同一 task space 跨调用复用浏览器上下文"
      default: "baize"
    timeout:
      type: number
      description: "总超时毫秒数，默认 90000"
      default: 90000
output:
  type: object
  properties:
    success:
      type: boolean
    data:
      type: object
      description: "操作结果数据（url/title/text/screenshot 等）"
    message:
      type: string
    output:
      type: string
      description: "ego-browser 原始输出（auto 模式）"
examples:
  - name: "搜索白泽AI助手"
    input:
      action: "navigate"
      url: "https://www.baidu.com/s?wd=白泽AI助手"
  - name: "提取网页内容"
    input:
      action: "extract"
      url: "https://example.com"
  - name: "自动搜索并提取结果"
    input:
      action: "auto"
      task: "在B站搜索'AI助手'，提取前5个视频标题和链接"
  - name: "填写表单"
    input:
      action: "fill"
      url: "https://example.com/form"
      selector: "#username"
      value: "testuser"
  - name: "截图"
    input:
      action: "screenshot"
      url: "https://www.baidu.com"
---

# Browser Automation Skill (ego-browser 驱动)

基于 [ego-lite](https://github.com/citrolabs/ego-lite) 的浏览器自动化技能。

## 核心优势

| 特性 | Playwright 旧版 | ego-browser 新版 |
|------|----------------|-------------------|
| 登录态 | ❌ 无 | ✅ 继承 Chrome 登录态 |
| 持久上下文 | ❌ 每次新 session | ✅ task space 跨调用复用 |
| LLM 调用 | 每步一次（15步循环）| 一次生成完整脚本 |
| 无头模式 | ✅ headless | ✅ --headless --no-startup-window |
| 隔离性 | ❌ | ✅ 独立 Space |

## 架构

```
白泽 skill loader
  └─ node main.js (BAIZE_PARAMS=JSON)
       └─ ego-browser nodejs <<'EOF'   (Mojo IPC)
            └─ ego lite app (--headless)
                 └─ Chromium (继承 Chrome 登录态)
```

## action 说明

### auto（默认）
LLM 根据自然语言 task 生成一段 ego-browser JS 脚本，通过 `ego-browser nodejs` 一次执行。

### navigate
导航到指定 URL，返回页面信息。

### screenshot
导航到 URL（可选）并截图。

### extract
提取页面文本内容，可选 selector 精确定位。

### click / fill
点击/填写指定元素。

### script
直接执行自定义 ego-browser JS 脚本。

## ego-browser API

执行环境已注入全局 `ego` 对象和 `ego.helpers`：

- `ego.helpers.gotoUrl(url)` — 导航
- `ego.helpers.pageInfo()` — `{url, title}`
- `ego.helpers.snapshotText()` — 页面文本快照
- `ego.helpers.captureScreenshot()` — 截图
- `ego.helpers.click(selector)` — 点击
- `ego.helpers.fillInput(selector, value)` — 填写
- `ego.helpers.waitForElement(selector)` — 等待元素
- `ego.helpers.wait(ms)` — 等待
- `ego.helpers.js(stringExpr)` — 执行 JS（**字符串表达式**，不是函数）

## 前置条件

1. ego lite app 已安装到 `/Applications/ego lite.app`
2. `ego-browser` CLI 在 `~/.local/bin/ego-browser`
3. ego lite onboarding 已完成（导入 Chrome 数据）
4. `~/.local/bin` 在 PATH 中

## 无头模式

main.js 会自动以 `--headless --no-startup-window` 启动 ego lite，无需桌面窗口。
ego-browser CLI 通过 Mojo IPC 连接，不依赖窗口。
