#!/bin/bash
# ============================================================================
#  wxlock.sh —— 微信机器人「操作锁」：同一时刻只允许一个会话/脚本动机器人。
#
#  为什么需要：两个会话同时动机器人会打架（各自 kill 进程 → 可能跑出两个机器人
#  发双份消息；各自读写 rules.yaml → 后写的把先加的规则覆盖掉）。
#
#  用法（**必须 source，不能当脚本跑**，否则锁随子进程立刻释放）：
#
#      source /sdcard/dsh/wechat-auto/wxlock.sh "bot.sh start" || exit 1
#
#  · 抢不到锁：打印「对方 PID / 在干什么 / 什么时候开始」并返回 1；
#  · 抢到锁：把同样的信息写进锁文件，进程退出时 flock 自动释放（不会死锁残留）；
#  · 已经持锁的脚本再去调子脚本时，靠 WX_LOCK_HELD 传递，免得自己跟自己抢；
#  · 只给「会改状态」的操作上锁 —— 急停（stop / stop.sh）**故意不上锁**，
#    任何时候都要能立刻把机器人按停。
#
#  ⚠️ 大坑（实测踩过）：**锁 fd 会被子进程继承**。持锁脚本里 `nohup` 起的
#  长驻进程（wxbridge.py / main_android.py）会把 fd 9 一起带走，于是这两个
#  进程一直攥着锁 —— 之后所有会话都抢不到（锁内容还停在第一个写入者）。
#  所以**每处启动长驻进程都要带 `9>&-`**（关闭继承的锁 fd）。加新的启动点
#  时别忘了这一条。
# ============================================================================
LOCK=/tmp/wechat-auto.lock

_wxlock_fail() {
  echo "⚠️ 另一个会话正在操作微信机器人，先等它做完："
  sed 's/^/     /' "$LOCK" 2>/dev/null || echo "     （锁文件读不到）"
  echo "     （锁：$LOCK —— 对方进程一结束就自动释放，过几秒重试即可）"
  echo "     （如果你确认没人动、只是想强行继续：rm -f $LOCK 后重试）"
}

if [ "${WX_LOCK_HELD:-}" = "1" ]; then        # 自己人：已经在锁里了
  return 0 2>/dev/null || exit 0
fi

exec 9>>"$LOCK" || { echo "❌ 打不开锁文件 $LOCK"; return 1 2>/dev/null || exit 1; }
if ! flock -n 9; then
  _wxlock_fail
  return 1 2>/dev/null || exit 1
fi

export WX_LOCK_HELD=1
printf 'PID %s  %s  开始 %s\n' "$$" "${1:-?}" "$(date '+%F %T')" > "$LOCK"
