# -*- coding: utf-8 -*-
"""表情包库：管理 ``表情包/`` 文件夹，按标签随机挑一张图。

目录约定（两种用法都支持）
--------------------------
   表情包/
     开心/            子文件夹名 = 标签，规则里写 ``image_tag: 开心`` 就只从这里挑
        a.gif
     无语/
     随便什么名字.png  直接放在根目录的算"无标签"，标签找不到时兜底用
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import List, Optional

IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class StickerLibrary:
    def __init__(self, root, logger=None) -> None:
        self.root = Path(root)
        self.logger = logger

    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)

    # ------------------------------------------------------------ 查询
    def tags(self) -> List[str]:
        """有哪些标签（= 根目录下的子文件夹名）。"""
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir()
                      if d.is_dir() and not d.name.startswith("."))

    def _all_in(self, d: Path) -> List[Path]:
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    def _files(self, tag: str = "") -> List[Path]:
        """按 tag 找图，三种情况：

        * ``tag`` 为空            -> 根目录下全部图
        * 存在 ``表情包/<tag>/``  -> 那个文件夹里的图
        * 其它                    -> 根目录里**文件名含 tag** 的图

        最后一条很关键：把图按情绪命名（``哭.png`` / ``哭_1.png``），
        规则里写 ``image_tag: 哭`` 就一次命中全部变体，不用建文件夹。

        ``tag`` 还可以是逗号分隔的多个候选（``无语,不语,亖``），
        命中任意一个的图都会被收进来。
        """
        if not tag:
            return self._all_in(self.root)

        parts = [p.strip() for p in str(tag).replace("，", ",").split(",") if p.strip()]
        if not parts:
            return self._all_in(self.root)

        out: List[Path] = []
        seen = set()
        for part in parts:
            sub = self.root / part
            found = self._all_in(sub) if sub.is_dir() \
                else [p for p in self._all_in(self.root) if part in p.stem]
            for p in found:
                if p.name not in seen:
                    seen.add(p.name)
                    out.append(p)
        return sorted(out)

    def count(self, tag: str = "") -> int:
        return len(self._files(tag))

    # ------------------------------------------------------------ 挑选
    def pick(self, tag: str = "",
             avoid: Optional[List[str]] = None) -> Optional[Path]:
        """随机挑一张图返回；没图就返回 None。

        * ``tag`` 为空、或者那个文件夹里没图 -> 退回根目录找
        * ``avoid`` 里的文件名尽量不挑（用来避免连着发同一张）
        """
        pool = self._files(tag)
        if not pool and tag:
            self._log(f"表情包「{tag}」里没有图，改用根目录的")
            pool = self._files("")
        if not pool:
            return None

        skip = set(avoid or [])
        fresh = [p for p in pool if p.name not in skip]
        return random.choice(fresh or pool)

    # ------------------------------------------------------------ 概览
    def summary(self) -> str:
        if not self.root.is_dir():
            return f"表情包目录不存在：{self.root}"
        parts = [f"根目录 {self.count('')}"]
        for t in self.tags():
            parts.append(f"{t} {self.count(t)}")
        return "表情包库（张）：" + "  ".join(parts)
