"""魔幻手机 HTTPS 服务(手机访问用) - 复用voice_server的app，8443端口SSL
后台调度器(提醒/建议/预热)只在HTTP进程跑，HTTPS进程通过环境变量跳过"""
import os, uvicorn
os.environ["SKIP_BACKGROUND"] = "1"  # 关键：防止重复启动调度器/建议/预热
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from voice_server import app  # 复用同一套 API
CERT = os.path.join(os.getcwd(), "cert", "cert.pem")
KEY = os.path.join(os.getcwd(), "cert", "key.pem")
if __name__ == "__main__":
    print("🔒 HTTPS服务: https://sxliuyudeMac-mini.local:8443")
    print("   手机同WiFi访问，首次需信任证书")
    print("   (后台调度器由HTTP进程管理，本进程不重复启动)")
    uvicorn.run(app, host="0.0.0.0", port=8443,
                ssl_certfile=CERT, ssl_keyfile=KEY)
