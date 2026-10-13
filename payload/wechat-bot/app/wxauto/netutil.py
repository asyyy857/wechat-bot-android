# -*- coding: utf-8 -*-
"""带「总时长硬上限」的 HTTP 取数（2026-10-12）。

为什么要它：``urlopen(timeout=N)`` 的 N 只管**单次套接字操作**（连接 / 读一次）。
对端一滴一滴往外挤字节，每次间隔都小于 N，整个请求就能无限拖下去。
机器人主循环是同步的，一次请求卡死 = 整个机器人卡死（漏掉这段时间的群消息）。

做法：把 ``urlopen`` + 读取整体放进一个**守护线程**，调用线程最多等 ``timeout + slack``，
超了就抛 ``TimeoutError`` 走原有的异常处理（调用方本来就都 try/except 了）。
线程留作 daemon 自生自灭，不会拖住进程退出。

⚠️ 只用于**幂等的读**（查资料 / 下载图 / 向量检索）。发送这类不可重做的动作
不要用它——那条路在 platforms/android.py 里有专门的「不确定」处理。
"""
from __future__ import annotations

import threading
import urllib.request
from typing import Any, Callable


def fetch_hard(req, timeout: float, handler: Callable[[Any], Any],
               slack: float = 5.0) -> Any:
    """``urlopen(req, timeout)`` 后把响应交给 ``handler(resp)``，返回它的结果。

    handler 在工作线程里跑（读 body 也算在总时长里）；handler 抛的异常原样重抛。
    总时长超过 ``timeout + slack`` 抛 ``TimeoutError``。
    """
    box: dict = {}

    def work() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                box["val"] = handler(resp)
        except BaseException as exc:             # 带回调用线程处理
            box["exc"] = exc

    th = threading.Thread(target=work, name="net-fetch", daemon=True)
    th.start()
    th.join(float(timeout) + float(slack))
    if th.is_alive():
        raise TimeoutError("总时长超过 %.0fs（硬超时）" % (float(timeout) + float(slack)))
    if "exc" in box:
        raise box["exc"]
    return box.get("val")
