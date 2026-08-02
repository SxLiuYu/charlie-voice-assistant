/** Tavily 搜索技能 - 为AI优化的网页搜索（支持域名过滤） */
const KEY = process.env.TAVILY_API_KEY;
async function main(params) {
  if (!KEY) return { success: false, error: '未配置 TAVILY_API_KEY' };
  const query = params.query || params.q;
  if (!query) return { success: false, error: '缺少 query 参数' };
  const maxResults = params.max_results || 5;
  const body = {
    api_key: KEY, query, max_results: maxResults,
    search_depth: params.search_depth || 'basic',
  };
  // 域名过滤：限定/排除某些站点（购物场景可限定电商/比价站）
  if (params.include_domains && Array.isArray(params.include_domains)) body.include_domains = params.include_domains;
  if (params.exclude_domains && Array.isArray(params.exclude_domains)) body.exclude_domains = params.exclude_domains;
  try {
    const r = await fetch('https://api.tavily.com/search', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.results) return { success: false, error: d.detail || '搜索失败' };
    const msg = d.results.map((r,i) => `${i+1}. ${r.title}\n   🔗 ${r.url}\n   ${r.content?.slice(0,150)||''}`).join('\n\n');
    return { success: true, message: `🔍 搜索"${query}"得到${d.results.length}条结果:\n\n${msg}`, data: d.results };
  } catch (e) { return { success: false, error: e.message }; }
}
module.exports = { main };
