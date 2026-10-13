#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rike-ocr-fix.py —— 用【游戏字体的字形先验】纠正 OCR 的形近误读。

为什么需要它：RapidOCR 对普通文字很准，但对游戏自绘 UI 的**形近字**会稳定读错，
而且错得有规律 ——
    「已领」                → 巴领 / 己领 / 已邻
    「点击右侧图标定位」    → 志击丰侧图标定位
这不是随机噪声，是**字形相似**造成的。所以拿游戏正文字体
（fonts/plugin_font.ttf = 游戏目录里抠出来的 PingFang SC 本体）把候选词
渲染成字形，用量化相似度把观测串"吸附"回词表里最像的那个词。

为什么不是"渲染模板做像素匹配"：实测过，**不行** —— 游戏文字带描边/渐变/纹理背景，
干净渲染的模板与真实帧的归一化相关分中位数只有 0.286（≥0.6 的仅 5/47）。
字形相似度（二值化后比 IoU）才是对描边/颜色/背景都鲁棒的那个量。

用法:
  sh tools/rike-ocr.sh --tsv 帧.jpg | python3 tools/rike-ocr-fix.py --tsv   # 保坐标，只改文字列
  sh tools/rike-ocr.sh 帧.jpg | python3 tools/rike-ocr-fix.py               # 纯文本行
  python3 tools/rike-ocr-fix.py --text "巴领"                               # 单条试
可选:
  --min F       采纳阈值（OCR 置信度高时），默认 0.80
  --min-low F   OCR 置信度低于 --ocr-low 时放宽到的阈值，默认 0.62
  --ocr-low F   "低置信"的分界，默认 0.90
  --gap F       第一名要比第二名高出多少才敢改，默认 0.03
  --only-changed 只输出改动过的行（默认全量输出，行数与输入一致）
"""
import argparse
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFont

DSH = os.environ.get("DSH_ROOT", "/sdcard/dsh")
FONT = DSH + "/fonts/plugin_font.ttf"
WORDLIST = DSH + "/tools/rike-wordlist.txt"
CELL = 64          # 字形归一化到 64x64
BIN = 110          # 二值化阈值

_cache = {}


def _binarize(im):
    return im.point(lambda v: 255 if v > BIN else 0).convert("1")


def glyph(ch):
    """字 → 归一化二值字形（'1' 模式）。带缓存。"""
    if ch in _cache:
        return _cache[ch]
    try:
        f = ImageFont.truetype(FONT, 96)
        b = f.getbbox(ch)
        if b[2] - b[0] <= 0 or b[3] - b[1] <= 0:
            raise ValueError
        im = Image.new("L", (b[2] - b[0] + 16, b[3] - b[1] + 16), 0)
        ImageDraw.Draw(im).text((8 - b[0], 8 - b[1]), ch, font=f, fill=255)
        g = _binarize(im.resize((CELL, CELL), Image.LANCZOS))
    except Exception:
        g = _binarize(Image.new("L", (CELL, CELL), 0))
    _cache[ch] = g
    return g


def _area(im):
    return im.convert("L").histogram()[255]


def iou(a, b):
    u = _area(ImageChops.logical_or(a, b))
    return _area(ImageChops.logical_and(a, b)) / u if u else 0.0


def sim(obs, word):
    """观测串 vs 词表词 的字形相似度（0~1，长度差有惩罚）。"""
    if not obs or not word:
        return 0.0
    n = max(len(obs), len(word))
    s = sum(iou(glyph(o), glyph(w)) for o, w in zip(obs, word)) / n
    return s - 0.12 * abs(len(obs) - len(word))


def load_words():
    out = []
    with open(WORDLIST, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
    return out


def fix(text, words, wordset, ocr_score=1.0, min_=0.72, min_low=0.62,
        ocr_low=0.85, gap=0.03):
    t = text.strip()
    if not t:
        return t, 1.0, False
    if t in wordset:                      # 本来就在词表里 → 一个字都不动
        return t, 1.0, False
    top = sorted(((sim(t, w), w) for w in words), reverse=True)[:2]
    if not top:
        return t, 0.0, False
    (best_s, best_w) = top[0]
    second = top[1][0] if len(top) > 1 else 0.0
    th = min_low if ocr_score < ocr_low else min_
    if best_s >= th and (best_s - second) >= gap:
        return best_w, best_s, True
    return t, best_s, False


def main():
    ap = argparse.ArgumentParser(description="字形先验 OCR 纠错")
    ap.add_argument("--tsv", action="store_true", help="输入是 rike-ocr.sh --tsv 格式（保留坐标）")
    ap.add_argument("--text", default=None, help="直接纠一条")
    ap.add_argument("--min", type=float, default=0.80)
    ap.add_argument("--min-low", type=float, default=0.62)
    ap.add_argument("--ocr-low", type=float, default=0.90)
    ap.add_argument("--gap", type=float, default=0.03)
    ap.add_argument("--only-changed", action="store_true",
                    help="只输出被改动的行；默认全量输出（行数与输入一致）")
    a = ap.parse_args()

    words = load_words()
    wordset = set(words)

    if a.text is not None:
        out, sc, ch = fix(a.text, words, wordset, 1.0, a.min, a.min_low, a.ocr_low, a.gap)
        print(f"{a.text}\t→\t{out}\t{sc:.3f}{'  ✏️' if ch else ''}")
        return

    n = k = 0
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        raw = parts[0]
        score = 1.0
        if a.tsv and len(parts) >= 6:
            try:
                score = float(parts[5])
            except ValueError:
                score = 1.0
        out, sc, changed = fix(raw, words, wordset, score, a.min, a.min_low,
                               a.ocr_low, a.gap)
        n += 1
        if changed:
            k += 1
            sys.stderr.write(f'  ✏️ "{raw}"(ocr {score:.2f}) → "{out}" (字形 {sc:.3f})\n')
        if a.only_changed and not changed:
            continue
        if a.tsv and len(parts) >= 6:
            parts[0] = out
            print("\t".join(parts))
        else:
            print(out)
    sys.stderr.write(f"共 {n} 行，纠正 {k} 行\n")


if __name__ == "__main__":
    main()
