# -*- coding: utf-8 -*-
"""匹配层：关键词规则。命中即出模板回复，**不花一个 Token**。"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .reader import normalize


@dataclass
class Rule:
    name: str
    keywords: List[str] = field(default_factory=list)
    mode: str = "contains"          # contains | exact | regex
    replies: List[str] = field(default_factory=list)
    priority: int = 0
    max_len: int = 0                # 0 = 不限
    cooldown: float = 0.0
    case_sensitive: bool = False
    # 被 @ 时不走这条规则，交给模型答（避免"你觉得呢？"被"问号"规则抢走，回一个"？"）
    mention_exempt: bool = False
    # ★ 2026-10-12：`mention_exempt` 的反面 —— 被 @ 时**照常**走这条规则，
    #   **不**被"以整句为主"那道判据让位给模型。给**动作指令**类规则用
    #   （看看腿 / 看看腹肌 / 今日美图）：群里习惯每次都带 @，
    #   不写这条它们在被 @ 时永远触发不了（实测 23:11 那条只回了一句话、图没发）。
    mention_ok: bool = False
    # 命中之后干什么：reply = 回文字（默认），image = 发一张表情包，
    # meitu = 发一张「今日美图」（联网取图，见 meitu.py）
    action: str = "reply"
    # action=image 时从哪个标签的文件夹里挑图；留空就用表情包根目录
    image_tag: str = ""
    # action=meitu 时指定图源分类（如「二次元」）；留空 = 所有分类混着随机
    meitu_source: str = ""
    # 这条规则的回复**不要**自动追加「喵」（默认 false = 照旧加喵）
    no_meow: bool = False

    @property
    def is_image(self) -> bool:
        return self.action == "image"

    @property
    def is_meitu(self) -> bool:
        return self.action == "meitu"

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(
            name=str(d.get("name") or "rule"),
            keywords=[str(k) for k in (d.get("keywords") or [])],
            mode=str(d.get("mode") or "contains").lower(),
            replies=[str(r) for r in (d.get("replies") or [])],
            priority=int(d.get("priority", 0) or 0),
            max_len=int(d.get("max_len", 0) or 0),
            cooldown=float(d.get("cooldown", 0) or 0),
            case_sensitive=bool(d.get("case_sensitive", False)),
            mention_exempt=bool(d.get("mention_exempt", False)),
            mention_ok=bool(d.get("mention_ok", False)),
            action=str(d.get("action") or "reply").lower(),
            image_tag=str(d.get("image_tag") or ""),
            meitu_source=str(d.get("meitu_source") or ""),
            no_meow=bool(d.get("no_meow", False)),
        )


class Matcher:
    def __init__(self, rules: List[Rule], rng: Optional[random.Random] = None) -> None:
        # 高优先级在前，命中多条时取第一条
        self.rules = sorted(rules, key=lambda r: -r.priority)
        self.rng = rng or random.Random()

    def match(self, text: str, skip=None) -> Optional[Rule]:
        """返回命中的最高优先级规则。

        ``skip`` 是一个 ``(rule) -> bool`` 回调，返回 True 表示跳过该规则
        （用于「被 @ 时跳过泛化关键词」）。
        """
        t = normalize(text)
        if not t:
            return None
        for rule in self.rules:
            if skip is not None and skip(rule):
                continue
            if self._hit(rule, t):
                return rule
        return None

    def _hit(self, rule: Rule, text: str) -> bool:
        if rule.max_len and len(text) > rule.max_len:
            return False
        hay = text if rule.case_sensitive else text.lower()
        for kw in rule.keywords:
            if not kw:
                continue
            if rule.mode == "regex":
                # regex 关键词**不能**归一（空格/标点可能是它的一部分），
                # 而且它匹配的是原文 `text`，不是删过空白的 `hay`。
                try:
                    if re.search(kw, text, 0 if rule.case_sensitive else re.I):
                        return True
                except re.error:
                    continue
                continue
            # ★★ 2026-10-10 评审修复：`hay` 是 `normalize(text)`（**删掉所有空白**），
            #   而关键词一直没跟着归一 —— 于是 `rules.yaml` 里带空格的那种写法
            #   （「拍了拍 主人昵称」就是为了兼容 OCR 读出的空格变体）**永远匹配不到**，
            #   只是没人发现，因为同一条规则还有无空格的变体顶着。
            #   这里对关键词做同一套归一（`normalize` = 只删空白，不动标点），
            #   让"兼容空格写法"这个意图真正生效。
            kw_n = normalize(kw)
            needle = kw_n if rule.case_sensitive else kw_n.lower()
            if rule.mode == "exact":
                if hay == needle:
                    return True
            else:  # contains
                if needle in hay:
                    return True
        return False

    def reply_for(self, rule: Rule) -> Optional[str]:
        return self.rng.choice(rule.replies) if rule.replies else None
