# -*- coding: utf-8 -*-
"""群友发的 PPT：取文件 → 抽文字 → 交给模型点评。

背景（2026-10-07 用户需求）
--------------------------
群里有人把「瓜」做成 PPT 发出来（聊天记录里张三说过「没看PPT」）。
机器人原来只把文件卡片上那行字 OCR 成普通文字，**内容一点都看不到** ——
本模块把后半段补上。

整条链路（每一环都实测过）
-------------------------
1. 用户在微信里点开那个 PPT（机器人没能力点开文件卡片，这一步由人代劳）；
2. 微信把文件导出到自己的目录，见下面 WX_MICROMSG / LOCAL_DIRS；
3. 本模块取走文件、抽文字；
4. ``brain.comment_on_ppt()`` 按人设写一句；
5. ``bot._reply()`` 发进群。

⚠️ 为什么必须走 adb
-------------------
微信「用其他应用打开」的落点在 ``/sdcard/Android/data/com.tencent.mm/...``，
容器**直接 ls 看不到**（Android 11+ scoped storage），但 adb shell 能看能拉。
2026-10-07 实测：一个 122KB 的 xlsx，``adb pull`` 53MB/s，zipfile 直接解出
292 条文本。

⚠️ 为什么必须按 mtime 过滤
-------------------------
微信目录里躺着历史文件（实测有 2025-07 的 xlsx）。不过滤的话，机器人一开机
就把陈年旧文件全点评一遍发进群。

⚠️ Office 三件套不需要任何第三方库
----------------------------------
pptx / docx / xlsx 本质都是 zip + xml，用标准库 zipfile + 正则就能抽干净
（本机没有 python-pptx，也不需要装）。
"""
from __future__ import annotations

from .paths import APP_DIR, DATA_DIR
import html
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

# pypdf 装在 /root/pylibs（容器有 PEP 668，装不进系统目录；/root 是持久的）。
import sys as _sys
if "/root/pylibs" not in _sys.path:
    _sys.path.insert(0, "/root/pylibs")

ROOT = DATA_DIR
SERIAL_FILE = Path("/sdcard/dsh/.adb-serial")
SEEN_FILE = ROOT / "ppt_seen.json"
FETCH_DIR = ROOT / "tmp" / "ppt"

# 能自己解开的（都是 zip 家族）。老格式 .ppt/.doc 是 OLE 复合文档，这里不碰。
SUPPORTED_EXTS = (".pptx", ".docx", ".xlsx", ".pdf")

# 微信文件的可能落点（都要走 adb —— Android 11+ 容器直接 ls 看不到 Android/data）：
#   openapi    「用其他应用打开」导出的，content_<时间戳>.<ext>（实测有 content_*.xlsx）
#   Download   微信自己的下载目录
#   attachment 老版本的文件缓存
WX_MICROMSG = "/sdcard/Android/data/com.tencent.mm/MicroMsg"
REMOTE_DIRS = (
    WX_MICROMSG + "/*/openapi",
    WX_MICROMSG + "/Download",
    WX_MICROMSG + "/*/attachment",
)
# 微信「保存到手机」的落点（这个容器能直接读，不用 adb）
LOCAL_DIRS = (
    "/sdcard/Download/Weixin",
    # ★ 2026-10-07 用户要求：吃瓜素材直接丢 DSH 收件箱，机器人自动读完发群。
    #   ⚠️ 这个目录里也放工作文件（.md/.zip 之类），但那些扩展名不在
    #   SUPPORTED_EXTS 里、不会被处理；只有 pptx/docx/xlsx/pdf 会触发点评。
    "/sdcard/dsh/inbox",
)


def _log(logger, level, msg):
    if logger:
        getattr(logger, level, logger.info)(msg)


# --------------------------------------------------------------- adb 小工具
def _serial() -> str:
    try:
        return SERIAL_FILE.read_text().strip()
    except Exception:
        return ""


def _adb(args, timeout=25) -> str:
    """跑一条 adb 命令，返回 stdout（任何异常都返回空串，绝不抛）。"""
    ser = _serial()
    cmd = ["adb"] + (["-s", ser] if ser else []) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


