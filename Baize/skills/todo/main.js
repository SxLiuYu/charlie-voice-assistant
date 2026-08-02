/**
 * 待办与提醒技能 (JARVIS式主动提醒)
 * 本地JSON存储，支持添加/列出/完成/检查到期
 */
const fs = require('fs');
const path = require('path');
const STORE = path.join(__dirname, 'todos.json');

function load(){ try{ return JSON.parse(fs.readFileSync(STORE,'utf8')); }catch{ return []; } }
function save(d){ fs.writeFileSync(STORE, JSON.stringify(d,null,2)); }

// 中文时间解析 → ISO 时间戳
function parseTime(str) {
  if (!str) return null;
  const now = new Date();
  let target = new Date(now);
  let matched = false;
  // X分钟后 / X分后
  let mm = str.match(/(\d+)\s*分(?:钟)?后/);
  if (mm) { target.setMinutes(target.getMinutes()+parseInt(mm[1])); matched=true; }
  // X小时后
  let hh = str.match(/(\d+)\s*(?:小时|个小时)后/);
  if (hh) { target.setHours(target.getHours()+parseInt(hh[1])); matched=true; }
  // X天后
  let dd = str.match(/(\d+)\s*天后/);
  if (dd) { target.setDate(target.getDate()+parseInt(dd[1])); matched=true; }
  // 明天/后天/今天
  if (/明天/.test(str)) { target.setDate(target.getDate()+1); matched=true; }
  if (/后天/.test(str)) { target.setDate(target.getDate()+2); matched=true; }
  if (/大后天/.test(str)) { target.setDate(target.getDate()+3); matched=true; }
  // 点数: 9点 / 9点半 / 9点30分 / 14:30
  let tm = str.match(/(\d{1,2})\s*[点时:：]\s*(\d{0,2})\s*(分)?/);
  if (tm) {
    let h = parseInt(tm[1]), mi = 0;
    if (tm[2]) mi = parseInt(tm[2]);
    else if (/半/.test(str)) mi = 30;
    // 下午/晚上 +12
    if (/下午|晚上|傍晚/.test(str) && h < 12) h += 12;
    target.setHours(h, mi, 0, 0);
    matched = true;
    // 如果时间已过且没说明今天，默认推迟到明天
    if (target <= now && !/今天|今/.test(str)) target.setDate(target.getDate()+1);
  }
  return matched ? target.toISOString() : null;
}

async function main(params) {
  const action = params.action || 'add';
  if (action === 'add' || action === 'add' || params.text) {
    const text = params.text || params.task || params.content || '';
    const timeStr = params.time || params.remind || params.when || '';
    if (!text) return { success:false, error:'缺少待办内容(text)' };
    const todos = load();
    const due = parseTime(timeStr);
    const item = { id: Date.now(), text, time: timeStr || '', due: due, done: false, created: new Date().toISOString() };
    todos.push(item);
    save(todos);
    const when = due ? `，提醒时间: ${new Date(due).toLocaleString('zh-CN',{hour12:false})}` : (timeStr ? `（时间"${timeStr}"未解析出具体时刻，已存为普通待办）` : '');
    return { success:true, message: `✅ 已添加待办：${text}${when}\n当前共${todos.length}项未完成`, data: item };
  }
  if (action === 'list' || action === 'ls') {
    const todos = load().filter(t => !t.done);
    if (!todos.length) return { success:true, message: '📋 当前没有待办事项' };
    const msg = '📋 待办清单：\n' + todos.map((t,i) => {
      const due = t.due ? ` ⏰${new Date(t.due).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}` : (t.time ? ` (${t.time})` : '');
      return `${i+1}. ${t.text}${due}`;
    }).join('\n');
    return { success:true, message: msg, data: todos };
  }
  if (action === 'done' || action === 'finish' || action === 'complete') {
    const idx = parseInt(params.index || params.id || '1') - 1;
    const todos = load();
    const active = todos.filter(t => !t.done);
    if (idx < 0 || idx >= active.length) return { success:false, error:'序号无效' };
    const target = active[idx];
    target.done = true;
    save(todos);
    return { success:true, message: `✅ 已完成：${target.text}` };
  }
  if (action === 'check' || action === 'due') {
    const now = Date.now();
    const due = load().filter(t => !t.done && t.due && new Date(t.due).getTime() <= now);
    if (!due.length) return { success:true, message: '', data: [] };
    // 到期的标记已提醒
    const all = load();
    due.forEach(d => { const t = all.find(x=>x.id===d.id); if(t) t.done=true; });
    save(all);
    const msg = '⏰ 提醒！以下待办已到期：\n' + due.map(t => `• ${t.text}（${t.time||''}）`).join('\n');
    return { success:true, message: msg, data: due };
  }
  return { success:false, error:'未知操作: '+action };
}
module.exports = { main };

// 入口
const _input = process.env.BAIZE_PARAMS ? JSON.parse(process.env.BAIZE_PARAMS) : {};
const _params = _input.params || _input;
main(_params).then(r => console.log(JSON.stringify(r))).catch(e => console.log(JSON.stringify({success:false,error:e.message})));
