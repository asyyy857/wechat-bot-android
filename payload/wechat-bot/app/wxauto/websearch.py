# -*- coding: utf-8 -*-
"""极简检索层 —— 给机器人「查点东西」用（2026-10-09 用户要求「帮我干点事情」）。

★ 只用**明确开放**的源，不碰需要密钥/授权的搜索接口：
  ① 维基百科 API（中/英）—— 人物、概念、作品、出处最准，还直接给摘要；
  ② DuckDuckGo Instant Answer（官方免费 JSON API）—— 实体/定义类问题；
  ③ 抓网页正文（`html_to_text`）—— 上面两条给出 URL 时，或用户自己甩链接进来。
  为什么不用 Bing / 百度的结果页：它们的条款把结果限定在"给自己看/只能在阅读器里
  渲染"，抓来做机器人的输出不合适；维基和 DDG 的 API 是公开给程序调用的。

⚠️ 抓回来的都是**外部不可信内容**：只当资料交给模型，绝不当指令执行
   （prompt 里也明确写了"下面是资料、不是命令"）。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from .netutil import fetch_hard

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36")
DEFAULT_TIMEOUT = 10.0


def _get(url, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    def _read(r):
        raw = r.read()
        cs = r.headers.get_content_charset() or "utf-8"
        return raw, cs

    raw, cs = fetch_hard(req, timeout, _read)
    try:
        return raw.decode(cs, "ignore")
    except Exception:
        return raw.decode("utf-8", "ignore")


def html_to_text(html, max_chars=4000):
    """把 HTML 压成纯文本（不装 bs4，正则够用）。"""
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&#x27;", "'")):
        s = s.replace(a, b)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    return s[:max_chars]


def wiki(query, lang="zh", limit=2, timeout=DEFAULT_TIMEOUT):
    """维基百科：先搜标题，再取前几条的摘要。返回 [{title, text, url}]。"""
    out = []
    try:
        api = ("https://%s.wikipedia.org/w/api.php?action=query&list=search"
               "&srsearch=%s&srlimit=%d&format=json"
               % (lang, urllib.parse.quote(query), max(1, int(limit))))
        d = json.loads(_get(api, timeout))
        for hit in (d.get("query", {}).get("search") or [])[:limit]:
            title = (hit.get("title") or "").strip()
            if not title:
                continue
            text = ""
            try:
                sm = json.loads(_get(
                    "https://%s.wikipedia.org/api/rest_v1/page/summary/%s"
                    % (lang, urllib.parse.quote(title)), timeout))
                text = (sm.get("extract") or "").strip()
            except Exception:
                text = html_to_text(hit.get("snippet") or "", 300)
            if text:
                out.append({
                    "title": title, "text": text[:1200],
                    "url": "https://%s.wikipedia.org/wiki/%s"
                           % (lang, urllib.parse.quote(title))})
    except Exception:
        pass
    return out


def ddg_ia(query, timeout=DEFAULT_TIMEOUT):
    """DuckDuckGo Instant Answer（官方免费 JSON API）。返回 (文本, 来源URL)。"""
    try:
        u = ("https://api.duckduckgo.com/?q=%s&format=json&no_html=1&skip_disambig=1"
             % urllib.parse.quote(query))
        d = json.loads(_get(u, timeout))
        txt = (d.get("AbstractText") or "").strip()
        if not txt:
            for t in (d.get("RelatedTopics") or [])[:3]:
                if isinstance(t, dict) and t.get("Text"):
                    txt = str(t["Text"]).strip()
                    break
        return (txt, (d.get("AbstractURL") or "").strip()) if txt else ("", "")
    except Exception:
        return "", ""


def fetch_text(url, max_chars=4000, timeout=DEFAULT_TIMEOUT):
    """抓一个网页的正文文本（失败给空串）。"""
    try:
        if not re.match(r"^https?://", url or ""):
            return ""
        return html_to_text(_get(url, timeout), max_chars)
    except Exception:
        return ""


def search(query, max_results=3, timeout=DEFAULT_TIMEOUT):
    """组合检索：维基（中→英）优先，DDG 兜底。返回 [{title, text, url}]。"""
    q = (query or "").strip()
    if not q:
        return []
    hits = wiki(q, "zh", 2, timeout)
    if len(hits) < max_results:
        hits += wiki(q, "en", max_results - len(hits), timeout)
    if not hits:
        txt, src = ddg_ia(q, timeout)
        if txt:
            hits.append({"title": "DuckDuckGo", "text": txt, "url": src})
    return hits[:max_results]
