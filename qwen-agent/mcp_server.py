"""迷你MCP服务器 - 模拟魔幻手机的部分能力"""
from mcp.server.fastmcp import FastMCP
from datetime import datetime
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

mcp = FastMCP("magic-phone")

@mcp.tool()
def get_current_time() -> str:
    """获取当前时间、日期和星期"""
    now = datetime.now()
    w = ['一','二','三','四','五','六','日'][now.weekday()]
    return f"现在是 {now.strftime('%Y年%m月%d日 %H:%M:%S')}，星期{w}"

@mcp.tool()
def search_charging_stations(city: str = "北京", count: int = 3) -> str:
    """搜索附近的充电桩(真实高德数据)。参数: city-城市, count-数量"""
    import os, requests
    AMAP = os.getenv("AMAP_KEY", os.getenv("AMAP_MAPS_API_KEY", "REDACTED"))
    try:
        r = requests.get("https://restapi.amap.com/v3/place/text",
            params={"keywords":"充电桩","city":city,"citylimit":"true","offset":min(count*3,20),
                    "page":1,"key":AMAP,"extensions":"base"}, timeout=10).json()
        pois = (r.get("pois") or [])[:count]
        if not pois: return f"在{city}没找到充电桩"
        lines = [f"在{city}找到{len(pois)}个真实充电桩(高德数据):"]
        for p in pois:
            line = f"• {p.get('name','?')} 地址:{p.get('address','') or '未知'}"
            tel = p.get('tel','')
            if tel and tel != '[]': line += f" 电话:{tel}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"充电桩查询失败:{e}。请用高德地图App查看。"

@mcp.tool()
def control_tesla_ac(action: str = "on", temperature: int = 22) -> str:
    """控制特斯拉空调。action: on/off, temperature: 温度"""
    return f"特斯拉空调已{'开启' if action=='on' else '关闭'}，设定温度{temperature}度。"

if __name__ == "__main__":
    mcp.run()
