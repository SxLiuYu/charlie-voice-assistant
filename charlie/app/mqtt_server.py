"""xiaozhi MQTT 协议端 — 替代 WebSocket 的常驻连接方案（多设备支持）

ESP32 通过 OTA 切换到 MqttProtocol 后:
1. ESP32 常驻连接 MQTT broker
2. 用户唤醒 → ESP32 发 hello 到 charlie/esp32/{device_id}/up
3. 服务器回复 hello(含 UDP server/port/AES key/nonce)到 charlie/esp32/{device_id}/down
4. ESP32 建 UDP → 加密 Opus 双向传输
5. 对话结束 → goodbye → ESP32 回唤醒词模式（MQTT 仍保持）

主动推送: 服务器随时可通过 MQTT 推 JSON 消息到每个设备的 down topic

多设备支持:
- 订阅通配符 charlie/esp32/+/up
- 从 topic 动态提取 device_id
- 维护 {device_id: publish_topic} 和 {addr: device_id} 映射
- push_tts/push_notification 广播到所有活跃设备
"""
import os, json, asyncio, socket, struct, secrets, logging, threading, time, re, array
from collections import deque
from typing import Optional, Callable

log = logging.getLogger("magic")

# 常量
OPUS_FRAME_DURATION_MS = 60
UDP_AUDIO_HEADER_SIZE = 16
DOWNLINK_SAMPLE_RATE = 16000
UPLINK_SAMPLE_RATE = 16000

# abort 自动 clear 延迟（秒），可由环境变量快速调小以加速测试
_ABORT_AUTO_CLEAR_DELAY = float(os.getenv("MQTT_ABORT_CLEAR_DELAY", "2.0"))

# 端点检测参数（复用 xiaozhi_ws.py 的阈值）
MIN_SPEECH_FRAMES = 12        # 最少语音帧数 (~0.7s)
MAX_UTTERANCE_FRAMES = 600    # 最大语音时长 (~36s)
SILENCE_FRAMES_VAD = 8        # VAD确认静音帧数 (~0.48s)
NOISE_DROP_FRAMES = 200       # 无语音超时丢弃 (~12s)

# 活跃的 UDP 会话: {device_id: {"aes_key": bytes, "aes_nonce": bytes, "addr": (ip,port)}}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

# device_id → publish_topic（服务器→设备下行）
_device_topics: dict[str, str] = {}
_device_topics_lock = threading.Lock()

# addr → device_id（UDP 反向查找）
_addr_to_device: dict[tuple, str] = {}
_addr_to_device_lock = threading.Lock()

# 设备 topic 正则: charlie/esp32/<device_id>/up
_TOPIC_RE = re.compile(r"^charlie/esp32/([^/]+)/up$")


def _generate_aes_key_nonce() -> tuple[bytes, bytes]:
    """生成 16 字节 AES key 和 16 字节 nonce"""
    return secrets.token_bytes(16), secrets.token_bytes(16)


def _hex_encode(data: bytes) -> str:
    return data.hex()


