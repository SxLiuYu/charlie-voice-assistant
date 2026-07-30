#!/bin/bash
# 魔幻手机 - 自动重启看门狗
# 每60秒检查服务健康，挂了自动重启
# 用法: screen -dmS watchdog bash watchdog.sh

cd "$(dirname "$0")"
source .venv/bin/activate
LOG=/tmp/voice_srv.log
HTTPS_LOG=/tmp/voice_https.log
RESTART_COUNT=0

while true; do
    # 检查HTTP
    HTTP_OK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:8000/health 2>/dev/null)
    # 检查HTTPS
    HTTPS_OK=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 https://localhost:8443/health 2>/dev/null)

    NEED_RESTART=0

    if [ "$HTTP_OK" != "200" ]; then
        echo "[$(date '+%H:%M:%S')] HTTP服务异常($HTTP_OK)，重启中..."
        pkill -9 -f "python voice_server.py" 2>/dev/null
        sleep 2
        screen -dmS voice bash -c "source .venv/bin/activate && python voice_server.py > $LOG 2>&1"
        NEED_RESTART=1
    fi

    if [ "$HTTPS_OK" != "200" ]; then
        echo "[$(date '+%H:%M:%S')] HTTPS服务异常($HTTPS_OK)，重启中..."
        pkill -9 -f "python https_server.py" 2>/dev/null
        sleep 2
        screen -dmS voice-https bash -c "source .venv/bin/activate && python https_server.py > $HTTPS_LOG 2>&1"
        NEED_RESTART=1
    fi

    if [ $NEED_RESTART -eq 1 ]; then
        RESTART_COUNT=$((RESTART_COUNT + 1))
        echo "[$(date '+%H:%M:%S')] 重启完成 (累计重启$RESTART_COUNT次)"
        sleep 15  # 等服务恢复
    else
        # 日志轮转: 超过10MB截断
        for f in $LOG $HTTPS_LOG; do
            if [ -f "$f" ]; then
                SIZE=$(stat -f%z "$f" 2>/dev/null || echo 0)
                if [ $SIZE -gt 10485760 ]; then
                    echo "[$(date '+%H:%M:%S')] 日志轮转: $f (${SIZE}字节 > 10MB)"
                    tail -5000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
                fi
            fi
        done
    fi

    sleep 60
done
