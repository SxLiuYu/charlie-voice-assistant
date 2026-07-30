#!/usr/bin/env python3
"""魔幻手机 - 系统自测脚本
快速验证所有API端点和核心功能
用法: python test_system.py [host]
"""
import sys, json, requests, time, os

HOST = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

def test(name, func):
    try:
        ok, detail = func()
        status = "✅" if ok else "❌"
        print(f"{status} {name}: {detail}")
        return ok
    except Exception as e:
        print(f"❌ {name}: 异常 {e}")
        return False

def t_health():
    r = requests.get(f"{HOST}/health", timeout=5)
    return r.json().get("ok") == True, f"HTTP {r.status_code}"

def t_version():
    r = requests.get(f"{HOST}/api/version", timeout=5)
    d = r.json()
    return d.get("version") is not None, f"v{d.get('version','?')} {d.get('brain','')}"

def t_status():
    r = requests.get(f"{HOST}/api/status", timeout=5)
    d = r.json()
    return d.get("brain_ready") == True, f"CPU={d.get('cpu_percent')}% RAM={d.get('memory_percent')}%"

def t_chat():
    r = requests.post(f"{HOST}/api/chat", json={"message": "1+1"}, timeout=30)
    reply = r.json().get("reply", "")
    return "2" in reply, f"问1+1 → {reply[:30]}"

def t_tts():
    r = requests.post(f"{HOST}/api/tts", json={"text": "测试语音"}, timeout=30)
    audio = r.content
    return len(audio) > 1000, f"音频{len(audio)}字节"

def t_asr():
    # 先TTS生成音频，再ASR识别
    r1 = requests.post(f"{HOST}/api/tts", json={"text": "你好世界"}, timeout=30)
    if len(r1.content) < 1000:
        return False, "TTS失败"
    r2 = requests.post(f"{HOST}/api/asr", files={"file": ("test.wav", r1.content, "audio/wav")}, timeout=30)
    text = r2.json().get("text", "")
    return "你好" in text or "世界" in text, f"TTS→ASR: 你好世界 → {text}"

def t_reminders():
    r1 = requests.get(f"{HOST}/api/reminders", timeout=5)
    before = r1.json().get("total", 0)
    r2 = requests.post(f"{HOST}/api/reminders", json={"text": "自测临时提醒", "time": "明天12点"}, timeout=5)
    rid = r2.json().get("id")
    r3 = requests.delete(f"{HOST}/api/reminders/{rid}", timeout=5)
    return r3.json().get("ok") == True, f"添加{rid}→完成 ✓"

def t_conversation():
    r = requests.get(f"{HOST}/api/conversation", timeout=5)
    d = r.json()
    return d.get("count", -1) >= 0, f"{d.get('count')}条历史"

def t_reset():
    r = requests.post(f"{HOST}/api/reset", timeout=5)
    return r.json().get("ok") == True, "对话已重置"

def t_cors():
    r = requests.options(f"{HOST}/api/chat", headers={"Origin": "https://test.com", "Access-Control-Request-Method": "POST"}, timeout=5)
    return "access-control-allow-origin" in {k.lower() for k in r.headers}, "CORS允许跨域"

def t_dashboard():
    r = requests.get(f"{HOST}/dashboard", timeout=5)
    return r.status_code == 200 and "监控面板" in r.text, f"HTTP {r.status_code}, {len(r.text)}字节"

def t_export():
    r = requests.get(f"{HOST}/api/export", timeout=5)
    return r.status_code == 200, f"HTTP {r.status_code}, {len(r.text)}字节"

def t_notifications():
    # First clear any existing
    requests.get(f"{HOST}/api/notifications", timeout=5)
    # Add a test notification via expired reminder
    import json as _j, datetime as _dt
    rf = os.path.join(os.path.dirname(__file__), "reminders.json")
    data = _j.load(open(rf))
    past = (_dt.datetime.now() - _dt.timedelta(minutes=1)).isoformat()
    data.append({"id": 999777, "text": "自测通知", "time": "1分钟前", "due": past, "done": False})
    _j.dump(data, open(rf, "w"), ensure_ascii=False, indent=2)
    time.sleep(35)
    r = requests.get(f"{HOST}/api/notifications", timeout=5)
    d = r.json()
    # Clean up
    data = _j.load(open(rf))
    data = [x for x in data if x.get("id") != 999777]
    _j.dump(data, open(rf, "w"), ensure_ascii=False, indent=2)
    return d.get("count", 0) > 0, f"通知队列: {d.get('count',0)}条"

def t_manifest():
    r = requests.get(f"{HOST}/manifest.json", timeout=5)
    d = r.json()
    return d.get("name") == "魔幻手机" and d.get("display") == "standalone", f'{d.get("name","?")} {d.get("display","?")}'

def t_search():
    # First ensure there's some conversation history
    requests.post(f"{HOST}/api/chat", json={"message": "测试搜索功能hello"}, timeout=30)
    r = requests.get(f"{HOST}/api/search?q=hello", timeout=5)
    d = r.json()
    return d.get("count", 0) >= 1, f"找到{d.get('count',0)}条匹配"


def t_stream_chat():
    """流式文字对话: SSE流应返回text+done事件"""
    t0 = time.time()
    r = requests.post(f"{HOST}/api/chat/stream",
        json={"message": "1+1等于几"}, stream=True, timeout=60)
    if r.status_code != 200:
        return False, "HTTP %d" % r.status_code
    has_text = False
    has_done = False
    for line in r.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8", "ignore")
        if line.startswith("data: "):
            try:
                d = json.loads(line[6:])
                if d.get("type") == "text":
                    has_text = True
                elif d.get("type") == "done":
                    has_done = True
            except:
                pass
    elapsed = time.time() - t0
    ok = has_text and has_done
    return ok, "流式对话 %.1fs (text=%s done=%s)" % (elapsed, "Y" if has_text else "N", "Y" if has_done else "N")

def main():
    print("=" * 50)
    print("  魔幻手机 · 系统自测")
    print(f"  目标: {HOST}")
    print("=" * 50)
    tests = [
        ("健康检查", t_health),
        ("版本信息", t_version),
        ("系统状态", t_status),
        ("文字对话", t_chat),
        ("TTS语音合成", t_tts),
        ("ASR语音识别", t_asr),
        ("提醒管理", t_reminders),
        ("对话历史", t_conversation),
        ("重置对话", t_reset),
        ("CORS跨域", t_cors),
        ("监控面板", t_dashboard),
        ("对话导出", t_export),
        ("通知队列", t_notifications),
        ("PWA Manifest", t_manifest),
        ("对话搜索", t_search),
        ("流式对话", t_stream_chat),
    ]
    passed = sum(1 for _, fn in tests if test(_, fn))
    print("=" * 50)
    print(f"  结果: {passed}/{len(tests)} 通过")
    if passed == len(tests):
        print("  🎉 全部通过！系统运行正常")
    else:
        print(f"  ⚠️ {len(tests)-passed}项失败，请检查")
    print("=" * 50)

if __name__ == "__main__":
    main()
