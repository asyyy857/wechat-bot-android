#!/bin/sh
# ensure-vsd.sh —— 保证"可信虚拟屏守护"在跑；缺 dex 时从已安装 APK 自解包
#   用法: sh ensure-vsd.sh [端口]    默认取 env.sh 的 VSD_PORT（现为 18250；18240 是被劫持的坏端口）
. "$(dirname "$0")/env.sh"
PORT="${1:-$VSD_PORT}"
D="$DSH_ROOT/vscreen-daemon"

# 1) 已在跑？
if curl -s --max-time 5 "http://127.0.0.1:$PORT/list" | grep -q displayId; then
  echo "✅ 守护在跑："; curl -s --max-time 5 "http://127.0.0.1:$PORT/list"; echo; exit 0
fi

# 2) 缺 dex → 从已安装 APK 自解包（老办法，仅在无源码/无编译环境时用）
if [ ! -f "$D/bin/classes.dex" ]; then
  echo "  工作区缺守护 dex，尝试从已安装 APK 自解包"
  [ -n "$SERIAL" ] || { echo "❌ 读不到 adb 串号，且本地无 dex"; exit 1; }
  APK=$(adb -s "$SERIAL" shell pm path dsh.vscreen | tr -d '\r' | sed 's/package://' | head -1)
  [ -n "$APK" ] || { echo "❌ dsh.vscreen 未安装，且本地无源码 —— 请先跑 build.sh"; exit 1; }
  adb -s "$SERIAL" pull "$APK" /tmp/vs-app.apk >/dev/null 2>&1 || { echo "❌ 拉取 APK 失败"; exit 1; }
  mkdir -p "$D/bin"
  unzip -o -q /tmp/vs-app.apk 'assets/vs-daemon.dex' -d /tmp/vs-unpack \
    && cp -f /tmp/vs-unpack/assets/vs-daemon.dex "$D/bin/classes.dex" \
    || { echo "❌ 解包 assets 失败"; exit 1; }
  rm -rf /tmp/vs-unpack /tmp/vs-app.apk
  echo "  ✅ 已解出 $D/bin/classes.dex（$(stat -c%s "$D/bin/classes.dex") 字节）"
fi

# 3) 启动
[ -x "$D/start.sh" ] || [ -f "$D/start.sh" ] || { echo "❌ 缺启动脚本 $D/start.sh"; exit 1; }
sh "$D/start.sh" "$PORT"
