#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rike-wordlist-scan.py —— 扫真实帧，找**词表缺口**（★2026-10-13 建）。

为什么要它：rike-ocr-fix.py 的纠错能力上限 = 词表覆盖度。
    · 词表里**缺**一个真实文案 → 它读错时没人能纠（漏纠）；
    · 词表里**混进**脏词       → 它会把正确文本吸过去（误纠）。
所以词表要"缺什么补什么"，而补的依据必须是**真实帧统计**，不是拍脑袋。

判据：真实 UI 文案的特征是**跨帧稳定重复 + 置信度高 + 字形上远离词表**
      （形近乱码则相反：每次读法都不一样、且离某个词表词很近）。

用法:
  python3 tools/rike-wordlist-scan.py [帧目录或文件 ...]      # 默认扫下列常用目录
输出三段（人工过一遍再往 rike-wordlist.txt 里加）：
  A = 疑似真实缺词（≥2 帧重复、平均分≥0.80、离词表 ≥0.22）
  B = 疑似形近乱码（**不要**加进词表 —— 它是被纠正的对象）
  C = 单帧低频但高分（可能是一次性文案/公告，谨慎）
"""
import collections
import glob
import importlib.util
import os
import re
import subprocess
import sys

FIXPY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rike-ocr-fix.py')
OCR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rike-ocr.sh')
spec = importlib.util.spec_from_file_location('roke', FIXPY)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
WORDS = M.load_words()
WSET = set(WORDS)

# 非游戏截图的排除名单（自己做的示意图/演示图）
BLACK = ('arttext-demo', 'fontdemo', 'click_check', 'seal.', 'pdemo', 'ff-x', 'ff1', 'ff2',
         'ff3', 'ff4', 'ff5', 'ff6', 'ff7', 'x-official', 'x-zh', 'grok-now',
         'mark-demo', '演示-点击标记', 'shd-shot')
DEFAULT_PATS = ('/sdcard/dsh/tools/rike-fixtures/*.jpg', '/sdcard/dsh/debug/*.jpg',
                '/sdcard/dsh/debug/*.png', '/sdcard/dsh/guild-shots/*.jpg',
                '/sdcard/dsh/*.jpg')


def collect(argv):
    pats = argv or DEFAULT_PATS
    frames = []
    for p in pats:
        frames += glob.glob(p) if any(c in p for c in '*?[') else [p]
    return sorted({f for f in frames if os.path.isfile(f)
                   and not any(b in os.path.basename(f) for b in BLACK)})


def plausible(t):
    return (2 <= len(t) <= 10 and re.search(r'[\u4e00-\u9fff]', t)
            and not re.fullmatch(r'[\d\s/:.\-]+', t))


def main():
    frames = collect(sys.argv[1:])
    per_frame, cnt, times = {}, collections.Counter(), collections.Counter()
    scores = collections.defaultdict(list)
    for f in frames:
        r = subprocess.run(['sh', OCR, '--tsv', f], capture_output=True, text=True)
        seen = set()
        for line in r.stdout.splitlines():
            p = line.split('\t')
            if len(p) < 6:
                continue
            t = p[0].strip()
            try:
                sc = float(p[5])
            except ValueError:
                continue
            if t:
                times[t] += 1; scores[t].append(sc); seen.add(t)
        per_frame[f] = seen
        for t in seen:
            cnt[t] += 1

    known, cand = [], []
    for t, n in cnt.items():
        if not plausible(t):
            continue
        avg = sum(scores[t]) / len(scores[t])
        if t in WSET:
            known.append((n, t)); continue
        bs, bw = max(((M.sim(t, w), w) for w in WORDS), default=(0, ''))
        cand.append((n, times[t], t, avg, bs, bw))
    cand.sort(reverse=True)

    print(f'帧数 {len(frames)}；文本种类 {len(cnt)}；命中词表 {len(known)}；候选 {len(cand)}')
    print('\n=== A. 疑似真实缺词（≥2帧重复 且 离词表远）→ 过一遍再加 ===')
    for n, tt, t, avg, bs, bw in cand:
        if n >= 2 and bs < 0.78 and avg >= 0.80:
            print(f'  {n}帧/{tt}次  平均分{avg:.2f}  最近词表"{bw}"({bs:.2f})  「{t}」')
    print('\n=== B. 疑似形近乱码（**别加**，这是要被纠的对象）===')
    for n, tt, t, avg, bs, bw in cand:
        if n >= 2 and 0.60 <= bs < 1.0 and avg < 0.95:
            print(f'  {n}帧/{tt}次  平均分{avg:.2f}  最近词表"{bw}"({bs:.2f})  「{t}」')
    print('\n=== C. 单帧高分（一次性文案，谨慎）===')
    for n, tt, t, avg, bs, bw in cand:
        if n == 1 and bs < 0.70 and avg >= 0.90 and len(t) >= 3:
            print(f'  1帧/{tt}次  平均分{avg:.2f}  「{t}」')


if __name__ == '__main__':
    main()
