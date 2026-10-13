#!/bin/sh
# rike-selftest.sh —— 离线回归自检：拿历史帧验识别逻辑，**改完脚本先跑这个**
#
#   为什么需要：rike-cards.cjs 的判据改过三次（lcs→编辑距离、高亮像素→金色像素、
#   加单卡兜底），每次都是"改了才发现别的样本被带坏"。有这套 fixture 就能一键回归。
#
#   用法: sh rike-selftest.sh
#   fixture 来源：2026-10-08 两个角色的真实帧，状态都用**裁图人工确认过**
FIX="$(cd "$(dirname "$0")" && pwd)/rike-fixtures"
SCAN="$(cd "$(dirname "$0")" && pwd)/rike-cards.cjs"
PASS=0; FAIL=0
ck(){ if [ "$2" = "$3" ]; then printf '  ✅ %-46s %s\n' "$1" "$3"; PASS=$((PASS+1));
      else printf '  ❌ %-46s\n       期望 %s\n       实际 %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi; }

echo "── 1. 卡片状态识别（金色的待做/领取/已领）──"
for f in r1:待做,待做,待做,待做,待做 \
         cb6:待做,领取,领取,领取,领取 \
         y6:待做,领取,领取,领取,领取 \
         p9:待做,领取,领取,待做,领取 \
         z7:待做,领取,待做,待做,待做 \
         j8:领取,领取,领取,领取,领取 \
         c1:已领,已领,已领,已领,已领 ; do
  name=${f%%:*}; want=${f#*:}
  got=$(node "$SCAN" "$FIX/$name.jpg" 2>/dev/null | awk -F'\t' '{printf "%s,",$6}' | sed 's/,$//')
  ck "$name.jpg" "$want" "$got"
done

echo "── 2. 卡名识别（卡5 这种最右卡，老版几乎必错）──"
ck "r1 卡名"  "剧组,庄园,好感度,游历,朋友圈" "$(node "$SCAN" "$FIX/r1.jpg" 2>/dev/null | awk -F'\t' '{printf "%s,",$2}' | sed 's/,$//')"
ck "j8 卡名"  "剧组,庄园,好感度,游历,朋友圈" "$(node "$SCAN" "$FIX/j8.jpg" 2>/dev/null | awk -F'\t' '{printf "%s,",$2}' | sed 's/,$//')"

echo "── 3. 界面健康检查（认不出卡名时要报错退出，不能当成"全非目标"）──"
node "$SCAN" "$FIX/v7.jpg" >/dev/null 2>&1; rc=$?
ck "待机帧 → 非零退出码" "3" "$rc"

echo "── 4. 待机画面检测（★2026-10-10 起 RapidOCR 能读出整句「江湖匆匆稍作停歇」）──"
if sh "$(dirname "$0")/rike-ocr.sh" "$FIX/v7.jpg" 2>/dev/null | grep -q '匆匆'; then r=命中; else r=未命中; fi
ck "待机帧 OCR 含「匆匆」" "命中" "$r"
if sh "$(dirname "$0")/rike-ocr.sh" "$FIX/v7.jpg" 2>/dev/null | grep -q '稍作停歇'; then r=命中; else r=未命中; fi
ck "待机帧 OCR 能读出整句「稍作停歇」（RapidOCR 新能力；tesseract 当年读不出）" "命中" "$r"

echo "── 5. 金色文字定位（受控背景）──"
g1=$(sh "$(dirname "$0")/rike-locate.sh" goldy "$FIX/cb6.jpg" 547 2>/dev/null | grep -oE '领取y=[^ ]+')
g2=$(sh "$(dirname "$0")/rike-locate.sh" goldy "$FIX/cb6.jpg" 688 2>/dev/null | grep -oE '领取y=[0-9]+')
ck "cb6 卡1(待做)应找不到金色大字" "领取y=未找到" "$g1"
ck "cb6 卡2(领取)应找到 y≈471~473" "领取y=472" "$g2"

echo
echo "── 结果：通过 $PASS ／ 失败 $FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
