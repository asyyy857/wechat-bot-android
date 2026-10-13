#!/bin/sh
# rike-find.sh —— 不依赖 OCR 的「定位 / 识别」（★2026-10-10 新增）
#
#   sh tools/rike-find.sh role [帧图]                  识别角色名 → 玉米鹿|清月辞|醉梦辰 + 分数
#   sh tools/rike-find.sh tmpl <模板名> [帧图] [区域]   在区域里找模板 → "x y 分数"（中心坐标）
#   sh tools/rike-find.sh list                         列出可用模板
#   sh tools/rike-find.sh rg [帧图] [区域]              红-绿差（页面判别：侠缘≈3 / 主世界≈67）
#   sh tools/rike-find.sh bright [帧图] [区域]          平均亮度
#
# ★ 为什么要有它：游戏是**自绘 UI**，OCR 对它有一半界面是瞎的 ——
#   美术字（闯荡江湖/进入游戏/领取）、竖排字（侠缘菜单）、手绘地图（全乱码）。
#   但**图形本身是固定的** → 模板匹配 / 颜色统计能稳定识别，而且更便宜：
#     · 同区域 RMSE 比对   ~0.1s   角色名区分度实测 0.000（自己）vs ≥0.159（别人）
#     · 子图搜索(限区域)   ~1.4s   位置精确到像素（300x200 区域找 60x60 实测 1.44s）
#     · 颜色统计           ~0.05s  侠缘上部 r-g=3，主世界=67（2026-10-10 实测）
#   ⚠️ 千万别在大图上无脑子图搜索（1400x636）—— 会慢到几十秒。**先裁候选区域**。
. "$(dirname "$0")/../env.sh"
TDIR="$DSH_ROOT/tools/rike-tmpl"
SLOT=${RIKE_SLOT:-1}
VS="node $DSH_ROOT/tools/vscreen -i $SLOT"
DEF=/tmp/rike-find.png

getframe(){  # $1=用户给的帧图（可空）→ 输出路径
  if [ -n "$1" ] && [ -f "$1" ]; then echo "$1"
  else $VS shot "$DEF" >/dev/null 2>&1; echo "$DEF"; fi
}
# 取 compare 的归一化 RMSE（括号里的数）
rmse(){ printf '%s' "$1" | grep -oE '\([0-9.]+\)' | head -1 | tr -d '()'; }

case "$1" in
  list) ls "$TDIR" 2>/dev/null | sed 's/\.png$//' ;;

  role)
    F=$(getframe "$2")
    convert "$F" -crop 320x110+1040+420 +repage /tmp/rf-hay.png 2>/dev/null
    best=""; bs=9
    for t in "$TDIR"/role-*.png; do
      [ -f "$t" ] || continue
      nm=$(basename "$t" .png); nm=${nm#role-}
      out=$(compare -metric RMSE -subimage-search /tmp/rf-hay.png "$t" /tmp/rf-diff.png 2>&1 | head -1)
      s=$(rmse "$out"); [ -z "$s" ] && s=9
      printf '  %-8s RMSE=%s\n' "$nm" "$s" >&2
      if awk -v a="$s" -v b="$bs" 'BEGIN{exit !(a<b)}'; then bs="$s"; best="$nm"; fi
    done
    echo "$best $bs"
    ;;

  tmpl)
    TN=$2; F=$(getframe "$3"); REG=${4:-1400x636+0+0}
    T="$TDIR/$TN.png"
    [ -f "$T" ] || { echo "❌ 没有模板 $TN（用 list 看有哪些）"; exit 1; }
    # ★★ 2026-10-10 教训：**模板必须是"有内容"的图**（标准差够高），否则匹配分数毫无意义 ——
    #   当天 btn-快门 模板的标准差只有 1（一块纯色），它跟任何纯色区域都能"匹配"，
    #   于是给出"精确到像素"的假象（其实是纯色配纯色）。这里强制体检。
    SD=$(identify -format '%[fx:int(standard_deviation*255)]' "$T" 2>/dev/null)
    if [ "${SD:-0}" -lt 8 ]; then
      echo "❌ 模板 $TN 几乎没有特征（标准差=$SD）→ 匹配分数不可信，请换一块有内容的区域重裁"; exit 1
    fi
    convert "$F" -crop "$REG" +repage /tmp/rf-hay.png 2>/dev/null
    out=$(compare -metric RMSE -subimage-search /tmp/rf-hay.png "$T" /tmp/rf-diff.png 2>&1 | head -1)
    s=$(rmse "$out")
    pos=$(printf '%s' "$out" | grep -oE '@ [0-9]+,[0-9]+' | head -1 | sed 's/@ //')
    [ -z "$pos" ] && { echo "❌ 没找到"; exit 1; }
    ox=$(printf '%s' "$REG" | sed -n 's/.*+\([0-9][0-9]*\)+\([0-9][0-9]*\)$/\1/p')
    oy=$(printf '%s' "$REG" | sed -n 's/.*+\([0-9][0-9]*\)+\([0-9][0-9]*\)$/\2/p')
    tw=$(identify -format '%w' "$T"); th=$(identify -format '%h' "$T")
    px=$(printf '%s' "$pos" | cut -d, -f1); py=$(printf '%s' "$pos" | cut -d, -f2)
    echo "$(( ${ox:-0} + px + tw/2 )) $(( ${oy:-0} + py + th/2 )) $s"
    ;;

  rg)
    F=$(getframe "$2"); REG=${3:-1400x220+0+0}
    convert "$F" -crop "$REG" +repage -format '%[fx:int((mean.r-mean.g)*255)]' info: 2>/dev/null; echo
    ;;

  bright)
    F=$(getframe "$2"); REG=${3:-1400x120+0+516}
    convert "$F" -crop "$REG" +repage -format '%[fx:int(mean.r*255)]' info: 2>/dev/null; echo
    ;;

  *) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
