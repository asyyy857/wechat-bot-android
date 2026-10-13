# -*- coding: utf-8 -*-
"""本地中文 embedding（bge-small-zh-v1.5 量化版，ONNX / CPU）。

★ 2026-10-12（用户让借鉴 B 站那几个开源项目）：AstrBot / LangBot 都有「知识库」
  和「按相关性取记忆」。我们原来只有**子串匹配**（`memory.py`）和**全量注入**
  （`逆水寒常识.md` 15.7KB 直接塞 system prompt），条数一涨就既不准又贵。
  这里补上「语义检索」这一层：全靠本机 CPU 跑，**不联网、不花钱**。

模型：`models/bge-small-zh/`（`model_quantized.onnx` 24MB + `vocab.txt`）
  · int8 量化、512 维、CLS pooling + L2 归一化（bge 的官方口径）；
  · 下载自 hf-mirror（`Xenova/bge-small-zh-v1.5`）。

⚠️ 依赖是**从 OCR 运行时借的**：`/root/ocrlibs` 里有 onnxruntime 1.30.0，它的
  numpy 是 2.5.3，跟系统 python 的 1.26.4 不是同一份。所以：
    · **所有 numpy / onnxruntime 的 import 都写在函数里（懒加载）** —— 这样
      机器人主进程 import 本模块**不会**被带进另一份 numpy；
    · 真正要算向量时，调用方得带上 `PYTHONPATH=/root/ocrlibs`（或者走
      `models/embed_server.py` 那个独立小服务）。
  这么做是为了**版本隔离 + 可降级**：拿不到向量就回退子串匹配，绝不影响回复。

用法：
    from wxauto.embedder import Embedder
    e = Embedder()
    e.embed(["没5灵能碰首席不"])           # -> [[512 个 float]]（已 L2 归一化）
    e.rank("没5灵能碰首席不", docs)        # -> [(下标, 相似度), ...] 从高到低
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

DEFAULT_DIR = "models/bge-small-zh"
MAX_LEN = 512
DIM = 512


# --------------------------------------------------------------------------
# 纯 Python 的 BERT tokenizer（vocab.txt + WordPiece）—— 不用 transformers
# --------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    out = []
    for ch in str(text or ""):
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD:
            continue
        if ch in ("\t", "\n", "\r") or unicodedata.category(ch) == "Zs":
            out.append(" ")
        elif unicodedata.category(ch) == "Cc":
            continue
        else:
            out.append(ch)
    return "".join(out)


def _is_cjk(cp: int) -> bool:
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
            or 0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2B73F
            or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF
            or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


def _is_punct(ch: str) -> bool:
    cp = ord(ch)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _tokenize_chinese(text: str) -> str:
    """CJK 字符两侧加空格（这样后面按空白切，中文就是逐字切）。"""
    out = []
    for ch in text:
        if _is_cjk(ord(ch)):
            out.append(" " + ch + " ")
        else:
            out.append(ch)
    return "".join(out)


def _split_on_punc(token: str):
    """标点单独成 token（HuggingFace BertTokenizer 的 split_on_punc 口径）。"""
    chars = list(token)
    out, buf = [], []
    for i, ch in enumerate(chars):
        if _is_punct(ch):
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _basic_tokenize(text: str):
    text = _clean_text(text)
    text = _tokenize_chinese(text)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("Mn"))
    text = text.lower()
    out = []
    for tok in text.split():
        out.extend(_split_on_punc(tok))
    return out


def _wordpiece(token: str, vocab: dict, unk: int, max_chars: int = 100):
    """贪心最长匹配（BERT WordPiece）。"""
    if not token:
        return []
    if len(token) > max_chars:
        return [unk]
    if token in vocab:
        return [vocab[token]]
    out, start = [], 0
    while start < len(token):
        end, cur = len(token), None
        while start < end:
            sub = token[start:end]
            if start > 0:
                sub = "##" + sub
            if sub in vocab:
                cur = vocab[sub]
                break
            end -= 1
        if cur is None:
            return [unk]
        out.append(cur)
        start = end
    return out


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------
class Embedder:
    """懒加载的本地向量模型。第一次 `embed()` 才建 onnxruntime 会话。"""

    def __init__(self, model_dir=None, threads: int = 2):
        base = Path(__file__).resolve().parent.parent
        self.dir = Path(model_dir) if model_dir else (base / DEFAULT_DIR)
        self.threads = max(1, int(threads))
        self._sess = None
        self._vocab = None
        self._np = None
        self.err = ""

    # -- 内部 ---------------------------------------------------------------
    def _load(self) -> bool:
        if self._sess is not None:
            return True
        try:
            import numpy as np            # 懒加载：见文件头（版本隔离）
            import onnxruntime as ort
        except Exception as exc:
            self.err = "拿不到 numpy/onnxruntime（要 PYTHONPATH=/root/ocrlibs）：%r" % (exc,)
            return False
        try:
            model = self.dir / "model_quantized.onnx"
            vocab_p = self.dir / "vocab.txt"
            if not model.is_file() or not vocab_p.is_file():
                self.err = "模型文件不在：%s" % self.dir
                return False
            vocab = {}
            with open(vocab_p, encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    vocab[line.rstrip("\n")] = i
            so = ort.SessionOptions()
            so.intra_op_num_threads = self.threads
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._sess = ort.InferenceSession(
                str(model), so, providers=["CPUExecutionProvider"])
            self._vocab = vocab
            self._np = np
            return True
        except Exception as exc:
            self.err = "模型加载失败：%r" % (exc,)
            return False

    @property
    def available(self) -> bool:
        return self._load()

    def _ids(self, text: str):
        v = self._vocab
        unk = v.get("[UNK]", 100)
        toks = []
        for t in _basic_tokenize(text):
            toks.extend(_wordpiece(t, v, unk))
            if len(toks) >= MAX_LEN - 2:
                break
        toks = toks[:MAX_LEN - 2]
        return ([v.get("[CLS]", 101)] + toks + [v.get("[SEP]", 102)])

    # -- 对外 ---------------------------------------------------------------
    def embed(self, texts):
        """一串文本 → 归一化后的向量（numpy 数组，形状 [n, 512]）。失败抛异常。"""
        if isinstance(texts, str):
            texts = [texts]
        if not self._load():
            raise RuntimeError(self.err or "embedder 不可用")
        np = self._np
        rows = [self._ids(t) for t in texts]
        width = max(len(r) for r in rows)
        ids = np.zeros((len(rows), width), dtype=np.int64)
        mask = np.zeros((len(rows), width), dtype=np.int64)
        types = np.zeros((len(rows), width), dtype=np.int64)
        for i, r in enumerate(rows):
            ids[i, :len(r)] = r
            mask[i, :len(r)] = 1
        feeds = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
        names = {i.name for i in self._sess.get_inputs()}
        feeds = {k: v for k, v in feeds.items() if k in names}
        out = self._sess.run(None, feeds)[0]
        vec = np.asarray(out)[:, 0, :]                 # bge：CLS pooling
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return vec / norm

    def rank(self, query: str, docs):
        """查询在一堆文档里的相似度排序 → ``[(下标, 相似度), ...]``（高到低）。

        文档为空或模型不可用都返回 ``[]``（调用方据此降级到子串匹配）。
        """
        docs = list(docs or [])
        if not docs:
            return []
        try:
            vecs = self.embed([query] + docs)
        except Exception:
            return []
        q = vecs[0]
        sims = vecs[1:] @ q
        order = sorted(range(len(docs)), key=lambda i: -float(sims[i]))
        return [(i, float(sims[i])) for i in order]
