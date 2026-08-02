#!/usr/bin/env node
/**
 * 浏览器自动化 - ego-browser 驱动版 (无头模式)
 *
 * 核心架构：
 * - ego lite app 以 --headless 模式运行，无需桌面窗口
 * - ego-browser CLI 通过 Mojo IPC 连接 ego app
 * - 继承 Chrome 登录态（onboarding 时导入）
 * - 持久 task space 跨轮对话复用浏览器上下文
 * - LLM 生成完整 JS 脚本一次执行，比逐步决策循环快 2.5×
 *
 * action=auto 模式（默认）：
 *   LLM 根据 task 描述生成一段 ego-browser JS 脚本，通过
 *   `ego-browser nodejs <<'EOF'` 一次执行
 *
 * action=navigate/screenshot/extract/click/fill/script：
 *   预设操作，不调 LLM，直接执行
 */

const { spawnSync } = require('child_process');
const path = require('path');
const os = require('os');

// 常量
const EGO_BROWSER = path.join(os.homedir(), '.local/bin/ego-browser');
const EGO_APP = '/Applications/ego lite.app';
const EGO_BINARY = path.join(EGO_APP, 'Contents/MacOS/ego lite');
const DEFAULT_TASK_SPACE = 'baize';

// 读取输入参数
let input = {};
try {
  const inputStr = process.env.BAIZE_PARAMS || process.argv[2] || '{}';
  const parsed = JSON.parse(inputStr);
  input = parsed.params || parsed;
} catch (e) {
  console.log(JSON.stringify({ success: false, error: '参数解析失败: ' + e.message }));
  process.exit(0);
}

const {
  task,
  url,
  action = 'auto',
  selector,
  value,
  script,
  taskSpaceName = DEFAULT_TASK_SPACE,
  timeout = 90000,
} = input;

// LLM 配置
const LLM_KEY = process.env.ALIYUN_API_KEY || process.env.OPENAI_API_KEY;
const LLM_URL = process.env.ALIYUN_API_KEY
  ? 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
  : 'https://api.openai.com/v1/chat/completions';
const LLM_MODEL = process.env.ALIYUN_API_KEY ? 'qwen-max' : 'gpt-4o';

// ---- 辅助函数 ----

function sleep(ms) {
  const start = Date.now();
  while (Date.now() - start < ms) { /* busy wait */ }
}

/** 确保 ego lite 在无头模式运行 */
function ensureEgoRunning() {
  try {
    const check = spawnSync('pgrep', ['-f', 'ego lite'], { encoding: 'utf8' });
    if (check.stdout.trim()) return;
  } catch (e) { /* ignore */ }

  try {
    spawnSync(EGO_BINARY, ['--headless', '--no-startup-window'], {
      detached: true, stdio: 'ignore',
    });
    let ready = false;
    for (let i = 0; i < 10; i++) {
      sleep(1000);
      const check = spawnSync('pgrep', ['-f', 'ego lite'], { encoding: 'utf8' });
      if (check.stdout.trim()) { ready = true; break; }
    }
    if (!ready) throw new Error('ego lite 启动超时');
  } catch (e) {
    throw new Error('无法启动 ego lite: ' + e.message);
  }
}

