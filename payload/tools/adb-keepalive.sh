#!/bin/sh
# adb-keepalive.sh —— 常驻看护（**现在是整套东西的看门狗**）。
#
# ★ 2026-10-12 升级（用户要求「看门狗」，借鉴 B 站那个鲸鱼娘的 supervisor.py）：
#   ① 补上**两个原来没人管的缺口**：机器人本体（崩了就一直躺着，直到用户发现
#      "它怎么不说话了"）和向量服务 :8766；
#   ② 加**指数退避**：同一个目标连续失败时，30s → 60s → 120s …… 到 30 分钟封顶，
#      探活一成功立刻复位。为什么需要：坏了的东西 30 秒硬撞一次只会刷日志、白耗电；
#      而且**机器人启动要 50~120 秒**，不退避会"边启动边被判没在跑"。
#
#   sh tools/adb-keepalive.sh [间隔秒=30] &        # 常驻
#   tail -f /tmp/adb-keepalive.log                 # 看它干过什么
#
# 为什么只做这两件事：
#   · adb 断 → 无线调试那条链路死了，脚本自己重连（省得每次手动 connect-adb.sh）；
#   · shell 守护（:18252）→ 文档《受信任虚拟屏自动化 v1.1》说"守护的命绑 adbd"，
#     adbd 一死守护 ~3 秒全灭，所以重连完必须把它拉回来。
# ⚠️ **故意不碰 vs-daemon（虚拟屏守护）**：重起它 = 屏重建 = displayId 变
#   = 微信被踢回物理屏（我们 10-06 已经因此翻过一次车）。那个要人来看一眼
#   再决定，脚本只**报告**不擅自动手。
. "$(dirname "$0")/../env.sh"
INTERVAL=${1:-30}
LOG=/tmp/adb-keepalive.log

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# ---------------------------------------------------------------- adb 探活（★2026-10-12 抖动修复）
# 原来是 `adb -s $SERIAL shell true`，两个毛病（见 adb抖动-原因审查-20261012.md）：
#   ① **没有超时**：transport 半死（TCP 还在、adbd 不响应）时命令会挂住，
#      把 30 秒一轮的看护拖成 2.4 分钟一轮（10-11 实测就是这个节奏）；
#   ② **"慢"被当成"断"**：一次卡顿就触发全端口扫描这种重活。
# 现在分两级判：
#   · 串号不在 `adb devices` 里  → 真断（或端口变了）→ 立刻重连（原行为）；
#   · 串号在、但 shell 探活超时   → 连记 3 轮（约 90s）才重连，单次卡顿不惊动扫描。
ADB_SHELL_TIMEOUT=${ADB_SHELL_TIMEOUT:-8}
ADB_SLOW_FILE=/tmp/adb-shell-slow.n

adb_devices_has() {   # 该串号在 devices 里、且状态是 device？
  adb devices 2>/dev/null | awk -v s="$1" '$1==s && $2=="device"{f=1} END{exit !f}'
}

# ---------------------------------------------------------------- 指数退避
# 每个目标两个小文件：/tmp/wd-<名字>.n（连续失败次数）/ .until（下次允许动手的时间）
BASE=30
MAXWAIT=1800

wd_gate() {   # 现在允许动手吗？（1 = 还在退避期，这段动作整块跳过）
  _u=$(cat "/tmp/wd-$1.until" 2>/dev/null || echo 0)
  [ "$(date +%s)" -ge "${_u:-0}" ]
}

wd_fail() {   # 记一次失败，并按 2 的幂算下次允许时间
  _n=$(( $(cat "/tmp/wd-$1.n" 2>/dev/null || echo 0) + 1 ))
  echo "$_n" > "/tmp/wd-$1.n"
  _d=$BASE
  _i=1
  while [ "$_i" -lt "$_n" ] && [ "$_d" -lt "$MAXWAIT" ]; do
    _d=$(( _d * 2 ))
    _i=$(( _i + 1 ))
  done
  [ "$_d" -gt "$MAXWAIT" ] && _d=$MAXWAIT
  echo $(( $(date +%s) + _d )) > "/tmp/wd-$1.until"
  if [ "$_n" -ge 2 ]; then
    log "⏳ $1 连续 $_n 次没起来 → 退避 ${_d}s 再试"
  fi
}

