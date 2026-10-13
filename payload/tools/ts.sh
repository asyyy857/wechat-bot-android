#!/bin/sh
# ts.sh —— 给上游输出**逐行加"相对起始秒数"时间戳**（★2026-10-10 加）
#
#   sh tools/rike.sh do 好感度 3 2>&1 | sh tools/ts.sh
#
# 为什么要它：找"这一段为什么慢"必须知道**每一行是什么时候打出来的**。
#   ⚠️ 2026-10-10 踩过：用 awk `cmd|getline t; close(cmd)` 打时间戳，**整段全显示同一秒**
#   （awk 的 getline/close 在这台机器上不重新执行命令）→ 完全看不出长尾，白折腾一轮。
#   → 改用 python3 -u 逐行透传（无缓冲、可靠）。
python3 -u -c "
import sys, time
t0 = time.time()
for line in sys.stdin:
    print('[%6.1fs] %s' % (time.time() - t0, line.rstrip()), flush=True)
"
