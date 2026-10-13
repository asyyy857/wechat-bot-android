#!/bin/sh
# rike-ocr.sh —— 日课 OCR 统一入口（RapidOCR 常驻服务优先，tesseract 兜底）
#
#   为什么要换：同一批历史帧实测 —— 卡片行 5 个卡名
#     tesseract 26/35 = 74%（最右那张卡几乎必读成乱码）
#     RapidOCR  35/35 = **100%**（人工裁图逐帧核对过）
#   耗时：卡行 0.76s → 1.05s（多 0.3s，但省掉 convert 起进程，端到端基本持平）；
#         整屏 1.5s → 3.0s（**整屏 OCR 要尽量少做**，优先用 --roi 缩小范围 + 帧缓存）。
#
#   用法:
#     sh tools/rike-ocr.sh <图>                     → 全文（每行一条）
#     sh tools/rike-ocr.sh --tsv <图> [x y w h]     → 带坐标 TSV：文字\tx1\ty1\tx2\ty2\tscore
#     sh tools/rike-ocr.sh --json <图>              → 同上，JSON
#     sh tools/rike-ocr.sh --roi x,y,w,h <图>       → 只识别该矩形（原图坐标）
#     sh tools/rike-ocr.sh --roi x,y,w,h --scale 2 <图>   → 识别前放大（小字偶尔有用）
#     sh tools/rike-ocr.sh --fix <图>               → 再过一遍字形纠错（把形近乱码吸回游戏文案词表）
#        见 rike-ocr-fix.py：实测「已领」会被读成 巴领/己领/已邻，「点击右侧图标定位」→ 志击丰侧图标定位
#   退出码: 0 成功（含"图里没字"）；2 参数/文件问题；3 服务与兜底都失败
F=""; FMT=text; ROI=""; SCALE=1; FIX=${RIKE_OCR_FIX:-0}   # 环境变量或 --fix 打开字形纠错
while [ $# -gt 0 ]; do
  case "$1" in
    --tsv)   FMT=tsv; shift ;;
    --json)  FMT=json; shift ;;
    --text)  FMT=text; shift ;;
    --roi)   ROI="$2"; shift 2 ;;
    --scale) SCALE="$2"; shift 2 ;;
    --fix)   FIX=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) F="$1"; shift ;;
  esac
done
[ -n "$F" ] || { echo "用法: sh tools/rike-ocr.sh [--tsv|--json] [--roi x,y,w,h] [--scale N] <图>" >&2; exit 2; }
[ -f "$F" ] || { echo "❌ 文件不存在: $F" >&2; exit 2; }
PORT=${OCR_PORT:-18790}
D=$(cd "$(dirname "$0")" && pwd)

OUT=/tmp/rike-ocr.out
req(){   # -G + --data-urlencode：路径里有中文/空格也不会崩；用 HTTP 码区分「服务故障」与「图里没字」
  curl -s --max-time 90 -G "http://127.0.0.1:$PORT/ocr" \
    --data-urlencode "f=$F" --data-urlencode "fmt=$FMT" \
    --data-urlencode "roi=$ROI" --data-urlencode "scale=$SCALE" \
    -o "$OUT" -w '%{http_code}' 2>/dev/null
}
CODE=$(req); R=$(cat "$OUT" 2>/dev/null)
if [ "$CODE" != "200" ]; then
  # 服务不在 → 拉起再试一次（首次约 7s；ensure 静默）
  sh "$D/ocr-daemon.sh" ensure >/dev/null 2>&1
  CODE=$(req); R=$(cat "$OUT" 2>/dev/null)
fi
if [ "$CODE" != "200" ]; then
  # ── 兜底：tesseract（识别率差，但保证有字出来）──
  FB="$F"
  if [ -n "$ROI" ]; then
    FB=/tmp/rike-ocr-fb.png; rm -f "$FB"
    X=$(printf '%s' "$ROI" | cut -d, -f1); Y=$(printf '%s' "$ROI" | cut -d, -f2)
    W=$(printf '%s' "$ROI" | cut -d, -f3); H=$(printf '%s' "$ROI" | cut -d, -f4)
    if [ "$SCALE" = "1" ]; then
      convert "$F" -crop "${W}x${H}+${X}+${Y}" +repage "$FB" 2>/dev/null || FB="$F"
    else
      PCT=$(awk "BEGIN{printf \"%d%%\", $SCALE*100}")
      convert "$F" -crop "${W}x${H}+${X}+${Y}" +repage -resize "$PCT" "$FB" 2>/dev/null || FB="$F"
    fi
  fi
  T=$(tesseract "$FB" stdout -l chi_sim --psm 11 2>/dev/null) || { echo "❌ OCR 失败（服务与 tesseract 都没出结果）" >&2; exit 3; }
  if [ "$FIX" = "1" ]; then printf '%s\n' "$T" | python3 "$D/rike-ocr-fix.py" --tsv; else printf '%s\n' "$T"; fi
  exit 0
fi
if [ "$FIX" = "1" ]; then
  printf '%s\n' "$R" | python3 "$D/rike-ocr-fix.py" --tsv
else
  printf '%s\n' "$R"
fi
