# -*- coding: utf-8 -*-
"""安全阀：紧急停止、出站内容校验、发送自检、连续失败熔断。

存在的理由很直接 —— 一个会自动往群里发东西的机器人，必须有一个你**随时
能按住它的开关**，以及它自己发现不对时主动闭嘴的能力。

三层防护，从外到内：

1. :class:`KillSwitch`  —— 急停。全局热键 / 急停文件 / 手动，按一下就停。
2. :func:`validate_outgoing` —— 发之前看这句话像不像乱码，像就不发。
3. :class:`FailureBreaker`  —— 连续发不出去就自己停，别在那儿死循环刷屏。
"""

from __future__ import annotations

import ctypes
import re
import threading
from pathlib import Path
from typing import Callable, Optional

# 全局热键是 Windows 专有（RegisterHotKey）。手机端（Linux）没有它，
# 急停退化成「急停文件 + Ctrl+C」两条路，其余安全机制完全不变。
try:
    import ctypes.wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    IS_WINDOWS = True
except Exception:              # pragma: no cover
    wt = None
    user32 = None
    kernel32 = None
    IS_WINDOWS = False

# ---------------------------------------------------------------- 全局热键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN, "super": MOD_WIN}

_VK_SPECIAL = {"esc": 0x1B, "escape": 0x1B, "space": 0x20,
               "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
               "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
               "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}


if IS_WINDOWS:
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                    ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                    ("time", wt.DWORD), ("pt", _POINT)]
else:
    _POINT = _MSG = None


def parse_hotkey(spec: str):
    """``"ctrl+alt+q"`` -> ``(mods, vk)``；解析不了返回 ``None``。"""
    if not spec:
        return None
    parts = [p.strip().lower() for p in str(spec).replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            key = p
    if key is None:
        return None
    if key in _VK_SPECIAL:
        vk = _VK_SPECIAL[key]
    elif len(key) == 1:
        vk = ord(key.upper())
    else:
        return None
    return mods | MOD_NOREPEAT, vk


class HotkeyListener:
    """在后台线程里注册一个全局热键。

    ``RegisterHotKey`` 绑定的是**线程**的消息队列，所以必须在自己的线程里
    注册并且自己跑消息循环。
    """

    def __init__(self, spec: str, callback: Callable[[], None],
                 hotkey_id: int = 0xB1B0) -> None:
        self.spec = spec
        self.callback = callback
        self.hotkey_id = hotkey_id
        self.thread: Optional[threading.Thread] = None
        self.thread_id: Optional[int] = None
        self.registered = False
        self.error = ""

    def start(self) -> bool:
        if not IS_WINDOWS:
            self.error = "当前平台没有全局热键（非 Windows），用急停文件或 Ctrl+C"
            return False
        parsed = parse_hotkey(self.spec)
        if parsed is None:
            self.error = f"热键写法看不懂：{self.spec!r}"
            return False
        self._mods, self._vk = parsed
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="hotkey-listener")
        self.thread.start()
        # 等它注册完（最多 1 秒）
        for _ in range(100):
            if self.registered or self.error:
                break
            threading.Event().wait(0.01)
        return self.registered

    def _run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self.hotkey_id, self._mods, self._vk):
            err = ctypes.get_last_error()
            self.error = (f"全局热键 {self.spec} 注册失败（错误码 {err}），"
                          f"可能被别的程序占用了")
            return
        self.registered = True
        try:
            msg = _MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, self.hotkey_id)

    def stop(self) -> None:
        if IS_WINDOWS and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- 出站校验
# 替换字符、ASCII 控制符、Unicode 私有区 —— 这些出现在要发的话里就是出问题了
_SUSPECT = re.compile(
    "[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ue000-\uf8ff]")
