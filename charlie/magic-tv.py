"""magic-tv: 电视控制 (DLNA/UPnP) — 小米/Redmi等支持DLNA的电视
5个工具: 查询状态/音量控制/播放暂停/停止/静音
设备通过 SSDP 自动发现（无需账号配对），IP 缓存10分钟自动刷新"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-tv",
    "tier": "core",
    "required_env": [],
    "label": "电视控制(DLNA音量/播放/状态)"
}

import json
import logging
import os
import re
import socket
import threading
import time

import requests
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("magic")
mcp = FastMCP("magic-tv")

# SSDP 发现缓存
_tv_cache: dict = {"ip": None, "port": 49152, "ts": 0.0, "name": ""}
_TV_CACHE_TTL = 600.0
_tv_lock = threading.Lock()


def _ssdp_discover(timeout: float = 4.0) -> dict | None:
    """SSDP 组播发现 DLNA 媒体渲染设备（电视），返回 {ip, port, name}"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    msg = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
           b'MAN: "ssdp:discover"\r\nMX: 2\r\nST: upnp:rootdevice\r\n\r\n')
    s.sendto(msg, ('239.255.255.250', 1900))
    candidates = {}
    try:
        while True:
            data, addr = s.recvfrom(4096)
            txt = data.decode(errors="ignore")
            # rootdevice 响应不含服务关键词，先收LOCATION再验证描述文档
            loc = ""
            for line in txt.split("\r\n"):
                if line.upper().startswith("LOCATION"):
                    loc = line.split(":", 1)[1].strip()
                    break
            if loc and addr[0] not in candidates:
                candidates[addr[0]] = loc
    except socket.timeout:
        pass
    finally:
        s.close()
    for ip, loc in candidates.items():
        try:
            desc = requests.get(loc, timeout=4).text
            if "AVTransport" in desc and "RenderingControl" in desc:
                m = re.match(r"http://([^/:]+):(\d+)", loc)
                return {"ip": ip, "port": int(m.group(2)) if m else 49152, "desc_url": loc}
        except Exception:
            continue
    return None


def _get_tv(force: bool = False) -> dict | None:
    """取电视连接信息（缓存TTL内直接用）。返回 {ip, port, name} 或 None。

    TV_IP 环境变量优先（SSDP组播在常驻服务进程内常收不到响应）；
    未配置时回退 SSDP 自动发现。"""
    with _tv_lock:
        now = time.time()
        if (not force and _tv_cache["ip"] and now - _tv_cache["ts"] < _TV_CACHE_TTL):
            return _tv_cache
        # 缓存过期先探活
        if _tv_cache["ip"]:
            c = socket.socket(); c.settimeout(2)
            try:
                c.connect((_tv_cache["ip"], _tv_cache["port"]))
                c.close()
                _tv_cache["ts"] = now
                return _tv_cache
            except OSError:
                pass
    tv_ip = os.environ.get("TV_IP", "").strip()
    if tv_ip:
        desc_url = f"http://{tv_ip}:49152/description.xml"
        try:
            desc = requests.get(desc_url, timeout=4).text
            if "AVTransport" in desc:
                import re as _re
                m = _re.search(r"<friendlyName>([^<]*)</friendlyName>", desc)
                _tv_cache.update({"ip": tv_ip, "port": 49152, "ts": time.time(),
                                  "name": m.group(1) if m else "电视"})
                log.info(f"[tv] TV_IP直连: {_tv_cache['name']} @ {tv_ip}")
                return _tv_cache
        except Exception as e:
            log.warning(f"[tv] TV_IP直连失败({tv_ip}): {e}")
    disc = _ssdp_discover()
    with _tv_lock:
        if disc:
            # 读 friendlyName
            name = "电视"
            try:
                r = requests.get(disc["desc_url"], timeout=4)
                m = re.search(r"<friendlyName>([^<]*)</friendlyName>", r.text)
                if m:
                    name = m.group(1)
            except Exception:
                pass
            _tv_cache.update({"ip": disc["ip"], "port": disc["port"], "ts": time.time(), "name": name})
            log.info(f"[tv] 发现DLNA渲染设备: {name} @ {disc['ip']}")
            return _tv_cache
    return None


