#!/bin/bash
# ============================================================================
#  battery_guard.sh —— 电量看护 + 低电量/占屏让路
#  （2026-10-05 新建；2026-10-07 补缺口 —— 用户：「拉起来。补缺口」）
#
#  ⚠️ 2026-10-07 补掉的三个缺口（背景：当天早上约 10% 电量时手机卡死重启）：
#
#   ① **只走 adb 读电量，adb 一断就瞎**：10-07 早上 adb 从 10:28 断到 11:14，
#      看护整整 46 分钟每轮都是「读不到电量 → 这一轮不动」，等于没看护。
#      现在 adb 拿不到就改走**命令守护 :18252**（它是手机上的独立 app_process，
#      adb 断了、DSH App 没重启时仍然活着），两条都不行才报读不到。
#      `--status` 会写明这次用的是哪条通道。
#
#   ② **阈值 10% 太晚**：到 10% 时系统本身已经开始不稳（当晚卡死就在这一档）。
#      STOP_BELOW 10 → **20**；另加 WARN_BELOW=25 的提前告警。
#
#   ③ **低电量 / 用户正占着物理屏微信时，机器人还在死磕**（读屏 + 截图 OCR +
#      反复「自动切回」）—— 白耗电、还给 framework 加负载。现在这两种情况会把
#      **读屏节奏拉长**（写 tuning.json，机器人几秒内自己重载；状态恢复自动还原）。
#      ⚠️ 只拉长间隔，不停机、不丢消息 —— 停机是 STOP_BELOW 那一档的事。
#
#  用法：
#     bash battery_guard.sh                  # 前台跑看护循环（bot.sh start 会自动后台拉起）
#     bash battery_guard.sh --once           # 只看一眼（测试用，真会停）
#     bash battery_guard.sh --once --dry-run # 只看一眼，只打印不真停
#     bash battery_guard.sh --check          # 供 bot.sh 启动前拦一道：0=能启动，1=电量太低且没充电
#     bash battery_guard.sh --read           # 打印 "87 none" / "90 plugged"，给别的脚本用
#     bash battery_guard.sh --status         # 人话版一行状态（bot.sh status / 面板用）
#     bash battery_guard.sh --stop           # 停掉看护进程
#
#  配置：同目录 battery_guard.conf（每轮重读，改完最迟 CHECK_INTERVAL 秒生效）
#
#  ⚠️ 三个踩过的坑，改这个文件时注意：
#   1. 长驻进程必须 `9>&-`（关掉 wxlock 继承来的锁 fd），否则它一直攥着锁 ——
#      见 wxlock.sh 顶部说明。bot.sh 里起看护时带了。
#   2. 找自己的进程**不能**用 `pkill -f battery_guard.sh`：那条命令行本身也含
#      这个名字，会把调用它的 shell 一起杀掉（交接说明坑 8 / 坑 9）。这里扫
#      /proc/<pid>/cmdline，只认「命令行以 battery_guard.sh 结尾」的看护本体。
#   3. **改过 tuning.json 就必须能还原**：退出（--stop / 被 kill / 正常结束）时
#      一律走 _pace_restore，否则机器人下次启动会一直用 300 秒的慢节奏，
#      用户会以为「它不读屏了」。trap 兜住 TERM/INT。
# ============================================================================
export TZ=Asia/Shanghai          # 容器跑在 UTC，比手机慢 8 小时，日志要按北京时间
set -u

DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
CONF="$DIR/battery_guard.conf"
LOG="$WXBOT_LOGDIR/wxbot-battery.log"
PIDF=/tmp/wxbot-battery.pid
MARK=/tmp/wxbot-battery-stopped   # 看护动手停机后留的痕（bot.sh status / 面板会用）
TUNING="$DIR/tuning.json"         # 读屏节奏（面板也写这个文件）
PACE_SAVE=/tmp/wxbot-pace-orig    # 我们降速前 poll_interval 的原值（只有一行）
YIELDF=/tmp/wxbot-yield.flag      # 正在为用户让路的痕（给人看）
SHD_PORT=${SHD_PORT:-18252}
PAT='main_android\.py'

