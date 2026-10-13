#!/bin/bash
# stop.sh —— 一键停干净（2026-10-03）
#
#   bash stop.sh                停机器人 + 停桥接 + 把微信还给用户（保留虚拟屏守护）
#   bash stop.sh --soft         只建 STOP.txt：机器人下一轮起闭嘴，所有进程都留着
#   bash stop.sh --keep-wechat  停机器人 + 桥接，但微信留在虚拟屏（用户在手机上看不到微信）
#   bash stop.sh --all          连两个守护一起停（⚠️ 会连带杀掉虚拟屏上的微信）
#
# 它会顺手停掉「电量看护」（battery_guard.sh）—— 那个只在机器人跑着时才有意义。
#
# 为什么默认要把微信搬回物理屏：机器人是跑在虚拟屏上的，停掉之后如果不搬回来，
# 用户在手机上会"找不到微信"（不是崩溃，是它还留在虚拟屏上）。
export TZ=Asia/Shanghai   # 容器是 UTC，比手机（北京）慢 8 小时 —— 日志时间戳/人设时段全靠它
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
# ★ 2026-10-10 评审修复（§7.2）：进程判据统一到 botpids.sh。
#   原来这里用 `ps|awk '/python3 -u main_android\.py/'` —— 它要求命令行里有
#   字面 `-u`，看不到"不带 -u 起的机器人"（而 bot.sh 那边用任意子串，两边不等价），
#   于是"停了但没停干净 / 没在跑却说在跑"都可能发生。
source "$DIR/botpids.sh"
S=$(cat /sdcard/dsh/.adb-serial 2>/dev/null)
MODE="${1:-}"

if [ "$MODE" = "--soft" ]; then
  touch "${WXBOT_DATADIR:-$DIR}/STOP.txt"
  echo "已建 STOP.txt —— 机器人下一轮开始不再发任何消息（进程都还活着）。"
  echo "想恢复：rm ${WXBOT_DATADIR:-$DIR}/STOP.txt"
  exit 0
fi

echo "① 停机器人"
_note_stop "stop.sh" "stop.sh（${MODE:-默认}）"
if [ -f /tmp/wxbot.pid ] && kill "$(cat /tmp/wxbot.pid)" 2>/dev/null; then
  echo "   按 pid 文件停掉了 $(cat /tmp/wxbot.pid)"
fi
# 兜底：命令行里含 main_android.py 的进程（awk 自己那条要排除，见交接说明坑 9）
for _p in $(_bot_pids); do echo "   兜底停掉: $_p"; kill "$_p" 2>/dev/null; done
# ★ 2026-10-08 审查修复（冗余）：这里原来最多空等 25 秒，等机器人发出
#   「喵走了喵会想念你们的喵」那句告别播报 —— 而**播报功能 2026-10-06 就按用户
#   要求删掉了**（config 的 announce 段 + bot.py 的 _announce() 一起删）。
#   也就是说这 25 秒从 10-06 起纯属白等：机器人收到 SIGTERM 就立刻退出，
#   循环每轮都要靠 sleep 1 把 25 秒耗完，用户感觉"停止很慢"。
#   现在只留 3 秒礼貌期（给信号处理留点时间收尾），然后照旧往下走。
for _ in $(seq 1 3); do
  _bot_running || break
  sleep 1
done
LEFT=$(_bot_pids | tr '\n' ' ')
[ -z "$LEFT" ] && echo "   ✅ 机器人已停" || echo "   ⚠️ 还剩进程：$LEFT"

echo "①.5 停电量看护（它是机器人的随从，机器人停了它也没必要盯了）"
bash "$DIR/battery_guard.sh" --stop 2>/dev/null | sed 's/^/   /'
# ★ 2026-10-10 评审修复：这句原来**无条件**删掉电量看护刚写的停机痕 ——
#   battery_guard.sh:341 先写 MARK、紧接着调本脚本，于是那条痕活不过一秒，
#   `bot.sh status` 与面板的红字（botctl.py:841）永远看不到"为什么停机"。
#   电量看护触发时传 --from-battery，这种情况保留（它下一轮自己会清）。
if [ "$MODE" != "--from-battery" ]; then
  rm -f /tmp/wxbot-battery-stopped
fi

echo "② 停桥接"
bash "$DIR/wxbridge_ctl.sh" stop
sleep 1
if curl -s -m 4 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  echo "   ⚠️ 桥接还在响应"
else
  echo "   ✅ 桥接已停"
fi
# 急停文件顺手清掉，否则下次启动会被它立刻掐住
if [ -f "${WXBOT_DATADIR:-$DIR}/STOP.txt" ]; then rm -f "${WXBOT_DATADIR:-$DIR}/STOP.txt"; echo "   （顺手删掉了遗留的 STOP.txt）"; fi

if [ "$MODE" = "--all" ]; then
  echo "③ 停两个守护（会连带杀掉虚拟屏上的微信）"
  sh /sdcard/dsh/vscreen-daemon/stop.sh 2>&1 | tail -2
  OLD=$(adb -s "$S" shell "ps -A -o PID,ARGS | grep [S]hellDaemon | awk '{print \$1}'" 2>/dev/null | tr -d '\r')
  for p in $OLD; do adb -s "$S" shell "kill -9 $p" >/dev/null 2>&1; done
  echo "   ✅ 守护已停"
fi

if [ "$MODE" != "--keep-wechat" ]; then
  echo "④ 把微信搬回物理屏（并清掉虚拟屏留下的脏配置）"
  # 只发 am start --windowingMode 1 是不够的：任务粘着的 density=280 override
  # 不会被重算，微信会按 280dpi 渲染 → 字变大。交给 rescue-wechat.sh 校验+重建。
  bash "$DIR/rescue-wechat.sh" | sed 's/^/   /'
fi

echo "完成。要重新跑起来：bash $DIR/recover.sh"
