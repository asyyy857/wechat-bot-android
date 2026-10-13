#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr-bench.py —— RapidOCR 参数对照实验（离线，拿历史帧跑）

目的：找出「小字游戏帧」上召回最高的底层配置，再把参数固化进 ocr-daemon.py。

用法: PYTHONPATH=/root/ocrlibs python3 tools/ocr-bench.py [变体名...]
     不给变体名就跑全部；结果落 /tmp/ocr-bench/<变体>.txt，汇总表打到 stdout。
"""
import os, sys, time, json, statistics
sys.path.insert(0, '/root/ocrlibs')
import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

FIX = '/sdcard/dsh/tools/rike-fixtures'
OUT = '/tmp/ocr-bench'
os.makedirs(OUT, exist_ok=True)

# 每帧「应该读出」的词（人工按 selftest 的期望值定；召回率＝命中数/应命中数）
COMMON = ['青崖书', '殊途同归', '日课点', '剧组', '庄园', '好感度', '游历', '朋友圈']
EXPECT = {n: list(COMMON) for n in ['r1', 'j8', 'cb6', 'y6', 'p9', 'z7', 'c1']}
EXPECT['v7'] = ['匆匆']
FRAMES = list(EXPECT)

# ── 变体定义：名字 → (初始化 kwargs, 预处理) ──────────────────
# 预处理: None | ('x', 倍数) 先高质量放大
VARIANTS = {
    'a-tess':      None,                                   # 占位：tesseract 基线（脚本外做）
    'b-base':      ({}, None),
    'c-nocls':     ({'use_cls': False}, None),
    'd-lim1024':   ({'limit_side_len': 1024}, None),
    'e-lim1280':   ({'limit_side_len': 1280}, None),
    'f-lim1536':   ({'limit_side_len': 1536}, None),
    'g-2x':        ({'max_side_len': 3200, 'min_side_len': 60}, ('x', 2)),
    'h-2x-lim':    ({'max_side_len': 3200, 'min_side_len': 60, 'limit_side_len': 1536}, ('x', 2)),
    'i-lowthr':    ({'limit_side_len': 1280, 'text_score': 0.3, 'box_thresh': 0.3}, None),
    'j-thr4':      ({'limit_side_len': 1280, 'intra_op_num_threads': 4}, None),
    'k-15x-lim':   ({'max_side_len': 3200, 'min_side_len': 60, 'limit_side_len': 1280}, ('x', 1.5)),
}


def run(name, kw, prep, frames):
    eng = RapidOCR(**kw)
    texts = {}
    times = []
    eng(os.path.join(FIX, 'r1.jpg'))          # 预热，不计时
    for n in frames:
        img = cv2.imread(os.path.join(FIX, n + '.jpg'))
        if prep:
            f = prep[1]
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_LANCZOS4)
        t0 = time.time()
        res, _ = eng(img)
        dt = time.time() - t0
        times.append(dt)
        texts[n] = '\n'.join(r[1] for r in res) if res else ''
    hit = tot = 0
    for n in frames:
        for w in EXPECT[n]:
            tot += 1
            if w in texts[n]:
                hit += 1
    with open(os.path.join(OUT, name + '.txt'), 'w') as f:
        for n in frames:
            f.write('=== %s ===\n%s\n' % (n, texts[n]))
    return {'name': name, 'hit': hit, 'tot': tot, 'rate': hit / tot,
            'avg': statistics.mean(times), 'max': max(times), 'times': [round(t, 2) for t in times]}


if __name__ == '__main__':
    want = sys.argv[1:]
    rows = []
    for name, spec in VARIANTS.items():
        if spec is None or (want and name not in want):
            continue
        kw, prep = spec
        try:
            r = run(name, dict(kw), prep, FRAMES)
        except Exception as e:
            print('!! %s 失败: %s' % (name, e)); continue
        rows.append(r)
        print('  %-12s 召回 %2d/%2d = %5.1f%%  平均 %5.2fs  最慢 %5.2fs  %s'
              % (r['name'], r['hit'], r['tot'], r['rate'] * 100, r['avg'], r['max'], r['times']), flush=True)
    rows.sort(key=lambda r: (-r['rate'], r['avg']))
    print('\n── 排名（先召回后速度）──')
    for r in rows:
        print('  %-12s %5.1f%%  %5.2fs' % (r['name'], r['rate'] * 100, r['avg']))
