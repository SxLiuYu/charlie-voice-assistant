"""飞书 WebSocket 长连接客户端 — 不需要公网回调地址，不需要事件订阅配置。

通过 lark-oapi SDK 的 WebSocket 长连接模式接收消息事件，
用户在飞书中发消息时，SDK 会回调注册的 handler，
无需在飞书开发者后台配置事件订阅回调 URL。
"""
import os, json, logging, threading, asyncio, time, collections, sys, subprocess, tempfile

log = logging.getLogger("magic")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_PUSH_OPEN_ID = os.getenv("FEISHU_PUSH_OPEN_ID", "")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VISION_SCRIPT = os.path.join(PROJECT_DIR, "skills", "mimo-vision", "scripts", "vision.py")

_ws_client = None
_ws_thread = None
_started = False

# 消息去重缓存：LRU + TTL（300 秒，最多 200 条）
_PROCESSED_MSG_TTL = 300
_PROCESSED_MSG_MAX = 200
_processed_messages: collections.OrderedDict[str, float] = collections.OrderedDict()

# 同一 chat 的消息串行化，避免回复乱序
_chat_locks: dict[str, threading.Lock] = {}
_chat_locks_guard = threading.Lock()
_CHAT_LOCKS_MAX = 64


def _get_chat_lock(chat_id: str) -> threading.Lock:
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            if len(_chat_locks) >= _CHAT_LOCKS_MAX:
                _chat_locks.clear()
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]


# 统一使用 feishu_token 模块管理 token，避免多模块竞争
from feishu_token import get_tenant_access_token as _get_tenant_access_token, invalidate as _invalidate_token


def _is_duplicate_message(msg_id: str) -> bool:
    """检查消息是否已处理过（LRU + TTL）。"""
    now = time.time()
    # 清理过期条目
    expired = [k for k, ts in _processed_messages.items() if now - ts > _PROCESSED_MSG_TTL]
    for k in expired:
        _processed_messages.pop(k, None)
    if msg_id in _processed_messages:
        return True
    _processed_messages[msg_id] = now
    # 超出容量时移除最旧的
    if len(_processed_messages) > _PROCESSED_MSG_MAX:
        _processed_messages.popitem(last=False)
    return False


def _send_text_reply(chat_id: str, text: str) -> bool:
    """通过飞书 API 发送文本回复，返回是否发送成功。

    超长消息（>3500 字符）自动按句号/换行分段发送，避免飞书 API 限制导致发送失败。
    """
    # 清理文本中的控制字符（保留换行和空格）
    clean = "".join(c for c in text if c == "\n" or c == " " or (ord(c) >= 0x20 and ord(c) != 0x7f))
    if not clean.strip():
        clean = "我没听明白，能再说一遍吗？"
    # 飞书单条文本消息上限约 4000 字符，留余量按 3500 分段
    FEISHU_MSG_MAX = 3500
    if len(clean) <= FEISHU_MSG_MAX:
        return bool(_send_single_feishu_msg(chat_id, clean))
    # 超长消息分段发送
    segments = _split_long_text(clean, FEISHU_MSG_MAX)
    all_ok = True
    for seg in segments:
        if not _send_single_feishu_msg(chat_id, seg):
            all_ok = False
    return all_ok


def _split_long_text(text: str, max_len: int) -> list[str]:
    """按句号/换行分割超长文本，每段不超过 max_len 字符。"""
    segments = []
    while len(text) > max_len:
        # 在 max_len 范围内找最后一个句号或换行
        cut = max_len
        for sep in ("。", "\n", "！", "？", "；", "."):
            idx = text.rfind(sep, 0, max_len)
            if idx > max_len // 2:
                cut = idx + len(sep)
                break
        segments.append(text[:cut])
        text = text[cut:]
    if text.strip():
        segments.append(text)
    return segments


def _send_single_feishu_msg(chat_id: str, clean: str) -> str:
    """发送单条飞书文本消息（不含分段逻辑）。返回 message_id 或空字符串。"""
    for attempt in range(2):
        token = _get_tenant_access_token()
        if not token:
            log.error("[feishu-ws] 无法获取 token")
            return ""
        try:
            import requests as _req
            r = _req.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": clean}),
                },
                timeout=15,
            )
            if r.status_code == 200:
                try:
                    return r.json().get("data", {}).get("message_id", "")
                except Exception:
                    return ""
            # 记录响应体以便诊断
            body = ""
            try:
                body = r.json()
            except Exception:
                body = r.text[:200]
            if r.status_code == 400 and attempt == 0:
                # 400 可能是 token 被其他模块挤掉，强制刷新后重试
                log.warning(f"[feishu-ws] 400 错误，强制刷新 token 重试: {body}")
                _invalidate_token()  # 统一失效缓存
                continue
            log.error(f"[feishu-ws] 发送回复失败: {r.status_code} {body}")
            return ""
        except Exception as e:
            log.error(f"[feishu-ws] 发送回复异常: {e}")
            return ""
    return ""


