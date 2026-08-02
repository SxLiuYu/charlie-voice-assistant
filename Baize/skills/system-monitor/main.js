#!/usr/bin/env node
/**
 * 系统监控技能 (JARVIS式设备状态)
 * CPU/内存/磁盘/系统负载/运行时间/网络
 */
const os = require('os');
const fs = require('fs');
const { execSync } = require('child_process');

function readBytes(file){
  try{ return fs.readFileSync(file,'utf8'); }catch{ return ''; }
}
// CPU使用率(采样)
function cpuUsage(){
  try{
    const stats1 = readBytes('/proc/stat');
    if(!stats1){
      // macOS: 用 top
      try{ const t=execSync("top -l 1 -n 0 2>/dev/null | grep 'CPU usage'",{timeout:3000}).toString(); return t.trim(); }catch{ return 'CPU: '+(os.loadavg().map((v,i)=>['1分','5分','15分'][i]+':'+v.toFixed(2)).join(' '))+' (load avg)'; }
    }
    return 'CPU load: '+os.loadavg().map((v,i)=>['1m','5m','15m'][i]+'='+v.toFixed(2)).join(' ');
  }catch{ return '无法获取CPU'; }
}

async function main(params){
  const action = params.action || 'all';
  const totalMem = os.totalmem(), freeMem = os.freemem();
  const usedMem = totalMem - freeMem;
  const memPct = (usedMem/totalMem*100).toFixed(1);
  const fmt = b => (b/1073741824).toFixed(1)+'GB';
  const uptime = os.uptime();
  const upStr = `${Math.floor(uptime/86400)}天${Math.floor(uptime%86400/3600)}小时${Math.floor(uptime%3600/60)}分`;
  const cpus = os.cpus();
  const cpuModel = cpus.length ? cpus[0].model : '未知';
  const cpuCores = cpus.length;

  let disk = '未知';
  try{
    const df = process.platform==='darwin'
      ? execSync("df -h / 2>/dev/null | tail -1",{timeout:3000}).toString().trim().split(/\s+/)
      : execSync("df -h / 2>/dev/null | tail -1",{timeout:3000}).toString().trim().split(/\s+/);
    if(df.length>=4) disk = `${df[2]}已用 / ${df[1]}总计 (剩余${df[3]})`;
  }catch{}

  const info = {
    hostname: os.hostname(),
    platform: `${os.type()} ${os.release()} (${os.platform()})`,
    cpuModel, cpuCores,
    cpu: cpuUsage(),
    memory: `${fmt(usedMem)} / ${fmt(totalMem)} (${memPct}%)`,
    disk,
    uptime: upStr,
    network: Object.entries(os.networkInterfaces()).filter(([k])=>!k.toLowerCase().includes('lo')).map(([k,v])=>`${k}: ${(v.find(x=>x.family==='IPv4')||{}).address||'无'}`).join(', '),
  };
  const msg = `🖥️ 系统状态 (${info.hostname})\n`+
    `• 系统: ${info.platform}\n`+
    `• CPU: ${info.cpuModel} ${info.cpuCores}核\n`+
    `• ${info.cpu}\n`+
    `• 内存: ${info.memory}\n`+
    `• 磁盘: ${info.disk}\n`+
    `• 运行时间: ${info.uptime}\n`+
    `• 网络: ${info.network}`;
  return { success:true, message: msg, data: info };
}
module.exports = { main };

// 入口
const _input = process.env.BAIZE_PARAMS ? JSON.parse(process.env.BAIZE_PARAMS) : {};
const _params = _input.params || _input;
main(_params).then(r => console.log(JSON.stringify(r))).catch(e => console.log(JSON.stringify({success:false,error:e.message})));
