# -*- coding: utf-8 -*-
"""机器人的记忆库（最小版，2026-10-11 用户要求）。

用户原话：「机器人可以有自己的记忆库吗」→ 他选的是「先记**群里的人** +
**群里的梗/事件**，按相关性取用」。这个模块只管**存储 + 检索**：

  · 写：`memory_tool.py`（人 / agent 手工加；以后可以接"自动整理"）；
  · 读：`brain.py::_memory_block()` —— 每轮按当前消息挑几条，拼进 **user prompt**。

为什么不像 `逆水寒常识.md` 那样全量塞进 system：
  常识是固定的一小份，而记忆条数会一直涨 —— 全量注入等于每轮都付一遍 token；
  而且记忆是"跟这句话相关才有用"，全塞反而稀释注意力。

文件：项目根目录 `记忆库.json`（人可读、可手改，改完即时生效）：

    {
      "version": 1,
      "items": [
        {"kind": "person", "name": "某群友", "note": "爱发水獭表情包",
         "aliases": ["某群友儿"], "pri": 0, "ts": 1791712000},
        {"kind": "topic", "name": "玄狐洞", "note": "新副本，5 灵不够别碰首席", "ts": ...}
      ]
    }

  · `kind`：person（人）/ topic（梗、事件、瓜）；
  · `aliases`：别名（昵称简写、OCR 常抖的写法）—— 检索时一起看；
  · `pri`：手写优先级，数字越大越靠前，默认 0；
  · **空 note 的条目不会被注入**（挖出来的骨架，等填了内容再启用）；
  · 名字长度 < 2 的不参与子串检索（"白"会命中"白天"，太吵）。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DEFAULT_FILE = "记忆库.json"
KINDS = ("person", "topic")
#: 同一条记忆的 note 最多留多少字 —— 每天学一次、天天拼，不封顶会越来越长
MAX_NOTE_TOTAL = 120


def _norm(s) -> str:
    """比较用的归一化：**去掉所有空白**再小写。

    ★ 2026-10-11 实测踩的（自动整理上线第一天）：群里那个昵称在流水里是
      「群友I 潮光」（中间有空格），模型学出来写成「群友I潮光」——精确比名字于是
      变成**两个条目**；检索时"带空格的名字"也匹配不上不带空格的正文。
      统一规范化之后，这两种写法就是同一个人。
    """
    return re.sub(r"\s+", "", str(s or "")).lower()
KIND_NAMES = {"person": "人", "topic": "梗/事件"}


def _atomic_write_json(path, obj):
    """原子写 JSON。

    ⚠️ 不能用 write 工具的硬链接那套（/sdcard 是 FUSE，不支持），也**不能只靠
    os.replace** —— 它在 /sdcard 上可能 EPERM（见 wxauto/atomic.py 的说明）。
    这里 tmp 写完先试 replace，失败退化成直接覆盖。
    """
    p = str(path)
    tmp = p + ".tmp"
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.replace(tmp, p)
    except OSError:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


class Memory:
    """记忆库。**所有方法都不抛异常**（读坏了当空库，绝不连累机器人）。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(DEFAULT_FILE)
        self._items = []
        self._mtime = None

    # ---------------------------------------------------------------- 读写
    def load(self):
        try:
            st = self.path.stat()
        except Exception:
            self._items, self._mtime = [], None
            return self
        if self._mtime is not None and st.st_mtime == self._mtime:
            return self
        items = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                items = raw.get("items") or []
            elif isinstance(raw, list):
                items = raw
        except Exception:
            items = []
        clean = []
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            kind = str(it.get("kind") or "person").strip()
            if kind not in KINDS:
                kind = "person"
            try:
                pri = float(it.get("pri") or 0)
            except Exception:
                pri = 0.0
            try:
                ts = float(it.get("ts") or 0)
            except Exception:
                ts = 0.0
            clean.append({
                "kind": kind,
                "name": name,
                "note": str(it.get("note") or "").strip(),
                "aliases": [str(a).strip() for a in (it.get("aliases") or [])
                            if str(a).strip()],
                "pri": pri,
                "ts": ts,
            })
        self._items = clean
        self._mtime = st.st_mtime
        return self

    def save(self):
        try:
            _atomic_write_json(self.path,
                               {"version": 1, "updated": int(time.time()),
                                "items": self._items})
            try:
                self._mtime = self.path.stat().st_mtime
            except Exception:
                self._mtime = None
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- 查询
    def items(self, kind=None):
        self.load()
        if kind:
            return [i for i in self._items if i["kind"] == kind]
        return list(self._items)

    def stats(self):
        self.load()
        person = sum(1 for i in self._items if i["kind"] == "person")
        topic = len(self._items) - person
        return {"total": len(self._items), "person": person, "topic": topic,
                "with_note": sum(1 for i in self._items if i["note"])}

    def find(self, key):
        self.load()
        k = _norm(key)
        if not k:
            return []
        out = []
        for it in self._items:
            names = [it["name"]] + list(it["aliases"])
            if any(k == _norm(n) or k in _norm(n) for n in names):
                out.append(it)
        return out

    def search(self, text, limit=6, max_chars=420, max_note=60,
               vector=True, vec_min=None):
        """按一段文本挑相关记忆，返回可直接拼进提示词的短句列表。

        排序：人排在梗前面（人更容易接错）→ 手写 pri 大的 → 最近更新的。

        ★ 2026-10-12（借鉴 AstrBot / LangBot 的知识库）：**子串命中仍是主路**
          （精确、可解释），上面那套排序原样不动；下面再用**向量**补位 ——
          文本里没提名字、但语义相关的那几条（例："上次那个瓜的结论是啥"）。
          ⚠️ 服务不通 / 拿不到向量 → `vec_added` 为空，行为跟改之前**完全一样**。
        """
        t = _norm(text)
        if not t:
            return []
        self.load()
        hit = []
        for it in self._items:
            if not it["note"]:
                continue                      # 空 note 不注入（骨架先不启用）
            names = [it["name"]] + list(it["aliases"])
            # 名字也规范化（「群友I 潮光」和「群友I潮光」算同一个），长度按规范化后的算
            if not any(len(_norm(n)) >= 2 and _norm(n) in t for n in names):
                continue
            hit.append(it)
        hit.sort(key=lambda i: (0 if i["kind"] == "person" else 1, -i["pri"], -i["ts"]))
        # ★ 向量补位（可选、失败即降级）：只补"名字没在文本里出现"的那些，
        #   顺序按相似度从高到低，接在主路命中之后。
        if vector:
            try:
                from . import vecsearch
                cand = [it for it in self._items if it["note"]]
                docs = ["%s %s %s" % (it["name"], " ".join(it["aliases"]), it["note"])
                        for it in cand]
                thr = float(vec_min if vec_min is not None else vecsearch.DEFAULT_MIN)
                got = vecsearch.rank(text, docs, top=max(4, int(limit)),
                                     timeout=4.0)
                have = {it["name"] for it in hit}
                for idx, sc in got:
                    if sc < thr or idx < 0 or idx >= len(cand):
                        continue
                    it = cand[idx]
                    if it["name"] in have:
                        continue
                    have.add(it["name"])
                    hit.append(it)
            except Exception:
                pass
        out, used = [], 0
        for it in hit:
            if len(out) >= max(1, int(limit)):
                break
            line = "· %s：%s" % (it["name"], it["note"][:max_note])
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line) + 1
        return out

    # ---------------------------------------------------------------- 修改
    def add(self, kind, name, note, aliases=None, pri=0):
        """加一条；同名（或撞别名）就**合并** note。返回 'added' / 'merged' / False。"""
        self.load()
        kind = (kind or "person").strip()
        if kind not in KINDS:
            kind = "person"
        name = str(name or "").strip()
        note = str(note or "").strip()
        if not name:
            return False
        al = [str(a).strip() for a in (aliases or []) if str(a).strip()]
        for it in self._items:
            names = [it["name"]] + list(it["aliases"])
            if any(_norm(name) == _norm(n) for n in names):
                if note and note not in it["note"]:
                    merged = ((it["note"] + "；" + note) if it["note"] else note)
                    if len(merged) > MAX_NOTE_TOTAL:
                        # ★ 太长就**换成最新的**（旧的多半过时了，一直拼下去
                        #   会把注入预算吃光 —— 每轮只给记忆 420 字）。
                        merged = note
                    it["note"] = merged
                for a in al:
                    if a not in it["aliases"]:
                        it["aliases"].append(a)
                if pri:
                    it["pri"] = max(it["pri"], float(pri))
                it["ts"] = time.time()
                self.save()
                return "merged"
        self._items.append({"kind": kind, "name": name, "note": note, "aliases": al,
                            "pri": float(pri or 0), "ts": time.time()})
        self.save()
        return "added"

    def remove(self, key):
        self.load()
        k = _norm(key)
        if not k:
            return 0
        keep = []
        for i in self._items:
            names = [i["name"]] + list(i["aliases"])
            if any(k == _norm(n) or k in _norm(n) for n in names):
                continue
            keep.append(i)
        n = len(self._items) - len(keep)
        if n:
            self._items = keep
            self.save()
        return n


def load_memory(path=None):
    """给 brain 用的便捷入口：出错返回 None。"""
    try:
        return Memory(path).load()
    except Exception:
        return None
