#!/bin/sh
# wait.sh —— 轮询等待界面上出现关键字（替代固定 sleep 死等）
#   用法: sh wait.sh "<grep -E 正则>" [超时秒=90] [间隔秒=1.5] [裁切WxH+X+Y]
#   退出码 0 = 等到；1 = 超时
#   实测单轮成本 ≈ 抓帧 80ms + 整屏 OCR 600ms（裁切后降到 ~120ms）
. "$(dirname "$0")/../env.sh"
KW=${1:?用法: wait.sh <正则> [超时=90] [间隔=1.5] [裁切]}
TMO=${2:-90}; IV=${3:-1.5}; CROP=${4:-}
SLOT=${RIKE_SLOT:-1}
T=/tmp/wait.jpg; T0=$(date +%s)
while :; do
  # ⚠️ 2026-10-05：先 rm 旧图 + 查抓帧结果，失败就**跳过本轮 OCR**。
  #    旧写法不看抓帧成败，下面 cp "$T" 会把上一轮（甚至几天前）的旧图喂给 OCR ——
  #    每轮对着同一张旧画面判关键字，可能「假命中」提前返回 0，也可能白等到超时。
  rm -f "$T"
  if node "$DSH_ROOT/tools/vscreen" -i "$SLOT" shot "$T" >/dev/null 2>&1 && [ -f "$T" ]; then
    # ★ 2026-10-10：原先是「裁图 + 放大 200% + tesseract」。RapidOCR 实测**放大反而更慢更差**
    #   （卡行 100%→94%、400%→94%，耗时翻倍）→ 直接把 roi 交给常驻服务，少一次 convert 进程。
    if [ -n "$CROP" ]; then
      ROI=$(printf '%s' "$CROP" | sed 's/^\([0-9]*\)x\([0-9]*\)+\([0-9]*\)+\([0-9]*\)$/\3,\4,\1,\2/')
      TXT=$(sh "$DSH_ROOT/tools/rike-ocr.sh" --roi "$ROI" "$T" 2>/dev/null)
    else
      TXT=$(sh "$DSH_ROOT/tools/rike-ocr.sh" "$T" 2>/dev/null)
    fi
    if echo "$TXT" | grep -qE "$KW"; then
      echo "  ✅ 等到「$KW」（用了 $(( $(date +%s) - T0 ))s）"; exit 0
    fi
    # 待机画面（竹林 +「江湖匆匆 稍作停歇…」）会吞掉一切点击 → 拖一下唤醒
    # ★ 2026-10-08 实测：整屏 OCR 读不出「稍作停歇」，只认得出「匆匆」——别改回长词
    if echo "$TXT" | grep -qE '匆匆|稍作停歇'; then
      echo "  ⏸ 检测到待机画面 → 唤醒"
      node "$DSH_ROOT/tools/vscreen" -i "$SLOT" swipe 300 318 1000 318 300 >/dev/null 2>&1
      sleep 3
    fi
  fi
  # 超时检查必须放在 if 之外：抓帧一直失败时也要能超时退出，不能空转
  if [ $(( $(date +%s) - T0 )) -ge "$TMO" ]; then
    echo "  ⚠️ 等「$KW」超时 ${TMO}s"; exit 1
  fi
  sleep "$IV"
done
