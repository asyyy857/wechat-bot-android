#!/bin/bash
# 微信机器人总开关
#
#   bash bot.sh start            真跑（会往群里发消息）
#   bash bot.sh start --dry-run  只记录不发（推荐先这么跑）
#   bash bot.sh stop             停
#   bash bot.sh status           看看在不在跑、桥通不通、最近日志
#   bash bot.sh log              跟着看日志
#
# 电量看护（2026-10-05 加）：start 会顺带把 battery_guard.sh 后台拉起来 ——
# 机器人跑着的时候盯手机电量：低于 STOP_BELOW（默认 20%）且没充电就自动走一遍
# stop（停机器人 + 停桥接 + 把微信搬回物理屏）；低于 WARN_BELOW（25%）或用户占着
# 物理屏微信时只降速/让路，不停机（2026-10-07 补）。
# 低电量时 start 会**先拦住不让启动**，真要硬启：bash bot.sh start --force
#
# 不用 pkill -f ...：那条命令行会匹配到执行它的 shell 自己（血泪，见交接说明坑 8）。
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
# ⚠️ 容器本体跑在 UTC，比手机（CST/北京时间）慢 8 小时。不锁时区的话
#    persona_schedule / proactive_window / midnight_window 全部错位 8 小时
#    （配置里写的"深夜 0-7 点"会变成北京时间 8-15 点）。2026-10-03 发现并修。
export TZ=Asia/Shanghai
# ★ 2026-10-12：**凭据不再依赖启动者的环境**。
#   DSH 的 bash 工具不带 DEEPSEEK_API_KEY，从无凭据环境（agent / 看门狗）
#   `bot.sh start` 会让 brain.available=False —— 走模型的回复（被 @、主动接话、
#   识图）**全部静默失效**，而日志里只看得到“模型调用 0 次”。
#   2026-10-12 实测踩到。容器内密钥文件（600，不在 /sdcard）兜底。
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -r /root/.dsh/.deepseek_key ]; then
  DEEPSEEK_API_KEY=$(cat /root/.dsh/.deepseek_key)
  export DEEPSEEK_API_KEY
fi
PIDF=/tmp/wxbot.pid
LOG="$WXBOT_LOGDIR/console.log"

# ★ 2026-10-13（v2.0.0）：日志轮转 —— 单文件超 10MB 滚动一次，保留 5 份。
#   原来日志写 /tmp/wxbot.log 从不清理；挪到 logs/ 之后会长期占用存储，
#   Android 用户很少手动清，所以启动时顺手滚一下。
_rotate_log() {
  [ -f "$LOG" ] || return 0
  _sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  [ "$_sz" -gt 10485760 ] || return 0
  rm -f "$LOG.5" 2>/dev/null
  for _i in 4 3 2 1; do
    [ -f "$LOG.$_i" ] && mv -f "$LOG.$_i" "$LOG.$((_i+1))" 2>/dev/null
  done
  mv -f "$LOG" "$LOG.1" 2>/dev/null
  return 0
}
# ★★ 2026-10-10 评审修复（§7.2）：进程判据统一到 botpids.sh（唯一实现）。
#   原来这里是 `pgrep -f main_android\.py`（任意子串）—— 它会把
#   `main_android.py --probe` / `--check-llm` 这类**一次性诊断进程**当成
#   "机器人已经在跑"，于是 `bot.sh start` 直接拒绝启动；而 stop.sh 那套
#   （要求命令行里有字面 `python3 -u`）又看不到不带 -u 起的进程。两边都能说谎。
source "$DIR/botpids.sh"

_running() {
  _bot_running
}

