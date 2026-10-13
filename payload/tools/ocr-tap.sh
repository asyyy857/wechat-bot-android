#!/bin/sh
# ocr-tap.sh —— 通用「找文字 → 点它」：OCR 拿到包围盒，取中心，再点
#   用法: sh ocr-tap.sh "<文字>" [屏槽=1] [--dry]
#   特点: 抗坐标漂移。
#   ★ 2026-10-10 换引擎：tesseract tsv → RapidOCR 带坐标 tsv（tools/rike-ocr.sh）
#     ① RapidOCR 把**整行合成一个框**，不用再像 tesseract 那样按行号拼字；
#     ② 认得出「点击领取 / 确认 / 切换账号」这类词（tesseract 对美术字/半透明浮层全瞎）；
#     ③ 坐标直接是原图坐标，不用再 /2（旧版是放大 200% 后的坐标）。
#   依赖: RapidOCR 常驻服务（tools/ocr-daemon.sh）+ convert（仅兜底）+ vscreen
. "$(dirname "$0")/../env.sh"
TXT="$1"; SLOT="${2:-1}"; DRY="$3"
[ -n "$TXT" ] || { echo "用法: sh ocr-tap.sh \"<文字>\" [屏槽] [--dry]"; exit 2; }
IMG=/tmp/ocrtap.png; rm -f "$IMG"
node "$DSH_ROOT/tools/vscreen" -i "$SLOT" shot "$IMG" >/dev/null 2>&1 || { echo "❌ 抓帧失败"; exit 1; }

HIT=$(sh "$DSH_ROOT/tools/rike-ocr.sh" --tsv "$IMG" 2>/dev/null | awk -F'\t' -v want="$TXT" '
  { t=$1
    if (t==want)              { printf "%d %d %s\n", int(($2+$4)/2), int(($3+$5)/2), t; found=1; exit }
    if (index(t,want)>0)      { if (length(t)>bl) { bl=length(t); bx=int(($2+$4)/2); by=int(($3+$5)/2); bt=t; b1=1 } }
    else if (index(want,t)>0) { if (length(t)>bl2) { bl2=length(t); cx=int(($2+$4)/2); cy=int(($3+$5)/2); ct=t; b2=1 } }
  }
  END { if (found) exit
        if (b1) { printf "%d %d %s\n", bx, by, bt; exit }
        if (b2) { printf "%d %d %s\n", cx, cy, ct } }')
[ -n "$HIT" ] || { echo "  ❌ 没找到「$TXT」"; exit 1; }
X=$(echo "$HIT" | awk '{print $1}')
Y=$(echo "$HIT" | awk '{print $2}')
T=$(echo "$HIT" | cut -d' ' -f3-)
echo "  🎯 「$T」 → 点 ($X,$Y)"
[ "$DRY" = "--dry" ] && { echo "  (--dry 未点击)"; exit 0; }
node "$DSH_ROOT/tools/vscreen" -i "$SLOT" swipe "$X" "$Y" "$X" "$Y" 120 2>&1 | head -1
