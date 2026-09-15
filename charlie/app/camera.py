"""摄像头服务: 安卓旧手机(IP Webcam/DroidCam)作为局域网IP摄像头

环境变量 CAMERA_SOURCES 配置多个源:
    CAMERA_SOURCES=门口=http://192.168.1.101:8080,桌面=http://192.168.1.102:8080
单个源可简写 CAMERA_URL=http://192.168.1.101:8080（源名为 default）

IP Webcam 端点约定:
    {base}/shot.jpg  按需快照（grab_frame 用这个，IP Webcam/DroidCam 均支持）
    {base}/video     MJPEG 实时流（mjpeg_stream 用这个）

抓帧带最近帧缓存（帧龄 FRAME_CACHE_TTL 秒内直接复用），避免连续调用重复拉流。
所有路径容错: 未配置/超时/断流返回 None，不抛异常。
"""
import io
import os
import time
import json
import logging
import threading
import urllib.request
import urllib.parse
from urllib.error import URLError

log = logging.getLogger("magic")

FRAME_CACHE_TTL = float(os.getenv("CAMERA_FRAME_TTL", "2.0"))  # 帧缓存有效期(秒)
GRAB_TIMEOUT = float(os.getenv("CAMERA_GRAB_TIMEOUT", "6"))    # 抓帧超时(秒)
STREAM_RECONNECT = 3.0                                          # 流断开重连间隔(秒)

_lock = threading.Lock()
_sources: dict[str, str] | None = None   # name -> base_url（懒解析）
_last_frames: dict[str, tuple[bytes, float]] = {}  # name -> (jpeg, 缓存时间戳)
_frame_time: dict[str, float] = {}       # name -> 画面真实拍摄时间（报警快照=报警时间，实时流=抓帧时间）


def _parse_sources() -> dict[str, str]:
    """解析 CAMERA_SOURCES / CAMERA_URL 环境变量。"""
    raw = os.environ.get("CAMERA_SOURCES", "").strip()
    sources: dict[str, str] = {}
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            name, url = item.split("=", 1)
            name, url = name.strip(), url.strip().rstrip("/")
            if name and url:
                sources[name] = url
    if not sources:
        single = os.environ.get("CAMERA_URL", "").strip().rstrip("/")
        if single:
            sources["default"] = single
    return sources


def _get_sources() -> dict[str, str]:
    """返回当前源配置（每次动态读环境，支持运行中改 .env 后重载进程生效）。"""
    global _sources
    with _lock:
        parsed = _parse_sources()
        if parsed != _sources:
            if _sources is not None:
                log.info(f"[camera] 源配置变更: {_sources or {}} -> {parsed or {}}")
            _sources = parsed
    return _sources or {}


def list_sources() -> list[dict]:
    """源列表 + 最近帧状态（路由/健康检查用）。"""
    srcs = _get_sources()
    now = time.time()
    result = []
    for name, url in srcs.items():
        cached = _last_frames.get(name)
        result.append({
            "name": name,
            "base_url": url,
            "last_frame_age": round(now - cached[1], 1) if cached else None,
        })
    return result


# 局域网摄像头必须直连：系统 http_proxy 会劫持手机IP请求（表现为502/404）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "charlie-camera/1.0"})
    with _opener.open(req, timeout=timeout) as resp:
        return resp.read()


