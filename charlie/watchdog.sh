#!/bin/bash
# Charlie - 自动重启看门狗 v2
# 每60秒检查服务健康，挂了自动重启 + 文件日志 + 内存监控
# 用法: screen -dmS watchdog bash watchdog.sh

cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi
LOG_DIR="${ASSISTANT_KID_LOG_DIR:-$SCRIPT_DIR/logs}"
mkdir -p "$LOG_DIR"
source .venv/bin/activate
LOG="${VOICE_SERVER_LOG:-$LOG_DIR/voice_srv.log}"
HTTPS_LOG="${HTTPS_SERVER_LOG:-$LOG_DIR/voice_https.log}"
WATCHDOG_LOG="${WATCHDOG_LOG:-$LOG_DIR/watchdog.log}"
HTTP_PORT="${ASSISTANT_KID_HTTP_PORT:-8000}"
HTTPS_PORT="${ASSISTANT_KID_HTTPS_PORT:-8443}"
case "$HTTP_PORT" in
    ''|*[!0-9]*) HTTP_PORT=8000 ;;
esac
case "$HTTPS_PORT" in
    ''|*[!0-9]*) HTTPS_PORT=8443 ;;
esac
if [ "$HTTP_PORT" -lt 1 ] || [ "$HTTP_PORT" -gt 65535 ]; then
    HTTP_PORT=8000
fi
if [ "$HTTPS_PORT" -lt 1 ] || [ "$HTTPS_PORT" -gt 65535 ]; then
    HTTPS_PORT=8443
fi
RESTART_COUNT=0
CONSECUTIVE_FAIL=0   # 连续启动失败次数，用于退避
CHECK_COUNT=0        # 运行检查计数，用于心跳日志
FEISHU_WEBHOOK="${FEISHU_WEBHOOK:-}"

notify_feishu() {
    local msg="$1"
    if [ -z "$FEISHU_WEBHOOK" ]; then return; fi
    curl -s -m 5 -X POST "$FEISHU_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$msg\"}}" >/dev/null 2>&1
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$WATCHDOG_LOG"
}

pkill_wait() {
    local pattern="$1"
    local timeout="${2:-5}"
    pkill -f "$pattern" 2>/dev/null
    local waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if ! pgrep -f "$pattern" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    # 仍存活则 SIGKILL
    pkill -9 -f "$pattern" 2>/dev/null
    return 0
}

log "看门狗v2启动 - 检查间隔60s"
if ! command -v screen &>/dev/null; then log "❌ screen未安装，请运行: brew install screen"; exit 1; fi

while true; do
    # 检查HTTP
    HTTP_OK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:$HTTP_PORT/api/status" 2>/dev/null)
    # 检查HTTPS
    HTTPS_OK=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "https://localhost:$HTTPS_PORT/api/status" 2>/dev/null)

    NEED_RESTART=0

    if [ "$HTTP_OK" != "200" ]; then
        log "⚠️ HTTP服务异常($HTTP_OK)，重启中..."
        notify_feishu "Charlie HTTP服务异常($HTTP_OK)，已自动重启"
        pkill_wait "voice_server.py" 5
        screen -dmS voice bash -c "cd '$SCRIPT_DIR' && exec '$SCRIPT_DIR/.venv/bin/python' voice_server.py > '$LOG' 2>&1"
        NEED_RESTART=1
    fi

    if [ "$HTTPS_OK" != "200" ]; then
        log "⚠️ HTTPS服务异常($HTTPS_OK)，重启中..."
        notify_feishu "Charlie HTTPS服务异常($HTTPS_OK)，已自动重启"
        pkill_wait "https_server.py" 5
        screen -dmS voice-https bash -c "cd '$SCRIPT_DIR' && exec '$SCRIPT_DIR/.venv/bin/python' https_server.py > '$HTTPS_LOG' 2>&1"
        NEED_RESTART=1
    fi

    # 检查 brain 连续失败（HTTP 服务正常但大脑死链时重启）
    if [ "$HTTP_OK" == "200" ]; then
        XIAOZHI_STATUS=$(curl -s --max-time 5 "http://localhost:$HTTP_PORT/api/xiaozhi/status" 2>/dev/null || echo "")
        if [ -n "$XIAOZHI_STATUS" ]; then
            BRAIN_FAILURES=$(echo "$XIAOZHI_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('brain',{}).get('consecutive_failures',0))" 2>/dev/null || echo 0)
            if [ "$BRAIN_FAILURES" -ge 3 ]; then
                log "⚠️ Brain连续失败${BRAIN_FAILURES}次，重启服务..."
                notify_feishu "Charlie Brain连续${BRAIN_FAILURES}次失败，已重启"
                pkill_wait "voice_server.py" 5
                screen -dmS voice bash -c "cd '$SCRIPT_DIR' && exec '$SCRIPT_DIR/.venv/bin/python' voice_server.py > '$LOG' 2>&1"
                NEED_RESTART=1
            fi
        fi
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
        CONSECUTIVE_FAIL=$((CONSECUTIVE_FAIL + 1))
        log "重启完成 (累计重启$RESTART_COUNT次)"
        # 连续失败退避: 等得更久，避免启动失败后立刻再轰炸
        if [ "$CONSECUTIVE_FAIL" -ge 3 ]; then
            log "⚠️ 连续$CONSECUTIVE_FAIL次异常，启用退避(60s)"
            notify_feishu "Charlie连续${CONSECUTIVE_FAIL}次启动失败，已进入退避(60s)"
            sleep 60
        else
            sleep 15
        fi
    else
        CONSECUTIVE_FAIL=0
        # 心跳日志：每 30 次检查记录一次服务正常
        CHECK_COUNT=$((CHECK_COUNT + 1))
        if [ "$CHECK_COUNT" -ge 30 ]; then
            log "服务正常(运行检查${CHECK_COUNT}次)"
            CHECK_COUNT=0
        fi
        # 内存检查
        MEM_PCT=$(python3 -c "import psutil;print(int(psutil.virtual_memory().percent))" 2>/dev/null || echo 0)
        if [ "$MEM_PCT" -gt 90 ]; then
            log "⚠️ 内存过高(${MEM_PCT}%)，可能OOM风险"
        fi
        
        # 日志轮转: 超过10MB截断
        for f in $LOG $HTTPS_LOG $WATCHDOG_LOG; do
            if [ -f "$f" ]; then
                SIZE=$(stat -f%z "$f" 2>/dev/null || echo 0)
                if [ $SIZE -gt 5242880 ]; then
                    log "日志轮转: $f ($(echo "scale=0; $SIZE/1048576" | bc 2>/dev/null || echo "?")MB > 10MB)"
                    tail -5000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
                fi
            fi
        done
    fi

    sleep 60
done
