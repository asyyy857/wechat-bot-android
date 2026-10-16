# -*- coding: utf-8 -*-
"""图片指纹（aHash）—— 判断"屏幕上这张图是不是机器人自己刚发的那张"。

★ 2026-10-15（用户报「我让它读张三的图片，它却读了自己的表情包」）：
    机器人和主人**共用一个微信号**，读屏层偶尔会把机器人自己发的图片气泡判成
    "别人发的"（图片气泡和文字气泡挨着时会串行 —— 交接说明里记过这个坑）。
    于是"被叫看图"时，屏幕上离得最近的那张"非自己"的图，很可能就是机器人刚发的
    表情包：它对着自己的图点评，用户真正要读的那张没人看。

    文本消息有 ``_mine_said``（"我说过什么"）兜着，图片没有可比的文本 —— 改用指纹：
    发图成功时记下**原图**的 aHash，读屏拿到候选图时比一比，够近就当自己的图跳过。

★ 算法必须与桥接 ``wxbridge._img_ahash`` **逐字一致**（裁边 10% → 灰度 → 8x8 →
   均值哈希），否则"我发出去的原图"和"屏幕上裁下来的那张"根本比不到一起。
  实测（2026-10-15，真机裁剪图 vs 163 张表情包原图）：同一张表情包距离 **12**，
  下一名 16；而不同图的 1% 分位是 15、中位 31 —— 阈值 12 是踩着实测定下来的。
"""
from __future__ import annotations

import os

IMG_SAME_MAX_DIST = 12      # 汉明距离 ≤ 它 = "同一张图"（与 state.IMG_HASH_MAX_DIST 同值）
_AHASH_SIDE = 8             # 8x8 = 64 位
_AHASH_TRIM = 0.10          # 缩略前裁掉四周 10%（裁剪边界的抖动多半落在那圈里）


def _pixels(im):
    """整张图的像素序列。Pillow 新版把 getdata 换成 get_flattened_data，两个都兜。"""
    f = getattr(im, "get_flattened_data", None)
    return f() if callable(f) else im.getdata()


def ahash_int(source) -> int:
    """算一张图的 aHash（64 位整数）。路径 / 已打开的 PIL.Image 都行；失败 → -1。"""
    try:
        if isinstance(source, (str, bytes, os.PathLike)):
            from PIL import Image
            im = Image.open(source)
            try:
                return _ahash(im)
            finally:
                im.close()
        return _ahash(source)
    except Exception:
        return -1


def _ahash(im) -> int:
    from PIL import Image
    w, h = im.size
    tx = max(1, int(w * _AHASH_TRIM))
    ty = max(1, int(h * _AHASH_TRIM))
    if w - 2 * tx >= 8 and h - 2 * ty >= 8:
        im = im.crop((tx, ty, w - tx, h - ty))
    g = im.convert("L").resize((_AHASH_SIDE, _AHASH_SIDE), Image.LANCZOS)
    px = list(_pixels(g))
    avg = sum(px) / float(len(px) or 1)
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    """两个 aHash 的汉明距离；任一是 -1（算不出来）→ 999。"""
    if a is None or b is None or a < 0 or b < 0:
        return 999
    return bin(int(a) ^ int(b)).count("1")


def same_image(a: int, b: int, max_dist: int = IMG_SAME_MAX_DIST) -> bool:
    return hamming(a, b) <= max_dist


def hash_from_hex(h: str) -> int:
    """桥接给的 16 位十六进制 aHash → int；拿不到 → -1。"""
    try:
        return int(str(h or "").strip(), 16)
    except (TypeError, ValueError):
        return -1
