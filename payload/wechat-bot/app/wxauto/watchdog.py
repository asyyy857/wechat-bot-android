# -*- coding: utf-8 -*-
"""主循环卡死诊断看门狗（2026-10-12）。

为什么要它：模型调用已经有硬超时（brain._fetch），但主循环还可能卡在别处 ——
OCR、adb、桥接请求、子进程。卡住时日志里是一片沉默，事后只能猜是谁。

它只做一件事：**一轮干活超过阈值，就把主线程当前的调用栈写进日志**，
让你一眼看出卡在哪一行。⚠️ 只记录，**不杀进程、不重启、不打断**——
误杀一次正在发送的流程比卡一会儿更糟（重复发言 / 发错群都出过事故）。

用法（bot.run 里）：
    wd = StallWatchdog(log, secs)      # secs<=0 即关闭
    wd.arm("本轮")                     # 开始干活前
    ...干活...
    wd.disarm()                        # 干完、进入睡眠/挂起前（睡眠不算卡）
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Optional


class StallWatchdog:
    def __init__(self, log, secs: float, repeat: int = 3, max_frames: int = 14) -> None:
        self.log = log
        try:
            self.secs = float(secs)
        except Exception:
            self.secs = 0.0
        self.repeat = max(1, int(repeat))        # 同一轮最多报几次，防刷屏
        self.max_frames = max(4, int(max_frames))
        self.fired = 0                           # 累计报警次数（测试 / 体检用）
        self._lock = threading.Lock()
        self._gen = 0                            # 每次 arm/disarm 自增，迟到的定时器据此作废
        self._timer: Optional[threading.Timer] = None
        self._t0 = 0.0
        self._label = ""
        self._tid = threading.get_ident()        # 被监视的线程 = 创建它的线程（主线程）
        self._n = 0                              # 当前这一轮已报几次

    @property
    def enabled(self) -> bool:
        return self.secs > 0

    def arm(self, label: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            self._cancel_locked()
            self._gen += 1
            self._t0 = time.time()
            self._label = label
            self._n = 0
            self._schedule_locked(self._gen)

    def disarm(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._gen += 1
            self._cancel_locked()

    # ---- 内部 ----
    def _cancel_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_locked(self, gen: int) -> None:
        t = threading.Timer(self.secs, self._fire, args=(gen,))
        t.daemon = True                          # 绝不能因为它拖住进程退出
        self._timer = t
        t.start()

    def _fire(self, gen: int) -> None:
        try:
            with self._lock:
                if gen != self._gen:             # 已经 disarm / 重新 arm 过，作废
                    return
                self._n += 1
                n = self._n
                held = time.time() - self._t0
                label = self._label
                if n < self.repeat:              # 还没报满就再约下一次
                    self._schedule_locked(gen)
            frame = sys._current_frames().get(self._tid)
            if frame is None:
                stack = "（取不到主线程栈）"
            else:
                lines = traceback.format_stack(frame)[-self.max_frames:]
                stack = "".join(lines).rstrip()
            self.fired += 1
            self.log.error(
                "⏱ 看门狗：%s已经 %.0f 秒没走完（第 %d/%d 次提醒，只记录、不干预）。"
                "主线程现在停在：\n%s" % (label or "本轮", held, n, self.repeat, stack))
        except Exception as exc:                 # 诊断工具自己出错，绝不能影响机器人
            try:
                self.log.warning("看门狗自身出错（忽略）：%r" % (exc,))
            except Exception:
                pass