/** 运行 ego-browser nodejs 脚本 */
function runEgoBrowser(script) {
  try {
    const result = spawnSync(EGO_BROWSER, ['nodejs'], {
      input: script,
      encoding: 'utf8',
      timeout,
      maxBuffer: 10 * 1024 * 1024,
    });
    if (result.error && result.error.code === 'ETIMEDOUT') {
      return { success: false, error: '执行超时' };
    }
    if (result.status !== 0) {
      return { success: false, error: result.stderr || result.stdout || 'ego-browser 执行失败' };
    }
    // ego-browser routes console.log output to stderr, not stdout
    const output = result.stderr || result.stdout;
    return { success: true, stdout: output.trim() };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/**
 * 执行脚本（自动选择/创建 task space）
 *
 * 注意：每次 ego-browser nodejs 调用是独立会话，
 * 所以 list + create + use + execute 必须在同一次调用中完成。
 */
function executeInTaskSpace(script) {
  const fullScript = `
// 获取或创建 task space
const spaces = await ego.listTaskSpaces();
let tsId = null;
for (const s of (spaces.taskSpaces || [])) {
  if (s.name === '${taskSpaceName}' && s.id !== 0) { tsId = s.id; break; }
}
if (!tsId) {
  const created = await ego.createTaskSpace('${taskSpaceName}');
  tsId = created.id;
}
await ego.useTaskSpace(tsId);

${script}
`;
  return runEgoBrowser(fullScript);
}

/** 调用 LLM 生成脚本 */
async function callLLM(messages) {
  const response = await fetch(LLM_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${LLM_KEY}`,
    },
    body: JSON.stringify({
      model: LLM_MODEL,
      messages,
      temperature: 0.3,
      max_tokens: 2000,
    }),
  });
  const data = await response.json();
  return data.choices[0].message.content;
}

/** 生成 LLM 脚本（auto 模式） */
async function generateLLMScript(taskDesc, startUrl) {
  const systemPrompt = `你是 ego-browser 操作脚本生成器。根据任务描述生成一段 JavaScript 代码，通过 ego-browser 执行。

## ego-browser API
全局对象 \`ego\` 和 \`ego.helpers\` 已自动注入，task space 已选中。

### ego.helpers 核心方法
- await ego.helpers.gotoUrl(url)             // 导航到URL
- await ego.helpers.pageInfo()               // 获取页面信息 {url, title}
- await ego.helpers.snapshotText()            // 获取页面文本快照
- await ego.helpers.captureScreenshot()       // 截图
- await ego.helpers.click(selector)           // 点击元素 (CSS选择器)
- await ego.helpers.fillInput(selector, value) // 填写输入框
- await ego.helpers.waitForElement(selector)  // 等待元素出现
- await ego.helpers.js(stringExpression)      // 在页面执行JS，返回结果

### js() 使用注意
- js() 接收**字符串表达式**，不是函数
- 正确用法: \`await ego.helpers.js('document.title')\`
- 提取数据: \`await ego.helpers.js('JSON.stringify({count: document.querySelectorAll(".item").length})')\`

### 重要：不要使用 ego.helpers.wait() 有挂起 bug，用 setTimeout 替代
- \`await new Promise(r => setTimeout(r, 2000))\`  // 等待2秒

### 常用搜索URL
- 百度: https://www.baidu.com/s?wd=关键词
- B站: https://search.bilibili.com/all?keyword=关键词
- 淘宝: https://s.taobao.com/search?q=关键词
- 京东: https://search.jd.com/Search?keyword=关键词

## 规则
1. 脚本必须是纯 JavaScript（可直接执行）
2. 用 \`console.log(JSON.stringify({...}))\` 输出结果
3. 导航后用 \`await new Promise(r => setTimeout(r, 2000))\` 等页面加载（不要用 ego.helpers.wait）
4. 最后用 console.log 输出提取的数据
5. 不需要定义函数或 async wrapper，直接写语句

## 输出格式
只输出 JavaScript 代码，不要 markdown 代码块，不要解释。`;

  const userPrompt = `任务：${taskDesc}${startUrl ? `\n起始URL：${startUrl}` : ''}`;

  const response = await callLLM([
    { role: 'system', content: systemPrompt },
    { role: 'user', content: userPrompt },
  ]);

  // 提取代码块（如果有）
  const codeMatch = response.match(/```(?:javascript|js)?\n?([\s\S]*?)```/);
  return codeMatch ? codeMatch[1].trim() : response.trim();
}

/** 预设操作（不调 LLM） */
function executePreset() {
  const wait = `await new Promise(r => setTimeout(r, `;

  switch (action) {
    case 'navigate':
      if (!url) return { success: false, error: 'url 参数为空' };
      return executeInTaskSpace(`const { gotoUrl, pageInfo } = ego.helpers;
await gotoUrl('${url}');
${wait}2000));
const info = await pageInfo();
console.log(JSON.stringify({ success: true, data: info, message: '已导航到: ' + info.url }));`);

    case 'screenshot':
      return executeInTaskSpace(`const { gotoUrl, captureScreenshot, pageInfo } = ego.helpers;
${url ? `await gotoUrl('${url}');\n${wait}2000));` : ''}
const info = await pageInfo();
const screenshot = await captureScreenshot();
console.log(JSON.stringify({ success: true, data: { url: info.url, title: info.title, screenshot: screenshot }, message: '截图完成' }));`);

    case 'extract':
      return executeInTaskSpace(`const { gotoUrl, pageInfo, snapshotText, js } = ego.helpers;
${url ? `await gotoUrl('${url}');\n${wait}2000));` : ''}
const info = await pageInfo();
const text = await snapshotText();
${selector
  ? `const extracted = await js('JSON.stringify(Array.from(document.querySelectorAll("${selector}")).map(el => el.textContent.trim()).slice(0,20))');
console.log(JSON.stringify({ success: true, data: { url: info.url, title: info.title, text: text.slice(0, 3000), extracted: extracted }, message: '内容提取完成' }));`
  : `console.log(JSON.stringify({ success: true, data: { url: info.url, title: info.title, text: text.slice(0, 3000) }, message: '内容提取完成' }));`}`);

    case 'click':
      if (!selector) return { success: false, error: 'selector 参数为空' };
      return executeInTaskSpace(`const { gotoUrl, click, pageInfo } = ego.helpers;
${url ? `await gotoUrl('${url}');\n${wait}2000));` : ''}
await click('${selector}');
${wait}1000));
const info = await pageInfo();
console.log(JSON.stringify({ success: true, data: info, message: '已点击: ${selector}' }));`);

    case 'fill':
      if (!selector || value === undefined) return { success: false, error: 'selector 和 value 参数为空' };
      return executeInTaskSpace(`const { gotoUrl, fillInput, pageInfo } = ego.helpers;
${url ? `await gotoUrl('${url}');\n${wait}2000));` : ''}
await fillInput('${selector}', '${value}');
${wait}500));
const info = await pageInfo();
console.log(JSON.stringify({ success: true, data: info, message: '已填写: ${selector} = ${value}' }));`);

    case 'script':
      if (!script) return { success: false, error: 'script 参数为空' };
      return executeInTaskSpace(script);

    default:
      return null; // auto 模式
  }
}

/** 主执行函数 */
async function main() {
  try {
    // 确保 ego 运行
    ensureEgoRunning();

    // 预设操作
    if (action !== 'auto') {
      const result = executePreset();
      if (result !== null) {
        console.log(result.stdout || JSON.stringify({ success: false, error: result.error }));
        return;
      }
    }

    // auto 模式
    if (!task) {
      console.log(JSON.stringify({ success: false, error: 'task 参数为空（auto 模式需要 task 描述）' }));
      return;
    }

    const llmScript = await generateLLMScript(task, url);
    const result = executeInTaskSpace(llmScript);

    if (result.success) {
      console.log(JSON.stringify({
        success: true,
        message: '任务执行完成',
        llmGeneratedScript: llmScript,
        output: result.stdout,
      }));
    } else {
      console.log(JSON.stringify({
        success: false,
        error: result.error || 'ego-browser 执行失败',
        llmGeneratedScript: llmScript,
      }));
    }
  } catch (error) {
    console.log(JSON.stringify({ success: false, error: error.message }));
  }
}

main().catch(e => {
  console.log(JSON.stringify({ success: false, error: e.message }));
});