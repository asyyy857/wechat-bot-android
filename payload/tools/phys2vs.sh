#!/bin/sh
# phys2vs.sh —— 物理屏(横屏有效区) ↔ 副屏 坐标换算
#
#   为什么比例是 1.986：
#     物理屏（内置）1264×2780 @density 560(3.5)  → 竖屏 361×794 dp
#       横过来用（游戏是横屏）    → 794×361 dp，像素 2780×1264
#     副屏1 1400×636 @density 280(1.75)          → 800×363 dp
#     → **两侧 dp 布局只差 0.5%**，所以游戏 UI 位置一一对应，坐标就是线性缩放：
#        物理屏x = 副屏x × 1.986     副屏x = 物理屏x ÷ 1.986
#        （2780/1400 = 1.9857，1264/636 = 1.9874）
#   📌 结论：**不需要"统一屏幕配置"** —— dp 一致就够了，改像素只会让游戏渲染更慢、
#      而且现有全部坐标要重标。真正要做的只是把这一处换算写成公式，别再手算。
#
#   用法: sh tools/phys2vs.sh 1504 1132      # 物理屏 → 副屏
#         sh tools/phys2vs.sh -r 757 566     # 副屏 → 物理屏
#         sh tools/phys2vs.sh 1504 1132 --crop 900x50   # 连裁剪框一起换算
K=${PHYS2VS_K:-1.986}
if [ "$1" = "-r" ]; then shift; INV=1; fi
X=$1; Y=$2; shift 2 2>/dev/null
if [ -z "$X" ] || [ -z "$Y" ]; then
  echo "用法: sh tools/phys2vs.sh [-r] <x> <y> [--crop WxH]"; exit 2
fi
awk -v k="$K" -v inv="${INV:-0}" -v x="$X" -v y="$Y" -v extra="$*" 'BEGIN{
  if (inv) { printf "副屏(%d,%d) → 物理屏(%d,%d)", x, y, x*k, y*k }
  else     { printf "物理屏(%d,%d) → 副屏(%d,%d)", x, y, x/k, y/k }
  if (extra != "") printf "   [裁剪框 %s 需 ÷%.3f 或 ×%.3f]", extra, (inv?1:k), (inv?k:1)
  printf "\n"
}'
