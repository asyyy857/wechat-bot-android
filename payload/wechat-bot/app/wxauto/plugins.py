# -*- coding: utf-8 -*-
"""插件目录 —— 零 Token 的本地扩展点。

★ 2026-10-12（用户要求「插件化 + 面板配置化」里的"插件"那一半）：借鉴 AstrBot
  的插件 / LangBot 的扩展，但**刻意做得很小** —— 我们不要"从市场装一个包"，
  只要「我自己丢一个 py 进去，就能给机器人加个本事」。

约定（`plugins/` 下一级目录 = 一个插件）：

    plugins/<名字>/plugin.py      必须
    plugins/<名字>/README.md      可选（写清干什么、谁能用）

`plugin.py` 里写一个 `register(api)`：

    def register(api):
        @api.on_message                       # 每条群消息都过一遍（本地、零 token）
        def _hello(text, sender, is_self):
            if text.strip() == "现在几点":
                return "现在是 " + api.now().strftime("%H:%M")
            return None                       # None = 这条不管，交给别人

        api.add_tool("dice", "掷骰子", "有人要掷骰子/比大小的时候",
                     lambda text: api.send("🎲 " + str(api.random().randint(1, 6))))

⚠️ 四条纪律（前三条是踩坑换来的）：
  ① **插件抛异常绝不影响回复链路** —— 每个钩子单独 try/except + 记日志；
  ② **插件跑在机器人进程里**，拿到的是完整权限（能发消息、能读文件）——
     只放自己信得过的代码，README 里也写了这句；
  ③ **热重载**：`plugins/` 下任何文件的 mtime 变了就整体重新加载（改完不用
     重启机器人，跟 `rules.yaml` / `personas/` 一个待遇）；
  ④ 插件的**工具**只能是"本地立刻能办"的（发文本/发图这类），要花钱、要联网、
     要人办的仍然走 `agent` 那条路。
"""

from __future__ import annotations

import importlib.util
import random as _random
import time
import traceback
from pathlib import Path

DIRNAME = "plugins"


class _Api:
    """交给插件的把手（只暴露这几个，别的一律不给）。"""

    def __init__(self, plugin_dir, log=None, sender=None):
        self.dir = plugin_dir
        self.log = log
        self._sender = sender
        self.hooks = []
        self.tools = []

    # -- 注册 ---------------------------------------------------------------
    def on_message(self, fn):
        """装饰器：注册一个"每条消息都过一遍"的钩子。

        签名 ``fn(text, sender, is_self)``：返回字符串 = 就回这句（算你处理了）；
        返回 None = 不管这条，交给下一个人。
        """
        self.hooks.append(fn)
        return fn

    def add_tool(self, name, desc, when, handler,
                 owner_only=True, cooldown=0.0, max_per_day=0):
        """注册一个工具（会并进 `wxauto/tools.py` 那张表）。

        ``handler(text)`` 返回 True = 这条办完了（调用方直接 return）。
        ⚠️ 默认 `owner_only=True`：群友 @ 它**不能**触发会发东西的工具。
        """
        self.tools.append({"name": str(name), "desc": str(desc), "when": str(when),
                           "handler": handler, "owner_only": bool(owner_only),
                           "cooldown": float(cooldown or 0),
                           "max_per_day": int(max_per_day or 0)})
        return handler

    # -- 便利 ---------------------------------------------------------------
    def send(self, text) -> bool:
        """直接往群里发一句（走机器人那套节流/校验）。没接上就返回 False。"""
        if not self._sender:
            return False
        try:
            return bool(self._sender(str(text)))
        except Exception:
            return False

    @staticmethod
    def now():
        return time.localtime()

    @staticmethod
    def random():
        return _random

    def log_line(self, msg):
        try:
            if self.log is not None:
                self.log.info("[插件 %s] %s" % (self.dir.name, msg))
        except Exception:
            pass


class Plugins:
    """`plugins/` 的加载器（带热重载与异常隔离）。"""

    def __init__(self, base_dir, log=None, sender=None):
        self.root = Path(base_dir) / DIRNAME
        self.log = log
        self._sender = sender
        self.loaded = []          # 插件名
        self.errors = []          # [(插件名, 原因)]
        self._apis = []
        self._stamp = None

    # -- 内部 ---------------------------------------------------------------
    def _dir_stamp(self):
        if not self.root.is_dir():
            return None
        out = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            py = d / "plugin.py"
            try:
                st = py.stat()
                out.append((d.name, st.st_mtime, st.st_size))
            except Exception:
                continue
        return tuple(out)

    def _load_one(self, d):
        api = _Api(d, self.log, self._sender)
        py = d / "plugin.py"
        try:
            spec = importlib.util.spec_from_file_location(
                "wxplugin_%s" % d.name, str(py))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)          # type: ignore[union-attr]
            reg = getattr(mod, "register", None)
            if not callable(reg):
                raise RuntimeError("plugin.py 里没有 register(api)")
            reg(api)
            self._apis.append(api)
            self.loaded.append(d.name)
        except Exception as exc:
            self.errors.append((d.name, "%s: %s" % (type(exc).__name__, exc)))
            try:
                if self.log is not None:
                    self.log.warning("插件 %s 没加载起来：%s" % (d.name, exc))
                    self.log.warning(traceback.format_exc(limit=2))
            except Exception:
                pass

    # -- 对外 ---------------------------------------------------------------
    def load_if_changed(self) -> bool:
        """目录变了就重新加载（返回 True = 这次真的重载了）。"""
        stamp = self._dir_stamp()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        self._apis, self.loaded, self.errors = [], [], []
        if not self.root.is_dir():
            return False
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and not d.name.startswith((".", "_")) and (d / "plugin.py").is_file():
                self._load_one(d)
        try:
            if self.log is not None and (self.loaded or self.errors):
                self.log.info("插件已重载：%s%s"
                              % ("、".join(self.loaded) or "（无）",
                                 ("；失败 %d 个" % len(self.errors)) if self.errors else ""))
        except Exception:
            pass
        return True

    def run_message(self, text, sender="", is_self=False):
        """让每个插件的钩子过一遍。返回第一个非空字符串；没人管就 None。

        ⚠️ 每个钩子单独兜异常 —— 插件写坏了不能把回复链路带崩。
        """
        for api in self._apis:
            for fn in api.hooks:
                try:
                    got = fn(text, sender, is_self)
                except Exception as exc:
                    try:
                        if self.log is not None:
                            self.log.warning("[插件 %s] 钩子出错：%r"
                                             % (api.dir.name, exc))
                    except Exception:
                        pass
                    continue
                if got:
                    t = str(got).strip()
                    if t:
                        return t
        return None

    def tools(self):
        """插件注册的工具（列表，每项是个 dict）。"""
        out = []
        for api in self._apis:
            out.extend(api.tools)
        return out
