#!/bin/sh
# bootstrap.sh —— 容器重建后的一键复原（★ 原文档缺这一步）
#
#   背景：adb / java / tesseract / vscreen 这些装在容器根文件系统里，
#         容器一重建就全没了；而 /sdcard 是持久的。
#         所以"恢复"必须从 /sdcard 这一份开始。
#
#   用法: sh /sdcard/dsh/bootstrap.sh [--no-apt]
set -e
D=/sdcard/dsh
. "$D/env.sh"

echo "=== [1/5] 基础工具 ==="
NEED=""
for c in curl unzip javac java tesseract adb git; do
  command -v "$c" >/dev/null 2>&1 || NEED="$NEED $c"
done
if [ -n "$NEED" ] && [ "$1" != "--no-apt" ]; then
  echo "  缺:$NEED → 开始安装"
  # 源修复：arm64 属 ports 架构，必须走 ubuntu-ports 路径（主档案 ubuntu/ 无 binary-arm64 → 404）
  if ! grep -q 'ubuntu-ports' /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null; then
    cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak 2>/dev/null || true
    cat > /etc/apt/sources.list.d/ubuntu.sources <<'SRC'
Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: noble noble-updates noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
SRC
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends ca-certificates curl wget unzip xz-utils \
    adb git openjdk-17-jdk-headless tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng
else
  echo "  工具齐备（或 --no-apt）"
fi

echo "=== [2/5] 恢复 jar 到容器内传统路径 ==="
mkdir -p /usr/lib/android-sdk/platforms/android-23 /usr/lib/android-sdk/build-tools/34.0.0/lib /usr/local/bin
[ -f "$D/vscreen-src/android.jar" ] && cp -f "$D/vscreen-src/android.jar" /usr/lib/android-sdk/platforms/android-23/android.jar
[ -f "$D/vscreen-src/d8.jar" ]      && cp -f "$D/vscreen-src/d8.jar"      /usr/lib/android-sdk/build-tools/34.0.0/lib/d8.jar
echo "  android.jar / d8.jar 已就位"

echo "=== [3/5] 恢复 /usr/local/bin 入口 ==="
cat > /usr/local/bin/vscreen <<'W'
#!/bin/sh
exec node /sdcard/dsh/tools/vscreen "$@"
W
chmod 755 /usr/local/bin/vscreen
cat > /usr/local/bin/shd <<'W'
#!/bin/sh
exec sh /sdcard/dsh/tools/shd.sh "$@"
W
chmod 755 /usr/local/bin/shd
cat > /usr/local/bin/ocr <<'W'
#!/bin/sh
exec sh /sdcard/dsh/tools/ocr.sh "$@"
W
chmod 755 /usr/local/bin/ocr
cat > /usr/local/bin/bgapp <<'W'
#!/bin/sh
exec sh /sdcard/dsh/tools/bg.sh "$@"
W
chmod 755 /usr/local/bin/bgapp
echo "  vscreen / shd / ocr / bgapp 已挂到 /usr/local/bin"

echo "=== [4/5] 编译守护 dex ==="
[ -f "$D/vscreen-daemon/bin/classes.dex" ] || sh "$D/vscreen-daemon/build.sh"
[ -f "$D/shell-daemon/bin/classes.dex" ]   || sh "$D/shell-daemon/build.sh"
echo "  vs=$(stat -c%s "$D/vscreen-daemon/bin/classes.dex" 2>/dev/null || echo 缺) 字节, shd=$(stat -c%s "$D/shell-daemon/bin/classes.dex" 2>/dev/null || echo 缺) 字节"

echo "=== [5/5] 守护与 adb 状态 ==="
curl -s --max-time 4 "http://127.0.0.1:$VSD_PORT/list"  >/dev/null 2>&1 && echo "  ✅ 虚拟屏守护 :$VSD_PORT 在跑" || echo "  ✗ 虚拟屏守护 :$VSD_PORT 未跑"
curl -s --max-time 4 -H "X-Token: $(cat $D/.shd-token)" "http://127.0.0.1:$SHD_PORT/health" >/dev/null 2>&1 && echo "  ✅ 命令守护 :$SHD_PORT 在跑" || echo "  ✗ 命令守护 :$SHD_PORT 未跑"
if [ -n "$SERIAL" ]; then
  adb devices 2>/dev/null | grep -q "$SERIAL" && echo "  ✅ adb 已连 $SERIAL" || echo "  ✗ adb 串号有但设备未连"
else
  echo "  ✗ 无 adb 串号（$D/.adb-serial）—— 需打开「无线调试」后跑 connect-adb.sh"
fi
echo
echo "=== [5.5] DSH 浮窗入口 ==="
if [ -f "$D/hud/link.sh" ]; then
  sh "$D/hud/link.sh" >/dev/null 2>&1 && echo "  ✅ hud / phone 入口已就位" || echo "  ✗ HUD 入口恢复失败"
  curl -s --max-time 4 "http://127.0.0.1:18787/ping" >/dev/null 2>&1 \
    && echo "  ✅ 浮窗服务在跑" \
    || echo "  · 浮窗服务未跑（下次调用 hud 会自动拉起）"
else
  echo "  · 没有 $D/hud/link.sh，跳过"
fi
echo

echo "=== [6/6] git 全局配置 ==="
sh "$D/git-setup.sh" 2>/dev/null || echo "  · 跳过 git 配置"
echo
echo "复原流程结束。守护未起时依次跑："
echo "  sh $D/connect-adb.sh && sh $D/vscreen-daemon/start.sh && sh $D/shell-daemon/start.sh"