# ---- 默认值（可被 battery_guard.conf 覆盖）----
STOP_BELOW=20            # 低于它就停机（10 → 20：10% 时系统已经不稳了）
WARN_BELOW=25            # 低于它开始告警 + 降速（仍高于停机线）
SKIP_WHEN_CHARGING=1     # 1 = 插着充电器就不停、也不降速
CHECK_INTERVAL=600       # 每多少秒看一眼（2026-10-07 用户：60 → 600）
SLOW_INTERVAL=120        # 低电量时的读屏间隔（正常是 15 秒左右）
YIELD_INTERVAL=60        # 用户占着物理屏微信时的读屏间隔（2026-10-07 用户要求 300 → 60）
YIELD_AFTER=86400        # 让路门槛（2026-10-07 用户选「不再让路」→ 实际关闭）

DRY_RUN=0
MODE="${1:-run}"
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1
[ "${1:-}" = "--dry-run" ] && { MODE=run; DRY_RUN=1; }

B_LEVEL=""      # 电量百分数（读不到就是空）
B_PLUG=0        # 1=插着充电器（AC/USB/无线）
SRCF=/tmp/wxbot-battery.src   # 最近一次取数据走的通道：adb / shd / none
# ⚠️ 必须落成文件：_shell 是被 `out=$(...)` 命令替换调用的，在**子 shell** 里赋值
#    传不回主进程（2026-10-07 自己踩的：电量读到了 22%，来源却显示「都读不到」）。

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

_cfg() { [ -f "$CONF" ] && . "$CONF" 2>/dev/null; return 0; }

# ---------------------------------------------------------------- 取数据通道
# 先 adb，不行再走命令守护 :18252（shell 身份的小 HTTP 服务）。
# 输出走 stdout，通道名写进 $SRCF。
_shell() {
  local cmd="$1" out="" S="" TOK=""
  S=$(cat /sdcard/dsh/.adb-serial 2>/dev/null | tr -d '\r\n')
  if [ -n "$S" ]; then
    out=$(timeout 25 adb -s "$S" shell "$cmd" 2>/dev/null | tr -d '\r')
  else
    out=$(timeout 25 adb shell "$cmd" 2>/dev/null | tr -d '\r')
  fi
  if [ -n "$out" ]; then printf 'adb\n' > "$SRCF"; printf '%s' "$out"; return 0; fi
  # 备用通道：命令守护（2026-10-07 加 —— 它是独立进程，adb 断了它多半还在）
  TOK=$(cat /sdcard/dsh/.shd-token 2>/dev/null)
  if [ -n "$TOK" ]; then
    out=$(curl -s -m 25 -H "X-Token: $TOK" --data "$cmd" \
          "http://127.0.0.1:$SHD_PORT/exec" 2>/dev/null | tr -d '\r')
    out=$(printf '%s' "$out" | grep -v '^\[EXIT=')
  fi
  if [ -n "$out" ]; then printf 'shd\n' > "$SRCF"; printf '%s' "$out"; return 0; fi
  printf 'none\n' > "$SRCF"
  return 1
}

_read_battery() {
  local out ac usb wl
  out=$(_shell "dumpsys battery")
  B_LEVEL=$(printf '%s\n' "$out" | awk -F: '/^[[:space:]]*level:/{gsub(/[^0-9]/,"",$2); print $2}')
  ac=$(printf '%s\n' "$out" | awk -F: '/^[[:space:]]*AC powered:/{gsub(/[[:space:]]/,"",$2); print $2}')
  usb=$(printf '%s\n' "$out" | awk -F: '/^[[:space:]]*USB powered:/{gsub(/[[:space:]]/,"",$2); print $2}')
  wl=$(printf '%s\n' "$out" | awk -F: '/^[[:space:]]*Wireless powered:/{gsub(/[[:space:]]/,"",$2); print $2}')
  if [ "$ac" = "true" ] || [ "$usb" = "true" ] || [ "$wl" = "true" ]; then B_PLUG=1; else B_PLUG=0; fi
  case "$B_LEVEL" in ''|*[!0-9]*) B_LEVEL="" ;; esac
}

