#!/bin/sh
# stop.sh —— 收起浮窗（停止服务）
hud --off 2>/dev/null || curl -s -m 5 "http://127.0.0.1:18787/quit"
echo
