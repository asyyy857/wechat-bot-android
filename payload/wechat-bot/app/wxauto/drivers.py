# -*- coding: utf-8 -*-
"""平台接口契约 —— 这就是"手机端移植"的那个插口。

上层（matcher / brain / governor / state / bot）只认下面这两个 Protocol，
既不认 Windows，也不认 Android。要让它在别的平台上跑，只要实现这两个协议，
然后注入给 Bot：

    from wxauto.bot import Bot
    from wxauto.platforms.android import AndroidReader, AndroidSender

    reader = AndroidReader("http://127.0.0.1:8765")
    sender = AndroidSender("http://127.0.0.1:8765")
    Bot(cfg, reader=reader, sender=sender).run()

PC 实现见 :class:`wxauto.reader.PcReader` 和 :class:`wxauto.sender.PcSender`。
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from .reader import Snapshot


@runtime_checkable
class Reader(Protocol):
    """读取层契约：回答"群里现在长什么样"。"""

    def attach(self) -> int:
        """连接底层客户端，返回一个平台相关的句柄（拿不到就抛异常）。"""
        ...

    def warmup(self) -> None:
        """预热（PC 上是加载 OCR 模型，手机端可以是空实现）。"""
        ...

    def read(self) -> Optional[Snapshot]:
        """读一帧。

        返回 ``None`` 表示这一帧读不到（PC 上窗口被最小化 / 手机端连接断了），
        调用方会跳过本轮，不会当成"群里没消息"。
        """
        ...

    def in_target_group(self, snap: Snapshot) -> bool:
        """当前打开的会话是不是目标群。不是的话上层什么都不做。"""
        ...


@runtime_checkable
class Sender(Protocol):
    """发送层契约：把一句话发出去。"""

    def send(self, text: str) -> bool:
        """真的发出去。返回是否成功。"""
        ...

    def is_ready(self) -> bool:
        """现在还能不能发（PC 上要复核当前会话；手机端通常是 True）。

        发送前会调用它做一次保护，避免"等待拟人延迟的那几秒里你切走了群"，
        结果把话发错地方。
        """
        ...
