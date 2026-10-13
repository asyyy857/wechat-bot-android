# -*- coding: utf-8 -*-
"""Windows 底层能力：窗口定位 / 后台截图 / 模拟输入。

设计要点
--------
* 截图走 ``PrintWindow(PW_RENDERFULLCONTENT)``：微信窗口被别的窗口挡住也能
  读到内容，**不会抢用户焦点**，所以轮询基本无感。
* 输入走 ``SendInput`` + ``KEYEVENTF_UNICODE``：直接把 Unicode 码元灌进去，
  不经过输入法，也不污染剪贴板。
* 只有"真正要发消息"的那一下才短暂激活微信窗口。
* 微信 4.x 是 Qt 自绘窗口（类名 ``Qt51514QWindowIcon``），UI Automation 读不到
  任何文本，所以只能走视觉路线 —— 这是实测结论，不是偷懒。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import time
from typing import List, Optional, Tuple

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量
SW_RESTORE = 9
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SM_CXSCREEN = 0
SM_CYSCREEN = 1
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D


# ---------------------------------------------------------------- 结构体
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_dpi_ready = False


def ensure_dpi_aware() -> None:
    """让进程感知 DPI，否则坐标会按缩放比例错位。"""
    global _dpi_ready
    if not _dpi_ready:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
        _dpi_ready = True


ensure_dpi_aware()


# ---------------------------------------------------------------- 窗口
def find_window(title: str) -> Optional[int]:
    """按标题精确匹配顶层可见窗口，返回 hwnd。"""
    found: List[int] = []

    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return None
    # 优先返回面积最大的那个
    return max(found, key=lambda h: _area(h))


def _area(hwnd: int) -> int:
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)


def get_rect(hwnd: int) -> Tuple[int, int, int, int]:
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return rc.left, rc.top, rc.right, rc.bottom


def get_size(hwnd: int) -> Tuple[int, int]:
    l, t, r, b = get_rect(hwnd)
    return r - l, b - t


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_alive(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def restore(hwnd: int) -> None:
    """从最小化恢复（不抢焦点）。"""
    if is_minimized(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.35)


def activate(hwnd: int) -> bool:
    """把窗口切到前台，返回是否成功。

    ``SetForegroundWindow`` 受前台锁定限制，所以这里用 AttachThreadInput
    把当前线程和前台线程的输入队列临时挂在一起。
    """
    restore(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True

    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    for tid in {fg_tid, tgt_tid}:
        if tid and user32.AttachThreadInput(cur_tid, tid, True):
            attached.append(tid)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)

    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


def get_foreground() -> int:
    return user32.GetForegroundWindow()


# ---------------------------------------------------------------- 截图
def grab_window(hwnd: int) -> Optional[Image.Image]:
    """后台抓取窗口内容（窗口被遮挡也可以）。"""
    w, h = get_size(hwnd)
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    old = gdi32.SelectObject(mfc_dc, bmp)
    try:
        # PW_RENDERFULLCONTENT = 2
        if not user32.PrintWindow(hwnd, mfc_dc, 2):
            return None
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h  # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bi), 0) == 0:
            return None
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.SelectObject(mfc_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def grab_screen_region(left: int, top: int, right: int, bottom: int) -> Image.Image:
    """从屏幕抓取区域（需要窗口在前台时用）。"""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)


# ---------------------------------------------------------------- 输入
def _send(inputs: List[INPUT]) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse(dx: int, dy: int, flags: int) -> INPUT:
    return INPUT(type=INPUT_MOUSE, union=_INPUTUNION(
        mi=_MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=None)))


def click(x: int, y: int, settle: float = 0.06) -> None:
    """在屏幕绝对坐标 (x, y) 点击一次。"""
    sw = user32.GetSystemMetrics(SM_CXSCREEN)
    sh = user32.GetSystemMetrics(SM_CYSCREEN)
    ax = int(x * 65535 / max(1, sw - 1))
    ay = int(y * 65535 / max(1, sh - 1))
    _send([_mouse(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])
    time.sleep(settle)
    _send([_mouse(ax, ay, MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE)])
    time.sleep(settle)
    _send([_mouse(ax, ay, MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE)])


def _key_event(vk: int, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(
        ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)))


def _unicode_event(code: int, keyup: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
    return INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(
        ki=_KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=None)))


def type_text(text: str, per_char: float = 0.012) -> None:
    """逐 UTF-16 码元灌入文本，绕过输入法。"""
    for ch in text:
        # BMP 外的字符（emoji 等）会被编码成代理对，这里按码元逐个发送
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            code = raw[i] | (raw[i + 1] << 8)
            _send([_unicode_event(code, False), _unicode_event(code, True)])
        time.sleep(per_char)


def send_enter() -> None:
    _send([_key_event(VK_RETURN), _key_event(VK_RETURN, KEYEVENTF_KEYUP)])


def send_ctrl_v() -> None:
    _send([_key_event(VK_CONTROL), _key_event(VK_V),
           _key_event(VK_V, KEYEVENTF_KEYUP), _key_event(VK_CONTROL, KEYEVENTF_KEYUP)])


def set_clipboard_text(text: str) -> bool:
    """用 tkinter 写剪贴板（标准库，不依赖 pywin32）。"""
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 剪贴板图片
# 发图走的是"图片进剪贴板 -> 微信里 Ctrl+V -> 点发送"这条路，
# 实测可行（见 README 的表情包章节）。不用碰微信那些加密的表情包文件。
CF_DIB = 8
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
user32.IsClipboardFormatAvailable.restype = wt.BOOL
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]


def pil_to_dib(image: Image.Image) -> bytes:
    """PIL 图 -> CF_DIB 字节流（BITMAPINFOHEADER + 自下而上的 24 位 BGR）。"""
    image = image.convert("RGB")
    w, h = image.size
    header = struct.pack("<IiiHHIIiiII",
                         40, w, h,      # biSize, 宽, 正数高度 = 自下而上
                         1, 24,         # planes, 24 位
                         0, 0, 0, 0, 0)  # BI_RGB 不压缩 + 后面几个 0
    row_size = ((w * 3 + 3) // 4) * 4      # 每行按 4 字节对齐
    px = image.load()
    body = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b = px[x, y]
            row += bytes((b, g, r))
        row += b"\x00" * (row_size - len(row))
        body += row
    return header + bytes(body)


def set_clipboard_image(image: Image.Image) -> bool:
    """把图片放进剪贴板（CF_DIB 格式，微信认这个）。"""
    try:
        data = pil_to_dib(image)
    except Exception:
        return False

    if not user32.OpenClipboard(None):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        # 数据交给剪贴板之后，内存所有权归系统，不能自己再 free
        if not user32.SetClipboardData(CF_DIB, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


def set_clipboard_image_file(path) -> bool:
    """读一个图片文件放进剪贴板。

    动图（GIF/WebP）在这里只会取第一帧 —— CF_DIB 是静态位图。
    发真正的动图要另想办法（目前没做）。
    """
    try:
        with Image.open(path) as im:
            return set_clipboard_image(im)
    except Exception:
        return False


def clipboard_has_image() -> bool:
    """剪贴板里现在有没有位图（自检用）。"""
    return bool(user32.IsClipboardFormatAvailable(CF_DIB))
