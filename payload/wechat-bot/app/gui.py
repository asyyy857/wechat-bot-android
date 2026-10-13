# -*- coding: utf-8 -*-
"""图形界面 —— 给不想碰命令行的人用的启动面板。

双击桌面上的「微信机器人」快捷方式就打开这个：
上面填 API Key，下面点「启动」，完事。

技术上就是 tkinter（Python 标准库，不用装任何东西）。
机器人在后台线程里跑，这个窗口只管显示状态和控制它。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wxauto.bot import Bot                       # noqa: E402
from wxauto.config import load_config            # noqa: E402
from wxauto.settings import get_value, set_value  # noqa: E402

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

# 界面字号基准（pt）。
# tk 会按屏幕 DPI 自动放大，所以这里是"逻辑字号" —— 高分屏不用手动调大，
# 真正决定观感的是 DPI 有没有开对（见文件末尾的 main）。
# 整体嫌小就把 BASE 调大（12、13），嫌大就调小。
BASE = 11

F_TITLE = (FONT, BASE + 7, "bold")
F_LABEL = (FONT, BASE)
F_INPUT = (FONT, BASE)
F_INPUT_MONO = (MONO, BASE)
F_MONO = (MONO, BASE - 1)
F_HINT = (FONT, BASE - 1)
F_LINK = (FONT, BASE - 1, "underline")
F_STATUS = (FONT, BASE + 3, "bold")

GREEN = "#07C160"
RED = "#D93025"
GRAY = "#8A8A8A"
ORANGE = "#E8A33D"

APPLY_URL = "https://platform.deepseek.com/api_keys"


def _setup_fonts(root: tk.Tk) -> None:
    """把 ttk 的默认字体也换掉。

    按钮、复选框、「设置」这种 LabelFrame 标题走的是 TkDefaultFont，
    不设的话它们不会跟着变大，界面就会字体大小不统一。
    """
    import tkinter.font as tkfont

    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                 "TkHeadingFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name).configure(family=FONT, size=BASE)
        except Exception:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=MONO, size=BASE - 1)
    except Exception:
        pass


class _QueueHandler(logging.Handler):
    """把机器人线程的日志丢进队列，交给界面线程显示。"""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.bot: Bot | None = None
        self.worker: threading.Thread | None = None
        self.log_q: queue.Queue = queue.Queue()
        self._qh: _QueueHandler | None = None

        root.title("微信自动回复机器人")
        _setup_fonts(root)
        root.geometry("1120x900")
        root.minsize(980, 800)

        self._build_ui()
        self._load_settings()

        # 界面一动就把日志队列抽干
        self._poll_log()
        self._tick()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        try:
            ttk.Style().theme_use("vista")
        except Exception:
            pass

        head = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        head.pack(fill="x")
        ttk.Label(head, text="微信自动回复机器人",
                  font=F_TITLE).pack(anchor="w")
        ttk.Label(head, text="填好 API Key → 点「启动」。机器人会盯着你的群，"
                             "关键词直接接梗，@你 才动用模型。",
                  font=F_HINT, foreground=GRAY).pack(anchor="w", pady=(3, 0))

        # ---------------- 设置 ----------------
        box = ttk.LabelFrame(self.root, text=" 设置 ", padding=(12, 8, 12, 10))
        box.pack(fill="x", padx=16, pady=(6, 0))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="API Key", width=9, font=F_LABEL).pack(side="left")
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(row, textvariable=self.key_var, show="●",
                                   font=F_INPUT_MONO)
        self.key_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row, text="显示", width=6,
                   command=self._toggle_key_visible).pack(side="left")
        ttk.Button(row, text="测试", width=6,
                   command=self.test_api).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="保存", width=6,
                   command=self.save_settings).pack(side="left", padx=(4, 0))

        hint = ttk.Frame(box)
        hint.pack(fill="x")
        ttk.Label(hint, text="没有 Key？", font=F_HINT,
                  foreground=GRAY).pack(side="left", padx=(62, 2))
        link = ttk.Label(hint, text="点这里去申请（免费注册，充 10 块能用很久）",
                         font=F_LINK, foreground="#1A73E8",
                         cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda _e: webbrowser.open(APPLY_URL))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(8, 3))
        ttk.Label(row2, text="目标群", width=9, font=F_LABEL).pack(side="left")
        self.group_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.group_var, font=F_LABEL).pack(
            side="left", fill="x", expand=True)
        ttk.Label(box, text="填群名（不用填全，能认出来就行）。只有停在这个群里才会工作。",
                  font=F_HINT, foreground=GRAY).pack(anchor="w", padx=(62, 0))

        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="演练模式 —— 只记录它「本来想说什么」，一条都不真发",
                        variable=self.dry_var).pack(anchor="w", pady=(8, 0))

        # ---------------- 控制 ----------------
        ctl = ttk.Frame(self.root, padding=(16, 12, 16, 4))
        ctl.pack(fill="x")

        self.start_btn = ttk.Button(ctl, text="▶   启 动", width=14,
                                    command=self.start_bot)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctl, text="■   停 止", width=14,
                                   command=self.stop_bot, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        # 急停按钮。和「停止」的区别：它写的是**急停文件**，所以不管机器人
        # 是这个面板启动的、还是在命令行里跑的，点一下都能停住。
        self.estop_btn = tk.Button(ctl, text="急  停", width=10,
                                   font=(FONT, BASE + 1, "bold"),
                                   bg=RED, fg="white",
                                   activebackground="#B3261E",
                                   activeforeground="white",
                                   relief="raised", bd=2, cursor="hand2",
                                   command=self.emergency_stop)
        self.estop_btn.pack(side="left", padx=(18, 0))
        self.resume_btn = ttk.Button(ctl, text="解除急停", width=10,
                                     command=self.clear_estop, state="disabled")
        self.resume_btn.pack(side="left", padx=(6, 0))

        self.status_var = tk.StringVar(value="● 未启动")
        self.status_lbl = tk.Label(ctl, textvariable=self.status_var,
                                   font=F_STATUS, fg=GRAY)
        self.status_lbl.pack(side="left", padx=(18, 0))

        ttk.Button(ctl, text="使用说明", command=self._open_memo).pack(
            side="right", padx=(0, 6))
        ttk.Button(ctl, text="打开日志", command=self._open_log).pack(side="right")
        ttk.Button(ctl, text="打开文件夹", command=self._open_folder).pack(
            side="right", padx=(0, 6))

        self.estop_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.estop_var, font=F_HINT,
                  foreground=ORANGE).pack(anchor="w", padx=16)

        self.stats_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.stats_var, font=F_HINT,
                  foreground=GRAY, justify="left").pack(anchor="w", padx=16, pady=(4, 0))

        # ---------------- 日志 ----------------
        logf = ttk.LabelFrame(self.root, text=" 运行日志 ", padding=(8, 6, 8, 8))
        logf.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        self.log_text = ScrolledText(logf, height=16, font=F_MONO,
                                     state="disabled", wrap="word",
                                     background="#FBFBFB")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------ 配置
    def _load_settings(self) -> None:
        try:
            self.key_var.set(str(get_value("api_key", default="") or ""))
            self.group_var.set(str(get_value("group", default="") or ""))
        except Exception as exc:
            self._append_log(f"读配置失败：{exc}")

    def save_settings(self, silent: bool = False) -> bool:
        try:
            ok1 = set_value("api_key", self.key_var.get().strip())
            ok2 = set_value("group", self.group_var.get().strip())
        except Exception as exc:
            messagebox.showerror("保存失败", f"写配置出错：{exc}")
            return False
        if not (ok1 and ok2):
            messagebox.showerror("保存失败", "config.yaml 里没找到对应的设置行，"
                                             "可能文件被改坏了。")
            return False
        if not silent:
            self._append_log("设置已保存到 config.yaml")
            messagebox.showinfo("已保存", "设置保存好了。\n\n"
                                          "（如果你正在运行，改动要下次启动才生效）")
        return True

    def _toggle_key_visible(self) -> None:
        self.key_entry.config(show="" if self.key_entry.cget("show") else "●")

    # ------------------------------------------------------------ 测试 Key
    def test_api(self) -> None:
        key = self.key_var.get().strip()
        if not key:
            messagebox.showwarning("还没填", "先把 API Key 粘到上面的框里。\n\n"
                                             "没有的话点下面那行蓝色链接去申请。")
            return
        if not self.save_settings(silent=True):
            return
        self._append_log("正在测试 API Key…")
        self._set_status("● 测试中…", ORANGE)

        def work() -> None:
            try:
                cfg = load_config()
                from wxauto.brain import Brain
                brain = Brain(cfg, "", None)
                if not brain.available:
                    self._post("api", (False, "配置里 llm.enabled 是 false，模型通道被关掉了"))
                    return
                body = brain._post({
                    "model": cfg.llm.model,
                    "messages": [{"role": "user", "content": "回复一个字：好"}],
                    "max_tokens": 8,
                    "stream": False,
                })
                if not body:
                    self._post("api", (False, "请求失败了。常见原因：\n"
                                              "· Key 填错了或复制时少了字符\n"
                                              "· 账号里没余额\n"
                                              "· 电脑连不上网"))
                    return
                text = body["choices"][0]["message"]["content"]
                usage = body.get("usage") or {}
                self._post("api", (True, str(text), usage))
            except Exception as exc:
                self._post("api", (False, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True, name="api-test").start()

    def _api_result(self, ok: bool, detail: str, usage: dict | None = None) -> None:
        self._set_status("● 未启动", GRAY)
        if ok:
            extra = f"\nToken 用量：{usage}" if usage else ""
            self._append_log(f"API Key 可用，模型回了：{detail}{extra}")
            messagebox.showinfo("测试通过", f"Key 能用了！\n\n模型回复：{detail}")
        else:
            self._append_log(f"测试失败：{detail}")
            messagebox.showerror("测试失败", detail)

    # ------------------------------------------------------------ 启停
    def start_bot(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.key_var.get().strip():
            if not messagebox.askokcancel(
                    "没填 API Key",
                    "你还没填 API Key。\n\n"
                    "没有 Key 的话：关键词（看看腿 / 萝莉 / 6 …）照样会回，\n"
                    "但别人 @你 的时候它不会说话。\n\n"
                    "现在就用吗？"):
                return
        if not self.save_settings(silent=True):
            return

        # 上次急停留下的文件还在的话，机器人一启动就会立刻停下
        if self._estop_active():
            if not messagebox.askokcancel(
                    "急停文件还在",
                    "上次急停留下的急停文件还在。\n\n"
                    "不删掉它，机器人启动后会立刻停止、什么都不干。\n\n"
                    "现在删掉它并继续启动吗？"):
                return
            self.clear_estop()

        try:
            cfg = load_config()
            self.bot = Bot(cfg, dry_run=self.dry_var.get())
        except Exception as exc:
            messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}")
            return

        self._attach_log_handler()
        self.worker = threading.Thread(target=self._run_bot, daemon=True, name="bot")
        self.worker.start()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        mode = "演练中" if self.dry_var.get() else "运行中"
        self._set_status(f"● {mode}", ORANGE if self.dry_var.get() else GREEN)

    def _run_bot(self) -> None:
        try:
            self.bot.run()
        except Exception as exc:
            self._post("log", f"!! 机器人异常退出：{type(exc).__name__}: {exc}")
        finally:
            # 这里跑在机器人线程上，**绝对不能**碰 tkinter 对象
            # （root.after 不是线程安全的，会抛 "main thread is not in main loop"，
            #  结果就是按钮状态永远卡住、再也启动不了）。只能往队列里丢。
            self._post("stopped")

    def _on_bot_stopped(self) -> None:
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._set_status("● 已停止", GRAY)
        self.estop_var.set("")

    def stop_bot(self) -> None:
        if self.bot:
            self.bot.kill.trigger("你在界面上点了「停止」")
        self._set_status("● 正在停止…", ORANGE)
        self._append_log("已发送停止指令，等它把当前这一轮走完…")

    # ------------------------------------------------------------ 急停
    def _stop_files(self) -> list[Path]:
        """急停文件可能出现的位置。

        config 里写的是相对路径（``STOP.txt``），解析基准是**进程的工作目录** ——
        机器人可能是这个面板启动的，也可能是在 wechat-auto 目录里用 run.ps1
        启动的。两处都写上，才不会出现"点了按钮却没反应"。
        """
        paths = [ROOT / "STOP.txt"]
        try:
            if self.bot and self.bot.kill.stop_file is not None:
                p = Path(self.bot.kill.stop_file)
                paths.append(p if p.is_absolute() else Path.cwd() / p)
        except Exception:
            pass
        return list(dict.fromkeys(paths))

    def _estop_active(self) -> bool:
        for p in self._stop_files():
            try:
                if p.exists():
                    return True
            except OSError:
                pass
        return False

    def emergency_stop(self) -> None:
        """一键急停：建急停文件，顺带停掉本面板启动的那个机器人。"""
        written = []
        for p in self._stop_files():
            try:
                p.write_text("stop", encoding="utf-8")
                written.append(str(p))
            except Exception as exc:
                self._append_log(f"急停文件写不了（{p}）：{exc}")
        if self.bot:
            try:
                self.bot.kill.trigger("你在界面上点了「急停」")
            except Exception:
                pass

        if not written:
            messagebox.showerror("急停失败",
                                 "急停文件一个都没写成功。\n\n"
                                 "请改用桌面上的「急停」图标，或直接关掉窗口。")
            return

        self._append_log("!!! 已急停：" + "、".join(written))
        self._set_status("● 已急停", RED)
        self.resume_btn.config(state="normal")
        messagebox.showwarning(
            "已急停",
            "机器人不会再发任何消息了。\n\n"
            "· 它还在读屏，但什么都不做\n"
            "· 想彻底结束进程：点「停止」\n"
            "· 想恢复工作：点「解除急停」，再点「启动」")

    def clear_estop(self) -> None:
        """解除急停：把急停文件删掉，让**下一次启动**能正常工作。

        急停是不可逆的 —— KillSwitch 一旦 stopped 就不会复位，所以已经停在
        半路的那个机器人不会自己活过来，必须重新启动。
        """
        bad = []
        for p in self._stop_files():
            try:
                if p.exists():
                    p.unlink()
            except Exception as exc:
                bad.append(f"{p}：{exc}")
        if bad:
            messagebox.showerror("解除失败", "删不掉急停文件：\n" + "\n".join(bad))
            return

        self.resume_btn.config(state="disabled")
        self._append_log(f"急停文件已删掉（{ROOT / 'STOP.txt'}）。")
        if self.worker and self.worker.is_alive():
            self._append_log("但机器人已经进入急停状态，不会自己恢复 —— "
                             "点「停止」再点「启动」才会重新干活。")

    def _attach_log_handler(self) -> None:
        log = logging.getLogger("wxauto")
        if self._qh is None:
            self._qh = _QueueHandler(self.log_q)
            log.addHandler(self._qh)
        log.setLevel(logging.INFO)

    # ------------------------------------------------------------ 定时
    def _poll_log(self) -> None:
        """在**界面线程**里把后台线程发来的消息处理掉。

        tkinter 不是线程安全的：机器人线程只允许往队列里丢东西，
        所有碰控件的动作都必须回到这里做。
        """
        try:
            while True:
                kind, payload = self.log_q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "stopped":
                    self._on_bot_stopped()
                elif kind == "api":
                    self._api_result(*payload)
        except queue.Empty:
            pass
        except ValueError:
            # 兼容旧格式（纯字符串）
            pass
        self.root.after(150, self._poll_log)

    def _tick(self) -> None:
        running = bool(self.bot and self.worker and self.worker.is_alive())
        if running:
            try:
                self.stats_var.set(f"{self.bot.governor.stats()}\n{self.bot.brain.usage()}")
            except Exception:
                pass
        self._refresh_estop(running)
        self.root.after(2000, self._tick)

    def _refresh_estop(self, running: bool) -> None:
        """把"现在是不是急停状态"画到界面上。"""
        try:
            active = self._estop_active()
        except Exception:
            active = False

        self.resume_btn.config(state="normal" if active else "disabled")
        if active:
            self.estop_var.set("⚠ 急停生效中（急停文件还在）—— 点「解除急停」，"
                               "再重新点「启动」才会继续工作")
            return

        stops = ["点红色「急停」按钮"]
        if running and self.bot:
            try:
                if self.bot.kill.active_hotkey:
                    stops.append(f"按 {self.bot.kill.active_hotkey}")
                if self.bot.kill.stop_file is not None:
                    stops.append(f"新建 {self.bot.kill.stop_file.name}")
            except Exception:
                pass
        self.estop_var.set("急停：" + "  /  ".join(stops))

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_lbl.config(fg=color)

    def _append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line.rstrip() + "\n")
        # 别让它无限长下去
        if int(self.log_text.index("end-1c").split(".")[0]) > 800:
            self.log_text.delete("1.0", "200.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _post(self, kind: str, payload=None) -> None:
        """从后台线程往界面线程传消息。队列是唯一安全的通道。"""
        self.log_q.put((kind, payload))

    # ------------------------------------------------------------ 杂项
    def _open_folder(self) -> None:
        try:
            os.startfile(str(ROOT))
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))

    def _open_log(self) -> None:
        p = ROOT / "bot.log"
        if not p.exists():
            messagebox.showinfo("还没有日志", "机器人还没跑过，所以还没有 bot.log。")
            return
        try:
            os.startfile(str(p))
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))

    def _open_memo(self) -> None:
        """打开备忘录 —— 人设和关键词在哪个文件改，都写在里面。"""
        p = ROOT / "备忘录.txt"
        if not p.exists():
            messagebox.showinfo("找不到", "备忘录.txt 不在了，可能被误删。")
            return
        try:
            os.startfile(str(p))
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("还在运行", "机器人还在跑，确定要退出吗？"):
                return
            if self.bot:
                self.bot.kill.trigger("关闭了界面")
            self.worker.join(timeout=3)
        self.root.destroy()


def main() -> None:
    # 先统一工作目录。配置里的 STOP.txt / bot.log / debug_frames 都是相对路径，
    # 解析基准是进程的 cwd —— 从桌面快捷方式启动时 cwd 未必是本目录，
    # 那样桌面上的「急停」图标（写的是本目录下的 STOP.txt）就会失灵，
    # 日志也会落到别处去。
    try:
        os.chdir(ROOT)
    except Exception:
        pass

    # DPI 感知必须在**创建任何窗口之前**打开，否则 Windows 会先把尺寸按 96 DPI
    # 虚拟化，tk 之后再怎么设都没用 —— 整个界面会偏小。
    # 这里也不手动设 tk 的 scaling：tk 拿到真实 DPI 后会自己算出正确值
    # （150% 缩放对应 scaling≈2.0）。
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE
    except Exception:
        try:
            from ctypes import windll
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
