#!/bin/sh
# screen-guard.sh —— 息屏看护（用户 2026-10-06 要求）：
#   连续息屏 ≥ SLEEP_STOP 秒（默认 600 = 10 分钟）→ 停掉机器人省电；
#   屏幕一亮 → 自动唤醒（**保持原来的模式**：真跑还是干跑）。
#
#   sh tools/screen-guard.sh [息屏秒数=600] [检查间隔=30] &
#   tail -f /tmp/screen-guard.log
#
# ⚠️ 铁律：**"息屏"和"读不到状态"必须分开**。adb 断的时候 `dumpsys` 会失败，
#   如果把它当成"息屏"，就会在你明明醒着的时候把机器人停掉（还连续计时）。
#   所以三态：awake / asleep / unknown —— unknown 什么都不做。
. "$(dirname "$0")/../env.sh"
SLEEP_STOP=${1:-${SLEEP_STOP:-600}}
INTERVAL=${2:-${INTERVAL:-30}}
LOG=/tmp/screen-guard.log
OFFSINCE=/tmp/screen-guard.offsince      # 这一轮"息屏"是什么时候开始的
STOPPED=/tmp/screen-guard.stopped        # 我们因为它息屏而停了机器人（内容 = 模式）
PIDF=/tmp/screen-guard.pid

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 单例（重复执行安全 —— 别在命令行里 pkill 自己的名字，今天踩过两次）
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "已在跑（PID $(cat "$PIDF")）"; exit 0
fi
echo $$ > "$PIDF"

bot_running() { pgrep -f '[m]ain_android' >/dev/null 2>&1; }
bot_mode() {                                  # dry / real
  if pgrep -af '[m]ain_android' 2>/dev/null | grep -q -- '--dry-run'; then echo dry; else echo real; fi
}
screen_state() {
  . "$DSH_ROOT/env.sh" 2>/dev/null            # 串号可能被 adb 看护更新过
  out=$(timeout 15 adb -s "$SERIAL" shell "dumpsys power | grep -m1 mWakefulness" 2>/dev/null | tr -d '\r')
  case "$out" in
    *Awake*)  echo awake ;;
    *Asleep*) echo asleep ;;
    *)        echo unknown ;;
  esac
}

log "息屏看护启动（息屏 ${SLEEP_STOP}s 就停，每 ${INTERVAL}s 一检）"
while :; do
  st=$(screen_state)
  case "$st" in
    awake)
      rm -f "$OFFSINCE"
      if [ -f "$STOPPED" ]; then
        m=$(cat "$STOPPED"); rm -f "$STOPPED"
        log "亮屏 → 30 秒后唤醒机器人（模式=$m）"
        sleep 30                                  # 别在你刚点亮正要点东西时抢屏
        # ★ 唤醒前先确认 adb 通：睡觉这几个小时里 adb 也可能断过（无线更常见），
        #   而 bot.sh start 全程依赖 adb（截屏/接管）。不通就先接回来，别白启一次。
        . "$DSH_ROOT/env.sh" 2>/dev/null
        # ★2026-10-12 抖动修复：先查 devices（毫秒级）再 shell 探活，且探活必须带超时
        if ! adb devices 2>/dev/null | awk -v s="$SERIAL" '$1==s && $2=="device"{f=1} END{exit !f}' \
           || ! timeout 8 adb -s "$SERIAL" shell true >/dev/null 2>&1; then
          log "唤醒前发现 adb 不通 → 先重连"
          ADB_CONNECT_AUTO=1 sh "$DSH_ROOT/connect-adb.sh" >>"$LOG" 2>&1
          . "$DSH_ROOT/env.sh" 2>/dev/null
        fi
        if [ "$m" = "dry" ]; then
          bash "$DSH_ROOT/wechat-bot/app/bot.sh" start --dry-run >>"$LOG" 2>&1
        else
          bash "$DSH_ROOT/wechat-bot/app/bot.sh" start >>"$LOG" 2>&1
        fi
        log "唤醒完成（模式=$m，机器人$(bot_running && echo 在跑 || echo 没起来)）"
      fi
      ;;
    asleep)
      [ -f "$OFFSINCE" ] || { date +%s > "$OFFSINCE"; log "屏幕熄灭，开始计时"; }
      off=$(cat "$OFFSINCE" 2>/dev/null || echo 0); now=$(date +%s)
      if [ $((now - off)) -ge "$SLEEP_STOP" ] && bot_running; then
        bot_mode > "$STOPPED"
        log "息屏已 $(( (now - off) / 60 )) 分钟 → 停止机器人（模式=$(cat "$STOPPED")，亮屏自动唤醒）"
        bash "$DSH_ROOT/wechat-bot/app/bot.sh" stop >>"$LOG" 2>&1
      fi
      ;;
    *)
      # 读不到状态（adb 断/命令失败）：什么都不做，避免误停
      ;;
  esac
  sleep "$INTERVAL"
done
