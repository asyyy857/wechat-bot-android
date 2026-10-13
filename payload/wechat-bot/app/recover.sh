#!/bin/bash
# recover.sh —— 虚拟屏 / 抓帧出问题时的标准恢复流程（2026-10-03 固化）
#
#   bash recover.sh            一键恢复：守护 → 微信上屏 → 桥接 → 机器人
#   bash recover.sh --no-bot   只恢复到桥接，不启动机器人
#
# 什么时候用：
#   · 桥接报「vscreen 抓帧不可用（守护挂了？）→ 本进程之后只用 screencap」
#     —— 注意 screencap 对受信任虚拟屏是**无效的**（会报 Display Id xx is not valid），
#     所以桥接一旦这么标记就等于瞎了，必须重启桥接才能清掉这个标记；
#   · /snapshot 一直失败 / 「截图为空」；
#   · 守护端口 18250 连得上但不回数据（Recv-Q 堆积）、或干脆没进程。
#
# 为什么必须按这个顺序：
#   守护每次重启都会**重建虚拟屏**，屏号就换了（实测 15→19→21→23→25），
#   微信会被赶回物理屏，而桥接进程里还缓存着旧屏号和一个 _vscreen_dead 标记 ——
#   所以「重启守护 → 重新把微信放上新屏 → 重启桥接」三步缺一不可。
export TZ=Asia/Shanghai   # 容器是 UTC，比手机（北京）慢 8 小时 —— 日志时间戳/人设时段全靠它
set -u
# 参数：--check 只体检 / --no-bot 不动机器人 / --no-takeover 不碰用户手上的微信（自动看护用）
NO_TAKEOVER=0
for _a in "$@"; do [ "$_a" = "--no-takeover" ] && NO_TAKEOVER=1; done
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
LOG="$WXBOT_LOGDIR/console.log"
S=$(cat /sdcard/dsh/.adb-serial 2>/dev/null)
[ -n "$S" ] || { echo "读不到 adb 串号（/sdcard/dsh/.adb-serial）—— 先跑 sh /sdcard/dsh/connect-adb.sh"; exit 1; }

if [ "${1:-}" = "--check" ]; then
  echo "=== 体检（只看不动）==="
  adb -s "$S" shell "ps -A -o PID,ARGS | grep -E '[V]sDaemon|[S]hellDaemon'" 2>/dev/null | sed 's/^/  进程  /'
  echo -n "  守护 :18250/list  -> "; curl -s -m 6 http://127.0.0.1:18250/list 2>/dev/null || echo "不通！"; echo
  echo -n "  桥接 :8765/health -> "; curl -s -m 6 http://127.0.0.1:8765/health 2>/dev/null || echo "不通！"; echo
  echo -n "  机器人 PID        -> "; ps -eo pid,args | awk '/python3 -u main_android\.py/ && !/awk/ {printf "%s ", $1}'; echo
  echo -n "  物理屏前台        -> "
  adb -s "$S" shell "dumpsys activity activities | grep -m1 'Display #0'" 2>/dev/null | head -1
  echo "（守护 /list 里 frames 在涨 = 抓帧活着；桥接 health 里 displayId 要和 slot0 对得上）"
  exit 0
fi

source "$DIR/wxlock.sh" "recover.sh $*" || exit 1

echo "① 起虚拟屏守护"
sh /sdcard/dsh/vscreen-daemon/start.sh 2>&1 | tail -4

echo "② 起命令守护"
sh /sdcard/dsh/shell-daemon/start.sh 2>&1 | tail -3

