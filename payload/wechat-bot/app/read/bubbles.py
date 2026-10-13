#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""气泡检测原型：从微信聊天截图里切出每条消息的 bbox，并判断左右（is_self）。"""
import sys
import numpy as np
from PIL import Image


def load(path):
    return np.array(Image.open(path).convert("RGB")).astype(np.int16)


def masks(a):
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    green = (g > 180) & (g - b > 40) & (g - r > 25)      # 自己的绿气泡
    white = (r > 248) & (g > 248) & (b > 248)            # 别人的白气泡
    return green, white


def row_blocks(mask, min_h=8, gap=4, min_px=3):
    rows = mask.sum(axis=1)
    ys = np.where(rows >= min_px)[0]
    if len(ys) == 0:
        return []
    blocks, start, prev = [], ys[0], ys[0]
    for y in ys[1:]:
        if y - prev > gap:
            if prev - start + 1 >= min_h:
                blocks.append((start, prev))
            start = y
        prev = y
    if prev - start + 1 >= min_h:
        blocks.append((start, prev))
    return blocks


def bbox_of(mask, y0, y1):
    sub = mask[y0:y1 + 1]
    cols = np.where(sub.any(axis=0))[0]
    return (int(cols[0]), int(y0), int(cols[-1]), int(y1)) if len(cols) else None


def analyze(path, ytop=70, ybot=1290):
    a = load(path)
    green, white = masks(a)
    green[:ytop] = False
    green[ybot:] = False
    white[:ytop] = False
    white[ybot:] = False
    out = []
    for kind, m in (("self", green), ("other", white)):
        for (y0, y1) in row_blocks(m):
            bb = bbox_of(m, y0, y1)
            if bb:
                out.append((kind,) + bb)
    out.sort(key=lambda t: t[2])
    return out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "base.jpg"
    items = analyze(path)
    print("共 %d 个气泡块（图 %s）" % (len(items), path))
    for kind, x0, y0, x1, y1 in items:
        print("  %-5s x=%3d..%3d y=%4d..%4d  w=%3d h=%3d" %
              (kind, x0, x1, y0, y1, x1 - x0 + 1, y1 - y0 + 1))
