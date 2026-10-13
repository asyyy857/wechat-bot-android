#!/bin/sh
# 恢复.sh —— 容器/手机重启后跑这一个就够了（用户 2026-10-06 问"重启后怎么办"）。
#
#   sh /sdcard/dsh/恢复.sh
#
# 它按顺序做（缺什么补什么，已就绪的会跳过）：
#   1) bootstrap.sh —— 容器重建过才需要（装 adb/java、挂 vscreen/shd 命令、确保 dex 在）
#   2) adb 连接（无线掉了就重连）
#   3) 两个守护（vs / shd）—— **它们跑在手机上、不在容器里**，通常还活着；
#      真没了（手机重启/adbd 断过）才重起 —— ⚠️ 重起 vs 会换 displayId、微信会掉下屏
#   4) 机器人整套（bot.sh start：桥接 + 面板 + 机器人 + adb看护 + 息屏看护）
# ★ 2026-10-08：容器时区修正（幂等、极快，故意放在最前面 —— 在"机器人已在跑就
#   exit 0"之前，否则机器人跑着的时候永远轮不到它）。
#   病根：镜像里 /etc/localtime → Etc/UTC，而 /etc/timezone 写着 Asia/Shanghai
#   （两者自相矛盾，glibc 只认 localtime）。后果是 agent 的 date/ls/stat 比手机
#   慢 8 小时，排查时老对不上机器人日志的时间戳。这里每次开机拽回北京时间。
if [ "$(readlink /etc/localtime)" != "/usr/share/zoneinfo/Asia/Shanghai" ]; then
  ln -sfn /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
fi

. /sdcard/dsh/env.sh

# ★ 2026-10-10：App 每次更新都会把 dsh-vscreen 插件的端口覆盖回坏的 18240/18242
#   （见 tools/fix-vscreen-ports.sh 的注释）—— 每次开机补一遍，幂等、极快。
sh "$DSH_ROOT/tools/fix-vscreen-ports.sh"

# ★ 2026-10-10：App 每次重启也会把两份 AGENTS.md 打回内置版（那份没有第 0 节语言
#   规则、也没有第 6 节的本机虚拟屏覆盖）—— 每次开机把底稿重新铺回去。
#   （语言规则的实时兜底仍是 AGENTS.local.md，它不会被覆盖；这里是补全 AGENTS.md。）
sh "$DSH_ROOT/restore-agents.sh" >/dev/null 2>&1 || true

# ★ 2026-10-06：这个脚本现在会被 DSH 自启插件**每次开机都调一次**，
#   所以第一件事是"看好就别动" —— 否则每次打开 App 都会白重启一次机器人。
if pgrep -f '[m]ain_android' >/dev/null 2>&1; then
  echo "机器人已经在跑 —— 什么都不做（要看细节就 bash wechat-bot/app/bot.sh status）"
  exit 0
fi

echo "=== 1/4 容器环境 ==="
# 只在关键命令缺失时跑 bootstrap（它要联网装东西，慢）
if ! command -v adb >/dev/null 2>&1 || ! command -v java >/dev/null 2>&1; then
  sh "$DSH_ROOT/bootstrap.sh"
else
  echo "  已就绪（adb/java 都在）"
fi

echo "=== 2/4 adb ==="
if ! adb devices 2>/dev/null | grep -q 'device$'; then
  sh "$DSH_ROOT/connect-adb.sh"
else
  echo "  已连接：$(adb devices | awk '/device$/{print $1}' | head -1)"
fi
. "$DSH_ROOT/env.sh"

echo "=== 3/4 守护 ==="
TOK=$(cat "$DSH_ROOT/.shd-token")
if curl -s -m 5 -H "X-Token: $TOK" "http://127.0.0.1:$SHD_PORT/health" 2>/dev/null | grep -q uid=2000; then
  echo "  shell 守护在（:$SHD_PORT）"
else
  echo "  重起 shell 守护"; sh "$DSH_ROOT/shell-daemon/start.sh" "$SHD_PORT"
fi
if curl -s -m 5 -H "X-Token: $TOK" "http://127.0.0.1:$VSD_PORT/list" 2>/dev/null | grep -q trusted; then
  echo "  虚拟屏守护在（:$VSD_PORT）"
else
  echo "  重起虚拟屏守护（⚠️ displayId 会变，微信稍后会被接管回屏）"
  sh "$DSH_ROOT/vscreen-daemon/start.sh" "$VSD_PORT"
fi

echo "=== 4/4 机器人整套 ==="
bash "$DSH_ROOT/wechat-bot/app/bot.sh" start
