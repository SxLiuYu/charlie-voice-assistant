/** 高德地图技能 - 兼容多种参数名 */
const KEY = process.env.AMAP_API_KEY;
const BASE = 'https://restapi.amap.com/v3';
async function api(url){const r=await fetch(url);const d=await r.json();if(d.status!=='1')throw new Error(`高德API错误:${d.info||'未知'}`);return d;}
async function geocode(address){const d=await api(`${BASE}/geocode/geo?address=${encodeURIComponent(address)}&key=${KEY}`);const g=d.geocodes[0];return{location:g.location,formatted:g.formatted_address};}
async function poiSearch(keyword,location,radius=3000){
  const d=await api(`${BASE}/place/around?location=${location}&keywords=${encodeURIComponent(keyword)}&radius=${radius}&offset=10&key=${KEY}`);
  return (d.pois||[]).map(p=>({name:p.name,address:p.address,location:p.location,distance:p.distance,tel:p.tel}));
}
async function routePlan(origin,destination){const d=await api(`${BASE}/direction/driving?origin=${origin}&destination=${destination}&key=${KEY}`);const p=d.route.paths[0];return{distance:`${p.distance}米`,duration:`${Math.round(p.duration/60)}分钟`};}
async function weather(city){const d=await api(`${BASE}/weather/weatherInfo?city=${city}&key=${KEY}`);return(d.lives||[]).map(w=>({city:w.city,weather:w.weather,temp:`${w.temperature}°C`,wind:`${w.winddirection}风${w.windpower}级`}));}

async function main(params){
  if(!KEY)return{success:false,error:'未配置AMAP_API_KEY'};
  // 兼容各种参数名
  const action=(params.action||params.operation||params.type||params.cmd||'poi_search').toLowerCase();
  const keyword=params.keyword||params.keywords||params.query||params.q||params.search||'';
  let location=params.location||params.center||params.lnglat||params.coordinate||params.latlng||'';
  const radius=params.radius||params.range||params.distance||3000;
  const address=params.address||params.place||params.location_name||params.city_name||keyword;
  const city=params.city||params.city_code||params.adcode||address;
  try{
    let result,msg;
    // POI搜索类（兼容多种action名）
    if(['poi_search','around_search','place_search','nearby_search','search','nearby','周边搜索'].includes(action)){
      if(!location&&address){const g=await geocode(address);location=g.location;}
      if(!keyword)return{success:false,error:'缺少搜索关键词'};
      result=await poiSearch(keyword,location,radius);
      msg=result.length?`🔍 在附近${radius}米内找到${result.length}个"${keyword}":\n`+result.slice(0,8).map((p,i)=>`${i+1}. ${p.name}\n   📍${p.address} ${p.location} 距${p.distance||'?'}米`).join('\n'):`未找到"${keyword}"`;
    }else if(['geocode','geo','address','地址转坐标'].includes(action)){
      result=await geocode(address);msg=`📍 ${address} → ${result.location}\n   ${result.formatted}`;
    }else if(['route','route_planning','direction','driving','navigate','路径规划','导航'].includes(action)){
      result=await routePlan(params.origin||location,params.destination);msg=`🚗 距离:${result.distance} 预计:${result.duration}`;
    }else if(['weather','forecast','天气'].includes(action)){
      result=await weather(city);msg=result.map(w=>`🌤️ ${w.city}:${w.weather} ${w.temp} ${w.wind}`).join('\n');
    }else{
      // 默认尝试POI搜索
      if(!location&&address){const g=await geocode(address);location=g.location;}
      if(keyword){result=await poiSearch(keyword,location,radius);msg=result.length?`🔍 找到${result.length}个"${keyword}":\n`+result.slice(0,8).map((p,i)=>`${i+1}. ${p.name}\n   📍${p.address} ${p.location} 距${p.distance||'?'}米`).join('\n'):`未找到`;}
      else return{success:false,error:`未知操作:${action}`};
    }
    return{success:true,message:msg,data:result};
  }catch(e){return{success:false,error:e.message};}
}
module.exports={main};
