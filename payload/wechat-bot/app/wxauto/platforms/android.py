# -*- coding: utf-8 -*-
"""Android / DSH 手机端驱动：通过一个本地 HTTP 桥跟无障碍服务通信。

手机端只要提供三个端点（Auto.js、Kotlin、或你手机上的 DSH 都能写）：

    GET  /health
        -> {"ok": true}

    GET  /snapshot
        -> {"ok": true,
            "chat_title": "目标群名",
            "messages": [
                {"text": "看看腿",     "is_self": false},
                {"text": "我先测两天", "is_self": true}
            ]}
        messages 按时间从旧到新排列。is_self 表示这条是不是你自己发的。

    POST /send      body: {"text": "看看腿"}
        -> {"ok": true}

为什么用 HTTP 桥，而不是把整套 Python 搬到手机上
------------------------------------------------
手机端读消息走**无障碍服务**，拿到的是结构化节点文本 —— 不用 OCR，
100% 准确而且几乎零耗时，比 PC 上的截图识别强一个档次。
而"该不该回、回什么、多久回一次"这套逻辑已经写好了，没必要重写一遍。

用 HTTP 把两边拆开之后，逻辑层可以跑在手机上的 DSH 里，
也可以跑在电脑上遥控手机 —— 同一个 :class:`wxauto.bot.Bot` 不用改一行。

用法::

    from wxauto.bot import Bot
    from wxauto.config import load_config
    from wxauto.platforms.android import AndroidReader, AndroidSender

    cfg = load_config()
    url = "http://127.0.0.1:8765"
    Bot(cfg, reader=AndroidReader(url, cfg.target.group),
        sender=AndroidSender(url)).run()

要先用 PC 版本验证人设和节奏，再换这个 —— 两者只差注入的两行。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..reader import Block, Message, Snapshot, normalize


_TITLE_DECOR = "「」『』【】《》\"'“”‘’ \t"


def _strip_title_noise(shown: str) -> str:
    """剥掉标题栏的装饰引号和「群人数」尾巴。

    2026-10-03 实测：虚拟屏只有 636 宽，长群名的**中段**会被系统吃掉，末尾还捎上
    人数、连左括号都被截掉 —— 截图上是「人...111)」。老逻辑要求省略号前 ≥2 个汉字，
    于是永远判「不是目标群」，机器人一直「待命中」什么都不回（进程却好好的）。
    """
    s = (shown or "").strip().strip(_TITLE_DECOR).strip()
    # 人数尾巴实测四种形态：(111) / (111 / 111) / (111人) / 「人间...(111人)」
    # —— 只认带括号或「人/名」的，别把「群2024」这种真群名吃掉；
    #   也别把 `.` 写进数字字符类：那样会把省略号一起吃掉，只剩一个字的头部。
    s = re.sub(r"(?:[\(（]\s*\d+\s*(?:人|名)?\s*[\)）]?|"
               r"\d+\s*(?:人|名)?\s*[\)）])\s*$", "", s).strip()
    # 带省略号说明标题被截断过，后面挂的裸数字也是人数：'人间...111'
    if re.search(r"\.{2,}", s):
        s = re.sub(r"\d+\s*$", "", s).strip()
    return s.strip(_TITLE_DECOR).strip()


def title_matches(shown: str, target: str) -> bool:
    """会话标题匹配，容忍微信的「目标群...录」式截断。"""
    shown = _strip_title_noise(normalize(shown).replace("…", "..."))
    target = normalize(target)
    if not shown or not target:
        return False
    if shown in target or target in shown:
        return True
    # 2026-10-04：微信有时把群名压成「...111)」—— 只剩省略号+人数，一个中文
    # 都没有，这种标题没法辨识。但**不能一律放行**（原实现是 return True）：
    # 用户在手机上切到英文/符号标题的会话时，机器人会误以为还在目标群，并往
    # 那个会话里回消息。判据：必须有「被截断」的痕迹（省略号），且不能有连续
    # 两个以上英文字母 —— 真英文会话名（ChatGPT / OpenAI）会被否掉，而实测标题
    # 「...111) N」里那个孤立的 N 是标题栏 🔔 图标被 OCR 误读的噪声，得容忍。
    if not re.search(r"[\u4e00-\u9fff]", shown):
        return bool(re.search(r"\.{2,}", shown)) and not re.search(r"[A-Za-z]{2,}", shown)
    # OCR 把省略号读成 ".." / "..." / "…" 都见过，一律当通配符
    m = re.search(r"\.{2,}|…+", shown)
    if m:
        head, tail = shown[:m.start()].strip(), shown[m.end():].strip()
        if not head or not target.startswith(head):
            return False
        if tail:
            return target.endswith(tail)
        # 尾部剥干净了才敢放行单字 head（群名被截到只剩一个字），
        # 但目标群名得够长 —— 免得「人」这种单字把别的会话也认成目标群
        # ★ 2026-10-10 实测修正：原来卡的是 `len(target) >= 4`，**3 个字的
        #   群名会被这条一律判否**。用户把群备注改成「目标群窝」之后，微信标题栏
        #   变成两行（第一行「备注 + (人数)」、第二行原群名），两行都被虚拟屏的
        #   窄宽度压扁 —— 实测读到的是 `缺...10` / `「人间.`，第一行只剩一个
        #   「缺」字，于是机器人**认为自己不在目标群**、每 3 轮白切一次群
        #   （当天日志累计 135 次「连续 3 轮不在目标群」）。
        #   现在放宽成 `len(target) >= 2`：单字 head 仍然必须**正好是目标群名的
        #   开头**（上面 startswith 已经把着），误判只可能发生在"另一个会话的
        #   标题也被读成同一个字打头的省略号"——实际概率极低，而代价是完全不可用。
        # ⚠️ 这份判据在 read/wxreader.py::match_title 里还有一份（桥接校验切群用），
        #    改这里记得一起改 —— 两边不一致会出现"机器人认为在群里、桥接认为切错群"。
        return len(head) >= 2 or len(target) >= 2
    return False


_LOG = logging.getLogger("wxauto.android")


class _Bridge:
    """跟手机端 HTTP 服务通信的公共部分。"""

    # ★ 2026-10-12：硬超时宽限（秒）。urlopen 的 timeout 只管「单次套接字操作」，
    #   对端一滴一滴往外挤字节就能无限拖长一次请求；这里再加一道总时长上限
    #   （timeout + 宽限）。主循环是同步的，一次请求卡死 = 整个机器人卡死。
    HARD_SLACK = 5.0

    # 最近一次 _request 的结局，供调用方判断「发送键到底点没点过」：
    #   ok        拿到了回包
    #   not_sent  请求根本没送到桥接（连接被拒 / 网络不可达）→ 确定没执行
    #   rejected  桥接明确拒绝（4xx）→ 没执行
    #   uncertain 超时 / 断连 / 5xx → 桥接**可能已经执行了**（⚠️ 不能当成「没发」）
    last_req_kind = ""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.last_req_kind = ""

    def _fetch(self, path: str, payload: Optional[dict], timeout: float):
        url = self.base_url + path
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST")
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _classify(exc: BaseException) -> str:
        """把异常归成「确定没执行 / 不确定」。拿不准一律算不确定（宁可漏，不可重）。"""
        if isinstance(exc, urllib.error.HTTPError):
            return "rejected" if 400 <= exc.code < 500 else "uncertain"
        if isinstance(exc, urllib.error.URLError):
            r = getattr(exc, "reason", None)
            if isinstance(r, (ConnectionRefusedError, socket.gaierror)):
                return "not_sent"
            if isinstance(r, OSError) and getattr(r, "errno", None) in (
                    errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH):
                return "not_sent"
        return "uncertain"

    def _request(self, path: str, payload: Optional[dict] = None,
                 timeout: Optional[float] = None) -> Optional[dict]:
        to = float(timeout or self.timeout)
        self.last_req_kind = ""
        box: Dict[str, Any] = {}

        def work() -> None:
            try:
                box["data"] = self._fetch(path, payload, to)
            except BaseException as exc:          # 线程里的异常带回调用线程处理
                box["exc"] = exc

        th = threading.Thread(target=work, name="android-req", daemon=True)
        th.start()
        th.join(to + self.HARD_SLACK)
        if th.is_alive():
            # 套接字超时没生效（对端在慢速滴字节）：不再等，线程留作 daemon 自生自灭
            self.last_req_kind = "uncertain"
            _LOG.warning("[android] %s 硬超时（%.0fs + %.0fs 宽限仍无响应），放弃等待",
                         path, to, self.HARD_SLACK)
            return None
        exc = box.get("exc")
        if exc is None:
            self.last_req_kind = "ok"
            return box.get("data")
        self.last_req_kind = self._classify(exc)
        if isinstance(exc, urllib.error.HTTPError):
            _LOG.warning("[android] %s HTTP %s", path, exc.code)
        else:
            _LOG.warning("[android] %s 失败：%s: %s", path, type(exc).__name__, exc)
        return None


class AndroidReader(_Bridge):
    """实现 :class:`wxauto.drivers.Reader`。"""

    def __init__(self, base_url: str, target_group: str = "",
                 timeout: float = 30.0) -> None:
        super().__init__(base_url, timeout)
        self.target_group = target_group
        # 最后一次读屏失败的**真实原因**（桥接给的 error），给上层日志和面板用 ——
        # 老版本直接吞掉，上层只能打 PC 版那句「微信被最小化」，非常误导。
        self.last_error = ""

    def attach(self) -> int:
        data = self._request("/health")
        if not data or not data.get("ok"):
            raise RuntimeError(
                f"手机端桥接服务不可达：{self.base_url}/health。"
                f"确认无障碍服务已启动、端口没被占。")
        return 1

    def warmup(self) -> None:
        """手机端读的是结构化文本，没有模型要预热。"""
        return None

    def read(self) -> Optional[Snapshot]:
        # ★ 2026-10-10（识图功能）：读屏时**顺带把图片消息裁下来**（桥接在同一帧
        #   里裁，坐标才不会漂）。机器人要识图就必须走 images=1；面板/调试那些
        #   调用方（它们直接打桥接）不用带，省一次裁剪。
        data = self._request("/snapshot?images=1")
        if not data:
            # 桥接压根没回话（刚重启 / 端口被占）—— 跟"读到了但画面不对"是两回事
            self.last_error = "桥接没回话（/snapshot 请求失败）"
            return None
        if not data.get("ok"):
            # 桥接会说清原因：微信不在虚拟屏前台 / 截图失败 / 解析失败
            self.last_error = str(data.get("error") or "桥接返回 ok=false")
            return None
        self.last_error = ""

        messages: List[Message] = []
        for i, item in enumerate(data.get("messages") or []):
            text = normalize(str(item.get("text", "")))
            if not text:
                continue
            # 手机端给的是已经切好的消息，不需要坐标。
            # y 用序号撑开，保证 Message 内部排序稳定。
            messages.append(Message(
                blocks=[Block(text=text, x=0, y=i * 10, w=10, h=8)],
                is_self=bool(item.get("is_self", False)),
                sender=str(item.get("sender", "") or ""),
                kind=str(item.get("type", "text") or "text"),
                img_path=str(item.get("img_path", "") or ""),
                img_w=int(item.get("img_w", 0) or 0),
                img_h=int(item.get("img_h", 0) or 0),
                img_md5=str(item.get("img_md5", "") or ""),
                img_hash=str(item.get("img_hash", "") or ""),
            ))

        return Snapshot(
            chat_title=normalize(str(data.get("chat_title", ""))),
            messages=messages,
            width=int(data.get("width", 0) or 0),
            height=int(data.get("height", 0) or 0),
            layout=None,          # 手机端不需要布局，也没有"点哪里"这回事
        )

    def in_target_group(self, snap: Snapshot) -> bool:
        if not self.target_group:
            return True
        if snap is None:
            # ★ 2026-10-10：读屏失败时 `read()` 会给 None，这里必须兜住 ——
            #   否则 `None.chat_title` 直接抛 AttributeError 打断主循环。
            #   （test_integration 里就是这么暴露出来的：桥接 404 → snap=None → 崩）
            #   语义上也对：**读不到画面 = 不在目标群**。
            return False
        return title_matches(snap.chat_title, self.target_group)

    def open_chat(self, name: str = "") -> bool:
        """切到指定会话（默认目标群）。手机端独有：靠微信搜索框跳转。"""
        name = name or self.target_group
        data = self._request("/open", {"chat": name}, timeout=150)
        return bool(data and data.get("ok"))

    def title(self) -> str:
        data = self._request("/title")
        return str((data or {}).get("chat_title", ""))


class AndroidSender(_Bridge):
    """实现 :class:`wxauto.drivers.Sender`。"""

    def __init__(self, base_url: str, timeout: float = 180.0,
                 target_group: str = "") -> None:
        super().__init__(base_url, timeout)
        self.target_group = str(target_group or "")
        # ★ 2026-10-09 晚：最近一次发图的**失败原因** + 是否"稍后可以再试"。
        #   机器人（bot.py::_send_sticker / _send_meitu）读这两个值决定要不要把这张图
        #   挂进发图补发队列 —— 起因是用户报「机器人有一些没发出去的」，查下来那次
        #   不是群名问题，而是他正在物理屏用微信（虚拟屏上没微信、截图全失败）。
        self.last_image_error = ""
        self.last_image_retryable = False
        # ★ 2026-10-10：最近一次**发文字**的失败原因 + 是否"稍后可以再试"。
        #   为什么要它：用户在物理屏打字时，桥接现在**不抢键盘**（见
        #   wxbridge.py::preflight_yield 的打字保护）→ 这条返回失败。这种
        #   "你占用着手机"的失败**不能计入熔断**，否则他打几条字就能把机器人
        #   逼成「连续 3 次发送异常，已自动停止」（发图那条路 2026-10-09 已经
        #   踩过一次同样的坑）。机器人（bot.py::_reply）靠 retryable 区分。
        self.last_send_error = ""
        self.last_send_retryable = False
        # ★ 2026-10-12：最近一次发文字是否「结果不确定」（桥接超时 / 断连 / 5xx）。
        #   不确定 = 发送键**可能已经点过、消息可能已进群**，机器人据此不补发、不重试
        #   （宁可漏，不可重）。与 retryable 相反：retryable=确定没点过，可以补。
        self.last_send_uncertain = False
        # ★ 2026-10-10：最近一次「读聊天记录卡片」的失败原因（桥接 /read_record 给的
        #   中文说明）—— 机器人要把这句回给主人，不然他只知道"没读出来"。
        self.last_record_error = ""
        # ★ 2026-10-10：最近一次「往回翻补读」的失败原因（桥接 /backfill）。
        self.last_backfill_error = ""

    def send(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        self.last_send_error = ""
        self.last_send_retryable = False
        self.last_send_uncertain = False
        self.last_req_kind = ""
        try:
            data = self._request("/send", {"text": text})
        except Exception as exc:
            self.last_send_error = "%s: %s" % (type(exc).__name__, exc)
            return False
        if not data:
            if self.last_req_kind == "uncertain":
                # ★ 2026-10-12：超时 / 断连时桥接多半**还在发**，消息可能已经进群。
                #   以前这里和「连接被拒」是同一句话，机器人会把它当成「没发」去补发
                #   同一句——群里就重复了（复现过：桥接先点发送、再慢慢验证，超过
                #   客户端超时，send 返回 False，补发队列又发一遍）。
                self.last_send_uncertain = True
                self.last_send_error = "桥接没在时限内回话（/send），这条可能已经发出去了"
            else:
                self.last_send_error = "桥接没回话（/send 请求失败）"
            return False
        if not data.get("ok"):
            self.last_send_error = str(data.get("error") or "")
            # ⚠️ `is True`：MagicMock 的默认真值会让老测试全变成"可重试"
            self.last_send_retryable = data.get("retryable") is True
        return bool(data.get("ok"))

    def send_image(self, path: str) -> bool:
        """发一张本地图片到目标群（走微信「分享图片」通道，2026-10-03 打通）。

        发图比发字慢得多（拉起分享页 → 选会话 → 校验预览 → 点发送），
        所以单独给 120 秒超时。桥接那边会在点「发送」**之前**校验「发送给：」
        是不是目标群，对不上就点取消 —— 宁可失败，也不把图发错群。
        """
        p = str(path or "").strip()
        if not p:
            return False
        # `file://` 只认绝对路径：传相对路径微信打不开图片、分享页根本起不来
        # （2026-10-04 踩过：桥接一直报「选择列表里没找到」，真凶是路径）。
        if not os.path.isabs(p):
            p = os.path.abspath(p)
        try:
            data = self._request("/send_image",
                                 {"path": p, "chat": self.target_group}, timeout=120)
        except Exception as exc:
            # HTTP 超时/断连时，桥接那边的分享流程可能**还停在分享页**，
            # 界面不收回来机器人就会一直读到「选择聊天」而永久待命。
            # 这里兜底再喊一次 /open 把会话切回目标群（异步、尽力而为）。
            # 注意 _Bridge 没有 logger，这里用 print，别用 self.log（会 AttributeError）
            _LOG.warning("[android] 发图请求异常（%s），兜底切回目标群", type(exc).__name__)
            # 超时/断连时**不确定**发送键点没点过 → 一律不补发（宁可漏，不可重复发图）
            self.last_image_error = "发图请求异常（%s）" % type(exc).__name__
            self.last_image_retryable = False
            try:
                self._request("/open", {"chat": self.target_group}, timeout=120)
            except Exception:
                pass
            return False
        ok = bool(data and data.get("ok"))
        if not ok:
            # 桥接报失败（预览校验不过 / 列表里找不到会话行 / 发送后标题对不上）时，
            # 它内部一般会走 abandon_share 把界面收回目标群，但那条收尾本身也可能
            # 失败（见交接说明的待办）：界面留在分享/搜索页，机器人就永久「待命中」。
            # 这里再兜底喊一次 /open。界面本来就在目标群时桥接走快速路径（约 1.4 秒），
            # 是很便宜的一道保险；真切不回去也不会比现在更糟。
            # ★ 2026-10-09：把桥接那句**中文原因**打出来 —— 以前只知道"失败了"，
            #   看不出是列表里找不到会话、预览页名字对不上，还是点完发送没进群。
            err = str((data or {}).get("error") or "").strip()
            self.last_image_error = err
            # ★ 桥接的 retryable 只在**没点过发送键**时为真（你占用手机 / 列表里没找到
            #   会话行 / 预览页对不上）—— 那几种重发不会造成重复发图，所以可以补发。
            #   `is True` 是故意的：测试里的 MagicMock 属性默认真值，不能让它误判成
            #   "可补发"（这个项目在 mock 默认值上已经踩过四次坑了）。
            self.last_image_retryable = (data or {}).get("retryable") is True
            _LOG.warning("[android] 发图返回失败%s，兜底确认切回目标群",
                         ("：" + err) if err else "")
            if self.last_image_retryable:
                _LOG.warning("[android] 这条属于「稍后可以再试」（%s），机器人会补发",
                             err or "环境忙")
            try:
                self._request("/open", {"chat": self.target_group}, timeout=120)
            except Exception:
                pass
        else:
            self.last_image_error = ""
            self.last_image_retryable = False
        return ok

    def read_record(self, match: str = "") -> Optional[dict]:
        """点开屏幕上那张聊天记录卡片，把里面的文字读回来。

        ★ 2026-10-10（用户要求「想个法子让他能读到聊天记录里的内容」）：
          走桥接的 ``POST /read_record`` —— 它定位「XX的聊天记录」标题行、点开、
          确认真的进了 RecordDetailUI、整页 OCR、再按返回退出（细节和纪律见
          wxbridge.py 里那段注释）。

        ⚠️ **这一步会替用户点屏幕**，所以桥接和机器人两边都只让它发生在
          「主人明确喊它读」的时候（bot.py::_read_record_and_comment），
          群友 @ 它走不到这条路。

        成本：两次整页 OCR（各约 9 秒）+ 点开/退出，实测 20~30 秒 —— 比转给
        agent 那趟（45~90 秒）快，所以这条路优先级排在 agent 前面。
        返回桥接的完整 dict（``title`` / ``text``），失败返回 None（原因在
        ``last_record_error``）。
        """
        try:
            data = self._request("/read_record",
                                 {"match": str(match or "")}, timeout=180)
        except Exception as exc:
            self.last_record_error = "读聊天记录请求异常（%s）" % type(exc).__name__
            _LOG.warning("[android] %s，兜底切回目标群", self.last_record_error)
            try:
                self._request("/open", {"chat": self.target_group}, timeout=120)
            except Exception:
                pass
            return None
        if not data:
            self.last_record_error = "桥接没回话（/read_record 请求失败）"
            return None
        if not data.get("ok"):
            self.last_record_error = str(data.get("error") or "桥接返回 ok=false")
            return None
        self.last_record_error = ""
        return data

    def backfill(self, screens: int = 2, chat: str = "") -> Optional[dict]:
        """叫它往回翻补读：往上翻 N 屏把刷过去的消息读回来，再切回最新。

        ★ 2026-10-10 用户口径：**只在他叫的时候翻**（自动补读他选了不要）。
          桥接 /backfill 的实现里，翻完**必须**切回最新 —— 否则接下来几轮读到的
          全是历史消息，新消息反而全漏。耗时实测约 10 秒/屏（含 OCR + 滑块等待）。
        """
        try:
            data = self._request("/backfill",
                                 {"screens": int(screens or 2),
                                  "chat": str(chat or self.target_group)},
                                 timeout=180)
        except Exception as exc:
            self.last_backfill_error = "补读请求异常（%s）" % type(exc).__name__
            return None
        if not data or not data.get("ok"):
            self.last_backfill_error = str((data or {}).get("error") or "桥接没回话")
            return None
        self.last_backfill_error = ""
        return data

    def is_ready(self) -> bool:
        """能不能发 —— **并且确实在目标群**。

        ★ 2026-10-08 审查修复（H4）：这里原来是拿 /health 判断的，而 /health
        只回"桥接活着 + 屏号"，跟"当前在哪个会话"毫无关系。于是 bot.py 里
        `if not self.sender.is_ready(): log("等待期间已离开目标群，取消本次发送")`
        这句在手机端是**假的**：会话被切走也照样 True，消息就发到别的会话去了。
        现在问桥接的 /where：
          · in_target=True  → 正常
          · in_target=False → 不在目标群（拒发）
          · in_target=None  → 标题读不出（不确定）→ 交给桥接发送路径 fail-closed
                             处理（它会先切回目标群再核对），这里不拦，免得
                             OCR 一抖就整条发不出去。
        """
        data = self._request("/where")
        if not data or not data.get("ok"):
            # /where 不可用（老版本桥接）→ 退回探活，至少别把发送全掐死
            h = self._request("/health")
            return bool(h and h.get("ok"))
        return data.get("in_target") is not False

    def wechat_on_screen(self) -> bool:
        """微信此刻在不在虚拟屏前台 —— **轻探**（桥接只跑一次 dumpsys）。

        ★ 2026-10-10：给 bot.py 的「退避期间轻探」用。原来连续读不到画面就死等
          300 秒才试下一次，微信哪怕 10 秒就回来了，机器人也要再瞎最多 5 分钟
          （用户问「为什么会退避五分钟」就是这个）。这条比 /where（截图 + OCR）
          便宜两个数量级，退避期间每 15 秒问一次不心疼。
        """
        d = self._request("/peek", timeout=10)
        return bool(d and d.get("ok") and d.get("on_screen"))

    def screen_awake(self):
        """物理屏亮着吗？``True``=亮 / ``False``=熄屏或锁屏 / ``None``=读不到。

        ★ 2026-10-11（用户拍板「锁屏时连读屏和模型也停掉，亮屏再唤醒」）：
          bot.py::_wait_awake 每轮问它一次，据此挂起/恢复。桥接那边只跑一次
          `dumpsys power`（见 wxbridge.py::screen_awake），比 /where 便宜几个数量级。
        ⚠️ 拿到的是**三态**：桥接挂了 / 读不到时回 None，调用方必须自己决定
          保守方向（挂起这条路要求 None 就当"亮着"，绝不自己睡过去）。
        """
        d = self._request("/screen", timeout=10)
        if not d or not d.get("ok"):
            return None
        return d.get("awake")
