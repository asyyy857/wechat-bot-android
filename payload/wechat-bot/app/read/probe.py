#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：看 OCR box 内的颜色分布能否可靠区分自己/别人。"""
import sys
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

img = sys.argv[1] if len(sys.argv) > 1 else "/tmp/base.jpg"
a = np.array(Image.open(img).convert("RGB")).astype(np.int16)
r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
green = (g > 180) & (g - b > 40) & (g - r > 25)
white = (r > 248) & (g > 248) & (b > 248)
dark = (r < 120) & (g < 120) & (b < 120)

# ★ 2026-10-09：跟主读屏（wxreader）用同一个识别模型 —— 探针是拿来对照
#   主路径解析结果的，两边模型不一样就白对了。
from wxreader import rec_model_path as _rec_p
_p = _rec_p()
ocr = RapidOCR(**({'rec_model_path': _p} if _p else {}))
res, _ = ocr(img)
for box, txt, score in res or []:
    ys = [int(p[1]) for p in box]; xs = [int(p[0]) for p in box]
    y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
    sub_px = (y1 - y0 + 1) * (x1 - x0 + 1)
    gr = green[y0:y1 + 1, x0:x1 + 1].sum()
    wh = white[y0:y1 + 1, x0:x1 + 1].sum()
    dk = dark[y0:y1 + 1, x0:x1 + 1].sum()
    # 扩展框：左右各 25px
    ex0, ex1 = max(0, x0 - 25), min(a.shape[1] - 1, x1 + 25)
    gr2 = green[y0:y1 + 1, ex0:ex1 + 1].sum()
    wh2 = white[y0:y1 + 1, ex0:ex1 + 1].sum()
    n2 = (y1 - y0 + 1) * (ex1 - ex0 + 1)
    print("y=%4d..%4d x=%4d..%4d | box gr=%.2f wh=%.2f dark=%.2f | ext gr=%.2f wh=%.2f | %s"
          % (y0, y1, x0, x1, gr / sub_px, wh / sub_px, dk / sub_px, gr2 / n2, wh2 / n2, txt))