def grab_frame(name: str | None = None) -> bytes | None:
    """抓一帧 JPEG。name=None 时用第一个源。失败返回 None。"""
    srcs = _get_sources()
    if not srcs:
        return None
    if name is None or name not in srcs:
        name = next(iter(srcs))
    base = srcs[name]

    # 帧缓存：帧龄内直接复用，语音连问不重复拉
    cached = _last_frames.get(name)
    if cached and time.time() - cached[1] < FRAME_CACHE_TTL:
        return cached[0]

    with _lock:
        cached = _last_frames.get(name)
        if cached and time.time() - cached[1] < FRAME_CACHE_TTL:
            return cached[0]
        try:
            if base.startswith("ezviz://"):
                # 优先实时 HLS（需云加速），失败回落最新报警抓拍（免费，猫眼默认能力）
                jpeg = _ezviz_grab_frame(base)
                if jpeg:
                    _frame_time[name] = time.time()
                else:
                    serial = urllib.parse.urlparse(base).netloc or base[len("ezviz://"):]
                    snap = _ezviz_alarm_snapshot(serial)
                    if snap:
                        jpeg, _frame_time[name] = snap
            elif base.startswith("rtsp://"):
                jpeg = _rtsp_grab_frame(base)
                if jpeg:
                    _frame_time[name] = time.time()
            else:
                jpeg = _http_get(f"{base}/shot.jpg", GRAB_TIMEOUT)
                _frame_time[name] = time.time()
            if not jpeg or len(jpeg) < 100:
                log.warning(f"[camera] {name} 抓帧为空: {base} -> {len(jpeg) if jpeg else 0}B")
                return None
            _last_frames[name] = (jpeg, time.time())
            return jpeg
        except (URLError, TimeoutError, OSError) as e:
            log.warning(f"[camera] {name} 抓帧失败: {e}")
            return None


# ===== RTSP 源 (rtsp://user:pass@ip:554/...) =====
# 海康/萤石系 IPC 原生支持。ffmpeg 抓单帧（连接+取流+首帧，超时 GRAB_TIMEOUT+4）

def _rtsp_grab_frame(base: str) -> bytes | None:
    """rtsp:// 地址 → ffmpeg 抓一帧 JPEG。协议错误/认证失败返回 None。"""
    import subprocess
    try:
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error",
             "-rtsp_transport", "tcp",
             "-stimeout", str(int(GRAB_TIMEOUT * 1e6)),
             "-i", base,
             "-frames:v", "1", "-q:v", "3",
             "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=GRAB_TIMEOUT + 4)
        if r.returncode == 0 and r.stdout and len(r.stdout) > 100:
            return r.stdout
        err = r.stderr.decode(errors="ignore").strip()[:150]
        # 401 = 验证码/密码错误；Connection refused = IP 不对或设备离线
        log.warning(f"[camera] RTSP抓帧失败({base[:40]}...): {err}")
        return None
    except subprocess.TimeoutExpired:
        log.warning(f"[camera] RTSP抓帧超时: {base[:40]}...")
        return None


# ===== 萤石云开放平台源 (ezviz://设备序列号) =====
# 需要 .env: EZVIZ_APP_KEY / EZVIZ_APP_SECRET (open.ys7.com 创建应用获取)
# 流程: token获取(缓存7天) → v2/live/address/get 取HLS → ffmpeg 抓一帧

_EZVIZ_API = "https://open.ys7.com/api"
_ezviz_token: dict | None = None  # {"accessToken": str, "expire": float}


def _ezviz_token_file() -> str:
    data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR", os.getcwd())
    return os.path.join(data_dir, "ezviz_token.json")


