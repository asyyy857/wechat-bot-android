#!/bin/sh
# ocr-daemon.sh —— OCR 常驻服务（RapidOCR）管理 / 保活
#   sh tools/ocr-daemon.sh start|stop|restart|status|ensure|warm
# ensure: 不通就拉起（给 keepalive 用，静默）
. "$(dirname "$0")/../env.sh"
PORT=${OCR_PORT:-18790}
PY=${OCR_PY:-python3}
LIB=${WX_OCRLIB:-/root/ocrlibs}
LOG=/tmp/ocr-daemon.log
D=$(cd "$(dirname "$0")" && pwd)

alive(){ curl -s --max-time 2 "http://127.0.0.1:$PORT/ping" 2>/dev/null | grep -q '^pong'; }
pids(){ pgrep -f "ocr-daemon.py" 2>/dev/null; }

start(){
  if alive; then echo "  ℹ️ OCR 服务已在运行：$(curl -s --max-time 2 http://127.0.0.1:$PORT/ping)"; return 0; fi
  for p in $(pids); do kill "$p" 2>/dev/null; done
  nohup env PYTHONPATH="$LIB" DSH_ROOT="$DSH_ROOT" OCR_PORT="$PORT" \
    "$PY" "$D/ocr-daemon.py" > "$LOG" 2>&1 &
  i=0
  while [ $i -lt 30 ]; do
    alive && { echo "  ✅ OCR 服务就绪：$(curl -s --max-time 2 http://127.0.0.1:$PORT/ping)"; return 0; }
    i=$((i+1)); sleep 1
  done
  echo "  ❌ OCR 服务 30s 没起来，看 $LOG"; return 1
}
stop(){ for p in $(pids); do kill "$p" 2>/dev/null && echo "  ▸ 已停 pid=$p"; done; sleep 1; alive && echo "  ⚠️ 还没停干净" || echo "  ✅ 已停"; }
status(){ if alive; then echo "  ✅ 运行中：$(curl -s --max-time 2 http://127.0.0.1:$PORT/ping)  stats=$(curl -s --max-time 2 http://127.0.0.1:$PORT/stats)"; else echo "  ❌ 未运行"; fi; }
case "$1" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  warm)    alive && curl -s --max-time 30 "http://127.0.0.1:$PORT/warm" >/dev/null && echo "  ✅ 已预热" || echo "  ❌ 服务不在" ;;
  ensure)  alive || start >/dev/null 2>&1 ;;
  *) echo "用法: sh tools/ocr-daemon.sh start|stop|restart|status|ensure|warm"; exit 2 ;;
esac
