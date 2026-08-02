/**
 * 白泽 Web 服务启动脚本
 * 同时启动 API 服务(3000) + Web 静态服务(8080)
 */
require('dotenv').config();
const { createAPIServer } = require('./dist/interaction/api');
const { startWebServer } = require('./dist/interaction/webServer');

const API_PORT = process.env.API_PORT || 3000;
const WEB_PORT = process.env.WEB_PORT || 8080;

async function main() {
  console.log('🦌 启动白泽 Web 服务...\n');

  // 启动 API 服务
  const apiServer = createAPIServer({ port: API_PORT });
  apiServer.start();
  console.log(`  ✅ API 服务: http://localhost:${API_PORT}`);

  // 启动 Web 静态服务
  startWebServer(WEB_PORT);
  console.log(`  ✅ Web 界面: http://localhost:${WEB_PORT}`);
  console.log(`\n🎤 浏览器打开 http://localhost:${WEB_PORT} 即可使用语音对话\n`);
}

main().catch(err => {
  console.error('启动失败:', err);
  process.exit(1);
});
