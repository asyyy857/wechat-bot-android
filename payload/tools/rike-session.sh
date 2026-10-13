#!/bin/sh
# rike-session.sh —— 逆水寒日课「开工体检 / 收尾验证」（★2026-10-10 加）
#
# 为什么有它（两个实测教训）：
#   ① 收尾用的 `bg.sh stop` 只负责**发** am force-stop，**不验证**；
#      2026-10-10 实测：以为关掉了，其实逆水寒在副屏又挂了一整夜
#      （pidof=12207 还活着，白吃 ~0.5 核 + 2.4G 内存 + 电量）。
#   ② 开工依赖（虚拟屏守护/命令守护/OCR 守护/adb）任一没起来，
#      日课第一步就会以"抓帧失败/识别不到"开场 —— 提前 3 秒查出来最省事。
#
#   sh tools/rike-session.sh open     开工准备一条龙：体检 + 机器人让路 + 起直播页（不碰游戏）
#   sh tools/rike-session.sh prep     开工前体检（**只读**，不改任何状态）
#   sh tools/rike-session.sh finish   收尾：关游戏 + 验证真关了 + 恢复让路的机器人
. "$(dirname "$0")/../env.sh"
SHD="$DSH_ROOT/tools/shd.sh"
PKG=com.netease.nshm
BOT="$DSH_ROOT/tools/rike-bot.sh"

hr() { printf '─%.0s' $(seq 1 46); echo; }

# 容器(UTC) vs 手机(CST)：锁北京时间，免得日志时间看不懂
export TZ=Asia/Shanghai

prep() {
  echo "── 逆水寒日课 · 开工体检 ──────────────────"
  OK=0; BAD=0

  # ① 虚拟屏守护
  L=$(curl -s --max-time 5 "http://127.0.0.1:$VSD_PORT/list" 2>/dev/null)
  if echo "$L" | grep -q displayId; then
    echo "① 虚拟屏守护 :$VSD_PORT   ✅ $(echo "$L" | sed 's/.*"i":1,"displayId":\([0-9]*\),"width":\([0-9]*\),"height":\([0-9]*\).*/displayId=\1 \2x\3/')"
    OK=$((OK+1))
  else
    echo "① 虚拟屏守护 :$VSD_PORT   ❌ 没在跑 → 先执行: sh $DSH_ROOT/ensure-vsd.sh"
    BAD=$((BAD+1))
  fi

  # ② 命令守护
  H=$(sh "$SHD" --health 2>/dev/null | head -1)
  case "$H" in *ok*) echo "② 命令守护  :$SHD_PORT   ✅ $H"; OK=$((OK+1)) ;;
                 *)  echo "② 命令守护  :$SHD_PORT   ❌ $H → 看 tools/shd.sh / adb 链路"; BAD=$((BAD+1)) ;; esac

  # ③ OCR 守护（不通就顺手拉起 —— 它只会省时间，不会改游戏状态）
  P=$(curl -s --max-time 5 "http://127.0.0.1:${OCR_PORT:-18790}/ping" 2>/dev/null)
  if echo "$P" | grep -q pong; then
    echo "③ OCR 守护  :${OCR_PORT:-18790}   ✅ $P"; OK=$((OK+1))
  else
    echo "③ OCR 守护  :${OCR_PORT:-18790}   ⚠️ 没响应 → 尝试拉起"
    sh "$DSH_ROOT/tools/ocr-daemon.sh" start >/dev/null 2>&1
    sleep 6
    P=$(curl -s --max-time 8 "http://127.0.0.1:${OCR_PORT:-18790}/ping" 2>/dev/null)
    if echo "$P" | grep -q pong; then echo "   ✅ 已拉起: $P"; OK=$((OK+1));
    else echo "   ❌ 拉不起来（首次会跑 tesseract 兜底，但会慢很多）"; BAD=$((BAD+1)); fi
  fi

  # ④ 电池
  B=$(sh "$SHD" "dumpsys battery 2>/dev/null | grep -E 'level|AC powered|USB powered'" 2>/dev/null)
  LVL=$(echo "$B" | sed -n 's/.*level: \([0-9]*\).*/\1/p' | head -1)
  CHG=no
  echo "$B" | grep -qE 'AC powered: true|USB powered: true' && CHG=yes
  if [ -n "$LVL" ]; then
    if [ "$LVL" -lt 20 ]; then echo "④ 电池                    ❌ ${LVL}%（充电: $CHG）—— **先充电**，日课要跑十几分钟，别中途关机"; BAD=$((BAD+1));
    elif [ "$LVL" -lt 45 ]; then echo "④ 电池                    ⚠️ ${LVL}%（充电: $CHG）—— 建议先充到 50% 以上"; OK=$((OK+1));
    else echo "④ 电池                    ✅ ${LVL}%（充电: $CHG）"; OK=$((OK+1)); fi
  else
    echo "④ 电池                    ❓ 读不到（不影响开跑）"
  fi

  # ⑤ CPU 峰值温度（容器里直接读温区）
  T=$(for z in /sys/class/thermal/thermal_zone*; do cat "$z/temp" 2>/dev/null; done | sort -nr | head -1)
  [ -n "$T" ] && echo "⑤ CPU 峰值温度             $(awk "BEGIN{printf \"%.1f\", $T/1000}")°C（>70°C 说明有别的重负载在抢 CPU）"

  # ⑥ 游戏进程（开工前应当**是干净的**；在跑 = 昨天的残留没关掉）
  G=$(sh "$SHD" "pidof $PKG" 2>/dev/null | tr -d '\r' | grep -v EXIT | head -1)
  if [ -z "$G" ]; then echo "⑥ 逆水寒进程              ✅ 未在跑（干净）"; OK=$((OK+1));
  else echo "⑥ 逆水寒进程              ⚠️ 已经在跑（pid=$G）—— 不影响开工，但记得收尾要用 finish"; fi

  # ⑦ 副屏 1 归属
  echo "⑦ 副屏 1 归属              $(sh "$DSH_ROOT/tools/bg.sh" list 2>/dev/null | grep '副屏 1' | sed 's/^ *//')"

  # ⑧ 微信机器人（让路建议）
  if bash "$DSH_ROOT/wechat-bot/app/bot.sh" status 2>/dev/null | grep -q '机器人在跑'; then
    echo "⑧ 微信机器人              ⚠️ 在跑 → 它会让全屏 OCR 慢约 40%、CPU 峰值 +20°C"
    echo "                          建议开跑前: sh tools/rike-bot.sh pause （收尾 finish 会自动 resume）"
  else
    echo "⑧ 微信机器人              ✅ 没在跑（不抢 CPU）"
  fi

  # ⑨ 直播页（用户看副屏画面 + 我的点击；没起就顺手拉起 —— 它只是本地只读转发）
  LP=${RIKE_LIVE_PORT:-18800}
  if curl -s -m 3 "http://127.0.0.1:$LP/health" 2>/dev/null | grep -q ok; then
    echo "⑨ 直播页 :$LP      ✅ 在跑（手机浏览器打开 http://127.0.0.1:$LP）"
  else
    sh "$DSH_ROOT/tools/rike-live.sh" start >/dev/null 2>&1
    if curl -s -m 4 "http://127.0.0.1:$LP/health" 2>/dev/null | grep -q ok; then
      echo "⑨ 直播页 :$LP      ✅ 刚拉起（手机浏览器打开 http://127.0.0.1:$LP）"
    else
      echo "⑨ 直播页 :$LP      ⚠️ 起不来（不影响跑日课，只是你看不到画面）"
    fi
  fi

  hr
  echo "体检结果：通过 $OK 项，需处理 $BAD 项"
  [ "$BAD" -eq 0 ] && echo "→ 可以开工" || echo "→ 先把上面 ❌ 处理掉再开工"
  [ "$BAD" -eq 0 ] && return 0 || return 1   # 退出码：0=可开工 1=有未通过项
}