# --------------------------------------------------------------- 发现文件
def _remote_entries() -> list:
    """列微信私有目录里的文档（走 adb）。

    一条 shell 搞定：find 出文件，直接 -exec stat 打出 ``路径|大小|mtime``。
    `MicroMsg/*/openapi` 这个 glob 由手机的 sh 展开；没有匹配时保持字面量、
    find 会报错，所以 2>/dev/null 兜住。
    """
    cond = " -o ".join("-iname '*%s'" % e for e in SUPPORTED_EXTS)
    # 多个起点一起 find；某个目录不存在时 find 会报错、被 2>/dev/null 吞掉，
    # 其余起点照常处理（实测这样一条命令就够，比分几次调用快）。
    dirs = " ".join(REMOTE_DIRS)
    sh = (f"find {dirs} -maxdepth 1 -type f "
          f"\\( {cond} \\) -exec stat -c '%n|%s|%Y' {{}} + 2>/dev/null")
    out = _adb(["shell", sh])
    items = []
    for line in (out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        path, size, mtime = parts
        try:
            items.append({"path": path, "size": int(size),
                          "mtime": float(mtime), "remote": True})
        except ValueError:
            continue
    return items


def _local_entries() -> list:
    """列本地目录里的文档（容器直接读得到）。"""
    items = []
    for d in LOCAL_DIRS:
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if not e.is_file():
                            continue
                        if os.path.splitext(e.name)[1].lower() not in SUPPORTED_EXTS:
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    items.append({"path": e.path, "size": st.st_size,
                                  "mtime": st.st_mtime, "remote": False})
        except Exception:
            continue
    return items


def list_docs() -> list:
    """两个落点合起来。"""
    return _remote_entries() + _local_entries()


def make_key(entry: dict) -> str:
    """去重键：路径 + 大小 + 修改时间。"""
    return "%s|%d|%d" % (entry.get("path", ""), int(entry.get("size") or 0),
                         int(entry.get("mtime") or 0))


# --------------------------------------------------------------- 取文件
def fetch(entry: dict, dest_dir=None) -> str:
    """把文件搬到容器本地，返回本地路径；失败返回空串。"""
    dest = Path(dest_dir) if dest_dir else FETCH_DIR
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""
    name = os.path.basename(entry.get("path", "")) or "doc"
    local = dest / ("%d_%s" % (int(time.time()), name))
    if not entry.get("remote"):
        try:
            shutil.copyfile(entry["path"], local)
        except Exception:
            return ""
    else:
        _adb(["pull", entry["path"], str(local)], timeout=90)
    try:
        if local.exists() and local.stat().st_size > 0:
            return str(local)
    except OSError:
        pass
    return ""


# --------------------------------------------------------------- 抽文字
def _xml_text(chunk: str, tag: str) -> str:
    """把一段 xml 里所有 <tag>文字</tag> 抽出来并反转义。"""
    found = re.findall(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), chunk, re.S)
    return " ".join(html.unescape(t).strip() for t in found if t.strip())


def _slide_no(name: str) -> int:
    m = re.search(r"(\d+)", os.path.basename(name))
    return int(m.group(1)) if m else 0


def _pptx_text(path: str) -> str:
    lines = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
        names.sort(key=_slide_no)
        for n in names:
            xml = z.read(n).decode("utf-8", "ignore")
            line = _xml_text(xml, "a:t")
            if line:
                lines.append(line)
    return "\n".join(lines)


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    paras = []
    # 按 <w:p> 段落切，段落内再把 <w:t> 拼起来（不然所有字会糊成一行）
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        line = _xml_text(p, "w:t")
        if line:
            paras.append(line)
    return "\n".join(paras)


def _xlsx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        try:
            ss = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        except KeyError:
            return ""
    return _xml_text(ss, "t")


def _merge_short_lines(raw: str, keep: int = 30) -> str:
    """把「一个字一行」的竖排 PDF 文本拼回正常句子。

    实测「帝都风云」那份吃瓜 PDF：每个字单独成行（文本框逐字排版），
    不拼的话喂给模型的就是几千行单字，又乱又费 token。
    """
    out, buf = [], []
    for ln in (raw or "").splitlines():
        t = ln.strip()
        if not t:
            continue
        if len(t) <= 3 and len("".join(buf)) < keep:
            buf.append(t)
            continue
        if buf:
            out.append("".join(buf))
            buf = []
        out.append(t)
    if buf:
        out.append("".join(buf))
    return "\n".join(out)


