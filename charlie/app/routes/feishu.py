"""飞书双向对话 — 接收飞书消息并路由到 brain 处理"""
import os, json, logging, asyncio
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(tags=["feishu"])
log = logging.getLogger("magic")

# 飞书配置
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")

# 存活任务集，防止 GC 回收
_feishu_tasks: set[asyncio.Task] = set()


def _keep_task(task: asyncio.Task):
    _feishu_tasks.add(task)
    task.add_done_callback(_feishu_tasks.discard)


@router.post("/api/feishu/webhook")
async def feishu_webhook(request: Request):
    """飞书事件订阅回调 — 接收消息事件并路由到 brain"""
    # 校验 verification_token（防止伪造请求）
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    if FEISHU_VERIFICATION_TOKEN:
        body_token = body.get("token", "")
        if body_token != FEISHU_VERIFICATION_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid verification token")
    else:
        # 未配置 verification_token 时拒绝处理消息事件（fail-closed）
        raise HTTPException(status_code=503, detail="FEISHU_VERIFICATION_TOKEN not configured")

    # 消息事件
    event = body.get("event", {})
    if event.get("msg_type") != "text":
        return {"code": 0}

    # 提取消息内容
    msg_content = event.get("message", {}).get("content", "{}")
    try:
        content = json.loads(msg_content)
        text = content.get("text", "").strip()
    except Exception:
        return {"code": 0}

    if not text:
        return {"code": 0}

    # 3. 路由到 brain
    sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    chat_id = event.get("message", {}).get("chat_id", "")

    log.info(f"[feishu] 收到消息 from={sender_id} chat={chat_id} text={text[:50]}")

    # 在后台异步处理，不阻塞 webhook 响应；保留 task 引用防 GC
    task = asyncio.create_task(_handle_feishu_message(text, sender_id, chat_id))
    _keep_task(task)

    return {"code": 0}

async def _handle_feishu_message(text: str, sender_id: str, chat_id: str):
    """处理飞书消息：先检测决策反馈，未命中再路由到 brain"""
    try:
        # 复用 feishu_ws 的带锁缓存 token（避免重复缓存）
        from feishu_ws import _get_tenant_access_token as _get_token
        token = await asyncio.to_thread(_get_token)
        if not token:
            log.error("[feishu] 无法获取 tenant_access_token")
            return

        # 1. 先检查是否是对待确认决策的回复（好/不用了/取消…）
        reply = ""
        try:
            from app import load_magic_module
            _dec = load_magic_module("magic_decisions", "magic-decisions.py")
            if _dec:
                fb_reply = _dec.check_feedback(text, user_id=sender_id)
                if fb_reply is not None:
                    reply = fb_reply
                    log.info(f"[feishu] 决策反馈命中: {reply[:50]}")
        except Exception as e:
            log.debug(f"[feishu] 决策反馈检测跳过: {e}")

        # 2. 未命中决策反馈 → 走正常 brain 对话
        if not reply:
            session_id = f"feishu_{sender_id}"
            try:
                from agent.llm import brain_stream_sentences
                gen = await asyncio.to_thread(lambda: list(brain_stream_sentences(text, session_id=session_id)))
                for sentence, _ in gen:
                    reply += sentence
            except Exception as e:
                log.error(f"[feishu] brain 调用失败: {e}")

        if not reply:
            reply = "（无回复）"

        await _send_feishu_reply(chat_id, reply, token)
        log.info(f"[feishu] 回复 sent={len(reply)}字")
    except Exception as e:
        log.error(f"[feishu] 处理失败: {e}")


async def _send_feishu_reply(chat_id: str, text: str, token: str):
    """发送飞书消息回复"""

    def _sync_post():
        import requests
        r = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=15,
        )
        return r

    try:
        r = await asyncio.to_thread(_sync_post)
        r.raise_for_status()
    except Exception as e:
        log.error(f"[feishu] 发送回复失败: {e}")
