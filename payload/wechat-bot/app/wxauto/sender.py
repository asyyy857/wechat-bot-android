# -*- coding: utf-8 -*-
"""发送层：把一句话真的打进微信群。

PC 实现要点
----------
* 只有在真正要发消息时才激活微信窗口，其余时间不动你的焦点。
* 文本用 SendInput 直接灌 Unicode，不经过输入法、不占用剪贴板。
* 发送前会复核一次"当前会话是不是目标群"，防止串台。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Tuple

try:                       # 同上：手机端不发 PC 消息
    from . import winapi
except Exception:          # pragma: no cover
    winapi = None
from .reader import PcReader, Snapshot, build_layout, normalize


class PcSender:
    def __init__(self, reader: PcReader, cfg, logger=None) -> None:
        self.reader = reader
        self.cfg = cfg
        self.behavior = cfg.behavior
        self.group = str(getattr(cfg.target, "group", "") or "")
        self.logger = logger
        # 图片粘进输入框后要等它渲染出来，才点得到发送按钮。实测 1.5s 稳。
        self.image_settle = float(
            getattr(getattr(cfg, "stickers", None), "settle", 1.6))

    # ------------------------------------------------------------ 内部
    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)

    def _origin(self) -> Tuple[int, int]:
        hwnd = self.reader.ensure_window(restore_if_minimized=True)
        if hwnd is None:
            raise RuntimeError("微信窗口不可用")
        left, top, _r, _b = winapi.get_rect(hwnd)
        return left, top

    def _layout(self):
        lay = getattr(self.reader, "last_layout", None)
        if lay is not None:
            return lay
        hwnd = self.reader.ensure_window(restore_if_minimized=True)
        if hwnd is None:
            raise RuntimeError("微信窗口不可用")
        img = winapi.grab_window(hwnd)
        if img is None:
            raise RuntimeError("无法截取微信窗口")
        return build_layout(img)

    def _current_group_ok(self) -> bool:
        snap = self.reader.read()
        return bool(snap and self.reader.in_target_group(snap))

    # 公开名字（drivers.Sender 协议要求）；保留旧名兼容
    def is_ready(self) -> bool:
        """现在还在目标群里吗。发送前用来兜底。"""
        return self._current_group_ok()

    def _switch_to_group(self) -> bool:
        """在会话列表里找到目标群并点开。"""
        if not self.group:
            return True
        hwnd = self.reader.ensure_window(restore_if_minimized=True)
        img = winapi.grab_window(hwnd)
        if img is None:
            return False
        blocks, _ms = self.reader.ocr.run(img)
        layout = build_layout(img)

        want = normalize(self.group)
        hits = [b for b in blocks
                if b.x < layout.chat_left
                and want[:6] in normalize(b.text)]     # OCR 可能截断群名，取前缀匹配
        if not hits:
            self._log(f"会话列表里没找到「{self.group}」，放弃发送", "warning")
            return False

        target = min(hits, key=lambda b: b.y)          # 会话列表按时间倒序，取最上面那个
        ox, oy = self._origin()
        x = ox + max(120, layout.chat_left // 2)
        y = oy + target.y + target.h // 2
        self._log(f"切换到群「{self.group}」({x},{y})")
        winapi.click(x, y)
        time.sleep(0.7)
        return True

    # ------------------------------------------------------------ 对外
    def send(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False

        hwnd = self.reader.ensure_window(restore_if_minimized=True)
        prev_fg = winapi.get_foreground()

        if not winapi.activate(hwnd):
            self._log("微信窗口未能切到前台，本次发送跳过", "warning")
            return False

        try:
            if not self._current_group_ok():
                if not self._switch_to_group():
                    return False
                if not self._current_group_ok():
                    self._log("切换后仍不在目标群，放弃发送", "warning")
                    return False

            layout = self._layout()
            ox, oy = self._origin()

            # 点输入框
            ix, iy = layout.input_click
            winapi.click(ox + ix, oy + iy)
            time.sleep(0.12)

            # 灌文本
            winapi.type_text(text)
            time.sleep(0.12)

            # 发送
            if str(getattr(self.behavior, "send_key", "button")).lower() == "enter":
                winapi.send_enter()
            else:
                sx, sy = layout.send_click
                winapi.click(ox + sx, oy + sy)

            time.sleep(0.45)
            self._log(f"已发送：{text}")
            return True
        finally:
            if bool(getattr(self.behavior, "restore_focus", True)) \
                    and prev_fg and prev_fg != hwnd and winapi.is_alive(prev_fg):
                winapi.activate(prev_fg)

    def send_image(self, path) -> bool:
        """发一张图片/表情包。

        走的是：图片塞进剪贴板 -> 微信输入框 Ctrl+V -> 点发送按钮。
        和发文字不同，这里**必须**点按钮 —— 图片粘进去后按回车不一定会发。
        """
        p = Path(path)
        if not p.is_file():
            self._log(f"找不到图片文件：{p}", "warning")
            return False

        # 先把图放进剪贴板。这一步失败就别去动微信窗口了。
        if not winapi.set_clipboard_image_file(p):
            self._log(f"图片读不进来（格式不支持或文件坏了）：{p.name}", "warning")
            return False

        hwnd = self.reader.ensure_window(restore_if_minimized=True)
        prev_fg = winapi.get_foreground()

        if not winapi.activate(hwnd):
            self._log("微信窗口未能切到前台，本次发图跳过", "warning")
            return False

        try:
            if not self._current_group_ok():
                if not self._switch_to_group():
                    return False
                if not self._current_group_ok():
                    self._log("切换后仍不在目标群，放弃发图", "warning")
                    return False

            layout = self._layout()
            ox, oy = self._origin()

            ix, iy = layout.input_click
            winapi.click(ox + ix, oy + iy)
            time.sleep(0.15)

            winapi.send_ctrl_v()
            time.sleep(self.image_settle)      # 等缩略图渲染出来

            sx, sy = layout.send_click
            winapi.click(ox + sx, oy + sy)

            time.sleep(0.5)
            self._log(f"已发送表情包：{p.name}")
            return True
        finally:
            if bool(getattr(self.behavior, "restore_focus", True)) \
                    and prev_fg and prev_fg != hwnd and winapi.is_alive(prev_fg):
                winapi.activate(prev_fg)
