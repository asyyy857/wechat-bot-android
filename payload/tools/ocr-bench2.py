#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr-bench2.py —— 专项：卡片行小字（rike-cards.cjs 的痛点）

rike-cards.cjs 现在 = convert 裁 900x50 → 放大 400% → tesseract psm7。
本脚本拿 7 张历史帧，对比「tesseract 现状」与「RapidOCR 各种 ROI 吃法」的
**5 个卡名召回率** 和 耗时。结论用来改造 rike-cards.cjs。
"""
import os, sys, time, subprocess, statistics, gc
sys.path.insert(0, '/root/ocrlibs')
import cv2
from rapidocr_onnxruntime import RapidOCR

FIX = '/sdcard/dsh/tools/rike-fixtures'
CARDS = ['剧组', '庄园', '好感度', '游历', '朋友圈']
FRAMES = ['r1', 'j8', 'cb6', 'y6', 'p9', 'z7', 'c1']
ROI = (460, 358, 900, 50)          # x,y,w,h  (rike-cards.cjs 的标题行)


def crop(n, roi=ROI, scale=1.0):
    img = cv2.imread(os.path.join(FIX, n + '.jpg'))
    x, y, w, h = roi
    c = img[y:y + h, x:x + w]
    if scale != 1.0:
        c = cv2.resize(c, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    return c


def score(text):
    return sum(1 for c in CARDS if c in text)


def tess(n):
    p = os.path.join(FIX, n + '.jpg')
    subprocess.run(['convert', p, '-crop', '%dx%d+%d+%d' % (ROI[2], ROI[3], ROI[0], ROI[1]),
                    '+repage', '-resize', '400%', '/tmp/_b2.png'], check=False, timeout=30)
    t0 = time.time()
    out = subprocess.run(['tesseract', '/tmp/_b2.png', 'stdout', '-l', 'chi_sim', '--psm', '7'],
                         capture_output=True, text=True, timeout=60).stdout
    return out, time.time() - t0


def run_rapid(name, kw, scale):
    eng = RapidOCR(**kw)
    eng(crop('r1', scale=scale))       # 预热
    hits = 0; times = []; lines = []
    for n in FRAMES:
        t0 = time.time()
        res, _ = eng(crop(n, scale=scale))
        dt = time.time() - t0
        times.append(dt)
        txt = '|'.join(r[1] for r in res) if res else ''
        lines.append('%s: %s' % (n, txt))
        hits += score(txt)
    del eng; gc.collect()
    return hits, times, lines


if __name__ == '__main__':
    print('── tesseract 现状（裁 900x50 → 400% → psm7）──', flush=True)
    th = 0; tt = []
    for n in FRAMES:
        out, dt = tess(n)
        s = score(out)
        th += s; tt.append(dt)
        print('  %-4s %d/5  %.2fs  %s' % (n, s, dt, out.replace('\n', ' ')[:70]), flush=True)
    print('  → 合计 %d/%d  平均 %.2fs\n' % (th, len(FRAMES) * 5, statistics.mean(tt)), flush=True)

    variants = [
        ('rapid-roi-plain', {}, 1.0),
        ('rapid-roi-2x', {}, 2.0),
        ('rapid-roi-3x', {}, 3.0),
        ('rapid-roi-3x-lim1440', {'limit_side_len': 1440, 'max_side_len': 4000, 'min_side_len': 20}, 3.0),
        ('rapid-roi-4x-lim1920', {'limit_side_len': 1920, 'max_side_len': 6000, 'min_side_len': 20}, 4.0),
    ]
    for name, kw, sc in variants:
        h, ts, lines = run_rapid(name, dict(kw), sc)
        print('  %-24s %2d/%d  平均 %.2fs' % (name, h, len(FRAMES) * 5, statistics.mean(ts)), flush=True)
        for l in lines:
            print('        ' + l[:110], flush=True)
        print(flush=True)