# 物理屏（display 0）当前前台组件名；读不到就空。
_phys_fg() {
  local out=""
  out=$(_shell 'dumpsys activity activities | grep -E "Display #|topResumedActivity"')
  printf '%s\n' "$out" | awk '
    /^[[:space:]]*Display #/ { in0 = ($2 == "#0"); next }
    in0 && /topResumedActivity=/ {
      if (match($0, /[A-Za-z0-9_.]+\/[A-Za-z0-9_.$]+/)) { print substr($0, RSTART, RLENGTH); exit }
    }'
}

# 0=正常  1=该停  2=读不到电量  3=低于阈值但插着电（按配置不停）
_decide() {
  [ -n "$B_LEVEL" ] || return 2
  [ "$B_LEVEL" -lt "$STOP_BELOW" ] || return 0
  if [ "$B_PLUG" = 1 ] && [ "$SKIP_WHEN_CHARGING" = 1 ]; then return 3; fi
  return 1
}

_plug_txt() { [ "$B_PLUG" = 1 ] && echo "充电中" || echo "未充电"; }

# ---------------------------------------------------------------- 读屏节奏
_pace_read() {
  python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("poll_interval",""))
except Exception: print("")' "$TUNING" 2>/dev/null
}

_pace_same() {
  python3 -c 'import sys
try:
    a=float(sys.argv[1]); b=float(sys.argv[2])
    sys.exit(0 if abs(a-b)<0.01 else 1)
except Exception: sys.exit(1)' "$1" "$2" 2>/dev/null
}

_pace_write() {
  python3 - "$TUNING" "$1" <<'PY' 2>/dev/null
import json,sys,time
p,v=sys.argv[1],float(sys.argv[2])
try:
    d=json.load(open(p))
    if not isinstance(d,dict): d={}
except Exception:
    d={}
d["poll_interval"]=v; d["updated"]=time.time()
json.dump(d,open(p,"w"),ensure_ascii=False,indent=2)
PY
}

# 降速到 $1 秒（$2=原因）。空参数 = 还原。
_pace_apply() {
  local target="${1:-}" why="${2:-}" cur
  if [ -z "$target" ]; then _pace_restore; return 0; fi
  cur=$(_pace_read)
  if [ ! -f "$PACE_SAVE" ]; then
    printf '%s\n' "${cur:-}" > "$PACE_SAVE"      # 第一次降速：记下原值
  elif [ -n "$cur" ] && _pace_same "$cur" "$target"; then
    return 0                                      # 已经在目标节奏上，别反复写
  fi
  _pace_write "$target"
  log "⚙️ 读屏节奏 ${cur:-默认} → ${target}s（${why}）"
  printf '%s\n' "$(date '+%F %T') 让路：${why}（读屏 ${target}s）" > "$YIELDF"
}

_pace_restore() {
  [ -f "$PACE_SAVE" ] || { rm -f "$YIELDF"; return 0; }
  local orig cur
  orig=$(cat "$PACE_SAVE" 2>/dev/null)
  rm -f "$PACE_SAVE"
  rm -f "$YIELDF"
  [ -n "$orig" ] || return 0
  cur=$(_pace_read)
  if [ -n "$cur" ] && _pace_same "$cur" "$orig"; then return 0; fi
  _pace_write "$orig"
  log "⚙️ 读屏节奏已还原到 ${orig}s"
  return 0
}

# $$ 的所有祖先 pid（一行一个）—— 避免「停看护」时把正在调用它的看护本体杀了：
# battery_guard.sh 动手停机器人时会走 stop.sh，stop.sh 里又调 --stop，
# 那时看护正是调用者的祖先，杀了它 stop.sh 就成孤儿了。
_ancestors() {
  local p=$$ pp
  while [ "${p:-1}" -gt 1 ] 2>/dev/null; do
    pp=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null)
    [ -n "${pp:-}" ] || break
    echo "$pp"
    p=$pp
  done
}

