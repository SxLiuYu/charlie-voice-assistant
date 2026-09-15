"""天猫精灵(AliGenie)云技能 Webhook — 把天猫精灵变成 Charlie 的第二个语音入口

协议: AliGenie 云技能 POST JSON → 本端点提取用户话术 → brain → 返回阿里响应格式。
安全: 通过 ALIGENIE_OPEN_ID 白名单限定只有绑定的天猫精灵账号可用（首个请求自动学习，
      之后加入 .env 的 ALIGENIE_OPEN_ID 锁定）。鉴权中间件对本路径豁免（阿里不带本机token）。
"""
import json
import logging
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.auth import _sanitize_text

log = logging.getLogger("magic")

router = APIRouter()

# 首个请求学习到的 open_id（重启后以 .env 的 ALIGENIE_OPEN_ID 为准）
_learned_open_id: str | None = None


def _extract_user_text(data: dict) -> str:
    """从 AliGenie 请求中提取用户话术（兼容多协议版本字段）"""
    if not isinstance(data, dict):
        return ""
    # 1. 顶层 utterance / originalUtterance
    for k in ("utterance", "originalUtterance", "text"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 2. 槽位拼接（自定义技能把整句话收进槽位）
    slots = data.get("slotEntities") or []
    parts = []
    for s in slots:
        if isinstance(s, dict):
            v = s.get("originalValue") or s.get("standardValue") or ""
            if v.strip():
                parts.append(v.strip())
    if parts:
        return " ".join(parts)
    # 3. intentMessage 变体
    im = data.get("intentMessage") or {}
    ents = (im.get("intentEntities") or [])
    if im.get("utterance"):
        return im["utterance"]
    return ""


def _extract_open_id(data: dict) -> str:
    rd = data.get("requestData") or {}
    oid = rd.get("userOpenId") or rd.get("openId") or ""
    if not oid:
        im = data.get("intentMessage") or {}
        up = (im.get("userProfile") or {})
        oid = up.get("openId", "")
    return str(oid)


def _aligenie_reply(text: str) -> JSONResponse:
    """构造 AliGenie 云技能标准响应"""
    return JSONResponse({
        "returnCode": "0",
        "returnErrorSolution": "",
        "returnMessage": "ok",
        "returnValue": {"reply": text, "actions": [], "properties": {}},
    })


_CERT_PATTERN = re.compile(r"^/aligenie/([0-9a-f]{32})\.txt$")
# AliGenie 平台生成的域名认证文件内容（从控制台"后端服务"页面获取后写入 .env）
_ALIGENIE_CERT_FILE = os.getenv("ALIGENIE_CERT_FILE", "").strip()


@router.get("/aligenie/{cert}.txt")
async def aligenie_certificate(cert: str):
    """AliGenie 域名认证文件 — 平台验证 webhook 域名所有权时请求此路径。
    认证文件内容从 AliGenie 控制台获取，写入 .env 的 ALIGENIE_CERT_FILE。"""
    if _ALIGENIE_CERT_FILE:
        return PlainTextResponse(_ALIGENIE_CERT_FILE)
    # 未配置认证文件内容时返回 404，提示需要从控制台获取
    log.warning("[aligenie] 认证文件被请求但未配置 ALIGENIE_CERT_FILE")
    return PlainTextResponse("cert not configured", status_code=404)


@router.post("/aligenie")
async def aligenie_webhook(request: Request):
    global _learned_open_id
    try:
        raw = await request.body()
        try:
            data = json.loads(raw)
        except Exception:
            # 兼容 form 提交（阿里旧协议）
            from urllib.parse import parse_qs
            form = parse_qs(raw.decode("utf-8", "ignore"))
            data = {}
            for k, v in form.items():
                try:
                    data[k] = json.loads(v[0])
                except Exception:
                    data[k] = v[0]
        open_id = _extract_open_id(data)
        allowed = os.environ.get("ALIGENIE_OPEN_ID", "").strip()
        # 学习/校验
        if allowed:
            if open_id != allowed:
                log.warning(f"[aligenie] 非授权open_id: {open_id[:12]}...")
                return _aligenie_reply("这个天猫精灵还没有绑定到我。")
        elif open_id:
            if _learned_open_id and _learned_open_id != open_id:
                log.warning(f"[aligenie] open_id与已学习的不一致，拒绝: {open_id[:12]}...")
                return _aligenie_reply("这个天猫精灵还没有绑定到我。")
            _learned_open_id = open_id
            log.info(f"[aligenie] 已学习绑定open_id: {open_id[:12]}...（建议写入.env ALIGENIE_OPEN_ID锁定）")
        # 兜底：无 open_id 且无白名单（本地测试）→ 放行但日志提醒
        if not open_id and not allowed:
            log.info("[aligenie] 无open_id请求（测试模式放行）")

        text = _sanitize_text(_extract_user_text(data), 200)
        if not text:
            return _aligenie_reply("没有听清你说的话，请再说一遍。")
        log.info(f"[aligenie] 收到: {text[:50]}")

        # 走 brain（与 /api/chat 同链路；天猫精灵超时约5秒，超时兜底）
        import asyncio
        from voice_agent import brain
        try:
            reply = await asyncio.wait_for(asyncio.to_thread(brain, text, "aligenie"), timeout=4.5)
        except asyncio.TimeoutError:
            reply = "我想想，稍后再问我一遍。"
        reply = (reply or "").strip()
        if not reply:
            reply = "这个问题我还没想好，换个说法试试。"
        log.info(f"[aligenie] 回复: {reply[:50]}")
        return _aligenie_reply(reply)
    except Exception as e:
        log.error(f"[aligenie] 处理异常: {e}")
        return _aligenie_reply("我这边出了点小状况，请稍后再试。")
