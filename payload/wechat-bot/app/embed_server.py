#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地向量服务（bge-small-zh-v1.5，CPU）—— 给机器人做「按相关性取用」。

★ 2026-10-12（借鉴 AstrBot / LangBot 的「知识库」）：原来只有子串匹配
  （`memory.py`）和全量注入（`逆水寒常识.md` 15.7KB 塞 system prompt）。

**为什么是独立进程，而不是塞进机器人主进程**：
  · onnxruntime 是从 OCR 运行时借的（`/root/ocrlibs`，那边 numpy 是 2.5.3，
    系统 python 是 1.26.4）—— 放主进程里等于给全机器人换一份 numpy，连坐风险高；
  · 服务没起来/挂了，机器人能**降级**回子串匹配，回复照常；
  · 模型常驻内存（几十 MB）也不占机器人主进程的额度。

接口（127.0.0.1，默认 8766）：
  GET  /health                              → {"ok":true,"dim":512,"cached":N}
  POST /rank   {"query":..,"docs":[..],"top":6} → {"ok":true,"items":[[i,score],..]}
  POST /embed  {"texts":[..]}                → {"ok":true,"vecs":[[..],..]}
文档向量按**文本内容**缓存（重复的文档只编码一次），所以第二次起只算 query。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wxauto.embedder import Embedder          # noqa: E402

PORT = int(os.environ.get("EMBED_PORT", "8766"))
CACHE_MAX = int(os.environ.get("EMBED_CACHE_MAX", "4000"))

_emb = Embedder()
_lock = threading.Lock()
_cache = {}          # text -> vec（numpy 行向量）
_hits = [0, 0]       # [命中, 未命中]


def _cached_embed(texts):
    """按文本缓存：只把没见过的送去编码。"""
    import numpy as np
    miss = [t for t in texts if t not in _cache]
    if miss:
        vecs = _emb.embed(miss)
        for t, v in zip(miss, vecs):
            _cache[t] = v
        _hits[1] += len(miss)
        if len(_cache) > CACHE_MAX:            # 简化的清理：满了丢一半（够用）
            for k in list(_cache.keys())[:CACHE_MAX // 2]:
                _cache.pop(k, None)
    _hits[0] += len(texts) - len(miss)
    return np.vstack([_cache[t] for t in texts])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):                 # 静音默认访问日志（ctl 里另有日志）
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json({"ok": True, "model": "bge-small-zh-v1.5(quant)",
                        "dim": 512, "cached": len(_cache),
                        "hit": _hits[0], "miss": _hits[1],
                        "err": _emb.err})
            return
        self._json({"ok": False, "error": "unknown path"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            self._json({"ok": False, "error": "bad body"}, 400)
            return
        try:
            with _lock:
                if self.path.startswith("/rank"):
                    query = str(payload.get("query") or "")
                    docs = [str(d) for d in (payload.get("docs") or [])]
                    top = int(payload.get("top") or 0)
                    if not query or not docs:
                        self._json({"ok": True, "items": []})
                        return
                    vecs = _cached_embed([query] + docs)
                    q = vecs[0]
                    sims = vecs[1:] @ q
                    pairs = sorted(((i, float(s)) for i, s in enumerate(sims)),
                                   key=lambda x: -x[1])
                    if top > 0:
                        pairs = pairs[:top]
                    self._json({"ok": True, "items": [[i, s] for i, s in pairs]})
                    return
                if self.path.startswith("/embed"):
                    texts = [str(t) for t in (payload.get("texts") or [])]
                    if not texts:
                        self._json({"ok": True, "vecs": []})
                        return
                    vecs = _cached_embed(texts)
                    self._json({"ok": True, "vecs": [[round(float(x), 6) for x in r]
                                                     for r in vecs]})
                    return
            self._json({"ok": False, "error": "unknown path"}, 404)
        except Exception as exc:
            self._json({"ok": False, "error": repr(exc)}, 500)


def main():
    t0 = time.time()
    ok = _emb.available
    print("[embed] 模型预加载 %s（%.1fs）%s"
          % ("成功" if ok else "失败", time.time() - t0, _emb.err), flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print("[embed] 启动：port=%d dim=512" % PORT, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
