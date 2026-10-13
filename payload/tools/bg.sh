#!/bin/sh
# bg.sh —— 把 App 丢到副屏"后台跑"（物理屏保持空闲）
#
#   sh tools/bg.sh start <包名|wechat|nshm> [0|1]  启动到副屏（不给屏号则自动猜）
#   sh tools/bg.sh stop  <包名|wechat|nshm>        停掉
#   sh tools/bg.sh pull  <包名|wechat|nshm>        从副屏**拉回物理屏**（不重启、不丢进度）
#   sh tools/bg.sh list                            各屏在跑什么
#   sh tools/bg.sh find  <关键词>                  搜包名
#
#   别名: wechat/微信 → com.tencent.mm（竖屏副屏 0）
#         nshm/逆水寒  → com.netease.nshm（横屏副屏 1）
. "$(dirname "$0")/../env.sh"
SHD="$DSH_ROOT/tools/shd.sh"

alias_pkg() {
  case "$1" in
    wechat|wx|微信)         echo com.tencent.mm ;;
    nshm|nishuihan|逆水寒)  echo com.netease.nshm ;;
    *)                      echo "$1" ;;
  esac
}
default_slot() {
  case "$1" in
    com.netease.nshm) echo 1 ;;
    *)                echo 0 ;;
  esac
}
launcher_of() {
  sh "$SHD" "cmd package resolve-activity --brief $1" 2>/dev/null \
    | tr -d '\r' | grep '/' | head -1
}
display_id_of() {
  curl -s --max-time 5 "http://127.0.0.1:$VSD_PORT/$1/info" \
    | sed -n 's/.*"displayId":\([0-9]*\).*/\1/p'
}
# 某 display 上当前顶层的包名
top_of() {
  sh "$SHD" "dumpsys activity activities | grep -A9 'Display #$1 (' | grep -m1 -oE 'u0 [a-zA-Z0-9._]+/'" 2>/dev/null \
    | tr -d '\r' | grep -v 'EXIT' | sed 's|^u0 ||; s|/$||' | head -1
}

case "$1" in
  start)
    PKG=$(alias_pkg "$2"); SLOT="${3:-$(default_slot "$PKG")}"
    [ -n "$PKG" ] || { echo "用法: sh bg.sh start <包名> [0|1]"; exit 2; }
    ID=$(display_id_of "$SLOT")
    [ -n "$ID" ] || { echo "❌ 副屏 $SLOT 不存在，先跑 ensure-vsd.sh"; exit 1; }
    CMP=$(launcher_of "$PKG")
    [ -n "$CMP" ] || { echo "❌ 解析不到 $PKG 的启动 Activity（没装？）"; exit 1; }
    echo "  副屏 $SLOT (displayId=$ID) ← $CMP"
    sh "$SHD" "am start --display $ID --windowingMode 1 -n $CMP" 2>&1 | grep -v 'EXIT' | sed 's/^/  /'
    sleep 5
    echo "  落点: $(top_of "$ID")"
    ;;
  stop)
    PKG=$(alias_pkg "$2")
    [ -n "$PKG" ] || { echo "用法: sh bg.sh stop <包名>"; exit 2; }
    sh "$SHD" "am force-stop $PKG" >/dev/null 2>&1
    echo "  已停 $PKG"
    ;;
  pull|to-physical)
    # 把 App 从副屏**拉回物理屏**（display 0）。
    # 用户 2026-10-05 明确要求：逆水寒日课做完必须拉回来，不能留在副屏挂着
    # —— 挂着照样吃 CPU/电，用户体感就是"手机卡卡的"。
    PKG=$(alias_pkg "$2")
    [ -n "$PKG" ] || { echo "用法: sh bg.sh pull <包名> [activity]"; exit 2; }
    ACT="$3"
    if [ -z "$ACT" ]; then
      # 关键：要用**它当前 task 的顶层 Activity**。`am start --display 0` 指到它时，
      # 系统会把已有 task 整块搬到物理屏（实测逆水寒回 "its current task has been
      # brought to the front"）；换成 launcher 的启动页会新开一个实例、丢进度。
      ACT=$(sh "$SHD" "dumpsys activity activities | grep -m1 -oE '$PKG/[A-Za-z0-9_.\$]+'" 2>/dev/null \
            | tr -d '\r' | grep -v EXIT | head -1)
    fi
    [ -n "$ACT" ] || ACT=$(launcher_of "$PKG")
    [ -n "$ACT" ] || { echo "❌ 找不到 $PKG 的 Activity"; exit 1; }
    echo "  拉回物理屏 ← $ACT"
    sh "$SHD" "am start --display 0 -n $ACT" 2>&1 | grep -v EXIT | sed 's/^/  /'
    sleep 4
    printf '  物理屏现在: %s\n' "$(top_of 0)"
    ;;
  list)
    printf '  %-26s %s\n' "物理屏 Display #0:" "$(top_of 0)"
    for i in 0 1; do
      ID=$(display_id_of "$i")
      [ -n "$ID" ] || continue
      printf '  %-26s %s\n' "副屏 $i (displayId=$ID):" "$(top_of "$ID")"
    done
    ;;
  find)
    sh "$SHD" "pm list packages | grep -i '$2'" 2>/dev/null | tr -d '\r' | grep -v EXIT | sed 's/^/  /'
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
