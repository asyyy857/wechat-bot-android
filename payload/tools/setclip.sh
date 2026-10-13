#!/bin/sh
# setclip.sh "文本" —— 通过 dsh-clip 小工具设置系统剪贴板
# 为什么要绕这一下：Android 16/OPPO 移除了 cmd clipboard，shell 无法直接写剪贴板，
# 而剪贴板是往虚拟屏微信输入框送中文的唯一干净通道。
set -e
TEXT="${1-}"
if [ -z "$TEXT" ] && [ ! -t 0 ]; then TEXT=$(cat); fi
B64=$(printf '%s' "$TEXT" | base64 -w0)
exec sh /sdcard/dsh/tools/shd.sh \
  "am start -n com.dsh.clip/.SetClipActivity --es text64 '$B64'" 2>&1 | tr -d '\r' | grep -v EXIT
