#!/bin/sh
# rike-bot.sh —— 做日课时让「微信机器人」让路（停 / 恢复）
#
#   sh tools/rike-bot.sh pause    停机器人（先记下"原本在不在跑"）
#   sh tools/rike-bot.sh resume   按记录恢复（原本没跑就不动它）
#   sh tools/rike-bot.sh status   看让路记录 + 机器人现状
#
# ⚠️ 为什么不用 SIGSTOP（挂起）：2026-10-10 实测，**这个 proot 容器里 kill -STOP 对任何
#    进程都无效**（连一个 sleep 都冻不住，状态仍为 S、CPU tick 照涨；手机侧 `kill` 又报
#    rc=1 权限不足）→ 只能走 `bot.sh stop` 真停。
# ✅ 实测收益：机器人停机时全屏 tesseract **871ms**，运行时 **1425ms**（快约 40%）；
#    代价：`bot.sh start` 恢复约 **16 秒**（可接受）。
# 🚨 调用 bot.sh 时**本脚本的命令行里绝不能出现 main_android 的字面量** —— bot.sh 的
#    `pgrep -f` 会匹配到**调用它的 shell 自己**并 SIGTERM 掉（2026-10-10 实测：我的
#    bash 调用就是这么被杀的，因为同一条命令里还 grep 了那个名字）。
#    本脚本的命令行只有 "sh tools/rike-bot.sh pause"，不含该模式，所以安全。
STATE=/tmp/rike-bot.state
BOT=/sdcard/dsh/wechat-bot/app/bot.sh

case "$1" in
  pause)
    if [ -f "$STATE" ]; then echo "ℹ️ 已经让路过了（记录：$(cat "$STATE")）—— 跳过"; exit 0; fi
    if bash "$BOT" status 2>/dev/null | grep -q '机器人在跑'; then
      echo running > "$STATE"
      echo "⏸ 机器人原本在跑 → 停掉它（做完 resume 会自动拉回来）"
      bash "$BOT" stop 2>&1 | tail -2
    else
      echo stopped > "$STATE"
      echo "ℹ️ 机器人本来就没在跑 —— 只记录，不动它"
    fi
    ;;
  resume)
    if [ ! -f "$STATE" ]; then echo "ℹ️ 没有让路记录 —— 什么都不用做"; exit 0; fi
    ST=$(cat "$STATE"); rm -f "$STATE"
    if [ "$ST" = "running" ]; then
      echo "▶️ 原本在跑 → 拉起来"
      bash "$BOT" start 2>&1 | tail -2
    else
      echo "▶️ 原本没在跑 → 保持原样，不启动"
    fi
    ;;
  status)
    echo "让路记录：$([ -f "$STATE" ] && cat "$STATE" || echo '无（没在让路）')"
    bash "$BOT" status 2>&1 | head -2
    ;;
  *) echo "用法: sh tools/rike-bot.sh pause|resume|status"; exit 2 ;;
esac