# 一个汉字/字母/数字都没有，全是符号
_NO_WORD = re.compile(r"^[^\w\u4e00-\u9fff]+$", re.UNICODE)
# 允许当成回复发出去的纯标点 —— 人设里明写「不知道怎么接就回一个？或者 6」，
# 「6」是数字本来就过得去，反倒是「？」被 _NO_WORD 拦死了：2026-10-03 群里有人发
# 「…吗？」，规则「问号」明明命中，回复却在出站校验这儿被吃掉（机器人等于哑巴）。
# 问号和省略号本来就是正常人类回复，放行；但限长，免得甩一屏符号出去。
_PUNCT_OK = set("？?！!。.、，,～~…—－-:：;；'\"“”‘’()（）[]【】<>《》/\\|+*= ")
_PUNCT_MAX = 6


# ---------------------------------------------------------------- 「喵」后缀
# 2026-10-04 用户要求：「每句话，所有机器人的自动回复，都加喵」。
# 于是原来的两条例外（纯符号 / 纯数字）**已取消** —— 「？」现在发「？喵」，
# 「6」现在发「6喵」，人设（猫娘 / 贴吧老哥）一律共用这一层。
# 只剩两种情况不加：
#   1) 已经以「喵」结尾（不重复加）
#   2) 加了会超过 max_reply_chars —— 宁可不加，也别让一条本来发得出去的回复
#      被长度校验拦下
# 注意 `-648` 带负号，`"-648".isdigit()` 是 False，**不算纯数字**，实际发出
# 「-648喵」，这是包里的预期行为，别"修"它。
MEOW_SUFFIX = "喵"


# ★ 2026-10-09 用户要求：「给机器人加上动作描写吧，就他说一句话，然后再由他
#   决定要不要加一段动作，用括号写在里边，比如什么舔舔爪子这种东西」。
# 动作由模型自己决定加不加（规矩写在 personas/猫娘.md 的「括号小动作」那节），
# 但**发送层要保证「喵」补在括号前面** —— 否则会变成「（舔舔爪子）喵」，
# 动作后面又拖一条尾巴，看着就是坏的。
# 匹配句尾的括号（全角/半角都认），里面 1~20 个字符、不许嵌套。
_TAIL_ACTION = re.compile(r"[（(][^（()）]{1,20}[)）]\s*$")

# ★ 动作形态统一（2026-10-09 上线当天真机暴露）：模型**不总是**照人设写
#   「句尾全角圆括号」。真机上已经漏出去过一条：
#       「芋圆软乎乎的 口感有点像本喵唔[尾巴尖抖了抖]喵」
#   —— 半角方括号，而且动作卡在「喵」**前面**。RP 语料里 `[ ]` / `* *` 太常见，
#   光靠人设管不住模型的排版习惯，所以**发送层统一收口**：不管它写成
#   `[动作]`、`【动作】`、`*动作*`，还是把圆括号写在「喵」前面，一律整成
#   「台词喵（动作）」。
_ACTION_BRACKETS = re.compile(
    r"[\[【]([^\[\]【】\n]{1,12})[\]】]"          # [动作] / 【动作】
    r"|\*([^*\n]{1,12})\*")                     # *动作*
# 微信自己的占位符，别当动作转（模型偶尔会把它们写进回复）
_ACTION_STOP = {"图片", "表情", "动画表情", "语音", "视频", "文件", "链接",
                "转账", "红包", "位置", "名片", "聊天记录", "引用", "动画"}
# 动作被写在「喵」前面：X（动作）喵 → X喵（动作）
# ⚠️ 只认"括号紧挨着喵"的形状，句首那类（「（把脸埋起来）…不说这个喵」）
#    中间隔着别的字，不会被它碰到 —— 那是人设里明写的特例，要原样保留。
_ACTION_BEFORE_MEOW = re.compile(r"^(.*)（([^（）\n]{1,12})）(喵+)$")


