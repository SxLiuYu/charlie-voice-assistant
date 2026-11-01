"""本地 IoT：扫描范围内的 WiFi / 蓝牙 / LAN 设备，尝试接入与控制。

硬约束：
- **绝不**让 PC 去连别的 WiFi AP（会把 192.168.1.x 断网，音箱和服务器一起掉线）。
  WiFi 只做被动扫描（netsh），配网中的设备走 UDP SmartConfig 思路另行处理。
- 蓝牙射频没开时返回明确提示，不去瞎连。
- LAN 控制只对已登记/已发现的设备；不爆破、不连邻居内网。

发现结果落 `data/iot_devices.json`，语音/面板共用。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("magic")

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "data" / "iot_devices.json"
_STORE.parent.mkdir(parents=True, exist_ok=True)

# LC-S3 小智音箱（已知）。Windows ARP 是 `aa-bb-cc` 格式，查表前归一成 `AA:BB:CC`。
_KNOWN_MAC = {
    "14:C1:9F:3A:9A:88": "小智音箱",
}


def _norm_mac(mac: str) -> str:
    return (mac or "").replace("-", ":").replace(".", "").strip().upper()

_TCP_PORTS = (
    (80, "http"), (8080, "http-alt"), (443, "https"), (5555, "adb"),
    (6668, "tuya"), (1883, "mqtt"), (8123, "hass"), (8000, "esphome"),
    (554, "rtsp"), (9000, "goke"), (34567, "hik"),
)

_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_STORE.read_text("utf-8"))
    except Exception:
        return {"ble": [], "wifi": [], "lan": [], "updated": 0}


def _save(data: dict) -> None:
    with _lock:
        try:
            data["updated"] = time.time()
            _STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception as e:
            log.warning("[iot] 保存失败: %s", e)


def ensure_bluetooth() -> bool:
    """尝试打开蓝牙射频。失败（需要用户手动）返回 False。"""
    try:
        import asyncio
        from winrt.windows.devices.radios import Radio, RadioState, RadioKind

        async def _on():
            radios = await Radio.get_radios_async()
            for r in radios:
                if r.kind == RadioKind.BLUETOOTH:
                    if r.state == RadioState.ON:
                        return True
                    try:
                        await r.set_state_async(RadioState.ON)
                    except Exception as e:
                        log.debug("[iot] set BT on fail: %s", e)
                    await asyncio.sleep(0.8)
                    return r.state == RadioState.ON
            return False

        return asyncio.run(_on())
    except Exception as e:
        log.debug("[iot] BT radio err: %s", e)
        return False


def bluetooth_on() -> bool:
    try:
        import asyncio
        from winrt.windows.devices.radios import Radio, RadioState, RadioKind

        async def _q():
            radios = await Radio.get_radios_async()
            for r in radios:
                if r.kind == RadioKind.BLUETOOTH:
                    return r.state == RadioState.ON
            return False

        return asyncio.run(_q())
    except Exception:
        return False


def scan_wifi() -> list[dict]:
    """被动扫描 WiFi（netsh），绝不 connect。中文 Windows 输出 GBK。"""
    try:
        r = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                           capture_output=True, timeout=10)
        text = r.stdout.decode("gbk", "replace")
    except Exception as e:
        log.warning("[iot] wifi scan fail: %s", e)
        return []
    nets: list[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SSID ") or line.startswith("SSID:"):
            if cur.get("ssid"):
                nets.append(cur)
            ssid = line.split(":", 1)[1].strip() if ":" in line else ""
            cur = {"ssid": ssid, "auth": "", "signal": "", "bssid": ""}
        elif line.startswith("身份验证") or line.lower().startswith("authentication"):
            cur["auth"] = line.split(":", 1)[1].strip() if ":" in line else ""
        elif line.startswith("信号") or line.lower().startswith("signal"):
            cur["signal"] = line.split(":", 1)[1].strip() if ":" in line else ""
        elif line.startswith("BSSID"):
            cur["bssid"] = line.split(":", 1)[1].strip() if ":" in line else ""
    if cur.get("ssid"):
        nets.append(cur)
    # 去重（同一 SSID 多 BSSID 保留最强）
    best: dict[str, dict] = {}
    for n in nets:
        ssid = n.get("ssid") or ""
        if not ssid:
            continue
        old = best.get(ssid)
        if old is None or _sig_num(n.get("signal")) > _sig_num(old.get("signal")):
            best[ssid] = n
    out = sorted(best.values(), key=lambda x: -_sig_num(x.get("signal")))
    log.info("[iot] WiFi 扫描 %d 个 SSID", len(out))
    return out


def _sig_num(s) -> int:
    try:
        return int(str(s).replace("%", "").strip() or "0")
    except Exception:
        return 0


def scan_ble(timeout: float = 6.0) -> list[dict]:
    """BLE 广播扫描。蓝牙没开返回 [{"error": "bluetooth_off"}]。"""
    if not bluetooth_on():
        if not ensure_bluetooth():
            log.info("[iot] 蓝牙射频未开")
            return [{"error": "bluetooth_off"}]
    try:
        import asyncio
        from bleak import BleakScanner

        async def _scan():
            # bleak 3.x：rssi 在 AdvertisementData，不在 BLEDevice 上
            return await BleakScanner.discover(timeout=timeout, return_adv=True)

        found = asyncio.run(_scan())
    except Exception as e:
        msg = str(e)
        if "POWERED_OFF" in msg or "not powered" in msg.lower():
            return [{"error": "bluetooth_off"}]
        log.warning("[iot] BLE scan fail: %s", e)
        return []
    out = []
    for addr, (d, adv) in (found or {}).items():
        out.append({
            "name": (adv.local_name if adv and adv.local_name else d.name) or "",
            "mac": d.address or addr,
            "rssi": getattr(adv, "rssi", None) if adv else None,
        })
    out.sort(key=lambda x: -(x.get("rssi") or -100))
    log.info("[iot] BLE 扫描 %d 台", len(out))
    return out


def _tcp_open(ip: str, port: int, timeout: float = 0.35) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _arp_hosts() -> list[tuple[str, str]]:
    """ARP 表里的本网段主机 [(ip, mac)]。"""
    hosts = []
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, timeout=5)
        text = r.stdout.decode("gbk", "replace")
    except Exception:
        return hosts
    for line in text.splitlines():
        if "192.168.1." not in line:
            continue
        parts = line.split()
        ip = next((p for p in parts if p.startswith("192.168.1.")), "")
        mac = next((p for p in parts if p.count("-") == 5 or p.count(":") == 5), "")
        if ip and mac and "ff-ff-ff-ff-ff-ff" not in mac.lower():
            hosts.append((ip, mac))
    return hosts


def scan_lan(timeout: float = 2.5) -> list[dict]:
    """LAN 服务发现：ARP + 常见 IoT 端口 TCP。不 ping（中文编码坑）、不连别的 AP。"""
    import ipaddress
    # 本机 IP 推断网段
    ip_self = "192.168.1.10"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip_self = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    net = ".".join(ip_self.split(".")[:3])
    ips = [f"{net}.{i}" for i in range(1, 255)]
    arp = dict(_arp_hosts())
    # ARP 已知主机优先；ARP 空才全段轻扫（80/8080/6668），别 254*11 拖 30s
    if arp:
        cand = list(arp.keys()) + [f"{net}.1", ip_self]
        light_only = False
    else:
        cand = ips
        light_only = True
    seen = set()
    cand = [x for x in cand if not (x in seen or seen.add(x))]

    def _probe(ip: str) -> dict | None:
        port_list = _TCP_PORTS if not light_only else ((80, "http"), (8080, "http-alt"), (6668, "tuya"))
        tmo = 0.4 if not light_only else 0.25
        ports = [p for p, name in port_list if _tcp_open(ip, p, tmo)]
        mac = arp.get(ip, "")
        if not ports and not mac:
            return None
        name = _KNOWN_MAC.get(_norm_mac(mac)) or ""
        dev = {"ip": ip, "mac": mac, "name": name, "ports": ports}
        if 80 in ports or 8080 in ports:
            dev["http"] = _http_probe(ip)
        return dev

    out = []
    with ThreadPoolExecutor(max_workers=40) as ex:
        for r in ex.map(_probe, cand):
            if r:
                out.append(r)
    # ARP 有但 TCP 全关的也留着（手机/休眠 IoT）
    for ip, mac in arp.items():
        if ip not in {d["ip"] for d in out}:
            out.append({"ip": ip, "mac": mac,
                        "name": _KNOWN_MAC.get(_norm_mac(mac)) or "",
                        "ports": []})
    out.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    log.info("[iot] LAN 发现 %d 台", len(out))
    return out


def _http_probe(ip: str) -> str:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://{ip}/", headers={"User-Agent": "Mozilla/5.0"}, timeout=1.5)
        body = urllib.request.urlopen(req).read(400)
        text = body.decode("utf-8", "replace")
        # 抠 <title>
        if "<title>" in text.lower():
            i = text.lower().find("<title>")
            return text[i + 7:text.lower().find("</title>", i)][:60].strip()
        return text[:60].replace("\n", " ").strip()
    except Exception as e:
        return type(e).__name__


def scan_all(ble_timeout: float = 6.0) -> dict:
    data = {
        "ble": scan_ble(ble_timeout),
        "wifi": scan_wifi(),
        "lan": scan_lan(),
    }
    old = _load()
    # BLE 保留历史名字（有的设备不广播名）
    prev = {d.get("mac"): d.get("name") for d in old.get("ble", []) if d.get("name")}
    for d in data["ble"]:
        if not d.get("name") and prev.get(d.get("mac")):
            d["name"] = prev[d.get("mac")]
    _save(data)
    return data


def _short_summary(data: dict) -> str:
    """语音短报：别念 MAC。"""
    bt_off = any(d.get("error") == "bluetooth_off" for d in data.get("ble", []))
    ble = [d for d in data.get("ble", []) if d.get("name") or d.get("mac")]
    ble_named = [d for d in ble if d.get("name")]
    wifi = [d for d in data.get("wifi", []) if d.get("ssid")]
    lan = [d for d in data.get("lan", []) if d.get("ip")]
    parts = []
    if bt_off:
        parts.append("蓝牙没开，请到设置里打开")
    elif ble:
        named = "、".join((d.get("name") or "未命名") for d in ble_named[:4])
        extra = len(ble) - len(ble_named)
        parts.append(f"蓝牙{len(ble)}台{('，' + named) if named else ''}"
                     f"{'，还有' + str(extra) + '台未命名' if extra > 0 else ''}")
    else:
        parts.append("蓝牙没扫到设备")
    parts.append(f"WiFi {len(wifi)}个"
                 + (f"，最强{wifi[0]['ssid']}" if wifi else ""))
    lan_named = [d for d in lan if d.get("name")]
    parts.append(f"局域网{len(lan)}台"
                 + (f"，包括{'、'.join(d['name'] for d in lan_named[:3])}" if lan_named else ""))
    return "。".join(parts) + "。"


def ble_connect(address: str) -> dict:
    """连接 BLE 并枚举服务/可写特征。不乱写。"""
    try:
        import asyncio
        from bleak import BleakClient

        from bleak import BleakScanner

        async def _c():
            # 广播设备不一定可连：先 find（5s），找不到别死等
            dev = await BleakScanner.find_device_by_address(address, timeout=5.0)
            if dev is None:
                return {"ok": False, "error": "不在广播（只扫到过，现在连不上）"}
            async with BleakClient(dev, timeout=6.0) as client:
                svcs = []
                for s in client.services:
                    chars = []
                    for c in s.characteristics:
                        props = ",".join(c.properties) if c.properties else ""
                        chars.append({"uuid": str(c.uuid), "props": props})
                    svcs.append({"uuid": str(s.uuid), "chars": chars})
                return {"ok": True, "services": svcs}

        async def _guarded():
            return await asyncio.wait_for(_c(), timeout=14.0)

        return asyncio.run(_guarded())
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def ble_write(address: str, uuid: str, hex_payload: str) -> dict:
    try:
        import asyncio
        from bleak import BleakClient

        payload = bytes.fromhex(hex_payload.replace(" ", ""))

        async def _w():
            async with BleakClient(address) as client:
                await client.write_gatt_char(uuid, payload)
                return {"ok": True}

        return asyncio.run(_w())
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def try_control(name: str, action: str) -> str:
    """按名字控制已发现设备。action: on/off/open/close/connect/scan。

    只动登记过的/名字匹配的设备；不连陌生 WiFi AP。
    """
    data = _load()
    action = (action or "").strip().lower()
    key = (name or "").strip()
    if action in ("scan", "list", ""):
        return _short_summary(scan_all())

    # Tuya 云（有 key 才走）
    if os.getenv("TUYA_API_KEY", "").strip():
        try:
            from tuya_api import TuyaAPI
            api = TuyaAPI()
            devs = api.get_all_devices() or {}
            rows = devs.get("result") or []
            for d in rows:
                if key in (d.get("name") or ""):
                    # 开关类 DP 1
                    if action in ("on", "open"):
                        api.issue_properties(d.get("id"), {"switch_1": True})
                        return f"已打开{d.get('name')}。"
                    if action in ("off", "close"):
                        api.issue_properties(d.get("id"), {"switch_1": False})
                        return f"已关闭{d.get('name')}。"
        except Exception as e:
            log.debug("[iot] tuya cloud control fail: %s", e)

    # BLE：名字/别名匹配
    for d in data.get("ble", []):
        dn = d.get("name") or ""
        if key and (key in dn or dn in key):
            mac = d.get("mac")
            if not mac or mac.startswith("error"):
                continue
            if action == "connect":
                r = ble_connect(mac)
                if r.get("ok"):
                    return f"已连接{dn or mac}，发现{len(r.get('services') or [])}个服务。"
                return f"连接{dn or mac}失败：{r.get('error')}"
            if action in ("on", "off", "open", "close"):
                return _ble_switch(mac, dn, action in ("on", "open"))
    # LAN 名字
    for d in data.get("lan", []):
        dn = d.get("name") or ""
        if key and key in dn and d.get("ip"):
            if action in ("on", "open"):
                return f"{dn or d['ip']} 在局域网，还没学会控制它。"
            if action in ("off", "close"):
                return f"{dn or d['ip']} 在局域网，还没学会控制它。"
    # 名字没匹配到
    if action == "connect":
        return f"没找到叫{key}的设备，先说扫描设备。"
    return f"没找到叫{key}的设备。先说扫描设备，再试一次。"


def _ble_switch(mac: str, name: str, on: bool) -> str:
    """尝试常见 DIY 灯/插座的可写特征。不保证通。"""
    r = ble_connect(mac)
    if not r.get("ok"):
        return f"连接{name or mac}失败：{r.get('error')}"
    # 常见透传特征
    for svc in r.get("services") or []:
        for c in svc.get("chars") or []:
            props = (c.get("props") or "").lower()
            uuid = (c.get("uuid") or "").lower()
            if "write" in props and (
                    uuid.endswith("ff01") or uuid.endswith("ffe1")
                    or uuid.endswith("ff02") or "fff" in uuid):
                payload = "01" if on else "00"
                w = ble_write(mac, c["uuid"], payload)
                if w.get("ok"):
                    return f"{'已打开' if on else '已关闭'}{name or '设备'}。"
    return f"{name or '设备'}已连接，但没认出开关特征，说不上开还是关。"


def push_scan_result() -> None:
    """后台扫完把短报推给音箱。"""
    def _run():
        try:
            data = scan_all(ble_timeout=6.0)
            text = _short_summary(data)
            log.info("[iot] 扫描结果: %s", text)
            from voice_agent import tts_to_mp3
            from app.notifications import push_tts_to_xiaozhi
            audio = tts_to_mp3(text)
            if audio and len(audio) > 100:
                push_tts_to_xiaozhi(text, audio)
        except Exception as e:
            log.warning("[iot] 推送扫描结果失败: %s", e)

    threading.Thread(target=_run, daemon=True, name="iot-scan").start()
