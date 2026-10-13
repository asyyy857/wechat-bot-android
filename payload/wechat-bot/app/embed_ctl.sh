#!/bin/bash
# 本地向量服务（embed_server.py）控制脚本：start / stop / restart / status / log
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
export TZ=Asia/Shanghai
LIB=${WX_OCRLIB:-/root/ocrlibs}
PIDF=/tmp/embed.pid
PORT=${EMBED_PORT:-8766}
LOG="$WXBOT_LOGDIR/embed.log"
source "$DIR/botpids.sh"

_embed_pids() { _pids_by_script embed_server.py; }

stop() {
  [ -f "$PIDF" ] && { kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF"; }
  local pid
  for pid in $(_embed_pids); do kill "$pid" 2>/dev/null; done
  sleep 0.6
}

start() {
  stop
  cd "$DIR" || return 1
  echo "===== $(date '+%F %T') embed start =====" >> "$LOG"
  # ⚠️ 必须带 PYTHONPATH：onnxruntime / numpy 在 /root/ocrlibs 里（见文件头）。
  PYTHONPATH="$LIB" nohup python3 -u embed_server.py >> "$LOG" 2>&1 9>&- &
  echo $! > "$PIDF"
  for _ in $(seq 1 40); do
    sleep 1
    if curl -s -m 3 "http://127.0.0.1:$PORT/health" | grep -q '"ok": *true'; then
      echo "✅ 向量服务已启动（PID $(cat "$PIDF")，port $PORT）"
      curl -s -m 3 "http://127.0.0.1:$PORT/health"; echo
      return 0
    fi
  done
  echo "❌ 向量服务没起来，日志尾部："; tail -5 "$LOG"; return 1
}

case "${1:-status}" in
  start) start ;;
  stop)  stop; echo "已停止" ;;
  restart) start ;;
  status)
    if [ -n "$(_embed_pids)" ]; then echo "✅ 向量服务在跑（PID $(_embed_pids | tr '\n' ' ')）"
    else echo "⭕ 向量服务没在跑"; fi
    echo -n "   "; curl -s -m 5 "http://127.0.0.1:$PORT/health" || echo "连不上 $PORT"
    echo ;;
  log) tail -f "$LOG" ;;
  *) sed -n '2,4p' "$0" ;;
esac
