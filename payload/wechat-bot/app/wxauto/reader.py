# -*- coding: utf-8 -*-
"""读取层：截屏 -> OCR -> 结构化消息。

这一层是**平台相关**的。上层（匹配/判断/发送编排）只认 :class:`Snapshot`，
所以换到手机端时只要另写一个 Reader 实现即可。

PC 实现依赖：
* 窗口截图走 ``winapi.grab_window``（后台截图，不抢焦点）
* 文字识别走 RapidOCR（本地离线，中文效果好，热态约 0.6s/帧）
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

try:                       # 手机端（Linux）用不到 PC 读取层，winapi 是 Windows 专有
    from . import winapi
except Exception:          # pragma: no cover
    winapi = None


def _rec_model_path() -> str:
    """识别模型（PP-OCRv5，2026-10-09 起）的路径；没有就返回空串。

    优先 server（更准更慢），其次 mobile —— 跟 read/wxreader.py 同一套规则。

    ⚠️ 这是 read/wxreader.py::rec_model_path 的**同一份逻辑的第二处实现**
      （两个读屏实现分属不同包，没法干净地互相 import）。改这里记得那边一起改。
    """
    env = os.environ.get("WX_OCR_REC", "")
    if env.lower() in ("off", "0", "no"):
        return ""
    if env:
        return env if os.path.exists(env) else ""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("ch_PP-OCRv5_rec_server.onnx", "ch_PP-OCRv5_rec_mobile.onnx"):
        p = os.path.join(base, "models", name)
        if os.path.exists(p):
            return p
    return ""

# ---------------------------------------------------------------- 数据结构


@dataclass
class Block:
    """一段 OCR 文本块。"""
    text: str
    x: int
    y: int
    w: int
    h: int
    score: float = 1.0

    @property
    def left(self) -> int:
        return self.x

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2


@dataclass
class Message:
    """一条消息（可能由多个文本块拼成，比如换行的长消息）。"""
    blocks: List[Block] = field(default_factory=list)
    is_self: bool = False
    # 群聊里的发言人昵称（手机端截图能读出来；PC 端从气泡上方也读得到）。
    # 留空表示不知道 / 单聊 / 自己发的。
    sender: str = ""
    # 消息类型：text / image / pat / system / time（手机端桥接给的 type 原样带过来）。
    # ★ 2026-10-10（识图功能）：机器人要能一眼认出"这是张照片"，而不是只看到
    #   正文里的 `[图片]` 四个字 —— 后者跟关键词规则、打分表混在一起，分不开。
    kind: str = "text"
    # 图片消息在本帧截图里的裁剪文件（容器内的绝对路径）。桥接裁的，只有
    # kind == "image" 且桥接开了 images=1 时才有；为空表示"拿不到这张图"。
    img_path: str = ""
    img_w: int = 0
    img_h: int = 0
    img_md5: str = ""
    # ★ 2026-10-10（识图去重）：裁剪图的**感知哈希**（aHash，16x16）——
    #   指纹优先用它，因为裁剪边界每帧会抖 ±1px，md5 对 1px 零容忍会把
    #   同一张图反复当新图（见 state._img_key）。空 = 老桥接没这个字段。
    img_hash: str = ""

    @property
    def text(self) -> str:
        return tidy(" ".join(b.text for b in self.sorted_blocks()))

    def sorted_blocks(self) -> List[Block]:
        return sorted(self.blocks, key=lambda b: (b.y, b.x))

    @property
    def top(self) -> int:
        return min(b.y for b in self.blocks)

    @property
    def bottom(self) -> int:
        return max(b.bottom for b in self.blocks)

    @property
    def left(self) -> int:
        return min(b.left for b in self.blocks)

    @property
    def right(self) -> int:
        return max(b.right for b in self.blocks)

    @property
    def align_edge(self) -> int:
        """用于判断"同一气泡的下一行"：自己发的看右边缘，别人看左边缘。"""
        return self.right if self.is_self else self.left

    def __repr__(self) -> str:
        who = "我" if self.is_self else "他"
        return f"<{who} y={self.top} {self.text!r}>"


@dataclass
class Snapshot:
    """一帧的完整解读结果。"""
    chat_title: str = ""
    messages: List[Message] = field(default_factory=list)
    width: int = 0
    height: int = 0
    layout: "Layout" = None  # type: ignore
    captured_at: float = field(default_factory=time.time)
    ocr_ms: int = 0

    def tail(self, n: int) -> List[Message]:
        return self.messages[-n:] if n > 0 else []


@dataclass
class Layout:
    """窗口内的区域划分（窗口坐标系，左上角为原点）。"""
    width: int
    height: int
    chat_left: int
    chat_right: int
    chat_top: int
    chat_bottom: int

    @property
    def chat_width(self) -> int:
        return max(1, self.chat_right - self.chat_left)

    @property
    def self_x_threshold(self) -> int:
        """文本块左边缘超过这个 x，就认为是我发的（右对齐气泡）。"""
        return int(self.chat_left + self.chat_width * 0.45)

    @property
    def input_click(self) -> Tuple[int, int]:
        """输入框里一个安全可点的位置。"""
        return (int(self.chat_left + self.chat_width * 0.5), self.height - 68)

    @property
    def send_click(self) -> Tuple[int, int]:
        """发送按钮中心。"""
        return (self.width - 83, self.height - 60)


# ---------------------------------------------------------------- 文本规范化

_WS = re.compile(r"\s+")
# 时间分隔符，例如 08:46 / 昨天 22:58 / 星期四 17:02 / 07.48（OCR 常把冒号认成点）
_TS = re.compile(
    r"^(?:昨天|今天|星期[一二三四五六日天]|[上下]午)?\s*\d{1,2}[:：.。]\d{2}$"
)


def tidy(s: str) -> str:
    """整理成"人能读"的文本：多余空白压成一个空格，保留断句用的空格。

    **要显示或要发出去**的文本用这个。
    """
    return _WS.sub(" ", (s or "").strip())


def normalize(s: str) -> str:
    """删掉所有空白，只用于**比对**（去重指纹、关键词匹配）。

    它会把 "adb shell pm grant" 变成 "adbshellpmgrant"，
    所以千万别拿它的结果去显示或发送。
    """
    return _WS.sub("", s or "")


def is_timestamp(s: str) -> bool:
    return bool(_TS.match(normalize(s)))


# ---------------------------------------------------------------- OCR 引擎


class OcrEngine:
    """RapidOCR 的薄封装（惰性初始化，省启动时间）。"""

    def __init__(self) -> None:
        self._engine = None

    def warmup(self) -> None:
        self._get()

    def _get(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            # ★ 2026-10-09：跟 read/wxreader.py 用同一个识别模型（PP-OCRv5 mobile）。
            #   两份读屏实现（PC 的 PcReader / Android 的 wxreader）必须用同一套，
            #   否则"同一张图在 PC 上读出来、在手机上是另一个样子"，排查会疯。
            p = _rec_model_path()
            self._engine = RapidOCR(**({"rec_model_path": p} if p else {}))
        return self._engine

    def run(self, img: Image.Image) -> Tuple[List[Block], int]:
        t0 = time.time()
        result, _elapse = self._get()(np.asarray(img))
        ms = int((time.time() - t0) * 1000)
        blocks: List[Block] = []
        for box, text, score in (result or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x, y = int(min(xs)), int(min(ys))
            w, h = int(max(xs) - x), int(max(ys) - y)
            if text and text.strip():
                blocks.append(Block(text=text.strip(), x=x, y=y, w=w, h=h,
                                    score=float(score)))
        return blocks, ms


# ---------------------------------------------------------------- 布局探测


def detect_chat_left(img: Image.Image, y0: int, y1: int,
                     fallback: int = 460) -> int:
    """自动找出会话列表与聊天区的分界线。

    依据：会话列表背景约 (238,238,240)，聊天区背景约 (250,250,250)。
    用列中位数而不是均值，这样聊天气泡不会干扰判断。
    """
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    y0, y1 = max(0, min(y0, h - 1)), max(1, min(y1, h))
    if y1 - y0 < 20:
        return min(fallback, w // 2)
    band = arr[y0:y1].astype(np.float32)
    col = np.median(band, axis=(0, 2))  # 每列的中位亮度

    lo, hi = int(w * 0.15), int(w * 0.65)
    need = 246.0
    run = 0
    for x in range(lo, hi):
        if col[x] >= need:
            run += 1
            if run >= 24:          # 连续 24 列都是亮背景，认定进入聊天区
                return x - run + 1
        else:
            run = 0
    return min(fallback, w // 2)


def build_layout(img: Image.Image) -> Layout:
    w, h = img.size
    chat_top = int(h * 0.095)
    chat_bottom = int(h * 0.885)
    chat_left = detect_chat_left(img, int(h * 0.25), int(h * 0.80))
    return Layout(width=w, height=h,
                  chat_left=chat_left, chat_right=w - 20,
                  chat_top=chat_top, chat_bottom=chat_bottom)


# ---------------------------------------------------------------- 消息聚类

# 同一气泡内换行的 y 间距上限
_LINE_GAP = 44
# 左/右边缘对齐容差
_EDGE_TOL = 10


def cluster_messages(blocks: Sequence[Block], layout: Layout) -> List[Message]:
    """把文本块聚合成消息。

    规则：先按 x 判断左右（自己发的右对齐），再要求"下一个块与当前消息的对齐
    边缘接近"且"y 间距不超过一行高"，才归入同一条消息。
    """
    inside = [b for b in blocks
              if b.x >= layout.chat_left
              and layout.chat_top <= b.y
              and b.bottom <= layout.chat_bottom
              and not is_timestamp(b.text)]

    inside.sort(key=lambda b: (b.y, b.x))
    messages: List[Message] = []

    for b in inside:
        is_self = b.left >= layout.self_x_threshold
        placed = False
        # 只回溯最近几条，避免长消息被错误地接到很远的消息上
        for m in reversed(messages[-4:]):
            if m.is_self != is_self:
                continue
            if b.y - m.bottom > _LINE_GAP:
                continue
            if b.y < m.top - _LINE_GAP:
                continue
            if abs(b.left - m.align_edge) <= _EDGE_TOL:
                m.blocks.append(b)
                placed = True
                break
        if not placed:
            m = Message(blocks=[b], is_self=is_self)
            messages.append(m)

    messages.sort(key=lambda m: m.top)
    return messages


# ---------------------------------------------------------------- PC Reader


class PcReader:
    """PC 微信读取层。"""

    def __init__(self, window_title: str = "微信", target_group: str = "",
                 debug_dir: Optional[str] = None,
                 save_frames: bool = False) -> None:
        self.window_title = window_title
        self.target_group = target_group
        # 调试存图是 PC 实现自己的事，不进入 Reader 协议，别的平台不受影响
        self.debug_dir = debug_dir
        self.save_frames = bool(save_frames and debug_dir)
        self.ocr = OcrEngine()
        self.hwnd: Optional[int] = None
        self.last_layout: Optional[Layout] = None
        self._frame = 0

    # ---- 窗口 ----
    def attach(self) -> int:
        hwnd = winapi.find_window(self.window_title)
        if hwnd is None:
            raise RuntimeError(
                f"找不到标题为「{self.window_title}」的窗口，请先打开微信")
        self.hwnd = hwnd
        return hwnd

    def warmup(self) -> None:
        """预加载 OCR 模型（首次要 0.3s 左右，别让它落在第一帧上）。"""
        self.ocr.warmup()

    def ensure_window(self, restore_if_minimized: bool = False) -> Optional[int]:
        """拿到可用的窗口句柄。

        窗口被最小化时 PrintWindow 只能拿到空白，所以默认**不**强行把它弹出来
        —— 那会打断你正在做的事。只有真要发消息时才允许恢复。
        """
        if self.hwnd is None or not winapi.is_alive(self.hwnd):
            try:
                self.attach()
            except RuntimeError:
                return None
        if self.hwnd is None:
            return None
        if winapi.is_minimized(self.hwnd):
            if not restore_if_minimized:
                return None
            winapi.restore(self.hwnd)
        return self.hwnd

    # ---- 读取 ----
    def read(self) -> Optional[Snapshot]:
        """读一帧。签名必须与 drivers.Reader 一致（不带任何可选参数）。"""
        hwnd = self.ensure_window()
        if hwnd is None:
            return None
        img = winapi.grab_window(hwnd)
        if img is None:
            return None

        if self.save_frames and self.debug_dir:
            import os
            self._frame += 1
            os.makedirs(self.debug_dir, exist_ok=True)
            img.save(os.path.join(self.debug_dir, f"frame_{self._frame:05d}.png"))

        blocks, ms = self.ocr.run(img)
        layout = build_layout(img)
        self.last_layout = layout
        messages = cluster_messages(blocks, layout)

        # 标题 = 聊天区顶部那行文字
        title = ""
        title_blocks = [b for b in blocks
                        if b.x >= layout.chat_left and b.y < layout.chat_top]
        if title_blocks:
            title = " ".join(b.text for b in sorted(title_blocks, key=lambda b: b.x))

        return Snapshot(chat_title=normalize(title), messages=messages,
                        width=layout.width, height=layout.height,
                        layout=layout, ocr_ms=ms)

    def in_target_group(self, snap: Snapshot) -> bool:
        if not self.target_group:
            return True
        return normalize(self.target_group) in snap.chat_title
