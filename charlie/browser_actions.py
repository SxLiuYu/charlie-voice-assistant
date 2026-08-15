"""browser_actions: 浏览器工具（已废弃 Playwright 发布自动化）

⚠️ 2026-08-12 更新：小红书因 AI 披露被封，Playwright 发布已废弃。
   现在只做：封面图生成 + 文案撰写，发布由用户手动完成。

保留的功能：
  - 飞书集成（发送通知、二维码图片）
  - Cookie 读取工具（备用）

用法：
  from browser_actions import send_feishu_text, send_feishu_image
  send_feishu_text("文案已生成，请手动发布")
"""
import os, json, time, subprocess, base64, requests
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_PUSH_OPEN_ID = os.getenv("FEISHU_PUSH_OPEN_ID", "")
FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 加载 .env
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_PUSH_OPEN_ID") and not locals().get(k):
                globals()[k] = v

# ── Cookie 读取（备用，仅供调试）──────────────────────────────────────────
CHROME_COOKIE_DB = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/Cookies")

def get_chrome_cookies_raw(domains: list = None) -> dict:
    """读取 Chrome cookies（明文值，不含加密）— 仅供调试"""
    import sqlite3, shutil, tempfile
    tmp_db = tempfile.mktemp(suffix=".db")
    try:
        if not os.path.exists(CHROME_COOKIE_DB):
            return {}
        shutil.copy2(CHROME_COOKIE_DB, tmp_db)
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cookies = {}
        for name, host, path, value in cur.execute(
            "SELECT name, host_key, path, value FROM cookies"
        ).fetchall():
            if domains and not any(d in host for d in domains):
                continue
            cookies.setdefault(host, {})[name] = value
        conn.close()
    finally:
        os.unlink(tmp_db)
    return cookies

# ── 飞书集成 ───────────────────────────────────────────────────────────────
def _get_feishu_token() -> str:
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return ""
    r = requests.post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=(5, 15))
    return r.json().get("tenant_access_token", "")

def send_feishu_text(text: str, open_id: str = None) -> bool:
    """发送文本消息到飞书"""
    open_id = open_id or FEISHU_PUSH_OPEN_ID
    if not open_id:
        print("⚠️ FEISHU_PUSH_OPEN_ID 未配置")
        return False
    token = _get_feishu_token()
    if not token:
        print("⚠️ 飞书 token 获取失败")
        return False
    r = requests.post(
        f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": open_id, "msg_type": "text",
              "content": json.dumps({"text": text})}, timeout=(5, 15)
    )
    ok = r.json().get("code", -1) == 0
    if ok:
        print(f"✅ 飞书消息已发送")
    else:
        print(f"❌ 飞书发送失败: {r.json()}")
    return ok

def send_feishu_image(image_path: str, caption: str = "", open_id: str = None) -> bool:
    """发送图片到飞书"""
    open_id = open_id or FEISHU_PUSH_OPEN_ID
    if not open_id or not FEISHU_APP_ID:
        print("⚠️ 飞书配置缺失")
        return False
    token = _get_feishu_token()
    if not token:
        return False
    with open(image_path, 'rb') as f:
        r = requests.post(f"{FEISHU_BASE}/im/v1/images",
                          headers={"Authorization": f"Bearer {token}"},
                          data={"image_type": "message"},
                          files={"image": f}, timeout=(5, 15))
    image_key = r.json().get("data", {}).get("image_key", "")
    if not image_key:
        print(f"⚠️ 图片上传失败: {r.json()}")
        return False
    requests.post(f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id",
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json"},
                  json={"receive_id": open_id, "msg_type": "image",
                        "content": json.dumps({"image_key": image_key})}, timeout=(5, 15))
    if caption:
        requests.post(f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json={"receive_id": open_id, "msg_type": "text",
                            "content": json.dumps({"text": caption})}, timeout=(5, 15))
    print(f"✅ 飞书图片已发送: {image_path}")
    return True
