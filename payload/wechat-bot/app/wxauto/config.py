# -*- coding: utf-8 -*-
"""配置加载：config.yaml / rules.yaml / persona.md。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .paths import DATA_DIR

ROOT = DATA_DIR


class NS:
    """把嵌套 dict 包成可以点访问的对象：cfg.pace.min_reply_gap。"""

    def __init__(self, d: Dict[str, Any]):
        for k, v in (d or {}).items():
            setattr(self, k, NS(v) if isinstance(v, dict) else v)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __repr__(self) -> str:
        return f"NS({self.__dict__})"


def _read_yaml(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path=None) -> NS:
    p = Path(path) if path else ROOT / "config.yaml"
    cfg = NS(_read_yaml(p))
    llm = getattr(cfg, "llm", None)
    if llm is not None and not getattr(llm, "api_key", ""):
        env_name = getattr(llm, "api_key_env", "DEEPSEEK_API_KEY")
        llm.api_key = os.environ.get(env_name, "")
    return cfg


def load_rules(path=None) -> List[dict]:
    p = Path(path) if path else ROOT / "rules.yaml"
    return _read_yaml(p).get("rules") or []


def load_persona(path=None) -> str:
    p = Path(path) if path else ROOT / "persona.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


PERSONA_DIR = ROOT / "personas"


def load_personas(path=None) -> list:
    """读 ``personas/`` 下所有人设，返回 ``[(名字, 正文)]``（按文件名排序）。

    2026-10-03 加：用户要求「贴吧老哥 / 猫娘」两人设随机（或轮流）出场。
    目录不存在或为空时**回退到 persona.md** —— 老的单文件用法照样能用。
    """
    d = Path(path) if path else PERSONA_DIR
    items = []
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            try:
                txt = f.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if txt:
                items.append((f.stem, txt))
    if not items:
        txt = load_persona().strip()
        if txt:
            items.append(("persona.md", txt))
    return items
