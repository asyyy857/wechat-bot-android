#!/bin/sh
# rike-live.sh —— 直播页服务的启停（★2026-10-10）
#   sh tools/rike-live.sh start | stop | status
. "$(dirname "$0")/../env.sh"
PORT=${RIKE_LIVE_PORT:-18800}
PIDF=/tmp/rike-live.pid
LOG=/tmp/rike-live.log
case "$1" in
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "✅ 已在跑（PID $(cat "$PIDF")）→ http://127.0.0.1:$PORT"; exit 0
    fi
    nohup node "$DSH_ROOT/tools/rike-live.js" "$PORT" >"$LOG" 2>&1 &
    echo $! > "$PIDF"
    sleep 2
    if curl -s -m 3 "http://127.0.0.1:$PORT/health" | grep -q ok; then
      echo "✅ 直播页已启动 → 手机浏览器打开: http://127.0.0.1:$PORT"
    else
      echo "❌ 起不来，日志："; tail -5 "$LOG"
    fi ;;
  stop)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF"
    echo "已停止" ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "✅ 在跑（PID $(cat "$PIDF")）：$(curl -s -m 3 http://127.0.0.1:$PORT/health)"
    else echo "⭕ 没在跑"; fi ;;
  *) echo "用法: sh tools/rike-live.sh start|stop|status" ;;
esac