def _pdf_text(path: str) -> str:
    """PDF 抽文字。需要 pypdf（装在 /root/pylibs）；没有库就返回空串、不报错。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        _log(None, "warning", "")
        return ""
    reader = PdfReader(path)
    raw = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    return _merge_short_lines(raw)


def _tidy(text: str, max_chars: int) -> str:
    """去重、压空白、截断 —— 喂给模型前统一收拾一遍。"""
    out, seen = [], set()
    for ln in (text or "").splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln or ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    joined = "\n".join(out)
    if max_chars and len(joined) > max_chars:
        joined = joined[:max_chars] + "…"
    return joined


def extract_text(path: str, max_chars: int = 3000) -> str:
    """按后缀抽文字。认不出的格式 / 损坏的包都返回空串，不抛。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pptx":
            raw = _pptx_text(path)
        elif ext == ".docx":
            raw = _docx_text(path)
        elif ext == ".xlsx":
            raw = _xlsx_text(path)
        elif ext == ".pdf":
            raw = _pdf_text(path)
        else:
            return ""
    except Exception:
        return ""
    return _tidy(raw, max_chars)


def fetch_and_extract(entry: dict, logger=None, max_chars: int = 3000) -> str:
    """取文件 + 抽文字，一条龙；本地临时文件用完就删。"""
    local = fetch(entry)
    if not local:
        _log(logger, "warning", "取文件失败：%s" % (entry.get("path"),))
        return ""
    try:
        text = extract_text(local, max_chars=max_chars)
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    return text


# --------------------------------------------------------------- 看门狗
class DocWatcher:
    """盯着微信的文件落点，挑出「刚出现、还没点评过」的文档。

    状态存在 ``ppt_seen.json``（路径|大小|mtime 的列表，只留最近 500 条），
    所以机器人重启不会把同一份 PPT 再点评一遍。
    """

    def __init__(self, logger=None, cfg=None):
        self.log = logger
        self.cfg = cfg
        self._seen = self._load_seen()

    # ---- 状态
    def _load_seen(self) -> list:
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            return list(data.get("seen") or [])
        except Exception:
            return []

    def _save_seen(self) -> None:
        try:
            # ★ 2026-10-08 审查修复（M3）：改为原子写。原来 write_text 写坏时
            #   静默失败，_load_seen 读不出来就当空表 —— 后果是"已经点评过的
            #   PPT 又被点评一遍"（重复发群，正是最不能出的事故）。
            from .atomic import atomic_write_json
            atomic_write_json(SEEN_FILE, {"seen": self._seen[-500:]})
        except Exception as exc:
            # 不再完全静默：写不进去要能在日志里看见
            if self.log:
                self.log.warning("ppt_seen 落盘失败（下轮可能重复点评）：%s" % exc)

    def mark_seen(self, entry: dict) -> None:
        key = make_key(entry)
        if key not in self._seen:
            self._seen.append(key)
            self._save_seen()

    # ---- 扫描
    def poll(self) -> list:
        """返回候选文档（**不改状态**，由调用方处理完再 mark_seen）。

        过滤规则：
        - 大小 <= 0 或超过 ``max_bytes`` → 丢弃；
        - ``mtime`` 太老（超过 ``fresh_secs``）→ 丢弃（陈年旧文件）；
        - ``mtime`` 距今不足 ``min_age_secs`` → 这轮跳过（多半还在写，下轮再看）。
        """
        now = time.time()
        fresh = float(getattr(self.cfg, "fresh_secs", 900) or 0)
        max_bytes = int(getattr(self.cfg, "max_bytes", 52428800) or 0)
        min_age = float(getattr(self.cfg, "min_age_secs", 4) or 0)

        out = []
        try:
            entries = list_docs()
        except Exception as exc:
            _log(self.log, "warning", "扫文件落点出错：%s: %s"
                 % (type(exc).__name__, exc))
            return []
        for e in entries:
            if make_key(e) in self._seen:
                continue
            size = int(e.get("size") or 0)
            if size <= 0 or (max_bytes and size > max_bytes):
                continue
            age = now - float(e.get("mtime") or 0)
            if age > fresh or age < min_age:
                continue
            out.append(e)
        out.sort(key=lambda x: float(x.get("mtime") or 0))
        if out:
            _log(self.log, "info", "发现 %d 个待看的文档" % len(out))
        return out
