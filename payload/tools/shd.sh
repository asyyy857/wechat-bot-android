#!/bin/sh
# shd.sh —— 走命令端点守护执行命令（不用 adb，关 WiFi 也能用）
#   用法: sh tools/shd.sh "命令"      |      sh tools/shd.sh --health
. "$(dirname "$0")/../env.sh"
TOK=$(cat "$DSH_ROOT/.shd-token" 2>/dev/null)
if [ "$1" = "--health" ]; then
  curl -s -m 8 -H "X-Token: $TOK" "http://127.0.0.1:$SHD_PORT/health"; echo; exit 0
fi
[ $# -ge 1 ] || { echo "用法: sh tools/shd.sh \"命令\" | --health"; exit 2; }
curl -s -m 60 -H "X-Token: $TOK" --data "$1" "http://127.0.0.1:$SHD_PORT/exec"
