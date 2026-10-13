#!/bin/bash
# 控制面板（botctl）起停。面板本身是个常驻 HTTP 服务，容器重启后要重新拉起。
#
#   bash botctl_ctl.sh start | stop | status | restart
#
# 2026-10-04 改（用户报「面板入口连接不上」）：
#   · 日志改成**追加**（原来是 >，面板一重启就把上次为什么挂的线索冲掉了）
#   · start 时若「进程活着但端口不通」＝假活，先杀掉再拉
#   · 新增 restart；start 起不来会打印日志尾部
export TZ=Asia/Shanghai   # 容器是 UTC，比手机（北京）慢 8 小时 —— 日志时间戳/人设时段全靠它
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
PIDF=/tmp/botctl.pid
LOG="$WXBOT_LOGDIR/botctl.log"
PAT='python3 botctl\.py$'
URL=http://127.0.0.1:8790

_alive()   { [ -n "$(pgrep -f "$PAT" 2>/dev/null)" ]; }
_pids()    { pgrep -f "$PAT" 2>/dev/null; }
_port_ok() { curl -s -m 5 -o /dev/null "$URL/api"; }

_kill() {
  for pid in $(_pids); do [ "$pid" = "$$" ] || kill "$pid" 2>/dev/null; done
  rm -f "$PIDF"
  for _ in 1 2 3 4 5; do _alive || break; sleep 1; done
}

_start() {
  cd "$DIR" || exit 1
  echo "===== $(date '+%F %T') 启动面板 =====" >> "$LOG"
  nohup python3 botctl.py >> "$LOG" 2>&1 9>&- &
  echo $! > "$PIDF"
  sleep 2
  if _port_ok; then
    echo "✅ 面板已启动：$URL"
    curl -s -m 5 "$URL/api"; echo
  else
    echo "❌ 面板起不来，日志尾部："; tail -20 "$LOG"; exit 1
  fi
}

case "${1:-start}" in
  start)
    if _alive && _port_ok; then echo "面板已经在跑：$URL"; exit 0; fi
    if _alive; then echo "⚠️ 面板进程活着但端口不通（假活），重启…"; _kill; fi
    _start
    ;;
  restart) _kill; _start ;;
  stop)    _kill; echo "面板已停" ;;
  status)
    if _alive; then echo "面板在跑（PID $(_pids | tr '\n' ' ')）"; else echo "面板没在跑"; fi
    if _port_ok; then echo -n "端口 $URL 可访问："; curl -s -m 5 "$URL/api"; echo
    else echo "端口 $URL 不通"; fi
    ;;
  *) echo "用法: $0 {start|stop|status|restart}" ;;
esac
