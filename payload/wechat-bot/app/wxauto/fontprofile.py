# -*- coding: utf-8 -*-
"""字体档案 —— 适配「别人的微信不是默认字体」（v2.0.0 新增）。

## 为什么需要它

读屏的版面参数（气泡高度、行高、头像带宽…）都是按**本机微信 + 标准字号**
调出来的。别人把微信字体调大、或者换了系统字体/主题字体，字形与行高都变，
同一套常量就会读漏行、把两个气泡粘成一个。

## 三种用法（对应 config.yaml → read.font）

    ① 配置项      read.font.scale / size_px / profile / engine
    ② 自动测量    python3 -m wxauto.fontprofile --auto
    ③ 字体档案    python3 -m wxauto.fontprofile --make/--list/--show/--use

档案是纯 JSON，放在 `data/font_profiles/<名字>.json`，可以随包分发。

## 与读屏层的约定

`scale` 是**相对基准行高的倍数**，读屏层拿它乘在 `apply_resolution()` 的
总缩放上（见 `read/wxreader.py::apply_resolution`）。基准
`BASE_LINE_H = 64.0` 是 2026-10-13 在 full 档（1264 宽）+ 微信标准字号下
实测的正文中文行高中位数。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from .paths import APP_DIR, DATA_DIR

PROFILES_DIR = DATA_DIR / "font_profiles"

BASE_LINE_H = 64.0      # 基准正文行高（px，full 档 1264 宽 / 标准字号）
BASE_FRAME_W = 1264     # 基准帧宽
MIN_ITEMS = 8           # 有效性阈值：含中文的文本框少于这个数就拒绝写档案
MIN_OK_LINE_H = 45.0
SINGLE_CLUSTER_MIN = 55.0  # 只有一个高度簇时的下限（低于它判为"只有昵称/标题"）
DARK_LUM_MAX = 110.0     # 文字平均亮度 ≤ 这个值算「正文黑字」，高于它是昵称灰字    # 归一化后的正文行高下限（低于此认为没抓到正文）
MIN_LINE_H = 30.0
MAX_LINE_H = 140.0
BRIDGE = os.environ.get("WXBRIDGE", "http://127.0.0.1:8765")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------- 档案读写
def profiles_dir() -> Path:
    d = PROFILES_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def list_profiles() -> list:
    out = []
    for f in sorted(profiles_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d.setdefault("name", f.stem)
            out.append(d)
        except Exception:
            out.append({"name": f.stem, "broken": True})
    return out


def load_profile(name):
    if not name:
        return None
    p = profiles_dir() / ("%s.json" % name)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_profile(data: dict) -> Path:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("档案必须有 name")
    p = profiles_dir() / ("%s.json" % name)
    data = dict(data)
    data["name"] = name
    data["updated"] = _now()
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def scale_of(profile) -> float:
    """档案 → 缩放倍数。优先 scale，其次用 size_px 反推，兜底 1.0。"""
    if not profile:
        return 1.0
    try:
        s = float(profile.get("scale") or 0)
    except Exception:
        s = 0.0
    if s <= 0:
        try:
            sz = float(profile.get("size_px") or 0)
        except Exception:
            sz = 0.0
        s = (sz / BASE_LINE_H) if sz > 0 else 1.0
    return max(0.5, min(2.5, s))


# --------------------------------------------------------------------- 配置解析
def resolve(font_cfg):
    """把 config.yaml → read.font 解析成 ``(scale, engine, note)``。

    优先级：**显式配置 > 档案 > 默认 1.0**（用户 2026-10-13 定的口径）。
    任何一段缺失/写错都安全退回 ``(1.0, "auto", "")``。
    """
    if font_cfg is None or not bool(getattr(font_cfg, "enabled", False)):
        return 1.0, "auto", ""
    # 1) 显式 scale 最优先
    try:
        raw = float(getattr(font_cfg, "scale", 0) or 0)
    except Exception:
        raw = 0.0
    engine = str(getattr(font_cfg, "engine", "auto") or "auto").lower()
    if raw and abs(raw - 1.0) > 1e-6:
        return max(0.5, min(2.5, raw)), engine, "config.scale"
    # 2) 显式 size_px
    try:
        sz = float(getattr(font_cfg, "size_px", 0) or 0)
    except Exception:
        sz = 0.0
    if sz > 0:
        return max(0.5, min(2.5, sz / BASE_LINE_H)), engine, "config.size_px"
    # 3) 字体档案
    name = str(getattr(font_cfg, "profile", "") or "").strip()
    prof = load_profile(name)
    if prof:
        return scale_of(prof), str(prof.get("engine") or engine), "profile:%s" % name
    return 1.0, engine, ""


# --------------------------------------------------------------------- 自动测量
def grab_frame(out_path=None) -> str:
    """抓一帧虚拟屏画面（优先问桥接要 displayId，失败则用最近的桥接帧）。"""
    out_path = out_path or "/tmp/wxfont-probe.jpg"
    did = None
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=5) as r:
            did = json.loads(r.read().decode("utf-8")).get("displayId")
    except Exception:
        did = None
    vsc = "/sdcard/dsh/tools/vscreen"
    if did is not None and os.path.isfile(vsc):
        import subprocess
        try:
            subprocess.run(["node", vsc, "-i", str(did), "shot", out_path],
                           check=True, timeout=30, capture_output=True)
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 1000:
                return out_path
        except Exception:
            pass
    # 兜底：桥接最近读屏留下的帧
    for cand in ("/tmp/wxbridge-snap.jpg", "/tmp/wxbridge-list.jpg"):
        if os.path.isfile(cand) and os.path.getsize(cand) > 1000:
            return cand
    raise RuntimeError("抓不到画面：桥接没在跑、也没有可用的历史帧（--image 指定一张图）")


def _cluster(hs, tol=6.0):
    """把高度按 ±tol 聚成簇（返回 [[h...], ...]，已排序）。"""
    groups = []
    for h in sorted(hs):
        if groups and h - groups[-1][-1] <= tol:
            groups[-1].append(h)
        else:
            groups.append([h])
    return groups


def analyze_heights(heights, width=BASE_FRAME_W) -> dict:
    """纯逻辑：一批「含中文文本框」的高度 → 测量结果（不含 OCR，便于单测）。

    ⚠️ 2026-10-13 实测踩到的坑：直接取中位数**会量错**。群聊截图里
    「昵称」是浅灰小字（实测 49px，且同一个人连发多条时出现多次），
    正文才是 63~66px —— 中位数会被昵称那一堆拉低，量出 0.78 倍这种假字号。
    所以改成**按高度聚类**：正文总是比昵称高，取「样本数 ≥2 的簇里较高的那个」；
    单条噪声（标题栏那种只有一行的）天然被 ≥2 挡掉。
    """
    hs = sorted(float(h) for h in heights if MIN_LINE_H <= float(h) <= MAX_LINE_H)
    info = {"count": len(hs), "width": int(width)}
    if len(hs) < MIN_ITEMS:
        return dict(ok=False,
                    reason=("含中文的文本框只有 %d 个（阈值 %d）—— 这不是一个"
                            "「有连续文字气泡」的界面，量出来是垃圾，拒绝写档案"
                            % (len(hs), MIN_ITEMS)), **info)
    groups = _cluster(hs)
    solid = [g for g in groups if len(g) >= 2]      # 单条的是标题/噪声
    if solid:
        chosen = max(solid, key=lambda g: statistics.median(g))   # 正文比昵称高
    else:
        chosen = groups[len(groups) // 2]
    line_h = float(statistics.median(chosen))
    line_norm = line_h * (BASE_FRAME_W / float(width or BASE_FRAME_W))
    # 拒绝的两种情况：
    #   ① 比正文下限还矮 —— 根本不是正文；
    #   ② **只有一个簇、而且偏低** —— 说明这一屏只有昵称/标题（典型是会话列表页），
    #      实测 49.5px。此时若照写档案，会把版面参数整体缩到 0.77 倍、读屏直接坏。
    #      （真·小字号正文极少见：微信字号只能调大，实测最小档也不低于 ~58px。）
    low_single = (len(solid) <= 1) and line_norm < SINGLE_CLUSTER_MIN
    if line_norm < MIN_OK_LINE_H or low_single:
        return dict(ok=False, line_h=round(line_h, 1), line_h_norm=round(line_norm, 1),
                    reason=("量到的文字只有 %.0fpx（正文下限 %.0fpx）—— 这个界面"
                            "多半只有昵称/标题、没有正文气泡。换到群聊页再测，"
                            "或者直接手工填 read.font.scale"
                            % (line_norm, MIN_OK_LINE_H)), **info)
    return dict(ok=True, line_h=round(line_h, 1), line_h_norm=round(line_norm, 1),
                scale=round(line_norm / BASE_LINE_H, 4),
                clusters=[[round(statistics.median(g), 1), len(g)] for g in groups],
                confidence=round(min(1.0, len(chosen) / 12.0), 2), **info)


def measure_frame(img_path) -> dict:
    """测一帧的正文行高 → dict（含 ok / reason / line_h / scale / count）。

    两道过滤，缺一不可：
      ① **颜色**：昵称是浅灰小字（`_text_lum` ≈137），正文是黑字（≈50）——
         先只留深色文字；若这样剩不下 8 条（例如深色模式截图），退回不过滤。
      ② **聚类**：见 `analyze_heights`。
    """
    sys.path.insert(0, str(APP_DIR / "read"))
    _lib = os.environ.get("WX_OCRLIB", "/root/ocrlibs")   # OCR 运行时（与 wxbridge_ctl.sh 同口径）
    if os.path.isdir(_lib) and _lib not in sys.path:
        sys.path.insert(0, _lib)
    import wxreader

    width = float(BASE_FRAME_W)
    arr = None
    try:
        import numpy as np
        from PIL import Image
        with Image.open(str(img_path)) as im:
            width = float(im.size[0])
            arr = np.asarray(im.convert("RGB"), dtype=np.int16)
    except Exception:
        arr = None

    res, _ = wxreader.ocr_call(str(img_path))
    rows = []
    for box, text, _score in (res or []):
        if not text or not any("\u4e00" <= c <= "\u9fff" for c in str(text)):
            continue
        try:
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            h = float(max(ys) - min(ys))
        except Exception:
            continue
        if not (MIN_LINE_H <= h <= MAX_LINE_H):
            continue
        dark = True
        if arr is not None and h >= MIN_OK_LINE_H:
            try:
                lum = wxreader._text_lum(arr, int(min(ys)), int(max(ys)),
                                         int(min(xs)), int(max(xs)))
                dark = lum <= DARK_LUM_MAX
            except Exception:
                dark = True
        rows.append((h, str(text), dark))

    dark_rows = [(h, t) for h, t, d in rows if d]
    if len(dark_rows) >= MIN_ITEMS:
        items, mode = dark_rows, "dark-only"
    else:
        items, mode = [(h, t) for h, t, _ in rows], "all"
    r = analyze_heights([h for h, _ in items], width=width)
    r["frame"] = str(img_path)
    r["filter"] = mode
    r["samples"] = [[round(h), t[:24]] for h, t in items]
    return r


# --------------------------------------------------------------------- config 写回
def set_config_profile(name: str, cfg_path=None) -> bool:
    """把档案名写进 ``config.yaml → read.font.profile``（行级替换，注释不动）。"""
    p = Path(cfg_path) if cfg_path else (DATA_DIR / "config.yaml")
    if not p.is_file():
        return False
    lines = p.read_text(encoding="utf-8").split("\n")
    in_read = in_font = False
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        indent = len(ln) - len(ln.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue
        if indent == 0:
            in_read = stripped.startswith("read:")
            in_font = False
            continue
        if in_read and indent <= 2 and stripped.startswith("font:"):
            in_font = True
            continue
        if in_read and in_font and stripped.startswith("profile:"):
            head, rest = ln.split("profile:", 1)
            comment = ""
            if "#" in rest:                     # 行尾注释照原样留着（配置里注释就是说明书）
                _c = rest.index("#")
                comment = "   " + rest[_c:].strip()
            lines[i] = '%sprofile: "%s"%s' % (head, name, comment)
            p.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


# --------------------------------------------------------------------- CLI
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m wxauto.fontprofile",
        description="字体档案工具：适配「微信不是默认字体」")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="列出所有档案")
    g.add_argument("--show", metavar="名字", help="看某份档案的完整内容")
    g.add_argument("--make", metavar="名字", help="手工建一份档案")
    g.add_argument("--auto", action="store_true", help="从当前屏幕自动测量字号")
    g.add_argument("--use", metavar="名字", help="把档案名写进 config.yaml")
    ap.add_argument("--image", help="--auto 用：指定一张截图（默认自己抓虚拟屏）")
    ap.add_argument("--file", help="--make 用：字体文件路径或字体名")
    ap.add_argument("--size", type=float, default=0, help="--make 用：正文字号 px")
    ap.add_argument("--scale", type=float, default=0, help="--make 用：缩放倍数")
    ap.add_argument("--engine", default="auto", help="识别引擎：auto | v5")
    ap.add_argument("--yes", action="store_true", help="--auto 用：不交互确认，直接写")
    args = ap.parse_args(argv)

    if args.list:
        profs = list_profiles()
        if not profs:
            print("（还没有档案）用 --auto 测一份，或 --make 手工建一份")
        for d in profs:
            print("  %-16s scale=%-6s size=%-6s engine=%-4s %s"
                  % (d.get("name"), d.get("scale"), d.get("size_px"),
                     d.get("engine", "auto"), d.get("updated", "")))
        return 0

    if args.show:
        d = load_profile(args.show)
        if not d:
            print("找不到档案：%s（--list 看看有哪些）" % args.show)
            return 1
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    if args.make:
        d = {"name": args.make, "engine": args.engine, "created": _now(),
             "source": "make"}
        if args.file:
            d["file"] = args.file
        if args.size:
            d["size_px"] = args.size
            d["scale"] = round(args.size / BASE_LINE_H, 4)
        if args.scale:
            d["scale"] = args.scale
        if not d.get("scale") and not d.get("size_px"):
            d["scale"] = 1.0
        p = save_profile(d)
        print("✅ 已写档案：%s" % p)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    if args.auto:
        img = args.image or grab_frame()
        print("测量图：%s" % img)
        r = measure_frame(img)
        print(json.dumps({k: v for k, v in r.items() if k != "samples"},
                         ensure_ascii=False, indent=2))
        if not r.get("ok"):
            print("❌ 没量出来：%s" % r.get("reason"))
            return 2
        name = args.use or ("auto-%d" % int(r["line_h_norm"]))
        print("--- 正文行高样本（前 8 条）---")
        for h, t in (r.get("samples") or [])[:8]:
            print("   %4.0fpx  %s" % (h, t))
        print("测得：正文行高 %.0fpx（归一化 %.0fpx）→ scale=%.3f，置信度 %.2f"
              % (r["line_h"], r["line_h_norm"], r["scale"], r["confidence"]))
        if not args.yes:
            try:
                ans = input("写入档案「%s」？[y/N] " % name).strip().lower()
            except EOFError:
                ans = "n"
            if ans not in ("y", "yes"):
                print("没写。（想直接写加 --yes）")
                return 0
        d = {"name": name, "scale": r["scale"], "engine": args.engine,
             "line_h": r["line_h"], "line_h_norm": r["line_h_norm"],
             "size_px": round(r["line_h_norm"], 1),
             "confidence": r["confidence"], "samples": r["count"],
             "frame": r["frame"], "width": r["width"],
             "created": _now(), "source": "auto"}
        p = save_profile(d)
        print("✅ 已写档案：%s" % p)
        print("   启用它：把 config.yaml 里 read.font.enabled 改成 true，"
              "profile 填 \"%s\"（或跑 --use %s）" % (name, name))
        return 0

    if args.use:
        d = load_profile(args.use)
        if not d:
            print("找不到档案：%s" % args.use)
            return 1
        ok = set_config_profile(args.use)
        print("✅ 已写进 config.yaml" if ok else "⚠️ 没找到 read.font.profile 那一行，请手工填")
        if ok:
            print("   还要确认 read.font.enabled: true，然后重启读屏：bash wxbridge_ctl.sh restart")
        return 0 if ok else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
