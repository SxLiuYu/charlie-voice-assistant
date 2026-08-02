#!/bin/bash
# Charlie - Cloudflare Tunnel 启动脚本
# 创建公网访问隧道, 让用户从任何地方都能访问Charlie
# 用法: bash start_tunnel.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi
CF_BIN="${CF_BIN:-$HOME/.local/bin/cloudflared}"
URL_FILE="${TUNNEL_URL_FILE:-$SCRIPT_DIR/tunnel_url.txt}"
LOG_DIR="${ASSISTANT_KID_LOG_DIR:-$SCRIPT_DIR/logs}"
LOG_FILE="${CLOUDFLARED_LOG_FILE:-$LOG_DIR/cloudflared.log}"
HTTP_PORT="${ASSISTANT_KID_HTTP_PORT:-8000}"
mkdir -p "$LOG_DIR"

case "$HTTP_PORT" in
    ''|*[!0-9]*) HTTP_PORT=8000 ;;
esac
if [ "$HTTP_PORT" -lt 1 ] || [ "$HTTP_PORT" -gt 65535 ]; then
    HTTP_PORT=8000
fi

# 检查cloudflared是否安装
if [ ! -f "$CF_BIN" ]; then
    echo "❌ cloudflared未安装, 请运行: brew install cloudflared"
    exit 1
fi

# 杀掉旧隧道进程
pkill -f "cloudflared tunnel" 2>/dev/null
sleep 2

# 启动新隧道
echo "🚀 启动Cloudflare Tunnel..."
$CF_BIN tunnel --url "http://localhost:$HTTP_PORT" > "$LOG_FILE" 2>&1 &
TUNNEL_PID=$!

# 等待URL生成
for i in $(seq 1 15); do
    sleep 2
    URL=$(grep -o 'https://[^ ]*\.trycloudflare\.com' "$LOG_FILE" | head -1)
    if [ -n "$URL" ]; then
        echo "$URL" > "$URL_FILE"
        echo "✅ 隧道已创建!"
        echo "📱 公网访问地址: $URL"
        echo "📝 已保存到: $URL_FILE"
        echo "🔄 PID: $TUNNEL_PID"
        exit 0
    fi
done

echo "❌ 隧道创建超时, 检查日志: $LOG_FILE"
exit 1
