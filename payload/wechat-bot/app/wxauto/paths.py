# -*- coding: utf-8 -*-
"""v2.0.0 路径解析：**程序（app/）与用户数据（data/）分离**。

    APP_DIR   程序目录（本文件的上级的上级 = app/）—— 升级时被整体覆盖
    DATA_DIR  用户数据目录，默认 ../data/；可用环境变量 WXBOT_DATA_DIR 覆盖
              （在意隐私的人可以指到容器私有目录，见《配置手册》）
    LOGS_DIR  日志目录，默认 ../logs/；可用 WXBOT_LOG_DIR 覆盖

★ 兼容旧结构：若 app/ 下就有 config.yaml（v1 那种"代码数据混装"布局），
  且没有同级 data/，则 DATA_DIR / LOGS_DIR 回落到 APP_DIR —— 老目录照样能跑。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

_SIBLING_DATA = APP_DIR.parent / "data"
_SIBLING_LOGS = APP_DIR.parent / "logs"
# v1 混装布局：代码旁边就是 config.yaml，且没有独立的 ../data/
_V1_LAYOUT = (APP_DIR / "config.yaml").is_file() and not _SIBLING_DATA.is_dir()


def _pick(env_name: str, sibling: Path, v1_fallback: Path) -> Path:
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        p = Path(raw).expanduser()
    elif _V1_LAYOUT:
        p = v1_fallback
    else:
        p = sibling
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


DATA_DIR = _pick("WXBOT_DATA_DIR", _SIBLING_DATA, APP_DIR)
LOGS_DIR = _pick("WXBOT_LOG_DIR", _SIBLING_LOGS, APP_DIR)


def app(*parts) -> Path:
    return APP_DIR.joinpath(*parts)


def data(*parts) -> Path:
    return DATA_DIR.joinpath(*parts)


def logs(*parts) -> Path:
    return LOGS_DIR.joinpath(*parts)


def resource(*parts) -> Path:
    """程序自带资源（模型、默认模板等）—— 在 app/ 下，不进 data/。"""
    return APP_DIR.joinpath(*parts)


__all__ = ["APP_DIR", "DATA_DIR", "LOGS_DIR", "app", "data", "logs", "resource"]
