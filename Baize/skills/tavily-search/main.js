const impl = require('./tavily');
const input = process.env.BAIZE_PARAMS ? JSON.parse(process.env.BAIZE_PARAMS) : {};
const params = input.params || input;
impl.main(params).then(r => {
  console.log(JSON.stringify(r));
}).catch(e => {
  console.log(JSON.stringify({ success: false, error: e.message }));
});
