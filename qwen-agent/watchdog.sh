#!/bin/bash
# 助手小子 - 自动重启看门狗 v2
# 每60秒检查服务健康，挂了自动重启 + 文件日志 + 内存监控
# 用法: screen -dmS watchdog bash watchdog.sh

cd "$(dirname "$0")"
source .venv/bin/activate
LOG=/tmp/voice_srv.log
HTTPS_LOG=/tmp/voice_https.log
WATCHDOG_LOG=/tmp/watchdog.log
RESTART_COUNT=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$WATCHDOG_LOG"
}

log "看门狗v2启动 - 检查间隔60s"

while true; do
    # 检查HTTP
    HTTP_OK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:8000/health 2>/dev/null)
    # 检查HTTPS
    HTTPS_OK=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 https://localhost:8443/health 2>/dev/null)

    NEED_RESTART=0

    if [ "$HTTP_OK" != "200" ]; then
        log "⚠️ HTTP服务异常($HTTP_OK)，重启中..."
        pkill -9 -f "python voice_server.py" 2>/dev/null
        sleep 2
        screen -dmS voice bash -c "source .venv/bin/activate && python voice_server.py > $LOG 2>&1"
        NEED_RESTART=1
    fi

    if [ "$HTTPS_OK" != "200" ]; then
        log "⚠️ HTTPS服务异常($HTTPS_OK)，重启中..."
        pkill -9 -f "python https_server.py" 2>/dev/null
        sleep 2
        screen -dmS voice-https bash -c "source .venv/bin/activate && python https_server.py > $HTTPS_LOG 2>&1"
        NEED_RESTART=1
    fi

    # 检查Cloudflare Tunnel(如果tunnel_url.txt存在则监控)
    TUNNEL_FILE="$(dirname "$0")/tunnel_url.txt"
    if [ -f "$TUNNEL_FILE" ]; then
        TUNNEL_RUNNING=$(pgrep -f "cloudflared tunnel" 2>/dev/null | head -1)
        if [ -z "$TUNNEL_RUNNING" ]; then
            log "⚠️ Cloudflare Tunnel已停止，重启中..."
            bash "$(dirname "$0")/start_tunnel.sh" >> "$WATCHDOG_LOG" 2>&1
            log "✅ Cloudflare Tunnel已重启"
        fi
    fi

    if [ $NEED_RESTART -eq 1 ]; then
        RESTART_COUNT=$((RESTART_COUNT + 1))
        log "重启完成 (累计重启$RESTART_COUNT次)"
        sleep 15
    else
        # 内存检查
        MEM_PCT=$(python3 -c "import psutil;print(int(psutil.virtual_memory().percent))" 2>/dev/null || echo 0)
        if [ "$MEM_PCT" -gt 90 ]; then
            log "⚠️ 内存过高(${MEM_PCT}%)，可能OOM风险"
        fi
        
        # 日志轮转: 超过10MB截断
        for f in $LOG $HTTPS_LOG $WATCHDOG_LOG; do
            if [ -f "$f" ]; then
                SIZE=$(stat -f%z "$f" 2>/dev/null || echo 0)
                if [ $SIZE -gt 10485760 ]; then
                    log "日志轮转: $f ($(echo "scale=0; $SIZE/1048576" | bc 2>/dev/null || echo "?")MB > 10MB)"
                    tail -5000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
                fi
            fi
        done
    fi

    sleep 60
done