def _soap(service: str, action: str, args: str = "") -> str | None:
    """调用 DLNA SOAP。service: RenderingControl|AVTransport"""
    tv = _get_tv()
    if not tv:
        return None
    url = f"http://{tv['ip']}:{tv['port']}/smartshare/render/_urn:schemas-upnp-org:service:{service}_control"
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">{args}</u:{action}>'
            f'</s:Body></s:Envelope>')
    try:
        r = requests.post(url, data=body, timeout=6,
                          headers={"Content-Type": 'text/xml; charset="utf-8"',
                                   f"SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'})
        return r.text
    except Exception as e:
        log.warning(f"[tv] SOAP {action} 失败: {e}")
        return None


def _soap_val(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml or "")
    return m.group(1) if m else None


@mcp.tool()
def get_tv_status() -> str:
    """查询电视状态: 是否在线/当前音量/播放状态。用户问"电视开着吗/音量多少"时调用。"""
    tv = _get_tv()
    if not tv:
        return "电视不在线（或未连接同一WiFi）。"
    vol_xml = _soap("RenderingControl", "GetVolume",
                    "<InstanceID>0</InstanceID><Channel>Master</Channel>")
    tra_xml = _soap("AVTransport", "GetTransportInfo", "<InstanceID>0</InstanceID>")
    vol = _soap_val(vol_xml, "CurrentVolume") if vol_xml else None
    state = _soap_val(tra_xml, "CurrentTransportState") if tra_xml else None
    state_cn = {"STOPPED": "已停止", "PLAYING": "播放中", "PAUSED_PLAYBACK": "已暂停",
                "TRANSITIONING": "切换中", "NO_MEDIA_PRESENT": "无媒体"}.get(state or "", state or "未知")
    return f"{tv.get('name', '电视')}在线。音量{vol if vol else '未知'}，状态：{state_cn}。"


@mcp.tool()
def set_tv_volume(level: int) -> str:
    """设置电视音量。level=0-100。用户说"电视音量调到X"时调用。"""
    level = max(0, min(100, int(level)))
    r = _soap("RenderingControl", "SetVolume",
              f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{level}</DesiredVolume>")
    if r is None:
        return "电视不在线，无法设置音量。"
    if "UPnPError" in r:
        return f"电视拒绝了音量设置（{_soap_val(r, 'errorDescription') or '未知错误'}）。"
    return f"电视音量已调到{level}。"


@mcp.tool()
def set_tv_mute(mute: bool) -> str:
    """电视静音/取消静音。mute=true静音，false取消。用户说"电视静音"时调用。"""
    val = "1" if mute else "0"
    r = _soap("RenderingControl", "SetMute",
              f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>{val}</DesiredMute>")
    if r is None:
        return "电视不在线。"
    return "电视已静音。" if mute else "电视已取消静音。"


@mcp.tool()
def tv_play_pause() -> str:
    """切换电视播放/暂停。用户说"电视暂停/继续播"时调用。"""
    tra_xml = _soap("AVTransport", "GetTransportInfo", "<InstanceID>0</InstanceID>")
    state = _soap_val(tra_xml, "CurrentTransportState") if tra_xml else ""
    action = "Pause" if state == "PLAYING" else "Play"
    r = _soap("AVTransport", action, "<InstanceID>0</InstanceID><Speed>1</Speed>")
    if r is None:
        return "电视不在线。"
    return "已暂停。" if action == "Pause" else "继续播放。"


@mcp.tool()
def tv_stop() -> str:
    """停止电视当前播放。用户说"电视停止"时调用。"""
    r = _soap("AVTransport", "Stop", "<InstanceID>0</InstanceID>")
    if r is None:
        return "电视不在线。"
    return "电视已停止播放。"


@mcp.tool()
def cast_to_tv(url: str, title: str = "") -> str:
    """把媒体文件投屏到电视。url=媒体文件的HTTP地址(mp4/mp3/图片), title=显示标题。
    注意: url必须是电视能访问的HTTP地址（局域网或公网）。"""
    if not url.strip():
        return "需要提供媒体地址。"
    meta = (f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="charlie-1" restricted="0"><dc:title>{title or "Charlie 投屏"}</dc:title>'
            f'<upnp:class>object.item.videoItem</upnp:class></item></DIDL-Lite>')
    import xml.sax.saxutils as sx
    safe_meta = sx.escape(meta)
    safe_url = sx.escape(url)
    r = _soap("AVTransport", "SetAVTransportURI",
              f"<InstanceID>0</InstanceID><CurrentURI>{safe_url}</CurrentURI>"
              f"<CurrentURIMetaData>{safe_meta}</CurrentURIMetaData>")
    if r is None:
        return "电视不在线，无法投屏。"
    _soap("AVTransport", "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
    return f"已把{title or '媒体'}投到电视上播放。"

if __name__ == "__main__":
    mcp.run()