_stop_guards() {
  local me=$$ p cmd anc
  anc=" $(_ancestors | tr '\n' ' ') "
  if [ -f "$PIDF" ]; then
    p=$(cat "$PIDF" 2>/dev/null)
    if [ -n "${p:-}" ] && kill "$p" 2>/dev/null; then echo "   停掉电量看护（PID $p）"; fi
    rm -f "$PIDF"
  fi
  # 兜底：只认「cmdline 以 battery_guard.sh 结尾」的看护本体
  for p in $(cd /proc 2>/dev/null && ls -d [0-9]* 2>/dev/null | grep -E '^[0-9]+$'); do
    [ "$p" = "$me" ] && continue
    case "$anc" in *" $p "*) continue ;; esac      # 自己的祖先（看护本体）不动
    [ -r "/proc/$p/cmdline" ] || continue
    cmd=$( { tr '\0' ' ' < "/proc/$p/cmdline"; } 2>/dev/null ) || continue
    cmd="${cmd% }"    # ⚠️ tr 出来的 cmdline 末尾带一个空格；不去掉的话下面的
                      #    `*battery_guard.sh` **永远匹配不上** —— 2026-10-07 实测：
                      #    --stop 只杀得掉 PIDF 里那一个，剩下的接着跑（日志成对出现）。
    case "$cmd" in
      *battery_guard.sh|*battery_guard.sh\ --dry-run)
        kill "$p" 2>/dev/null && echo "   停掉电量看护（PID $p）" ;;
    esac
  done
  rm -f "$PIDF" 2>/dev/null
  return 0
}

_bot_running() { pgrep -f "$PAT" >/dev/null 2>&1; }

_src_txt() {
  case "$(cat "$SRCF" 2>/dev/null)" in
    adb) echo "adb" ;;
    shd) echo "命令守护(${SHD_PORT})" ;;
    *)   echo "都读不到" ;;
  esac
}

case "$MODE" in
  --check)
    _cfg; _read_battery; _decide; d=$?
    if [ "$d" = 1 ]; then
      echo "电量 ${B_LEVEL}%（$(_plug_txt)）低于 ${STOP_BELOW}%"
      exit 1
    fi
    exit 0
    ;;
  --read)
    _cfg; _read_battery
    if [ -z "$B_LEVEL" ]; then echo "? ?"; else echo "$B_LEVEL $([ "$B_PLUG" = 1 ] && echo plugged || echo none)"; fi
    exit 0
    ;;
  --status)
    _cfg; _read_battery
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
      g="开（PID $(cat "$PIDF")，低于 ${STOP_BELOW}% 自动停$([ "$SKIP_WHEN_CHARGING" = 1 ] && echo '，充电时不看'))"
    else
      g="没在跑（机器人启动时会自动拉起）"
    fi
    if [ -z "$B_LEVEL" ]; then
      printf '电量 读不到（adb / 命令守护都没回话） · 看护 %s\n' "$g"
    else
      printf '电量 %s%%（%s，来源 %s） · 看护 %s\n' \
        "$B_LEVEL" "$(_plug_txt)" "$(_src_txt)" "$g"
    fi
    if [ -f "$YIELDF" ]; then printf '   %s\n' "$(cat "$YIELDF")"; fi
    if [ -f "$MARK" ]; then printf '   %s\n' "$(cat "$MARK")"; fi
    exit 0
    ;;
  --stop)
    _stop_guards
    _pace_restore
    exit 0
    ;;
  --once)
    _cfg; _read_battery; _decide; d=$?
    case "$d" in
      0) echo "电量 ${B_LEVEL}%（$(_plug_txt)）→ 正常，不停（停机线 ${STOP_BELOW}%）" ;;
      3) echo "电量 ${B_LEVEL}%（充电中）→ 低于停机线，但插着电，按配置不停" ;;
      2) echo "读不到电量（adb / 命令守护都没回话）→ 这一轮不动" ;;
      1)
        if [ "$DRY_RUN" = 1 ]; then
          echo "电量 ${B_LEVEL}%（$(_plug_txt)）→ 低于 ${STOP_BELOW}%，**该停**（dry-run：没真停）"
        else
          echo "电量 ${B_LEVEL}%（$(_plug_txt)）→ 低于 ${STOP_BELOW}%，停机器人…"
          printf '%s 电量 %s%% 低于 %s%%，电量看护已停机\n' \
            "$(date '+%F %T')" "$B_LEVEL" "$STOP_BELOW" > "$MARK"
          WXBOT_STOP_WHO="电量看护" WXBOT_STOP_WHY="电量 ${B_LEVEL}% 低于 ${STOP_BELOW}%" \
            timeout 240 bash "$DIR/stop.sh" 2>&1 | tail -10
        fi
        ;;
    esac
    exit 0
    ;;
