#!/bin/sh
# ocr.sh —— 零成本识图（本机 tesseract，不联网不花钱）
#   sh ocr.sh --pipe <屏索引>     ★推荐：抓该屏 JPEG 直管道进 tesseract（不落盘/不重编码）
#   sh ocr.sh -i <屏索引>         落盘后 OCR
#   sh ocr.sh <图片>              识别已有图片
# 关键参数：OMP_THREAD_LIMIT=1（手机上多线程反而慢）、--psm 11（整屏）、-l chi_sim
. "$(dirname "$0")/../env.sh"
OCRLANG="${OCRLANG:-chi_sim}"; OCRPSM="${OCRPSM:-11}"
ocr(){ OMP_THREAD_LIMIT=1 tesseract "$1" stdout -l "$OCRLANG" --psm "$OCRPSM" 2>/dev/null | grep -vE '^[[:space:]]*$'; }
case "$1" in
  --pipe) # ⚠️ 2026-10-05：空屏时守护返回的是 12 字节纯文本 "no-frame-yet"
          #    （Content-Type: text/plain，不是 JPEG）。旧写法把它直接塞进 tesseract，
          #    报错又被 2>/dev/null 吞掉 —— 结果是**退出码 1 且一个字都没有**，
          #    调用方无法区分「屏上没文字」和「根本没有帧」。所以先看类型再决定。
          CT=$(curl -s --max-time 20 -o /tmp/ocr-pipe.bin -w '%{content_type}' "http://127.0.0.1:$VSD_PORT/$2/frame")
          case "$CT" in
            image/*) OMP_THREAD_LIMIT=1 tesseract /tmp/ocr-pipe.bin stdout -l "$OCRLANG" --psm "$OCRPSM" 2>/dev/null \
                       | grep -vE '^[[:space:]]*$' ;;
            *) echo "❌ 副屏 $2 还没有帧（返回 $CT）—— 应用是不是还没上屏？" >&2; exit 1 ;;
          esac ;;
  -i)     IMG="/tmp/ocr-vs$2.jpg"; rm -f "$IMG"
          node "$DSH_ROOT/tools/vscreen" -i "$2" shot "$IMG" >/dev/null 2>&1
          [ -f "$IMG" ] && ocr "$IMG" || { echo "❌ 抓屏失败" >&2; exit 1; } ;;
  *)      [ -n "$1" ] && [ -f "$1" ] && ocr "$1" || { echo "用法: sh ocr.sh --pipe <屏> | -i <屏> | <图片>"; exit 2; } ;;
esac
