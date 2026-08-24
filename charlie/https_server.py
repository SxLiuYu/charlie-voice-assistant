"""Charlie HTTPS 服务(手机访问用) - 复用voice_server的app，8443端口SSL
后台调度器(提醒/建议/预热)只在HTTP进程跑，HTTPS进程通过环境变量跳过"""
import os, sys, socket, uvicorn
os.environ["SKIP_BACKGROUND"] = "1"  # 关键：防止重复启动调度器/建议/预热
os.environ["LOG_FILE_DISABLE"] = "1"  # 防止双进程同写 app.log 行重复（stdout 由 watchdog 重定向到 voice_https.log）
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from voice_server import app  # 复用同一套 API
from app.config import https_port
from app.cert import ensure_cert
CERT = os.path.join(os.getcwd(), "cert", "cert.pem")
KEY = os.path.join(os.getcwd(), "cert", "key.pem")
if __name__ == "__main__":
    # 证书缺失时自动生成自签证书（T5）
    if not ensure_cert(CERT, KEY):
        print("❌ 证书不可用，无法启动 HTTPS。请手动运行: bash scripts/gen-cert.sh")
        sys.exit(1)
    port = https_port()
    print(f"🔒 HTTPS服务: https://{socket.gethostname()}:{port}")
    print("   手机同WiFi访问，首次需信任证书")
    print("   (后台调度器由HTTP进程管理，本进程不重复启动)")
    uvicorn.run(app, host="0.0.0.0", port=port,
                ssl_certfile=CERT, ssl_keyfile=KEY)
