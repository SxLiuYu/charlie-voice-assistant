"""Charlie 端到端验收：功能完整性 + 功能准确性。

前置：voice_server 已在 ASSISTANT_KID_HTTP_PORT（默认 8020）跑起来，
      .env 里 NM_CLOUD_KEY 指向北京服务器（www.sxliuyu.bond）。

用法：
    ./.venv/Scripts/python.exe scripts/e2e_acceptance.py
    ./.venv/Scripts/python.exe scripts/e2e_acceptance.py --base http://127.0.0.1:8020

每条用例输出 PASS/FAIL/SKIP + 耗时 + 实际值，最后打印汇总与失败明细。
不依赖第三方测试框架，退出码 = 失败数（>0 即失败）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

BASE = "http://127.0.0.1:8020"
TIMEOUT_CHAT = 90
TIMEOUT_AUDIO = 120
# 服务端 IP 级限流：general 60/min、voice 10/min（app/state.py）。
# 整套用例约 55 个请求，连续跑两轮必然触发 429 —— 不是功能问题。
# 这里给每次请求之间留间隔，可用 --gap 调整。
REQUEST_GAP = 0.0


# ---------------------------------------------------------------- 基础工具
def _req(method: str, path: str, body=None, ctype="application/json", raw=False):
    if REQUEST_GAP:
        time.sleep(REQUEST_GAP)
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = ctype + "; charset=utf-8"
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_CHAT) as r:
            payload = r.read()
            return r.status, payload, (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000


def jget(method, path, body=None, **kw):
    code, payload, ms = _req(method, path, body, **kw)
    try:
        return code, json.loads(payload), ms
    except Exception:
        return code, payload, ms


def chat(text: str, session: str = "e2e") -> tuple[int, str, float]:
    code, data, ms = jget("POST", "/api/chat", {"message": text, "session_id": session})
    if isinstance(data, dict):
        return code, data.get("reply", ""), ms
    return code, "", ms


def multipart(path: str, field_name: str, filename: str, content: bytes,
              extra: dict | None = None):
    if REQUEST_GAP:
        time.sleep(REQUEST_GAP)
    boundary = "----charlie" + uuid.uuid4().hex
    parts = []
    for k, v in (extra or {}).items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_AUDIO) as r:
            return r.status, r.read(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000


# ---------------------------------------------------------------- 结果收集
@dataclass
class Result:
    name: str
    status: str
    ms: float = 0.0
    detail: str = ""
    group: str = ""


RESULTS: list[Result] = []


def record(group: str, name: str, ok: bool | None, ms: float = 0.0, detail: str = ""):
    st = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append(Result(name, st, ms, detail, group))
    mark = {"PASS": "\033[32mPASS\033[0m", "FAIL": "\033[31mFAIL\033[0m", "SKIP": "\033[33mSKIP\033[0m"}[st]
    print(f"  [{mark}] {name:<44} {ms:7.0f}ms  {detail}")


# ---------------------------------------------------------------- 测例
def t_health():
    g = "基础可用性"
    code, d, ms = jget("GET", "/health")
    ok = code == 200 and isinstance(d, dict) and d.get("ok") is True
    record(g, "GET /health 返回 ok", ok, ms, f"{code} {str(d)[:70]}")

    code, d, ms = jget("GET", "/api/version")
    ok = code == 200 and isinstance(d, dict) and d.get("version")
    record(g, "GET /api/version 有版本号", ok, ms, str(d.get("version") if isinstance(d, dict) else d))

    code, d, ms = jget("GET", "/api/status")
    ok = code == 200 and isinstance(d, dict) and "cpu_percent" in d
    record(g, "GET /api/status 有 CPU 指标", ok, ms, f"{code}")


def t_chat_accuracy():
    g = "回答准确性"
    # 预期值同时收阿拉伯数字与中文数字：模型常按语气写"等于二""一百"，
    # 只要答案正确就算通过，不能因为写法不同判失败。
    cases = [
        ("1加1等于几", ["2", "二", "两"]),
        ("中国的首都是哪里", ["北京"]),
        ("25乘4等于多少", ["100", "一百"]),
        ("把'hello world'翻译成中文", ["你好"]),
        ("水的沸点是多少度", ["100", "一百"]),
        ("一年有多少个月", ["12", "十二"]),
        ("3的平方是多少", ["9", "九"]),
    ]
    for q, expects in cases:
        code, reply, ms = chat(q, "acc")
        hit = any(e in reply for e in expects) if reply else False
        record(g, f"问：{q}", code == 200 and hit, ms, f"答：{reply[:60]}")


def t_fastpath():
    g = "本地快路径（不依赖 LLM）"
    code, reply, ms = chat("现在几点了", "fp")
    ok = code == 200 and ("点" in reply)
    record(g, "问时间走本地解析", ok, ms, reply[:50])

    code, reply, ms = chat("今天星期几", "fp")
    record(g, "问星期", code == 200 and bool(reply), ms, reply[:50])


def t_memory():
    g = "记忆读写闭环"
    # 每条用例用独立 session：同一 session 会把上一轮的旧值留在对话历史里，
    # LLM 可能从历史而不是偏好库里取答案（这不是记忆 bug，是上下文优先级）。
    sess = "mem_" + uuid.uuid4().hex[:8]
    # 纯中文标记，避免 # / 大小写等格式噪声影响子串比对
    marker = "紫罗兰" + uuid.uuid4().hex[:4]
    code, reply, ms = chat(f"记住我最喜欢的颜色是{marker}", sess)
    record(g, "写入偏好", code == 200, ms, reply[:60])

    code, reply, ms = chat("我最喜欢什么颜色", sess)
    # 模型可能改写大小写/加空格，比对时统一小写并去掉空白
    norm = lambda s: "".join(str(s).lower().split())
    ok = norm(marker) in norm(reply)
    record(g, "同会话召回偏好", ok, ms, reply[:60])

    # 落盘校验：偏好文件里确实是新值（排除"回答对但没存"）
    code, d, ms = jget("GET", "/api/preferences")
    stored = ""
    if isinstance(d, dict):
        prefs = d.get("preferences", d)
        if isinstance(prefs, dict):
            stored = str(prefs.get("favorite_color", ""))
    record(g, "偏好已落盘", norm(marker) in norm(stored), ms, f"favorite_color={stored!r}")

    code, d, ms = jget("GET", "/api/preferences")
    ok = code == 200 and isinstance(d, dict)
    record(g, "GET /api/preferences 可读", ok, ms, f"{code}")


def t_reminder():
    g = "提醒全链路"
    text = f"e2e提醒{uuid.uuid4().hex[:4]}"
    code, d, ms = jget("POST", "/api/reminders", {"text": text, "time": "23:59"})
    ok = code == 200 and isinstance(d, dict) and d.get("ok")
    rid = d.get("id") if isinstance(d, dict) else None
    record(g, "创建提醒", ok, ms, f"id={rid}")

    code, d, ms = jget("GET", "/api/reminders")
    got = isinstance(d, dict) and any(r.get("text") == text for r in d.get("reminders", []))
    record(g, "提醒出现在列表里", got, ms, f"total={d.get('total') if isinstance(d, dict) else '?'}")

    if rid:
        code, d, ms = jget("DELETE", f"/api/reminders/{rid}")
        ok = code == 200
        record(g, "删除提醒", ok, ms, f"{code}")


def t_scenes():
    g = "场景协议"
    code, d, ms = jget("GET", "/api/protocols")
    n = len(d.get("protocols", [])) if isinstance(d, dict) else 0
    record(g, "场景列表非空", code == 200 and n > 0, ms, f"{n} 个场景")

    code, reply, ms = chat("晚安", "scene")
    ok = code == 200 and len(reply) > 5
    record(g, "「晚安」场景可执行", ok, ms, reply[:70])


def t_tool_call():
    g = "MCP 工具调用"
    code, reply, ms = chat("帮我算 137 乘 24", "tool")
    ok = code == 200 and "3288" in reply
    record(g, "计算类工具（137×24=3288）", ok, ms, reply[:70])

    code, reply, ms = chat("把我刚才说的记下来：明天要交报告", "tool")
    record(g, "写记忆类工具", code == 200 and bool(reply), ms, reply[:60])


def t_audio_roundtrip():
    g = "语音链路（ASR↔TTS）"
    code, payload, ms = _req("POST", "/api/tts", {"text": "这是一次语音合成测试。", "voice": "Ethan"})
    ok = code == 200 and len(payload) > 2000 and payload[:2] in (b"\xff\xf3", b"\xff\xfb", b"ID")
    record(g, "TTS 产出 MP3", ok, ms, f"{len(payload)} bytes")
    if not ok:
        record(g, "ASR 回灌", None, 0, "TTS 未出音频，跳过")
        return
    mp3 = payload

    code, payload, ms = multipart("/api/asr", "file", "t.mp3", mp3)
    text = ""
    if code == 200:
        try:
            text = json.loads(payload).get("text", "")
        except Exception:
            pass
    hit = "语音合成测试" in text
    record(g, "ASR 回灌一致", hit, ms, f"识别：{text!r}")


def t_full_voice():
    g = "语音全链路（ASR→LLM→TTS）"
    code, payload, ms = _req("POST", "/api/tts", {"text": "请用一句话介绍你自己。", "voice": "Ethan"})
    if code != 200 or len(payload) < 2000:
        record(g, "整段语音对话", None, ms, "TTS 起不来，跳过")
        return
    code, payload, ms = multipart("/api/voice", "file", "v.mp3", payload,
                                  {"fmt": "mp3", "session_id": "e2evoice"})
    d = {}
    try:
        d = json.loads(payload)
    except Exception:
        pass
    ok = code == 200 and d.get("text") and d.get("reply") and d.get("audio")
    record(g, "整段语音对话", ok, ms,
           f"asr={str(d.get('text'))[:22]!r} reply={str(d.get('reply'))[:26]!r} audio={len(str(d.get('audio','')))}")


def t_stream():
    g = "流式输出"
    if REQUEST_GAP:
        time.sleep(REQUEST_GAP)
    body = json.dumps({"message": "从一数到三", "session_id": "st"}, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/api/chat/stream", data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    t0 = time.perf_counter()
    events, texts = [], []
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            for line in r:
                s = line.decode("utf-8", "ignore").strip()
                if s.startswith("data: "):
                    try:
                        d = json.loads(s[6:])
                    except Exception:
                        continue
                    events.append(d.get("type"))
                    if d.get("type") == "text":
                        texts.append(d.get("text", ""))
                    if d.get("type") == "done":
                        break
    except Exception as e:
        record(g, "SSE 流式返回", False, (time.perf_counter() - t0) * 1000, str(e)[:60])
        return
    ms = (time.perf_counter() - t0) * 1000
    ok = "text" in events and "done" in events
    record(g, "SSE 流式返回", ok, ms, f"events={set(events)} text={' '.join(texts)[:40]!r}")


def t_input_guard():
    g = "输入防护"
    code, d, ms = jget("POST", "/api/chat", {"message": "", "session_id": "g"})
    record(g, "空消息被拒", code in (400, 422), ms, f"{code}")

    code, d, ms = jget("POST", "/api/chat", {"message": "测" * 3000, "session_id": "g"})
    record(g, "超长消息被拒", code in (400, 413, 422), ms, f"{code}")

    code, reply, ms = chat("<script>alert(1)</script>你好", "g")
    ok = code == 200 and "<script>" not in reply
    record(g, "XSS 标签被清洗", ok, ms, reply[:60])


def t_management_apis():
    g = "管理接口完整性"
    paths = [
        ("/api/mcp/servers", "MCP 服务列表"),
        ("/api/roles", "角色列表"),
        ("/api/sessions", "会话列表"),
        ("/api/context", "上下文快照"),
        ("/api/devices", "设备列表"),
        ("/api/decisions", "决策引擎"),
        ("/api/protocols", "场景协议"),
        ("/api/evolution", "自进化"),
        ("/api/behaviors", "行为统计"),
        ("/api/habits", "习惯打卡"),
        ("/api/memory", "记忆状态"),
        ("/api/setup/mcp-status", "MCP 配置状态"),
        ("/api/metrics", "请求指标"),
        ("/api/audit", "审计日志"),
        ("/api/wake/status", "唤醒状态"),
        ("/api/user/current", "当前用户"),
        ("/api/tts/voices", "音色列表"),
        ("/api/logs", "运行日志"),
        ("/api/setup/llm-priority", "LLM 优先级"),
        ("/api/lan-info", "局域网信息"),
        ("/api/tunnel", "隧道状态"),
    ]
    for p, label in paths:
        code, d, ms = jget("GET", p)
        record(g, f"{label} ({p})", code == 200, ms, str(code))


def t_write_apis():
    g = "管理接口写入"
    key = f"e2e_{uuid.uuid4().hex[:5]}"
    code, d, ms = jget("POST", "/api/preferences", {"key": key, "value": "v1"})
    record(g, "写偏好", code == 200 and isinstance(d, dict) and d.get("ok"), ms, f"{code}")

    code, d, ms = jget("DELETE", f"/api/preferences/{key}")
    record(g, "删偏好", code == 200, ms, f"{code}")

    code, d, ms = jget("POST", "/api/reset?session_id=e2e", {})
    record(g, "重置会话", code == 200, ms, f"{code}")


def t_degradation():
    g = "失效降级"
    code, reply, ms = chat("随便聊聊今天过得怎么样", "deg")
    ok = code == 200 and bool(reply) and "错误" not in reply
    record(g, "闲聊不报错", ok, ms, reply[:60])


GROUPS = [
    ("基础可用性", t_health),
    ("回答准确性", t_chat_accuracy),
    ("本地快路径", t_fastpath),
    ("记忆闭环", t_memory),
    ("提醒链路", t_reminder),
    ("场景协议", t_scenes),
    ("工具调用", t_tool_call),
    ("语音链路", t_audio_roundtrip),
    ("语音全链路", t_full_voice),
    ("流式输出", t_stream),
    ("输入防护", t_input_guard),
    ("管理读接口", t_management_apis),
    ("管理写接口", t_write_apis),
    ("降级行为", t_degradation),
]


def main() -> int:
    global BASE, REQUEST_GAP
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--gap", type=float, default=0.0,
                    help="每次请求间隔秒数；服务端 IP 限流 60/min，连跑多轮时给 1.1 以上")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    REQUEST_GAP = max(0.0, args.gap)

    print(f"目标：{BASE}\n")
    for label, fn in GROUPS:
        print(f"── {label} " + "─" * max(0, 56 - len(label.encode('gbk', 'ignore'))))
        try:
            fn()
        except Exception as e:
            record(label, f"{label} 整组异常", False, 0, f"{type(e).__name__}: {e}")
        print()

    passed = sum(1 for r in RESULTS if r.status == "PASS")
    failed = [r for r in RESULTS if r.status == "FAIL"]
    skipped = [r for r in RESULTS if r.status == "SKIP"]
    total = len(RESULTS)

    print("=" * 68)
    print(f"总计 {total} 项：PASS {passed} / FAIL {len(failed)} / SKIP {len(skipped)}")
    if failed:
        print("\n失败明细：")
        for r in failed:
            print(f"  ✗ [{r.group}] {r.name}  →  {r.detail}")
    if skipped:
        print("\n跳过：")
        for r in skipped:
            print(f"  - [{r.group}] {r.name}  ({r.detail})")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
