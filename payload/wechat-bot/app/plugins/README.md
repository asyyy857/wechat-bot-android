# plugins/ —— 插件目录

一个子目录 = 一个插件，必须有 `plugin.py`，里面写 `register(api)`：

```python
def register(api):
    @api.on_message                       # 每条群消息过一遍（本地、零 token）
    def _hello(text, sender, is_self):
        if (text or "").strip() == "现在几点":
            return "现在是 " + api.now().strftime("%H:%M")
        return None                       # None = 不管这条

    api.add_tool("dice", "掷骰子", "有人要掷骰子的时候",
                 lambda text: api.send("🎲 " + str(api.random().randint(1, 6))))
```

- `api.on_message(fn)`：返回字符串就回这句；返回 `None` 就交给下一个人。
- `api.add_tool(名字, 干什么, 什么时候用, handler, owner_only=True, cooldown=0, max_per_day=0)`
  —— 名字要**短、全小写**（模型只回这个名字）。
- `api.send(文本)`：直接往群里发（走机器人那套节流/校验）。
- `api.now()` / `api.random()` / `api.dir`（本插件目录）/ `api.log_line(文本)`。

⚠️ 三条：
1. **改完不用重启机器人**（目录 mtime 变了自动重载，跟 `rules.yaml` 一样）；
2. 插件**跑在机器人进程里**，权限跟机器人一样（能发消息、能读文件）——只放自己信得过的代码；
3. 插件抛异常**不会**影响机器人回复（每个钩子单独兜住，只进日志）。
