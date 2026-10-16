# -*- coding: utf-8 -*-
"""wxreader.py —— 微信聊天截图的结构化读取（虚拟屏 Route B）

为什么不用 tesseract：
    tesseract 对微信的中文字体识别率极差（"端到端发送测试OK" -> "凤到师友大测试 OK"），
    换成 RapidOCR（PP-OCRv4 onnx）后**单屏全图 OCR 仅 0.5~2 秒且逐字准确**。

★ 2026-10-09：识别模型从 PP-OCRv4 mobile 换成 **PP-OCRv5 mobile**（见 rec_model_path）。
    起因是用户报「又重复读消息了」—— 病根之一是灰色昵称读不准
    （「甜湫 素问」被读成 `甜秋素问` / `品淋系问` / 空，每帧都不一样）。
    v5 实测（同一帧、同样 1.23 秒）：
        v4: 甜秋素问 / 涂17歌也沉喵      ← 两个错字，前者进了指纹、后者连发送自检都过不了
        v5: 甜湫 素问 / 淦17歌也沉喵     ← 都对
    18 帧批量对比：平均耗时 1.59s → 1.61s（几乎不变），检测框数不变。
    ⚠️ 不是万能药：v5 也会在别的行上错（繁体「貓」、漏字），跟 v4 互有胜负 ——
       它把错误分布挪了位置，只是**在我们最痛的那两类字（灰字昵称、形近字）上明显更好**。
    ⚠️ 模型文件在项目 `models/` 下（/sdcard 侧），**不要**塞进 /root/ocrlibs：
       那是可以从 ocrlibs.tar.gz 重建的运行时，塞进去的模型和配置会被重装冲掉。
    回滚：删掉 models/ch_PP-OCRv5_rec_mobile.onnx（或设 WX_OCR_REC=off）。

结构化（谁发的 / 是不是自己）不靠 OCR，靠**像素证据**：
    - 自己的气泡是微信绿 #95EC69（g-b>40）；
    - 别人的气泡紧贴左侧头像列（x<=105），自己发的紧贴右侧头像列（x>=530）；
    - 灰字（meanDark≈137）且不在气泡里 = 群昵称，挂在后面第一条消息上；
    - 居中灰字 = 时间戳/系统提示；
    - 落在左右头像列里的文字（头像图上印的字）直接丢弃；
    - 高度超过 200px 的气泡 = 图片/表情消息。

坐标全部基于虚拟屏 0 的 636x1400 布局。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading

import numpy as np
from PIL import Image

# ---- 虚拟屏聊天页固定布局参数（636x1400）----
CHAT_TOP = 72          # 标题栏下沿（消息行检测的起点，别乱动）
# read_title 的 OCR 高度。要比标题栏大 —— 2026-10-04 实测虚拟屏上的微信
# 标题栏会下移到 y≈200，只裁 72px 只剩搜索/加号图标，标题读成空，
# on_wx_tab 判假 → 切群永远失败。改成按行取标题，兼容两种布局。
TITLE_STRIP = 300
# 输入区**上沿**。原来是 1288 —— 那是输入框的**下沿**，等于把整个输入框
# 也算进聊天区（实测输入框白块 y≈1248-1287），输入框里只要有字就会被当成
# 一条群消息。2026-10-04 晚改到 1240（输入框上沿再留 8px 余量）。
CHAT_BOTTOM = 1240
# 微信在输入框正上方还有一条「群工具 / 小程序」快捷栏（白色圆角卡片，
# 636x1400 上实测 y≈1096-1196，左侧带一个圆角群图标）。
# **它不是消息**，但白得超过 248，会被白掩码当成"别人发的气泡"，里面的
# 小程序名就成了群聊内容 —— 用户报的「阿兰发的图片被识别成社群专享抽奖」
# 就是它（阿兰的彩色照片没被检测到，紧接着这条又挂到了他名下）。
BOTTOM_BAR_Y0 = 1085   # 快捷栏上沿允许范围
BOTTOM_BAR_Y1 = 1205   # 快捷栏下沿允许范围
BOTTOM_BAR_MIN_H = 80  # 且够高（普通单行文字气泡 ≈60px，够不到）
LEFT_AVATAR_X = 110    # 左头像列右边缘（白掩码在这里清空，避免头像被当成气泡）
RIGHT_AVATAR_X = 528   # 右头像列左边缘
AVATAR_GAP_X = 140     # 白气泡左边界超过它 => 自己发的（别人一律从 x≈110 起）
GREEN_RATIO_SELF = 0.30
NICK_LUM = 100         # OCR 框内暗色像素平均亮度 > 它 => 灰字（昵称），< 它 => 黑字（正文）
IMG_MIN_H = 200        # 气泡高度超过它 => 直接认定图片/表情消息
IMG_SOFT_H = 130       # 高度在 [130,200) 且几乎没有黑色文字 => 图片
IMG_SELF_X = 300       # 图片气泡左边界超过它才算"自己发的"（图片左边界会缩水，见 detect_bubbles）
# 图片消息的**彩色通道**（照片/表情没有白气泡包着，白绿掩码抓不到）：
IMG_COLOR_MIN_H = 60   # 成片彩色的最小高度
IMG_COLOR_MIN_W = 80   # 最小宽度（比它窄的多半是头像/小图标）
IMG_MERGE_GAP = 120    # 上下两块图片气泡挨得比它近 => 同一张图被切开了，合并
                       # （原来 45 太紧：表情包中间压着一大片浅色，实测被切开 100px）
IMG_EDGE_TOL = 14      # 图片气泡上下沿的归属容差（OCR 框可能压在块外一点，见 _pick_bubble）
# 头像列实测范围（636x1400）：左头像 x≈52-176、右头像 x≈540-634。
# 判断「这条谁发的」最稳的证据是**哪边真有头像** —— 比气泡左边界可靠得多：
# 边界会被头像自己的白像素、圆角、消息长短拉来拉去。2026-10-04 实测：
# 顶部被裁掉昵称的别家消息，左边界 199 > AVATAR_GAP_X(140) 就被判成「我」，
# 面板上出现「机器人：…难杂交出什么东西」。
AVATAR_BAND_L = (52, 176)
AVATAR_BAND_R = (540, 634)
AVATAR_INK = 0.03      # 头像列里「非背景」像素占比超过它 = 这边有头像

# ============================================================================
# ★ 2026-10-12：**低置信行复核**（A）与**同帧昵称一致性校正**（B）
#
# 起因：实测一帧 24 个 OCR 条目，中位置信度 0.999、正文几乎全 1.000，
# 而 2 个 <0.90 的**全是昵称**（浅灰小字）。两个典型案例：
#   「张三」    0.848 → 用同一张图重喂识别（跳过检测）后 **0.99** ✓
#   「张二」0.535 → 复核后仍是错的（真值是同帧另一个 0.998 的「张三」）
# 也就是说：**低分的根源常常是检测阶段把框裁歪了，而不是识别认不出** ——
# 拿已知的框跳过检测重跑一次识别就能救回来，实测只要 0.05 秒/行（det 要 1.5 秒）。
# ⚠️ 别加放大/锐化：rec 的输入高度是固定的，放大后会**被重新缩回去**（实测
#    原样/2x/2x锐化 结果基本一样），而锐化还会引入新错（`小末皎收`/`小木皎蚁`）。
# ⚠️ 更别加二值化：社区实测（dev.to「13 OCR preprocessing tricks」）浅灰小字
#    （#999 on #f5f5f5，最暗像素 153）在阈值 128 下**整条变白**，直接 0 字符 ——
#    我们的昵称正好是这种浅灰小字。
# ============================================================================
LOW_CONF_RETRY = float(os.environ.get("WX_LOW_CONF", "0.90"))       # 低于它才复核
LOW_CONF_ADOPT = float(os.environ.get("WX_LOW_CONF_ADOPT", "0.02"))  # 新分至少高这么多才采纳
LOW_CONF_MAX = int(os.environ.get("WX_LOW_CONF_MAX", "6"))          # 每帧最多复核几行
LOW_CONF_TRUST = float(os.environ.get("WX_LOW_CONF_TRUST", "0.85"))  # 改了文本就要达到这个可信线
NAME_TRUST_CONF = float(os.environ.get("WX_NAME_TRUST", "0.95"))    # 可信昵称的门槛
NAME_FIX_MAX_CONF = float(os.environ.get("WX_NAME_FIX", "0.80"))    # 低于它才尝试校正
NAME_FIX_SIM = float(os.environ.get("WX_NAME_SIM", "0.5"))          # 校正所需的相似度下限

# ============================================================================
# ★ 2026-10-10：**像素常量的自动缩放**（虚拟屏可以切档了）
#
# 背景：虚拟屏由 vs-daemon 创建，支持 `/res` 切"同 dp 档位"：
#     half    636x1400 @280dpi   ← 一直在用的档（像素 1/4，省电、OCR 快）
#     full   1272x2800 @560dpi   ← 跟物理屏一模一样，**微信不再把 UI 放大**
# 实测切到 full 后：群名/会话名完整显示、一屏消息从 6 条变 11 条，
# OCR 只慢 47%（5.7s → 8.4s）。代价就是**下面这些像素常量全部要 ×2**。
#
# 所以这里不再把它们写死成"half 档的值"，而是留一份**基准值（按 636 宽标定）**，
# 读屏时按 `实际图宽 / 636` 重算模块级常量 —— 两个档位都能跑，切档不用改代码。
# ⚠️ 只缩放**像素量**（坐标、高度阈值、容差）；比例（AVATAR_INK / GREEN_RATIO_SELF）、
#    亮度（NICK_LUM）、差值（IMG_REFINE_DIFF）、行数（IMG_REFINE_STOP）**不缩放**。
# ⚠️ 改这些常量时只改下面的基准表，别改上面的定义（定义会被覆盖）。
BASE_WIDTH = 636

_PIXEL_BASE = {
    "CHAT_TOP": 72, "CHAT_BOTTOM": 1240, "TITLE_STRIP": 300,
    "BOTTOM_BAR_Y0": 1085, "BOTTOM_BAR_Y1": 1205, "BOTTOM_BAR_MIN_H": 80,
    "LEFT_AVATAR_X": 110, "RIGHT_AVATAR_X": 528, "AVATAR_GAP_X": 140,
    "IMG_MIN_H": 200, "IMG_SOFT_H": 130, "IMG_SELF_X": 300,
    "IMG_COLOR_MIN_H": 60, "IMG_COLOR_MIN_W": 80,
    "IMG_MERGE_GAP": 120, "IMG_EDGE_TOL": 14,
    "IMG_REFINE_MAX": 220, "IMG_REFINE_PAD_X": 16,
}
_PIXEL_BASE_TUPLES = {
    "AVATAR_BAND_L": (52, 176), "AVATAR_BAND_R": (540, 634),
}
_cur_scale = 1.0

# ---------------------------------------------------------------------------
# 字体适配（v2.0.0）：把「微信不是默认字体」的档案缩放接进版面总闸。
#   档案/配置在 data/ 下；缺段、关掉、读失败一律 scale=1.0（与旧版行为一致）。
#   详见 app/wxauto/fontprofile.py 与《字体与OCR.md》。
# ---------------------------------------------------------------------------
FONT_BASE_LINE_H = 64.0          # full 档（1264 宽）标准字号的正文行高基准
_font_scale = 1.0
_font_engine = "auto"
_font_note = ""
_font_applied = None             # 上一次真正套用的总缩放（分辨率 × 字体）
_font_cfg_stamp = {"t": -1.0}


def _data_dir_for_font():
    env = (os.environ.get("WXBOT_DATA_DIR") or "").strip()
    if env:
        return env
    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sib = os.path.join(os.path.dirname(app), "data")
    return sib if os.path.isdir(sib) else app


def _profile_scale(name):
    """读字体档案 → 缩放倍数（读不到返回 0 = 让调用方走别的分支）。"""
    try:
        import json
        fp = os.path.join(_data_dir_for_font(), "font_profiles", "%s.json" % name)
        with open(fp, encoding="utf-8") as fh:
            d = json.load(fh)
        v = float(d.get("scale") or 0)
        if v <= 0:
            sz = float(d.get("size_px") or 0)
            v = (sz / FONT_BASE_LINE_H) if sz > 0 else 0.0
        return max(0.5, min(2.5, v)) if v > 0 else 0.0
    except Exception:
        return 0.0


def reload_font_config(force=False):
    """读 data/config.yaml → read.font（按 mtime 热更新，开销只有一次 stat）。"""
    global _font_scale, _font_engine, _font_note, _font_applied
    env = (os.environ.get("WX_FONT_SCALE") or "").strip()
    if env:
        try:
            v = max(0.5, min(2.5, float(env)))
        except Exception:
            v = 1.0
        if abs(v - _font_scale) > 1e-6:
            _font_scale, _font_note, _font_applied = v, "env:WX_FONT_SCALE", None
            print("[wxreader] 字体缩放 ×%.3f（环境变量 WX_FONT_SCALE）" % v, flush=True)
        _font_cfg_stamp["t"] = -2.0        # 保证 env 撤销后 config 会被重读
        return _font_scale
    cfgp = os.path.join(_data_dir_for_font(), "config.yaml")
    try:
        mt = os.path.getmtime(cfgp)
    except OSError:
        mt = -1.0
    if not force and abs(mt - _font_cfg_stamp["t"]) < 1e-6:
        return _font_scale
    _font_cfg_stamp["t"] = mt
    scale, engine, note = 1.0, "auto", ""
    try:
        import yaml
        with open(cfgp, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        fc = ((cfg.get("read") or {}).get("font") or {})
        if fc.get("enabled"):
            engine = str(fc.get("engine") or "auto").lower()
            try:
                raw = float(fc.get("scale") or 0)
            except Exception:
                raw = 0.0
            try:
                sz = float(fc.get("size_px") or 0)
            except Exception:
                sz = 0.0
            name = str(fc.get("profile") or "").strip()
            if raw and abs(raw - 1.0) > 1e-6:
                scale, note = max(0.5, min(2.5, raw)), "config.scale"
            elif sz > 0:
                scale, note = max(0.5, min(2.5, sz / FONT_BASE_LINE_H)), "config.size_px"
            elif name:
                v = _profile_scale(name)
                if v > 0:
                    scale, note = v, "profile:%s" % name
    except Exception:
        scale, engine, note = 1.0, "auto", ""
    if abs(scale - _font_scale) > 1e-6 or engine != _font_engine:
        print("[wxreader] 字体适配：×%.3f（%s，engine=%s）"
              % (scale, note or "默认", engine), flush=True)
    _font_scale, _font_engine, _font_note = scale, engine, note
    _font_applied = None            # 强制下一帧重算版面常量
    return _font_scale


def font_state():
    return {"scale": _font_scale, "engine": _font_engine, "note": _font_note}



def scale():
    """当前档位的缩放因子（相对 636 宽的 half 档）。"""
    return _cur_scale


def apply_resolution(width, height=None):
    """按截图的**像素宽度**把所有像素常量缩放到当前档位。

    ``parse_snapshot`` 每次读屏都会调一次；缩放因子没变就直接返回（几乎零开销）。
    ``height`` 可选，只喂给 `_fix_chat_bottom` —— 贴底锚点按图高走，不按图宽等比。
    """
    global _cur_scale, _font_applied
    try:
        k = float(width) / float(BASE_WIDTH)
    except (TypeError, ValueError, ZeroDivisionError):
        return _cur_scale
    if k <= 0:
        return _cur_scale
    _cur_scale = k
    reload_font_config()                      # v2.0.0：字体档案（有 mtime 缓存）
    total = k * _font_scale
    if _font_applied is None or abs(total - _font_applied) >= 0.02:
        g = globals()
        for name, base in _PIXEL_BASE.items():
            if name in g:
                g[name] = int(round(base * total))
        for name, base in _PIXEL_BASE_TUPLES.items():
            if name in g:
                g[name] = tuple(int(round(v * total)) for v in base)
        _font_applied = total
        print("[wxreader] 像素常量按 %.0f%% 缩放（图宽 %d，基准 %d，字体×%.2f）"
              % (total * 100, width, BASE_WIDTH, _font_scale), flush=True)
    # ⚠️ 聊天区下边界每次都重算：它按**图高**锚定，而图高不在上面的缓存键里。
    _fix_chat_bottom(width, height, total)
    return k


def _fix_chat_bottom(width, height, total):
    """聊天区下边界（＝输入框上沿）：**贴底锚点，不按图宽等比**。

    half 档（636 宽）基准 1240 本来是对的（输入框白块实测 y≈1248-1287）。
    但 full 档（1264x2780）实测输入区上沿在 **≈2570**，而按图宽等比只到
    1240×1.9874 = **2464** —— 少了 96px，正好把**最底部那条最新消息**切掉：
    2026-10-13 实测用户 @ 机器人的绿气泡真实范围 2438→2563，被截成
    2438→2463（25px，只够一行字的上半截），OCR 直接读不出 → 机器人像
    "没看见"，被 @ 也不回；同一张图完整气泡的 OCR 置信度是 0.9997。
    （同类坑：AVATAR_BAND_L 那次 —— 按图宽等比缩放在边缘锚定元素上不成立。）

    判据：竖屏 + 高清档（宽 ≥1000 且高 > 1.5×宽）→ 取图高的 0.921
    （2780 × 0.921 = 2560，距输入区上沿留 10px）；其余档位保持基准等比，
    免得 half 档把输入框吃进聊天区（旧注释里"输入框里的字被当消息"的回归）。
    """
    g = globals()
    if not height or width < 1000 or height <= width * 1.5:
        g["CHAT_BOTTOM"] = int(round(_PIXEL_BASE["CHAT_BOTTOM"] * total))
        return
    g["CHAT_BOTTOM"] = int(round(float(height) * 0.921))

TIME_RE = re.compile(r"^(上午|下午|凌晨|中午|晚上)?\s*\d{1,2}[:：]\d{2}$")
SYS_HINT = ("撤回", "以下是新消息", "条新消息", "消息已发出", "对方正在输入",
            "加入了群聊", "领取了", "群公告", "邀请你", "移出了群聊")
# ⚠️ 「拍了拍」2026-10-06 从 SYS_HINT 里**拿出来**了：用户要"被拍脑袋就挠回去"，
#    所以它得能传到机器人手里。单独标成 kind="pat"（见下面 parse_snapshot 里那段），
#    这样它既不会掉进普通聊天内容那条路（免得被关键词/人设乱回），又会出现在
#    /snapshot 里（wxbridge 只过滤 time/system，pat 照常放行）。
TITLE_TAIL_RE = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")
TITLE_HEAD_RE = re.compile(r"^[\s\d,，]+")

_ocr = None
_ocr_v4 = None
_ocr_lock = threading.Lock()

# 识别/检测模型（项目 models/ 下）；字典内嵌在 onnx 的 metadata 里，
# 所以换模型只要指个路径，不用配字典。可以用 WX_OCR_REC 覆盖（别的路径，或 off 关掉）。
#
# ★ 2026-10-12：**换成 PP-OCRv6 small**（用户要求「OCR 精准度提升」→ 社区查到 v6 已发布）。
#   实测（同一帧真实群聊截图，1264x2780，max_side_len=3000、关 cls）：
#       v4 det + v5 server rec：89MB，**5.4s**，13 行
#       v6 tiny：6MB，1.2s，13 行（但**标题读成「目标群(109)及」，多一个"及"** → 不能用于标题）
#       **v6 small：31MB，3.2s，13 行** ← 选它：内容与 v5 逐行一致（引号还更完整）
#       v6 medium：139MB，11.0s，16 行（多出 `0`/`0`/`十` 三行**噪声** → 不划算）
#   ⚠️ medium 反而更差 —— 官方 benchmark 测的是**扫描文档**，我们是**屏幕截图**
#      （抗锯齿 + JPEG 压缩 + 一堆不该读的 UI 元素）。
#   ⚠️ **标题继续用 v4**（`get_ocr(v4=True)`）：v6 tiny 已验证会读错标题，而标题读错
#      会去白切群（2026-10-08 那场"反复切群失败"的老病根）。v6 small 单帧标题是对的，
#      但这条路径不值得赌 —— 标题只裁顶部一条，省不出多少速度。
#   回退：`mv models/PP-OCRv6_*.onnx /tmp/` 再 `bash wxbridge_ctl.sh restart`
#        （下面的名字链会自动退到 v5 server / 包内 v4）。
REC_MODEL_NAMES = ("PP-OCRv6_rec_small.onnx",
                   "ch_PP-OCRv5_rec_server.onnx", "ch_PP-OCRv5_rec_mobile.onnx")
DET_MODEL_NAMES = ("PP-OCRv6_det_small.onnx",)
REC_MODEL_NAME = "ch_PP-OCRv5_rec_mobile.onnx"      # 旧名，留着手工指路时用


def rec_model_path():
    """返回可用的 v5 识别模型路径；没有就返回空串（调用方回退包内默认模型）。

    ⚠️ 这里**显式传路径**、而不是去改 /root/ocrlibs 里的 config.yaml：那个运行时
      可以从 /sdcard/dsh/ocrlibs.tar.gz 重建，改在里面的东西会被重装冲掉；
      而这份代码和模型都在 /sdcard 项目里，重建后照样生效。
    """
    import os
    env = os.environ.get("WX_OCR_REC", "")
    if env.lower() in ("off", "0", "no"):
        return ""
    if env:
        return env if os.path.exists(env) else ""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in REC_MODEL_NAMES:
        p = os.path.join(base, "models", name)
        if os.path.exists(p):
            return p
    return ""


def det_model_path():
    """返回可用的 v6 检测模型路径；没有就返回空串（回退包内自带的 v4 det）。"""
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in DET_MODEL_NAMES:
        p = os.path.join(base, "models", name)
        if os.path.exists(p):
            return p
    return ""


def _ocr_thread_kw():
    """OCR 线程数上限（★ 2026-10-10 省电）。

    RapidOCR 默认 ``intra_op_num_threads=-1`` = onnxruntime 自己开满全部核（本机 8 个），
    而 ``OMP_NUM_THREADS`` 管不到 onnxruntime 自己的线程池。实测桥接读屏一爆发就冲到
    450%~650% 单核。同一张 1264×2780 现场帧、同参数、各跑 4 次取均值：

        线程数     墙钟      CPU 时间（≈耗电）
        默认(8)    9.5s      35.0s
        4          11.3s     23.3s
        2          12.7s     16.8s   ← 默认取它：CPU 省 52%，单帧只慢约三成
        1          21.6s     18.8s   （更慢还不更省）

    机器人读屏请求超时 30s，2 线程余量足够。环境变量 ``WX_OCR_THREADS`` 可覆盖
    （0 = 不限制，回到 onnxruntime 默认）。
    """
    import os
    try:
        n = int(os.environ.get("WX_OCR_THREADS", "2"))
    except ValueError:
        n = 2
    if n <= 0:
        return {}
    return {"intra_op_num_threads": n, "inter_op_num_threads": 1}


def get_ocr(v4: bool = False):
    """RapidOCR 单例：首次加载约 1.5 秒，之后每次 0.4~0.8 秒。

    ``v4=True`` 拿的是**旧的 PP-OCRv4 mobile** 引擎（包内自带）—— 只有读标题用它。
    为什么标题不跟正文一起升级 v5：标题那行是「「...10)」这种只剩省略号+人数的
    极短噪声，v5 反而更差 —— 40 帧实测 v4 的 title_matches 全过，v5 有 3 帧读成
    `厂 .` / `.10` / `D`，判「不在目标群」→ 机器人会去白切群（2026-10-08 那场
    "反复切群失败"的老病根）。正文才是 v5 的主场，标题留在 v4。
    """
    global _ocr, _ocr_v4
    if v4:
        if _ocr_v4 is None:
            with _ocr_lock:
                if _ocr_v4 is None:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        from rapidocr_onnxruntime import RapidOCR
                        _ocr_v4 = RapidOCR(**_ocr_thread_kw())   # 包内默认 = v4 mobile
        return _ocr_v4
    if _ocr is None:
        with _ocr_lock:
            if _ocr is None:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    from rapidocr_onnxruntime import RapidOCR
                    p = rec_model_path()
                    # ★ 2026-10-10（用户要求「读屏能更清晰一点吗」）：两处画质相关设置。
                    #   ① max_side_len 默认 **2000**，而我们的截图是 1272×2800 —— 也就是
                    #      每次 OCR 前图都被缩到长边 2000（丢约 29% 细节）。设 3000 = 不缩。
                    #   ② use_cls 是「每行判断是否倒置 180°」的方向分类器；微信聊天全是
                    #      正向横排，关掉它省时间（实测把它省下的部分抵掉了 ①的代价）。
                    #   实测同一帧（20 行那条现场）：默认 4.09s → 新设置 5.39s、多读出 2 处
                    #   原来漏掉的图上小字。代价每轮 +1.3 秒，想换回速度就删掉这两个参数。
                    kw = {"rec_model_path": p} if p else {}
                    d = det_model_path()          # ★ 2026-10-12：v6 检测模型（没有就回退 v4）
                    if d:
                        kw["det_model_path"] = d
                    kw.update({"max_side_len": 3000, "use_cls": False})
                    kw.update(_ocr_thread_kw())      # ★ 2026-10-10 省电：限线程数
                    _ocr = RapidOCR(**kw)
    return _ocr


_ocr_call_lock = threading.Lock()


def ocr_call(img, v4: bool = False, rec_only: bool = False):
    """统一 OCR 入口 —— **所有**调用都必须走这里，串行化。

    为什么：桥接服务是 ThreadingHTTPServer，而 RapidOCR / onnxruntime 的
    session **不是线程安全的**。并发调用（/snapshot、/open、/title 撞在一起）
    会让它**静默返回空结果、且不报错**。
    2026-10-03 实测：同一张截图、同一份代码 —— 桥接进程里 read_title 返回 ''，
    独立进程返回 '微'；后果就是「切群」永远失败、机器人一启动就退出。
    串行化之后恢复正常。

    ``v4=True`` = 用旧引擎读（只有 read_title 这么调，原因见 get_ocr 的说明）。
    """
    with _ocr_call_lock:
        eng = get_ocr(v4=v4)
        # ★ 2026-10-12（A）：rec_only —— 跳过检测网络，只对**已知是文字**的裁剪图
        #   跑识别。实测 0.04~0.05s，而带检测要 0.5~1.6s（快 10~30 倍），
        #   而且对浅灰小字往往**更准**（检测到的小图常常直接返回空）。
        #   ⚠️ 关掉检测时返回的是**两元组** [text, score]（不是三元组），
        #      而且**不走 text_score 过滤**，所以低分结果也拿得到。
        if rec_only:
            return eng(img, use_det=False)
        return eng(img)


def _green_mask(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (g > 180) & (g - b > 40) & (g - r > 25)


def _row_blocks(mask, ytop, ybot, min_h=10, gap=4, min_px=3):
    sub = mask[ytop:ybot]
    ys = np.where(sub.sum(axis=1) >= min_px)[0]
    if len(ys) == 0:
        return []
    blocks, start, prev = [], ys[0], ys[0]
    for y in ys[1:]:
        if y - prev > gap:
            if prev - start + 1 >= min_h:
                blocks.append((int(start + ytop), int(prev + ytop)))
            start = y
        prev = y
    if prev - start + 1 >= min_h:
        blocks.append((int(start + ytop), int(prev + ytop)))
    return blocks


def detect_bubbles(a):
    """返回 [{'y0','y1','x0','x1','is_self','image'}]，按 y 升序。"""
    green = _green_mask(a).copy()
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    white = (r > 248) & (g > 248) & (b > 248)
    # 图片/表情消息**没有气泡底色**（照片直接以圆角矩形显示），白/绿掩码都抓不到，
    # 整条消息会凭空消失 —— 所以再开一条彩色通道（见函数末尾）。
    colorful = (a.max(axis=2) - a.min(axis=2)) > 30
    # 头像常带白/绿区域（青蛙头像就是绿的），会把昵称行连成假气泡
    # —— 先把左右头像列整列从掩码里清掉。
    # ⚠️ 2026-10-05：左边界从 LEFT_AVATAR_X(110) 收到 AVATAR_BAND_L[1]+1(177)。
    #    头像列实测到 x≈176，只清到 110 时头像右半边的**彩色**会自成一块
    #    （实测「🌟林林孖 素问」那条：彩色块 y=196-287、x=110-245，宽 136 过了
    #    IMG_COLOR_MIN_W=80 那道宽度检查），接着被彩色通道的 ④ 分支当成
    #    "同一张图的另一部分"并与它下面真正的白文字气泡合并 —— 于是文字气泡被
    #    标成 image=True，昵称「麻子素问」和正文「有没有门票车」一起被吞进
    #    `[图片] 麻子素问 有没有门票车`（用户 2026-10-05 报的"读屏还是有点问题"）。
    #    内容区左边界实测 ≈183（头像列 52-176 之后还有 7px 空隙），清到 176
    #    不会碰到任何真实图片/气泡的左边缘。
    for m in (green, white, colorful):
        m[:, :AVATAR_BAND_L[1] + 1] = False
        m[:, RIGHT_AVATAR_X + 1:] = False
    out = []
    for y0, y1 in _row_blocks(green, CHAT_TOP, CHAT_BOTTOM):
        cols = np.where(green[y0:y1 + 1].any(axis=0))[0]
        if len(cols) == 0:
            continue
        out.append({"y0": y0, "y1": y1, "x0": int(cols[0]), "x1": int(cols[-1]),
                    "is_self": True, "image": (y1 - y0 + 1) >= IMG_MIN_H,
                    "_src": "green"})
    for y0, y1 in _row_blocks(white, CHAT_TOP, CHAT_BOTTOM, min_h=32):
        cols = np.where(white[y0:y1 + 1].any(axis=0))[0]
        if len(cols) == 0:
            continue
        # ⚠️ 底部「群工具 / 小程序」快捷栏：白得发光、位置固定、高度固定，会被
        #    当成别人发的气泡（用户报的「阿兰发图片 → 面板显示社群专享抽奖」）。
        #    判定 = 整块落在实测窗口里 + 够高；普通消息气泡要同时满足这两个条件
        #    概率很低。真误伤了就把窗口收窄（常量在文件头）。
        if (BOTTOM_BAR_Y0 <= y0 and y1 <= BOTTOM_BAR_Y1
                and (y1 - y0 + 1) >= BOTTOM_BAR_MIN_H):
            continue
        x0, x1 = int(cols[0]), int(cols[-1])
        # 归属看**哪边真有头像**（_who_sent），不再拿气泡左边界猜 —— 边界会被
        # 头像白像素、圆角、消息长短带偏（2026-10-04 实测：顶部被裁掉昵称的
        # 别家消息，左边界 199 > AVATAR_GAP_X(140) 就被判成「我」）。
        is_self = _who_sent(a, y0, y1, x0, x1)
        img = _looks_image(a, y0, y1, x0, x1)
        if img and x0 <= IMG_SELF_X and not _has_avatar(a, y0, y1, *AVATAR_BAND_R):
            # ⚠️ 图片气泡的左边界会"缩水"：表情包左边常是浅色/粉色，纯白像素从
            #    x≈193 才开始，于是被当成"靠右的自己人"（实测「没眼看」那张被记成
            #    「机器人：没眼看」，用户以为机器人在群里乱说话）。
            #    靠左 **且右侧没有头像** 才当别人；右侧真有头像说明就是自己发的图。
            is_self = False
        out.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1, "is_self": is_self,
                    "image": img, "_src": "white"})
    # 彩色通道：成片彩色 = 照片 / 表情包（没有白绿底色、上面两条通道抓不到的）。
    # 2026-10-04 实测阿兰发的照片：y≈650-1040、彩色占比 0.5+、纯白 0.00 ——
    # 老代码一个气泡都没有，他这条消息在面板上整条消失，紧随其后的底部快捷栏
    # 反倒成了「张三 社群专享抽奖...」。
    spans = [(o["y0"], o["y1"]) for o in out]
    # min_px 从 40 降到 10、gap 保持 6：照片里常有一段低饱和（浅色墙、白衣服），
    # 每行彩色像素掉到 40 以下就把同一张照片切成上下两块 —— 下半块会变成一条
    # "自己发的 [图片]"（实测 dbg-snap3 就是这样）。降到 10 能让那段重新连上，
    # 又不至于像放大 gap 那样把照片和它下面的绿气泡（实测只隔 38px）糊成一条。
    for y0, y1 in _row_blocks(colorful, CHAT_TOP, CHAT_BOTTOM,
                              min_h=IMG_COLOR_MIN_H, gap=6, min_px=10):
        cols = np.where(colorful[y0:y1 + 1].any(axis=0))[0]
        if len(cols) == 0:
            continue
        x0, x1 = int(cols[0]), int(cols[-1])
        if x0 <= AVATAR_BAND_L[1]:
            # 落在左头像列里 = 头像残留（头像自己就是彩色的），不是图片。
            # 掩码上面已经清到 177，这里是第二道保险：万一头像残留的彩色块活到
            # 这一步，它会在下面的 ④ 分支里和真实文字气泡重叠、把气泡升成
            # [图片]（2026-10-05 实测的 bug），宁可提前丢掉。
            continue
        if x1 - x0 + 1 < IMG_COLOR_MIN_W:
            continue                       # 太窄：多半是图标/头像残影
        # 与白/绿通道已经抓到的块重叠 = **同一张图的另一部分**（照片里的浅色区、
        # 白边，或白底表情包）。这里必须**合并**，不能像老代码那样「只要有 y 重叠
        # 就整块 continue」—— 低饱和照片（浅蓝背景、米色衣服这种）大片像素卡在
        # colorful>30 的边缘，彩色通道只能抓到其中一段，结果整张照片被丢掉，
        # 面板上只剩包住昵称行的那条白块（用户在群里发的娃照就这么没的）。
        # 反过来，"彩色块完全落在已有块里"就是同一个东西（典型是绿气泡自己），
        # 直接跳过，绝不能把文字气泡标成图片。
        hit = None
        for o in out:
            if not (y1 < o["y0"] or y0 > o["y1"]):
                hit = o
                break
        if hit is not None:
            hb = hit["y1"] - hit["y0"] + 1
            hc = y1 - y0 + 1
            # ① hit 是绿通道的块、且和彩色块差不多高 = 就是同一个绿气泡（自己的
            #    文字气泡本来就是绿的，在彩色通道里当然也是"成片彩色"）。跳过，
            #    绝不能把它升成 [图片] —— 那样机器人会把自己发的一句话读成图片
            #    消息，发送自检当场失败。容差给足：两个通道的边界天然会差几像素。
            if hit.get("_src") == "green" and hc <= hb * 1.5 + 20:
                continue
            # ② 彩色块整个落在已有块里 = 同一个东西（白底表情包、绿气泡）
            if y0 >= hit["y0"] - 6 and y1 <= hit["y1"] + 6:
                continue
            # ③ hit 只是绿通道抓到的一小块残渣（实测：小程序卡片上一个 17px 高的
            #    绿图标），而彩色块大得多（下面是整张表情包）。以彩色块为准，
            #    否则整张表情包会被这块残渣"吃掉"、在面板上消失。
            if hit.get("_src") == "green":
                for i, o in enumerate(out):
                    if o is hit:
                        del out[i]
                        break
                out.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1,
                            "is_self": _who_sent(a, y0, y1, x0, x1), "image": True,
                            "_src": "color"})
                continue
            # ④ 同一条消息的另一部分（照片的浅色区、白边）：把范围并起来
            hit["y0"] = min(hit["y0"], y0)
            hit["y1"] = max(hit["y1"], y1)
            hit["x0"] = min(hit["x0"], x0)
            hit["x1"] = max(hit["x1"], x1)
            hit["image"] = True
            continue
        out.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1,
                    "is_self": _who_sent(a, y0, y1, x0, x1), "image": True,
                    "_src": "color"})
    out.sort(key=lambda d: (d["y0"], d["x0"]))
    return out


def _looks_image(a, y0, y1, x0, x1):
    """气泡是图片/表情还是文本 —— **彩色优先**。

    判据：
      · 有彩色内容（通道差 > 30 的像素占比 > 5%）→ 图片/表情；
        文字气泡是白底黑字/灰字，几乎没有彩色。
      · 否则：很高 **且** 几乎没有黑色笔画 → 图片（纯色背景的大图）。

    ⚠️ 这个函数 2026-10-04 被连改两次，两个坑都要躲：
      · 老代码「高度 ≥ IMG_MIN_H(200) 就算图片」→ 把「昵称行 + 两行消息」粘成的
        243px 白块整块当图片，昵称被吞进正文、几条消息并成一条
        （用户报的「阿兰说了挺多，机器人那边只显示一条」）；
      · 改成「高大也要没有黑字」之后，又反过来把**带大字的表情包**（「没眼看」）
        当成文字气泡 —— 那张图于是变成一条文字消息，再被位置判据判成"自己发的"，
        面板上出现「机器人：没眼看」（用户以为机器人在群里乱说话）。
      彩色判据能同时躲开这两个坑：文字气泡无彩，带字表情包有彩。
    """
    sub = a[y0:y1 + 1, x0:x1 + 1]
    if sub.size == 0:
        return False
    g = sub.mean(axis=2)
    dark = float((g < 100).mean())
    color = (sub.max(axis=2) - sub.min(axis=2)) > 30
    # ⚠️ 左头像列里的彩色**不是图片证据**：头像本身就带颜色（青蛙绿的、腮红粉的），
    #    白掩码只清到 LEFT_AVATAR_X(110)，而实测头像列一直延伸到 x≈176 ——
    #    残留的彩色会把「昵称行那块白像素」染成 3% 彩色，整块被判成图片。
    #    后果（2026-10-04 用户报）：头像变成一条假的 [图片]，三条消息显示成四条，
    #    昵称被吞进图片文字、后面几条消息的 sender 全丢。
    #    只把 x < AVATAR_BAND_L[1] 的彩色挖掉再算比例，分母仍是整块面积 ——
    #    这样处理不会改变其它白色卡片（底部快捷栏）的彩色占比，避免连带误判。
    cut = max(0, AVATAR_BAND_L[1] - int(x0) + 1)
    if cut:
        color[:, :cut] = False
    colorful = float(color.mean())
    # 阈值取 0.02：实测文字气泡几乎**一个彩色像素都没有**（0.000），
    # 而白毛表情包「没眼看」也有 0.025 —— 差距不大，所以宁可贴着 0.02 走，
    # 别把阈值抬到 0.05（那样这张图会被当文字，再被位置判据判成"机器人说的"）。
    if colorful > 0.02:
        # ⚠️ 2026-10-05：彩色得**成片**才算图片 —— 文字消息里的 emoji 也是彩色的。
        #    实测 /tmp/frame.jpg 那条「我们这黄焖鸡 大份25😭」：哭脸 emoji 只占
        #    y=724-784、x=419-476 一小块（彩色占比 4.15% > 2%），老代码据此把整条
        #    白底黑字的**文字消息**判成 [图片]；它又因为 IMG_MERGE_GAP(120) 被并进
        #    上面那张广告图，面板上成了
        #    `[图片] 生鸡现煲肉香下饭 22 元/小份 我们这黄焖鸡 大份25`，
        #    昵称「李四」也跟着丢了。
        #    真实图片/表情包的彩色会铺开（那张广告图的跨度比 ≈0.99），emoji 只挤在
        #    一角，所以用彩色像素的**跨度占比** + 黑字不多来摘 emoji：
        #      · 「文字 + emoji」实测 dark=0.084、彩色跨度 0.17/0.32；
        #      · 白底大字表情包「没眼看」（必须仍是图片）dark=0.236/0.249、
        #        跨度 0.36/0.38 —— 大字笔画多，dark 一眼就能分开，所以卡 0.15。
        #    彩色小而**没有**黑字、或黑字一大片的块（真表情包）都仍旧按图片处理。
        ys = np.where(color.any(axis=1))[0]
        xs = np.where(color.any(axis=0))[0]
        if len(ys) and len(xs) and 0.02 < dark < 0.15:
            span_x = (xs[-1] - xs[0] + 1) / float(color.shape[1])
            span_y = (ys[-1] - ys[0] + 1) / float(color.shape[0])
            if span_x >= 0.45 or span_y >= 0.45:
                return True
            # 彩色只挤在一小块 = 文字里的 emoji / 小图标，落到下面的高度兜底再判
        else:
            return True
    hh = y1 - y0 + 1
    return bool(hh >= IMG_MIN_H and dark < 0.02)


def _has_avatar(a, y0, y1, x0, x1):
    """这一行（留点上下余量）里，指定头像列上有没有真头像。"""
    y0 = max(0, int(y0) - 8)
    y1 = min(a.shape[0] - 1, int(y1) + 12)
    band = a[y0:y1 + 1, int(x0):int(x1)]
    if band.size == 0:
        return False
    # 聊天背景是浅灰（#EDEDED 左右）；头像上有明显的深浅/彩色像素
    return float((band.min(axis=2) < 205).mean()) > AVATAR_INK


def _who_sent(a, y0, y1, x0, x1):
    """这条气泡是谁发的：看左右哪边真有头像（群聊里每条消息都带头像）。

    两边都没头像时保守判「别人」：这多半是屏幕边缘裁断的残块，而自己发的消息
    一定带右侧头像；判成「自己」会让聊天流水凭空多出一条「机器人：…」。
    """
    left = _has_avatar(a, y0, y1, *AVATAR_BAND_L)
    right = _has_avatar(a, y0, y1, *AVATAR_BAND_R)
    if right and not left:
        return True
    return False


def _similar_name(a, b, thr=0.6):
    """两个昵称是不是同一个人（容忍 OCR 抖动）：剥掉装饰符再比。"""
    def norm(x):
        return re.sub(r"[^\w\u4e00-\u9fff]", "", str(x or ""))
    ca, cb = norm(a), norm(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if ca in cb or cb in ca:
        return min(len(ca), len(cb)) >= 2
    return difflib.SequenceMatcher(None, ca, cb).ratio() >= thr


def _resolve_data_dir():
    """v2.0.0：名字库在 data/ 下（见 app/wxauto/paths.py 的口径）。"""
    _env = (os.environ.get("WXBOT_DATA_DIR") or "").strip()
    if _env:
        return _env
    _app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _sib = os.path.join(os.path.dirname(_app), "data")
    return _sib if os.path.isdir(_sib) else _app


_NAMEBOOK_FILE = os.path.join(_resolve_data_dir(), "名字库.json")
_namebook_cache = {"t": 0.0, "n2": {}}


def _norm_name(x):
    """归一化昵称：去掉空格/标点/装饰符。

    ⚠️ 必须和 build_namebook.py 的 norm() 保持一致，否则两边匹配不上。
    """
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(x or ""))


def _namebook():
    """加载名字库（按 mtime 热更新）。读不到就返回空 dict —— 调用方必须能降级。"""
    try:
        mt = os.path.getmtime(_NAMEBOOK_FILE)
    except OSError:
        return {}
    if mt != _namebook_cache["t"]:
        try:
            with open(_NAMEBOOK_FILE, encoding="utf-8") as f:
                names = (json.load(f) or {}).get("names") or {}
            _namebook_cache["n2"] = {_norm_name(n): n for n in names if _norm_name(n)}
            _namebook_cache["t"] = mt
        except Exception as exc:
            print("[wxreader] 名字库读取失败（保持上一次）：%r" % (exc,), flush=True)
    return _namebook_cache["n2"]


def _split_known(name):
    """用名字库把「昵称：正文」切开，返回库里的**标准写法**；认不出就原样返回。

    实测 chat_log 里 **5~8% 的 sender 是昵称和正文被 OCR 粘成一个框**
    （``某某某某：因为我们宿舍六点半大门才开``）—— 同一个人会被 memory 记成好几十个人。
    拿名字库做前缀匹配就能把昵称切出来（«长的优先»，避免被短名截胡）。
    全量实测：531 条里切开 29 条，不同 sender **89 个 → 67 个**。
    """
    n2 = _namebook()
    if not n2:
        return name
    k = _norm_name(name)
    if not k:
        return name
    if k in n2:
        return n2[k]
    best = None
    for key, std in n2.items():
        if len(key) >= 2 and k.startswith(key):
            if best is None or len(key) > len(best[0]):
                best = (key, std)
    return best[1] if best else name


def _name_ratio(a, b):
    """两个昵称的字面相似度（0~1，difflib 口径）。"""
    return difflib.SequenceMatcher(None, str(a or ""), str(b or "")).ratio()


def _rec_only_line(img, box, pad=None):
    """对**已知是文字**的一个框重跑识别（跳过检测）。

    返回 ``(text, score)`` 取分数最高的一条；读不出返回 None。
    为什么不用检测：实测同一张裁剪图，带检测常常一个框都检不出来（返回空），
    而直接喂识别反而能读出来 —— 见 LOW_CONF_RETRY 上面那段说明。
    """
    try:
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
    except Exception:
        return None
    if not xs or not ys:
        return None
    if pad is None:
        pad = max(2, int(round(6 * _cur_scale)))
    crop = img.crop((max(0, min(xs) - pad), max(0, min(ys) - pad),
                     min(img.width, max(xs) + pad), min(img.height, max(ys) + pad)))
    if crop.width < 4 or crop.height < 4:
        return None
    res, _ = ocr_call(np.array(crop), rec_only=True)
    best = None
    for x in (res or []):
        # 关掉检测时是 [text, score]；开着是 [box, text, score]
        txt, sc = (x[0], float(x[1])) if len(x) == 2 else (x[1], float(x[2]))
        txt = str(txt).strip()
        if not txt:
            continue
        if best is None or sc > best[1]:
            best = (txt, sc)
    return best


def _retry_low_conf(img, res):
    """A：把置信度偏低的条目用「原框 + 只跑识别」复核一遍。

    只在新结果分数**明显更高**时才替换（默认 +0.02），所以它不会把已经读对的
    条目改坏 —— 最坏情况是原地不动。返回 ``(新res, 改了几条)``。
    """
    if not res or LOW_CONF_MAX <= 0:
        return res, 0
    idx = [i for i, it in enumerate(res) if float(it[2]) < LOW_CONF_RETRY]
    if not idx:
        return res, 0
    idx.sort(key=lambda i: float(res[i][2]))      # 最不可信的先复核
    fixed = 0
    for i in idx[:LOW_CONF_MAX]:
        it = res[i]
        try:
            got = _rec_only_line(img, it[0])
        except Exception as exc:                  # pragma: no cover
            print("[wxreader] 低置信复核失败：%r" % (exc,), flush=True)
            continue
        if not got:
            continue
        txt, sc = got
        old_txt, old_sc = str(it[1]), float(it[2])
        if sc <= old_sc + LOW_CONF_ADOPT:
            continue
        # 文本没变（只是分数更可信）→ 直接采纳；
        # 文本变了 → 必须达到可信线，否则宁可保留原样：
        #   实测「张二」0.535 复核成「小末皎收」0.709 —— 分数高了但**还是错的**，
        #   这种"更自信的错"比低分更难被后面的兜底发现（B 只看 conf<0.80）。
        #   0.85 也是 A 和 B 的分工线：A 只收"有把握的纠正"，不确定的留给 B。
        if txt != old_txt and sc < LOW_CONF_TRUST:
            continue
        res[i] = [it[0], txt, sc]
        fixed += 1
    if fixed:
        print("[wxreader] 低置信复核：改了 %d 条" % fixed, flush=True)
    return res, fixed


# ---------------------------------------------------------------- 气泡漏行补救
# ★ 2026-10-12（用户报「微信里看看腿只被识别成了看看」）
#   那条消息在气泡里换行成「@主人昵称（艾特我召唤猫娘）看看」+「腿」，第二行
#   **只有一个字** —— 检测网络（DBNet）对这种孤立的小字行很不敏感，那一帧整行
#   漏检，正文就只剩「看看」，于是 `rules.yaml` 的「看看腿」永远命不中，
#   用户看到的是"它读了、但读少了一个字"。
#   实测（同一帧 tui_frame.jpg）：把气泡下半部分单独裁出来再识别，**立刻读出
#   「腿」(score 1.000)**。
#   ⚠️ 用 rec_only 而不是重跑整套 pipeline：860×110 的裁剪图 det+rec 要 **3.3 秒**
#      （等于半帧读屏的开销），rec_only 只要 **0.04~0.58 秒**，两者都读出了「腿」。
#      rec 是单行识别 —— 正好，这里要补的就是一行。
#   ⚠️ 判据只用**几何**（气泡下沿还剩多少空白），不看置信度：漏掉的行**根本没有
#      条目**，没有分数可看（和 A 的"低置信复核"是互补的两件事）。
BUBBLE_RESCUE = os.environ.get("WX_BUBBLE_RESCUE", "1") not in ("0", "", "false", "False")
BUBBLE_RESCUE_MAX = int(os.environ.get("WX_BUBBLE_RESCUE_MAX", "2"))
BUBBLE_RESCUE_GAP = float(os.environ.get("WX_BUBBLE_RESCUE_GAP", "0.55"))


def _rescue_bubble_tail(img_rgb, items, bubbles):
    """气泡里只读到上半部分时，把下半部分补读一次（返回可能扩充过的 items）。

    正常气泡里的文字是**贴着下沿**的（1264 档实测：文字底距气泡底 ≈23px、
    行高 ≈70px），所以 `gap > 0.55 × 行高` 只在真漏行时命中，不会误伤正常帧。
    """
    if not BUBBLE_RESCUE or BUBBLE_RESCUE_MAX <= 0:
        return items
    items = list(items)          # 不动调用方的列表（原地 append 会让调用方/测试踩坑）
    groups = {}
    for it in items:
        if it.get("kind") != "text" or it.get("_img_seed"):
            continue
        b = it.get("bubble")
        if not b or b.get("image") or it.get("y1") is None:
            continue
        groups.setdefault((b["y0"], b["y1"]), [b, []])[1].append(it)
    todo = []
    for b, its in groups.values():
        top = min(it["y"] for it in its)
        bottom = max(it["y1"] for it in its)
        line_h = max(8.0, (bottom - top) / float(max(1, len(its))))
        gap = b["y1"] - bottom
        if gap > max(12.0, BUBBLE_RESCUE_GAP * line_h):
            todo.append((gap, b, its, bottom))
    if not todo:
        return items
    todo.sort(key=lambda t: -t[0])          # 空得最多的先补
    added = 0
    for gap, b, its, bottom in todo:
        if added >= BUBBLE_RESCUE_MAX:
            break
        x0 = max(0, min([b["x0"]] + [it["x"] for it in its]) - 4)
        x1 = min(img_rgb.shape[1],
                 max([b["x1"]] + [it.get("x1", it["x"]) for it in its]) + 4)
        y0 = max(0, int(bottom) + 2)
        y1 = min(img_rgb.shape[0], int(b["y1"]) + 4)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        crop = np.ascontiguousarray(img_rgb[y0:y1, x0:x1])
        try:
            res, _ = ocr_call(crop, rec_only=True)
        except Exception as exc:                       # pragma: no cover
            print("[wxreader] 气泡漏行补救失败：%r" % (exc,), flush=True)
            continue
        best = None
        for x in (res or []):
            txt, sc = (x[0], float(x[1])) if len(x) == 2 else (x[1], float(x[2]))
            txt = str(txt).strip()
            if not txt:
                continue
            if best is None or sc > best[1]:
                best = (txt, sc)
        if not best or best[1] < 0.5:
            continue
        txt, sc = best
        # 与「无气泡短碎片」同一口径：数字 / 字母噪声（图上印的、气泡边缘）不要
        if len(txt) <= 6 and not _has_cjk(txt) and re.search(r"[0-9A-Za-z]", txt):
            continue
        if any(str(it["text"]).strip() == txt for it in its):
            continue
        items.append({"kind": "text", "text": txt, "y": y0, "x": x0,
                      "y1": y1, "x1": x1, "is_self": bool(b.get("is_self")),
                      "bubble": b, "strong_self": False, "_rescued": True})
        added += 1
        print("[wxreader] 气泡漏行补救：补回 %r（气泡还空 %.0fpx）" % (txt, gap), flush=True)
    return items


def _fix_sender_items(items):
    """B：同帧昵称一致性校正。

    同一屏里同一个人会反复出现昵称。若某条昵称**置信度明显偏低**，而同帧存在
    一个**唯一**的「长度相同 + 字符位置过半一致」的高置信昵称，就采用后者。
    实测案例：``张二``(0.535) 与同帧 ``张三``(0.998) —— 真值是后者
    （已裁图肉眼确认过三处是同一个昵称）。

    ⚠️ 三重保守：① 只动 conf < NAME_FIX_MAX_CONF(0.80) 的；② 候选必须**唯一**；
    ③ 只改 ``kind == "sender"`` 的条目，**正文一个字都不碰**。
    已有的 ``_similar_name`` 沿用机制（阈值 0.6）保持不变，这里只多认下
    「相似度不够但明显是同一个」的那一批。
    """
    snd = [it for it in items if it.get("kind") == "sender"]
    if not snd:
        return items

    # ★ 2026-10-12（C）：名字库切分 —— 把「昵称：正文」切开（见 _split_known）。
    #   ⚠️ 必须在下面建信任表**之前**做：切开的昵称要参与一致性校正。
    for it in snd:
        _t0 = str(it.get("text") or "")
        _t1 = _split_known(_t0)
        if _t1 != _t0:
            it["text"] = _t1
            it["_split_from"] = _t0
            print("[wxreader] 昵称切分：%r -> %r" % (_t0, _t1), flush=True)

    if len(snd) < 2:
        return items
    trust = {}
    for it in snd:
        c = float(it.get("conf") or 0.0)
        if c < NAME_TRUST_CONF:
            continue
        t = str(it.get("text") or "")
        if t and (t not in trust or c > trust[t]):
            trust[t] = c
    if not trust:
        return items
    for it in snd:
        if float(it.get("conf") or 1.0) >= NAME_FIX_MAX_CONF:
            continue
        t = str(it.get("text") or "")
        if not t or t in trust:
            continue
        cands = [k for k in trust
                 if len(k) == len(t) and _name_ratio(t, k) >= NAME_FIX_SIM]
        if len(cands) == 1:
            it["text"] = cands[0]
            it["_fixed_from"] = t
            print("[wxreader] 昵称校正：%r -> %r" % (t, cands[0]), flush=True)
    return items


def _green_ratio(a, y0, y1, x0, x1):
    """气泡区域里微信绿(#95EC69)的占比。

    ⚠️ 左头像列不算：头像自己就可能是绿的（群里那只青蛙），否则别人发的消息
    也能凑出一截"绿"，把 is_self 和发送自检一起带偏。
    """
    x0 = max(int(x0), AVATAR_BAND_L[1] + 1)
    sub = _green_mask(a)[y0:y1 + 1, x0:x1 + 1]
    return float(sub.mean()) if sub.size else 0.0


def _pick_bubble(bubbles, y0, y1, x0, x1):
    best, best_score = None, 0
    h = y1 - y0 + 1
    for bub in bubbles:
        by0, by1 = bub["y0"], bub["y1"]
        # ⚠️ 2026-10-05：**图片**气泡的上下沿再放 IMG_EDGE_TOL 像素。
        #    图片上/下边缘常压着一圈黑色状态栏或白边，彩色通道抓不到它，
        #    OCR 框就落在块外一点点 —— 实测 /tmp/now.jpg 里图片顶部那条黑字
        #    「AIENUE」(y=552-566) 与彩色块 (y=564-776) 只重叠 3px，判不进气泡，
        #    于是掉进"居中黑字=时间戳"的兜底，变成一条假 time，还把昵称
        #    「张三」的 pending_sender 冲掉（那条 [图片] 从此没有发送者）。
        #    文字气泡不放这个余量：正文紧贴气泡时本来就重叠得很充分，
        #    放宽只会把上一条消息尾部的字吸进来。
        if bub.get("image"):
            by0 -= IMG_EDGE_TOL
            by1 += IMG_EDGE_TOL
        ov = max(0, min(by1, y1) - max(by0, y0) + 1)
        if ov <= 0 or ov * 2 < h:
            continue
        x_ov = max(0, min(bub["x1"], x1) - max(bub["x0"], x0) + 1)
        score = ov * 1000 + x_ov
        if score > best_score:
            best, best_score = bub, score
    return best


def _text_lum(a, y0, y1, x0, x1):
    """OCR 框内"文字像素"的平均亮度：黑字≈50，灰字≈137。"""
    sub = a[y0:y1 + 1, x0:x1 + 1].mean(axis=2)
    dark = sub < 150
    if not dark.any():
        return 255.0
    return float(sub[dark].mean())


_TITLE_DECOR = "「」『』【】《》\"'“”‘’ \t"

# ★ 2026-10-10（用户报「机器人一直抢键盘、把我打的字重置成目标群，他是不是回不去群了」）：
#   聊天页里会出现**带「微信」二字的系统提示**，最常见的是语音/消息翻译的那行
#   「由微信提供翻译支持」。老规则是「标题条里含『微信』的那一行 = 会话列表标题」，
#   于是这条提示被当成标题 → read_title 返回它 → match_title 判否 →
#     ① /open 明明切进了群，却报「匹配=False」（日志实证：`/open 目标群 -> 界面=ChattingUI
#        标题='由微信提供翻译支持' 匹配=False`，17:0x 连着好几次）；
#     ② parse_snapshot 的 chat_title 也变成它 → 机器人认为自己「不在目标群」→
#        每 15 秒抢一次物理屏去切群（用户正在打字，输入框被塞成「目标群」）→ 死循环。
#   判据收紧：只有**去掉「微信」二字后只剩数字/括号/空白**的行才算会话列表标题
#   （实测是「微信(4)」这种）；带别的汉字的行一律不是。
_WX_TITLE_NOISE_RE = re.compile(r"^[\s\d,，、()（）\[\]【】「」<>《》.:：·\-—_]*$")
# 「…由微信提供翻译支持」「微信团队」这类系统提示：聊天页取标题时先排掉它们
_WX_SYS_HINT_RE = re.compile(r"由微信|微信提供|翻译支持|微信团队")


def _is_wx_list_title(t):
    """这一行是不是**会话列表**的标题行（微信 / 微信(4) / 微信 4）？

    见上面 _WX_SYS_HINT_RE 旁的说明：判据必须收紧到"去掉微信二字后没有别的字"，
    否则聊天页里「由微信提供翻译支持」会被当成会话列表标题。
    """
    t = str(t or "").strip()
    if "微信" not in t:
        return False
    return bool(_WX_TITLE_NOISE_RE.match(t.replace("微信", "", 1)))


def _strip_title_noise(shown):
    """剥掉标题栏的装饰引号和「群人数」尾巴（跟 wxauto/platforms/android.py 保持一致）。

    虚拟屏只有 636 宽，长群名的中段会被系统吃掉，末尾还捎上人数、连左括号都截掉 ——
    实测截图上是「人...111)」。
    """
    s = (shown or "").strip().strip(_TITLE_DECOR).strip()
    # 人数尾巴实测四种形态：(111) / (111 / 111) / (111人) / 「示例...(111人)」
    s = re.sub(r"(?:[\(（]\s*\d+\s*(?:人|名)?\s*[\)）]?|"
               r"\d+\s*(?:人|名)?\s*[\)）])\s*$", "", s).strip()
    if re.search(r"\.{2,}", s):
        s = re.sub(r"\d+\s*$", "", s).strip()
    return s.strip(_TITLE_DECOR).strip()


def match_title(shown, target):
    """标题匹配：微信会把长群名截断成「示例群名...字」，省略号要当通配符。"""
    shown = _strip_title_noise((shown or "").replace("…", "..."))
    target = (target or "").strip()
    if not shown or not target:
        return False
    if shown in target or target in shown:
        return True
    # 2026-10-04：微信有时把群名压成「...111)」—— 只剩省略号+人数，一个中文
    # 都没有，这种标题无法辨识。但**不能一律放行**（原实现是 return True）：
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
        # 尾部剥干净才放行单字 head，但要求目标群名够长（防「人」这种单字误判）
        # ★ 2026-10-10：**这份逻辑在 wxauto/platforms/android.py::title_matches
        #   里还有一份，改一处必须改另一处**（两边判据不一样时会出现"机器人认为
        #   在目标群、桥接认为切错了群"这种鬼状态）。
        #   原来卡的是 `len(target) >= 4` —— **3 个字的群名一律判否**。用户把群备注
        #   改成「群备注名」之后，微信标题栏被虚拟屏的大字体压成 `缺...10`，桥接于是
        #   明明切进了群却报「/open 群备注名 -> 标题='缺...10' 匹配=False」，
        #   切群永远"失败"。放宽成 `len(target) >= 2`：单字 head 仍然必须正好是
        #   目标群名的开头（上面 startswith 把着），误判只剩"另一个会话的标题也被
        #   读成同一个字打头的省略号"这一种，概率极低。
        return len(head) >= 2 or len(target) >= 2
    return False


def _title_from_ocr(res):
    """从一整条标题带子的 OCR 结果里挑出「标题那一行」。

    规则（跟 `title_band` 一致，两处口径必须一样）：
      ① 含「微信」的那行 = 会话列表标题（不管它在 y=60 还是 y=200）；
      ② 否则取最上面一行 = 聊天页的群名（先排掉「由微信提供翻译支持」这类系统提示）。
    ★ 2026-10-12 从 `read_title` 里抽出来：标题引擎换成 v6 之后，读空要能**用同一套
      规则**拿 v4 的结果再解析一次 —— 逻辑只留一份才不会两边跑偏。
    """
    items = []
    for item in (res or []):
        box, txt = item[0], str(item[1]).strip()
        if not txt:
            continue
        ys = [int(p[1]) for p in box]
        xs = [int(p[0]) for p in box]
        items.append((sum(ys) / max(len(ys), 1), min(xs), txt))
    if not items:
        return ""
    for y0, _x, t in items:
        if _is_wx_list_title(t):
            row = sorted((x, s2) for y, x, s2 in items if abs(y - y0) < 30)
            return clean_title(" ".join(s2 for _, s2 in row))
    items.sort()
    cand = [it for it in items if not _WX_SYS_HINT_RE.search(it[2])] or items
    y0 = cand[0][0]
    row = sorted((x, s2) for y, x, s2 in cand if y - y0 < 30)
    return clean_title(" ".join(s2 for _, s2 in row))


def read_title(img):
    """只 OCR 顶部标题条。img 可以是路径 / ndarray / PIL.Image。

    2026-10-04 修：原来直接裁 CHAT_TOP(72px) 并把所有片段按 x 拼接。虚拟屏上的
    微信标题栏会下移到 y≈200，72px 里只剩搜索/加号图标，标题读成空 —— 机器人
    判不出「在会话列表」，切群永远失败（连续 30+ 次 on_wx_tab=False）。
    现在裁 TITLE_STRIP 高，再**按 y 分组取标题那一行**：
      ① 含「微信」的那行 = 会话列表标题（不管它在 y=60 还是 y=200）；
      ② 否则取最上面一行 = 聊天页的群名。
    """
    if isinstance(img, str):
        im = Image.open(img).convert("RGB")
    elif isinstance(img, Image.Image):
        im = img.convert("RGB")
    else:
        im = Image.fromarray(np.asarray(img, dtype=np.uint8))
    strip = np.array(im)[:TITLE_STRIP, :, :]
    res = None
    try:
        # ★ 2026-10-12（用户要求）：标题**主引擎也换成 v6**（跟正文同款）。
        #   实测 17 帧跨 5 种界面（会话列表 / 聊天页×13 / 「我」页 / 文件传输助手）：
        #   关键路径（会话列表读「微信」、聊天页读群名）与 v4 **完全打平**，
        #   在「我」页还更干净（v6 读成 `眠雾与惊芒`，v4 带着 `回品 0:0` 的尾巴）；
        #   当年坑 v5 的那种"读成 `厂 .` / `.10` / `D`"（读空或全错）**一次都没出现**。
        #   ⚠️ 但标题读错的后果是"以为不在目标群 → 白切群"（10-08 的老病根），
        #      所以下面加了一道**读空就用 v4 重读**的兜底。
        res, _ = ocr_call(strip)
    except Exception as exc:
        # 别静默吞：这个 except 曾经把"桥接进程里 OCR 一直失败"藏了很久
        print("[wxreader] read_title OCR 抛异常：%r" % (exc,), flush=True)
        return ""
    title = _title_from_ocr(res)
    if title:
        return title
    # ---- 兜底：主引擎（v6）一条都没读出来 → 用 v4 再读一遍 ----
    # ⚠️ v4 引擎是**懒加载**的：只要不走到这里，它就永远不会被创建、不占内存。
    try:
        res4, _ = ocr_call(strip, v4=True)
        title4 = _title_from_ocr(res4)
        if title4:
            print("[wxreader] 标题主引擎读空 → v4 兜底读到 %r" % title4, flush=True)
            return title4
    except Exception as exc:
        print("[wxreader] read_title v4 兜底也失败：%r" % (exc,), flush=True)
    return ""


def title_band(res, strip=None):
    """在**整张图**的 OCR 结果里定位「标题那一行」，返回它的 y 区间。

    规则和 read_title 一模一样（先找含「微信」的行 = 会话列表标题；否则取最上面
    一行 = 聊天页群名），区别只是复用现成的 OCR 结果，不再多跑一次。

    ★ 2026-10-05 为什么要有它：parse_snapshot 原来用 `y0 <= CHAT_TOP+16`（88px）
      判标题栏。微信自己的标题栏在虚拟屏上会被 freeform 窗口装饰挤到 y≈200，
      这时一个词都挑不出来 → chat_title 变成**空串** → 机器人判定「不在目标群」，
      于是：① 不再往实战台记消息（用户看到的「10 点以后没动静」）；② 每 60 秒
      「自动切回」一次（用的是桥接的 read_title，它能读到 → 匹配=True → 循环永不
      退出）。两条标题解析路径不一致，才把这个 bug 藏了一整天。
    """
    strip = TITLE_STRIP if strip is None else strip
    rows = []
    for item in (res or []):
        box, txt = item[0], str(item[1]).strip()
        if not txt:
            continue
        ys = [int(p[1]) for p in box]
        if min(ys) > strip:          # 标题只可能在顶部条里
            continue
        rows.append((sum(ys) / max(len(ys), 1), txt))
    if not rows:
        return None
    anchor = None
    for yc, txt in rows:
        if _is_wx_list_title(txt):   # ① 会话列表（必须整行就是「微信(N)」，见 _WX_SYS_HINT_RE）
            anchor = yc
            break
    if anchor is None:               # ② 聊天页：群名在最上面一行（先排掉系统提示行）
        cand = [r for r in rows if not _WX_SYS_HINT_RE.search(r[1])] or rows
        anchor = min(yc for yc, _ in cand)
    ys = [yc for yc, _ in rows if abs(yc - anchor) < 30]
    return (min(ys) - 6.0, max(ys) + 6.0)


def find_result_row(img, ytop=150, ybot=700):
    # 2026-10-04：原来是 (180, 430)。虚拟屏上微信搜索页布局偏低 —— 实测目标群
    # 落在 y≈450，刚好被 430 裁掉，导致「没找到搜索结果」→ 切群永远失败。
    # 放宽到 700；只取最上面那个绿色块（blocks[0]），不会误选下面的聊天记录。

    """在搜索结果页里找第一条「群聊」结果（群名是微信绿）。返回中心 (x, y)。"""
    if isinstance(img, str):
        a = np.array(Image.open(img).convert("RGB")).astype(np.int16)
    else:
        a = np.asarray(img, dtype=np.int16)
    if a.ndim != 3:
        return None
    m = _green_mask(a)
    m[:ytop, :] = False
    m[ybot:, :] = False
    blocks = _row_blocks(m, ytop, min(ybot, a.shape[0]), min_h=14, gap=8, min_px=5)
    if not blocks:
        return None
    y0, y1 = blocks[0]
    cols = np.where(m[y0:y1 + 1].any(axis=0))[0]
    if len(cols) == 0:
        return None
    return int((cols[0] + cols[-1]) / 2), int((y0 + y1) / 2)


def clean_title(raw):
    """标题清洗：微信标题栏里混着返回箭头、未读数、书名号、群人数。

    实测 OCR 会把返回箭头读成 '<'，把未读数读成 '5'，所以先剥装饰字符再剥数字。
    """
    t = (raw or "").strip().replace("…", "...")
    t = TITLE_TAIL_RE.sub("", t)                       # 尾巴的 (112)
    t = re.sub(r"^[^\w\u4e00-\u9fff]+", "", t)         # 前导装饰符号（< 、[ 等）
    t = TITLE_HEAD_RE.sub("", t)                       # 前导未读数字
    return t.strip("「」《》〈〉【】[]()（）<>‹›【】 \t")


def _has_cjk(t) -> bool:
    """这段文字里有没有中文字符（判断"是不是有意义的话"最省事的标尺）。"""
    return bool(re.search(r"[\u4e00-\u9fff]", str(t or "")))


# ---------------------------------------------------------------- 图片裁剪精修
# ★ 2026-10-10（加「识图」功能时实测出来的）：detect_bubbles 给出的**图片气泡框
#   常常只包住图片中间的一条**，直接拿它裁剪喂给视觉模型 = 只给人家看一条横带。
#
#   实测（/tmp/wxbridge-snap.jpg 那张兔子抱玉米的表情包，真实边界 y=150~375）：
#   检测结果 y=212~349 —— 上面丢 62px、下面丢 26px、右边丢 50px。
#   原因：图片气泡靠**白色/彩色掩码**检测，而照片/表情包的上下边缘多是模糊、
#   渐变、低饱和的（虚化背景、白墙、天空），掩码抓不到那一圈。
#
#   修法：从检测到的框出发**只向外扩**（绝不收缩），用"这一行有多少像素明显
#   不同于聊天背景色"来判"这行还是不是图片"。实测判据很干净：
#     · 图片行：>80 的像素占 57%~100%
#     · 文字行：最高 33%（黑字只占一行里的一小段），多数 <5%
#     · 空白行：0%
#   所以 45% 这条线两边都留了余量。
IMG_REFINE_DIFF = 80      # 与聊天背景的通道差之和超过它 => 这个像素算"图片内容"
IMG_REFINE_FRAC = 0.45    # 一行里"图片内容"占比超过它 => 这行算图片
IMG_REFINE_STOP = 3       # 连续这么多行不像图片就停止外扩（容过渡行/模糊边）
IMG_REFINE_MAX = 220      # 单侧最多外扩多少像素（防把整屏框进来）
IMG_REFINE_PAD_X = 16     # 左右各留一点边。★ 横向**不做**行剖面精修：照片中间
#                           常有低饱和的横带（实测那张表情包的绿茎 x=404~436），
#                           逐列扫描会被它骗停；横向多带一点背景无妨。


def _bg_color(a):
    """聊天背景色：左右两条竖带的中位数。

    虚拟屏微信的聊天背景是均匀的浅灰（实测 RGB(237,237,237)），左右两条竖带
    以背景为主，中位数能稳住；比"整图取众数"便宜，也不怕大图占满屏幕。
    """
    h, w = a.shape[:2]
    band = max(8, min(60, w // 8))
    px = np.concatenate([a[:, :band].reshape(-1, 3),
                         a[:, w - band:].reshape(-1, 3)], 0)
    return np.median(px, axis=0)


def refine_image_box(a, box, bg=None, top=None, bottom=None, is_self=False):
    """把图片气泡框向外精修到图片的**真实边界**（只扩不收）。

    ``box`` = (x0, y0, x1, y1)，``a`` 是整帧的 ndarray（int16，HxWx3）。
    ``top`` / ``bottom`` 是允许扩展到的上下界（默认聊天区）。

    返回 (x0, y0, x1, y1)，已经 clamp 到图内。识图那条路（裁剪喂视觉模型）
    必须过这个函数，否则模型只看得到图片中间的一条 —— 实测一张 370x225 的照片
    检测框只有 199x72，上下左右都缺一块。

    两步：
      ① **纵向**用行剖面外扩（"这行有多少像素明显不同于聊天背景"）；
      ② **横向**在①确认的图片行里取内容像素的左右极值。
    两步都"只扩不收"：宁可多带一圈背景，也不能把图片内容切掉。
    """
    h, w = a.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in box)
    x0 = max(0, min(x0, w - 1))
    x1 = max(x0 + 1, min(x1, w))
    y0 = max(0, min(y0, h - 1))
    y1 = max(y0 + 1, min(y1, h))

    if top is None:
        top = CHAT_TOP
    if bottom is None:
        bottom = min(h, CHAT_BOTTOM)
    top = max(0, int(top))
    bottom = min(h, max(top + 1, int(bottom)))

    if bg is None:
        bg = _bg_color(a)
    # 只在框的 x 范围里看：范围外的内容（头像列、别的气泡）不该影响判断
    frac = (np.abs(a[:, x0:x1] - bg).sum(2) > IMG_REFINE_DIFF).mean(axis=1)

    ny0, miss = y0, 0
    y = y0 - 1
    limit = max(top, y0 - IMG_REFINE_MAX)
    while y >= limit:
        if frac[y] >= IMG_REFINE_FRAC:
            ny0, miss = y, 0
        else:
            miss += 1
            if miss >= IMG_REFINE_STOP:
                break
        y -= 1

    ny1, miss = y1, 0
    y = y1
    limit = min(bottom, y1 + IMG_REFINE_MAX)
    while y < limit:
        if frac[y] >= IMG_REFINE_FRAC:
            ny1, miss = y + 1, 0
        else:
            miss += 1
            if miss >= IMG_REFINE_STOP:
                break
        y += 1

    # ---- 横向：在 [ny0, ny1) 里取"图片内容"像素的左右极值 ----
    # 为什么纵向那套（行剖面）不能直接转 90° 用：照片里常有低饱和的**竖带**
    # （实测那张表情包中间那根绿茎 x=404~436，逐列占比只有 36%~85%），逐列扫描
    # 会被它骗停。反过来取极值就稳 —— 低饱和区只是"这列像素少"，不影响极值。
    # 实测同一张图：检测框 x=273~472，真实边界 x=200~570，极值法一次还原。
    # ⚠️ 头像列必须排除：别人的图片左边紧挨着彩色头像，会把左极值拉到 x≈52。
    lo = AVATAR_BAND_L[1] + 1
    hi = w if is_self else min(w, RIGHT_AVATAR_X)
    nx0, nx1 = max(0, x0 - IMG_REFINE_PAD_X), min(w, x1 + IMG_REFINE_PAD_X)
    if hi > lo and ny1 > ny0:
        diff = np.abs(a[ny0:ny1, lo:hi] - bg).sum(2)
        cols = np.where((diff > IMG_REFINE_DIFF).any(axis=0))[0]
        if len(cols):
            nx0 = min(nx0, max(0, lo + int(cols[0]) - IMG_REFINE_PAD_X))
            nx1 = max(nx1, min(w, lo + int(cols[-1]) + 1 + IMG_REFINE_PAD_X))
        # cols 为空 = 整块都是浅色（白底截图/白底表情包）→ 保留原框 + 小 padding
    return (nx0, ny0, nx1, ny1)


def _keep_img_text(t) -> bool:
    """图片上的 OCR 文字，值不值得追加进 ``[图片]``。

    ★ 2026-10-05 用户报：「读屏幕，他把图片读成乱七八糟的文字」—— 照片/表情包
    上常有数字、英文字母、水印，RapidOCR 会读成「69」「252」「2E2」「3214620」
    这类碎片，追加进消息正文后就会被关键词规则当成"群友说的话"（那条 `["6"]`
    规则就是这么被误触的，用户已经点名删了）。
    这里只留**含中文**的片段：图上真有用的字（内功图的「气海/根骨」）照样留下，
    纯数字 / 纯字母的垃圾丢掉。
    """
    return _has_cjk(t)


def parse_snapshot(img_path):
    """把一张聊天页截图解析成 {chat_title, messages}。"""
    img = Image.open(img_path).convert("RGB")
    a = np.array(img).astype(np.int16)
    h, w = a.shape[:2]
    # ★ 2026-10-10：先按这一帧的实际宽度把像素常量缩放到当前档位（half/full 都能跑）
    apply_resolution(w, h)   # h 用于贴底锚点（CHAT_BOTTOM）
    bubbles = detect_bubbles(a)

    res = None
    try:
        res, _ = ocr_call(np.array(img))
    except Exception as exc:      # pragma: no cover
        print("[wxreader] OCR 失败：%r" % (exc,), flush=True)

    # ★ 2026-10-12（A）：低置信条目复核 —— 拿原框跳过检测重跑识别（见 _retry_low_conf）
    res, _low_fixed = _retry_low_conf(img, res)

    # 标题行：和 read_title 用同一套规则（见 title_band 的注释）
    tb = title_band(res)

    title_parts, items = [], []
    for item in (res or []):
        box, txt = item[0], str(item[1]).strip()
        if not txt:
            continue
        ys = [int(p[1]) for p in box]
        xs = [int(p[0]) for p in box]
        y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)

        # 标题栏判定。原来是 y1 <= CHAT_TOP(72)，但标题框的下沿实际能压到 75 ——
        # 差这 3px，整条标题就被当成消息丢掉，chat_title 变成空串，机器人于是
        # 一直认为"当前会话不是目标群"无限待命。改成看框的**上沿**并留 16px 容差。
        # ★ 2026-10-05：再加一层 title_band —— 标题栏被窗口装饰挤到 y≈200 时，
        #   88px 这条线一个词都盖不住，chat_title 直接空掉（实战台从此不更新）。
        yc = (y0 + y1) / 2.0
        if y0 <= CHAT_TOP + 16 or (tb is not None and tb[0] <= yc <= tb[1]):
            title_parts.append((x0, txt))
            continue
        if y0 >= CHAT_BOTTOM:
            continue
        # 底部「群工具 / 小程序」快捷栏（见 detect_bubbles 里同一段说明）：
        # 整块落在这个窗口里的 OCR 不是消息 —— 小程序名可以很长，不拦住的话
        # 它会绕过"长得像时间戳"那条兜底，直接变成某个人说的话。
        # ★ 2026-10-15（用户报「"我还真看过这个短剧"都没识别出来」）：
        #   **这里漏了高度条件** —— `detect_bubbles` 那边一直有
        #   `>= BOTTOM_BAR_MIN_H`，这一条没有。而窗口是按**宽度**等比缩放的
        #   （full 档 2156~2395，见 apply_resolution），那正好压住屏幕下方
        #   一整条普通消息带：实测张三的「我还真看过这个短剧」在 y=2202、
        #   高约 40px，**整页 OCR 明明读到了**，却在这里被当成快捷栏丢掉
        #   （同屏的「李四：」y=2331 一起没了）。结果就是"群友正常说话它没看见"。
        #   修：与气泡检测同口径 —— 够高（≥ 快捷栏高度）才当快捷栏。
        if (BOTTOM_BAR_Y0 <= y0 and y1 <= BOTTOM_BAR_Y1
                and (y1 - y0 + 1) >= BOTTOM_BAR_MIN_H):
            continue
        # 头像图上印的字：整块落在左右头像列里，直接丢弃
        # 头像图上印的字：整块落在左右头像列里，直接丢弃。
        # ⚠️ 左边用 AVATAR_BAND_L[1](176) 而不是 LEFT_AVATAR_X(110)：头像列实测
        #    到 x≈176，用 110 会漏掉头像右半边印的字（「不讲武德」被读成昵称，
        #    把它下面那几条消息的说话人都带偏）。
        # ★ 2026-10-12（用户报「不知道是谁在说话」）—— **这条把昵称整批杀了**：
        #   头像列右边界 `AVATAR_BAND_L[1]` 是**按图宽等比缩放**的（half 176 →
        #   full 350），而 full 档的头像**没有跟着变宽**（实测头像右边界 ≈150）。
        #   昵称是灰字窄框（实测「张三」x0=203 / x1≈283），整块落在 350 以内 →
        #   被当成"头像上印的字"丢掉。后果：整屏消息 sender 全空、最近 200 条
        #   里 48% 没昵称（`chat_log.json` 里 sender="" 就是这么来的）。
        #   修法：**头像列右边界不参与等比缩放**，按实际图宽给（full ≈180）。
        #   ⚠️ 只改这一条过滤，**不动全局 `AVATAR_BAND_L`** —— 它同时当着气泡
        #      左边界用（detect_bubbles / _pick_bubble 好几处），动它会把整个
        #      气泡检测带偏。要动全局得单独做回归。
        _av_right = 180 if w >= 1000 else AVATAR_BAND_L[1]
        if x1 <= _av_right or x0 >= RIGHT_AVATAR_X:
            continue

        lum = _text_lum(a, y0, y1, x0, x1)
        bub = _pick_bubble(bubbles, y0, y1, x0, x1)
        t = txt.strip()

        # 系统提示（「XX撤回了一条消息」「8条新消息」…）不是聊天内容 —— 它常常
        # 压在未读分隔线上、被白色/彩色块包住。**必须在气泡分支之前拦掉**：一旦
        # 落进图片气泡，就会被当成"图上的字"追加进 [图片]，机器人看到的是
        # 「[图片] 也 8条新消息」，图片规则和关键词规则都可能被带偏。
        # ★ 2026-10-06：拍一拍单独成类（用户点名要"被拍脑袋 → 发挠人的表情包"）。
        #   ⚠️ 必须放在 SYS_HINT 判断**之前**：拍一拍是居中的系统灰字，一旦掉进下面
        #   `bub is None` 那支，会被 lum 判成昵称、或兜底判成正文，两种都是错的。
        if "拍了拍" in t:
            items.append({"kind": "pat", "text": t, "y": y0, "x": x0,
                          "is_self": False})
            continue

        if any(hint in t for hint in SYS_HINT):
            items.append({"kind": "system", "text": t, "y": y0, "x": x0})
            continue

        if bub is None:
            # ★ 2026-10-06 用户拍板「治，漏了没事」：**没有气泡包着**的短碎片，
            #   一个中文字都没有、又不是时间戳 → 判为读图噪声，不入账。
            #   典型来源：照片/表情包上的数字被 OCR 读成 "252" / "2E2" / "69"，
            #   或者图上的字母 "nedtea" / "XF" —— 它们掉在图片气泡**外面**，
            #   所以 _keep_img_text() 那道"图内文字"过滤拦不住，只能在这儿丢。
            #   代价（用户明确接受）：群里真有人发 "OK" / "lol" / "6" 这种纯英文
            #   或纯数字的短消息，也会一起被丢 —— 他说"漏了没事"。
            #   ⚠️ 只拦这一支：**灰字（lum 高）那支是昵称**，别动 —— 群里真有
            #   英文昵称（台账里见过 "J/U" / "ju" / "lf"），拦了会把 sender 丢掉。
            # ⚠️ 2026-10-06 修：第一版只判"不含中文 + ≤6 字"，结果把真实的
            #   「？」消息也一起丢了 —— 实测群里三个人连着发「？」，机器人一条都没
            #   读到（用户会以为它瞎了）。真正的噪声特征是**夹着数字/字母**：
            #   252 / 2E2 / nedtea / XF / OK，而「？」「！」「。。。」这类纯符号
            #   是群里真会发的消息，必须留下。
            if (len(t.strip()) <= 6 and not _has_cjk(t)
                    and re.search(r"[0-9A-Za-z]", t) and not TIME_RE.match(t)):
                continue
            if TIME_RE.match(t):
                items.append({"kind": "time", "text": t, "y": y0, "x": x0})
            elif any(hint in t for hint in SYS_HINT):
                items.append({"kind": "system", "text": t, "y": y0, "x": x0})
            elif lum > NICK_LUM:
                items.append({"kind": "sender", "text": t, "y": y0, "x": x0,
                              "conf": float(item[2])})
            else:
                # 没气泡包着的黑字：居中就是时间戳，否则算左侧兜底正文
                center = (x0 + x1) / 2.0
                if abs(center - w / 2.0) < 70 and len(t) <= 24:
                    items.append({"kind": "time", "text": t, "y": y0, "x": x0})
                else:
                    items.append({"kind": "text", "text": t, "y": y0, "x": x0,
                                  "y1": y1, "x1": x1,
                                  "is_self": center > w * 0.62, "bubble": None})
            continue

        is_self = bool(bub["is_self"])
        # 「绿气泡 = 自己发的」只对**文字气泡**成立 —— 图片/表情本来就是各种颜色，
        # 一张绿色表情包会被这条判成自己发的（2026-10-04 实测 colorful=0.97 的图
        # 里绿色占了三成）。图片的归属交给昵称和左边界去判。
        # strong_self：绿气泡（#95EC69）是"这条是我发的"的**最强证据**，别人的
        # 文字气泡一律是白的。组装时要靠它顶住"有昵称 ⇒ 不是自己"那条推断 ——
        # 昵称一旦识别错位，机器人自己刚发的绿气泡就会被判成别人（实测 chat_log
        # 里出现过 `他 / 人多点 / ？喵`，而「？喵」是机器人发的），发送自检也会
        # 跟着失败：「屏幕上最后一条是 '[图片]'，期望 '老吴！'」。
        strong_self = (not bub.get("image")
                       and _green_ratio(a, y0, y1, x0, x1) > GREEN_RATIO_SELF)
        if strong_self:
            is_self = True
        # ⚠️ 白块常把上面的「昵称行」和气泡连成一块（见 _looks_image 的注释）——
        #    昵称是**灰字**，lum 明显高于正文的黑字（黑≈50 / 灰≈137，阈值 NICK_LUM）。
        #    这里再兜一道，别让昵称变成正文 —— 否则机器人会以为「张三」是
        #    他说的话，后面消息的 sender 也全挂错。
        # ⚠️ 图片/表情上的字也算「lum 高」，不能一律当昵称 —— 实测表情包上的
        #    「倒…」被当成昵称，这条消息的说话人就成了「倒」。反过来，头像列里
        #    的残留白像素会把「昵称行」连进图片气泡的范围，不加区分的话昵称就
        #    变成"图上的字"（实测「[图片] 张三」）。判据：群昵称永远紧挨着
        #    头像列（x0≈205），而图片上的字在图片内部 —— 灰字 + 紧贴头像列即昵称，
        #    哪怕它落在图片气泡的范围里。
        is_nick = (not is_self and lum > NICK_LUM and (
            not bub.get("image") or x0 <= AVATAR_BAND_L[1] + 44))
        # 文字探出气泡上边缘 = 上一条消息被屏幕顶部裁断的残影（实测 OCR 把那半行
        # 读成 "VXJUUI" 之类乱码，还会和下面这条粘成一句话，面板上完全对不上）。
        # ⚠️ 但**昵称本来就在气泡上方几像素**，得先让昵称过 —— 否则整条消息的
        # sender 会凭空丢掉（实测「精子小鲜心饮」的昵称 y=146、气泡 y=151，
        # 只差 5px 就被当成残影，那条 [图片] 在面板上就没有发送者了）。
        if y0 < bub["y0"] - 2 and not is_nick:
            continue
        if is_nick:
            items.append({"kind": "sender", "text": t, "y": y0, "x": x0,
                          "conf": float(item[2])})
            continue
        items.append({"kind": "text", "text": t, "y": y0, "x": x0,
                      "y1": y1, "x1": x1,
                      "is_self": is_self, "bubble": bub, "lum": lum,
                      "strong_self": strong_self})

    # 图片/表情气泡**不靠 OCR 触发**：老代码只在"某个 OCR 词条落在这个气泡里"
    # 时才生成 [图片]，而用户发的照片上常常一个字都没有 —— 于是整条消息消失，
    # 紧跟着的底部快捷栏反倒成了说话人（用户报的「阿兰发图片被识别成专属抽奖」）。
    # 这里给每个图片气泡补一个种子词条，再和 OCR 词条一起按 y 排序；
    # 如果图上真有字（内功截图那种），OCR 词条会随后把文字追加进同一条。
    items += [{"kind": "text", "text": "[图片]", "y": bub["y0"], "x": bub["x0"],
               "is_self": bool(bub["is_self"]), "bubble": bub, "_img_seed": True}
              for bub in bubbles if bub.get("image")]
    # ★ 2026-10-12：气泡里只读到上半部分时，把下半部分单独喂识别补回来
    #   （用户报「看看腿只被识别成了看看」，见 _rescue_bubble_tail）
    items = _rescue_bubble_tail(np.array(img), items, bubbles)
    items.sort(key=lambda m: (m["y"], m["x"]))

    # ★ 2026-10-12（B）：同帧昵称一致性校正（见 _fix_sender_items）
    items = _fix_sender_items(items)

    # 组装：昵称挂到后面第一条消息；图片气泡补 [图片]；同一气泡的多行合并
    messages, pending_sender, seen_img = [], "", set()
    last_sender = ""
    # 气泡 y0 -> 被"上面挂着昵称"推翻后的 is_self。
    # ⚠️ 群聊里**自己发的消息不显示昵称**，所以「有昵称 ⇒ 不是自己发的」这条判据
    #    要能推翻 detect_bubbles 的位置判据，而且**该气泡的每一行都得记住**，
    #    否则两行以上的气泡会被拆成两条消息。
    #    2026-10-04 实测：张三的白底表情包「没眼看」，白色像素从 x=193 起
    #    （> AVATAR_GAP_X=140），被判成"自己发的"，面板显示成「机器人：没眼看」，
    #    用户以为机器人在群里乱说话。
    self_override = {}
    img_msg = {}          # 气泡 key -> 那条「[图片]」消息，用来往里追加 OCR 文字
    for it in items:
        if it["kind"] == "sender":
            # 同一个人连着发言时 OCR 会把昵称读出各种花样（张三儿 / 张三 /
            # 麻子素问…）。长得像上一条昵称的就沿用上一条，别在面板上变成好几个人。
            name = it["text"]
            if last_sender and _similar_name(name, last_sender):
                name = last_sender
            else:
                last_sender = name
            pending_sender = name
            # 昵称行被连进图片气泡时，昵称词条排在图片种子**之后** —— 不补挂的话
            # 面板上这张图没有发送者（显示成「?」，就是用户看到的那种）。
            prev = messages[-1] if messages else None
            if (prev is not None and prev.get("type") == "image"
                    and not prev.get("sender")
                    and int(prev.get("_by0") or 10 ** 6) <= it["y"] <= int(prev.get("_by1") or -1)):
                prev["sender"] = name
                pending_sender = ""
            continue
        if it["kind"] in ("time", "system", "pat"):
            # pat（拍一拍）单独成一条：type 保持 "pat"，不挂发送者 —— 机器人侧只读
            # text/is_self/sender，靠 rules.yaml 里的关键词（要带上自己的昵称）筛"拍我"。
            messages.append({"text": it["text"], "is_self": False,
                             "type": it["kind"], "sender": ""})
            pending_sender = ""
            continue
        bub = it.get("bubble")
        bkey = None if bub is None else bub["y0"]
        cur_self = bool(it.get("is_self"))
        if bkey is not None and bkey in self_override:
            cur_self = self_override[bkey]
        elif pending_sender and not it.get("strong_self"):
            # 「有昵称 ⇒ 不是自己发的」要能被绿气泡顶掉：昵称是灰字、可能识别错位
            # （挂到上一张图上、被 OCR 读残），而气泡颜色不会骗人。
            cur_self = False
            if bkey is not None:
                self_override[bkey] = False
        if bub is not None and bub["image"]:
            key = (bub["y0"], bub["y1"])
            # 图片气泡里的字不能丢：OCR 已经把图上的文字读出来了（比如内功截图的
            # 「气海 / 根骨」），原来是 append 一个「[图片]」就把它们全扔了 ——
            # 结果是 rules.yaml 里的「内功图」规则**永远命中不了**，别人发内功图
            # 机器人只看到「[图片]」就什么都不做。
            # 现在攒成「[图片] 气海 根骨」这种形式，匹配层照样能扫到。
            if key not in seen_img:
                seen_img.add(key)
                prev = messages[-1] if messages else None
                # 同一张照片可能被彩色通道切成上下两半（中间压着一片低饱和区域，
                # 比如照片里的白墙）—— 合并成一条 [图片]，别在面板上显示成两条。
                # 只比 is_self 和距离：昵称只挂在第一块上，第二块的 pending_sender
                # 已经是空的，拿它比较永远合不上（实测第一版就是这么错的）。
                gap = (bub["y0"] - int(prev.get("_by1") or -10 ** 6)
                       if prev is not None else 10 ** 6)
                overl = 0
                if prev is not None:
                    overl = (min(bub["x1"], int(prev.get("_bx1") or -1))
                             - max(bub["x0"], int(prev.get("_bx0") or 10 ** 6)))
                if (prev is not None and prev.get("type") == "image"
                        and prev.get("is_self") == cur_self
                        and gap <= IMG_MERGE_GAP and overl > 0):
                    m = prev
                    m["_by1"] = bub["y1"]
                    m["_bx0"] = min(int(m.get("_bx0") or bub["x0"]), bub["x0"])
                    m["_bx1"] = max(int(m.get("_bx1") or bub["x1"]), bub["x1"])
                else:
                    m = {"text": "[图片]", "is_self": cur_self,
                         "type": "image", "sender": pending_sender,
                         "_by0": bub["y0"], "_by1": bub["y1"],
                         "_bx0": bub["x0"], "_bx1": bub["x1"]}
                    messages.append(m)
                img_msg[key] = m
            else:
                m = img_msg.get(key)
            # ⚠️ 这里原来用的是 **t** —— 那是上面 OCR 循环遗留的变量（整张图最后
            #    一个词条），不是当前词条。于是每条 [图片] 都会重复追加同一句话，
            #    用户看到的就是「[图片] MB MB MB MB MB」「[图片] 用小铁了没」。
            if m is not None and it.get("text") and not it.get("_img_seed"):
                if _keep_img_text(it["text"]):
                    m["text"] = (m["text"] + " " + it["text"]).strip()
            pending_sender = ""
            continue
        if messages and it.get("is_self") is not None \
                and messages[-1]["type"] == "text" \
                and messages[-1]["is_self"] == cur_self \
                and bub is not None and messages[-1].get("_by0") == bub["y0"]:
            messages[-1]["text"] += it["text"]
            continue
        messages.append({"text": it["text"], "is_self": cur_self,
                         "type": "text",
                         # 自己发的消息在群聊里**不显示昵称**，前面那个 pending 昵称
                         # 不属于它（实测会出现「我 / 0:03 / 别6喵」这种脏组合）。
                         "sender": "" if cur_self else pending_sender,
                         "_by0": None if bub is None else bub["y0"]})
        pending_sender = ""

    for m in messages:
        # ★ 2026-10-10（识图功能）：**图片消息要把气泡坐标留下来** —— 上层要拿它
        #   去裁剪这张图喂视觉模型。以前这四种坐标一律 pop 掉，图片消息出了读屏层
        #   就只剩一个 `[图片]` 字符串，机器人根本不知道图在屏幕的哪一块。
        #   用合并后的 _bx*/_by*（同一张图被彩色通道切成上下两半时，那里是并集）。
        if m.get("type") == "image" and m.get("_by0") is not None:
            _bx0 = int(m.get("_bx0") or 0)
            _by0 = int(m.get("_by0") or 0)
            _bx1 = int(m.get("_bx1") or (_bx0 + 1))
            _by1 = int(m.get("_by1") or (_by0 + 1))
            m["bbox"] = [_bx0, _by0, _bx1, _by1]
        m.pop("_by0", None)
        m.pop("_by1", None)
        m.pop("_bx0", None)
        m.pop("_bx1", None)

    title_parts.sort()
    raw_title = " ".join(t for _, t in title_parts)
    final_title = clean_title(raw_title)
    # ★ 2026-10-09：标题现在**一律以 read_title（v4 引擎、只裁标题条）为准**，
    #   反过来把整图 OCR 的结果当兜底。为什么把这个顺序倒过来：
    #   整图这一路现在走 v5（正文明显更准），但标题那行是「「...10)」这种只剩
    #   省略号+人数的极短噪声，v5 反而更差 —— 40 帧实测 v4 的 title_matches 全过，
    #   v5 有 3 帧读成 `厂 .` / `.10` / `D`（非空，所以原来的"空了才兜底"根本
    #   不会触发），判「不在目标群」→ 连续几轮就去白切群，正是 2026-10-08 那场
    #   "反复切群失败"的老病根。正文用 v5、标题用 v4，各取所长。
    try:
        fb = read_title(img)
        if fb:
            raw_title = fb
            # ⚠️ read_title 内部已经 clean_title 过一遍了，**绝不能再洗第二次**：
            #    clean_title 会把纯 '...' 当「前导装饰符号」整个删掉 → 又变空串。
            final_title = fb
            # ⚠️ OCR 读标题栏的点号个数不稳定（实测 '...' / '..' / '.' 都出现过），
            #    而 title_matches 的「无中文」分支要求**至少两个点**才算「被截断」。
            #    纯点号标题只可能是「长群名被截断」，统一补齐成 '...' 再交给匹配
            #    （实测 21:39 那轮读到单个 '.' → 判「不在目标群」→ 白待命一轮）。
            if re.fullmatch(r"\.+", final_title):
                final_title = "..."
        elif not final_title:
            # 两条路都没读到 —— 保持空串，上层会判"不在目标群"（但那是真读不到）
            pass
    except Exception as exc:
        print("[wxreader] read_title 失败（回退整图 OCR 的标题）：%r" % (exc,), flush=True)
    return {
        "ok": True,
        "chat_title": final_title,
        "chat_title_raw": raw_title,
        "messages": messages,
        "bubbles": bubbles,
        "width": w,
        "height": h,
    }


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/base.jpg"
    out = parse_snapshot(path)
    print("标题: %r（raw=%r）气泡数=%d" %
          (out["chat_title"], out["chat_title_raw"], len(out["bubbles"])))
    for m in out["messages"]:
        print("  [%s|%-6s]%s %s" % ("我" if m["is_self"] else "他", m["type"],
                                    ("<" + m["sender"] + ">") if m["sender"] else "",
                                    m["text"]))