_stop() {
  local me=$$ waited=0
  _note_stop "bot.sh" "bot.sh ${1:-stop}（命令行或面板的停止/重启）"
  for pid in $(_bot_pids); do
    [ "$pid" = "$me" ] || kill "$pid" 2>/dev/null
  done
  [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
  rm -f "$PIDF"
  # 2026-10-06：群内播报已删（用户说没用），这里等的是它把 finally 走完、
  # 进程真正退出（播报没了之后通常 1~2 秒，上限 25 秒只是兜底），
  # 最多 25 秒；超时就照旧往下走，不卡住启动流程。
  while [ "$waited" -lt 25 ]; do
    _bot_running || break
    sleep 1
    waited=$((waited + 1))
  done
  if [ "$waited" -ge 25 ]; then
    echo "   ⚠️ 机器人 25 秒还没退干净（告别语可能没发完），继续往下走"
  fi
  # 电量看护跟着一起停（否则它会再空转 3 轮才发现机器人没了）
  bash "$DIR/battery_guard.sh" --stop 2>/dev/null | sed 's/^/   🔋/'
  return 0
}

case "${1:-status}" in
  start)
    # --force：跳过「电量太低先别启动」的拦阻。它只是 bot.sh 自己的开关，
    # 不能传给 main_android.py（那边不认识，会当参数报错），所以先摘掉。
    force=0
    restart=0
    args=()
    for a in "$@"; do
      case "$a" in
        --force)   force=1 ;;      # 跳过"电量太低先别启动"的拦阻
        --restart) restart=1 ;;    # 真的要把在跑的机器人重启一遍
        *)         args+=("$a") ;;
      esac
    done
    set -- ${args[@]+"${args[@]}"}

    # ★ 2026-10-08 用户要求（原话「这样是不是启动能快一点？」）：启动要**幂等**。
    #   原来这里无条件往下走（第 77 行的 `_stop`）—— 于是息屏看护的"亮屏唤醒"、
    #   恢复.sh、手动 start，**任何一次调用只要机器人本来就在跑，都会被白白重启
    #   一遍**：先把进程杀掉、切群从头再来，用户多等 18 秒、中间还漏读群消息。
    #   实测证据（2026-10-08）：21:49:59 跑得好好的 → 21:53:13 收到 SIGTERM
    #   → 21:53:18 又 bot start，就是被这条害的。
    #   已在跑就直接退出；确实要重启就 `bash bot.sh start --restart`。
    if _running && [ "$restart" != 1 ]; then
      echo "✅ 机器人已经在跑（PID $(_bot_pids | head -1)），这次不重复启动。"
      echo "   （确实要重启：bash bot.sh start --restart）"
      exit 0
    fi

    # 启动前先看一眼电量（用户 2026-10-05 要求）：机器人在虚拟屏上跑微信 + 截图 OCR，
    # 很费电，刚跑起来就把手机耗到关机就本末倒置了。充着电就不拦（看护配置说了算）。
    if [ "$force" != 1 ]; then
      if ! bmsg=$(bash "$DIR/battery_guard.sh" --check 2>&1); then
        echo "🔋 $bmsg —— 先别启动机器人。"
        echo "   机器人是耗电大户（虚拟屏跑微信 + 截图 OCR + 常驻）。"
        echo "   确实要现在启动：bash bot.sh start --force"
        exit 1
      fi
    fi

    source "$DIR/wxlock.sh" "bot.sh ${*:-start}" || exit 1
    shift
    _stop
    bash "$DIR/setup_ocr.sh" || exit 1
    bash "$DIR/wxbridge_ctl.sh" start >/dev/null || exit 1
    # 顺带把控制面板拉起来（面板和机器人是两条命，用户报过「入口连不上」）
    bash "$DIR/botctl_ctl.sh" start >/dev/null 2>&1 || true
    # ★ 2026-10-12：本地向量服务（记忆库的语义召回用；没起来也只是退回子串匹配，
    #   不影响回复）。恢复.sh 最后就是调 bot.sh start，所以开机也会带上它。
    bash "$DIR/embed_ctl.sh" start >/dev/null 2>&1 || true
    # ★ 2026-10-12：重建名字库（从 chat_log + 群成员前缀）—— 读屏层靠它把
    #   「昵称：正文」粘连的名字切开（实测能合并掉 22 个假身份）。
    #   构建脚本读不到数据也不会非零退出，这里再兑一层 || true。
    python3 "$DIR/build_namebook.py" >/dev/null 2>&1 || true

    # 用户主动启动机器人 = 授权把微信从物理屏拿到虚拟屏。
    # 跑起来之后就不能再主动抢了（用户一碰微信就被抢回去会很难受）。
    if [ "${1:-}" != "--no-takeover" ]; then
      echo "把微信交给机器人（拿到虚拟屏）…"
      curl -s -m 40 -X POST http://127.0.0.1:8765/takeover || true
      echo
    else
      shift 2>/dev/null || true
    fi

    _rotate_log
    cd "$DIR" || exit 1
    # ★ 2026-10-08 审查修复（M16）：这个前台日志原来只增不减（启动时 >> 追加）。
    #   启动前先留最近 2000 行，避免它长到几 MB；历史另有 bot.log 轮转兜着。
    if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 3000 ]; then
      tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    fi
    echo "===== $(date '+%F %T') bot start =====" >> "$LOG"
    # 只认本次启动写进去的行：LOG 是 append 的，不记基线就会读到上一次的「启动完成」
    MARK=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    rm -f /tmp/wxbot.stopwhy   # ★ 2026-10-12：新一轮开始，旧的停机记录作废（免得 120 秒内被误认）
    nohup python3 -u main_android.py "$@" >> "$LOG" 2>&1 9>&- &
    echo $! > "$PIDF"
    # ⚠️ 别再用 sleep 6 就报成功：切群最慢要 40 秒，第 6 秒进程当然还活着 ——
    #    2026-10-03 就是这样骗了 agent 一次，日志里其实写着「切群失败」。
    ok=0
    for _ in $(seq 1 60); do
      sleep 2
      _running || break
      if tail -n +$((MARK + 1)) "$LOG" | grep -q "切群成功"; then ok=1; break; fi
      if tail -n +$((MARK + 1)) "$LOG" | grep -qE "切群失败|标题没能匹配上"; then break; fi
    done
    if _running && [ "$ok" = 1 ]; then
      echo "✅ 机器人已启动（PID $(cat "$PIDF")）  参数：${*:-（真跑）}"
      rm -f /tmp/wxbot.intent-stop /tmp/wxbot.starting   # 用户要它跑，清掉停止标记
      # 电量看护挂上（⚠️ 9>&- 关掉 wxlock 继承的锁 fd，长驻进程必须带）
      bash "$DIR/battery_guard.sh" --stop >/dev/null 2>&1 || true
      # ★★ 2026-10-10 评审修复（§7.1）：长驻看护不仅要关锁 fd（`9>&-`），
      #   还要**清掉 `WX_LOCK_HELD`** —— 它是个 export 出去的环境变量，子进程
      #   一律继承。看护（screen-guard / adb-keepalive）正是"亮屏唤醒机器人"和
      #   "adb 断了自动重启桥接"的入口，它们带着这个标记去调 bot.sh /
      #   wxbridge_ctl.sh 时，wxlock.sh 第 33 行会认为"自己人已经在锁里"直接放行
      #   —— 于是那些自动启动**全程无锁**，与用户手动操作真撞上就打架。
      #   项目为锁 fd 做过细致的 `9>&-`，唯独漏了 env 这条同源通道。
      nohup env -u WX_LOCK_HELD bash "$DIR/battery_guard.sh" >> "$WXBOT_LOGDIR/wxbot-battery.log" 2>&1 9>&- &
      # ★ 2026-10-06：顺手把 adb 自愈看护也挂上（用户要"更方便一点"）——
      #   每 30 秒看一眼：adb 断了自动重连、shell 守护没了自动重起。
      #   见 tools/adb-keepalive.sh 的文件头（那边解释了为什么**不**自动重建虚拟屏）。
      # ⚠️ 2026-10-06：这里原来漏了 `9>&-` —— keepalive / screen-guard 都是长驻进程，
      #   会把 wxlock 的锁 fd 一起继承走，锁就永远不释放（下次启动报
      #   「另一个会话正在操作微信机器人」，实测踩到一次）。长驻进程一律关 fd 9。
      env -u WX_LOCK_HELD sh /sdcard/dsh/tools/start-keepalive.sh >/dev/null 2>&1 9>&- || true
      # ★ 2026-10-06：息屏看护（用户要求：连续息屏 10 分钟停机器人，亮屏按原模式唤醒）
      # ⚠️ 2026-10-06 修：这行原来**漏了末尾的 `&`** —— screen-guard.sh 是死循环，
      #    于是 `bot.sh start` 会**永远卡在这一步**（机器人其实早起来了，脚本不返回）。
      setsid nohup env -u WX_LOCK_HELD sh /sdcard/dsh/tools/screen-guard.sh 600 30 >/dev/null 2>&1 9>&- < /dev/null &
      sleep 1
      bash "$DIR/battery_guard.sh" --status 2>/dev/null | sed 's/^/   🔋 /'
      echo "   日志：$LOG     停止：bash $0 stop"
      echo "--------------------------------------------------"
      tail -n 14 "$LOG"
      # ⚠️ 2026-10-06（实测卡死两次，都是这个原因）：这个脚本常被 `setsid bash bot.sh start`
      #   这样脱离终端来跑，bash 在脚本末尾会 **wait4 它自己起的后台作业**（实测
      #   /proc/<pid>/syscall = wait4(-1)），而 main_android / battery_guard /
      #   screen-guard 都是长驻进程 —— 于是 bot.sh **永不退出**，还一直攥着
      #   wxlock 的 flock（锁 fd 由它持有）。后果：下一次 `bot.sh start` /
      #   `wxbridge_ctl.sh restart` 全被「另一个会话正在操作微信机器人」挡住。
      #   把后台作业从作业表摘掉（disown -a），bash 才会真的退出。
      #
      #   ★ 但「脚本一定退得掉」不能当假设（这台机器上它真的卡过两次，wait4(-1)），
      #     所以再上一道**与进程生死无关**的保险：显式关掉锁 fd（exec 9>&-）。
      #     wxlock 的锁是 flock + fd —— fd 一关，锁立刻释放，脚本就算还赖着也没用。
      exec 9>&- 2>/dev/null || true
      disown -a 2>/dev/null || true
      # 双保险：显式 exit —— 立刻结束 shell，不留给它"再去 wait 一遍后台作业"的机会。
      exit 0
    else
      echo "❌ 启动失败（进程$([ "$ok" = 1 ] && echo 在 || echo 不在)），日志："
      tail -n 20 "$LOG"
      exit 1
    fi
    ;;
  stop)
    # ★ 2026-10-12：给看门狗留个"**是用户想停**"的标记 —— 否则它探到进程没了
    #   会立刻把机器人拉回来（用户就永远停不掉它了）。start 成功时会清掉这个标记。
    echo "$(date '+%F %T') 用户手动停止" > /tmp/wxbot.intent-stop
    rm -f /tmp/wxbot.starting
    _stop
    echo "已停止"
    ;;
  status)
    if _running; then
      echo "✅ 机器人在跑（PID $(_bot_pids | tr '\n' ' ')）"
    else
      echo "⭕ 机器人没在跑"
    fi
    echo -n "   桥接服务： "; curl -s -m 6 http://127.0.0.1:8765/health || echo "连不上"
    echo
    bash "$DIR/battery_guard.sh" --status 2>/dev/null | sed 's/^/   🔋 /'
    if [ -f "$LOG" ]; then
      echo "   最近日志："
      tail -6 "$LOG" | sed 's/^/     /'
    fi
    ;;
  log)
    tail -f "$LOG"
    ;;
  *)
    sed -n '2,12p' "$0"
    ;;
esac
