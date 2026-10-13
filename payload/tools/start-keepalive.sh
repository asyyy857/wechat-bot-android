#!/bin/sh
# start-keepalive.sh —— 单例启动 adb 看护（单独一个文件，免得在命令行里写它的名字
# 又被 pkill -f 自匹配杀掉 —— 2026-10-06 连踩两次）。
#
# ★2026-10-12 抖动修复（见 adb抖动-原因审查-20261012.md 的 C2）：
#   10-12 复查时容器里**同时躺着两个 adb-keepalive**（PID 19882 / 19910），pidfile 只记了一个。
#   后果：两个实例各判各的、各调各自 connect-adb.sh —— 一个刚 connect 上的 transport
#   被另一个判"不通"再扫再断，**抖动自我维持**。两道保险：
#     ① **判定前移**：启动前先扫 /proc 找真身（原来要等 sleep 6 之后才回写 pidfile，
#        两个调用者在这 6 秒里都认为"没在跑"）；
#     ② **flock 串行化**整个"检查 → 启动 → 回写"窗口。
#   ⚠️ /tmp 被清空（容器重启）导致 pidfile 丢失时，第 ① 步也能靠 /proc 认出现有实例。
. "$(dirname "$0")/../env.sh"
K="$DSH_ROOT/tools/adb-keepalive.sh"
PIDF=/tmp/adb-keepalive.pid
LOCK=/tmp/adb-keepalive.lock

# 只认"sh/bash 在跑 tools/adb-keepalive.sh"，与机器人的判据（botpids.sh）同一套思路
_pid_of_keepalive() {
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [ -r "$d/cmdline" ] || continue
    c=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    c=${c% }
    first=${c%% *}
    case "${first##*/}" in sh|bash|dash) ;; *) continue ;; esac
    case "$c" in *"tools/adb-keepalive.sh"*) echo "$p"; return 0 ;; esac
  done
  return 1
}

exec 9>"$LOCK"
flock -w 60 9 || { echo "❌ 拿不到锁（另一个启动流程卡住了？）"; exit 1; }

# ① 判定前移：已经有真身在跑就直接退出（顺手把脏/丢了的 pidfile 纠正回来）
p=$(_pid_of_keepalive)
if [ -n "$p" ]; then
  if [ "$(cat "$PIDF" 2>/dev/null)" != "$p" ]; then echo "$p" > "$PIDF"; fi
  echo "已在跑（PID $p）"; exit 0
fi

# ② 没有真身才启动
#    ⚠️ **必须 9>&-**：子进程会继承 fd 9，而 flock 锁绑在"打开的文件描述"上 ——
#    不关掉的话，看护本体活着就一直替我们攥着锁，之后每次启动都报「拿不到锁」
#    （2026-10-12 实测踩到：两个并发调用都失败）。
setsid nohup sh "$K" 30 >/dev/null 2>&1 9>&- < /dev/null &
sleep 6
p=$(_pid_of_keepalive)
if [ -n "$p" ]; then echo "$p" > "$PIDF"; echo "✅ 已启动（PID $p）"; else echo "❌ 没起来"; fi
