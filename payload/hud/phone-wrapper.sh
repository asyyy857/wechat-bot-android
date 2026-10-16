#!/bin/sh
# phone —— 原手机操作桥的包装层。
#
# 新增两件事：
#   ① 执行前把"agent 正在干什么"异步写到屏幕浮窗上 —— 用户看不见 agent 在他手机里
#      做了什么，浮窗是他唯一的进度反馈；
#   ② 交还界面（open DSH 自己）时告诉浮窗"可以收了" —— 浮窗平时**不显示**，
#      只在 agent 占着他屏幕的这段时间出现，操作一结束就消失（用户 2026-10-05 要求）。
#
# 推送走后台，绝不拖慢真正的手机操作。
REAL=/usr/local/bin/phone-real

LINGER=""
case "$1" in
  open)
    case "$2" in
      *dshmobile.probe*|*DSH*|*Tdsh*)
        SAY="界面已交还，浮窗即将收起"
        LINGER="--linger 2"
        ;;
      *) SAY="切到应用：$2" ;;
    esac
    ;;
  shot)   SAY="看一眼屏幕（截图）" ;;
  ui)     SAY="读取界面控件" ;;
  click)  SAY="点击「$2」" ;;
  tap)    SAY="点击 (${2:-?}, ${3:-?})" ;;
  swipe)  SAY="滑动屏幕" ;;
  text)   SAY="输入文字" ;;
  ime)    SAY="切换输入法" ;;
  key)    SAY="按键 $2" ;;
  status) SAY="检查手机状态" ;;
  apps)   SAY="列出可用应用" ;;
  *)      SAY="" ;;
esac

if [ -n "$SAY" ]; then
  ( /usr/local/bin/hud -t "DSH 操作手机" -d "" $LINGER "$SAY" >/dev/null 2>&1 & )
fi

exec "$REAL" "$@"
