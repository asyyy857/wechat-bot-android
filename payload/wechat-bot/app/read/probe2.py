#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针2：昵称行 vs 正文的颜色/位置特征。"""
import sys
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

img = sys.argv[1] if len(sys.argv) > 1 else "/tmp/g1.jpg"
a = np.array(Image.open(img).convert("RGB")).astype(np.int16)
# ★ 2026-10-09：跟主读屏（wxreader）用同一个识别模型 —— 探针是拿来对照
#   主路径解析结果的，两边模型不一样就白对了。
from wxreader import rec_model_path as _rec_p
_p = _rec_p()
ocr = RapidOCR(**({'rec_model_path': _p} if _p else {}))
res, _ = ocr(np.array(Image.open(img).convert("RGB")))
print("%-28s %-16s %-22s %s" % ("文本", "bbox", "暗色像素占比/均值", "索引"))
for box, txt, score in res or []:
    ys = [int(p[1]) for p in box]; xs = [int(p[0]) for p in box]
    y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
    sub = a[y0:y1 + 1, x0:x1 + 1]
    lum = sub.mean(axis=2)
    dark = lum < 150
    n = lum.size
    dk = dark.sum()
    mean_dark = lum[dark].mean() if dk else -1
    print("%-28s x%4d-%4d y%4d-%4d  dark%%=%.2f meanDark=%6.1f" % (txt[:26], x0, x1, y0, y1, dk / n, mean_dark))
