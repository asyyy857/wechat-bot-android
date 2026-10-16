#!/bin/sh
# start.sh —— 拉起浮窗服务（服务起来后显示常驻细条）
#
# ★ 2026-10-09：改成「先 force-stop 再起」。
#   起因：用户点面板上的「拉起浮窗」后看不到任何东西。查下来是——`hud --off`
#   （`/quit` → `stopSelf()`）之后服务**往往没被真正销毁**（同进程 + START_STICKY），
#   于是再 `am start` 只走 `onStartCommand`：服务活着、HTTP 也通，
#   **浮窗却不会重新挂出来**（实测 visible=false，点了等于没点）。
#   先 force-stop 保证是**全新进程** → 走 onCreate → 细条一定挂出来。
cd "$(dirname "$0")"
sh /sdcard/dsh/tools/shd.sh "am force-stop com.dsh.hud" >/dev/null 2>&1
sleep 1
sh /sdcard/dsh/tools/shd.sh "am start -n com.dsh.hud/.BootActivity" >/dev/null 2>&1
sleep 2
curl -s -m 5 "http://127.0.0.1:18787/ping" || echo "（浮窗服务没起来，试试 install.sh）"
echo
