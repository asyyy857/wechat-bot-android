#!/bin/sh
# imetype.sh "文本" —— 用 dsh-clip 的无键盘输入法，把文字提交到「当前焦点输入框」
#
# 为什么用它：Route B（微信跑在虚拟屏）下，
#   - shell `input text` 只对 ASCII 可靠，中文进不去；
#   - 剪贴板粘贴要长按唤菜单，极不稳、易误触；
#   - 输入法是系统合法通道，commitText() 能原样送进任意 Unicode。
# 本脚本会临时把默认输入法换成「DSH 输入」（无键盘、不弹物理屏键盘），
# 广播 commit 之后再切回用户原来的输入法。
#
# 用法: sh tools/imetype.sh "要输入的文字"
set -e
ROOT=/sdcard/dsh
SHD="$ROOT/tools/shd.sh"

TEXT="${1-}"
[ -n "$TEXT" ] || { echo "用法: sh $0 \"文本\""; exit 2; }

USER_IME=$(sh "$SHD" "settings get secure default_input_method" 2>/dev/null | tr -d '\r' | grep -v EXIT | head -1)
[ -n "$USER_IME" ] || USER_IME=com.iflytek.inputmethod/.FlyIME

B64=$(printf '%s' "$TEXT" | base64 -w0)

sh "$SHD" "ime set com.dsh.clip/.DshIme" >/dev/null 2>&1
sleep 0.8
sh "$SHD" "am broadcast -a com.dsh.clip.COMMIT --es text64 '$B64'" >/dev/null 2>&1
sleep 0.8
sh "$SHD" "ime set $USER_IME" >/dev/null 2>&1
echo "  ✅ 已提交（原输入法 $USER_IME 已恢复）: $TEXT"
