#!/bin/bash
# rescue-wechat.sh —— 把微信搬回物理屏，并**清掉虚拟屏留下的脏配置**
#
# 背景（2026-10-03 用户报障「其他应用字变大了」，实测只有微信中招）：
#   微信被 `am start --display <虚拟屏> --windowingMode 5` 拉到虚拟屏时，任务会被
#   粘上那块屏的配置：density=280（物理屏是 560）、bounds=636x1400。
#   把任务挪回物理屏时如果只发 `am start --windowingMode 1`（不带目标屏），
#   **配置不会被重算** —— 微信在手机上仍按 280dpi 渲染，表现就是
#   「字变大 / 界面怪 / 只占左上角一块」。
#
#   本脚本：搬回 → 校验物理屏上微信任务的 density → 不对就用 `--display 0` 重算，
#   必要时 force-stop 重建兜底。force-stop 只杀进程，聊天记录不丢。
#
# 用法：
#   bash rescue-wechat.sh          搬回并校验（微信在虚拟屏时也会拉回）
#   bash rescue-wechat.sh --check  只体检，不动手
#      退出码 0 = 微信在物理屏且密度正确
#             1 = 需要处理（在虚拟屏，或密度被粘脏）
#             2 = 环境问题（adb / 串号）
export TZ=Asia/Shanghai   # 容器是 UTC，比手机（北京）慢 8 小时 —— 日志时间戳/人设时段全靠它
set -u
S=$(cat /sdcard/dsh/.adb-serial 2>/dev/null || true)
WX=com.tencent.mm
LAUNCHER=com.tencent.mm/.ui.LauncherUI
MODE="${1:-}"
if [ "$MODE" != "--check" ]; then
  source "$DIR/wxlock.sh" "rescue-wechat.sh $*" || exit 1
fi

if [ -z "$S" ]; then echo "❌ 读不到 adb 串号"; exit 2; fi
sh_() { adb -s "$S" shell "$@" 2>/dev/null; }

phys_dpi() {
  sh_ "wm density" | tr -d '\r' | sed -n 's/^Physical density: *\([0-9]*\).*/\1/p' | head -1
}

# 物理屏（Display #0 段）上微信任务当前用的 density；微信不在物理屏则输出空。
# ⚠️ dumpsys 里 `Display #0` 是**行首无缩进**的（踩过：写成两个空格会导致正则
#    匹配失败退化成全文搜索，把虚拟屏上微信的 280dpi 误读成物理屏的，进而误抢）。
wx_dpi_on_physical() {
  sh_ "dumpsys activity activities" > /tmp/.rescue-wx.txt
  python3 - <<'PY' 2>/dev/null
import re
try:
    t = open('/tmp/.rescue-wx.txt', encoding='utf-8', errors='replace').read()
except OSError:
    raise SystemExit
m = re.search(r'\nDisplay #0\b(.*?)(?=\nDisplay #\d|\Z)', t, re.S)
seg = m.group(1) if m else ''
for b in re.split(r'\* Task\{', seg)[1:]:
    if not re.search(r'A=\d+:com\.tencent\.mm\b', b):
        continue
    d = re.search(r'CurrentConfiguration=\{[^}]*?(\d+)dpi', b)
    if d:
        print(d.group(1))
        break
PY
}

P=$(phys_dpi)
if [ -z "$P" ]; then echo "❌ 读不到物理屏密度（adb 掉线？）"; exit 2; fi

D=$(wx_dpi_on_physical)

if [ -z "$D" ]; then
  # 物理屏上没有微信任务 = 微信在虚拟屏（机器人正拿着）
  if [ "$MODE" = "--check" ]; then
    echo "ℹ️ 微信不在物理屏（在虚拟屏上，机器人正在用）—— 没有脏配置；要拿回：bash rescue-wechat.sh"
    exit 1
  fi
  echo "微信不在物理屏（在虚拟屏上）→ 搬回物理屏"
elif [ "$D" = "$P" ]; then
  echo "✅ 物理屏密度 ${P}dpi，微信任务密度 ${D}dpi —— 干净，无需处理"
  exit 0
else
  if [ "$MODE" = "--check" ]; then
    echo "⚠️ 物理屏密度 ${P}dpi，但微信任务密度 ${D}dpi —— 沾了虚拟屏配置（脏）"
    exit 1
  fi
  echo "物理屏密度 ${P}dpi，微信任务密度 ${D}dpi —— 脏，开始搬回"
fi

# ① 温和方式：显式指定目标屏 display 0，配置才会按物理屏重算
sh_ "am start --display 0 --windowingMode 1 -n $LAUNCHER" >/dev/null 2>&1
sleep 2
D=$(wx_dpi_on_physical)
if [ "$D" = "$P" ]; then
  echo "✅ 搬回成功，微信密度 ${D}dpi，全屏恢复正常"
  exit 0
fi

# ② 兜底：重建任务（只有这条路能清掉极端情况粘住的 override）
echo "  ↳ 温和搬回没清掉（仍 ${D:-读不到}dpi）→ 重建微信任务"
sh_ "am force-stop $WX" >/dev/null 2>&1
sleep 1
sh_ "am start -W --windowingMode 1 -n $LAUNCHER" >/dev/null 2>&1
sleep 4
D=$(wx_dpi_on_physical)
if [ "$D" = "$P" ]; then
  echo "✅ 已重建，微信密度 ${D}dpi，恢复正常"
  exit 0
fi
echo "❌ 仍是 ${D:-读不到}dpi —— 请在手机上把微信从后台划掉，再重新打开"
exit 1