def _load_ezviz_token() -> dict | None:
    """内存+文件两级缓存萤石 accessToken（有效期7天）。"""
    global _ezviz_token
    if _ezviz_token and time.time() < _ezviz_token.get("expire", 0) - 600:
        return _ezviz_token
    try:
        with open(_ezviz_token_file(), encoding="utf-8") as f:
            saved = json.load(f)
        if time.time() < saved.get("expire", 0) - 600:
            _ezviz_token = saved
            return _ezviz_token
    except (OSError, ValueError):
        pass
    app_key = os.environ.get("EZVIZ_APP_KEY", "").strip()
    app_secret = os.environ.get("EZVIZ_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        log.warning("[camera] 萤石源未配置 EZVIZ_APP_KEY/EZVIZ_APP_SECRET")
        return None
    try:
        body = urllib.parse.urlencode({"appKey": app_key, "appSecret": app_secret}).encode()
        req = urllib.request.Request(f"{_EZVIZ_API}/lapp/token/get", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with _opener.open(req, timeout=GRAB_TIMEOUT) as resp:
            out = json.loads(resp.read())
        if str(out.get("code")) != "200":
            log.warning(f"[camera] 萤石token获取失败: {out.get('msg')}")
            return None
        data = out["data"]
        _ezviz_token = {"accessToken": data["accessToken"],
                        "expire": float(data.get("expireTime", time.time() + 604800))}
        try:
            with open(_ezviz_token_file(), "w", encoding="utf-8") as f:
                json.dump(_ezviz_token, f)
        except OSError:
            pass
        return _ezviz_token
    except (URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        log.warning(f"[camera] 萤石token异常: {e}")
        return None


def _ezviz_live_address(serial: str) -> str | None:
    """取萤石设备 HLS 直播地址（猫眼/门铃为云设备，需走开放平台）。
    正确端点为 /lapp/live/address/get，且必须带 source=序列号:通道。
    设备未开云存/直播加速时返回 ret=60060（地址未绑定），视为无地址。"""
    token = _load_ezviz_token()
    if not token:
        return None
    body = urllib.parse.urlencode({
        "accessToken": token["accessToken"],
        "deviceSerial": serial,
        "channel": 1,
        "protocol": 2,  # HLS（ffmpeg 可直接读）
        "quality": 0,   # 流畅优先，抓帧够用
        "source": f"{serial}:1",
    }).encode()
    req = urllib.request.Request(f"{_EZVIZ_API}/lapp/live/address/get", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _opener.open(req, timeout=GRAB_TIMEOUT) as resp:
        out = json.loads(resp.read())
    if str(out.get("code")) != "200":
        log.warning(f"[camera] 萤石直播地址失败({serial}): {out.get('code')} {out.get('msg')}")
        return None
    # data 是通道列表；取第一个通道的 HLS 地址
    for ch in out.get("data") or []:
        url = ch.get("hlsHd") or ch.get("hls")
        if url:
            return url
        ret = ch.get("ret")
        if ret and ret != "200":
            log.warning(f"[camera] 萤石直播地址未就绪({serial}): ret={ret} {ch.get('desc')}")
    return None


def _ezviz_grab_frame(base: str) -> bytes | None:
    """ezviz://SERIAL → HLS地址 → ffmpeg 抓一帧 JPEG。"""
    import subprocess
    serial = urllib.parse.urlparse(base).netloc or base[len("ezviz://"):]
    url = _ezviz_live_address(serial)
    if not url:
        return None
    try:
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-rw_timeout", str(int(GRAB_TIMEOUT * 1e6)),
             "-i", url, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=GRAB_TIMEOUT + 4)
        if r.returncode == 0 and r.stdout and len(r.stdout) > 100:
            return r.stdout
        err = r.stderr.decode(errors="ignore").strip()[:150]
        log.warning(f"[camera] 萤石抓帧失败({serial}): {err}")
        return None
    except subprocess.TimeoutExpired:
        log.warning(f"[camera] 萤石抓帧超时({serial})")
        return None


def _ezviz_alarm_snapshot(serial: str) -> tuple[bytes, float] | None:
    """取最新一条 PIR 报警抓拍（免云存，猫眼默认能力）。
    返回 (jpeg, 报警时间戳秒)；无报警/下载失败返回 None。"""
    token = _load_ezviz_token()
    if not token:
        return None
    body = urllib.parse.urlencode({
        "accessToken": token["accessToken"],
        "deviceSerial": serial,
        "channel": 1,
        "pageSize": 1,
    }).encode()
    req = urllib.request.Request(f"{_EZVIZ_API}/lapp/alarm/device/list", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _opener.open(req, timeout=GRAB_TIMEOUT) as resp:
            out = json.loads(resp.read())
    except (URLError, TimeoutError, OSError) as e:
        log.warning(f"[camera] 萤石报警列表异常({serial}): {e}")
        return None
    if str(out.get("code")) != "200":
        log.warning(f"[camera] 萤石报警列表失败({serial}): {out.get('code')} {out.get('msg')}")
        return None
    alarms = out.get("data") or []
    if not alarms or not alarms[0].get("alarmPicUrl"):
        log.warning(f"[camera] 萤石无报警抓拍({serial})")
        return None
    ts = float(alarms[0].get("alarmTime", 0))
    if ts > 1e12:  # 毫秒转秒
        ts /= 1000.0
    try:
        jpeg = _http_get(alarms[0]["alarmPicUrl"], GRAB_TIMEOUT)  # 302 重定向自动跟随
    except (URLError, TimeoutError, OSError) as e:
        log.warning(f"[camera] 萤石报警图下载失败({serial}): {e}")
        return None
    if not jpeg or len(jpeg) < 100:
        return None
    return jpeg, ts


def mjpeg_stream(name: str | None = None, max_fps: float = 5.0):
    """解析 {base}/video 的 MJPEG multipart 流，yield JPEG bytes。

    生成器内部自动重连（STREAM_RECONNECT 间隔），调用方断开时生成器退出。
    """
    srcs = _get_sources()
    if not srcs:
        return
    if name is None or name not in srcs:
        name = next(iter(srcs))
    base = srcs[name]
    min_interval = 1.0 / max_fps if max_fps > 0 else 0
    last_yield = 0.0

    while True:
        try:
            req = urllib.request.Request(f"{base}/video", headers={"User-Agent": "charlie-camera/1.0"})
            with _opener.open(req, timeout=GRAB_TIMEOUT) as resp:
                buf = b""
                boundary = None
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    # 首次从 Content-Type 或数据中探测 boundary
                    if boundary is None:
                        ctype = resp.headers.get("Content-Type", "")
                        if "boundary=" in ctype:
                            boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
                        else:
                            m = buf[:200].split(b"\r\n")[0]
                            if m.startswith(b"--"):
                                boundary = m
                    if boundary is None:
                        continue
                    # 按 boundary 分帧
                    frames = _split_mjpeg(buf, boundary)
                    if len(frames) >= 2:
                        for frame in frames[:-1]:  # 末段可能不完整，保留在 buf
                            now = time.time()
                            if now - last_yield >= min_interval:
                                jpg = _extract_jpeg(frame)
                                if jpg:
                                    last_yield = now
                                    yield jpg
                        buf = frames[-1]
        except (URLError, TimeoutError, OSError) as e:
            log.warning(f"[camera] {name} 流中断: {e}，{STREAM_RECONNECT}s后重连")
            time.sleep(STREAM_RECONNECT)


def _split_mjpeg(buf: bytes, boundary: bytes) -> list[bytes]:
    """按 boundary 切分 MJPEG 缓冲。"""
    delim = b"--" + boundary
    if not delim.endswith(b"--"):
        delim = delim
    if delim not in buf:
        return []
    parts = buf.split(delim)
    return parts[1:] if len(parts) > 1 else []


def _extract_jpeg(part: bytes) -> bytes | None:
    """从 multipart 段中提取 JPEG 数据（SOI~EOI 标记）。"""
    soi = part.find(b"\xff\xd8")
    eoi = part.rfind(b"\xff\xd9")
    if soi >= 0 and eoi > soi:
        return part[soi:eoi + 2]
    return None


def analyze_camera(name: str | None = None, question: str = "") -> str:
    """抓帧 + 视觉模型分析，返回文本（失败返回空串）。供快路径与 API 复用。"""
    from agent.vision import analyze_image_bytes
    srcs = _get_sources()
    if name is None or name not in srcs:
        name = next(iter(srcs), None) if srcs else None
    jpeg = grab_frame(name)
    if not jpeg:
        return ""
    q = question.strip() or "描述画面内容"
    # 报警快照非实时：画面拍摄时间早于当前 60 秒以上时向用户如实说明
    ts = _frame_time.get(name or "")
    if ts:
        age = time.time() - ts
        if age > 60:
            human = f"{int(age // 60)}分钟前" if age < 3600 else f"{int(age // 3600)}小时前"
            q = f"{q}\n注意: 该画面是{human}最后一次触发抓拍的，不是实时画面。"
    return analyze_image_bytes(jpeg, q)