esac

# ---------------------------------------------------------------- 看护主循环
rm -f "$MARK" 2>/dev/null            # 新一轮看护开始，清掉上一次的停机痕
echo $$ > "$PIDF"
trap '_pace_restore; if [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ]; then rm -f "$PIDF"; fi; exit 0' TERM INT
#   ★ 2026-10-08 审查修复（F5）：这一行原来**没有写信号名** —— bash 会直接报
#     `trap: usage: ...` 并且**什么也没注册**（/tmp/wxbot-battery.log 里有 31 次
#     这个报错，每次都紧跟看护退出）。后果：被 SIGTERM / 容器重启 / OOM 杀掉时，
#     PIDF 不清理（`--status` 和面板会一直说"看护在跑"，其实早没了）。
#     `_pace_restore` 本身在主循环里还有几处兜底（所以节奏不会永久卡住），
#     但"退出必清理"这条必须真的生效。
#     下面这一行是自检：trap 没注册上就当场退出并说清楚，别带着坏状态继续跑。
if [ -z "$(trap -p TERM 2>/dev/null)" ]; then
  echo "❌ battery_guard: trap 注册失败（TERM）—— 拒绝带病运行" >&2
  exit 1
fi
#   ⚠️ 上面那个 `[ ... = $$ ]` 判断**不能省**：--stop 杀掉旧看护后，旧看护要等它那段
#      sleep 睡完才执行 trap；如果无条件删 PIDF，会把**新**看护的 PIDF 删掉 ——
#      表现成「--status 说没在跑，其实它活得好好的」（2026-10-07 实测踩到）。
_cfg                                 # 先把配置读进来，别让下面这行打印出默认值
log "电量看护启动（停机线 ${STOP_BELOW}%，警戒 ${WARN_BELOW}%，充电时$([ "$SKIP_WHEN_CHARGING" = 1 ] && echo '不停不降速' || echo '照样停')，每 ${CHECK_INTERVAL} 秒看一眼，dry-run=${DRY_RUN}）"
log "  读数通道：先 adb，不行退到命令守护 :${SHD_PORT}（2026-10-07 补）"

gone=0
round=0
yield_secs=0
warn_band=""

