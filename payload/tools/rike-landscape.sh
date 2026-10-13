#!/bin/sh
# rike-landscape.sh —— 把日课副屏锁成横屏（★2026-10-11 加，解决剧组站"退不出来"）
#
# 【为什么需要】2026-10-11 实测（AOSP 源码级）：
#   剧组站这类**竖屏模块**会向系统请求 SCREEN_ORIENTATION_PORTRAIT，而 virtual display
#   默认 `ignoreOrientationRequest=false` → **副屏真的被转 90°**：
#     实测进剧组站前 `mDisplayRotation=ROTATION_0 / cur=1400x636`，
#          进剧组站后 `mDisplayRotation=ROTATION_270 / cur=636x1400`。
#   此时**截图仍是 1400×636**（内容转 90° 塞进去），而 `input -d <id>` 用的是**逻辑坐标**
#   → 两套坐标空间整整差 90°，表现就是"点了没反应 / 点到了别的元素"，
#   剧组站因此**退不出来**（旧结论"只能重启游戏"就是这么来的）。
#
# 【修法效果】锁横屏后副屏不再跟着应用转，竖屏模块改为 **letterbox 居中竖条**显示：
#   实测窗口 `mBounds=Rect(556,0-844,636)`（中间 288px 宽），**方向正常、坐标可点** ——
#   退出键「◁ 剧组站」在 **(588,34)**，点一次即退出，**游戏不用重启**。
#
# 【为什么必须每次设】该设置按 display 记录，而**虚拟屏重建后 displayId/uniqueId 会变**
#   （本机铁律：任何地方都别写死屏号）→ 这里每次现查 displayId 再设，幂等可重复跑。
#   ⚠️ 只锁**日课副屏**（默认副屏 1 = dsh-trusted-1）。**别碰副屏 0**（微信读屏在用）。
#
#   用法：sh tools/rike-landscape.sh [副屏序号=1]
. "$(dirname "$0")/../env.sh"
SHD="$DSH_ROOT/tools/shd.sh"
SLOT=${1:-1}

DID=$(sh "$DSH_ROOT/tools/bg.sh" list 2>/dev/null | grep "副屏 $SLOT" \
      | sed -n 's/.*displayId=\([0-9]*\).*/\1/p' | head -1)
if [ -z "$DID" ]; then
  echo "  ❌ 查不到副屏 $SLOT 的 displayId（守护没跑？先 sh ensure-vsd.sh）"; exit 1
fi

echo "  ▸ 副屏 $SLOT = displayId=$DID → 锁成横屏（fixed-to-user-rotation enabled）"
sh "$SHD" "wm fixed-to-user-rotation -d $DID enabled" >/dev/null 2>&1
sleep 2

CUR=$(sh "$SHD" "dumpsys window displays" 2>/dev/null \
      | grep -m1 "mDisplayId=$DID" -A 3 | grep -oE 'cur=[0-9]+x[0-9]+' | head -1)
ROT=$(sh "$SHD" "dumpsys window displays" 2>/dev/null \
      | grep -m1 "mDisplayId=$DID" -A 6 | grep -oE 'mDisplayRotation=ROTATION_[0-9]+' | head -1)
echo "     现在 $CUR  $ROT"
case "$CUR" in
  cur=1400x636) echo "  ✅ 已锁横屏（竖屏模块会以居中竖条显示，坐标可用）" ;;
  *)           echo "  ⚠️ 还不是横屏（$CUR）—— 若正卡在竖屏模块里，退出该模块后会自动恢复；不行就再跑一次本脚本" ;;
esac