def _aes_ctr_crypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """AES-CTR 加密/解密（对称操作）"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
        encryptor = cipher.encryptor()
        return encryptor.update(data) + encryptor.finalize()
    except ImportError:
        # 降级 pyaes（纯 Python）
        try:
            import pyaes
            aes = pyaes.AESModeOfOperationCTR(key, iv=nonce)
            return aes.encrypt(data) if isinstance(data, bytes) else aes.encrypt(data.encode())
        except ImportError:
            log.error("[mqtt-server] 需要 cryptography 或 pyaes")
            raise


def _build_audio_packet(aes_key: bytes, aes_nonce: bytes, payload: bytes, timestamp: int, sequence: int) -> bytes:
    """构造加密 UDP 音频包

    格式: |type 1|flags 1|payload_len 2(big)|ssrc 4|timestamp 4(big)|sequence 4(big)|encrypted_payload|
    aes_key = AES-128 密钥, aes_nonce = base nonce，bytes[2:4]=payload_len, bytes[8:12]=timestamp, bytes[12:16]=sequence
    """
    payload_len = len(payload)
    # 构造 per-packet nonce
    nonce = bytearray(aes_nonce)
    struct.pack_into("!H", nonce, 2, payload_len)       # payload_len big-endian
    struct.pack_into("!I", nonce, 8, timestamp)         # timestamp big-endian
    struct.pack_into("!I", nonce, 12, sequence)          # sequence big-endian

    # 加密 payload
    encrypted = _aes_ctr_crypt(aes_key, bytes(nonce), payload)

    # 构造完整包
    header = bytearray(UDP_AUDIO_HEADER_SIZE)
    header[0] = 0x01  # type = audio
    header[1] = 0x00  # flags
    struct.pack_into("!H", header, 2, payload_len)
    struct.pack_into("!I", header, 4, 0)  # ssrc
    struct.pack_into("!I", header, 8, timestamp)
    struct.pack_into("!I", header, 12, sequence)

    return bytes(header) + encrypted


def _extract_device_id(topic: str) -> str:
    """从 MQTT topic 提取 device_id"""
    m = _TOPIC_RE.match(topic)
    return m.group(1) if m else "unknown"


class MqttXiaozhiServer:
    """xiaozhi MQTT 协议服务端（多设备支持）

    负责:
    1. 连接 MQTT broker，订阅通配符 charlie/esp32/+/up
    2. 处理 hello → 回复 hello + UDP 配置（per-device）
    3. UDP 音频收发（按 addr→device_id 路由）
    4. 主动推送 TTS 到所有活跃设备
    """

    def __init__(self):
        self._client = None
        self._udp_sock: Optional[socket.socket] = None
        self._udp_port = 0
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 兼容旧代码: 保留单设备 ID（用于日志）
        self._device_id: str = ""
        self._opus_decoders: dict[str, "opuslib.Decoder"] = {}
        self._utterance_state: dict[str, dict] = {}
        self._opus_decoders_lock = threading.Lock()
        # per-device abort flag（用于中断当前播放 / brain 流）
        self._abort_flags: dict[str, threading.Event] = {}
        self._abort_flags_lock = threading.Lock()
        # per-device abort 世代：每次新活动（_handle_abort / _process_utterance）都会 +1，
        # 使旧的 auto-clear 线程失效，避免旧 clearer 吞掉新一轮 abort。
        self._abort_generations: dict[str, int] = {}
        self._abort_generations_lock = threading.Lock()

    def _get_opus_decoder(self, device_id: str) -> Optional["opuslib.Decoder"]:
        """Get or create a cached Opus decoder for the given device."""
        try:
            import opuslib
        except ImportError:
            return None
        with self._opus_decoders_lock:
            if device_id not in self._opus_decoders:
                self._opus_decoders[device_id] = opuslib.Decoder(UPLINK_SAMPLE_RATE, 1)
            return self._opus_decoders[device_id]

    def _get_abort_flag(self, device_id: str) -> threading.Event:
        """获取或创建 per-device abort flag（惰性创建，线程安全）。"""
        with self._abort_flags_lock:
            if device_id not in self._abort_flags:
                self._abort_flags[device_id] = threading.Event()
            return self._abort_flags[device_id]

    def _send_audio_packets(self, device_id: str, addr: tuple, aes_key: bytes, aes_nonce: bytes, opus_packets: list[bytes]) -> None:
        """发送加密 Opus 包到设备。

        seq 由 per-device 锁统一串行化：同设备的领号+发包全程持锁，
        保证包到达顺序与号序一致（同设备不应并发播放；发送 0.06s/帧期间
        阻塞同设备后续推送正是期望行为）。
        """
        with _sessions_lock:
            session = _sessions.get(device_id)
            if session is None or not opus_packets:
                return
            seq_lock = session.get("seq_lock")
            cur_seq = session.get("seq", 0)
            session["seq"] = (cur_seq + len(opus_packets)) & 0xFFFFFFFF
            ts_base = int(time.time() * 1000) & 0xFFFFFFFF
        lock = seq_lock if hasattr(seq_lock, "acquire") else threading.Lock()
        with lock:
            ts = ts_base
            for i, pkt in enumerate(opus_packets):
                seq = (cur_seq + i) & 0xFFFFFFFF
                packet = _build_audio_packet(aes_key, aes_nonce, pkt, ts + i * 60, seq)
                try:
                    self._udp_sock.sendto(packet, addr)
                except Exception as e:
                    log.warning(f"[mqtt-server] [{device_id}] UDP 发送失败: {e}")
                    break
                time.sleep(0.06)

    @property
    def udp_port(self) -> int:
        return self._udp_port

    def start(self, loop: asyncio.AbstractEventLoop):
        """启动 MQTT 服务端（在 FastAPI lifespan 中调用）"""
        broker = os.getenv("MQTT_BROKER", "")
        if not broker:
            log.info("[mqtt-server] MQTT_BROKER 未配置，跳过启动")
            return False

        self._loop = loop
        # 默认设备 ID（用于日志和兼容），实际设备 ID 从 topic 动态提取
        self._device_id = os.getenv("MQTT_DEVICE_ID", "esp32-default")
        port = int(os.getenv("MQTT_PORT", "1883"))
        user = os.getenv("MQTT_USER", "")
        password = os.getenv("MQTT_PASSWORD", "")
        # 通配符订阅：支持任意 device_id
        subscribe_topic = "charlie/esp32/+/up"

        # 1. 启动 UDP 音频服务（固定端口，Docker 可映射）
        udp_port = int(os.getenv("MQTT_UDP_PORT", "8888"))
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.bind(("0.0.0.0", udp_port))
        self._udp_port = udp_port
        self._running = True

        # UDP 接收线程
        threading.Thread(target=self._udp_recv_loop, daemon=True).start()

        # 2. 连接 MQTT broker
        try:
            import paho.mqtt.client as mqtt
            self._client = mqtt.Client(client_id=f"charlie-server-{os.getpid()}")
            if user:
                self._client.username_pw_set(user, password)
            self._client.on_connect = lambda c, u, f, rc, p=None: self._on_mqtt_connect(
                c, subscribe_topic, rc)
            self._client.on_message = lambda c, u, msg: self._on_mqtt_message(msg)
            self._client.connect(broker, port, 60)
            self._client.loop_start()
            log.info(f"[mqtt-server] 已连接 {broker}:{port}, UDP端口={self._udp_port}, 订阅={subscribe_topic}")
            return True
        except Exception as e:
            log.warning(f"[mqtt-server] 连接失败: {e}")
            self._running = False
            return False

    def _on_mqtt_connect(self, client, subscribe_topic, rc):
        """MQTT 连接成功 → 订阅通配符 topic"""
        client.subscribe(subscribe_topic)
        log.info(f"[mqtt-server] 已订阅 {subscribe_topic}（多设备）")

    def _on_mqtt_message(self, msg):
        """收到 ESP32 的 MQTT 消息（hello/listen/goodbye 等）"""
        try:
            payload = msg.payload.decode("utf-8")
            data = json.loads(payload)
            mtype = data.get("type", "")

            # 从 topic 提取 device_id
            device_id = _extract_device_id(msg.topic)
            if device_id == "unknown":
                log.warning(f"[mqtt-server] 无法从 topic 提取 device_id: {msg.topic}")
                return

            log.info(f"[mqtt-server] [{device_id}] 收到 {mtype}: {payload[:100]}")

            if mtype == "hello":
                # 过滤掉自己发出的 hello 回复（含 session_id），避免反馈循环
                if "session_id" in data:
                    return
                self._handle_hello(data, device_id, msg.topic)
            elif mtype == "listen":
                self._handle_listen(data, device_id)
            elif mtype == "goodbye":
                self._handle_goodbye(data, device_id)
            elif mtype == "abort":
                self._handle_abort(device_id)
        except Exception as e:
            log.warning(f"[mqtt-server] 消息处理失败: {e}")

    def _get_publish_topic(self, device_id: str, up_topic: str) -> str:
        """从 up_topic 推导 down topic: charlie/esp32/{device_id}/up → .../down"""
        return up_topic.replace("/up", "/down")

    def _register_device(self, device_id: str, up_topic: str):
        """注册设备映射"""
        down_topic = self._get_publish_topic(device_id, up_topic)
        with _device_topics_lock:
            _device_topics[device_id] = down_topic
        log.info(f"[mqtt-server] 注册设备 {device_id} → {down_topic}")

    def _unregister_device(self, device_id: str):
        """注销设备映射"""
        with _device_topics_lock:
            _device_topics.pop(device_id, None)
        with _addr_to_device_lock:
            # 清理该设备的所有 addr 映射
            addrs_to_remove = [a for a, d in _addr_to_device.items() if d == device_id]
            for a in addrs_to_remove:
                _addr_to_device.pop(a, None)

    def _get_publish_topic_for_device(self, device_id: str) -> str:
        """获取设备的下行 topic"""
        with _device_topics_lock:
            return _device_topics.get(device_id, "")

    def _handle_hello(self, data: dict, device_id: str, up_topic: str):
        """处理 hello → 回复 hello + UDP 配置（per-device）"""
        # 注册设备映射
        self._register_device(device_id, up_topic)

        # 生成 AES key/nonce
        aes_key, aes_nonce = _generate_aes_key_nonce()

        # 记录会话
        with _sessions_lock:
            _sessions[device_id] = {
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "addr": None,  # UDP 地址在收到第一个包时填充
                "timestamp": time.time(),
                "seq": 0,
                "seq_lock": threading.Lock(),
            }
        # 重置端点检测状态：设备重连（网络闪断后二次hello）时上一会话的
        # utterance_active/buf 残留会让新会话的帧走"说话中"分支，端点永不触发
        if device_id in self._utterance_state:
            st = self._utterance_state[device_id]
            st.update({"buf_frames": [], "speech_count": 0, "silence_count": 0,
                       "utterance_active": False, "hot_frames": 0})
            st["tail"].clear()
            log.info(f"[mqtt-server] [{device_id}] 重连，端点检测状态已重置")

        # 获取 LAN IP
        lan_ip = os.getenv("ESP32_OTA_IP", "")
        if not lan_ip:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("8.8.8.8", 80))
                    lan_ip = s.getsockname()[0]
                finally:
                    s.close()
            except Exception:
                lan_ip = "127.0.0.1"

        # 回复 hello 到该设备的 down topic
        down_topic = self._get_publish_topic(device_id, up_topic)
        response = {
            "type": "hello",
            "transport": "udp",
            "session_id": secrets.token_hex(8),
            "audio_params": {
                "format": "opus",
                "sample_rate": DOWNLINK_SAMPLE_RATE,
                "channels": 1,
                "frame_duration": OPUS_FRAME_DURATION_MS,
            },
            "udp": {
                "server": lan_ip,
                "port": self._udp_port,
                "key": _hex_encode(aes_key),
                "nonce": _hex_encode(aes_nonce),
            },
        }
        if self._client:
            self._client.publish(down_topic, json.dumps(response, ensure_ascii=False), qos=1)
            # 固件 v2.1.0 的 MqttProtocol 不订阅 down topic，只监听 publish_topic
            # 同时向 up_topic 发送以确保设备收到
            self._client.publish(up_topic, json.dumps(response, ensure_ascii=False), qos=1)
        log.info(f"[mqtt-server] [{device_id}] hello 回复: UDP {lan_ip}:{self._udp_port} (down+up)")

    def _handle_listen(self, data: dict, device_id: str):
        """处理 listen → 开始接收语音"""
        state = data.get("state", "")
        if state == "detect":
            log.info(f"[mqtt-server] [{device_id}] 唤醒: {data.get('text', '')}")
        elif state == "start":
            log.info(f"[mqtt-server] [{device_id}] 开始监听")
        elif state == "stop":
            log.info(f"[mqtt-server] [{device_id}] 停止监听，开始 ASR")

    def _handle_goodbye(self, data: dict, device_id: str):
        """处理 goodbye → 清理 UDP 会话和设备映射"""
        with _sessions_lock:
            _sessions.pop(device_id, None)
        with self._opus_decoders_lock:
            self._opus_decoders.pop(device_id, None)
        with self._abort_flags_lock:
            self._abort_flags.pop(device_id, None)
        with self._abort_generations_lock:
            self._abort_generations.pop(device_id, None)
        self._unregister_device(device_id)
        log.info(f"[mqtt-server] [{device_id}] goodbye: {data.get('session_id', '')}")

    def _handle_abort(self, device_id: str):
        """处理 abort → 中断当前播放/brain 流（世代化 auto-clear）"""
        abort_flag = self._get_abort_flag(device_id)
        with self._abort_generations_lock:
            self._abort_generations[device_id] = self._abort_generations.get(device_id, 0) + 1
            gen = self._abort_generations[device_id]
        abort_flag.set()
        log.info(f"[mqtt-server] [{device_id}] 中断播放 (gen={gen})")
        # 短延时后自动 clear，仅当世代未变时才 clear（避免旧 clearer 吞掉新一轮 abort）
        def _auto_clear():
            time.sleep(_ABORT_AUTO_CLEAR_DELAY)
            with self._abort_generations_lock:
                if self._abort_generations.get(device_id, 0) == gen:
                    abort_flag.clear()
        threading.Thread(target=_auto_clear, daemon=True).start()

    def _publish_to_device(self, device_id: str, text: str):
        """推 JSON 消息到指定设备的 down topic"""
        topic = self._get_publish_topic_for_device(device_id)
        if topic and self._client:
            self._client.publish(topic, text, qos=1)

    def _publish_to_all(self, text: str):
        """推 JSON 消息到所有活跃设备"""
        with _sessions_lock:
            device_ids = list(_sessions.keys())
        for did in device_ids:
            self._publish_to_device(did, text)

    def push_tts(self, text: str, opus_packets: list[bytes]) -> bool:
        """主动推送 TTS 到所有活跃设备

        1. MQTT 发 JSON 通知 TTS 开始
        2. UDP 发加密 Opus 帧（per-device）
        3. MQTT 发 JSON 通知 TTS 结束
        """
        with _sessions_lock:
            devices = list(_sessions.keys())
        if not devices:
            log.warning("[mqtt-server] 无活跃会话，无法推送 TTS")
            return False

        # 对每个设备分别发送
        success_count = 0
        for device_id in devices:
            with _sessions_lock:
                session = _sessions.get(device_id)
            if not session:
                continue
            addr = session.get("addr")
            if not addr:
                log.warning(f"[mqtt-server] [{device_id}] 无 UDP 地址，跳过音频")
                continue

            aes_key = session["aes_key"]
            aes_nonce = session["aes_nonce"]

            # MQTT 通知 TTS 开始
            self._publish_to_device(device_id, json.dumps({
                "type": "tts", "state": "start",
                "text": text,
                "voice": "zh-CN",
            }))

            def _send_audio(_aes_key=aes_key, _aes_nonce=aes_nonce, _addr=addr, _packets=opus_packets, _did=device_id):
                try:
                    self._send_audio_packets(_did, _addr, _aes_key, _aes_nonce, _packets)
                except Exception as e:
                    log.warning(f"[mqtt-server] [{_did}] UDP 发送异常: {e}")
                self._publish_to_device(_did, json.dumps({"type": "tts", "state": "stop"}))
                log.info(f"[mqtt-server] [{_did}] TTS 推送完成: {text[:30]} ({len(_packets)}帧)")
            threading.Thread(target=_send_audio, daemon=True).start()
            success_count += 1

        log.info(f"[mqtt-server] TTS 推送: {success_count}/{len(devices)} 个设备")
        return success_count > 0

    def push_notification(self, text: str):
        """推送纯文字通知（不播音频，仅显示在 ESP32 屏幕上）

        优先推送给有活跃 session 的设备；若无 session，也向已注册 topic
        的设备推送（设备连着 MQTT broker 但未发 hello 的待机态）。
        还会向 OTA 配置的默认 device_id 推送（覆盖从未发过 hello 的设备）。
        """
        payload = json.dumps({"type": "notification", "text": text}, ensure_ascii=False)
        # 1. 活跃 session 中的设备
        with _sessions_lock:
            session_devices = list(_sessions.keys())
        # 2. 已注册 topic 的设备（hello 过但 session 过期）
        with _device_topics_lock:
            topic_devices = list(_device_topics.keys())
        # 3. 默认设备 ID（OTA 返回的 device_id，覆盖从未 hello 的待机设备）
        default_did = os.getenv("MQTT_DEVICE_ID", "esp32-default")

        all_devices = set(session_devices) | set(topic_devices) | {default_did}
        for did in all_devices:
            self._publish_to_device_or_default(did, payload)
        log.info(f"[mqtt-server] 通知推送: {text[:30]} ({len(all_devices)} 设备)")

    def _publish_to_device_or_default(self, device_id: str, payload: str):
        """推送到设备 down topic，若无注册 topic 则用默认格式构造"""
        topic = self._get_publish_topic_for_device(device_id)
        if not topic:
            topic = f"charlie/esp32/{device_id}/down"
        if self._client:
            self._client.publish(topic, payload, qos=1)

    def _udp_recv_loop(self):
        """UDP 接收循环 — 接收 ESP32 发来的加密 Opus 音频，VAD 端点检测后走 ASR→LLM→TTS

        多设备支持: 通过 _addr_to_device 反向查找 device_id
        """
        log.info(f"[mqtt-server] UDP 接收循环启动 (port={self._udp_port})")

        # 每个设备的端点检测状态: {device_id: {...}}
        # 端点检测状态使用 self._utterance_state（与 hello 线程共享，重连时可重置）

        while self._running:
            try:
                data, addr = self._udp_sock.recvfrom(4096)
                if len(data) < UDP_AUDIO_HEADER_SIZE:
                    continue

                # 查找 device_id
                with _addr_to_device_lock:
                    device_id = _addr_to_device.get(addr)

                if not device_id:
                    # 未知 addr → 尝试匹配 addr=None 的会话（设备刚 hello 完首次发 UDP）
                    with _sessions_lock:
                        for did, sess in list(_sessions.items()):
                            if sess.get("addr") is not None:
                                continue
                            # 尝试用此会话的 key 解密第一个包来验证
                            try:
                                if data[0] != 0x01:
                                    continue
                                plen = struct.unpack_from("!H", data, 2)[0]
                                if len(data) != UDP_AUDIO_HEADER_SIZE + plen:
                                    continue
                                ts = struct.unpack_from("!I", data, 8)[0]
                                seq = struct.unpack_from("!I", data, 12)[0]
                                n = bytearray(sess["aes_nonce"])
                                struct.pack_into("!H", n, 2, plen)
                                struct.pack_into("!I", n, 8, ts)
                                struct.pack_into("!I", n, 12, seq)
                                decrypted = _aes_ctr_crypt(sess["aes_key"], bytes(n), data[UDP_AUDIO_HEADER_SIZE:])
                                # 验证解密成功: 用 opus 解码器试解码（TOC byte 因编码配置而异，
                                # 不能假设 0xFC——16k/60ms SILK/CELT 帧的实际 TOC 不同）
                                if decrypted:
                                    try:
                                        import opuslib
                                        opuslib.Decoder(UPLINK_SAMPLE_RATE, 1).decode(
                                            decrypted, frame_size=UPLINK_SAMPLE_RATE * 60 // 1000)
                                        ok = True
                                    except Exception:
                                        ok = False
                                    if ok:
                                        sess["addr"] = addr
                                        device_id = did
                                        with _addr_to_device_lock:
                                            _addr_to_device[addr] = did
                                        log.info(f"[mqtt-server] 自动匹配设备: {addr} → {did}")
                                        break
                            except Exception:
                                continue
                    if not device_id:
                        continue

                with _sessions_lock:
                    session = _sessions.get(device_id)
                    if not session:
                        continue
                    # 记录 addr→device_id 映射
                    with _addr_to_device_lock:
                        _addr_to_device[addr] = device_id
                    aes_key = session["aes_key"]
                    aes_nonce = session["aes_nonce"]

                    if data[0] != 0x01:
                        continue
                    payload_len = struct.unpack_from("!H", data, 2)[0]
                    timestamp = struct.unpack_from("!I", data, 8)[0]
                    sequence = struct.unpack_from("!I", data, 12)[0]
                    if len(data) != UDP_AUDIO_HEADER_SIZE + payload_len:
                        continue
                    nonce = bytearray(aes_nonce)
                    struct.pack_into("!H", nonce, 2, payload_len)
                    struct.pack_into("!I", nonce, 8, timestamp)
                    struct.pack_into("!I", nonce, 12, sequence)
                    encrypted = data[UDP_AUDIO_HEADER_SIZE:]
                    opus_frame = _aes_ctr_crypt(aes_key, bytes(nonce), encrypted)

                # ── 端点检测（复用 xiaozhi_ws.py 的逻辑）──
                frame_size = UPLINK_SAMPLE_RATE * 60 // 1000
                try:
                    import opuslib
                    decoder = self._get_opus_decoder(device_id)
                    if decoder is None:
                        raise RuntimeError("opuslib decoder unavailable")
                    raw_pcm = decoder.decode(opus_frame, frame_size=frame_size)
                except Exception as e:
                    log.debug("[mqtt] opus decode failed: %s", e)
                    continue

                # RMS 能量（直接吃裸 PCM，不带 WAV 头）
                samples = array.array('h', raw_pcm[:len(raw_pcm) - len(raw_pcm) % 2])
                if samples:
                    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                else:
                    rms = 0

                # Silero VAD（复用 xiaozhi_ws 的模型，避免重复加载）
                vad_speech = False
                try:
                    from app.xiaozhi_ws import _is_speech_vad, _load_silero_vad
                    vad_model = _load_silero_vad()
                    if vad_model:
                        vad_speech = _is_speech_vad(raw_pcm, rms, 0.5)
                    else:
                        vad_speech = rms > 500
                except Exception as e:
                    log.debug("[mqtt] VAD check failed, using RMS fallback: %s", e)
                    vad_speech = rms > 500

                is_hot = vad_speech or rms > 500
                self._diag_count = getattr(self, '_diag_count', 0) + 1
                if self._diag_count % 10 == 0:
                    log.info(f"[mqtt-diag] 帧#{self._diag_count} addr={addr} rms={rms} "
                             f"vad={vad_speech} hot={is_hot} active={st['utterance_active']} "
                             f"buf={len(st['buf_frames'])} data0={data[0]} len={len(data)}")

                # 初始化该设备的 utterance 状态
                if device_id not in self._utterance_state:
                    self._utterance_state[device_id] = {
                        "buf_frames": [], "speech_count": 0, "silence_count": 0,
                        "utterance_active": False, "hot_frames": 0,
                        "tail": deque(maxlen=12),
                    }
                st = self._utterance_state[device_id]

                if not st["utterance_active"]:
                    # 未开始说话：滚动缓冲 + 热帧计数
                    st["tail"].append(opus_frame)
                    if is_hot:
                        st["hot_frames"] += 1
                        if st["hot_frames"] >= 3:
                            # 语音开始
                            st["buf_frames"] = list(st["tail"])
                            st["speech_count"] = 1
                            st["silence_count"] = 0
                            st["utterance_active"] = True
                            st["hot_frames"] = 0
                            log.info(f"[mqtt-server] [{device_id}] speech start ({len(st['buf_frames'])} tail frames)")
                    else:
                        st["hot_frames"] = 0
                    continue

                # 说话中：收集帧直到静音
                st["buf_frames"].append(opus_frame)
                if is_hot:
                    st["speech_count"] += 1
                    st["silence_count"] = 0
                else:
                    st["silence_count"] += 1
                if len(st["buf_frames"]) % 15 == 0:
                    log.info(f"[mqtt-server] [{device_id}] 诊断: buf={len(st['buf_frames'])} "
                             f"speech={st['speech_count']} silence={st['silence_count']}")

                silence_limit = SILENCE_FRAMES_VAD
                capped = len(st["buf_frames"]) >= MAX_UTTERANCE_FRAMES
                noise_timeout = len(st["buf_frames"]) >= NOISE_DROP_FRAMES and st["speech_count"] < MIN_SPEECH_FRAMES

                if ((st["speech_count"] >= MIN_SPEECH_FRAMES and st["silence_count"] >= silence_limit)
                        or capped or noise_timeout):
                    had_speech = st["speech_count"] >= MIN_SPEECH_FRAMES
                    frames = list(st["buf_frames"])
                    st["buf_frames"] = []
                    st["speech_count"] = 0
                    st["silence_count"] = 0
                    st["utterance_active"] = False
                    st["hot_frames"] = 0

                    if not had_speech:
                        log.info(f"[mqtt-server] [{device_id}] no clear speech, ignoring")
                        continue

                    log.info(f"[mqtt-server] [{device_id}] endpoint: {len(frames)} frames")
                    # 异步处理语音（不阻塞 UDP 接收）
                    threading.Thread(
                        target=self._process_utterance,
                        args=(frames, device_id),
                        daemon=True
                    ).start()

            except OSError:
                if not self._running:
                    break
            except Exception as e:
                log.debug(f"[mqtt-server] UDP 接收异常: {e}")

    def _process_utterance(self, frames: list[bytes], device_id: str):
        """处理一段完整语音：Opus→WAV→ASR→LLM(流式)→TTS(逐句)→MQTT/UDP 下行"""
        try:
            from app.xiaozhi_codec import opus_decode_to_pcm, pcm_to_wav, mp3_to_opus_packets
            from voice_agent import asr, is_low_intent_asr, is_garbled_asr, tts_to_mp3
            from agent.intent import LOW_INTENT_ASR_REPLY, strip_wake_word

            with _sessions_lock:
                session = _sessions.get(device_id)
            if not session:
                return
            addr = session.get("addr")
            if not addr:
                return

            # 1. 解码 Opus → 裸 PCM，送 ASR 时再封装 WAV
            raw_pcm = opus_decode_to_pcm(frames, UPLINK_SAMPLE_RATE)
            if not raw_pcm:
                return
            wav = pcm_to_wav(raw_pcm, UPLINK_SAMPLE_RATE)

            # 2. ASR
            asr_text = asr(wav, "wav")
            asr_text = (asr_text or "").strip()
            if not asr_text or is_garbled_asr(asr_text):
                self._publish_to_device(device_id, json.dumps({"type": "stt", "text": ""}, ensure_ascii=False))
                return

            # 剥离唤醒词
            stripped = strip_wake_word(asr_text)
            if stripped:
                asr_text = stripped
            elif stripped == "":
                self._publish_to_device(device_id, json.dumps({"type": "stt", "text": ""}, ensure_ascii=False))
                return

            log.info(f"[mqtt-server] [{device_id}] ASR: {asr_text}")
            self._publish_to_device(device_id, json.dumps({"type": "stt", "text": asr_text}, ensure_ascii=False))

            # 3. LLM + TTS（流式分句）
            if is_low_intent_asr(asr_text):
                self._push_text_tts(LOW_INTENT_ASR_REPLY, device_id)
                return

            import voice_agent
            session_id = f"mqtt-{device_id}"
            abort_flag = self._get_abort_flag(device_id)
            # 新 utterance 启动：世代 +1 使旧 auto-clear 线程全部失效，再 clear flag
            with self._abort_generations_lock:
                self._abort_generations[device_id] = self._abort_generations.get(device_id, 0) + 1
            abort_flag.clear()
            any_pushed = False
            tts_start_sent = False
            tts_stop_sent = False

            def _ensure_tts_stop():
                nonlocal tts_stop_sent
                # 从未发过 tts start（如 brain 启动失败/整段无产出）就不补孤立 stop，
                # 兜底播报由 _push_text_tts 自带完整 start/sentence/stop 序列
                if not tts_start_sent or tts_stop_sent:
                    return
                tts_stop_sent = True
                try:
                    self._publish_to_device(device_id, json.dumps({"type": "tts", "state": "stop"}))
                except Exception as e:
                    log.debug(f"[mqtt-server] [{device_id}] tts stop 发送失败: {e}")

            try:
                stream = voice_agent.brain_stream_sentences(
                    asr_text, session_id=session_id, channel="voice", cancel_event=abort_flag
                )
            except Exception as e:
                log.warning(f"[mqtt-server] [{device_id}] brain_stream_sentences 启动失败: {e}")
                stream = iter([])

            try:
                for sentence, full_reply in stream:
                    if abort_flag.is_set():
                        log.info(f"[mqtt-server] [{device_id}] abort 中断流式输出")
                        break
                    if not sentence:
                        continue
                    try:
                        mp3_data = tts_to_mp3(sentence)
                    except Exception as e:
                        log.warning(f"[mqtt-server] [{device_id}] 单句 TTS 失败，跳过: {e}")
                        continue
                    if not mp3_data:
                        continue
                    try:
                        opus_packets = mp3_to_opus_packets(mp3_data, DOWNLINK_SAMPLE_RATE)
                        if not opus_packets:
                            continue
                        if not tts_start_sent:
                            self._publish_to_device(device_id, json.dumps({
                                "type": "tts", "state": "start",
                                "text": full_reply or sentence,
                                "voice": "zh-CN",
                            }))
                            tts_start_sent = True
                        self._publish_to_device(device_id, json.dumps({
                            "type": "tts", "state": "sentence_start",
                            "text": sentence,
                        }))
                        self._send_audio_packets(device_id, addr, session["aes_key"], session["aes_nonce"], opus_packets)
                        any_pushed = True
                    except Exception as e:
                        log.warning(f"[mqtt-server] [{device_id}] 单句推送失败，跳过: {e}")
                        continue
            except Exception as e:
                log.warning(f"[mqtt-server] [{device_id}] 流式输出异常: {e}")
            finally:
                _ensure_tts_stop()

            if not any_pushed:
                self._push_text_tts("语音处理失败了，请再试一次", device_id)

        except Exception as e:
            log.error(f"[mqtt-server] [{device_id}] 语音处理失败: {e}")
            try:
                self._push_text_tts("语音处理失败了，请再试一次", device_id)
            except Exception as e:
                log.debug(f"[app.mqtt_server] error at line 630: {e}")
                pass

    def _publish_stt(self, text: str, device_id: str):
        """推送 STT 结果到指定 ESP32（显示在屏幕上）"""
        self._publish_to_device(device_id, json.dumps({"type": "stt", "text": text}, ensure_ascii=False))

    def _push_text_tts(self, text: str, device_id: str):
        """纯文字 TTS（使用默认 TTS 引擎生成音频后推送）"""
        try:
            from voice_agent import tts_to_mp3
            mp3 = tts_to_mp3(text)
            if mp3:
                self._push_audio_tts(text, mp3, device_id)
            else:
                log.warning(f"[mqtt-server] [{device_id}] TTS 合成失败，下发文字提示")
                self._publish_stt("语音合成暂时不可用，请稍后再试", device_id)
        except Exception as e:
            log.warning(f"[mqtt-server] [{device_id}] TTS 失败: {e}")
            try:
                self._publish_stt("语音合成暂时不可用，请稍后再试", device_id)
            except Exception:
                pass

    def _push_audio_tts(self, text: str, mp3_data: bytes, device_id: str):
        """推送 TTS 音频到指定 ESP32：MQTT 通知 + UDP Opus 帧"""
        try:
            from app.xiaozhi_codec import mp3_to_opus_packets
            # hello 协商的采样率是 DOWNLINK_SAMPLE_RATE(16k)，
            # 编码必须一致，否则设备按 16k 解码 24k 流 → 变速变调
            opus_packets = mp3_to_opus_packets(mp3_data, DOWNLINK_SAMPLE_RATE)
            if not opus_packets:
                return
            # 只推给指定设备
            self.push_tts_single(text, opus_packets, device_id)
        except Exception as e:
            log.warning(f"[mqtt-server] [{device_id}] 推送 TTS 失败: {e}")

    def push_tts_single(self, text: str, opus_packets: list[bytes], device_id: str) -> bool:
        """主动推送 TTS 到单个设备"""
        with _sessions_lock:
            session = _sessions.get(device_id)
        if not session:
            log.warning(f"[mqtt-server] [{device_id}] 无活跃会话，无法推送 TTS")
            return False

        addr = session.get("addr")
        if not addr:
            log.warning(f"[mqtt-server] [{device_id}] 无 UDP 地址，跳过音频")
            return False

        aes_key = session["aes_key"]
        aes_nonce = session["aes_nonce"]

        # MQTT 通知 TTS 开始
        self._publish_to_device(device_id, json.dumps({
            "type": "tts", "state": "start",
            "text": text,
            "voice": "zh-CN",
        }))

        def _send_audio():
            try:
                self._send_audio_packets(device_id, addr, aes_key, aes_nonce, opus_packets)
            except Exception as e:
                log.warning(f"[mqtt-server] [{device_id}] UDP 发送异常: {e}")
            self._publish_to_device(device_id, json.dumps({"type": "tts", "state": "stop"}))
            log.info(f"[mqtt-server] [{device_id}] TTS 推送完成: {text[:30]} ({len(opus_packets)}帧)")
        threading.Thread(target=_send_audio, daemon=True).start()
        return True

    def device_count(self) -> int:
        """当前活跃设备数"""
        with _sessions_lock:
            return len(_sessions)

    def is_connected(self) -> bool:
        """MQTT 是否已连接且有活跃会话"""
        with _sessions_lock:
            return len(_sessions) > 0

    def stop(self):
        """停止服务"""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        if self._udp_sock:
            self._udp_sock.close()
        with _sessions_lock:
            _sessions.clear()
        with _device_topics_lock:
            _device_topics.clear()
        with _addr_to_device_lock:
            _addr_to_device.clear()
        log.info("[mqtt-server] 已停止")


# 全局单例
_server: Optional[MqttXiaozhiServer] = None


def get_server() -> Optional[MqttXiaozhiServer]:
    """获取 MQTT 服务端实例"""
    return _server


def init_server(loop: asyncio.AbstractEventLoop) -> bool:
    """初始化 MQTT 服务端（在 FastAPI lifespan 中调用）"""
    global _server
    if _server and _server.is_connected():
        return True
    if _server:
        try:
            _server.stop()
        except Exception as e:
            log.debug(f"[app.mqtt_server] error at line 741: {e}")
            pass
    _server = MqttXiaozhiServer()
    return _server.start(loop)


def push_tts_to_mqtt(text: str, mp3_data: bytes) -> bool:
    """通过 MQTT+UDP 推送 TTS 到所有活跃 ESP32 设备（供 _push_tts_to_xiaozhi 调用）

    MP3 → Opus → 加密 UDP → ESP32 播放
    """
    if not _server or not _server.is_connected():
        return False
    try:
        from app.xiaozhi_codec import mp3_to_opus_packets
        # 与 hello 协商的 DOWNLINK_SAMPLE_RATE 保持一致（同 _push_audio_tts）
        packets = mp3_to_opus_packets(mp3_data, DOWNLINK_SAMPLE_RATE)
        if not packets:
            log.warning("[mqtt-push] Opus 编码失败")
            return False
        return _server.push_tts(text, packets)
    except Exception as e:
        log.warning(f"[mqtt-push] 推送失败: {e}")
        return False
