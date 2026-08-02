/** amap 技能入口 - 白泽子进程协议：从 BAIZE_PARAMS 读参，stdout 输出结果 JSON */
const impl = require('./amap');
const input = process.env.BAIZE_PARAMS ? JSON.parse(process.env.BAIZE_PARAMS) : {};
const params = input.params || input;
impl.main(params).then(r => {
  console.log(JSON.stringify(r));
}).catch(e => {
  console.log(JSON.stringify({ success: false, error: e.message }));
});
