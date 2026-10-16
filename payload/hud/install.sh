#!/bin/sh
# install.sh —— 装「DSH 浮窗」并授权（走命令守护，不需要 adb）
set -e
cd "$(dirname "$0")"
APK=$PWD/bin/dsh-hud.apk
[ -f "$APK" ] || { echo "缺 $APK，先跑 build.sh"; exit 1; }

echo "[1/3] 安装"
sh /sdcard/dsh/tools/shd.sh \
  "cp $APK /data/local/tmp/dsh-hud.apk && pm install -r /data/local/tmp/dsh-hud.apk"

echo "[2/3] 授予悬浮窗权限（appops）"
sh /sdcard/dsh/tools/shd.sh "appops set com.dsh.hud SYSTEM_ALERT_WINDOW allow"

echo "[3/3] 校验"
sh /sdcard/dsh/tools/shd.sh "dumpsys package com.dsh.hud | grep -m1 versionName"
echo "装好了。启动：sh /sdcard/dsh/hud/start.sh"
