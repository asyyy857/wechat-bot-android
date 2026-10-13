#!/bin/sh
# install.sh —— 装到手机（走命令守护，不需要 adb）
set -e
cd "$(dirname "$0")"
APK=$PWD/bin/dsh-clip.apk
[ -f "$APK" ] || { echo "缺 $APK，先跑 build.sh"; exit 1; }
sh /sdcard/dsh/tools/shd.sh \
  "cp $APK /data/local/tmp/dsh-clip.apk && pm install -r /data/local/tmp/dsh-clip.apk"
echo "验证："
sh /sdcard/dsh/tools/shd.sh "pm list packages | grep com.dsh.clip"
