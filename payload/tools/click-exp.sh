#!/bin/sh
# click-exp.sh —— 「点击被吞」对照实验：4 种注入方式谁最不容易丢
#
#   sh tools/click-exp.sh [每种次数=10]
#
# 靶子：主世界菜单按钮 (1350,32) —— 它是 toggle（开/关可逆、无副作用）。
# 判据：每次点击**前后各查一次菜单开没开**：
#        状态翻转 = 这次点击生效 ✅   状态没变 = 被吞 ❌
# 四种方式：
#   A `swipe x y x y+6 110`   —— 现在所有脚本在用的基线
#   B `swipe x y x y+6 300`   —— 加长按压（跨更多帧）
#   C `tap x y`               —— 零位移
#   D `motionevent DOWN` → 真实 sleep 250ms → `UP`  —— 让"按下"跨多帧
. "$(dirname "$0")/../env.sh"
SLOT=${RIKE_SLOT:-1}
N=${1:-10}
X=${EXP_X:-1350}; Y=${EXP_Y:-32}
VS="node $DSH_ROOT/tools/vscreen -i $SLOT"
SHD="$DSH_ROOT/tools/shd.sh"
DID=$(curl -s --max-time 5 "http://127.0.0.1:$VSD_PORT/$SLOT/info" | sed -n 's/.*"displayId":\([0-9]*\).*/\1/p')
[ -n "$DID" ] || { echo "❌ 拿不到副屏 displayId"; exit 1; }

# ★ 判据是**实测出来的**（2026-10-10）：裁右侧 tile 区 (920,55) 480x530 再放大 160%，
#   菜单开 → 命中「好感/结义」等，菜单关 → 命中空。
#   ⚠️ 别用全屏 OCR（只命中 1 个词）、更别用"裁菜单区不放大"（命中 0）——
#   当天就是因为判据失效，40 次点击全被误判成"被吞"。
menu_open(){
  $VS shot /tmp/exp.jpg >/dev/null 2>&1
  convert /tmp/exp.jpg -crop 480x530+920+55 +repage -resize 160% /tmp/exp-crop.png 2>/dev/null
  tesseract /tmp/exp-crop.png stdout -l chi_sim --psm 11 2>/dev/null \
    | grep -qE '外观|星梦乐园|休闲竞技|小暖厨房|珍玩收藏|约战|结义|情缘|兑换|成就|身份|好感|副本|帮会|师徒|竞技|任务'
}
click_A(){ $VS swipe $X $Y $X $((Y+6)) 110 >/dev/null 2>&1; }
click_B(){ $VS swipe $X $Y $X $((Y+6)) 300 >/dev/null 2>&1; }
click_C(){ $VS tap $X $Y >/dev/null 2>&1; }
click_D(){ sh "$SHD" "input -d $DID motionevent DOWN $X $Y" >/dev/null 2>&1
           sleep 0.25
           sh "$SHD" "input -d $DID motionevent UP $X $Y" >/dev/null 2>&1; }
# ★ 2026-10-10 加：E = **先等画面静止再点**（这次要验的假设）。
#   吞点击的主因是"游戏在加载/准备界面时抑制输入"，那就别在加载的那几帧点。
click_E(){ node "$DSH_ROOT/tools/rike-probe.cjs" settle --still 2 --gap 80 --timeout 1.2 >/dev/null 2>&1
           $VS swipe $X $Y $X $((Y+6)) 110 >/dev/null 2>&1; }

run(){
  tag=$1; fn=$2; ok=0; bad=0; i=0
  echo "── $tag ──"
  while [ $i -lt "$N" ]; do
    i=$((i+1))
    if menu_open; then b=open; else b=closed; fi
    $fn; sleep 1.0
    if menu_open; then a=open; else a=closed; fi
    if [ "$b" != "$a" ]; then ok=$((ok+1)); printf '   #%-2d %s → %s  ✅\n' "$i" "$b" "$a"
    else bad=$((bad+1)); printf '   #%-2d %s → %s  ❌ 被吞\n' "$i" "$b" "$a"; fi
  done
  echo "   >>> $tag：成功 $ok / 被吞 $bad"
  echo "$tag	$ok	$bad" >> /tmp/click-exp.txt
}

: > /tmp/click-exp.txt
echo "靶子 (${X},${Y}) · displayId=$DID · 每种 ${N} 次"
echo "▸ 先关掉可能挡路的「新活动速递」(1143,100)"
$VS swipe 1143 100 1143 106 120 >/dev/null 2>&1; sleep 1
if menu_open; then $VS swipe $X $Y $X $((Y+6)) 200 >/dev/null 2>&1; sleep 1.2; fi
if [ "${EXP_MODE:-all}" = "still" ]; then
  # 假设验证：直接点 vs 等静止再点（A 是脚本一直在用的基线）
  run "A-直接点"    click_A
  run "E-等静止+点" click_E
else
  run "A-swipe110" click_A
  run "B-swipe300" click_B
  run "C-tap"      click_C
  run "D-motionevent" click_D
fi
echo; echo "==== 汇总（方式 成功 被吞）===="; cat /tmp/click-exp.txt
