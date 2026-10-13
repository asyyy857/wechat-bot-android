#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr-daemon.py —— 常驻 OCR 服务 v2（RapidOCR / onnxruntime）

为什么常驻：RapidOCR 首次加载模型 ~7s，热起来每帧 0.7~3s；识别率**碾压 tesseract**
（实测同一批历史帧：卡片行 5 个卡名 tesseract 26/35=74%，RapidOCR **35/35=100%**）。

v2 相对 v1 的四项改动（都有实测依据）：
  ① **线程数固定 4**（intra_op=4 / inter_op=1）：8 线程反而慢 —— 同批 8 帧整屏
     3.80s → **2.97s（快 22%）**，召回不变。手机 8 核上少开线程=少切换。
  ② **ROI 裁剪 + 可选放大**：只识别需要的一小块（卡片行、按钮），又快又准。
     实测卡行「原样不放大」最快且满分 —— **别学 tesseract 那套 400% 放大**。
  ③ **帧内容缓存**：key=(文件md5, roi, scale, fmt)。同一帧常被问好几次
     （is_idle / in_qy / scan…），第二次直接 0ms 返回，这是省时间最狠的一招。
  ④ **带坐标输出**（fmt=tsv/json）：RapidOCR 的框比 tesseract 准得多，
     用它替代 tesseract tsv 做「找文字→点它」（ocr-tap.sh）。

接口（GET）：
    /ping                         → pong v2 threads=4 warm=1
    /warm                         → 预热（阻塞到模型就绪）
    /ocr?f=<图>                   → 纯文本，每行一个识别结果
        [&roi=x,y,w,h]            → 只识别该矩形（原图坐标）
        [&scale=2]                → 识别前 LANCZOS 放大（小字场景偶尔有用）
        [&fmt=text|tsv|json]      → tsv: text\tx1\ty1\tx2\ty2\tscore（已换算回原图坐标）
    /stats                        → 缓存命中率等

配套：tools/rike-ocr.sh（统一入口，自动起服务 + tesseract 兜底）
      tools/ocr-daemon.sh（start/stop/status/restart + 保活）
"""
import os
import sys
import json
import time
import hashlib
import threading

sys.path.insert(0, os.environ.get('WX_OCRLIB', '/root/ocrlibs'))
import numpy as np
import cv2
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get('OCR_PORT', '18790'))
ROOT = os.environ.get('DSH_ROOT', '/sdcard/dsh')
THREADS = int(os.environ.get('OCR_THREADS', '4'))
VERSION = 'v2'

_engine = None
_lock = threading.Lock()          # RapidOCR 实例非线程安全 → 串行化
_cache = {}                       # (md5, roi, scale, fmt) -> 结果字符串
_cache_order = []
CACHE_MAX = int(os.environ.get('OCR_CACHE', '128'))
_stats = {'req': 0, 'hit': 0, 'miss': 0, 'ocr_s': 0.0}


def engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR(intra_op_num_threads=THREADS, inter_op_num_threads=1)
    return _engine


def cache_get(k):
    if k in _cache:
        _stats['hit'] += 1
        return _cache[k]
    return None


def cache_put(k, v):
    _cache[k] = v
    _cache_order.append(k)
    while len(_cache_order) > CACHE_MAX:
        old = _cache_order.pop(0)
        _cache.pop(old, None)


def parse_roi(s):
    if not s:
        return None
    try:
        x, y, w, h = [int(v) for v in s.split(',')]
        return (x, y, w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def run_ocr(path, roi, scale, fmt):
    """返回 (结果字符串, 缓存key)；文件读不出来时抛异常。"""
    with open(path, 'rb') as fp:
        data = fp.read()
    key = (hashlib.md5(data).hexdigest(), roi or (), round(scale, 2), fmt)
    hit = cache_get(key)
    if hit is not None:
        return hit, key

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('不是图片: %s' % path)
    ox = oy = 0
    if roi:
        ox, oy, w, h = roi
        img = img[max(0, oy):oy + h, max(0, ox):ox + w]
        if img.size == 0:
            raise ValueError('roi 超出图片范围: %s' % (roi,))
    if scale and abs(scale - 1.0) > 1e-6:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)

    t0 = time.time()
    with _lock:
        res, _ = engine()(img)
    _stats['miss'] += 1
    _stats['ocr_s'] += time.time() - t0

    inv = 1.0 / (scale if scale else 1.0)
    if fmt == 'text':
        out = '\n'.join(r[1] for r in res) if res else ''
    else:
        rows = []
        for r in (res or []):
            box, txt = r[0], r[1]
            sc = float(r[2]) if len(r) > 2 else 0.0
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x1 = int(min(xs) * inv) + ox
            y1 = int(min(ys) * inv) + oy
            x2 = int(max(xs) * inv) + ox
            y2 = int(max(ys) * inv) + oy
            rows.append((y1, x1, x2, y2, txt, sc))
        rows.sort(key=lambda r: (r[0], r[1]))          # 上→下、左→右
        if fmt == 'json':
            out = json.dumps([{'text': r[4], 'x1': r[1], 'y1': r[0], 'x2': r[2],
                               'y2': r[3], 'score': round(r[5], 3)} for r in rows],
                             ensure_ascii=False)
        else:
            out = '\n'.join('%s\t%d\t%d\t%d\t%d\t%.3f' % (r[4], r[1], r[0], r[2], r[3], r[5])
                            for r in rows)
    cache_put(key, out)
    return out, key


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        _stats['req'] += 1
        if u.path == '/ping':
            return self._out(('pong %s threads=%d warm=%d' %
                              (VERSION, THREADS, 1 if _engine is not None else 0)).encode())
        if u.path == '/stats':
            return self._out(json.dumps(_stats).encode())
        if u.path == '/warm':
            engine()
            return self._out(b'ready')
        if u.path != '/ocr':
            return self._out(b'ERR bad path', 404)

        f = (q.get('f') or [''])[0]
        if not f:
            return self._out(b'ERR no f', 400)
        if not os.path.isabs(f):
            f = os.path.join(ROOT, f)
        if not os.path.exists(f):
            return self._out(('ERR no file %s' % f).encode(), 404)
        roi = parse_roi((q.get('roi') or [''])[0])
        try:
            scale = float((q.get('scale') or ['1'])[0])
        except Exception:
            scale = 1.0
        fmt = (q.get('fmt') or ['text'])[0]
        if fmt not in ('text', 'tsv', 'json'):
            fmt = 'text'
        try:
            out, _k = run_ocr(f, roi, scale, fmt)
        except Exception as e:
            return self._out(('ERR %s' % e).encode(), 500)
        return self._out(out.encode('utf-8'))

    def _out(self, b, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == '__main__':
    print('warming up (threads=%d) ...' % THREADS, flush=True)
    engine()
    print('READY port=%d %s' % (PORT, VERSION), flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