def normalize_action(text: str) -> str:
    """把动作描写统一成「台词喵（动作）」。

    只做形态归一，不改一个字的内容：方括号/【】/星号 → 全角圆括号，并挪到句尾
    （挪到句尾是因为人设要求动作在句尾；排在「喵」后面由 :func:`with_meow` 保证）。
    句首的圆括号动作**不动** —— 人设里的「（把脸埋起来）…不说这个喵」是特例。
    """
    t = (text or "").strip()
    if not t:
        return t
    m = _ACTION_BRACKETS.search(t)
    if m:
        act = (m.group(1) or m.group(2) or "").strip()
        if act and act not in _ACTION_STOP:
            head = (t[:m.start()] + t[m.end():]).strip()
            head = re.sub(r"\s{2,}", " ", head)
            if head:
                t = head + "（%s）" % act
    t = _ACTION_BEFORE_MEOW.sub(r"\1\3（\2）", t)
    return t


def strip_action(text: str) -> str:
    """★ 2026-10-10 用户要求删掉括号动作：把**句尾**那个括号整段去掉。

    用户原话：「大家都在聊天，结果你出来一个动作真的很奇怪很违和」→
    「把那些括号后面的动作删掉吧」。

    ⚠️ **人设是软约束**（今天实测：模型被要求"三四句带一次"，实际几乎每条都带），
    所以真正的硬保证在**发送层**：不问人设、统一剥掉。
    只剥**句尾**那一个（1~20 字）；句首/句中的括号不动（那可能是正常内容，
    比如人设里「（把脸埋起来）…不说这个喵」那类特例）。

    返回空串 = 整句只有一个动作 → 调用方应当**不发**。
    """
    t = (text or "").strip()
    if not t:
        return t
    t = normalize_action(t)      # ★ 先归一形态：[歪头] / *甩尾巴* → 句尾（歪头）
    m = _TAIL_ACTION.search(t)
    if not m:
        return t
    return t[:m.start()].rstrip()


def with_meow(text: str, limit: Optional[int] = None,
              no_meow: bool = False) -> str:
    """给要发的文字追加「喵」；只有「已经带喵 / 加了会超长」两种例外。

    ``no_meow=True`` 仍然可以显式要求原样发（规则表的 ``no_meow`` 字段），
    但 2026-10-04 用户要求全局带喵之后，``rules.yaml`` 里已经不再用它了。

    ★ 句尾带括号小动作时，「喵」补在**括号前面**：
    ``本喵才不稀罕（甩了甩尾巴）`` → ``本喵才不稀罕喵（甩了甩尾巴）``。
    """
    if not text:
        return text
    t = normalize_action(text)       # 0) 动作形态先收口（方括号→圆括号、挪到句尾）
    if no_meow:                      # 0.5) 规则显式要求不加喵（当前无人使用）
        return t
    if not t:
        return t
    m = _TAIL_ACTION.search(t)
    if m and m.start() > 0:               # 1) 句尾有括号小动作
        head = t[:m.start()].rstrip()
        act = t[m.start():].strip()
        if head.endswith(MEOW_SUFFIX):    # 主体已经带喵 → 一个字都不动
            return head + act
        if limit and len(head) + len(act) + len(MEOW_SUFFIX) > int(limit):
            return t                      # 加了会超长 → 宁可不加
        return head + MEOW_SUFFIX + act
    if t.endswith(MEOW_SUFFIX):          # 2) 已经带喵
        return t
    if limit and len(t) + len(MEOW_SUFFIX) > int(limit):   # 3) 加了超长
        return t
    return t + MEOW_SUFFIX


def validate_outgoing(text: str, max_chars: int = 40) -> Optional[str]:
    """发之前自检。返回 ``None`` 表示可以发，否则返回拒绝理由。"""
    if not text or not text.strip():
        return "内容为空"
    if len(text) > max_chars:
        return f"太长（{len(text)} 字 > {max_chars}）"
    bad = _SUSPECT.findall(text)
    if bad:
        codes = " ".join(f"U+{ord(c):04X}" for c in bad[:4])
        return f"含异常字符（{codes}）"
    if _NO_WORD.match(text):
        stripped = text.strip()
        if len(stripped) <= _PUNCT_MAX and all(c in _PUNCT_OK for c in stripped):
            return None                  # 纯标点短回复（「？」「…」）放行
        return "没有实际文字内容"
    return None


