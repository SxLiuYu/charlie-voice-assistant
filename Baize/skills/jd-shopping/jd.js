/**
 * 京东联盟商品搜索技能
 * 接口: jd.union.open.goods.query (需权限) + material.query (已开通)
 */
const APP_KEY = process.env.JD_APP_KEY;
const APP_SECRET = process.env.JD_APP_SECRET;

function sign(params) {
  const sk = Object.keys(params).sort();
  const kv = sk.map(k => `${k}${params[k]}`).join('');
  const crypto = require('crypto');
  return crypto.createHash('md5').update(APP_SECRET + kv + APP_SECRET, 'utf8').digest('hex').toUpperCase();
}

async function callApi(method, bizParam) {
  const ts = new Date().toLocaleString('zh-CN', {hour12:false}).replace(/\//g,'-');
  const paramJson = JSON.stringify(bizParam);
  const params = {
    app_key: APP_KEY, method, timestamp: ts,
    format: 'json', v: '1.0', sign_method: 'md5',
    '360buy_param_json': paramJson,
  };
  params.sign = sign(params);
  const qs = new URLSearchParams(params).toString();
  const r = await fetch(`https://api.jd.com/routerjson?${qs}`);
  return r.json();
}

// 关键词搜索（需 goods.query 权限）
async function searchGoods(keyword, pageSize = 10) {
  const resp = await callApi('jd.union.open.goods.query', {
    goodsReqDTO: { keyword, pageSize, pageIndex: 1 }
  });
  const key = 'jd_union_open_goods_query_responce';
  if (!resp[key]) return { error: '无响应' };
  let inner;
  try { inner = JSON.parse(resp[key].queryResult); } catch { inner = JSON.parse(resp[key].getResult); }
  if (inner.code === 403) return { error: '关键词搜索接口权限未开通，请去京东联盟后台申请 goods.query 权限' };
  return extractGoods(inner.data || []);
}

// 频道推荐（已开通，不支持关键词）
async function recommendGoods(eliteId = 1, pageSize = 10) {
  const resp = await callApi('jd.union.open.goods.material.query', {
    goodsReq: { eliteId, pageSize, pageIndex: 1 }
  });
  const key = 'jd_union_open_goods_material_query_responce';
  if (!resp[key]) return { error: '无响应' };
  const inner = JSON.parse(resp[key].queryResult);
  return extractGoods(inner.data || []);
}

function extractGoods(goods) {
  return goods.map(g => {
    const price = g.priceInfo || {};
    const img = g.imageInfo || {};
    const imgs = img.imageList || [];
    const shop = g.shopInfo || {};
    return {
      name: g.skuName || g.materialName || g.goodsName || '未知商品',
      price: price.lowestPrice || price.price || '未知',
      couponPrice: price.lowestCouponPrice || '',
      shop: shop.shopName || '',
      shopLevel: shop.shopLevel || '',
      commission: (g.commissionInfo || {}).commissionShare || 0,
      sales30: g.inOrderCount30Days || 0,
      image: imgs[0] ? imgs[0].url : '',
      link: g.materialUrl || '',
      isSelf: (g.skuTagList || []).some(t => t.name === '自营'),
      comments: g.comments || 0,
    };
  });
}

async function main(params) {
  if (!APP_KEY) return { success: false, error: '未配置 JD_APP_KEY/JD_APP_SECRET' };
  const action = params.action || 'search';
  const keyword = params.keyword || params.query || params.q || '';
  try {
    let goods, title;
    if (action === 'search' || keyword) {
      const r = await searchGoods(keyword, 10);
      if (r.error) return { success: false, error: r.error };
      goods = r; title = `京东搜索"${keyword}"`;
    } else {
      const r = await recommendGoods(params.eliteId || 1, 10);
      if (r.error) return { success: false, error: r.error };
      goods = r; title = '京东推荐商品';
    }
    if (!goods.length) return { success: true, message: `${title}: 未找到商品` };
    // 按性价比排序：价格升序+销量权重
    goods.sort((a,b) => (parseFloat(a.price)||9999) - (parseFloat(b.price)||9999));
    const msg = `${title} 找到${goods.length}个商品:\n\n` +
      goods.slice(0, 8).map((g,i) =>
        `${i+1}. ${g.name.slice(0,50)}\n   💰¥${g.price}${g.couponPrice ? '(券后¥'+g.couponPrice+')' : ''} ${g.isSelf?'🏷️自营':''}\n   🏪${g.shop} ${g.shopLevel?'评分'+g.shopLevel:''} | 30天售${g.sales30}件`
      ).join('\n\n');
    return { success: true, message: msg, data: goods };
  } catch(e) { return { success: false, error: e.message }; }
}
module.exports = { main };
