#!/bin/bash
# ============================================================================
#  botpids.sh —— 进程判据的**唯一**实现（bot.sh / stop.sh / wxbridge_ctl.sh 共用）。
#
#  为什么单独抽出来（2026-10-10 评审 §7.2 / §7.3）：原来是三套不等价的实现 ——
#    · bot.sh   `pgrep -f main_android\.py`（任意子串）→ 把
#      `main_android.py --probe` / `--check-llm` 这类**一次性诊断进程**当成
#      "机器人已经在跑"，于是 `bot.sh start` 拒绝启动（真机踩过）；
#    · stop.sh  `ps|awk '/python3 -u main_android\.py/'`（要求字面 `-u`）→
#      看不到不带 `-u` 起的进程，停不干净；
#    · wxbridge_ctl.sh `pgrep -f "python3 wxbridge\.py$"` → 真身是
#      `python3 -u wxbridge.py`，**精确匹配零命中**，只剩 pidfile 兜底。
#  统一成一条规矩：**从 /proc/<pid>/cmdline 判断，第一个词必须是 python 解释器**
#  （这样"某个 shell 的命令行里恰好含这个文件名"不会被误判），且参数里出现目标
#  脚本名。battery_guard.sh:216 早就是这套写法，这里把它固化成公共实现。
# ============================================================================

# 通用：按"被执行的脚本名"找 python 进程
_pids_by_script() {
  local want="$1" p cmd first
  [ -n "$want" ] || return 0
  for p in $(cd /proc 2>/dev/null && ls -d [0-9]* 2>/dev/null | grep -E '^[0-9]+$'); do
    [ -r "/proc/$p/cmdline" ] || continue
    cmd=$( { tr '\0' ' ' < "/proc/$p/cmdline"; } 2>/dev/null ) || continue
    cmd="${cmd% }"        # ⚠️ tr 出来末尾会多一个空格（battery_guard.sh 踩过这个坑）
    [ -n "$cmd" ] || continue
    first="${cmd%% *}"
    case "${first##*/}" in
      python|python3|python3.*) ;;    # 只认"python 解释器在跑这个脚本"
      *) continue ;;
    esac
    case "$cmd" in
      *"$want"*) echo "$p" ;;
    esac
  done
}

# 机器人本体（排除 --probe / --check-llm 这类一次性诊断进程）
_bot_pids() {
  local p cmd
  for p in $(_pids_by_script main_android.py); do
    cmd=$( { tr '\0' ' ' < "/proc/$p/cmdline"; } 2>/dev/null ) || continue
    case "$cmd" in
      *--probe*|*--check-llm*) continue ;;
    esac
    echo "$p"
  done
}

_bot_running() {
  [ -n "$(_bot_pids)" ]
}

# ★ 2026-10-12：记下"是谁、为什么要停机器人"。
#   起因：日志里只有一行「收到 SIGTERM，退出」，分不清是人在面板上点的、
#   stop.sh、电量看护还是别的什么 —— 那天 20:03 的停机就这样成了悬案。
#   机器人收到信号时会读 /tmp/wxbot.stopwhy 并打进日志（wxauto/bot.py）。
#   规则：**外层调用者先写、内层不覆盖**（60 秒内已有记录就不动）——
#   面板→bot.sh stop→kill，记的是"面板"而不是"bot.sh"。
#   谁在调：用环境变量 WXBOT_STOP_WHO / WXBOT_STOP_WHY 声明（面板、电量看护用），
#   没声明就记 $2 的默认值 + 父进程命令行（兜底线索）。
#   ⚠️ 只写一个小文件，失败一律吞掉：绝不能因为记不下原因而停不掉机器人。
_note_stop() {
  local def_who="${1:-未知}" def_why="${2:-}" f=/tmp/wxbot.stopwhy now ts age
  now=$(date +%s)
  if [ -f "$f" ]; then
    ts=$(sed -n '1p' "$f" 2>/dev/null); ts=${ts:-0}
    case "$ts" in *[!0-9]*) ts=0 ;; esac
    age=$(( now - ts ))
    [ "$age" -ge 0 ] && [ "$age" -lt 60 ] && return 0
  fi
  local who="${WXBOT_STOP_WHO:-$def_who}" why="${WXBOT_STOP_WHY:-$def_why}" pp
  pp=$(tr '\0' ' ' < "/proc/$PPID/cmdline" 2>/dev/null | tr '\n\r' '  ' | cut -c1-80)
  { printf '%s\n' "$now"
    printf '%s | %s | %s（父进程：%s）\n' "$(date '+%F %T')" "$who" "$why" "${pp:-?}"
  } > "$f" 2>/dev/null || true
}

# 桥接服务本体（真身是 `python3 -u wxbridge.py`，别再用 $ 结尾的精确匹配）
_bridge_pids() {
  _pids_by_script wxbridge.py
}