class FailureBreaker:
    """连续失败熔断：连着几次发不出去就别再试了。"""

    def __init__(self, limit: int = 3) -> None:
        self.limit = max(1, int(limit))
        self.streak = 0
        self.total_failures = 0

    def record_failure(self) -> bool:
        """记一次失败，返回是否已触发熔断。"""
        self.streak += 1
        self.total_failures += 1
        return self.streak >= self.limit

    def record_success(self) -> None:
        self.streak = 0


# ---------------------------------------------------------------- 急停
class KillSwitch:
    """急停开关。

    触发来源有三个：
      * 全局热键（默认 Ctrl+Alt+Q）
      * 急停文件（默认 ``STOP.txt`` 一出现就停）—— 适合远程 / 脚本
      * 代码里手动 :meth:`trigger`（熔断器用它）
    """

    def __init__(self, cfg, logger=None) -> None:
        sec = getattr(cfg, "safety", None)
        self.hotkey_spec = str(getattr(sec, "hotkey", "") or "") if sec else ""
        self.fallbacks = [str(x) for x in
                          (getattr(sec, "hotkey_fallbacks", None) or [])] if sec else []
        stop_file = str(getattr(sec, "stop_file", "") or "") if sec else ""
        self.stop_file = Path(stop_file) if stop_file else None
        self.logger = logger

        self.stopped = False
        self.reason = ""
        self.listener: Optional[HotkeyListener] = None
        self.active_hotkey = ""

    # ---- 触发 ----
    def trigger(self, reason: str) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.reason = reason
        self._log(f"!!! 急停已触发：{reason} !!!")

    def _log(self, msg: str, level: str = "warning") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)

    # ---- 启动热键 ----
    def arm(self) -> None:
        """注册急停热键。首选被占用就依次试候补，全失败也不致命 —— 还有急停文件。"""
        if not IS_WINDOWS:
            # 手机（容器里跑的这个）根本没有全局热键这套东西。以前这里会拿 5 个候选
            # 逐个试一遍、每个都往日志里糊一行「当前平台没有全局热键（非 Windows）」，
            # 用户看到的是一坨没用的红字 —— 2026-10-04 明确要求别再刷了。
            # 静默收工，急停方式由 bot.py 启动时那行「急停方式：…」负责说明。
            return
        candidates = [self.hotkey_spec] + self.fallbacks
        candidates = [c for c in candidates if c and c.strip()]
        if not candidates:
            return

        errors = []
        for spec in candidates:
            listener = HotkeyListener(
                spec, lambda s=spec: self.trigger(f"你按了 {s}"))
            if listener.start():
                self.listener = listener
                self.active_hotkey = spec
                note = "（首选被占用，自动降级）" if spec != self.hotkey_spec else ""
                self._log(f"急停热键已就绪：{spec}  <- 按一下立刻停 {note}", "info")
                return
            errors.append(f"{spec} —— {listener.error}")

        self._log("急停热键一个都没注册上（换个组合键即可），"
                  "现在只能靠急停文件 / Ctrl+C：\n      " + "\n      ".join(errors),
                  "warning")

    def disarm(self) -> None:
        if self.listener:
            self.listener.stop()

    # ---- 每轮检查 ----
    def check(self) -> Optional[str]:
        """该不该停？返回停止原因，``None`` 表示继续。"""
        if self.stopped:
            return self.reason
        if self.stop_file is not None:
            try:
                if self.stop_file.exists():
                    self.trigger(f"发现急停文件 {self.stop_file.name}")
                    return self.reason
            except OSError:
                pass
        return None
