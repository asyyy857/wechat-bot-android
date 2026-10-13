#!/bin/sh
# rike-mark.sh —— 抓副屏帧、把"我点了哪儿"画上去（★2026-10-10 用户提出）
#
#   sh tools/rike-mark.sh <输出文件> [--src 已有帧] x1 y1 标签1 [x2 y2 标签2 ...]
#
# 为什么要它：用户看不见虚拟屏（副屏只在后台渲染）—— 他只能看物理屏和我的文字。
#   把「帧 + 点击位置 + 序号」一起给他，他才能判断我点得对不对、给出有效建议，
#   而不是我自己在看不见的屏幕上瞎摸索。
#
# 例：
#   sh tools/rike-mark.sh /sdcard/dsh/debug/step1.jpg 1348 160 "3)日课" 1143 100 "2)关弹窗"
#
# 实现备注（两个踩过的坑）：
#   ① 本机 ImageMagick 的 security policy 禁了 `-draw @文件` → 不能把绘制脚本写进文件；
#   ② 本脚本用 `sh` 跑（手册惯例），**dash 不支持数组** → 用"一个标记叠一次 convert"最稳。
. "$(dirname "$0")/../env.sh"
SLOT=${RIKE_SLOT:-1}
# 字体可覆盖：RIKE_FONT=/sdcard/dsh/fonts/MaShanZheng-Regular.ttf 就用书法字标注
FONT="${RIKE_FONT:-$DSH_ROOT/fonts/NotoSansSC-Regular.otf}"
OUT="$1"; shift
SRC=""
if [ "$1" = "--src" ]; then SRC="$2"; shift 2; fi
[ -n "$OUT" ] || { echo "用法: sh tools/rike-mark.sh <输出文件> [--src 帧] x y 标签 ..."; exit 2; }
if [ -z "$SRC" ]; then
  SRC=/tmp/_mark_src.jpg
  node "$DSH_ROOT/tools/vscreen" -i "$SLOT" shot "$SRC" >/dev/null 2>&1 || { echo "❌ 抓帧失败"; exit 1; }
fi
[ -f "$SRC" ] || { echo "❌ 找不到帧 $SRC"; exit 1; }

TMP="$SRC"; IDX=0
while [ $# -ge 3 ]; do
  x=$1; y=$2; lbl=$3; shift 3
  IDX=$((IDX+1))
  NEXT=/tmp/_mark_step.jpg
  convert "$TMP" \
    -stroke '#ff2d2d' -strokewidth 4 -fill none -draw "circle $x,$y $x,$((y+30))" \
    -stroke '#ff2d2d' -strokewidth 2 -draw "line $((x-45)),$y $((x+45)),$y" \
    -draw "line $x,$((y-45)) $x,$((y+45))" \
    -stroke none -fill '#ff2d2d' -font "$FONT" -pointsize 34 \
    -draw "text $((x+38)),$((y+14)) '$lbl'" \
    -quality 90 "$NEXT" 2>/tmp/_mark_err.txt
  [ $? -eq 0 ] || { echo "❌ convert 失败（第 $IDX 个标记）："; head -2 /tmp/_mark_err.txt; exit 1; }
  TMP="$NEXT"
done
[ "$IDX" -gt 0 ] || { echo "⚠️ 没给标记，直接拷贝原帧"; cp "$SRC" "$OUT"; exit 0; }
cp "$TMP" "$OUT"
echo "✅ 已存 $OUT（标记 $IDX 个点）"