wd_ok() {     # 探活成功 → 复位
  rm -f "/tmp/wd-$1.n" "/tmp/wd-$1.until"
}

# 机器人本体在不在（★ 用 [.] 是为了不让 pgrep 匹配到它自己那条命令行）
_bot_up() { pgrep -f "main_android[.]py" >/dev/null 2>&1; }

# 物理屏亮着吗？0=亮 / 1=熄屏或锁屏 / 2=读不到
# ⚠️ 按看护的铁律：2（读不到）**什么都不做** —— 绝不把"读不到"当成"用户睡了"。
screen_on() {
  # ★2026-10-12：加 timeout —— 原来 transport 半死时 dumpsys 会挂住整轮看护。
  out=$(timeout 15 adb -s "$SERIAL" shell "dumpsys power | grep mWakefulness=" 2>/dev/null)
  case "$out" in
    *mWakefulness=Awake*) return 0 ;;
    *mWakefulness=*)      return 1 ;;
    *)                    return 2 ;;
  esac
}

log "看护启动（每 ${INTERVAL}s 一检）"
while :; do
  TOK=$(cat "$DSH_ROOT/.shd-token" 2>/dev/null)

  # ① adb 活着吗（用 env.sh 记的串号；没串号就直接重连）
  #    ★2026-10-12 三级判：串号不在 devices（真断/端口变了）→ 立刻重连；
  #      串号在但 shell 超时 → 连续 3 轮才重连；一切正常 → 复位退避。
  if [ -z "$SERIAL" ] || ! adb_devices_has "$SERIAL"; then
    rm -f "$ADB_SLOW_FILE"
    if wd_gate adb; then
      log "adb 不通（serial='$SERIAL' 不在 devices 里）→ 重连"
      ADB_CONNECT_AUTO=1 sh "$DSH_ROOT/connect-adb.sh" >>"$LOG" 2>&1
      . "$DSH_ROOT/env.sh"          # 重新读串号
    fi
    wd_fail adb
  elif ! timeout "$ADB_SHELL_TIMEOUT" adb -s "$SERIAL" shell true >/dev/null 2>&1; then
    _n=$(( $(cat "$ADB_SLOW_FILE" 2>/dev/null || echo 0) + 1 ))
    echo "$_n" > "$ADB_SLOW_FILE"
    if [ "$_n" -ge 3 ] && wd_gate adb; then
      log "adb shell 连续 $_n 次超时（>${ADB_SHELL_TIMEOUT}s，串号仍在 devices）→ 判定链路已坏，重连"
      ADB_CONNECT_AUTO=1 sh "$DSH_ROOT/connect-adb.sh" >>"$LOG" 2>&1
      . "$DSH_ROOT/env.sh"
      rm -f "$ADB_SLOW_FILE"
      wd_fail adb
    else
      log "adb shell 探活超时（第 $_n 次，>${ADB_SHELL_TIMEOUT}s）→ 本轮不重连，下轮再看"
    fi
  else
    rm -f "$ADB_SLOW_FILE"
    wd_ok adb
  fi

  # ② shell 守护活着吗
  if ! curl -s -m 5 -H "X-Token: $TOK" "http://127.0.0.1:$SHD_PORT/health" 2>/dev/null | grep -q 'uid=2000'; then
    if wd_gate shd; then
      log "shell 守护 :$SHD_PORT 没响应 → 重起"
      sh "$DSH_ROOT/shell-daemon/start.sh" "$SHD_PORT" >>"$LOG" 2>&1
    fi
    wd_fail shd
  else
    wd_ok shd
  fi

  # ③ 虚拟屏守护死了 → 重建屏 + 把微信收回虚拟屏（2026-10-06 用户点头自动化）
  #    ⚠️ 这一步会让 displayId 变、微信在手机上闪一下（用户已知情同意）。
  #    ⚠️ 只有 adb 通的时候才做得了 —— **WiFi 真断了这条链子救不回来**，
  #       那种情况只能靠 USB 路线（见文件头 / SKILL.md）。
  if ! curl -s -m 5 -H "X-Token: $TOK" "http://127.0.0.1:$VSD_PORT/list" 2>/dev/null | grep -q '"trusted":true'; then
    if ! wd_gate vsd; then
      wd_fail vsd
      sleep "$INTERVAL"
      continue        # 还在退避期：重建屏太重（displayId 会变），别硬撞
    fi
    log "虚拟屏守护 :$VSD_PORT 没响应 → 重建虚拟屏（displayId 会变）"
    sh "$DSH_ROOT/vscreen-daemon/start.sh" "$VSD_PORT" >>"$LOG" 2>&1
    sleep 8
    if curl -s -m 6 -H "X-Token: $TOK" "http://127.0.0.1:$VSD_PORT/list" 2>/dev/null | grep -q '"trusted":true'; then
      log "虚拟屏重建成功 → 让桥接把微信收回虚拟屏"
      # 桥接可能也死了（adb 断久了它会报错），先探一下，不行就拉起来
      if ! curl -s -m 8 http://127.0.0.1:8765/health 2>/dev/null | grep -q '"ok": *true'; then
        log "桥接没响应 → 重启桥接"
        bash "$DSH_ROOT/wechat-bot/app/wxbridge_ctl.sh" restart >>"$LOG" 2>&1
        sleep 6
      fi
      curl -s -m 120 -X POST http://127.0.0.1:8765/takeover >>"$LOG" 2>&1
      log "已请求桥接接管微信（详见上面的 takeover 返回）"
    else
      log "⚠️ 虚拟屏重建失败 —— 需要人工看一眼"
    fi
    wd_fail vsd
  else
    wd_ok vsd
  fi

  # ④ 浮窗服务（:18787）—— ★ 2026-10-09 加。
  #    用户 2026-10-09 要求删掉那条「DSH 浮窗待命」常驻通知，浮窗服务因此从
  #    **前台服务降级成普通后台服务**（见 hud/src/com/dsh/hud/HudService.java
  #    的 goForeground 注释），被 ColorOS 后台清理的概率上升 —— 这里补一道自愈：
  #    ping 不通就重新拉起，最多 30 秒回来（原来只能等"下一次 hud 调用"）。
  #    ⚠️ /ping 的返回是状态串（visible=… 开头），**不是 JSON** —— 别去 grep ok。
  #    ★ 2026-10-11（用户报「锁屏时一直在震动/手机老是自己亮」）：
  #      **熄屏/锁屏时整段跳过**。ColorOS 在熄屏时会把 com.dsh.hud 冻住
  #      （logcat 实证：`freeze uid: ... com.dsh.hud ... scene: |StrictMode-1|LcdOff`），
  #      /ping 必然超时 —— 那是系统的省电行为，**不是服务挂了**。
  #      照旧 force-stop + am start 拉的后果：① `am start` 会**点亮屏幕**
  #      （10-11 凌晨 2 点这个动作被触发 37 次，等于拿手电筒照用户）；
  #      ② 拉起来的新进程一熄屏又被冻结，纯白折腾。
  #      屏幕亮着才探活；screen_on=2（读不到）照旧探活，别漏了真的挂了。
  screen_on; _scr=$?
  if [ "$_scr" -eq 1 ]; then
    : # 熄屏/锁屏：浮窗被系统冻结是正常的，不拉、不亮屏
  elif ! curl -s -m 5 "http://127.0.0.1:18787/ping" 2>/dev/null | grep -q "visible="; then
    if wd_gate hud; then
      log "浮窗服务 :18787 没响应 → 重新拉起"
      sh "$DSH_ROOT/hud/start.sh" >>"$LOG" 2>&1
    fi
    wd_fail hud
  else
    wd_ok hud
  fi

  # ⑤ OCR 常驻服务（:18790）—— ★ 2026-10-10 加。
  #    日课全流程的识别都走它（tesseract 已降级为兜底）。被 OOM 掉/崩了虽然脚本会自己
  #    ensure 拉起，但那 7 秒恰好卡在"抓帧后要判页面"的节骨眼上；这里每 30s 静默补齐。
  if ! curl -s -m 5 "http://127.0.0.1:${OCR_PORT:-18790}/ping" 2>/dev/null | grep -q '^pong'; then
    if wd_gate ocr; then
      log "OCR 服务 :${OCR_PORT:-18790} 没响应 → 重新拉起"
      sh "$DSH_ROOT/tools/ocr-daemon.sh" start >>"$LOG" 2>&1
    fi
    wd_fail ocr
  else
    wd_ok ocr
  fi

  # ⑥ 向量服务 :8766（记忆库的语义召回）—— ★ 2026-10-12 加。
  #    它挂了只是"退回子串匹配"、不影响回复，但既然能自愈就别让它一直躺着。
  if ! curl -s -m 4 "http://127.0.0.1:${EMBED_PORT:-8766}/health" 2>/dev/null | grep -q '"ok": *true'; then
    if wd_gate vec; then
      log "向量服务 :${EMBED_PORT:-8766} 没响应 → 重新拉起"
      bash "$DSH_ROOT/wechat-bot/app/embed_ctl.sh" start >>"$LOG" 2>&1
    fi
    wd_fail vec
  else
    wd_ok vec
  fi

  # ⑦ 机器人本体 —— ★ 2026-10-12 加（**这是原来最大的缺口**）。
  #    机器人进程崩了（OOM / Python 异常 / 被系统清掉）原来没人管：一直躺着，
  #    直到用户发现"它怎么不说话了"。
  #    ⚠️ 三种「故意停的」绝不能拉起来，否则用户想停都停不掉：
  #      · STOP.txt —— 急停（用户手动建的）
  #      · /tmp/screen-guard.stopped —— 息屏看护停的，等亮屏它自己唤醒
  #      · /tmp/wxbot.intent-stop —— 用户手动 `bot.sh stop` 写的（start 时清掉）
  _want_stop=0
  [ -f "$DSH_ROOT/wechat-bot/data/STOP.txt" ] && _want_stop=1
  [ -f /tmp/screen-guard.stopped ] && _want_stop=1
  [ -f /tmp/wxbot.intent-stop ] && _want_stop=1
  if [ "$_want_stop" = 1 ]; then
    wd_ok bot
  elif _bot_up; then
    rm -f /tmp/wxbot.starting
    wd_ok bot
  else
    _st=$(cat /tmp/wxbot.starting 2>/dev/null || echo 0)
    if [ "$_st" != 0 ] && [ $(( $(date +%s) - _st )) -lt 240 ]; then
      log "机器人启动中（$(( $(date +%s) - _st ))s）→ 这轮不重复拉"
    elif wd_gate bot; then
      log "机器人没在跑 → 后台拉起（启动+切群要 50~120 秒）"
      date +%s > /tmp/wxbot.starting
      # ⚠️ bot.sh 的启动横幅有几十行，别灌进看护日志（那样自愈一次就刷一屏）——
      #    它自己的输出单独落 /tmp/watchdog-bot.log，看护日志只留上面那一行结论。
      ( cd "$DSH_ROOT/wechat-bot/app" && setsid nohup bash bot.sh start >>/tmp/watchdog-bot.log 2>&1 & )
      wd_fail bot
    else
      wd_fail bot
    fi
  fi

  sleep "$INTERVAL"
done
