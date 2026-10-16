#!/bin/sh
# link.sh —— 容器重建后恢复 HUD 入口（/sdcard 是 noexec，只能复制不能软链）
set -e
cp /sdcard/dsh/hud/hud.js /usr/local/bin/hud
chmod +x /usr/local/bin/hud

# phone 包装层：先备份原脚本，再换成会顺带推浮窗的版本
if [ ! -f /usr/local/bin/phone-real ]; then
  cp /usr/local/bin/phone /usr/local/bin/phone-real
  chmod +x /usr/local/bin/phone-real
fi
cp /sdcard/dsh/hud/phone-orig.js /usr/local/bin/phone-real
chmod +x /usr/local/bin/phone-real
cp /sdcard/dsh/hud/phone-wrapper.sh /usr/local/bin/phone
chmod +x /usr/local/bin/phone

echo "入口已就位：hud=$(command -v hud) phone=$(command -v phone)"