finish() {
  echo "── 逆水寒日课 · 收尾 ──────────────────────"
  echo "▸ 关闭游戏（am force-stop $PKG）"
  sh "$DSH_ROOT/tools/bg.sh" stop nshm >/dev/null 2>&1
  sleep 4

  # ★ 关键：**验证**真的退出了（这一步就是昨天缺的）
  G=$(sh "$SHD" "pidof $PKG" 2>/dev/null | tr -d '\r' | grep -v EXIT | head -1)
  if [ -n "$G" ]; then
    echo "  ⚠️ 还在跑（pid=$G）→ 再来一次"
    sh "$SHD" "am force-stop $PKG" >/dev/null 2>&1
    sleep 5
    G=$(sh "$SHD" "pidof $PKG" 2>/dev/null | tr -d '\r' | grep -v EXIT | head -1)
  fi
  if [ -n "$G" ]; then
    echo "  ❌ 仍然没关掉（pid=$G）—— **需要人工看一眼**（可能被自启动/闪电启动拉回来了）"
    RC=1
  else
    echo "  ✅ 已确认退出（pidof 为空）"
    RC=0
  fi

  echo "▸ 副屏 1 归属              $(sh "$DSH_ROOT/tools/bg.sh" list 2>/dev/null | grep '副屏 1' | sed 's/^ *//')"

  # 让路恢复（只在本次真的让路过时才动机器人）
  if [ -f /tmp/rike-bot.state ]; then
    echo "▸ 恢复让路的微信机器人"
    sh "$BOT" resume 2>&1 | sed 's/^/  /'
  else
    echo "▸ 微信机器人              本次没让路 → 不动它"
  fi
  hr
  return $RC
}

# ── open：开工准备一条龙（★2026-10-10 加）──────────────────────────────
#   体检 → 让机器人让路 → 起直播页（用户能看见我操作）。**不含进场**（那步单独跑：
#   `sh tools/rike-enter.sh` + `sh tools/rike.sh auto 8 5`），因为进场要占 1~2 分钟且要盯着。
open(){
  echo "════ 开工准备 ════"
  prep || { echo "❌ 体检没过 → 先处理上面带 ❌ 的项（要强行开跑就逐步单独执行）"; return 1; }
  hr
  echo "▸ 把日课副屏锁成横屏（防竖屏模块把副屏转 90° 导致点击错位）"
  sh "$DSH_ROOT/tools/rike-landscape.sh" 2>&1 | sed 's/^/  /'
  hr
  echo "▸ 让微信机器人让路"
  sh "$BOT" pause 2>&1 | sed 's/^/  /'
  echo "▸ 起直播页（给用户看副屏画面 + 我的点击）"
  sh "$DSH_ROOT/tools/rike-live.sh" start 2>&1 | sed 's/^/  /'
  hr
  echo "下一步："
  echo "  sh tools/rike-enter.sh            # 冷启动 → 青崖书（约 1~2 分钟）"
  echo "  sh tools/rike.sh auto 8 5         # 刷到 5 张目标卡"
  echo "  sh tools/rike.sh state            # 报卡片状态，再决定做哪几张"
}

case "$1" in
  prep)   prep ;;
  open)   open ;;
  finish) finish ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
