/**
 * Deeplink 跳转技能
 * 根据用户意图生成美团/饿了么/淘宝/京东/拼多多/大众点评的搜索跳转链接
 * 手机端点击自动唤起对应App到搜索/下单页
 */
function buildLinks(intent, keyword, location) {
  const q = encodeURIComponent(keyword || '');
  const links = {};

  switch (intent) {
    case 'waimai': // 外卖
      links['美团外卖'] = `https://h5.waimai.meituan.com/waimai/mindex/search?query=${q}`;
      links['饿了么'] = `https://www.ele.me/search/${q}`;
      links['美团外卖App'] = `imeituan://www.meituan.com/waimai/mindex/search?query=${q}`;
      break;
    case 'food': // 餐厅/美食
      links['大众点评'] = `https://m.dianping.com/searchlist?keyword=${q}`;
      links['美团团购'] = `https://meituan.com/s/${q}/`;
      links['大众点评App'] = `dianping://searchlist?keyword=${q}`;
      break;
    case 'shopping': // 购物
      links['淘宝'] = `https://s.m.taobao.com/h5?q=${q}`;
      links['京东'] = `https://so.m.jd.com/ware/search.action?keyword=${q}`;
      links['拼多多'] = `https://mobile.yangkeduo.com/search_result.html?search_key=${q}`;
      links['淘宝App'] = `taobao://s.m.taobao.com/h5?q=${q}`;
      links['京东App'] = `openapp.jdmobile://virtual?params={"category":"jump","des":"productList","keyword":"${keyword||''}"}`;
      break;
    case 'grocery': // 生鲜/买菜
      links['美团买菜'] = `https://h5.waimai.meituan.com/waimai/mindex/search?query=${q}`;
      links['饿了么买菜'] = `https://www.ele.me/search/${q}`;
      break;
    case 'pharmacy': // 买药
      links['美团买药'] = `https://h5.waimai.meituan.com/waimai/mindex/search?query=${q}`;
      links['饿了么买药'] = `https://www.ele.me/search/${q}`;
      break;
    case 'ride': // 打车
      links['滴滴出行'] = `https://common.diditaxi.com.cn/webapp_landing?from=web`;
      links['滴滴App'] = `didipassenger://`;
      break;
  }
  return links;
}

async function main(params) {
  const intent = params.intent || params.type || 'shopping';
  const keyword = params.keyword || params.query || params.q || params.what || '';
  const location = params.location || params.address || '';

  if (!keyword && intent !== 'ride') {
    return { success: false, error: '缺少 keyword（要搜什么）' };
  }

  const links = buildLinks(intent, keyword, location);
  const entries = Object.entries(links);
  if (entries.length === 0) {
    return { success: false, error: `未知意图: ${intent}` };
  }

  // 生成可点击的链接列表
  const lines = entries.map(([name, url], i) =>
    `${i + 1}. ${name}\n   👉 ${url}`
  ).join('\n');

  const msg = `已为你找到"${keyword}"的跳转链接，点击即可跳转到对应App下单：\n\n${lines}\n\n💡 在手机浏览器打开链接会自动唤起App。`;

  return { success: true, message: msg, data: { intent, keyword, links } };
}
module.exports = { main };
