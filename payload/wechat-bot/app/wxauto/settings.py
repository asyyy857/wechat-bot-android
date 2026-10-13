# -*- coding: utf-8 -*-
"""配置读写：只改 ``config.yaml`` 里的某一两项，**一个字都不多动**。

为什么不用 ``yaml.safe_dump`` 写回去 —— 它会把所有注释和排版全丢掉，
而这个配置文件里的注释就是使用说明书。所以这里做行级替换。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import ROOT


def config_path(path=None) -> Path:
    return Path(path) if path else ROOT / "config.yaml"


def read_text(path=None) -> str:
    return config_path(path).read_text(encoding="utf-8")


def read_all(path=None) -> dict:
    return yaml.safe_load(read_text(path)) or {}


def get_value(key: str, path=None, default: Any = "") -> Any:
    """按顶层字段名读值，例如 ``get_value("api_key")``。"""
    data = read_all(path)
    for section in data.values():
        if isinstance(section, dict) and key in section:
            return section[key]
    return data.get(key, default)


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = "" if value is None else str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_value(key: str, value: Any, path=None) -> bool:
    """把 ``<key>:`` 那一行的值换掉，注释和排版全部保留。

    找到第一处形如 ``   key: xxx`` 的行（冒号后必须是空白或行尾，
    所以 ``api_key_env:`` 不会被 ``api_key`` 误伤）。
    """
    p = config_path(path)
    text = p.read_text(encoding="utf-8")
    pattern = re.compile(rf"^([ \t]*{re.escape(key)}:[ \t]*)(.*)$", re.M)
    m = pattern.search(text)
    if not m:
        return False
    new_text = text[:m.start()] + m.group(1) + _render(value) + text[m.end():]
    p.write_text(new_text, encoding="utf-8")
    return True


def set_many(pairs: dict, path=None) -> bool:
    ok = True
    for k, v in pairs.items():
        ok = set_value(k, v, path) and ok
    return ok