echo "③ 等守护就绪、取 slot0 的屏号"
DID=""
for i in $(seq 1 30); do
  DID=$(curl -s -m 4 http://127.0.0.1:18250/list 2>/dev/null | python3 -c "
import sys, json
try:
    print(next(s['displayId'] for s in json.load(sys.stdin) if s.get('i') == 0))
except Exception:
    print('')
" 2>/dev/null)
  [ -n "$DID" ] && { echo "   屏号 = $DID（第 $i 次探测）"; break; }
  sleep 2
done
if [ -z "$DID" ]; then
  echo "   18250 一直被拒/不通 —— 先看上面 start.sh 的输出，"
  echo "   再确认端口是不是又被劫持了（换 1825x 试，见交接说明坑 11）"
  exit 1
fi

echo "④ 把微信放到虚拟屏 $DID（这一步会把微信从用户手里拿走）"
if [ "${NO_TAKEOVER:-0}" = "1" ]; then
  echo "   （--no-takeover：跳过 —— 自动看护不许动用户手上的微信）"
else
  # ★ 2026-10-10：优先用 move-stack —— 只搬 task，不重新 start LauncherUI。
  #   原来那句 `am start -n LauncherUI` 会把栈顶聊天页清掉 / 触发 onNewIntent，
  #   用户看到的就是"回到会话列表"。task 不在（微信进程被杀）时才冷启动。
  WX_TID=$(adb -s "$S" shell "cmd activity stack list" 2>/dev/null \
           | grep -oE 'taskId=[0-9]+: com\.tencent\.mm/' | head -1 | grep -oE '[0-9]+')
  if [ -n "$WX_TID" ]; then
    echo "   move-stack $WX_TID -> display $DID（保留聊天页）"
    adb -s "$S" shell "cmd activity display move-stack $WX_TID $DID" >/dev/null 2>&1
  else
    echo "   （微信 task 不在，冷启动拉起）"
    adb -s "$S" shell "am start --display $DID -n com.tencent.mm/.ui.LauncherUI" >/dev/null 2>&1
  fi
  sleep 8
  adb -s "$S" shell "dumpsys activity activities | grep -A2 'Display #$DID'" 2>/dev/null | head -3
fi

echo "⑤ 重启桥接（清掉缓存的旧屏号 + _vscreen_dead 标记）"
bash "$DIR/wxbridge_ctl.sh" start
for i in $(seq 1 30); do
  H=$(curl -s -m 4 http://127.0.0.1:8765/health 2>/dev/null)
  if echo "$H" | grep -q '"ok": *true'; then echo "   桥接就绪：$H"; break; fi
  sleep 2
done

if [ "${1:-}" = "--no-bot" ]; then
  echo "（--no-bot：不动机器人）完成。"
  exit 0
fi

# ⑤.5 先把会话切进目标群，让界面先稳定下来。
# ⚠️ 2026-10-03 实测：守护刚重建 + 微信刚冷启动那会儿，切群要 20~40 秒；
#    这活儿交给机器人自己干，它的 /open 超时只有 30 秒 —— 于是
#    「切群成功」之后紧接着机器人自己再切一次就超时退出（启动即失败）。
#    在这里先替它切好，机器人启动时走快速路径（约 1.4 秒）就稳了。
GROUP=$(sed -n 's/^[[:space:]]*group:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' "$DIR/config.yaml" | head -1)
if [ -n "$GROUP" ]; then
  echo "⑤.5 预热：先把会话切到「$GROUP」"
  for _t in 1 2 3; do
    OPEN=$(curl -s -m 120 -X POST http://127.0.0.1:8765/open \
             -H 'Content-Type: application/json' \
             --data-binary "$(python3 -c 'import json,sys;print(json.dumps({"chat":sys.argv[1]}))' "$GROUP")" 2>/dev/null)
    if echo "$OPEN" | grep -q '"ok": *true'; then
      echo "   第 $_t 次切群成功"
      break
    fi
    echo "   第 $_t 次切群没成（$OPEN），2 秒后再试"
    sleep 2
  done
  curl -s -m 30 http://127.0.0.1:8765/title 2>/dev/null | sed 's/^/   当前标题：/'
  echo
else
  echo "⑤.5 读不到 config.yaml 里的 target.group，跳过预热"
fi

echo "⑥ 重启机器人（先停干净，别留两个实例互相抢）"
[ -f /tmp/wxbot.pid ] && kill "$(cat /tmp/wxbot.pid)" 2>/dev/null
ps -eo pid,args | awk '/python3 -u main_android\.py/ && !/awk/ {system("kill " $1)}'
sleep 1
cd "$DIR" || exit 1
echo "===== $(date '+%F %T') bot start (recover.sh) =====" >> "$LOG"
nohup python3 -u main_android.py >> "$LOG" 2>&1 9>&- &
echo $! > /tmp/wxbot.pid
sleep 40
grep -v onnxruntime "$LOG" | head -5
ps -eo pid,args | awk '/python3 -u main_android\.py/ && !/awk/ {print "   机器人 PID", $1}'
echo "完成。急停：在 $DIR 下建 STOP.txt，或 bash $DIR/bot.sh stop"
