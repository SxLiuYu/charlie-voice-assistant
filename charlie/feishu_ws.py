"""飞书 WebSocket 长连接客户端 — 不需要公网回调地址，不需要事件订阅配置。

通过 lark-oapi SDK 的 WebSocket 长连接模式接收消息事件，
用户在飞书中发消息时，SDK 会回调注册的 handler，
无需在飞书开发者后台配置事件订阅回调 URL。
"""
import os, json, logging, threading, asyncio, time, collections

log = logging.getLogger("magic")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_PUSH_OPEN_ID = os.getenv("FEISHU_PUSH_OPEN_ID", "")

_ws_client = None
_ws_thread = None
_started = False

# 消息去重缓存：LRU + TTL（300 秒，最多 200 条）
_PROCESSED_MSG_TTL = 300
_PROCESSED_MSG_MAX = 200
_processed_messages: collections.OrderedDict[str, float] = collections.OrderedDict()

# tenant_access_token 缓存（7200 秒 = 2 小时，飞书 token 有效期）
_feishu_token_cache: dict[str, float | str] = {"token": "", "at": 0.0}
_feishu_token_lock = threading.Lock()


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


def _get_tenant_access_token() -> str:
    """获取飞书 tenant_access_token（带缓存，默认 7200 秒有效）。"""
    with _feishu_token_lock:
        now = time.time()
        if _feishu_token_cache["token"] and (now - _feishu_token_cache["at"] < 7000):
            return _feishu_token_cache["token"]
        try:
            import requests as _req
            r = _req.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                timeout=10,
            )
            token = r.json().get("tenant_access_token", "")
            if token:
                _feishu_token_cache["token"] = token
                _feishu_token_cache["at"] = now
                return token
        except Exception as e:
            log.error(f"[feishu-ws] 获取 token 失败: {e}")
        return ""


def _send_text_reply(chat_id: str, text: str) -> bool:
    """通过飞书 API 发送文本回复，返回是否发送成功。"""
    token = _get_tenant_access_token()
    if not token:
        log.error("[feishu-ws] 无法获取 token")
        return False
    try:
        import requests as _req
        r = _req.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"[feishu-ws] 发送回复失败: {e}")
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

        # 只处理文本消息，非文本消息给出友好提示
        msg_type = msg.message_type if hasattr(msg, "message_type") else ""
        if msg_type != "text":
            chat_id = msg.chat_id if hasattr(msg, "chat_id") else ""
            log.info(f"[feishu-ws] 收到非文本消息 type={msg_type}，返回提示")
            _send_text_reply(chat_id, "我目前只能处理文字消息，请用文字描述你的需求。")
            return

        # 解析消息内容
        content_str = msg.content if hasattr(msg, "content") else "{}"
        try:
            content = json.loads(content_str)
            text = content.get("text", "").strip()
        except Exception:
            return

        if not text:
            return

        chat_id = msg.chat_id if hasattr(msg, "chat_id") else ""
        sender_id = ""
        if sender and hasattr(sender, "sender_id"):
            sid = sender.sender_id
            sender_id = sid.open_id if hasattr(sid, "open_id") else ""

        log.info(f"[feishu-ws] 收到消息 from={sender_id} chat={chat_id} text={text[:50]}")

        # 在新线程中处理（不阻塞 SDK 的事件循环）
        t = threading.Thread(
            target=_process_message_sync,
            args=(text, sender_id, chat_id),
            daemon=True,
        )
        t.start()

    except Exception as e:
        log.error(f"[feishu-ws] 消息处理异常: {e}")


def _process_message_sync(text: str, sender_id: str, chat_id: str):
    """同步处理消息：先检查决策反馈，再走 brain。"""
    try:
        reply = ""

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

        # 2. 未命中决策反馈 → 走正常 brain 对话
        if not reply:
            session_id = f"feishu_{sender_id}"
            try:
                from agent.llm import brain_stream_sentences
                gen = list(brain_stream_sentences(text, session_id=session_id, channel="feishu_text"))
                for sentence, _ in gen:
                    reply += sentence
            except Exception as e:
                log.error(f"[feishu-ws] brain 调用失败: {e}")

        if not reply:
            reply = "抱歉，我暂时无法回复，请稍后再试。"

        # 3. 发送回复
        _send_text_reply(chat_id, reply)
        log.info(f"[feishu-ws] 回复 sent={len(reply)}字")

    except Exception as e:
        log.error(f"[feishu-ws] 处理失败: {e}")


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
