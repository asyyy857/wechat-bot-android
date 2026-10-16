# -*- coding: utf-8 -*-
"""向量检索客户端 —— 走本机 `embed_server`（默认 127.0.0.1:8766）。

★ 2026-10-12（借鉴 AstrBot / LangBot 的「知识库」）：给 `memory.py` 的**子串
  匹配**补一层**语义召回** —— 文本里没提名字、但确实相关的那几条，原来一条都
  命不中（例："上次那个瓜的结论是啥"）。

三条纪律（都是为了让"向量只是加分项"）：
  ① **失败一律返回 []** —— 调用方据此降级回子串匹配，回复绝不能因此受影响；
  ② 健康检查结果**缓存 60 秒** —— 否则每轮都探一次，白等一个超时；
  ③ 超时给得短（默认 6 秒）—— 拿不到就当没有，不许把回复卡住。

服务本体：项目根 `embed_server.py`（启停 `sh embed_ctl.sh start|stop|status`）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from .netutil import fetch_hard

URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8766")
_health = {"ok": None, "at": 0.0}

DEFAULT_MIN = 0.45          # 相似度低于它算不相关
                            # ★ 2026-10-15：0.55 → 0.45。本机 bge-small-zh 实测
                            #   相关 0.44~0.56、无关 ≤0.41（config 里 memory.vector_min
                            #   也同步改了；这个只是没配 config 时的兜底值）。


def available(ttl: float = 60.0) -> bool:
    """服务在不在（结果带缓存）。永远不抛异常。"""
    now = time.time()
    if _health["ok"] is not None and now - _health["at"] < ttl:
        return bool(_health["ok"])
    ok = False
    try:
        def _read(r):
            return bool(json.loads(r.read().decode()).get("ok"))
        ok = bool(fetch_hard(URL + "/health", 2.0, _read, slack=1.0))
    except Exception:
        ok = False
    _health["ok"] = ok
    _health["at"] = now
    return ok


def rank(query: str, docs, top: int = 0, timeout: float = 6.0):
    """``[(下标, 相似度), ...]`` 从高到低；服务不可用/出错都返回 ``[]``。"""
    docs = list(docs or [])
    if not query or not docs:
        return []
    try:
        body = json.dumps({"query": str(query), "docs": [str(d) for d in docs],
                           "top": int(top)}).encode("utf-8")
        req = urllib.request.Request(URL + "/rank", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        def _read(r):
            return json.loads(r.read().decode("utf-8"))
        d = fetch_hard(req, timeout, _read, slack=1.0)
        if not d.get("ok"):
            return []
        out = []
        for pair in (d.get("items") or []):
            try:
                out.append((int(pair[0]), float(pair[1])))
            except Exception:
                continue
        return out
    except Exception:
        # 一次失败就把它标记成"不可用"，省得后面几条回复继续等超时
        _health["ok"] = False
        _health["at"] = time.time()
        return []
