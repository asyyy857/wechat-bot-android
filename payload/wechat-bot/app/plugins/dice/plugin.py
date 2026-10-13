# -*- coding: utf-8 -*-
"""示例插件：掷骰子 / 比大小。

它演示两件事（想写自己的插件，照这个改就行）：
  1. `on_message` —— **零 Token** 的本地回应（"掷骰子"这种话不用动模型）；
  2. `add_tool` —— 把能力**并进工具表**，让模型在一句话里自己决定要不要用。

⚠️ 插件跑在机器人进程里，权限和机器人一样（能发消息）—— 只放自己信得过的代码。
"""


def register(api):
    @api.on_message
    def _dice(text, sender, is_self):
        t = (text or "").strip()
        if t in ("掷骰子", "掷个骰子", "摇骰子", "扔骰子", "骰子"):
            return "🎲 %d 点" % api.random().randint(1, 6)
        return None

    def _handler(text):
        return api.send("🎲 %d 点" % api.random().randint(1, 6))

    api.add_tool("dice", "掷一个骰子",
                 "有人想掷骰子 / 比大小 / 要个随机数的时候",
                 _handler, owner_only=True, cooldown=3.0, max_per_day=100)