def _delete_feishu_msg(message_id: str) -> bool:
    """删除单条飞书消息。失败时静默降级，不阻塞主流程。"""
    if not message_id:
        return False
    try:
        token = _get_tenant_access_token()
        if not token:
            return False
        import requests as _req
        r = _req.delete(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.debug(f"[feishu-ws] 删除占位消息失败: {e}")
        return False


def _on_message_receive(data):
    """飞书消息接收回调 — 路由到 brain 处理并回复。"""
    try:
        # 从事件数据中提取消息内容
        event = data.event if hasattr(data, "event") else None
        if not event:
            return

        msg = event.message if hasattr(event, "message") else None
        if not msg:
            return

        # 过滤 bot 自身消息（sender_type == "app" 表示消息来自 bot 自己）
        sender = event.sender if hasattr(event, "sender") else None
        sender_type = ""
        if sender and hasattr(sender, "sender_type"):
            sender_type = sender.sender_type or ""
        if sender_type == "app":
            log.debug("[feishu-ws] 跳过 bot 自身消息 (sender_type=app)")
            return

        # 消息去重：基于 message_id
        msg_id = msg.message_id if hasattr(msg, "message_id") else ""
        if msg_id and _is_duplicate_message(msg_id):
            log.debug(f"[feishu-ws] 跳过重复消息: {msg_id}")
            return

        # 只处理文本/图片消息，非文本消息给出友好提示
        msg_type = msg.message_type if hasattr(msg, "message_type") else ""
        chat_id = msg.chat_id if hasattr(msg, "chat_id") else ""
        sender_id = ""
        if sender and hasattr(sender, "sender_id"):
            sid = sender.sender_id
            sender_id = sid.open_id if hasattr(sid, "open_id") else ""

        if msg_type == "image":
            content_str = msg.content if hasattr(msg, "content") else "{}"
            try:
                content = json.loads(content_str)
                image_key = content.get("image_key", "")
            except Exception as e:
                log.debug(f"[feishu-ws] error at line 128: {e}")
                image_key = ""
            if not image_key:
                _send_text_reply(chat_id, "图片消息解析失败，请重新发送。")
                return
            log.info(f"[feishu-ws] 收到图片消息 from={sender_id} chat={chat_id} image_key={image_key}")
            t = threading.Thread(
                target=_process_message_sync,
                args=("", sender_id, chat_id, msg_id, "image", image_key),
                daemon=True,
            )
            t.start()
            return

        if msg_type != "text":
            log.info(f"[feishu-ws] 收到非文本消息 type={msg_type}，返回提示")
            _send_text_reply(chat_id, "我目前只能处理文字消息，请用文字描述你的需求。")
            return

        # 解析消息内容
        content_str = msg.content if hasattr(msg, "content") else "{}"
        try:
            content = json.loads(content_str)
            text = content.get("text", "").strip()
        except Exception as e:
            log.debug(f"[feishu_ws] error at line 128: {e}")
            return

        if not text:
            return

        log.info(f"[feishu-ws] 收到消息 from={sender_id} chat={chat_id} text={text[:50]}")

        # 在新线程中处理（不阻塞 SDK 的事件循环）
        t = threading.Thread(
            target=_process_message_sync,
            args=(text, sender_id, chat_id, msg_id, "text", ""),
            daemon=True,
        )
        t.start()

    except Exception as e:
        log.error(f"[feishu-ws] 消息处理异常: {e}")


def _process_message_sync(text: str, sender_id: str, chat_id: str, msg_id: str = "", msg_kind: str = "text", image_key: str = ""):
    """同步处理消息：先检查决策反馈，再走 brain。"""
    timer = None
    _first_sent = threading.Event()
    _placeholder_msg_id = [""]
    try:
        # 文本消息才显示"正在思考…"占位；图片消息走 vision，耗时较长，省略占位。
        if msg_kind == "text":
            def _placeholder():
                if not _first_sent.is_set():
                    try:
                        _placeholder_msg_id[0] = _send_single_feishu_msg(chat_id, "正在思考…")
                    except Exception as e:
                        log.debug(f"[feishu-ws] 占位消息发送失败: {e}")
            timer = threading.Timer(3.0, _placeholder)
            timer.start()

        with _get_chat_lock(chat_id):
            reply = ""
            _streamed = False  # 是否已流式发送过首句

            # 1. 先检查是否是对待确认决策的回复
            try:
                from app import load_magic_module
                _dec = load_magic_module("magic_decisions", "magic-decisions.py")
                if _dec:
                    fb_reply = _dec.check_feedback(text, user_id=sender_id)
                    if fb_reply is not None:
                        reply = fb_reply
                        log.info(f"[feishu-ws] 决策反馈命中: {reply[:50]}")
            except Exception as e:
                log.debug(f"[feishu-ws] 决策反馈检测跳过: {e}")

            # 图片消息 → 调用 vision 脚本分析
            if not reply and msg_kind == "image" and image_key:
                try:
                    token = _get_tenant_access_token()
                    if not token:
                        raise RuntimeError("无法获取 tenant_access_token")
                    import requests as _req
                    img_resp = _req.get(
                        "https://open.feishu.cn/open-apis/im/v1/images/" + image_key,
                        headers={"Authorization": f"Bearer {token}"},
                        params={"image_type": "message"},
                        timeout=30,
                    )
                    if img_resp.status_code != 200:
                        raise RuntimeError(f"图片下载失败 status={img_resp.status_code}")
                    img_bytes = img_resp.content
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                            tmp.write(img_bytes)
                            tmp_path = tmp.name
                        r = subprocess.run(
                            [sys.executable, VISION_SCRIPT, tmp_path, "-q", "描述这张图片的内容"],
                            capture_output=True,
                            text=True,
                            timeout=60,
                            cwd=PROJECT_DIR,
                        )
                        if r.returncode == 0 and r.stdout.strip():
                            reply = r.stdout.strip()[:2000]
                        else:
                            reply = "图片识别暂时失败，请稍后再试。"
                    finally:
                        if tmp_path:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                except Exception as e:
                    log.error(f"[feishu-ws] 图片识别失败: {e}")
                    reply = "图片识别暂时失败，请稍后再试。"

            # 2. 未命中决策反馈/图片 → 走正常 brain 对话
            if not reply:
                session_id = f"feishu_{sender_id}" if sender_id else f"feishu_chat_{chat_id}"
                try:
                    from agent.llm import brain_stream_sentences
                    _buf = ""
                    for sentence, _ in brain_stream_sentences(text, session_id=session_id, channel="feishu_text"):
                        reply += sentence
                        _buf += sentence
                        # 首句立即发送，降低感知延迟；后续累积避免消息碎片
                        if not _streamed and _buf.strip():
                            _first_sent.set()
                            _send_text_reply(chat_id, _buf)
                            _streamed = True
                            _buf = ""
                    # 发送剩余累积内容
                    if _buf.strip():
                        _send_text_reply(chat_id, _buf)
                except Exception as e:
                    log.error(f"[feishu-ws] brain 调用失败: {e}")
                    if _streamed:
                        # 已流式发送部分内容，追加错误提示让用户知道回复中断
                        _send_text_reply(chat_id, "（回复中断了，请重试）")
                    reply = ""  # 清空部分结果，触发兜底

            if not reply:
                reply = "抱歉，我暂时无法回复，请稍后再试。"

            # 3. 发送回复（若已流式发送，仅记录日志）
            if not _streamed:
                _first_sent.set()
                ok = _send_text_reply(chat_id, reply)
                if ok:
                    log.info(f"[feishu-ws] 回复 sent={len(reply)}字")
                else:
                    log.error(f"[feishu-ws] 回复发送失败 reply={reply[:50]}")
            else:
                log.info(f"[feishu-ws] 流式回复完成 total={len(reply)}字")

    except Exception as e:
        log.error(f"[feishu-ws] 处理失败: {e}")
        try:
            _send_text_reply(chat_id, "抱歉，处理失败，请稍后再试。")
        except Exception as e2:
            log.debug(f"[feishu_ws] error at line 196: {e2}")
            pass
        log.warning("消息 %s 处理失败（已标记去重，不会重试）", msg_id)
    finally:
        if timer:
            timer.cancel()
        if _placeholder_msg_id[0]:
            try:
                _delete_feishu_msg(_placeholder_msg_id[0])
            except Exception:
                pass


def start():
    """启动飞书 WebSocket 长连接（在后台线程中运行）。"""
    global _ws_client, _ws_thread, _started

    if _started:
        log.debug("[feishu-ws] 已启动，跳过")
        return

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        log.info("[feishu-ws] 未配置 FEISHU_APP_ID/SECRET，跳过启动")
        return

    def _run():
        global _ws_client
        try:
            from lark_oapi.ws.client import Client
            from lark_oapi import EventDispatcherHandler
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
            from lark_oapi import LogLevel

            # 构建事件处理器（长连接模式不需要 encrypt_key/verification_token）
            handler = (
                EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(_on_message_receive)
                .build()
            )

            # 创建 WebSocket 客户端
            _ws_client = Client(
                app_id=FEISHU_APP_ID,
                app_secret=FEISHU_APP_SECRET,
                log_level=LogLevel.INFO,
                event_handler=handler,
            )

            log.info("[feishu-ws] WebSocket 长连接启动中...")
            _ws_client.start()  # 阻塞调用，自动重连

        except Exception as e:
            log.error(f"[feishu-ws] 启动失败: {e}")

    _ws_thread = threading.Thread(target=_run, daemon=True)
    _ws_thread.start()
    _started = True
    log.info("[feishu-ws] 后台线程已启动")


def is_running() -> bool:
    """检查 WebSocket 客户端是否在运行。"""
    return _ws_thread is not None and _ws_thread.is_alive()


def is_ever_started() -> bool:
    """是否曾经启动过（区分『进程刚起还没启动』与『线程退出需拉起』）。"""
    return _started