while :; do
  _cfg; _read_battery; _decide; d=$?
  round=$((round + 1))
  case "$d" in
    0) log "电量 ${B_LEVEL}%（$(_plug_txt)，$(cat "$SRCF" 2>/dev/null || echo none)）→ 正常" ;;
    3) log "电量 ${B_LEVEL}%（充电中，$(cat "$SRCF" 2>/dev/null || echo none)）→ 低于停机线但插着电，先不停" ;;
    2) log "读不到电量（adb / 命令守护都没回话）→ 这一轮不动" ;;
    1)
      log "⚠️ 电量 ${B_LEVEL}%（$(_plug_txt)）低于 ${STOP_BELOW}% → 停机器人"
      if [ "$DRY_RUN" = 1 ]; then
        log "（dry-run：只记录，没真停）"
      else
        printf '%s 电量 %s%% 低于 %s%%，电量看护已停机\n' \
          "$(date '+%F %T')" "$B_LEVEL" "$STOP_BELOW" > "$MARK"
        _pace_restore
        # ★ 2026-10-10 评审修复：带着 --from-battery 调 stop.sh —— 否则
        #   stop.sh:52 会把我们刚写下的停机痕（$MARK）立刻删掉，于是
        #   `bot.sh status` 和面板的红字永远看不到"为什么停机"。
        out=$(WXBOT_STOP_WHO="电量看护" WXBOT_STOP_WHY="电量 ${B_LEVEL}% 低于 ${STOP_BELOW}%" \
              timeout 240 bash "$DIR/stop.sh" --from-battery 2>&1); rc=$?
        log "stop.sh 退出码 ${rc}：$(printf '%s' "$out" | tail -3 | tr '\n' '|')"
        [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] && rm -f "$PIDF"
        exit 0
      fi
      ;;
  esac

  # ---- 警戒线告警（每 5% 一档，别刷屏）----
  if [ -n "$B_LEVEL" ] && [ "$B_PLUG" != 1 ] && [ "$B_LEVEL" -le "$WARN_BELOW" ]; then
    band=$(( B_LEVEL / 5 ))
    if [ "$band" != "$warn_band" ]; then
      log "⚠️ 电量 ${B_LEVEL}%（未充电）进入 ${WARN_BELOW}% 警戒区：读屏降到 ${SLOW_INTERVAL}s，${STOP_BELOW}% 自动停机"
      warn_band=$band
    fi
  else
    warn_band=""
  fi

  # ---- 让路判定：用户正占着物理屏微信 ----
  # 铁律同 screen-guard：**「用户在忙」和「读不到状态」必须分开** ——
  # 读不到（adb/命令守护都挂了）什么都不做，绝不能当成「用户在用」。
  if _bot_running; then
    fg=$(_phys_fg)
    case "$fg" in
      com.tencent.mm*) yield_secs=$((yield_secs + CHECK_INTERVAL)) ;;
      "")              : ;;                    # 读不到 → 保持原状，不累加也不清零
      *)               yield_secs=0 ;;
    esac
  else
    yield_secs=0
  fi

  # ---- 目标节奏（低电量优先于让路：取更慢的那个）----
  target=""
  if [ -n "$B_LEVEL" ] && [ "$B_PLUG" != 1 ] && [ "$B_LEVEL" -lt "$WARN_BELOW" ]; then
    target="$SLOW_INTERVAL"
    why="电量 ${B_LEVEL}% 低于警戒 ${WARN_BELOW}%"
  fi
  if [ "$yield_secs" -ge "$YIELD_AFTER" ]; then
    target="$YIELD_INTERVAL"
    why="用户在物理屏用微信 ${yield_secs}s"
  fi
  _pace_apply "$target" "${why:-}"
  if [ -z "$target" ] && [ "$yield_secs" -eq 0 ]; then rm -f "$YIELDF" 2>/dev/null; fi

  # 机器人已经被停了（人手动停的 / 崩了）→ 看护退休，别空转
  if _bot_running; then gone=0; else gone=$((gone + 1)); fi
  if [ "$gone" -ge 3 ]; then
    log "机器人已不在跑（连续 3 轮），电量看护退出"
    _pace_restore
    [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] && rm -f "$PIDF"
    exit 0
  fi

  # 日志别无限长：超过 3000 行就只留最后 1000 行
  if [ "$round" -ge 60 ]; then
    round=0
    if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 3000 ]; then
      tail -1000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    fi
  fi

  # ⚠️ 分片睡：整段 `sleep 60` 会让 TERM 最多迟 60 秒才被处理，那段时间看护
  #    还活着、还在写日志，甚至会去删新看护的 PIDF（2026-10-07 实测）。
  #    切成 5 秒一片，最多迟 5 秒。
  slept=0
  while [ "$slept" -lt "$CHECK_INTERVAL" ]; do
    sleep 5
    slept=$((slept + 5))
  done
done
