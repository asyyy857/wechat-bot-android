#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxbridge.py —— 微信手机端桥接服务（Route B：微信跑在虚拟屏）

配合 wxauto/platforms/android.py 使用，提供三个端点：
    GET  /health    -> {"ok": true}
    GET  /snapshot  -> {"ok": true, "chat_title": ..., "messages": [...]}
    POST /send      -> body {"text": "..."}，真的发出去 -> {"ok": true}

发送通道（已端到端验证）：
    1. 点输入框聚焦
    2. 默认输入法切到 dsh-clip 的「无键盘输入法」DshIme（不弹物理屏键盘）
    3. 广播 clear + commitText(text)：中文/任意 Unicode 原样进输入框
    4. 用「输入框区域灰度标准差」校验文字进去了（空≈0.002，有字≈0.27）
    5. 点发送，再校验输入框已清空
    6. 把输入法还给用户
每步可重试。读取走截图 + tesseract（best effort）。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



def _pixels(im):
    """取整张图的像素序列（L 图给 int，RGB 图给 (r,g,b)）。

    ★ 2026-10-12：Pillow 新版把 ``Image.getdata()`` 标成弃用、换成
    ``get_flattened_data()``（先上弃用警告，再往后会删）。本机装的 10.2.0 还没有
    新名字，所以**新名字优先、旧名字兜底**，两个版本都能跑。
    ⚠️ 这个函数的调用方里有一处外面包着 ``except Exception: return False``
    （``_group_toolbar_green``）—— 将来 getdata 真被删了，不是报错而是"永远检测不到"，
    静默失效；所以这里不能偷懒留着旧名字。
    """
    f = getattr(im, "get_flattened_data", None)
    return f() if callable(f) else im.getdata()


class _TsOut:
    """★ 2026-10-12：给桥接日志每一行行首打 ``HH:MM:SS``。

    起因：/tmp/wxbridge.log 一直没有时间戳，要对时间线只能拿机器人日志（有时间）
    反推 —— 20:03 那次停机、19:50 那几次发送失败都是这样靠猜。
    做法：包一层 stdout/stderr，**按行**补前缀；整个文件里几百处 print 一个都不用改。
    ⚠️ 要点：
      · 线程安全（桥接是 ThreadingHTTPServer，多线程同时 print）→ 锁 + 只在整行边界补；
      · 只在"新行开头"补，不在行中间补（一次 print 可能拆成"正文"和"\\n"两次 write）；
      · 任何异常都退回原样输出 —— 绝不能因为日志包装把桥接搞挂；
      · ``WXBRIDGE_LOG_TS=0`` 可关（回到原样输出）。
    ``[timing]`` 这类子串过滤（measure_send.py）不受影响。
    """

    def __init__(self, raw):
        self._raw = raw
        self._lock = threading.Lock()
        self._bol = True            # 当前是不是在行首

    def write(self, text):
        try:
            if not isinstance(text, str) or not text:
                return self._raw.write(text)
            with self._lock:
                out = []
                for part in text.splitlines(True):
                    if self._bol:
                        out.append(time.strftime("%H:%M:%S "))
                    out.append(part)
                    self._bol = part.endswith("\n")
                return self._raw.write("".join(out))
        except Exception:
            try:
                return self._raw.write(text)
            except Exception:
                return 0

    def flush(self):
        try:
            return self._raw.flush()
        except Exception:
            return None

    def __getattr__(self, name):            # fileno / isatty / encoding ……原样透传
        return getattr(self._raw, name)


if (__name__ == "__main__" and os.environ.get("WXBRIDGE_LOG_TS", "1") != "0"
        and not isinstance(sys.stdout, _TsOut)):   # 被测试 import 时不包，免得测试输出被加前缀
    sys.stdout = _TsOut(sys.stdout)
    sys.stderr = _TsOut(sys.stderr)

ROOT = os.environ.get("DSH_ROOT", "/sdcard/dsh")
SH = os.path.join(ROOT, "tools", "shd.sh")
VSCREEN = os.path.join(ROOT, "tools", "vscreen")

# 结构化读取（截图 + RapidOCR + 像素气泡分析），见 read/wxreader.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "read"))
try:
    import wxreader
except Exception as _exc:      # pragma: no cover
    wxreader = None
    print("[wxbridge] wxreader 载入失败：%r" % (_exc,), flush=True)
SLOT = int(os.environ.get("WX_SLOT", "0"))
VSD_PORT = int(os.environ.get("VSD_PORT", "18250"))   # 守护端口（18240 是被劫持的坏端口）
PORT = int(os.environ.get("WX_BRIDGE_PORT", "8765"))

# 虚拟屏微信聊天页固定 UI 坐标（636x1400 竖屏）
# ⚠️ 2026-10-04 实测：虚拟屏重建后（屏号 45 → 47）底部输入栏整体上移，
# 写死的 (250,1350) 点在了输入框下面的空白里 → 三次输入全「还是空的」，发不出去。
# 现在给一组候选，输入重试时轮换着点；判据区域也放宽到能框住新旧两种布局。
# ★★ 2026-10-08 重大修正（"麦克风陷阱" —— 5 条垃圾消息的真正入口）：
#   微信聊天页输入框的**右侧内部**有一个语音麦克风按钮。虚拟屏 636x1400 实测：
#     · 输入框本体  x≈148~362, y≈1264~1362
#     · 麦克风按钮  x≈282~326, y≈1284~1342（中心 ≈ (304,1313)）
#   旧坐标 (300,1305)/(300,1318) **正好压在麦克风正中** —— 实测 tap 它一下：
#     `dumpsys audio` → `rec update ... pack:com.tencent.mm`（微信真的开始录音）
#   录到的是用户**外放**的抖音直播，转成文字顶进输入框 → 下一轮被当回复发进群。
#   修复：tap 点全部左移到输入框的**文字区**（x 200~240，离麦克风 ≥45px）。
#   二次实测（2026-10-08 晚）：输入框**有字时会变窄**，右侧麦克风跟着左移到
#   x≈212~245（见 /tmp/wxbridge-filled-1.jpg）。所以 215 也不够安全 ——
#   x=180 才是"空框 / 有字"两种状态下都安全的点：
#     · tap(180,1315) → dumpsys mServedView=…MMEditText（**聚焦成功**）；
#     · 同时 dumpsys audio 无 com.tencent.mm 录音（**不开麦**）。
INPUT_BOX_POS = ((180, 1315), (160, 1315), (200, 1310), (180, 1335))
INPUT_BOX = INPUT_BOX_POS[0]        # 兼容老引用
# 输入栏最左侧的「语音/键盘」切换按钮（实测：点它**必停录音**，而 BACK 停不掉）
VOICE_TOGGLE_POS = (75, 1310)
# "按住 说话"两个字所在的区域（语音模式判据用；INPUT_CROP 那一块读不到它）
VOICE_MODE_CROP = "320x140+150+1240"
# 发送按钮同样会随布局上下移动：先 OCR 认字（find_send_btn），认不到才用这两组候选
SEND_BTN_POS = ((508, 1268), (555, 1357))
SEND_BTN = SEND_BTN_POS[0]          # 兼容老引用
# ★ 2026-10-07 修正：原来框到 y1220~1390 —— **那段主要是下面的工具栏**（语音/表情/+）
#   实测空输入框时该区域 std=0.164（工具栏图标的图案），而真正的输入框在
#   y≈1170~1256，空框 std 只有 **0.015**。
#   旧值的后果：`base`（发送前基线）虚高到 0.166 → 判"有字"的阈值被抬到 0.216 →
#   **字已经进框了也判成"空"** → 放弃发送。用户 14:58 那次就是这么白抢一次屏、
#   群里什么都没发出去（日志：stddev=0.185986 < 0.2164 且 ocr 为空）。
# ★ 2026-10-08 二次修正（旧值实测已**完全失效**）：
#   旧 "340x86+130+1170" 框到的是**输入框上方的聊天区/气泡**：实测
#   空框 std=0.1616、有字 std=0.1617 —— **完全区分不出有没有字**，
#   而且 OCR 会读出聊天区的字（'目' 这种不在期望文本里的）→ 把正常消息
#   误判成"内容对不上"而拒绝发送。25 倍的信号白白丢掉。
#   现值实测（虚拟屏 636x1400，微信输入栏）：
#     · 空框 std=0.0097、有字（「坐标自检喵」）std=0.2386（≈25 倍）；
#     · 有字时 OCR 读到 ['检','喵']（两个字都在期望文本里）。
#   x 只到 280：把输入框右侧那个**语音麦克风按钮**（x≈282~326）排除在外，
#   免得它的图标图案抬高基线、污染判据。
INPUT_CROP = "135x130+145+1240"
# ★ 2026-10-08 晚（实测踩到的真 bug）：**OCR 复核要另一个更大的窗口**。
#   上面那个 135x130 是给 stddev 判据标定过的（空框 0.0097 / 有字 0.2386），
#   故意只取输入框下部 130px、x 只到 280（避开右侧麦克风按钮）—— 这些都不能动。
#   但把它**同时**用来做「框里的字是不是我要发的」复核就出事了：
#   实测虚拟屏的字体约 54px/字，输入框可用宽度只够**一个字**，所以
#   「耗时测量喵」在框里是**竖排 5 行**（y≈1060~1320）。固定窗口只覆盖
#   y=1240~1370，正好切在「量」字中间 → OCR 把残字读成「里」→
#   _input_text_ok 正确地拒绝了一条**其实完全正常**的输入 → 这条发不出去。
#   所以复核单独用这个窗口：覆盖整个输入框，上边界取 1040 是为了不碰
#   输入框上方那条「下午4:43」时间戳。
# ★ 2026-10-09：**上边界 1040 → 1210、高度 300 → 160**。用户报「机器人说框里
#   的字不是要发的，这条死活发不出去」—— 拿当时那帧（/tmp/wxbridge-input-9.jpg）
#   按两个窗口各 OCR 一遍，结论很干脆：
#     · 「土群专享」（群里那条「社群专享」）在**原图 y=1116~1174**，是聊天区；
#     · 输入框里的字在 **y≥1240**（135x130 那个小窗口读到的就是「？喵」本身）。
#   也就是说 1040~1210 这 170px **全是聊天区**，被拼进复核文本之后，
#   「土群专享?喵」里的四个不明字直接把一条完全正常的输入拒了（连拒三次）。
#   ⚠️ 代价：输入框被长文本撑高时，上面那截读不到了。这是**故意**的取舍 ——
#   读不全时读到的仍是期望文本的子集，正向判据照过；而多读聊天区是**必然误判**。
INPUT_OCR_CROP = "135x160+145+1210"
# ★ 2026-10-09 下午（用户报「这条没发出去 · 输入框里的字不是要发的（读到 卷1）」）：
#   **复核窗口按要发的字数自适应放大** —— 见 `_input_ocr_crop_for()`。
#   一句话说清病根：虚拟屏字体约 54px/字、输入框宽度只够一个字，文本在框里是
#   **竖排**（一个字一行）、微信最多显示 7 行；而上面那个固定窗口只有 160px 高
#   ≈ 3 行，所以复核**只读得到末尾两三个字**，2 个字里再错 1 个就只能从严拒发
#   （连拒 3 次 + 补发 2 次 = 这条永久没了）。
#   实测同一帧：135x160+145+1210 → 读到 ['卷']；145x410+145+900 → 读到
#   ['尾','巴','/','卷'] → 判据 ② 直接放行。
_LAST_INPUT_CROP = [INPUT_OCR_CROP]      # 最近一次复核实际用的窗口（只给日志用）
# 发送键定位裁窗的默认值（half 档）；apply_screen_size() 会按实际档位覆盖它。
SEND_BTN_CROP = "636x210+0+1190"
SEND_BTN_CROP_Y = 1190

# ============================================================================
# ★ 2026-10-10：**坐标/裁剪窗的自动缩放**（虚拟屏可以切 half/full 档了）
# 上面那些坐标和 INPUT_*_CROP 全是按 **636 宽**（half 档）实测标定的；切到 full 档
# （1272x2800@560dpi）像素翻倍，不缩放的话点「放大镜」会点空、输入框裁剪窗全错位
# （实测症状：`第1次点放大镜 (430,80) 没进搜索页`，切群直接失败）。
# 做法跟 read/wxreader.py 一样：留一份基准值，按 `实际图宽 / 636` 重算模块级常量。
_SCREEN_BASE_W = 636
# ★★ 2026-10-10 实测结论：**两档的 UI 布局本来就不同，坐标不能靠 ×2 换算**。
#   half 档（280dpi）微信会把 UI 整体放大（标题字号约 17sp），full 档（560dpi）
#   才是"正常手机"的样子（12sp）—— 所以输入框、发送键这些**边缘锚定**的元素
#   在两种档位下的像素位置**不成简单比例**（实测：输入框 y 差 180px）。
#   于是每个档位**各存一套实测值**，按截图宽度选最接近的那一档。
_SCREEN_PIXEL_BASE = {
    # half：636x1400 @280dpi（历史实测值）
    636: {
        "INPUT_BOX_POS": ((180, 1315), (160, 1315), (200, 1310), (180, 1335)),
        "SEND_BTN_POS": ((508, 1268), (555, 1357)),
        "SEARCH_BTN_POS": ((430, 80), (508, 47), (455, 170), (455, 65)),
        "SEARCH_INPUT_POS": ((350, 107), (300, 105), (400, 110)),
    },
    # full：1272x2800 @560dpi（2026-10-10 真机实测：输入框 y2660~2770、
    #       发送键 x1050~1245 y2660~2765、群工具条 y~2550~2610）
    1272: {
        "INPUT_BOX_POS": ((400, 2715), (380, 2715), (430, 2715), (400, 2740)),
        "SEND_BTN_POS": ((1150, 2712), (1150, 2712)),
        "SEARCH_BTN_POS": ((860, 160), (1016, 94), (910, 340), (910, 130)),
        "SEARCH_INPUT_POS": ((700, 214), (600, 210), (800, 220)),
    },
}
# 发送键定位用的裁窗（OCR 认「发送」二字）。half 档在 x0~636 / y1190~1400；
# full 档发送键实测 x1050~1245 / y2660~2765，取输入栏那一整条。
# ★ 2026-10-09 晚：原来 find_send_btn 里写死 half 档的 "636x210+0+1190"，
#   full 档下裁到的是屏幕中部 → 认不到「发送」→ 只能回退固定坐标（能用，但
#   位置一旦变就点空）。这里按档位各存一套。
_SCREEN_SEND_CROP_BASE = {            # (宽, 高, x, y)
    636: (636, 210, 0, 1190),
    1272: (1272, 300, 0, 2500),
}
_SCREEN_CROP_BASE = {                 # (宽, 高, x, y)
    # half：输入框 x100~480 / y1240~1370
    636: {"INPUT_CROP": (135, 130, 145, 1240),
          "INPUT_OCR_CROP": (135, 160, 145, 1210)},
    # full：输入框 x155~1000 / y2660~2770
    1272: {"INPUT_CROP": (450, 110, 175, 2665),
           "INPUT_OCR_CROP": (450, 125, 175, 2650)},
}
# 复核窗按字数算高度时用的参数（行高、底边、上界、宽、x、每行几个字）
#   ★ 2026-10-10（续22）「per_line」是**两档排版不同的关键**：
#     · half 档（636）：输入框窄，文字**竖排**（一个字一行）→ per_line=1，
#       行数 = 字数，沿用老公式（行为跟以前一个字节都没变）。
#     · full 档（1272）：输入框白框实测 x152~925（**宽 774px**）、字步进约 48px
#       → 文字**横排**、**一行约 16 个字**，行步进 **65px**。
#   老代码把 full 档也当成"一字一行"（`line: 110`），又用 ceiling 把窗口锁成
#   125px 高、450px 宽 —— 于是超过一行的回复必然**骑在两行交界**上，而宽度
#   只截到每行左边 9 个字。实测 17 条正常回复被拒发（读到「荣道服约战别奴店
#   上礼拜刚被塞去卡」这种跨行残字），补发也走同一条路 → 消息永久丢失。
_SCREEN_LINE_BASE = {
    636: {"line": 55, "bottom": 1370, "ceiling": 900, "w": 135, "x": 145,
          "per_line": 1},
    # ★ 2026-10-09 晚先记一笔（用户报「读到 '群专享抽奖...完成哦全国可飞本喵爪'」）：
    #   ceiling 曾是 1800（照 half 档 900 ×2 推的），窗口 450x800+175+1975 一路顶
    #   进输入框上方的群工具条 → 拒发。当时把 ceiling 压到 2650（= 只能看一行）
    #   治好了那一条，却埋下了"长文本必然骑行边界"的新病（见上）。
    # ★ 2026-10-10（续22）按**实测排版**重标，不再靠"一行/多行"猜：
    #   line=65（行步进）、w=772/x=152（正好框住输入框白框 152~925）、
    #   per_line=16（一行几个字）、ceiling=2258（= 7 行白框顶，微信输入框最多
    #   显示 7 行）。窗口高 = 62 + 65×行数，正好等于输入框白框高，**不会再骑在
    #   行中间**；工具条永远被输入框顶在上面（差 ≥1 行），所以读不到它。
    #   ⚠️ 宁可把行数算少（per_line 取实测容量）：窗口偏矮只会读不全，
    #   而判据① 对"子集"是放行的；算多了才会往上读进聊天区。
    1272: {"line": 65, "bottom": 2775, "ceiling": 2258, "w": 772, "x": 152,
           "per_line": 16},
}
_cur_screen_scale = 1.0
_SCREEN_KNOWN = [0]                   # 已知的屏宽（0 = 还不知道）


def screen_scale():
    """当前档位相对 636 宽（half 档）的缩放因子（只给散点坐标兜底用）。"""
    return _cur_screen_scale


def _pick_tier(width):
    """选最接近的档位键（636 / 1272）。"""
    return min(_SCREEN_PIXEL_BASE.keys(), key=lambda k: abs(int(width) - k))


def apply_screen_size(width):
    """按截图的实际像素宽度切换到对应档位的坐标/裁剪窗（并通知 wxreader）。"""
    global _cur_screen_scale, INPUT_BOX, SEND_BTN, INPUT_CROP, INPUT_OCR_CROP
    try:
        w = int(width)
    except (TypeError, ValueError):
        return _cur_screen_scale
    if w <= 0:
        return _cur_screen_scale
    _SCREEN_KNOWN[0] = w
    tier = _pick_tier(w)
    k = w / float(_SCREEN_BASE_W)
    changed = abs(k - _cur_screen_scale) >= 0.02
    g = globals()
    for name, base in _SCREEN_PIXEL_BASE[tier].items():
        g[name] = base
    for name, crop in _SCREEN_CROP_BASE[tier].items():
        g[name] = "%dx%d+%d+%d" % crop
    _sc = _SCREEN_SEND_CROP_BASE[tier]
    g["SEND_BTN_CROP"] = "%dx%d+%d+%d" % _sc
    g["SEND_BTN_CROP_Y"] = _sc[3]
    g["INPUT_BOX"] = INPUT_BOX_POS[0]        # 兼容老引用
    g["SEND_BTN"] = SEND_BTN_POS[0]
    _cur_screen_scale = k
    if changed:
        print("[wxbridge] 坐标切到 %d 宽档（缩放 %.0f%%，实测基准）"
              % (w, k * 100), flush=True)
        if wxreader is not None:
            try:
                wxreader.apply_resolution(w)
            except Exception:
                pass
    return k


def line_params():
    """复核窗按字数算高度用的参数（当前档位）。"""
    return _SCREEN_LINE_BASE[_pick_tier(_SCREEN_KNOWN[0] or _SCREEN_BASE_W)]


def _note_frame_size(img_path):
    """截图后记下这一帧的像素尺寸（缩放全靠它）。"""
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            apply_screen_size(im.width)
    except Exception:
        pass


def _sp(x, y):
    """把「按 636 宽标定的坐标」换算到当前档位（half/full 都能用）。"""
    _ensure_screen_known()
    k = screen_scale()
    return int(round(x * k)), int(round(y * k))


# ★ 2026-10-10（续22）：full 档输入框的实测几何（都是真机量出来的，改之前先重新量）
#   行步进 65px、文字行高 48px、文字块底部距白框底 38px、顶部留白 41px
#   ⇒ 白框高 = 62 + 65 × 行数（1 行 127px、3 行 257px）；白框横向 x152~925（宽 772）
_FULL_LINE_STEP = 65
_FULL_BOX_PAD = 62


def _input_lines_from_shot(img, lp):
    """从"已填充"的截图里**数**输入框有几行文字（full 档）。

    ★ 为什么要数、不按字数估：一行放几个字会随空格/标点浮动（实测 12~16 字），
    按字数估行数就会错一行 —— 而窗口一旦比白框矮 65px，它的上沿就**骑在上一行
    中间**，OCR 读到的是跨行的残字（「白」→「八」、「喵」→「临」），
    判据① 于是把一条**完全正常**的回复拒发。2026-10-10 实测：51 字的回复按
    16 字/行估成 3 行、实际 4 行 → 拒发（读到「八好能这么本临从头首到尾…」）。

    做法：在最下面一行往上找"暗像素行段"，**行步进落在 55~75px 才认作同一框内
    的上一行**（实测 64~65px；聊天区/群工具条的文字与输入框之间差 ≥124px，
    所以不会串进来）。数不出来返回 0，交给调用方按字数估算兜底。
    """
    if not img or not os.path.exists(img):
        return 0
    try:
        from PIL import Image
        import numpy as _np
        x0 = lp["x"]
        x1 = lp["x"] + lp["w"]
        y_bot = lp["bottom"]
        # 最多 7 行白框 + 一点余量；上界不越过 ceiling 太多
        y_top = max(y_bot - (_FULL_BOX_PAD + _FULL_LINE_STEP * 7 + 20),
                    lp["ceiling"] - 20)
        with Image.open(img) as im:
            col = _np.asarray(im.convert("L").crop((x0, y_top, x1, y_bot)))
        dark = (col < 120).sum(axis=1)        # 每行的暗像素个数
        tops = []                             # 每个"文字行段"的起始 y（原图坐标）
        inrow, start = False, 0
        for i, v in enumerate(dark):
            if v > 3 and not inrow:
                start, inrow = y_top + i, True
            elif v <= 3 and inrow:
                tops.append(start)
                inrow = False
        if inrow:
            tops.append(start)
        if not tops:
            return 0
        # 最后一行必须紧贴输入框底部（文字从底部往上堆，实测距底 ≈86px）
        if not (40 <= y_bot - tops[-1] <= 110):
            return 0
        lines, prev = 1, tops[-1]
        for a in reversed(tops[:-1]):
            if 55 <= (prev - a) <= 75:
                lines += 1
                prev = a
            else:
                break
        return lines
    except Exception:
        return 0


def _input_ocr_crop_for(text, img=None):
    """复核用的输入框裁剪窗口 —— 按**这一档的实际排版**定位输入框白框。

    ★ 2026-10-10（续22）：两档排版不同，必须分开算（见 `_SCREEN_LINE_BASE`）：
      · half（`per_line == 1`）：文字竖排（一个字一行）、行高 55px、最多 7 行，
        沿用老公式 —— 行为跟以前完全一致。
      · full（`per_line > 1`）：文字横排、行步进 65px、白框高 = 62 + 65×行数，
        底边 2775、左右刚好是白框的 x152~925。行数**优先从截图里数**
        （`_input_lines_from_shot`），数不出来才按字数估算。
    窗口 = 输入框白框本身，所以**永远不会骑在两行交界**（老代码锁死 125px 高、
    450px 宽，长文本必然骑边界、且只截到每行左边 9 个字 → OCR 读成跨行残字
    → 正常回复被当"串输入"拒发）。
    上边界仍不许越过本档 `ceiling`（那是"最多 7 行"的白框顶，兜底用）。
    """
    n = len(re.sub(r"\s+", "", text or ""))
    lp = line_params()          # ★ 2026-10-10：按当前档位取
    per = max(1, int(lp.get("per_line", 1) or 1))
    if per > 1:
        # full 档：横排。① 先从截图里数行（准）；② 数不出来才按每行容量估。
        lines = _input_lines_from_shot(img, lp)
        if lines <= 0:
            lines = (n + per - 1) // per
        lines = max(1, min(lines, 7))
        h = _FULL_BOX_PAD + _FULL_LINE_STEP * lines
    else:
        # half 档：竖排（一字一行）—— 老公式原样保留，别动。
        lines = max(1, min(n, 7))
        h = max(lp["w"] + 25, min(30 + lp["line"] * lines, 415))
    # 底边留在输入框下沿：短句只有两行、「喵」会落到下沿以下，底边抬高会把
    # 「?喵」读成「?」（2026-10-09 实测踩过）；上界不许越过聊天区。
    top = max(lp["bottom"] - h, lp["ceiling"])
    return "%dx%d+%d+%d" % (lp["w"], lp["bottom"] - top, lp["x"], top)
# ★ 2026-10-09：复核走「反向判据」的最短期望长度（见 _input_text_ok ③）。
#   长句才开口子：短句（「喵」「？」）一旦允许"框里多出来的字不管"，别人的一句话里
#   随便带一个同样的字就能骗过它 —— 宁可这一条不发。
REVERSE_MIN_CHARS = 6
# OCR 会把输入框里的「一」读成「1」（竖排 + 54px 字体，实测「尾巴一卷」→「卷1」）。
# 复核比对时两边都做一次数字归一 —— 汉字和阿拉伯数字谁也别赖谁。
_OCR_DIGITS = str.maketrans("0123456789", "零一二三四五六七八九")
# ★ 2026-10-09 晚：OCR 常把全角标点读成半角（实测输入框里的「？」读出来是「?」）。
#   全标点短句走 :func:`_punct_subset_ok` 时两边都先过这张表，免得因为半/全角
#   差异把一条正常输入拒掉。
_PUNCT_NORM = str.maketrans({
    "?": "？", "!": "！", ",": "，", ".": "。", ":": "：", ";": "；",
    "(": "（", ")": "）", "~": "～",
})


def _punct_subset_ok(got, want):
    """全是标点的短句兜底：读到的字符**全都在**期望文本里 → 放行。

    ★ 2026-10-09 晚：用户早就报过「？喵 发不出去」，当时只治了"框里多出杂字"
    那一半。这次实测把另一半坐实了 —— 「？喵」在框里被读成「?」，而主判据开头
    的 ``re.sub(r"[\\s\\W_]+", "", …)`` 会把标点**全部删光**，于是 g="" → 直接
    ``return False``，纯标点短句永远发不出去（机器人被 @ 时的「？喵」正是这条）。

    ⚠️ 只兜"清完什么都不剩"这一种，**不改主判据**：主判据那条路标点照旧被忽略，
    所以 OCR 往句子里插一个噪声标点（实测有 ``群专享抽奖.|完成``）不会新增误拒；
    而这条兜底因为不看字数，绝不能拿去替代主判据 —— 别人的消息里一个标点就能
    骗过它。这里只要求「读到的标点全在期望里」，读到的若含别的标点仍然拒发。
    """
    g = re.sub(r"\s+", "", got or "").translate(_OCR_DIGITS).translate(_PUNCT_NORM)
    w = re.sub(r"\s+", "", want or "").translate(_OCR_DIGITS).translate(_PUNCT_NORM)
    if not g or not w:
        return False
    return all(ch in w for ch in g)
STDDEV_FILLED = 0.05
STDDEV_EMPTY = 0.02

IME_OURS = "com.dsh.clip/.DshIme"
IME_FALLBACK = "com.iflytek.inputmethod/.FlyIME"

# ★ 2026-10-09（续14）用户报：「有时候我在桌面的时候仍然会卡输入法，机器人回复的
#   时候桌面上仍然有一个输入法没按下去」。
#   根子在 `ime set` 是**全局**操作 —— 机器人给**虚拟屏**微信打字，换掉的却是
#   **整个系统**的默认输入法（「DSH 输入」是个没有键盘的输入法）。所以"还回去"
#   必须可靠，而原来它有几处会漏：adb 超时/断线、桥接中途被杀（重启、看护重启）、
#   甚至 `ime set` 静默失败没人管。而整条路**一行日志都没有**，出事了没法回溯。
#   这两个变量用来兜底：
#     · `_IME_USER_LAST` —— 用户自己的输入法（最近一次见到的，"还回"时的首选）；
#     · `_IME_LEASE`     —— 借用登记：什么时候借的、该还给谁。看护线程每
#       RECLAIM_TICK 秒看一眼，**超时还没还就替他还**（见 `_ime_unstick`）。
_IME_USER_LAST = [""]                    # 例：["com.iflytek.inputmethod/.FlyIME"]
_IME_LEASE = {"at": 0.0, "back": ""}
# 借用超过这么久还挂着 = 泄漏了（任何单次发送/切群都远不到 2 分钟）
IME_LEASE_MAX = float(os.environ.get("WX_IME_LEASE_MAX", "120"))

_lock = threading.Lock()
_seq = 0
# 物理屏最后一次确认「正在用微信」的时刻 —— 用来给用户留收回缓冲（见 ensure_wechat_front）
_phys_wx_seen = 0.0


def _run(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        print("[wxbridge] 执行失败 %r: %s" % (args[:2], exc), flush=True)
        return None


def shd(cmd, timeout=25):
    r = _run(["sh", SH, cmd], timeout)
    if r is None:
        return ""
    out = (r.stdout or "").replace("\r", "")
    return "\n".join(l for l in out.splitlines()
                     if not l.startswith("[EXIT")).strip()


def _serial():
    try:
        with open(os.path.join(ROOT, ".adb-serial"), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


SERIAL = _serial()


ADB_RECHECK = 60.0        # 「adb 不可用」多久允许重探一次（秒）；见 adb_ok() 的说明
_adb_state = {"ok": None, "at": 0.0}   # ok=最近一次探测结果；at=那次探测的时间

def _serial_live():
    """★2026-10-14：每次 adb 调用前重读 .adb-serial（旧的 SERIAL 只读一次）。

    手机重启 / 重新配对之后，无线调试的地址和端口都会变（`connect-adb.sh`
    会把新地址写进 .adb-serial），但桥接原来只在**进程启动时**读一次，
    于是一直攥着旧串号，报 `adb: device '旧地址' not found` ——
    **adb 明明是通的，机器人却照样瞎**（2026-10-14 实测踩到）。
    串号真变了就换掉。**故意不动 `_adb_state`**：同一个设备只是换了地址，
    真连不上时后续命令失败自会翻转状态；而且离线测试会把 `SERIAL` 打桩成假地址
    （test_adb_classify.py），在这里重置 adb 状态会把打桩打坏（2026-10-14 踩过）。
    """
    global SERIAL
    s = _serial()
    if s and s != SERIAL:
        print("[wxbridge] adb 串号已换：%s → %s（自动跟随）" % (SERIAL, s), flush=True)
        SERIAL = s
    return SERIAL


# ★ 2026-10-12：adb 状态翻转统计（进 /health）+ 传输层故障判别。
#   起因：桥接日志里反复出现「adb 直连 可用」→「adb 执行失败 → 改用命令守护」，
#   而手动 `adb shell echo ok` 一直正常。根子在 `adb()` 原来拿 **returncode != 0**
#   当"adb 断了"：`dumpsys … | grep -m1 xxx` 没匹配到时 grep 退出码是 1，
#   adb 如实转回 1 —— 命令本身一切正常，却被判成"adb 断线"，于是：
#     ① adb 被翻成不可用，之后 60 秒内**每条命令**都走慢几百倍的命令守护；
#     ② 同一条命令还在守护里**再执行一遍**（点击/按键这类有副作用的会做两次）；
#     ③ preflight_yield 看到 adb 不可用，就报「抢不了：adb 断了」把这条发送放掉
#        （19:50 / 20:02 那几次"抢不到输入焦点"很可能就是这么来的）。
#   现在只有**真的传输层故障**（超时/起不来 adb/stderr 是 adb 自己的报错）才翻。
_ADB_STAT = {"flips": 0, "remote_nonzero": 0, "last_err": "", "last_flip_at": 0.0}
_ADB_TRANSPORT_ERR = re.compile(
    r"(?m)^(?:adb: |error: (?:device|no devices|closed|protocol|more than one|"
    r"cannot|insufficient))|"
    r"device (?:'[^']*' )?not found|device offline|no devices/emulators|"
    r"failed to connect|cannot connect|daemon not running|device unauthorized")


def _adb_transport_failed(r):
    """一次 ``adb shell`` 的结果是不是**传输层**坏了（而不是远端命令自己退出码非 0）。

    · r 是 None → 超时 / 起不来 adb 进程 → 坏；
    · returncode 非 0 且 stderr 命中 adb 自己的报错措辞 → 坏；
    · returncode 非 0 但 stderr 是远端命令的话（``ls: …: No such file``）或空
      （grep 没匹配）→ 远端命令的正常结果，**不算** adb 故障。
    """
    if r is None:
        return True
    if r.returncode == 0:
        return False
    return bool(_ADB_TRANSPORT_ERR.search(r.stderr or ""))


def adb_ok(force=False):
    """adb 直连可用吗？

    无线调试一关，adb 立刻变 offline（用户按 README 的最佳实践就是"用完就关"），
    所以不能假设它一直在。结果缓存，免得每条命令都去探一次。
    """
    # ★ 2026-10-07：原来只在**进程启动时**探一次 —— 中途一旦失败（adb 抖一下 /
    #   kill-server / 同步大量操作把它挤掉）就被永久翻成 False：health 一直显示
    #   `adb:false`，而且这个进程之后每条命令都走**慢几百倍**的命令守护，永不恢复。
    #   现在给「不可用」加冷却：每 ADB_RECHECK 秒允许重探一次；「可用」仍长缓存。
    if (_adb_state["ok"] is None or force
            or (not _adb_state["ok"]
                and time.time() - _adb_state["at"] > ADB_RECHECK)):
        ok = False
        _s = _serial_live()
        if _s:
            r = _run(["adb", "-s", _s, "shell", "echo ok"], 12)
            ok = bool(r is not None and r.returncode == 0 and "ok" in (r.stdout or ""))
        _adb_state["ok"] = ok
        _adb_state["at"] = time.time()
        print("[wxbridge] adb 直连 %s" % ("可用" if ok else
              "不可用（无线调试关了？）→ 回退命令守护，会慢很多"),
              flush=True)
    return bool(_adb_state["ok"])


def adb(cmd, timeout=30):
    """在设备上执行 shell 命令：adb 优先，不可用就回退命令守护。

    交接说明坑 1 说"一律用 adb，shd 慢 500 倍"；但 README 的最佳实践又是
    "拉起守护后就把无线调试关掉"。两者冲突 —— 所以两条腿都得能走：
    有 adb 走 adb（快），没有就退回 shd（慢，但不需要无线调试常开）。
    """
    if adb_ok():
        _t0 = time.time()
        r = _run(["adb", "-s", _serial_live(), "shell", cmd], timeout)
        if r is not None and r.returncode == 0:
            return (r.stdout or "").replace("\r", "").strip()
        if r is not None and not _adb_transport_failed(r):
            # 远端命令自己退出码非 0（典型：grep 没匹配）——adb 本身是好的，
            # 原样返回 stdout，**不翻状态、不重跑**（重跑会让有副作用的命令做两次）
            _ADB_STAT["remote_nonzero"] += 1
            return (r.stdout or "").replace("\r", "").strip()
        _why = ("超时/起不来" if r is None else
                "rc=%s stderr=%s" % (r.returncode, (r.stderr or "").strip()[:100]))
        _ADB_STAT["flips"] += 1
        _ADB_STAT["last_err"] = "%s | %.1fs | %s" % (_why, time.time() - _t0, str(cmd)[:60])
        _ADB_STAT["last_flip_at"] = time.time()
        # ★ 2026-10-05：「adb 判定可用」却执行失败 —— 多半是中途断了（adb kill-server /
        #   无线调试被关）。adb_ok() 的结果是**进程启动时**缓存的，不翻掉的话之后每条命令
        #   都要先白等一次 adb 超时（最长 30s）才回退守护。一次发送要点好几次输入，
        #   累积几十秒足够让整轮判失败（实测 adb 断开期间出现过「本次发送判定为失败」）。
        #   这里翻成 False，后续直接走命令守护，不再空等。
        _adb_state["ok"] = False
        _adb_state["at"] = time.time()
        print("[wxbridge] adb 执行失败（%s）→ 本进程改用命令守护（慢，但不需要 adb）"
              % _ADB_STAT["last_err"], flush=True)
    return shd(cmd, timeout=max(timeout, 90))


def display_id_from_adb():
    """vscreen 守护不通时的备用路径：用 dumpsys display 反查虚拟屏号。

    守护每次重启，虚拟屏的 displayId 都会变（实测 15 → 19 → 21 …），硬编码必错；
    但屏的 uniqueId 里始终带 `dsh-trusted-<slot>`，拿它反查 displayId 最稳。
    2026-10-03 加，起因：守护 HTTP 层卡死，display_id() 拿不到值后退回写死的 15，
    而 15 早就不存在了 —— 于是 ensure_wechat_front 永远失败、切群必然超时。
    """
    try:
        out = adb("dumpsys display", 60)
    except Exception:
        out = None
    if not out:
        return None
    want = "dsh-trusted-%d" % SLOT
    # ① mBaseDisplayInfo=DisplayInfo{"dsh-trusted-0", displayId 21, ...
    m = re.search(r'DisplayInfo\{"%s", displayId (\d+)' % re.escape(want), out)
    if m:
        return int(m.group(1))
    # ② mViewports 里：displayId=21, uniqueId='virtual:com.android.shell,2000,dsh-trusted-0,17'
    m = re.search(r"displayId=(\d+), uniqueId='[^']*%s," % re.escape(want), out)
    if m:
        return int(m.group(1))
    return None


_DID_CACHE = {"v": None}


def display_id(cached_only: bool = False):
    """虚拟屏号 —— 带缓存，别每次重猜。

    ⚠️ 2026-10-03 实测：vscreen 守护挂掉之后，这条函数每次都要
    `node vscreen list` 等满 8 秒（连不上）再回退 dumpsys —— 而 `/health` 里
    就是调的它，看护线程探测只给 **4 秒**，于是 `/health` 必然超时、看护判定
    「桥接没在跑」→ 反复重启桥接，机器人正读屏时被一刀切断（日志里成片
    connection refused 就是这么来的）。屏号在一次守护生命周期里不会变，
    缓存起来既能救 /health，也让每次 tap/send 少等 8 秒。
    """
    if _DID_CACHE["v"] is not None:
        return _DID_CACHE["v"]
    if cached_only:
        # ★ 2026-10-08 审查修复（F3 配套）：`/health` 这类探活接口**绝不阻塞**。
        #   拿不到就用 _LAST_DID 的记忆值（可能为 None），既不重探、也不等 8 秒。
        #   历史教训：/health 一旦会阻塞，看护 4 秒探测就超时 → 判定"桥接没在跑"
        #   → 反复重启桥接，机器人正读屏时被一刀切断。
        return _LAST_DID["known"]
    did = None
    r = _run(["node", VSCREEN, "list"], 8)
    if r is not None:
        try:
            for s in json.loads(r.stdout):
                if s.get("i") == SLOT:
                    did = s.get("displayId")
        except Exception:
            pass
    if did is None:
        did_adb = display_id_from_adb()
        if did_adb:
            print("[wxbridge] vscreen 客户端不通，改从 dumpsys 认屏：displayId=%d"
                  % did_adb, flush=True)
        did = did_adb
    if did is not None:
        _DID_CACHE["v"] = did
    # ★ 2026-10-08 审查修复（F3）：拿不到就**不要缓存**。原来只有成功才写缓存，
    #   但 did() 会把 None 变成常量 15 再缓存 —— 一旦两条获取路径都失败，整个
    #   进程生命周期内所有 tap / swipe / screencap / am start 都会打到不存在的
    #   display 15 上，而且一声不吭（机器人看起来"活着"，实际什么都点不到）。
    #   现在失败时留空，下次调用重试（node 冷启动慢，但不该被永久判死）。
    return did


# 最近一次成功认到的屏号 —— 拿不到时的兜底（比硬编码 15 靠谱得多）
_LAST_DID = {"known": None}


def did():
    """当前该操作哪个屏。

    ⚠️ 2026-10-08 审查修复（F3）：这里原来是 ``display_id() or 15``。
    硬编码 15 是 2026-10-03 之前的历史屏号，早就不存在了 —— 它唯一的"作用"
    是把「读不到屏号」这件必须被看见的事故，变成一串静默失败（实测教训见
    ``display_id_from_adb`` 的文档串：15 不存在时 ensure_wechat_front 永远失败、
    切群必然超时）。现在改成三级：**本次读到 > 上次读到 > None**。
    ⚠️ 绝不回退到 0（物理屏）：在用户自己的屏幕上乱点，比"这一下没生效"更糟。
    拿不到就返回 None，由"动手前的守卫"统一拦下（见 _require_did）。
    """
    v = display_id()
    if v is not None:
        _LAST_DID["known"] = v
        return v
    last = _LAST_DID["known"]
    if last is not None:
        print("[wxbridge] ⚠️ 读不到虚拟屏号，沿用上次已知的 displayId=%s" % last,
              flush=True)
        return last
    return None


# 读不到屏号时**必须拒绝动手**的调用点用它包一层（见 did() 的说明）。
_DID_WARN = {"at": 0.0}


def _require_did():
    """要动手之前先要一个可信的屏号；拿不到就返回 None 并打节流告警。

    为什么专门有个函数：did() 读不到时返回 None，而 `"input -d %s" % None` 会拼出
    畸形命令（静默失败）、`int(None)` 会抛异常。与其在十几个调用点各写一遍判断，
    不如统一在这里拦下，并且**告警有节流**（60 秒最多一条，别刷屏）。
    """
    v = did()
    if v is not None:
        return v
    now = time.time()
    if now - _DID_WARN["at"] > 60:
        _DID_WARN["at"] = now
        print("[wxbridge] ⚠️ 读不到虚拟屏号 —— 本次操作已放弃（查 vscreen 守护 / adb）",
              flush=True)
    return None


def tap(x, y):
    _ensure_screen_known()
    d = _require_did()
    if d is None:
        return False
    adb("input -d %d tap %d %d" % (d, int(x), int(y)), 15)
    return True


WX_TMP = os.path.join(ROOT, "tmp")
_vscreen_dead = {"v": False}


def _frame_direct(path):
    """直接问守护要一帧 JPEG：``GET http://127.0.0.1:<VSD_PORT>/<slot>/frame``。

    这就是 vscreen 客户端内部走的那条路，只是少了一层 node 进程。
    2026-10-04 实测的教训：node 客户端冷启动常超过 6 秒被判超时 → 整进程标记
    ``_vscreen_dead`` → 退回 ``screencap`` —— 而 screencap 对 vscreen 造的
    VirtualDisplay 只会 ``Capturing failed``，机器人从此**永久瞎掉**（还看不出原因）。
    所以这条 HTTP 通道必须排在最前面。
    """
    url = "http://127.0.0.1:%d/%d/frame" % (VSD_PORT, SLOT)
    # ⚠️ 2026-10-05 守护升级后的新情况：屏上**还没合成出任何一帧**时，/frame 返回
    #    12 字节纯文本 "no-frame-yet"（旧版副屏一直镜像物理屏，任何时候都有图，
    #    永远不会出现这个）。这不是「守护挂了」—— 微信刚被搬上副屏的头几百毫秒
    #    就会这样。必须短暂重试：否则会掉进 shot() 的回退链（node 客户端冷启动最长
    #    20 秒 → 再退回对 VirtualDisplay 必然失败的 screencap），白白废掉一轮读屏。
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wxbridge"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
            if data and len(data) > 2000 and data[:2] == b"\xff\xd8":      # JPEG 魔数
                with open(path, "wb") as f:
                    f.write(data)
                return True
            if (data or b"").strip() == b"no-frame-yet":
                if attempt < 3:
                    time.sleep(0.5)
                    continue
                print("[wxbridge] 守护 /frame 连续 4 次 no-frame-yet（屏上还没画面）",
                      flush=True)
                return False
            print("[wxbridge] 守护 /frame 没给图（%d 字节）" % len(data or b""), flush=True)
            return False
        except Exception as exc:
            print("[wxbridge] 守护 /frame 取图失败：%r" % (exc,), flush=True)
            return False
    return False


def shot(path):
    """抓虚拟屏画面，两条通道任一条通就行。

    ① vscreen 客户端（走 vscreen-daemon :18240）—— 最快，但它挂了就没戏；
    ② 退回 `screencap -d N`，图片落 /sdcard（容器直接读得到）。

    实测 vscreen-daemon 会挂，而 screencap 走命令守护时第一次约 12 秒、
    之后就 100~300 毫秒 —— 所以这条回退路是真能用的，不是摆设。
    """
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    # ① 守护 HTTP 直接取帧（最稳，见 _frame_direct 里的血泪）
    if _frame_direct(path):
        return True

    # ② node 客户端（老路，保留兜底；超时从 6 秒提到 20 秒，冷启动常 >6 秒）
    if not _vscreen_dead["v"]:
        r = _run(["node", VSCREEN, "-i", str(SLOT), "shot", path], 20)
        if r is not None and os.path.exists(path) and os.path.getsize(path) > 2000:
            return True
        _vscreen_dead["v"] = True
        print("[wxbridge] vscreen 抓帧不可用（守护挂了？）→ 本进程之后只用 screencap",
              flush=True)

    try:
        os.makedirs(WX_TMP, exist_ok=True)
    except Exception:
        pass
    tmp = os.path.join(WX_TMP, "shot.png")
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    _d = _require_did()
    if _d is None:
        return False                      # 读不到屏号就别截（截到物理屏是错的图）
    adb("screencap -d %d -p %s" % (_d, tmp), 150)
    if os.path.exists(tmp) and os.path.getsize(tmp) > 2000:
        try:
            shutil.copy(tmp, path)
            return True
        except Exception as exc:
            print("[wxbridge] screencap 拷贝失败：%r" % (exc,), flush=True)
    return False


def _ensure_screen_known():
    """还没截过图、但马上要用坐标时，问一下 vs-daemon 当前档位的尺寸。"""
    if _SCREEN_KNOWN[0]:
        return
    try:
        import json as _json
        import urllib.request as _u
        url = "http://127.0.0.1:%d/list" % int(os.environ.get("VSD_PORT", "18250"))
        with _u.urlopen(url, timeout=4) as r:
            data = _json.loads(r.read().decode("utf-8"))
        for item in data:
            if int(item.get("displayId", -1)) == int(did()):
                apply_screen_size(int(item.get("width") or 0))
                return
    except Exception:
        pass
    apply_screen_size(_SCREEN_BASE_W)      # 兜底：按 half 档算


def shot_wechat(img, tries=2):
    """截一张「确定是微信」的画面。

    坑：DSH 自己的 UI 和微信共用虚拟屏 15，每次工具调用都可能把微信挤下去
    （dumpsys 里 Display #15 会短暂变成空的，截出来是 DSH 界面）。
    所以截图前必须校验前台，失败就重新拉起来，别把 DSH 的界面当成微信读。
    """
    for i in range(tries):
        if wechat_front() and shot(img):
            return True
        ensure_wechat_front()
        time.sleep(1.0)
    ok = wechat_front() and shot(img)
    if not ok:
        print("[wxbridge] 截图失败：微信不在虚拟屏前台", flush=True)
    else:
        _note_frame_size(img)      # ★ 记下这一帧的尺寸 → 坐标/裁剪窗按档位缩放
    return ok


# ★ 2026-10-07：最近一次「输入框区域截图」的路径 —— 同一次发送里
#   验证输入框/定位发送键可以**共用同一张图**，别各截一次（每次截图 = 一次
#   抓帧 + jpg 编码，手机上约 0.4~1 秒；用户报「一次发送要 20 秒」就是这些攒的）。
_LAST_INPUT_SHOT = {"path": ""}


def input_stddev(img=None, shoot=True):
    """输入框区域灰度标准差：约 0.002=空，>0.05=有文字。

    ``shoot=False`` 表示 ``img`` 是一张**现成的图**，别再截一次（2026-10-07 加，
    给"一次发送里多个判据共用一张图"用）。
    """
    global _seq
    if img is None:
        _seq += 1
        img = "/tmp/wxbridge-%d.jpg" % _seq
        shoot = True
    if shoot and not shot_wechat(img):
        return None
    r = _run(["convert", img, "-crop", INPUT_CROP, "-colorspace", "Gray",
              "-format", "%[fx:standard_deviation]", "info:"], 20)
    if r is None:
        return None
    try:
        return float((r.stdout or "").strip())
    except Exception:
        return None


def _ime_now():
    """当前**系统默认**输入法（一次 adb，约 50ms）。读不到给空串。"""
    out = adb("settings get secure default_input_method", 15)
    v = (out or "").strip().splitlines()
    return v[0].strip() if v else ""


def _ime_shown():
    """物理屏（display 0）的输入法键盘现在是不是显示着。

    `dumpsys input_method` 里 display 0 的 section 排在最前面，取第一个
    `mInputShown` 就是物理屏的。⚠️ 它只说"键盘显示着"，不说**是谁要的** ——
    所以要在**借用输入法之前**把它记下来，归还后对比（见 `_hide_stray_keyboard`）。
    """
    out = adb("dumpsys input_method | grep -m1 mInputShown", 15) or ""
    return "mInputShown=true" in out


def _hide_stray_keyboard():
    """还回输入法时**统一把键盘收掉**（2026-10-09 续23，用户拍板）。

    用户报原话：「有时候我在桌面没用输入法呀，他也把输入法给我弹出来了」，
    随后定口径：**「机器人用完键盘给我还回来的时候统一收起来吧」** ——
    不再区分"这键盘是不是他正在用的"，还回来就是干净的、没有键盘的状态。

    病根（dumpsys 实证）：`ime set` 切回用户输入法时，系统会把"显示键盘"的请求
    交给新输入法；只要那一刻**还有输入连接**（虚拟屏微信的 MMEditText、或者 DSH
    自己的 WebView 在前台握着连接），讯飞就把键盘弹出来 —— 而它会被创建在
    **displayId=0（物理屏）** 上：
        InputMethod frame=[Rect(0,140 - 1264,2780)] displayId=0 mHasSurface=true
    借用期间看不见（DshIme 没有键盘视图），一切回去就冒出来。

    收法：ESC（keyevent 111）。2026-10-09 真机实测 `mInputShown` true → false；
    比 BACK(4) 安全 —— ESC 只让 IME 收键盘，不会给 App 传"返回"。
    调用点在 `ime_set()` 的归还分支里，所以**所有**归还路径（发送、切群、
    看护 `_ime_unstick` 超时替还）都统一收，不会有哪条漏掉。
    """
    try:
        if _ime_shown():
            adb("input keyevent 111", 20)
            print("[wxbridge] 还回键盘后收起（ESC）", flush=True)
            return True
    except Exception as exc:
        print("[wxbridge] 收键盘出错：%r" % (exc,), flush=True)
    return False


def _sweep_stray_keyboard(tops):
    """看护：把"虚拟屏微信留在物理屏上的键盘"收掉（2026-10-09 续23）。

    `_hide_stray_keyboard()` 只在**归还输入法的那一刻**收一次；可机器人切群/发送
    之后，虚拟屏微信的输入框仍握着输入连接，微信或 IMS 随时可能再发一次"显示键盘"
    请求 —— 2026-10-09 真机实测就是这样：归还时收掉了（日志有「还回键盘后收起」），
    切群刚结束键盘又出现在物理屏上：
        mServedView=MMEditText（虚拟屏微信）  而  InputMethod … displayId=0（物理屏）
    所以这里每 RECLAIM_TICK(10s) 兜一道，跟 `_ime_unstick` 一样排在
    `bot_running()` 之前（机器人没跑时留下的残键盘同样要收）。

    判据（两条同时成立才收）：
      · IME 键盘窗口在 **displayId=0（物理屏）**；
      · 输入连接是**微信输入框**（MMEditText —— 只存在于虚拟屏微信）。
    另外：物理屏前台**就是微信**时不动手 —— 那是用户自己把微信拿到物理屏在用。
    """
    try:
        if tops.get(0, "").startswith("com.tencent.mm"):
            return False
        if not _ime_shown():
            return False
        win = adb("dumpsys window windows | grep -m1 'InputMethod, frame='", 20) or ""
        if "displayId=0" not in win:
            return False
        served = adb("dumpsys input_method | grep -m1 'mServedView=.*MMEditText'", 15) or ""
        if "MMEditText" not in served:
            return False
        adb("input keyevent 111", 20)
        print("[wxbridge] 看护：收掉虚拟屏微信留在物理屏上的键盘（ESC）", flush=True)
        return True
    except Exception as exc:
        print("[wxbridge] 看护收键盘出错：%r" % (exc,), flush=True)
        return False


def ime_set(ime, verify=True):
    """切换**系统默认**输入法 —— ⚠️ 全局操作，会同时影响用户在物理屏的键盘。

    ★ 2026-10-09（续14）三处改动，都是被用户那句「我在桌面的时候仍然会卡输入法，
    机器人回复的时候桌面上仍然有一个输入法没按下去」逼出来的：

      ① **加日志**。原来这里一行都不打，用户报"卡输入法"时完全没法回溯是谁在
         什么时候切的（今天查这件事就吃了这个亏 —— 日志里只有 onnxruntime 的噪声）；
      ② **已经是它就不切**：省一次 adb，也少一次"系统弹输入法切换"的动静；
      ③ **切完校验**，没生效就补一刀。原来连 `ime set` 的返回都不看，
         `finally` 里那句"还回去"要是静默失败，用户的键盘就一直回不来。
    """
    cur = _ime_now()
    if cur and cur == ime:
        return True                      # 已经是它了，别白折腾（②）
    out = adb("ime set %s" % ime, 20)
    if not verify:
        return bool(out)
    # ③ 判据是**读回来的实际状态**，不是命令的返回 —— `ime set` 有两种失败：
    #    ① 命令本身失败（返回空）；② 返回 "selected" 却根本没换过去。
    #    只看 ① 的话，② 会让"发完消息键盘没回来"变成静默事故（用户报的就是它）。
    time.sleep(0.2)
    if _ime_now() != ime:
        print("[wxbridge] 输入法没切过去（%s，命令返回 %r）→ 补一次"
              % (ime, (out or "")[:40]), flush=True)
        adb("ime set %s" % ime, 20)
        time.sleep(0.2)
    ok = (_ime_now() == ime)
    print("[wxbridge] 输入法 %s → %s%s"        # ① 日志
          % (cur or "?", ime, "" if ok else "（**还是没成功**）"), flush=True)
    if not ok:
        return False
    if ime == IME_OURS:                  # 登记借用（看护超时没还就替它还）
        if cur and cur != IME_OURS:
            _IME_USER_LAST[0] = cur
            _IME_LEASE["back"] = cur
        _IME_LEASE["at"] = time.time()
    else:                                # 已经还回去了
        _IME_LEASE["back"] = ""
        _IME_LEASE["at"] = 0.0
        # ★ 2026-10-09（续23）：还回去之后顺手把"机器人自己弄出来的"键盘收掉 ——
        #   切回讯飞时，虚拟屏微信那个还挂着的"要求显示键盘"会让讯飞把键盘弹到
        #   物理屏上（用户：「我在桌面没用输入法，他也把输入法给我弹出来了」）。
        _hide_stray_keyboard()
    return True


def input_focused():
    """聊天输入框（微信 MMEditText）现在是不是真的聚焦了。

    ★ 2026-10-04 晚：这是「点输入框」这一步的判据 —— 老做法是点完就**再切一次
    输入法**才 commit，而那次 ime_set 会把焦点弄丢（面板「发到群里」报
    「发送接口没回 ok」的真凶，实测 mServedView 根本不在 MMEditText 上）。
    现在：点完先问系统焦点在谁身上，不在微信输入框就换个坐标重点，
    确认聚焦了才 commit。顺序固定为 ime_set → tap → commit。
    """
    try:
        out = adb("dumpsys input_method | grep 'mServedView=' "
                  "| grep -vi fallback | head -1", 20)
    except Exception:
        return True          # 查不到就不挡路，宁可照发
    return "MMEditText" in (out or "")


def _wait_input_focus(seconds=3.0):
    """点完输入框后**轮询**等它真的聚焦（见 `input_focused` 与调用处的说明）。

    2026-10-09 加：原来 tap 完睡 1.2 秒、查一次就判死，而实测输入连接从物理屏的
    陈旧窗口交到虚拟屏微信手上要 2 秒上下 —— 单次判定太早，于是
    「点完输入框还没聚焦」在日志里攒了 25 条。只在真的没聚焦时才多花时间：
    聚焦了第一次查就返回（约 0.3 秒）。
    """
    deadline = time.time() + max(0.0, float(seconds))
    while True:
        if input_focused():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.35)


def current_ime():
    """当前默认输入法；若它已经是我们的 DshIme，就返回**用户自己的**那个
    （见过的话），最后才退回写死的 `IME_FALLBACK`。

    ★ 2026-10-09（续14）：原来是"只要读到 DshIme 就返回 IME_FALLBACK"，
    而 IME_FALLBACK 写死的是讯飞。用户哪天换用搜狗/百度，"还回去"就会**换错**，
    他还得自己再切回来 —— 这也是他说的"卡输入法"的一份。现在优先还他真正在用的那个。
    """
    v = _ime_now()
    if not v or v == IME_OURS or "/" not in v:
        return _IME_USER_LAST[0] or IME_FALLBACK
    _IME_USER_LAST[0] = v          # 记下"用户的输入法"（还回时的首选）
    return v


def rebuild_ime():
    """把 DshIme 重新装一遍（disable → enable → set）。

    ★ 2026-10-04 晚实测：输入法服务会进"坏状态" —— `ime set` 明明返回 selected，
    可虚拟屏微信的输入框**怎么点都拿不到输入连接**（mServedView 永远停在窗口层
    的 DecorView，不是 MMEditText），commitText 全部落空。重建一次立刻恢复。
    """
    print("[wxbridge] 重建 DshIme（disable → enable → set）…", flush=True)
    adb("ime disable %s" % IME_OURS, 20)
    time.sleep(0.6)
    adb("ime enable %s" % IME_OURS, 20)
    time.sleep(0.6)
    adb("ime set %s" % IME_OURS, 20)
    time.sleep(1.0)


# 2026-10-05：是否允许桥接为了发消息去抢物理屏的输入焦点。
#   抢的办法只有 `input -d 0 keyevent 3`（HOME）把物理屏送回桌面，发完再
#   `am start` 把它拉回来 —— BACK/ESC 都踢不掉那个输入会话（见下面的
#   physical_input_taken）。反复这么折腾会把 **DSH 自己弄黑屏**（用户 2026-10-05
#   报「DSH 又黑屏打不开了，只能把 DSH 关掉」），代价远大于"这一条晚点发/不发"。
#   所以默认**关**：你正在用手机时，机器人这条安静跳过（日志写原因），绝不动你的屏。
#   想恢复旧行为：启动桥接时带上 `WXBRIDGE_TAKE_FOCUS=1`。
TAKE_FOCUS = os.environ.get("WXBRIDGE_TAKE_FOCUS", "0") == "1"

# ★ 2026-10-06 用户要求（原话：「让他主动抢一下吧，我不知道我在占用着，然后他也没消息。
#   要不就他要发消息的时候，我在占用，让他弹我一下。或者出个弹窗让我确认一下，
#   让他抢一下」）。
#   所以现在的规矩是：**授权抢（TAKE_FOCUS=1）≠ 闷头抢** ——
#   发现物理屏占着输入焦点时，先弹一条不受节流限制的通知告诉你"我要发了"，
#   再留 GRAB_GRACE 秒让你自己让位（收键盘 / 点页面空白 / 切走 / 锁屏都行）；
#   你不动，才按一次 HOME 抢屏，发完把原来的 App 切回来（restore_physical_app）。
#   设 0 = 不缓冲，发现占用立刻抢。
# ★ 2026-10-09 用户：「最好机器人能提前通知我，然后**直接抢**」——
#   2 → 1 秒：浮窗来得及显示（约 0.3~0.5 秒）就抢，不再给他两秒钟"让位"。
GRAB_GRACE = float(os.environ.get("WX_GRAB_GRACE", "1"))   # 6 → 2 → 1

# 2026-10-05：发送失败时，是否允许把 **DSH 自己的输入法**（com.dsh.clip/.DshIme）
#   `ime disable → enable → set` 重建一遍。
#   · 这招 2026-10-04 加的时候确实管用：输入法服务进坏状态后，虚拟屏微信的输入框
#     怎么点都拿不到输入连接，重建一次立刻恢复。
#   · 但它是 **DSH 的一部分**：用户 2026-10-05 报「被主动艾特的时候，DSH 就会白屏
#     或者黑屏，打不开新的对话」—— 时间点跟这里的重建次数（日志 `重建 DshIme`）吻合。
#     重建输入法服务会把 DSH 的界面/输入一起带崩，代价远大于"这条晚点发"。
#   · 所以默认**关**：发不出去就换个坐标再点，点不进就放弃这一条，下一轮再试。
#     想恢复旧行为：启动桥接时带上 `WXBRIDGE_REBUILD_IME=1`。
REBUILD_IME = os.environ.get("WXBRIDGE_REBUILD_IME", "0") == "1"


def screen_awake():
    """物理屏是不是亮着（``mWakefulness=Awake``）。

    返回 ``True``=亮着 / ``False``=熄屏或锁屏 / ``None``=**读不到**。
    调用方各自决定保守方向：要"别打扰用户"的（震动/抢屏）读不到就当作熄屏，
    要"停机器人"的读不到就**什么都不做**。

    ★ 2026-10-11 用户报「锁屏时发不出消息、一直在震动」查出来的病根：
      熄屏后系统**仍然**把微信记成物理屏前台（实测熄屏时
      ``topResumedActivity=com.tencent.mm/.ui.LauncherUI``，那是熄屏前的陈旧记录），
      于是 ``physical_wechat_in_use()`` 一路为真 → 机器人以为"你在用微信" →
      每条消息发送前都弹「要借一下屏」的预告（那条通知
      ``vibrate=default/defaults=ALL``，会震动+响铃）+ 抢屏；而锁屏下输入法根本
      拿不到输入连接（实测 ``mServedView=null``），发送必然失败，失败又挂补发 →
      再震一遍……用户听到的就是"一直在震动"。
    """
    try:
        out = adb("dumpsys power | grep mWakefulness=", 15) or ""
    except Exception:
        return None
    if "mWakefulness=" not in out:
        return None
    return "mWakefulness=Awake" in out


def physical_keyboard_shown():
    """物理屏的软键盘**是不是真的弹着**（`mInputShown=true`）。

    为什么要单独问这个：`mServedView` 会被**陈旧的输入会话**骗到 ——
    用户在手机浏览器里点过面板的输入框之后，那个 WebView 会一直挂着输入连接
    （App 退到后台也不松手），于是"他根本没在用手机"被读成"他占着键盘"。
    键盘弹着才是真的有人在打字。

    返回 ``True`` / ``False``；**读不到返回 None**（调用方按"不知道"保守处理）。
    """
    try:
        out = adb("dumpsys input_method | grep mInputShown | head -1", 20) or ""
    except Exception:
        return None
    if "mInputShown" not in out:
        return None
    return "mInputShown=true" in out.replace(" ", "")


def physical_input_taken():
    """输入焦点是不是被**物理屏**占着（虚拟屏因此拿不到输入连接）。

    物理屏的浏览器 WebView 一旦聚焦过输入框，输入会话就一直挂在它身上，
    虚拟屏微信怎么点都点不到焦点。BACK / ESC / TAB 都踢不掉，只有 HOME 有效。
    """
    try:
        out = adb("dumpsys input_method | grep 'mServedView=' "
                  "| grep -vi fallback | head -1", 20) or ""
    except Exception:
        out = ""
    v = out.strip()
    if not v:
        # ★ 2026-10-10：**读不到 ≠ 没人用**。以前这里直接返回 False（放行），
        #   而 adb 一抖就读不到 —— preflight 于是判「你让位了」，接着切输入法/
        #   点输入框，把正在打字的人的键盘顶掉（用户原话：「不是每 15s，
        #   我打字都打不完就抢」；wxbridge.log 里紧挨着的两行就是证据：
        #   「adb 断了…改等你让位」→「你让位了（等了 1 秒）」）。
        #   现在读不到一律**保守当作有人在用**。
        #   （真没人用时读到的是 "mServedView=null"，由下面那段正常处理。）
        print("[wxbridge] 读不到 mServedView（adb 抖了）→ 保守按「你在用手机」处理",
              flush=True)
        return True
    if "MMEditText" in v:
        return False
    # ★ 2026-10-08：`mServedView=null` 是"**没有任何 view 持有输入焦点**"，
    #   本该放行；旧判据看到"非空且不是 MMEditText"就当成了"物理屏占着焦点"，
    #   于是机器人在没人用手机时也干等 60 秒然后放弃发送（实测 16:33 那次
    #   基线就是 mServedView=null）。这正是"机器人平时不回消息"的一半原因。
    if v.endswith("=null"):
        return False
    if "636,1400" in v:                 # 虚拟屏自己的窗口（636x1400）不算
        return False
    # ★ 2026-10-09 用户报「**我没动还是发不出去**」—— 病根在这里：
    #   `mServedView` 还粘着**浏览器的 WebView**（他在手机浏览器里点过面板的输入框，
    #   实测 `android.webkit.WebView ... 1264x2584`），而人明明在桌面、键盘也没弹
    #   （`mInputShown=false`）。旧判据只看 mServedView → 一直认为"你占着键盘"
    #   → 每轮干等 8 秒然后放弃（浮窗就写着「你一直占着键盘」）。
    #   这种**陈旧会话不影响虚拟屏输入**（同日实测：粘着的时候照样把图发出去了），
    #   所以再看一眼键盘是不是真的弹着 —— 弹着才算"他在用手机"。
    kb = physical_keyboard_shown()
    if kb is None:
        return True        # 读不到就保守：宁可慢一步，也别在人家打字时硬插
    return bool(kb)


# ---- 抢屏闸门：★ 2026-10-12 **整块删除** ----
#   删掉的是 `GRAB_MIN_GAP` / `DSH_PKG` / `_GRAB_LAST` / `grab_allowed_now()` ——
#   它们的调用方只有彼此：`preflight_yield()` 从 2026-10-10 晚起就**只看两个硬前提**
#   （用户显式关掉抢屏 / adb 断了），这几道闸门早就不参与判断了。留着只会让人
#   以为它们在起作用（用户 2026-10-12 问「不参与判断的没用的可以删」→ 删）。
#   历史：2026-10-08 拍的「物理屏是 DSH 就不抢 + 其它应用 20 秒限流」，
#   2026-10-09 用户说「已经有点习惯抢键盘了……直接抢」之后逐步废弃。


def release_physical_input():
    """把物理屏送回桌面，逼它交出输入焦点；返回原前台，稍后好切回来。"""
    prev = ""
    try:
        prev = top_activity_of(0) or ""
    except Exception:
        prev = ""
    print("[wxbridge] 物理屏占着输入焦点（%s），先送它回桌面松手"
          % (prev or "?"), flush=True)
    adb("input -d 0 keyevent 3", 15)
    time.sleep(1.2)
    # ★ 2026-10-08 晚（实测）：按 HOME 只对「前台应用占着输入焦点」有效。
    #   用户**在桌面挂机**时（物理屏前台 = com.android.launcher），焦点挂在桌面的
    #   搜索框上，HOME 按了等于没按 —— 抢屏白抢，紧接着「/send 第1次：点完输入框
    #   还没聚焦」失败、整条重试一次（实测这一下吃掉 15.39 秒）。
    #   补一个 BACK：收起桌面搜索框 / 关掉软键盘，真正把输入会话还回来。
    if physical_input_taken():
        print("[wxbridge] 按 HOME 后仍被占（多半是桌面搜索框）→ 补一个 BACK", flush=True)
        adb("input -d 0 keyevent 4", 15)
        time.sleep(0.8)
    return prev


# ---- 让位提醒（2026-10-05 用户要求：机器人要回消息、你在用手机时，弹个通知）----
# 想彻底关掉提醒：起桥接时带 `WXBRIDGE_NOTIFY=0`。
NOTIFY_ON = os.environ.get("WXBRIDGE_NOTIFY", "1") == "1"
_NOTIFY_LAST = 0.0
NOTIFY_MIN_GAP = 60.0        # 秒：两条提醒之间至少隔这么久，别把通知栏刷爆
# ★ 2026-10-12（用户：「通知栏这个抢屏可以关了」）：抢屏预告**不再往通知栏发**
#   「微信机器人 · 要借一下屏」（那条是带横幅+响铃+连震三下的高优先级通知）。
#   浮窗预告 + 一次震动照旧。想恢复：起桥接时带 `WXBRIDGE_ALARM=1`。
# ★ 2026-10-14（用户要求「借屏只留浮窗，开始/结束各一条」）：**这个开关彻底不用了** ——
#   桥接侧再也没有 `_hud_alarm()` 调用点（调用已注释在 `notify_user` 里）。
#   常量与函数都保留：一是回滚方便，二是 `test_preflight.py` 会断言这行存在。
#   借屏提示请统一走 `_hud_grab_start()` / `_hud_released()`。
ALARM_ON = os.environ.get("WXBRIDGE_ALARM", "0") == "1"

def _hud_notice(text, seconds=8, title="DSH 微信机器人", detail=None):
    """在**屏幕上浮一条**提示（抢屏 / 让位预告）。★ 2026-10-07 用户选的方案 B。

    为什么不用系统通知就够了：shell 发的通知只能走 `shell_cmd` 那条**静默渠道**
    （只进通知栏、不弹横幅、不响不震，`cmd notification post` 也没有 importance /
    声音 / 震动的参数，`cmd vibrator` 在本机干脆不存在）。实测 2026-10-07：
    桥接日志写着「已弹让位提醒」，而用户全屏玩逆水寒时**完全没察觉** ——
    他只知道自己被抢屏了。

    浮窗能直接盖在全屏游戏上，显示几秒自己淡出，正好当"我要动手了"的预告。
    ⚠️ 异步起、**绝不 wait** —— 别拖慢发送流程；失败就当没有，不影响发消息。
    """
    try:
        exe = shutil.which("hud") or "/usr/local/bin/hud"
        argv = [exe, "--linger", str(int(seconds)), "-t", str(title)]
        # ⚠️ detail 一定要**显式传**（哪怕是空串）：hud 服务不清字段的话，
        #    上一次浮窗的细节会留在上面（实测看到过别人的「用户要求：」串进来）。
        argv += ["-d", "" if detail is None else str(detail)]
        argv.append(str(text))
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _hud_buzz():
    """让手机震一下。

    ★ 2026-10-09 用户要求：抢键盘这件事必须**有头有尾**，而且他得能感知到。
      他原话「机器人抢键盘的时候还是没有提醒，主要是这一点我不知道抢到什么时候
      才结束」—— 系统通知走的是 shell_cmd **静默渠道**（不弹横幅、不响不震、
      连屏都不亮），他只顾着打字时根本看不到；**震动是唯一"不看不听也能感知"
      的通道**。

    ⚠️ 震动只能由 hud App 自己做（`cmd vibrator` 在本机根本不存在），
       见 HudService.vibrate()。异步起、绝不 wait，异常一律吞掉 ——
       提示失败绝不能影响发消息。

    ★ 2026-10-13（用户要求，原话「机器人抢屏抢之前震的我一下，抢完成了又震了我
      一下。取消抢屏震动吧」）：**抢屏这套流程默认不再震动**。
      震动是 2026-10-09 为「让他知道抢到什么时候结束」加的，但他现在觉得这动静
      比信息量更烦人 —— **提示本身全都保留**（浮窗 + 抢屏横幅通知），只是不震。
      四个调用点（抢前预告 / 发完归还 / 发图预告 / 切群归还）共用这一个出口，
      所以开关放这儿，一处生效、想开回来也只改一处：
      启动桥接时设 `WX_GRAB_BUZZ=1`（或把下面的默认值 "0" 改掉）。
    """
    if os.environ.get("WX_GRAB_BUZZ", "0").strip().lower() not in (
            "1", "true", "yes", "on"):
        return False
    try:
        exe = shutil.which("hud") or "/usr/local/bin/hud"
        subprocess.Popen([exe, "--buzz"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _hud_alarm(text, detail=""):
    """发一条**能弹横幅**的抢屏提醒（浮窗 App 的高优先级通知）+ 震动。

    ★ 2026-10-10（用户报障）：「他在那抢微信，他不告诉我，他没有通知，没有通知
      我就会乱点。」—— 原来 `notify_user` 里那句 `adb shell cmd notification post`
      是 **shell_cmd 静默渠道**（实测 importance=1、mShowBanner=false）：dumpsys 里
      看得见，屏幕上一片安静，横幅不弹、不响、连屏都不亮。

    现在改由浮窗 App 自己发（`HudService.alert`，走它自己的渠道 dsh_hud_alarm）——
    实测 importance=4、mShowBanner=true、mIsInterruptive=true：**会弹横幅、会响、
    会连震三下**，而且一直挂到 `_hud_alarm_off()` 才撤（抢完才收）。

    异步起、绝不 wait、异常一律吞掉 —— 提示失败绝不能影响发消息。

    ⚠️ ★ 2026-10-14：**桥接侧已不再调用本函数**（借屏只走浮窗了，见 `_hud_grab_start`
    / `_hud_released`）。hud App 的 `/alarm` 接口本身保留，人工仍可 `hud --alarm` 用。
    """
    try:
        exe = shutil.which("hud") or "/usr/local/bin/hud"
        subprocess.Popen([exe, "--alarm", "-t", "微信机器人 · 要借一下屏",
                          "-d", str(text)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _hud_alarm_off():
    """撤掉那条抢屏提醒（抢完调 —— 有头有尾，别让它一直挂在他通知栏里）。

    ★ 2026-10-14：桥接侧**已不再调用**（借屏提示统一走浮窗，见下面 `_hud_released`）。
      函数保留是为了回滚方便，别删。
    """
    try:
        exe = shutil.which("hud") or "/usr/local/bin/hud"
        subprocess.Popen([exe, "--alarmoff"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ------------------------------------------------------ 借屏提示：统一成两个出口
# ★ 2026-10-14（用户要求，原话「微信机器人借屏好像也有两种通知方式，其中一种是
#   键盘已经还给我了……不能统一一下吗？还是说这是两种路径？」）：
#   借屏这套提示原来散在 6 处、4 个标题、2 条渠道（浮窗 + 通知栏横幅），
#   光「我还给你了」就有「抢屏结束 / 借屏结束 / 输入法已还回」三种说法 ——
#   用户会以为那是三件不同的事。
#   现在收敛成**两个出口，全部只走浮窗**（标题正文固定，只有细节行按原因变）：
#     · `_hud_grab_start()` —— **开始**：预告和「正在借」共用同一个标题，
#       浮窗本来就是原地更新内容，所以视觉上始终是**同一条**；
#     · `_hud_released()`    —— **结束**：发完消息 / 切完群 / 补还输入法三个调用点共用。
#   ⚠️ **通知栏那一路（`_hud_alarm` / `hud --alarm`）桥接侧已不再调用** ——
#   hud App 的 `/alarm` 接口本身保留（人工仍然可以 `hud --alarm` 手动发一条），
#   想恢复「抢屏开始弹横幅」见 `_hud_alarm` 的说明。
GRAB_TITLE = "🔒 微信机器人 · 借一下屏"
RELEASED_TITLE = "✅ 借屏结束 · 可以拿手机了"


def _hud_grab_start(text, detail=None, seconds=8):
    """借屏**开始**的唯一出口（预告 / 真正动手两次调用都是它）。"""
    return _hud_notice(text, seconds=seconds, title=GRAB_TITLE, detail=detail)


def _hud_released(reason="发消息", detail=None):
    """借屏**结束**的唯一出口 —— 标题正文固定，只有细节行按原因不同。

    ``reason`` 只用来拼默认的细节行（如「发消息」「切群」）；要更具体的说明
    就直接传 ``detail``。收尾顺带 `_hud_buzz()`（★ 默认已关，见那边的说明）。
    """
    try:
        _hud_notice("键盘已经还给你了", seconds=6, title=RELEASED_TITLE,
                    detail=detail or ("刚才是机器人%s临时借了一下" % reason))
    except Exception:
        pass
    _hud_buzz()


# ★ 2026-10-12：失败类弹窗合并。同一个原因在 FAIL_POPUP_DEDUP 秒内只弹一次。
#   起因：补发队列 / 下一轮重试会把同一条消息反复送进 send_text，每次失败都浮一条
#   「这条没发出去」；悬浮小窗拦截更糟 —— 先弹一条「已拦下」，返回后 send_text 又按
#   返回值再弹一条，一次失败两条（用户在打字时被连着打断）。
#   ⚠️ 只管**显示**：失败照样 return False、照样挂补发队列、拒发判断一个字没动。
#   key = 标题 + 去掉数字和括号内容的原因文字（OCR 残留、窗口个数会变，不该让它们破坏合并）。
FAIL_POPUP_DEDUP = float(os.environ.get("WXBRIDGE_FAIL_POPUP_DEDUP", "45"))
_FAIL_POPUP_SEEN = {}
_FAIL_POPUP_LOCK = threading.Lock()
_FAIL_POPPED = [False]       # 本次 send_text 里内层是不是已经弹过（外层就别再弹第二条）


def _hud_fail_notice(text, seconds=8, title="微信机器人 · 这条没发出去", detail=None):
    """失败类浮窗的统一出口：窗口期内同一原因只弹一次。返回是否真的弹了。"""
    try:
        key = re.sub(r"\d+", "#", re.sub(r"[（(][^）)]*[）)]", "", "%s|%s" % (title, text)))
        now = time.time()
        with _FAIL_POPUP_LOCK:
            last = _FAIL_POPUP_SEEN.get(key, 0.0)
            if FAIL_POPUP_DEDUP > 0 and 0 <= now - last < FAIL_POPUP_DEDUP:
                _FAIL_POPPED[0] = True        # 刚弹过 = 也算"弹过了"，外层别再补一条
                print("[wxbridge] 同样的失败提示 %.0f 秒前刚弹过，这次不再弹：%s"
                      % (now - last, str(text)[:30]), flush=True)
                return False
            _FAIL_POPUP_SEEN[key] = now
            if len(_FAIL_POPUP_SEEN) > 50:                      # 不让表无限长
                for k in sorted(_FAIL_POPUP_SEEN, key=_FAIL_POPUP_SEEN.get)[:25]:
                    _FAIL_POPUP_SEEN.pop(k, None)
        _FAIL_POPPED[0] = True
        return _hud_notice(text, seconds=seconds, title=title, detail=detail)
    except Exception:
        return False


def _hud_sent(content, kind="已发"):
    """发送**成功**后浮一条，把"发了什么"显示出来（2026-10-07 用户要求）。

    以前只有"要抢屏了"才浮窗，发成功反而没动静 —— 用户想知道机器人到底发了啥。
    超长截断（浮窗是给人扫一眼的，不是阅读器）。
    """
    try:
        t = str(content or "").strip().replace("\n", " ")
        if len(t) > 42:
            t = t[:42] + "…"
        # ★ 2026-10-08 晚（用户要求）：把「已发出」放标题位（最醒目），
        #   而不是塞在细节行 —— 用户要的就是"发完了告诉我"这一眼。
        _hud_notice(t, seconds=3, title="微信机器人 · 已发出", detail=kind)
    except Exception:
        pass


def notify_user(text, title="DSH 微信机器人", force=False):
    """给手机发一条系统通知。

    场景：机器人要往群里发消息，但**物理屏占着输入焦点**（你正在用手机），
    而按设置它又不去抢（抢会把 DSH 搞白屏/黑屏）—— 这条就发不出去了。
    与其默默失败，不如弹一条通知让你知道"让一下位它就能发"。

    节流 60 秒：群里连着几条消息只提醒一次。任何异常都吞掉，绝不影响发送流程。
    ``force=True`` 绕过节流 —— 2026-10-06 用户要求「要发消息时我在占用，弹我一下」，
    这个是"马上要抢你屏了"的预告，不能因为 60 秒节流被吞掉。

    ⚠️ ★ 2026-10-14：**桥接侧已无调用点**（原来的两处抢屏/发图预告都改成
    `_hud_grab_start()` 了）。留着是为了回滚方便，别再往里加新调用 ——
    借屏提示请统一走 `_hud_grab_start()` / `_hud_released()`。
    """
    global _NOTIFY_LAST
    if not NOTIFY_ON:
        return False
    now = time.time()
    if not force and now - _NOTIFY_LAST < NOTIFY_MIN_GAP:
        return False
    _NOTIFY_LAST = now
    try:
        t = str(title).replace("'", "")
        m = str(text).replace("'", "")
        # ★ 2026-10-10（用户报障「他在那抢微信，他不告诉我，他没有通知，没有通知
        #   我就会乱点」）：**抢屏预告改走浮窗 App 自己的高优先级通知**，会弹横幅。
        #   非抢屏的普通提醒（force=False）仍旧走 shell —— 它本来就只是"顺嘴提一句"，
        #   不值得每 60 秒弹一条横幅打断他（那条静默通知他看不到也无所谓）。
        if force:
            # ★ 2026-10-14（用户要求「借屏只留浮窗」）：这里**不再发通知栏横幅** ——
            #   原来 ALARM_ON 开着会调 `_hud_alarm(m)`（高优先级、会弹横幅）。
            #   抢屏预告现在统一走 `_hud_grab_start()`（只浮窗）。想恢复：
            #   把下面这行放开 + 起桥接时带 `WXBRIDGE_ALARM=1`。
            # if ALARM_ON:
            #     _hud_alarm(m)
            pass
        else:
            adb("shell cmd notification post -S bigtext -t '%s' dshbot '%s'" % (t, m), 15)
        print("[wxbridge] 已弹让位提醒：%s" % m, flush=True)
        # ★ 2026-10-07（用户选方案 B）：**再浮一条到屏幕上** —— 系统通知是静默渠道，
        #   用户在全屏游戏里看不到；浮窗能盖上去，几秒后自己淡出。
        #   多显示 2 秒，别在它刚动手时浮窗就没了。
        # ★ 2026-10-08 晚（用户要求）：让位提醒的浮窗必须**活到用户让位**为止。
        #   原来只浮 4 秒（GRAB_GRACE+2）—— 用户看到 → 读懂 → 退出 DSH 根本
        #   来不及，浮窗早没了，他还是不知道为什么机器人不回消息。按时长分场景：
        #     · 抢屏预告（force）：抢之前那几秒就够；
        #     · 让位提醒：要盖住整个等待窗口（PHYS_WAIT_SECONDS），再多留 5 秒。
        _hud_secs = int(max(6, GRAB_GRACE + 2)) if force else int(PHYS_WAIT_SECONDS + 5)
        _hud_notice(m, seconds=_hud_secs, title=title,
                    detail=("你不动它就自己抢一下屏（界面会切后台，发完切回来）"
                            if force else
                            "退出 DSH / 收起键盘 / 点一下空白 / 锁屏都行，你让开它立刻发"))
        return True
    except Exception as exc:
        print("[wxbridge] 让位提醒没发出去：%r" % (exc,), flush=True)
        return False


def restore_physical_app(prev):
    """把用户原来在前台的 App 切回来（release_physical_input 的收尾）。"""
    if not prev or "launcher" in prev.lower():
        return
    try:
        # ★ 2026-10-09：加 --activity-reorder-to-front —— 明确要求"把已经在栈里的
        #   那个任务提到前台"，而不是新建/重建 Activity。用户拍板"DSH 也抢"之后，
        #   这一步是减少白屏的关键（白屏＝WebView 被重建）。
        adb("am start -n %s --activity-reorder-to-front" % prev, 20)
        time.sleep(0.8)
        print("[wxbridge] 已把用户切回 %s" % prev, flush=True)
    except Exception as exc:
        print("[wxbridge] 切回 %s 失败：%r" % (prev, exc), flush=True)


def commit(text, do_clear=True):
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    extra = "--es clear '1' " if do_clear else ""
    adb("am broadcast -a com.dsh.clip.COMMIT %s--es text64 '%s'" % (extra, b64),
        timeout=20)


# ★ 2026-10-08：rec 事件的「新鲜度」上限（秒）。见 wechat_recording 里的说明。
_REC_STALE = 30.0


def _rec_event_age(line):
    """从 '10-08 16:43:53:594 rec update ...' 算出这条 rec 事件距今多少秒。

    解析不了就返回 None（调用方按「新鲜」处理，宁严勿松）。
    """
    m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}):(\d{3})", (line or "").strip())
    if not m:
        return None
    mo, dd, hh, mi, ss, ms = (int(x) for x in m.groups())
    try:
        now = time.localtime()
        t = time.mktime((now.tm_year, mo, dd, hh, mi, ss, 0, 0, -1)) + ms / 1000.0
    except Exception:
        return None
    return time.time() - t


def wechat_recording():
    """微信此刻是不是正在开麦克风（语音输入 / 按住说话）。

    ★ 2026-10-08 事故（这次钉死了）：
      机器人在虚拟屏微信上 tap"输入框"时，界面状态一变就会误触
      **微信自己的语音输入** —— 微信开始录音（右上角麦克风绿点亮起），
      录到的正是用户**外放**的抖音新闻直播；约 1.4 秒后机器人点"发送"，
      那一下实际只是**停止录音**，微信把语音转成文字塞进输入框。
      下一轮机器人再来，"框里有字" → 点发送 → **新闻原样进群**。

    实测证据（dumpsys audio 录音活动，与群里 5 条垃圾一一对应）：
      14:37:29 / 14:37:48 / 14:39:07 / 14:39:26 / 14:41:18
      pack:com.tencent.mm —— 五次开麦 ↔ 五条消息。

    所以发送前必须先问一句"微信是不是正在录音"。是 → 一律不发。
    """
    out = adb("dumpsys audio", 20) or ""
    last = ""
    for line in out.splitlines():
        if " rec " in line:
            last = line.strip()
    if not last or "com.tencent.mm" not in last:
        return (False, last)
    if not (" rec start " in last or " rec update " in last):
        return (False, last)
    # ★ 2026-10-08 追加：**新鲜度**判断（回答用户那句「我切回打字很容易，你为什么切不掉」）。
    #   微信放掉 MIC 时**不一定写 rec stop**：实测用户手动「切回打字」之后，dumpsys 里
    #   长时间只留一条 rec update（16:43:53 开麦 → 16:44:05 才 stop）。
    #   不看时间的话，最后那条 update 会让本函数**永远返回 True** ——
    #   于是机器人此后**每一条都拒发**，表现成「机器人不回消息了」。
    #   超过 _REC_STALE 秒还没有 stop 就当它已经结束（真正的兜底是发送前的
    #   **内容核对**，所以这里放宽不会把别人的话放进群）。
    age = _rec_event_age(last)
    if age is not None and age > _REC_STALE:
        return (False, "%s（陈旧 %.0fs，按已停处理）" % (last, age))
    return (True, last)


def in_voice_mode(img=None, shoot=True):
    """微信输入栏是不是停在「按住说话」的语音模式。

    ★ 2026-10-08：语音模式下输入框整块变成"按住 说话"按钮 —— 机器人 tap
      输入框坐标就等于**按住麦克风**，一样会开麦（实测）。所以动手之前
      必须先问一句：是语音模式就先切回键盘模式。
    判据：OCR 输入栏那块区域，读到「按住」或「说话」。实测语音模式读到
    ['说话']、键盘模式读到 []（麦克风图标不是文字，不会误判）。
    """
    if wxreader is None:
        return False
    global _seq
    if img is None:
        _seq += 1
        img = "/tmp/wxbridge-voicemode-%d.jpg" % _seq
        shoot = True
    try:
        if shoot and not shot_wechat(img):
            return False
        r = _run(["convert", img, "-crop", VOICE_MODE_CROP,
                  "/tmp/wxbridge-voicemode-crop.jpg"], 20)
        if r is None:
            return False
        res = wxreader.ocr_call("/tmp/wxbridge-voicemode-crop.jpg")
        t = "".join(str(l[1]).strip() for l in (res[0] if res and res[0] else []))
        return ("按住" in t) or ("说话" in t)
    except Exception as exc:
        print("[wxbridge] 语音模式检测异常（当作键盘模式继续）：%r" % (exc,), flush=True)
        return False


def leave_voice_mode():
    """停在「按住说话」就点切换按钮回键盘模式。返回 True=确认已在键盘模式。

    最多点两次（切换按钮是 toggle，多点会把模式又切回去）。
    """
    for _i in range(2):
        if not in_voice_mode():
            return True
        print("[wxbridge] 微信在「按住说话」模式 → 点切换按钮回键盘模式", flush=True)
        tap(*VOICE_TOGGLE_POS)
        time.sleep(0.9)
    return not in_voice_mode()


def stop_wechat_recording():
    """把微信正在进行的录音停掉。返回 True=确认已停。

    ★ 2026-10-08 实测（很重要，别再改回 BACK）：
      · `input keyevent 4`（BACK）**停不掉**录音 —— 按完 BACK 1.2 秒后
        dumpsys 里仍然只有 `rec update`、没有 `rec stop`，微信一直在录；
      · 点输入栏最左的「语音/键盘」切换按钮 (75,1310)：**键盘模式下有效**
        （16:31:14 实测立刻 rec stop），但**「按住说话」那种录音停不掉** ——
        16:43:53 开麦后连点两次切换按钮，录音硬是又录了 12 秒才停；
      · 所以这里加**硬兜底**：把虚拟屏微信切到后台再拉回来（系统级中断录音）。
      · 副作用：切换按钮会把输入模式换到另一边，停完要用 leave_voice_mode()
        把"按住说话"切回键盘模式。
    """
    _rec, _line = wechat_recording()
    if not _rec:
        leave_voice_mode()
        return True
    # ① 先试点「语音/键盘」切换按钮（最轻）
    print("[wxbridge] 停录音①：点语音/键盘切换按钮", flush=True)
    tap(*VOICE_TOGGLE_POS)
    time.sleep(1.0)
    _rec, _line = wechat_recording()
    if _rec:
        # ② 硬兜底：切后台再拉回（对"按住说话"这种录音有效）
        print("[wxbridge] 停录音②：切换按钮没停掉 → 把微信切后台再拉回", flush=True)
        _d = _require_did()
        if _d is not None:
            adb("input -d %d keyevent 3" % _d, 15)
        time.sleep(1.5)
        try:
            ensure_wechat_front()
        except Exception as exc:
            print("[wxbridge] 拉回微信失败：%r" % (exc,), flush=True)
        time.sleep(1.0)
        _rec, _line = wechat_recording()
    if _rec:
        # ③ 终极手段：重启微信进程。实测前两招都压不住「按住说话」——
        #    16:44:56 开麦后，切换按钮 / 切后台 / 切输入法 / 点聊天区全试过，
        #    录了 3 分钟都没停；`am force-stop com.tencent.mm` 一下立刻 rec release。
        if physical_wechat_in_use():
            print("[wxbridge] ‼️ 录音停不掉，但物理屏正在用微信 —— 不重启微信，本次不发送",
                  flush=True)
            leave_voice_mode()
            return False
        print("[wxbridge] 停录音③：重启微信进程（最后手段）", flush=True)
        adb("am force-stop com.tencent.mm", 20)
        time.sleep(2.5)
        _rec, _line = wechat_recording()
        _d = _require_did()
        if _d is not None:
            adb("am start --display %d -n com.tencent.mm/.ui.LauncherUI" % _d, 25)
        time.sleep(5.0)
        try:
            ensure_wechat_front()
            open_chat(_default_chat())
        except Exception as exc:
            print("[wxbridge] 重启微信后恢复会话失败：%r" % (exc,), flush=True)
        _rec, _line = wechat_recording()
    if _rec:
        print("[wxbridge] ‼️ 录音仍停不掉（%s）—— 本次绝不发送" % _line, flush=True)
    leave_voice_mode()
    return not _rec


def _clear_input_box():
    """把微信输入框清空 —— 只发 clear 广播，不带任何文本。

    ★ 2026-10-07 新增（用户报「群里冒出一句无意义的『胜利长路顶』」）：
    `/send` 输入失败**放弃**时原来直接 return，**没有清输入框**。输入框里
    一旦串进来过内容（输入法 / 剪贴板环境很乱，用户当时还在玩逆水寒），
    它就会一直留在那儿 —— 之后**任何人在那个输入框上按一下发送/回车**
    （比如在别的 App 里发消息时被串过来），这段残留就直接**发到群里**，
    而机器人毫不知情（它以为"这条没发"，`sent.jsonl` 里也没有记录）。

    所以：**放弃/失败时一律清空**，宁可多发一次广播，也不给后面留雷。
    清不掉也不抛异常（不该影响主流程）。
    """
    for _i in range(2):
        try:
            commit("", do_clear=True)
        except Exception as exc:
            print("[wxbridge] 清空输入框失败：%r" % (exc,), flush=True)
            return False
        time.sleep(0.7)
        try:
            left = _ocr_input_box(wide=False)   # ★ 只看输入框：用大窗口会读到消息列表，误判"还有残留"而反复清空
        except Exception:
            left = ""
        if not left:
            print("[wxbridge] 已清空输入框（复读确认无残留）", flush=True)
            return True
        print("[wxbridge] ⚠️ 输入框仍有残留 %r（第 %d 次清空）" % (left[:20], _i + 1), flush=True)
    # ★ 2026-10-08：实测输入框会长期残留（当天框里一直躺着一个「查」字），
    #   说明"发一次 clear 就算清干净"这个假设不成立。清不掉必须喊出来 ——
    #   残留会被下一次发送原样送进群。
    print("[wxbridge] ‼️ 输入框清不干净（读到 %r）—— 下次发送前务必当心" % (left[:20],), flush=True)
    return False


def ensure_wechat_front(wait=1.6):
    """把微信拉到虚拟屏前台。DSH 会抢前台，所以每次操作前都要调。

    已经在前台就直接返回 —— 省掉 1.6 秒 sleep 和一次 am start。
    **用户正在物理屏用微信时绝对不拉**（见 physical_wechat_in_use 的说明）；
    用户**刚把微信切走**也先缓 ``RECLAIM_IDLE`` 秒（2026-10-04 要求 10 秒，
    ★ 2026-10-14 用户改口要 5 秒）——
    否则他切出去看一眼别的 App 再切回来，微信已经被机器人抢没了。
    （这里读屏/发送都会调，所以这道缓冲等于「谁来都别急着抢」。）
    """
    global _phys_wx_seen
    if wechat_front():
        return
    if physical_wechat_in_use():
        _phys_wx_seen = time.time()
        print("[wxbridge] 物理屏正在用微信，不抢（抢了用户手上的微信会凭空消失）",
              flush=True)
        return
    left = RECLAIM_IDLE - (time.time() - _phys_wx_seen)
    if 0 < left < RECLAIM_IDLE:
        print("[wxbridge] 用户刚把微信切走，再缓 %.0f 秒收回" % left, flush=True)
        return
    _d = _require_did()
    if _d is not None:
        # ★ 2026-10-10：跨屏用 move-stack（不动 Activity 栈）；同屏但不在前台
        #   或 task 不存在时才退回 am start。
        _move_wx_task_to_display(_d)
        time.sleep(0.6)
        if not wechat_front():
            adb("am start --display %d -n com.tencent.mm/.ui.LauncherUI" % _d, 25)
    time.sleep(wait)


def top_activity_of(dpy):
    """指定 display 当前 topResumedActivity 的组件名。

    设备端先 grep 再传回来，比把整份 dumpsys 拉回本地快得多。
    """
    out = adb('dumpsys activity activities | grep -E "Display #|topResumedActivity"', 25)
    want = "Display #%d" % dpy
    in_scope = False
    for ln in (out or "").splitlines():
        t = ln.strip()
        if t.startswith("Display #"):
            in_scope = t.startswith(want)
            continue
        if in_scope and "topResumedActivity=" in t:
            m = re.search(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", t)
            return m.group(1) if m else ""
    return ""


def screen_activity():
    """虚拟屏（微信跑的那块）当前前台的组件名。"""
    _d = _require_did()
    return top_activity_of(_d) if _d is not None else ""


def physical_wechat_in_use():
    """物理屏（display 0）前台是不是微信。

    ⚠️ 微信**只有一个实例**：用 `am start --display 15 --windowingMode 5` 把它拉到
    虚拟屏，就等于从用户手里**把它拿走** —— 用户那边表现成「微信老是自己回桌面」，
    而且任务被粘上 freeform 之后还可能带着怪窗口尺寸回来。
    所以用户正在物理屏用微信时，宁可这一轮什么都不做，也绝不抢。
    """
    return top_activity_of(0).startswith("com.tencent.mm")


def wechat_front():
    """当前虚拟屏前台是不是微信。"""
    return screen_activity().startswith("com.tencent.mm")


def physical_ime_visible():
    """物理屏上的软键盘是不是弹着（≈ 用户正在打字）。

    实测（Android 16 / OPPO）看 server 侧的 ``mInputShown``：
    键盘收起时是 false、弹出来才是 true。
    ⚠️ 别用下面 InputMethodService 段的 ``mIsInputViewShown`` —— 键盘收起之后它
    可能仍然是 true（只留下上次算出来的 touchableRegion），会一直误判成"在打字"。
    ★ 2026-10-09 补一个大坑：``mInputShown`` 是**全局**的（输入法服务只有一个），
    所以**虚拟屏微信自己弹的键盘也算数** —— 而机器人发完消息就会点一下输入框，
    键盘随即弹起。实测：发图后点一下输入框夺回输入连接（`_reclaim_input_focus`），
    紧接着的配文 `/send` 就被判成「你在物理屏上打字」，白等 8 秒后放弃
    （日志：`物理屏占着输入焦点（你在用手机）… 改等你让位`）。
    区分办法：看输入连接**挂在谁身上** —— 挂在微信的 `MMEditText`（虚拟屏）上，
    那就是机器人自己的键盘，不是用户的。用户真在物理屏打字时，
    `mServedView` 会是他那个 App 的输入框（物理屏微信那一路另有
    `physical_wechat_in_use()` 兜着）。
    """
    out = adb("dumpsys input_method | grep -m1 'mInputShown='", 20)
    if "mInputShown=true" not in (out or "").replace(" ", ""):
        return False
    served = adb("dumpsys input_method | grep 'mServedView=' "
                 "| grep -vi fallback | head -1", 20) or ""
    if "MMEditText" in served:
        return False        # 键盘是虚拟屏微信自己的
    return True


# 用户正在物理屏上用手机时，**最多每隔这么久抢一次键盘**
# （2026-10-04 用户拍板：「我能接受你每 15 秒抢一次键盘，不必非得等键盘不用了」）
USER_IDLE_GAP = float(os.environ.get("WX_USER_IDLE_GAP", "15"))
# 键盘/微信一直"忙"超过这么久 → 当成**挂着忘了关**（用户原话：「有时候我会一直挂着
# 键盘，忘了关」）。这种情况他根本没在敲，我们就不必为了避让再等那 15 秒了。
USER_HANG_SECONDS = float(os.environ.get("WX_USER_HANG_SECONDS", "2"))   # 2026-10-07 用户报「切屏后近 20 秒」：兜底默认值 30 → 2
#   （⚠️ 实际值由 wxbridge_ctl.sh 传 WX_USER_HANG_SECONDS=2；这里只是兜底，
#    原来兜底写的是 30，跟 ctl 里的 8 不一致 —— 顺便对齐）
# ★ 2026-10-05 夜：物理屏占着输入焦点（面板编辑框聚焦 → IME 挂在 WebView）时，
#   等用户让位的最长时间。0 = 退回旧行为（直接放弃这条，不补发）。
# ★ 2026-10-09（用户拍板「全改」）：30 → **8** 秒。理由：用户主动让位是概率事件，
#   他多半根本不知道机器人在等；原来吊满 30 秒，实测一条消息白等 46 秒还没发出去
#   （10-08 21:42 那次）。窗口短了不等于漏发 —— 发不出去的这条**下一轮（20 秒后）
#   还会重试**，用户几乎察觉不到差别，但发送流程不再被吊死。
PHYS_WAIT_SECONDS = float(os.environ.get("WX_PHYS_WAIT_SECONDS", "8"))
_last_grab = [0.0]        # 上一次"抢键盘"的时刻（进程内全局节流）
_busy_since = [0.0]       # 这一轮"忙"是从什么时候开始的（用来识别"挂着忘了关"）
_BUSY_SEEN = [0.0]        # 上一次**观察到**忙的时刻（中间隔太久没看 → 重新计时）
# ★ 2026-10-12：`TYPING_PATIENCE`（打字耐心，原默认 60 秒）**已删除** ——
#   它和 `grab_allowed_now()` 一起在 2026-10-10 晚就不参与判断了。


def user_busy_reason():
    """用户是不是正在物理屏上打字 / 翻微信？返回原因字符串，不忙给空串。"""
    # ★ 2026-10-11：**熄屏/锁屏 = 用户不在看手机**，下面两条判据全部不成立。
    #   尤其 `physical_wechat_in_use()`：熄屏后 topActivity 会停在微信（陈旧记录），
    #   实测熄屏时读到 `topResumedActivity=com.tencent.mm/.ui.LauncherUI` ——
    #   不短路的话机器人会一直以为"你在用微信"，每条消息都震一次。
    #   ⚠️ 读不到屏幕状态（None）时**不短路**，继续按原判据走 —— 按看护的铁律，
    #      绝不把"读不到"当成"用户不在"。
    try:
        if screen_awake() is False:
            return ""
    except Exception:
        pass
    try:
        if physical_ime_visible():
            return "你在物理屏上打字（键盘弹着）"
    except Exception:
        pass
    try:
        if physical_wechat_in_use():
            return "你在物理屏上用微信"
    except Exception:
        pass
    return ""


def wait_user_idle(gap=None, hang=None):
    """动手之前先看用户在不在用手机：刚开始忙就**等他停手**，最多等到 hang 秒。

    用户 2026-10-04 的三次口径这一版都兜住了：

    1. 「我能接受你每 15 秒抢一次键盘，不必非得等键盘不用了」
       → **不等死**：到点（`hang` 秒）就走，不会因为他挂着键盘就一直不回消息；
    2. 「打字儿不会花太多时间，但有时候我会一直挂着键盘，忘了关」
       → 键盘长时间弹着 = 人不在，到点直接做；
    3. 「页面一直在跟我抢输入法」（他在面板上打字时被反复顶掉键盘）
       → 刚开始忙的那段时间**不去抢**，每 2 秒看一眼：**他手一停（键盘收起）就立刻继续**。

    返回 True = 可以继续（恒为 True，留着返回值是为了以后想改回"放弃"语义）。
    """
    hang = USER_HANG_SECONDS if hang is None else float(hang)
    why = user_busy_reason()
    now = time.time()
    if not why:
        _busy_since[0] = 0.0
        _last_grab[0] = 0.0
        return True
    if not _busy_since[0]:
        _busy_since[0] = now
    deadline = _busy_since[0] + hang
    if now >= deadline:
        print("[wxbridge] %s —— 已经挂了 %.0f 秒（多半是忘了关），就不等了"
              % (why, now - _busy_since[0]), flush=True)
        _last_grab[0] = now
        return True
    print("[wxbridge] %s，先等你 %.0f 秒（你手一停我就动手）"
          % (why, deadline - now), flush=True)
    while time.time() < deadline:
        time.sleep(2.0)
        if not user_busy_reason():
            print("[wxbridge] 你停手了，继续", flush=True)
            _busy_since[0] = 0.0
            return True
    print("[wxbridge] 等满 %.0f 秒还在忙（多半是挂着），照做" % hang, flush=True)
    _last_grab[0] = time.time()
    return True


# ---- 开场协商：先弹预告 → 等你让位 → 再动手（2026-10-09 用户要求）----
# ★ 用户原话：「为什么他是先抢了我键盘，然后才告诉我…… 改成直接让他先弹窗，
#   然后我再放手，放手之后他再抢」。
#   **真凶**：`ime_set(IME_OURS)` 把系统默认输入法切成「DSH 输入」（一个没有键盘的
#   输入法）—— 用户在物理屏上打字的键盘**当场消失**，这才是他说的"抢键盘"。
#   而它原来排在 `/send` 很靠前的位置（会话校验、读基线之后），"要抢屏了"的预告
#   却排在**点输入框失败**之后，中间隔着 1.5 秒输入法预热 + 一次点击 + 一次查询。
#   所以用户看到的顺序必然是「键盘先没 → 过几秒才弹窗」。
#   现在：预告 + 让位 + 抢屏统一提前到**任何动手之前**（preflight_yield），
#   而且这段窗口里**只弹窗、只震动** —— 不切输入法、不点输入框、不切群。
_ANNOUNCE_LAST = [0.0]
ANNOUNCE_GAP = 8.0        # 秒：同一条消息的多次重试只弹一次预告
# ★ 2026-10-13：发图那条路的预告/让位窗口（见 send_image）——单独记时刻，别跟
#   发文字的预告互相压制（发完文字紧接着发图，用户两条提醒都该看到）。
_IMG_ANNOUNCE_LAST = [0.0]
IMG_HANG_SECONDS = float(os.environ.get("WX_IMAGE_HANG_SECONDS", "3"))
_YIELD_LAST = [0.0]       # 上一次"开场协商"的时刻（见 preflight_yield 的短路）
_PF_WHY = [""]            # 上一次 preflight_yield 放弃的原因（给 send 的失败提示用）


def announce_grab(grace=None):
    """弹「要借一下屏」的预告。8 秒内只弹一次。

    ★ 2026-10-14：改走统一出口 `_hud_grab_start()` —— **只浮窗，不再发通知栏横幅**
      （原先是 `notify_user(force=True)`，ALARM_ON 开着时还会多发一条高优先级通知）。
    """
    grace = GRAB_GRACE if grace is None else float(grace)
    now = time.time()
    if now - _ANNOUNCE_LAST[0] < ANNOUNCE_GAP:
        return False
    _ANNOUNCE_LAST[0] = now
    print("[wxbridge] 物理屏占着输入焦点 → 先弹预告，%.0f 秒后你不动才抢"
          % grace, flush=True)
    try:
        _hud_grab_start("要借一下屏发消息：约 %.0f 秒后界面会切到后台，发完自动切回来。"
                        "不想被打断就收一下键盘 / 点一下空白 / 切走" % grace,
                        detail="你不动它就自己抢一下屏（界面会切后台，发完切回来）",
                        seconds=int(max(6, grace + 2)))
    except Exception as exc:
        print("[wxbridge] 预告没发出去：%r" % (exc,), flush=True)
    _hud_buzz()          # 通知是静默渠道、浮窗可能没在看 —— 震一下最实在
    return True



def yield_reason():
    """现在该不该等用户让位？返回原因（空串 = 不用等，直接干）。"""
    try:
        if physical_input_taken():
            return "物理屏占着输入焦点"
    except Exception:
        pass
    try:
        return user_busy_reason()
    except Exception:
        return ""


def preflight_yield(force=False):
    """动手之前先打招呼：**先弹预告 → 等 GRAB_GRACE 秒 → 到点就抢**。

    返回 ``(能不能继续, 抢屏前的物理屏前台)``：
      · ``(True, "")``   —— 本来没人占 / 你已经让位 / 不用抢 → 直接往下做；
      · ``(True, prev)`` —— 真抢了（prev 交给调用方的 finally 切回来）；
      · ``(False, "")``  —— 你在用手机、又抢不了（没授权 / adb 断 / 刚抢过还在
                            限流）且等不到你让位 → **这一条留给下一轮**，不打扰你。

    ⚠️ 这段窗口里**绝不做任何会打扰用户的事**，尤其不能 ``ime_set()`` ——
       那正是用户报的"先抢我键盘"（见本段上方注释）。
    """
    # ★ 2026-10-11（用户报「锁屏时发不出消息、一直在震动」）：
    #   **熄屏/锁屏时一律静默** —— 不弹预告、不震动、不抢屏，直接把这条留给下一轮。
    #   两个理由：
    #     ① 锁屏下系统不给非锁屏窗口输入连接（实测 `mServedView=null`），
    #        这条**根本发不出去**，重试/抢屏都是白折腾；
    #     ② 用户多半在睡觉/把手机放兜里，弹「要借一下屏」（带震动+响铃的通知）
    #        只会把人震醒 —— 用户原话就是"一直在震动"。
    #   ⚠️ 读不到屏幕状态（None）时**不走这条**，照原逻辑办 —— 宁可照旧，
    #      也别因为 adb 抖一下就把发送全掐了。
    _PF_WHY[0] = ""             # 每次协商先清上次的放弃原因，别串到别的发送上
    try:
        if screen_awake() is False:
            print("[wxbridge] 熄屏/锁屏中 → 不发送、不震动、不抢屏（等亮屏再补）",
                  flush=True)
            return False, ""
    except Exception:
        pass
    # ★ 同一次发送里 `/send` 和它内部的 `/open`（切群）会连着调两次 ——
    #   5 秒内只协商一次，别让用户在宽限窗口里被问两遍。
    now = time.time()
    if now - _YIELD_LAST[0] < 5.0:
        return True, ""
    _YIELD_LAST[0] = now
    if not yield_reason():
        _busy_since[0] = 0.0
        _BUSY_SEEN[0] = 0.0
        return True, ""
    # 忙计时：只有**连续观察到**的忙才累积（中间很久没检查过就用现在重新计时，
    # 否则"上次忙是半小时前"会被误算成"他已经挂了半小时键盘"）。
    if not _busy_since[0] or (now - _BUSY_SEEN[0]) > 60.0:
        _busy_since[0] = now
    _BUSY_SEEN[0] = now
    # ★ 2026-10-12（用户：「浮窗弹三条，说三秒后抢屏、然后立即告诉我没抢到失败了」）：
    #   原来先预告、等满 GRAB_GRACE 秒，**之后**才检查 adb —— 预告说"3 秒后抢"，
    #   3 秒后改口"抢不了"，自相矛盾。现在把硬前提挪到预告**之前**：抢不了就
    #   静默放弃（不预告、不震、不白等），原因交给 send_text 的失败浮窗说一次。
    #   adb 状态是缓存的（一次命令抖动就会翻成 False），放弃前强制重探一次。
    why_not = ""
    if not (TAKE_FOCUS or force):
        why_not = "你关掉了抢屏（WXBRIDGE_TAKE_FOCUS=0）"
    elif not (adb_ok() or adb_ok(force=True)):
        why_not = "adb 断了，抢屏命令发不出去"
    if why_not:
        _PF_WHY[0] = why_not
        _YIELD_LAST[0] = 0.0     # 谈崩了就别记成"刚谈过"，否则 5 秒内下一次直接放行
        print("[wxbridge] 抢不了：%s —— 这条留给下一轮（不预告、不震动）" % why_not,
              flush=True)
        return False, ""
    announce_grab()                      # ① 预告（只弹窗 + 震动，什么都不碰）
    # ② 等满 GRAB_GRACE 秒 —— **不管你在不在动，到点就抢**
    #   ★ 2026-10-12 用户拍板：「改成弹预告之后 3 秒不管动不动都抢，不用等我」。
    #     原来这里每 ≤0.4 秒探一次"你让位了没"（连续两次不忙才算），让位就提前发 ——
    #     好处是省一次抢屏，代价是**你永远不知道它到底几秒后动手**。
    #     现在预告里写几秒就几秒，到点必抢（预告文案里的数字就是 GRAB_GRACE）。
    time.sleep(max(0.0, GRAB_GRACE))
    # ③ 还占着 → **直接抢**（★ 2026-10-10 晚用户拍板）。
    #   用户原话：「为什么会有三种情况？我想的不应该是不管啥情况，2s 之后直接抢吗？」
    #   —— 原来这里叠了五道"不抢"的分支（物理屏在用微信 / 键盘弹着还没到耐心值 /
    #   没授权 / 刚抢过还在限流 / adb 断），每道都弹一条不同的提示，他在真机上同时
    #   看到三条互相矛盾的提醒：「要借屏了」+「用 60 秒等你」+「这条先攒着」。
    #   现在**收敛成一条路**：弹预告 → 等 GRAB_GRACE 秒 → 直接抢。
    #   只留两个真正的硬前提：
    #     · WXBRIDGE_TAKE_FOCUS=0（用户显式关掉抢，想退回"等他让位"）
    #     · adb 断了（抢屏命令根本发不出去）
    #   ⚠️ 以下三个已**不再参与判断**，别再拿它们解释行为：
    #      `TYPING_PATIENCE`（原 60 秒耐心）、`grab_allowed_now()` / `GRAB_MIN_GAP`
    #      （原 20 秒限流）、`_physical_wechat_busy()`（原"你在用微信就永不抢"——
    #      它现在没有调用方了，保留定义只为少动一处）。
    prev = release_physical_input()
    if prev:
        _hud_grab_start("正在借你的屏发消息…（发完自动切回来）", seconds=12,
                        detail="界面会先切到后台，几秒就好")
    return True, prev


WX_LAUNCHER = "com.tencent.mm/.ui.LauncherUI"        # 会话列表
WX_CHAT = "com.tencent.mm/.ui.chatting.ChattingUI"   # 聊天页
WX_SEARCH = "com.tencent.mm/.plugin.fts.ui.FTSMainUI"  # 搜索页
# 只有这三个界面是我们认得、可以放心点的。其余（收藏、朋友圈、设置、支付…）
# 一律只点左上角返回箭头，绝不输入文字 —— 血泪教训见下。
SAFE_ACTIVITIES = (WX_LAUNCHER, WX_CHAT, WX_SEARCH)
CHAT_ACTIVITY = WX_CHAT


def on_chat_page():
    return screen_activity() == CHAT_ACTIVITY


def _bottom_tab_green(img):
    """会话列表的「微信」tab 选中时，左下角那个图标是绿的（#07C160）。

    2026-10-04 实测校准（同一批截图）：会话列表 4388 个绿像素 / 聊天页 0 / 搜索页 0。
    这是**不依赖 OCR** 的独立判据 —— 虚拟屏上会话列表的标题栏有时会渲染成重影，
    OCR 把「微信(6)」读成 "Q+"，光靠标题会把 back_to_list 卡死在列表页。

    ★★ 2026-10-10（重要修复）：裁剪区**必须按图尺寸算**，不能写死像素 ——
      老代码写死 `(0, 1230, 220, 1400)`，那是 **half 档(636x1400)** 的坐标；
      而 slot 0 正常跑 **full 档(1264x2780)**，那个窗口裁到的是图片**中部**
      （y 1230~1400 ≈ 44%~50%，正好是聊天页里的**绿色消息气泡**）→
      聊天页被判成"会话列表"。后果是"列表里直接点群"这条路整个失效：
        · `_open_chat_by_list` 点进群后校验失败 → 白回退搜索框（借输入法 = 抢键盘）；
        · `/where` 报 chat_title 空、`_already_in_chat` 判"不在群里"→ 反复切群。
      实测（2026-10-10，full 档）：老窗口在聊天页有 1941~25055 个绿像素（全误判）。
    现在的判据 = **只取底部最左那一小块**（tab 图标本身），按比例：
      实测 目标群聊天页 0 / 文件传输助手聊天页 0 / 会话列表 4407。
      ⚠️ y 起点必须 ≥0.925：0.90 时聊天页底部那条绿色群工具条会被算进来（3250），
         0.92 时文件传输助手页还有 353 —— 都会误判。
    """
    try:
        from PIL import Image
        with Image.open(img) as _im:
            w, h = _im.size
            im = _im.convert("RGB").crop((0, int(h * 0.925), int(w * 0.20), h))
        n = 0
        for r, g, b in _pixels(im):
            if g > 120 and g - r > 50 and g - b > 30:
                n += 1
        return n >= 300
    except Exception:
        return False


def _main_tab_green_counts(img):
    """底部四个 tab 图标各自的绿像素数（0=微信 1=通讯录 2=发现 3=我）。

    ★ 2026-10-12：微信四个 tab **共用同一个 LauncherUI** —— 停在「我 / 通讯录 /
      发现」页时 `screen_activity()` 照样是 LauncherUI、`on_wx_tab()` 照样为假，
      可那三页**左上角没有返回箭头**：`back_to_list()` 老代码在那里点 (55,60)
      点满 6 轮全是空炮 → 切群卡满超时 → 机器人启动直接退出（2026-10-12 真机
      踩到，日志只写「标题没能匹配上目标群」，现场是「我」页）。
      这两类页面在底部 tab 栏上长得完全不一样：
        · tab 页（我 / 通讯录 / 发现）：底部有 4 个图标，**亮的那个是绿的**；
        · 聊天页：底部是输入框，压根没有 tab 栏（四个 slot 全 0）。
    判据与 `_bottom_tab_green` 同源（同一个 y 区间 + 同一个绿色阈值），只把窗口
    从"最左那一块"扩成"四块分别数" —— 于是 slot 0 的结果与它**完全一致**。
    """
    out = [0, 0, 0, 0]
    try:
        from PIL import Image
        with Image.open(img) as _im:
            w, h = _im.size
            rgb = _im.convert("RGB")
            y0, y1 = int(h * 0.925), h
            for i in range(4):
                x0 = int(w * (0.25 * i))
                x1 = int(w * (0.25 * i + 0.20))
                buf = rgb.crop((x0, y0, x1, y1)).tobytes()
                n = 0
                for j in range(0, len(buf), 3):
                    r, g, b = buf[j], buf[j + 1], buf[j + 2]
                    if g > 120 and g - r > 50 and g - b > 30:
                        n += 1
                out[i] = n
    except Exception:
        return [0, 0, 0, 0]
    return out


def _on_other_main_tab(img="/tmp/wxbridge-tabpage.jpg"):
    """当前是不是停在「通讯录 / 发现 / 我」页（既不是会话列表、也不是聊天页）。

    判据：底部 tab 栏**最左那个不绿、其余某一个绿**。
    ⚠️ 必须现截一张新图：`on_wx_tab()` 用的 `/tmp/wxbridge-tab.jpg` 在它自己
      截图失败时**不会更新**，拿旧图判等于把上一种界面当成现在这种。
    """
    if not shot(img):
        return False
    c = _main_tab_green_counts(img)
    return c[0] < 300 and max(c[1:]) >= 300


def on_wx_tab():
    """会话列表 = LauncherUI + 顶部标题是「微信」。

    微信的四个 tab（微信 / 通讯录 / 发现 / 我）**共用同一个 LauncherUI**，
    只比 Activity 会把「我」的个人页当成会话列表 —— 然后往一个没有放大镜的
    页面上狂点（踩过，浪费了三轮）。
    """
    if screen_activity() != WX_LAUNCHER:
        print("[wxbridge] on_wx_tab: 假 —— activity=%r 不是 LauncherUI" % screen_activity(),
              flush=True)
        return False
    img = "/tmp/wxbridge-tab.jpg"
    wf = wechat_front()
    sh = shot(img) if wf else False
    if not (wf and sh):
        print("[wxbridge] on_wx_tab: 假 —— wechat_front=%s shot=%s" % (wf, sh), flush=True)
        return False
    if wxreader is None:
        print("[wxbridge] on_wx_tab: 假 —— wxreader 为 None（OCR 没起来）", flush=True)
        return False
    t = (wxreader.read_title(img) or "").replace(" ", "").rstrip(".．…")
    if not t:
        # 诊断：标题读空时，把这张图另存一份 + 立刻再读一次。
        # 用来区分是「图有问题」还是「进程内的 OCR 有问题」。
        try:
            size0 = os.path.getsize(img)
            import shutil as _sh
            _sh.copy(img, "/tmp/wxbridge-tab-fail.jpg")
        except Exception as _e:
            size0 = -1
        t2 = (wxreader.read_title(img) or "").replace(" ", "").rstrip(".．…")
        print("[wxbridge] on_wx_tab 诊断：第1次=%r 第2次=%r 图=%s 字节（另存 /tmp/wxbridge-tab-fail.jpg）"
              % (t, t2, size0), flush=True)
        t = t2
    # 标题正常是「微信(4)」；但在虚拟屏的 freeform 窗口里，它常被窗口标题栏
    # 挤成「微…」，OCR 只读得到一个「微」字（2026-10-03 实测）。
    # 死等完整「微信」会让 back_to_list 永远失败 —— 所以认开头即可。
    # 聊天页顶部是群名/人名，几乎不会以「微」开头，且长度也放了个闸。
    ok = (t.startswith("微") or "微信" in t) and len(t) <= 8
    if not ok and _bottom_tab_green(img):
        # 标题被渲染重影弄脏（实测读成 "Q+"），但左下角「微信」tab 是绿的 →
        # 确实在会话列表，放行（否则切群第一步就死）。
        print("[wxbridge] on_wx_tab: 标题 OCR 脏了（%r），但底部微信 tab 是绿的 → 判为会话列表" % t,
              flush=True)
        return True
    if not ok:
        size = os.path.getsize(img) if os.path.exists(img) else -1
        print("[wxbridge] on_wx_tab: 假 —— 标题=%r，图 %d 字节（%s）" % (t, size, img),
              flush=True)
    return ok


# ---------------------------------------------------------------- 模态弹窗
# 2026-10-03 加。起因：微信冷不丁弹了个白底模态框 ——
# 「有人正通过微信密码登录你的微信…请立刻修改微信密码，慎防盗号」［忽略］［修改密码］。
# 它盖在会话列表上，但 **activity 一点没变**（还是 LauncherUI）：back_to_list 每轮
# 都以为"人在列表上、只是标题不对"，点返回箭头又点不动，最后假失败 → 机器人报
# 「切群失败」当场退出。这里用 OCR 认弹窗，按钮坐标也从 OCR 结果里取，不硬编码。
POPUP_SIGNS = ("慎防盗号", "修改密码")          # 命中任一词即认定是安全弹窗
POPUP_BUTTONS = ("忽略", "我知道了", "知道了", "不再提示")   # 只点这些无害按钮


def _ocr_words(img):
    """整图 OCR，返回 [(文字, 中心x, 中心y)]；失败给空表。"""
    if wxreader is None:
        return []
    try:
        import numpy as np
        from PIL import Image
        res, _ = wxreader.ocr_call(np.array(Image.open(img).convert("RGB")))
    except Exception as exc:
        print("[wxbridge] 整图 OCR 失败：%r" % (exc,), flush=True)
        return []
    words = []
    for item in (res or []):
        try:
            box, txt = item[0], str(item[1]).strip()
            if not txt:
                continue
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            words.append((txt, (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2))
        except Exception:
            continue
    return words


def find_popup(img):
    """这张截图里是不是微信的模态弹窗？是就返回该点的按钮中心 (x, y)。"""
    words = _ocr_words(img)
    if not words:
        return None
    joined = " ".join(w for w, _, _ in words)
    if not any(s in joined for s in POPUP_SIGNS):
        return None
    print("[wxbridge] 检测到微信模态弹窗：%s" % joined[:70], flush=True)
    for w, cx, cy in words:
        if w.replace(" ", "") in POPUP_BUTTONS:
            return (cx, cy)
    print("[wxbridge] 是弹窗但没找到可点的按钮，只记日志、不乱点", flush=True)
    return None


def dismiss_popup(img="/tmp/wxbridge-popup.jpg"):
    """检测并点掉挡路的模态弹窗。返回 True = 确实关掉了一个。"""
    if not shot_wechat(img):
        return False
    pos = find_popup(img)
    if not pos:
        return False
    print("[wxbridge] 自动点掉弹窗 (%d,%d)，界面继续" % pos, flush=True)
    tap(pos[0], pos[1])
    time.sleep(1.2)
    return True


def back_to_list(tries=6):
    """退回会话列表。碰到不认识的界面也只会点返回箭头，绝不输入任何文字。

    为什么这么小心：曾经在某次切换里，界面上其实停着「收藏 - 笔记」编辑页，
    旧版 /open 照样点搜索框位置、还往里 commitText —— 那会**改坏用户的收藏数据**。
    从那以后订了规矩：**只有 Activity 明确是搜索页时才允许输入文字**。
    """
    if "com.tencent.mm" not in screen_activity():
        ensure_wechat_front()
    for i in range(tries):
        act = screen_activity()
        if act == WX_LAUNCHER:
            if on_wx_tab():
                print("[wxbridge] back_to_list: 已在会话列表（第%d轮）" % (i + 1), flush=True)
                return True
            # 微信的「聊天页」也是 com.tencent.mm/.ui.LauncherUI，但它**没有底部 tab 栏** ——
            # 老代码在这儿点 (55,1300)（底部「微信」tab），在聊天页等于点在消息区上，
            # 永远退不出去；再兜底 am start LauncherUI 也没用（本来就是 LauncherUI），
            # 于是 back_to_list 假成功，goto_search 接着点右上角就打开了「聊天信息」页。
            # 2026-10-03 改成点左上角返回箭头（聊天页/子页都吃这一下）。
            # ★ 2026-10-12（用户报「拉不起机器人」，实测根因）：先分清这是
            #   **tab 页**还是**聊天页 / 子页** —— 停在「我 / 通讯录 / 发现」页时
            #   左上角**没有返回箭头**，点 (55,60) 点多少轮都是空炮：切群会一直
            #   耗到超时（实测 71 秒）然后整个启动失败，微信留在「我」页等人来救。
            #   tab 页底部有 tab 栏（非最左那个图标是绿的），聊天页没有（底部是输入框）。
            if _on_other_main_tab():
                print("[wxbridge] back_to_list: 第%d轮 停在「我/通讯录/发现」页"
                      "（底部 tab 栏的绿不在最左）→ 点回「微信」tab" % (i + 1), flush=True)
                tap(*_sp(80, 1345))
                time.sleep(1.0)
                continue
            # ★ 2026-10-12 晚（用户报「切群卡了 79.6 秒」）：还有第三种遮挡 ——
            #   **下拉小程序面板**（标题读作「最近」）。它盖在会话列表上，activity
            #   一点没变（还是 LauncherUI）、底部「微信」tab 还是绿的（所以
            #   `_on_other_main_tab()` 判不出来），而**左上角根本没有返回箭头** ——
            #   原来每轮都点 (55,60) 是空炮，6 轮全打空，切群直接失败、微信留在
            #   面板上等人来救。
            #   这里不额外读一次标题（省一次 OCR），改成**两种手段轮流上**：
            #     · 奇数轮：点左上角返回箭头（聊天页 / 子页吃这一下）；
            #     · 偶数轮：发系统 BACK（浮层一律吃这一下，且不会改动任何数据）。
            #   谁有效，下一轮 `on_wx_tab()` 就会变真、直接返回 —— 代价只是多花一轮。
            if i % 2 == 0:
                print("[wxbridge] back_to_list: 第%d轮 act=LauncherUI 但 on_wx_tab=假"
                      "（标题不对）→ 点返回箭头" % (i + 1), flush=True)
                tap(*_sp(55, 60))
            else:
                print("[wxbridge] back_to_list: 第%d轮 标题还是不对 → 发 BACK 收浮层"
                      "（下拉小程序面板左上角没有返回箭头）" % (i + 1), flush=True)
                _d = _require_did()
                if _d is not None:
                    adb("input -d %d keyevent 4" % _d, 25)
            time.sleep(0.9)
            continue
        if act and not act.startswith("com.tencent.mm"):
            print("[wxbridge] back_to_list: 前台不是微信（%r），放弃" % act, flush=True)
            return False                      # 前台已经不是微信了，别乱点
        if act and act not in SAFE_ACTIVITIES:
            print("[wxbridge] 不认识的界面 %s，只点返回箭头" % act, flush=True)
        tap(*_sp(55, 60))
        time.sleep(0.8)
    # 6 轮都没退出去 —— 十有八九是被模态弹窗挡住了（activity 却一直显示 LauncherUI，
    # 点返回箭头也点不动）。检测到就点掉，然后重试一轮。
    if dismiss_popup():
        print("[wxbridge] back_to_list: 点掉了弹窗，重试一轮", flush=True)
        return back_to_list(tries=4)
    if screen_activity() != WX_LAUNCHER:      # 兜底：直接启动微信主界面
        print("[wxbridge] back_to_list: 兜底 am start LauncherUI", flush=True)
        _d = _require_did()
        if _d is not None:
            adb("am start --display %d -n %s" % (_d, WX_LAUNCHER), 25)
        time.sleep(1.6)
    if screen_activity() != WX_LAUNCHER:
        print("[wxbridge] back_to_list: 兜底后仍不是 LauncherUI（%r），失败"
              % screen_activity(), flush=True)
        return False
    # 必须真的停在「微信」tab 的会话列表上 —— 只看 Activity 会把聊天页当成列表
    ok = on_wx_tab()
    print("[wxbridge] back_to_list: 兜底后 act=LauncherUI，on_wx_tab=%s" % ok, flush=True)
    return ok


def _wx_root_task_id():
    """微信主 task 的 root task id（给 move-stack 用）。找不到返回 None。

    ★ 2026-10-10：`cmd activity stack list` 里 `RootTask id=N ... displayId=D`
      后面跟 `taskId=N: com.tencent.mm/<activity>`。优先认 LauncherUI 那条。
    """
    out = adb("cmd activity stack list", 25) or ""
    fallback = None
    for m in re.finditer(r"taskId=(\d+): com\.tencent\.mm/(\S+)", out):
        tid, act = int(m.group(1)), m.group(2)
        if "LauncherUI" in act:
            return tid
        if fallback is None:
            fallback = tid
    return fallback


def _wx_task_display(tid):
    """微信 task 当前在哪块屏；查不到返回 None。"""
    out = adb("cmd activity stack list", 25) or ""
    for m in re.finditer(r"RootTask id=(\d+) bounds=\S+ displayId=(\d+)", out):
        if int(m.group(1)) == tid:
            return int(m.group(2))
    return None


# ══════════════ 虚拟屏画面冻结：检测 + 自愈（★ 2026-10-14） ══════════════
# 病根（已坐实）：用户把微信拿到物理屏、切走后 `reclaim()` 用 `move-stack` 把它移回
#   虚拟屏 —— 但**虚拟屏的 surface 不会重新合成**，画面永远停在"移走前的最后一帧"。
#   后果是**静默失效**：机器人能读到画面、不报错、面板不挂红字，却永远读到同一张
#   旧图（2026-10-14 实测失效近 1 小时；用户只能从"机器人不回消息"察觉）。
# 检测 `_note_frame()`：`snapshot()` 本来就为省一次 OCR 算了截图 md5（实测静止的
#   虚拟屏连抓三张 JPEG 逐位一致），直接拿来用。「连续 FREEZE_ROUNDS 轮 md5 完全
#   相同」**且**「FREEZE_MOVE_WINDOW 秒内发生过跨屏移动」两条同时成立才怀疑 ——
#   第二条把"群里本来就安静"挡掉大半。
# 自愈 `_thaw_screen()`（独立线程，不阻塞读屏）：
#   ① 唤醒 = 用 `am start --display <d>` 重放一次微信窗口，然后**再抓一帧**比对：
#      变了 → 其实没冻（只是没新内容），收工；没变 → 真冻。
#   ② 真冻时**只报警、不自动重建** —— 重建要杀掉虚拟屏上的微信任务，还要处理屏号
#      缓存、重新放微信、切回群，链条长且当时无法在线验证安全性，所以留人工：
#      日志写 WARNING + 浮窗挂出来，命令写在提示里（`SKILL.md` 也有）。
#   ⚠️ 唤醒用的 `am start LauncherUI` 会把微信**提到会话列表** —— 这在
#      `_move_wx_task_to_display` 里是禁忌（会把用户的聊天页顶掉），但**冻结时
#      无所谓**：反正已经瞎着，且这条路径只在"确认冻结"之后才走。
_FREEZE = {"md5": "", "same": 0, "moved_at": 0.0, "last_try": 0.0, "busy": False}
FREEZE_ROUNDS = int(os.environ.get("WXBRIDGE_FREEZE_ROUNDS", "20"))
FREEZE_MOVE_WINDOW = float(os.environ.get("WXBRIDGE_FREEZE_MOVE_WINDOW", "900"))
FREEZE_COOLDOWN = float(os.environ.get("WXBRIDGE_FREEZE_COOLDOWN", "600"))
# ★ 2026-10-16：复核时观察守护帧计数的秒数。实测虚拟屏**内容不变时本来就不产新帧**，
#   所以这个窗口只能当"还要不要继续复核"的信号，不能单独当冻结证据（见 _thaw_screen）。
FREEZE_PROBE_SECS = float(os.environ.get("WXBRIDGE_FREEZE_PROBE", "5"))


def _note_frame(md5):
    """读屏每帧报一次：连续多少轮画面一模一样；够格就叫一次自愈。"""
    if not md5:
        return
    now = time.time()
    if md5 == _FREEZE["md5"]:
        _FREEZE["same"] += 1
    else:
        _FREEZE["md5"] = md5
        _FREEZE["same"] = 0
        return
    if _FREEZE["busy"] or _FREEZE["same"] < FREEZE_ROUNDS:
        return
    if now - _FREEZE["moved_at"] > FREEZE_MOVE_WINDOW:
        return                      # 很久没跨屏移动过 —— 多半只是群里安静
    if now - _FREEZE["last_try"] < FREEZE_COOLDOWN:
        return
    _FREEZE["last_try"] = now
    _FREEZE["busy"] = True
    threading.Thread(target=_thaw_screen, daemon=True).start()


def _thaw_screen():
    """疑似冻结 → 先用**守护帧计数**复核死活，真冻才唤醒复验 + 报警。

    ★ 2026-10-16（用户报「自动唤醒没成功，但是下一条也能发出去」）：
      「连续 N 轮截图 md5 完全相同」**根本不能证明冻结** —— 群里安静时静止画面
      的 JPEG 编码是逐位确定的，20 轮（约 1~2 分钟没人说话）必然相同，而
      `FREEZE_MOVE_WINDOW=900` 秒那个附加条件几乎总成立。实测 08:28 / 08:51
      两次报警都是误报：报警后连探 15 次 `/where` 全部读到「目标群」、消息照发。
      更坏的是原来自愈一上来就 `am start LauncherUI` 重放窗口，**会把微信提到
      会话列表**（有副作用），只是当时误判才没出事。
      现在第一道判据换成守护的 `frames`：它在 ImageReader 回调里自增（每来一帧
      +1，与客户端取不取图无关）—— **frames 在涨 = 屏还活着，画面静止只是群里
      没新内容**；frames 停着不动才是真的不出帧。只有后者才继续唤醒复验。
    """
    try:
        print("[wxbridge] ⚠️ 读屏画面连续 %d 轮完全没变（且 %.0f 秒前跨屏移动过）"
              "→ 疑似虚拟屏冻结，先复核屏的死活"
              % (_FREEZE["same"], time.time() - _FREEZE["moved_at"]), flush=True)
        _FREEZE["same"] = 0        # 本次复核已经接手，先复位，别让它每轮重复进来
        # ★ 2026-10-16（第二轮修正）：**不再 `am start` 重放窗口**。两个原因：
        #   ① 它根本改不动画面 —— 微信 task 已经在同一块 display 上，`am start`
        #      LauncherUI 到同一个 display 等于 no-op，所以"唤醒后画面纹丝不动"
        #      是必然的，**这一点从来不能证明冻结**（08:51 那次就是这么误报的）；
        #   ② 万一它真生效，会把微信**提到会话列表**（顶掉聊天页）—— 误报时纯属添乱。
        #   现在改成两步纯只读的复核：
        #   ① 守护出帧计数在 `_FREEZE_PROBE_SECS` 秒内涨过 → 屏活着，收工
        #      （注：Android 虚拟屏**内容不变时本来就不产新帧**，所以"不涨"仍不足为证，
        #       只是把它当"继续复核"的信号）；
        #   ② 画面静止但**标题照样读得出来** → 只是群里没有新内容，收工；
        #   ③ 帧不涨 **且** 连读两帧连标题都读不出 → 才算读屏真的瞎了，提示用户。
        _f1 = _vsd_frames()
        if _f1 is not None:
            time.sleep(FREEZE_PROBE_SECS)
            _f2 = _vsd_frames()
            if _f2 is not None and _f2 > _f1:
                print("[wxbridge] 自愈① 守护仍在出帧（frames %d→%d）→ 屏没冻，"
                      "只是画面没新内容（群里安静），收工" % (_f1, _f2), flush=True)
                return
            print("[wxbridge] 冻结复核：帧计数停着（%s→%s）→ 再看读屏还读不读得到东西"
                  % (_f1, _f2), flush=True)
        else:
            print("[wxbridge] 冻结复核：读不到守护帧计数 → 再看读屏还读不读得到东西",
                  flush=True)
        for _i in range(2):
            _d2 = {}
            _t2, _ = _title_probe("/tmp/wxbridge-freeze-probe.jpg", _d2)
            if _t2:
                print("[wxbridge] 自愈① 画面静止但标题仍读得到（%r）→ 判为"
                      "「本来就没新内容」，不是冻结，收工" % _t2, flush=True)
                return
            if _i == 0:
                time.sleep(1.0)
        # ★ 2026-10-16（用户拍板「扰动复验加上」）：到这一步仍然只是"怀疑" ——
        #   静止的画面 + 读不出的标题，也可能是抓帧那一瞬的偶发失败。**唯一能定案的
        #   是主动扰动**：把微信 task 搬到另一块虚拟屏再搬回，这一步必然触发重绘。
        #   · 画面/帧动了 → 屏活着，收工（不打扰用户）；
        #   · 纹丝不动     → 真冻结，才报「需要重建」；
        #   · 做不了（只有一块屏 / 拿不到 task id）→ 按"疑似"保守提示（不谎报已确认）。
        _dv = _disturb_and_verify()
        if _dv is True:
            print("[wxbridge] 自愈② 扰动后画面已更新 → 屏活着（刚才只是没新内容），"
                  "收工", flush=True)
            return
        if _dv is None:
            print("[wxbridge] 自愈② 扰动做不了（只有一块屏 / 拿不到微信 task）"
                  "→ 只能按「疑似」提示", flush=True)
        else:
            print("[wxbridge] ⚠️⚠️ 自愈② 扰动后画面纹丝不动 → **虚拟屏已冻结**："
                  "先 bash /sdcard/dsh/wechat-bot/app/wxbridge_ctl.sh restart；"
                  "仍不行重建虚拟屏 sh /sdcard/dsh/vscreen-daemon/start.sh 18250"
                  " 再 bash bot.sh start --restart", flush=True)
        _hud_notice("读屏画面长时间没变化、内容也读不出来",
                    seconds=20, title="⚠️ 微信机器人 · 可能看不见新消息",
                    detail="先重启桥接；仍不行再重建虚拟屏（命令见日志/SKILL.md）")
    except Exception as exc:
        print("[wxbridge] 自愈异常：%r" % (exc,), flush=True)
    finally:
        _FREEZE["busy"] = False


def _move_wx_task_to_display(did):
    """把微信 task **整体**搬到 did 屏，保留 Activity 栈。返回 task 是否存在。

    ★ 2026-10-10（用户报「物理屏切回虚拟屏时微信回到会话列表」）：
      原来这里用 `am start -n LauncherUI`。LauncherUI 是 task root，重新 start 它
      会把栈顶的聊天页清掉 —— 用户看到的就是"回到会话列表"。
      `cmd activity display move-stack` 只搬 task、不解析 Intent、不动栈。
      task 不存在（微信进程被杀）时返回 False，交给调用方 am start 冷启动。
    """
    tid = _wx_root_task_id()
    if tid is None:
        return False
    if _wx_task_display(tid) == did:
        return True
    adb("cmd activity display move-stack %d %d" % (tid, did), 25)
    # ★ 2026-10-14：记下"刚跨屏移动过" —— 画面冻结**只在这之后发生**，
    #   `_note_frame()` 拿它当判据之一（把"群里本来就安静、画面几十分钟没变"挡掉）。
    _FREEZE["moved_at"] = time.time()
    return True


def takeover():
    """**明确**把微信从物理屏拿到虚拟屏。

    只有"用户主动启动机器人"这种场合才该调用 —— 那时把微信挪走是用户的意思。
    机器人跑起来之后就不再主动抢了（`ensure_wechat_front` 里那道保护），
    免得用户点一下微信就被机器人抢回去。
    """
    if wechat_front():
        return True
    # ★ 2026-10-08 审查修复（F3 配套）：这里原来直接用 did()，读不到屏号时会把命令
    #   拼成 `--display None`（静默失败并空等 5 秒）。改成拿不到就当场说清楚。
    _d = _require_did()
    if _d is None:
        print("[wxbridge] ensure_wechat_front：读不到虚拟屏号，无法把微信拉回前台",
              flush=True)
        return False
    # ★ 2026-10-10：优先 move-stack（保留聊天页），task 不在才冷启动
    if not _move_wx_task_to_display(_d):
        print("[wxbridge] 微信 task 不在，冷启动拉起来", flush=True)
        adb("am start --display %d -n com.tencent.mm/.ui.LauncherUI" % _d, 25)
    # ⚠️ 冷启动必须等足：微信**进程被杀之后**重新拉起来，2 秒是回不来的。
    #    实测（2026-10-03）：用户点了一下微信 → 微信被系统回收 → 启动脚本里
    #    takeover 2 秒就判失败 → 紧接着 /open 切群必然失败 → 机器人当场退出，
    #    面板上只剩「已停止」，看着像"点了启动没反应"。
    for _ in range(12):                  # 最多等 18 秒
        time.sleep(1.5)
        if wechat_front():
            break
    ok = wechat_front()
    print("[wxbridge] /takeover -> %s（物理屏前台 %s）"
          % ("已把微信拿到虚拟屏" if ok else "没成功",
             top_activity_of(0) or "?"), flush=True)
    return ok


def _input_text_ok(got, want):
    """输入框里 OCR 到的文字，是不是我们正要发的那一条。

    ★ 2026-10-08（事故：连发 5 条垃圾进群）：
      用户在物理屏看抖音新闻直播，直播间的字幕/公屏文字串进了微信输入框。
      机器人只验「框里有没有字」（stddev 高 / OCR 非空）就点发送，于是把
      「层面的法治 / 动荡与不稳定 / 法国六号试射 / 相关内容开展 / 每次申请」
      这五条别人的文字发进了群 —— 它自己还以为发的是「本喵承认 喵放假怎么了喵」
      那几句，自检当然不过，于是重试，又发下一条。

    判据（宁可少发一条，也绝不把别人的文字发进群）：
      · 读到空 → 不认（保守）；
      · 读到的字**全部**都能在期望文本里找到 → 认；
      · 只要有一个字对不上 → 不认。
    实测：正常那几次输入框裁剪 OCR 读到「绍」（=「介绍下喵」里的字）→ 放行；
          串入那次读到「次」「由」，两个都不在期望文本里 → 拦下。
    """
    # ★ 数字归一（2026-10-09）：实测「尾巴一卷」在框里被读成「卷1」——
    #   「一」认成「1」是最常见的一类抖动，两边都归一后这条就不再是"错字"。
    g = re.sub(r"[\s\W_]+", "", got or "").translate(_OCR_DIGITS)
    w = re.sub(r"[\s\W_]+", "", want or "").translate(_OCR_DIGITS)
    if not g or not w:
        # ★ 2026-10-09 晚：两边都被 \W 清光 = 这一条（至少读到的这边）全是标点。
        #   「？喵」实测就读成「?」→ g="" → 老代码在这里无脑拒发。交给标点兜底。
        return _punct_subset_ok(got, want)
    # ★ 2026-10-08 晚：容忍 **1 个**字对不上（原来是"有一个对不上就拒发"）。
    #   实测：虚拟屏字体约 54px/字、输入框可用宽度只够一个字，长文本在框里是
    #   **竖排**的；OCR 会把竖排末尾的字读残或读重 —— 「耗时测量喵」被读成
    #   「耗时测量里喵」（多出的「里」= 切残的「量」）。原判据于是把一条
    #   **完全正常**的输入判成"串输入"拒发 —— 用户看到的就是"机器人不回话"。
    #   为什么放宽 1 个字仍然安全：2026-10-08 那次真实事故（抖音直播字幕串进
    #   输入框、连发 5 条垃圾进群）里，每条串入文本都有**多个**字不在期望文本里；
    #   而且这里还要求"读到的字至少 3 个"才启用容忍 —— 只读到 1~2 个字时仍然
    #   从严（宁可这一条不发，也不冒把别人文字发进群的风险）。
    unknown = [ch for ch in g if ch not in w]
    if not unknown:
        return True
    if len(unknown) == 1 and len(g) >= 3:
        return True
    # ★ ③ 2026-10-09 新增（用户报「？喵 三条都没发出去」）：**反向判据** ——
    #   要发的字**一个不缺**地出现在框里 → 注入是成功的，多出来的那些是复核窗口
    #   串进来的页面文字（那次读到「土群专享?喵」，"土群专享"来自输入框上方
    #   170px 的聊天区，见 INPUT_OCR_CROP 的说明）。
    #   为什么仍然安全：2026-10-08 那次真实事故（抖音直播字幕把输入框占满、
    #   连发 5 条垃圾进群）里，框里**根本没有我们要发的字** —— 反向判据同样不通过。
    #   ⚠️ 只给长句开口子（≥ REVERSE_MIN_CHARS）：短句用这条等于没判据。
    if len(w) >= REVERSE_MIN_CHARS and all(ch in g for ch in w):
        print("[wxbridge] 输入框复核：框里多出 %d 个不明白的字（%r），但我们要发的"
              "字一个不缺 → 放行" % (len(unknown), "".join(unknown)[:8]), flush=True)
        return True
    # ⚠️ 2026-10-09 这里**试过**再加一条"末尾片段判据"（读到的字都落在期望文本
    #   最后 12 个字里就放行），随后发现它是**死代码** —— 判据 ① 的条件
    #   （读到的字全都在期望文本里）本来就覆盖了它（末尾 ⊆ 全文），永远轮不到。
    #   真正治好当天那条故障的是上面的**数字归一**：读到「卷1」→ 归一成「卷一」
    #   → 没有不明白的字 → 判据 ① 直接放行。所以这里保持原样，不再为"假设会
    #   发生"的抖动放宽判据 —— 防串输入这条线是 2026-10-08 真事故换来的。
    return False


def _input_filled(sd=None, base=None, img=None):
    """输入框里到底有没有字。返回 (有字?, 依据字符串)。

    ★ 2026-10-07：加 ``img`` —— 调用方指定"截到哪张图"，函数会把路径记进
    ``_LAST_INPUT_SHOT``，紧接着的 ``find_send_btn()`` 就能复用同一张图。
    ⚠️ 之前走 stddev 判定（够高就直接 return True）时**根本不截图也不记图**，
    导致 §46.24 那个复用**永远不生效**（2026-10-07 实测日志里还是"OCR 认到发送按钮"）。

    ⚠️ 2026-10-03 实测：**空输入框的 stddev 不是 0.002，而是在 0.06~0.07 之间漂**
    （虚拟屏本身有噪声），比老代码「>0.05 就算有字」的阈值还高 —— 于是
    「发送后 0.0629」既被判成"还有字"、又永远够不到 0.02 的"空"标准，
    两头都错，机器人以为没发出去、重发，群里就连出三个「？」。
    所以「有字」必须**相对基线**判：比基线高出半个 (0.05) 以上才算有字；
    拿不到基线时才退回绝对值。
    """
    if sd is None:
        _seq_holder = None
        if img is None:
            global _seq
            _seq += 1
            img = "/tmp/wxbridge-filled-%d.jpg" % _seq
        sd = input_stddev(img)
        if sd is not None:
            _LAST_INPUT_SHOT["path"] = img     # ★ 给别人复用（见 find_send_btn）
    thr = STDDEV_FILLED if base is None else base + STDDEV_FILLED
    if sd is not None and sd >= thr:
        return True, "stddev=%s ≥ %s" % (sd, round(thr, 4))
    # ★ wide=False：这是 stddev 之外的兜底，保持"只看输入框"的原行为最稳
    ocr_txt = (_ocr_input_box(img, shoot=False, wide=False) if img
               else _ocr_input_box(wide=False))
    if ocr_txt:
        return True, "ocr=%r" % ocr_txt
    return False, "stddev=%s < %s 且 ocr 为空" % (sd, round(thr, 4))


def _ocr_input_box(img=None, shoot=True, wide=True, want=""):
    """输入框区域里 OCR 到的文字（过滤掉"发送"这类按钮字）。读不到就返回 ""。

    ``want``：这一条**要发什么**。给了它、且 ``wide=True`` 时，裁剪窗口按
    字数自适应放大（见 :func:`_input_ocr_crop_for`）—— 复核要读全框里的字，
    否则长文本只读得到末尾两三个字，判据会把**正常输入**误判成串输入。

    ``shoot=False`` 表示 ``img`` 是现成的图（同 input_stddev）。执行完会把路径
    记进 ``_LAST_INPUT_SHOT``，方便紧接着的 ``find_send_btn()`` 复用同一张图。

    ★ 2026-10-08 晚：``wide`` 决定用哪个窗口，**两个场景必须分开**：
      · ``wide=True``（默认，**发送前**复核"框里的字是不是我要发的"）——
        用 ``INPUT_OCR_CROP``，覆盖整个输入框。因为虚拟屏字体约 54px/字、
        输入框只够一个字宽，长文本在框里是**竖排**的，小窗口会把字切残
        （实测「耗时测量喵」→ 残字被读成「里」，整条正常输入被误判成串入而拒发）。
      · ``wide=False``（**发送后**自检）—— 用原来的 ``INPUT_CROP``（只覆盖输入框
        下半部）。用大窗口会**读到自己刚发出去的那条消息**：实测发送后大窗口读到
        「毛时测」（= 消息列表最后一条气泡），于是把"已经发成功"判成"框里还有字、
        这条没发出去"。
    """
    if wxreader is None:
        return ""
    global _seq
    if img is None:
        _seq += 1
        img = "/tmp/wxbridge-input-%d.jpg" % _seq
        shoot = True
    try:
        if shoot and not shot_wechat(img):
            return ""
        _LAST_INPUT_SHOT["path"] = img          # ★ 给别人复用（见 find_send_btn）
        # ★ 2026-10-08 晚：发送前用大窗口（读全竖排的字），发送后用原来的小窗口
        #   （只看输入框，别读到自己刚发出去的消息）。见函数 docstring 的 wide 说明。
        # ★ 2026-10-10（续22）：把这张**已填充**的图交给窗口函数 —— full 档它要
        #   从图里数"输入框占了几行"（按字数估的行数会差一行 = 骑边界 = 误拒）。
        _crop = (_input_ocr_crop_for(want, img=img) if (wide and want)
                 else (INPUT_OCR_CROP if wide else INPUT_CROP))
        _LAST_INPUT_CROP[0] = _crop
        r = _run(["convert", img, "-crop", _crop,
                  "/tmp/wxbridge-input-crop.jpg"], 20)
        if r is None:
            return ""
        res = wxreader.ocr_call("/tmp/wxbridge-input-crop.jpg")
        words = []
        for line in (res[0] if res and res[0] else []):
            t = str(line[1]).strip()
            if t and t not in ("发送", "语音", "+"):
                words.append(t)
        return "".join(words)
    except Exception as exc:
        print("[wxbridge] 输入框 OCR 复核异常：%r" % (exc,), flush=True)
        return ""


def find_send_btn(img=None):
    """OCR 认右下角的「发送」按钮，返回要点的坐标（认不到就回退候选坐标）。

    2026-10-04：底部输入栏会随屏号/输入内容上下移动（写死的 (555,1357) 在新屏上点空），
    所以发之前先认字定位。**这个函数只定位、不点击** —— 「点发送」全程仍然只执行一次。
    """
    global _seq
    # ★ 2026-10-07：优先复用"刚刚验证输入框"那张图 —— 同一时刻、同一个屏，
    #   发送键就在那张图上，再截一次纯属白等（这是用户报"20 秒"里的固定开销之一）。
    path = img or (_LAST_INPUT_SHOT.get("path") or "")
    if path and os.path.exists(path):
        print("[wxbridge] 复用上一张截图定位发送按钮（省一次抓帧）", flush=True)
    else:
        _seq += 1
        path = img or "/tmp/wxbridge-sendbtn-%d.jpg" % _seq
    try:
        if not os.path.exists(path) and not shot_wechat(path):
            return SEND_BTN_POS[0]
        crop = "/tmp/wxbridge-sendbtn-crop.jpg"
        if _run(["convert", path, "-crop", SEND_BTN_CROP, crop], 20) is None:
            return SEND_BTN_POS[0]
        res = wxreader.ocr_call(crop)
        for line in (res[0] if res and res[0] else []):
            if str(line[1]).strip().replace(" ", "") != "发送":
                continue
            box = line[0]
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            pos = (int(sum(xs) / len(xs)), int(sum(ys) / len(ys)) + SEND_BTN_CROP_Y)
            print("[wxbridge] OCR 认到发送按钮 %s" % (pos,), flush=True)
            return pos
    except Exception as exc:
        print("[wxbridge] 发送按钮定位失败：%r（回退候选坐标）" % (exc,), flush=True)
    return SEND_BTN_POS[0]


# ★ 2026-10-10：这条 `/send` 的失败是不是「你占用着手机 / 在打字，我**没抢**键盘」。
#   是的话：① 机器人可以稍后补发；② **不计入熔断** —— 否则你打几条字，机器人
#   就会连着失败三次、自己「连续 3 次发送异常，已自动停止」（发图那条路
#   2026-10-09 踩过同样的坑，见 bot.py 里 last_image_retryable 那段）。
_SEND_RETRYABLE = [False]


def send_text(text, input_retries=4, force=False):
    """往当前会话发一句话（对外入口）。实现见 ``_send_text_inner``。

    ★ 2026-10-09 用户要求「浮窗要有头有尾」：原来"输入框内容对不上""字没进去"
      这些失败路径**一个字都不浮**，用户看到的浮窗就永远停在"要借屏了"上
      （他原话：「浮窗一直在说他要抢键盘了，但一直没更新下一步」）。
      所以失败统一在这里收口：浮一条带原因的提示。
    """
    _FAIL_POPPED[0] = False
    ok, why = _send_text_inner(text, input_retries=input_retries, force=force)
    if not ok and not _FAIL_POPPED[0]:       # 内层（如悬浮小窗拦截）已经弹过就不再补一条
        _hud_fail_notice((why or "没发出去")[:44], seconds=8,
                         title="微信机器人 · 这条没发出去", detail="下一轮会自动重试")
    return ok, why


def _send_text_inner(text, input_retries=4, force=False):
    """发一句话到当前会话 —— **只点一次「发送」，绝不重复发**。

    ⚠️ 2026-10-03 血泪（群里连出三个「？」就是这个函数干的）：
    老版本把「点发送」放在重试循环里，而验证条件是 `发送后 stddev < 0.02`。
    实测发送后稳定停在 **0.0629**（字其实已经出去了，只是没降到阈值以下），
    于是每次都被判失败、重来一次 —— 一次逻辑回复在群里变成三条一模一样的消息。
    **发送是不可重做的动作：验证再不确定，也严禁重发。**
    所以现在的规矩：
      · 输入阶段失败（字没进去 / 基线读不到）可以重试，最多 input_retries 次；
      · 「点发送」全局只执行一次，之后无论验证结果如何都**不再发送**；
      · 验证用「发送前基线 → 发送后回落」的相对判据，而不是写死的 0.02，
        输入框里 OCR 不到字就算成功（明确读到残留字才记 False）。
    """
    text = (text or "").strip()
    if not text:
        return False, "没有内容"
    _SEND_RETRYABLE[0] = False        # 每次发送先清掉上一次的标记（见模块里那个说明）
    ensure_wechat_front()
    if not wechat_front():
        print("[wxbridge] 微信不在前台，拒绝发送", flush=True)
        return False, "微信不在虚拟屏前台"
    # ★ 2026-10-09（用户要求「先弹窗，我再放手，放手之后他再抢」）：
    #   原来这里是 `wait_user_idle()` —— 它只等 USER_HANG_SECONDS(2s) 就"照做"，
    #   而且**不弹任何预告**；紧接着的 `ime_set(IME_OURS)` 会把用户在物理屏上打字的
    #   键盘当场抢走（= 他说的"抢键盘"），弹窗却排在点输入框失败之后。
    #   现在统一走开场协商：先弹预告 → 等 2 秒 → 你让位就不抢，不让位才按一次 HOME。
    #   ⚠️ 这段（以及下面直到 ime_set 之前）**任何会打扰用户的事都不做**。
    # ★ 2026-10-11 事故拦截（用户报「机器人把之前的话发给我妈了」）：**屏幕上有两个
    #   微信主窗口就拒发**。会话核对只读顶部标题条，小窗会顶着另一个会话浮在上面，
    #   核对永远通过，而输入法连接挂的是小窗 —— 这一条就是这么发进私聊的。
    #   代价只是"这条不发"（挂补发队列，小窗关掉后自动补），远好过发错人。
    try:
        _nwin = wx_main_windows()
    except Exception:
        _nwin = None
    if _nwin is not None and _nwin > 1:
        print("[wxbridge] 屏幕上有 %d 个微信主窗口（多半有悬浮小窗）→ 拒发，绝不冒险"
              % _nwin, flush=True)
        _hud_fail_notice("检测到微信悬浮小窗 · 这条没发（怕发错人）", seconds=12,
                         title="微信机器人 · 已拦下", detail="关掉那个小窗，下一条就能发")
        _SEND_RETRYABLE[0] = True
        return False, ("屏幕上有微信悬浮小窗（%d 个会话窗口）：会话无法确认，"
                       "这条没发（关掉小窗后会自动补）" % _nwin)
    _pre_ok, _pre_prev = preflight_yield(force=force)
    if not _pre_ok:
        _SEND_RETRYABLE[0] = True     # ★ 占用类：我没抢你的键盘 → 可补发、不计熔断
        # ★ 2026-10-11：锁屏这条单独给个说法 —— 不是"你在用手机"，而是
        #   "手机锁着，等亮屏解锁后再说"（锁屏下输入法拿不到输入连接，
        #   硬发也进不去字；用户 2026-10-11 拍板：锁屏时机器人整体挂起）。
        try:
            if screen_awake() is False:
                return False, "锁屏中：这条先攒着，亮屏解锁后自动补发"
        except Exception:
            pass
        if _PF_WHY[0]:
            _w, _PF_WHY[0] = _PF_WHY[0], ""
            return False, "抢不了屏（%s）：这条先攒着，下一轮自动重试" % _w
        return False, ("你在用手机：这条先攒着（收起键盘 / 切走 / 锁屏，"
                       "或在面板上点「抢一下再发」）")
    _T0 = time.time()
    _timing = os.environ.get("WXBRIDGE_TIMING", "0") not in ("0", "", "false", "no")
    def _tk(label):
        nonlocal _T0
        if _timing:
            print("[timing] send | %-18s %5.2fs" % (label, time.time()-_T0), flush=True)
        _T0 = time.time()
    saved = current_ime()
    _released_phys = _pre_prev        # preflight 里真抢过的话，交给 finally 切回来
    _rebuilt_ime = False
    # ★ 2026-10-14：输入连接重建只做一次（见下面"没聚焦"分支）
    _ime_reswitched = False
    # ★ 2026-10-09（用户拍板「全改」）：抢也**只抢一次** —— 抢过还拿不到焦点就该
    #   认输（这一条留给下一轮），而不是像原来那样掉进"等你让位"分支白等 30 秒。
    _grab_tried = bool(_pre_prev)
    try:
        # ★ 2026-10-08：先确认"还在目标会话里"再动手。
        #   AndroidSender.is_ready() 只看桥接活没活，会话被切走它照样说 ok ——
        #   那样这条就会发到别的会话去。标题 OCR 常被读残（实测「...10」「r...10」）。
        # ★ 2026-10-08 审查修复（S1）：这里原来对"读不出标题"是 **fail-open**
        #   （【读不出就不拦】）—— 而发错群是这套系统里唯一不可逆的事故面，
        #   保守方向必须是 fail-closed。现在改成三段：
        #     ① 读到别的会话名        → 拒发（原行为，保留）
        #     ② 读不出 / 读残         → **先主动切回目标群，再核对一次**
        #     ③ 切回后仍读不出        → 拒发（宁可漏这一条，也不能发错群）
        # ★ 2026-10-16（用户报「前一条发出去了，后一条就不确定是不是在目标群」）：
        #   ①②③ 的**判据从"读一帧"改成"连读 N 帧投票"**（见 `_title_vote`）——
        #   现场铁证是同一秒里 `/send` 读不出、而它自己调用的 `open_chat` 却判定
        #   "已经在目标会话"，说明读空是随机的瞬时故障。任一帧读到目标群即放行；
        #   读到别的会话立刻拒发；全读不出才走切群那一步。安全边界没变
        #   （仍要求至少一帧铁证看着是目标群）。
        _want = _default_chat()
        if _want and wxreader is not None:
            try:
                _ok, _cur, _notes = _title_vote(_want, keep=True)
                if _ok is False:
                    print("[wxbridge] /send 当前会话「%s」≠ 目标「%s」→ 这条不发（%s）"
                          % (_cur, _want, "；".join(_notes)), flush=True)
                    return False, "当前不在目标会话（读到 %r），这条没发" % _cur
                if _ok is None:
                    # ② 连读 N 帧都读不出/读残 → 先切回目标群（open_chat 有自己的严判据）
                    print("[wxbridge] /send 连读 %d 帧都没读到标题（%s）"
                          "→ 先切回目标群再核对"
                          % (_TITLE_VOTE_FRAMES, "；".join(_notes) or "无"), flush=True)
                    _back = False
                    try:
                        _back = bool(open_chat(_want, verify=True))
                    except Exception as exc:
                        print("[wxbridge] /send 切回目标群异常：%r" % (exc,), flush=True)
                    if not _back:
                        _ok2, _cur2, _notes2 = None, "", []
                    else:
                        _ok2, _cur2, _notes2 = _title_vote(_want, keep=True)
                    if _ok2 is True:
                        print("[wxbridge] /send 已切回目标群「%s」，继续发送" % _cur2,
                              flush=True)
                    else:
                        # ③ 切了也认不出来 —— 拒发（不可逆的错发比漏发严重得多）
                        print("[wxbridge] /send 切回后仍无法确认会话（%r，切群=%s；%s）"
                              "→ 这条不发"
                              % (_cur2, _back, "；".join(_notes2) or "无"), flush=True)
                        return False, "无法确认当前会话是不是目标群，这条没发"
            except Exception as exc:
                # 校验本身出错也不再"忽略继续发"：这是发送前的最后一道闸门
                print("[wxbridge] /send 会话校验异常 → 这条不发：%r" % (exc,), flush=True)
                return False, "会话校验异常，这条没发"
        if _timing:
            _tk("会话校验+切群")
        _FILLED_SHOT = [""]                  # ★ 注入后那张图的路径（循环内写，循环外用）
        base = input_stddev()                # 发送前基线（此刻输入框应为空）
        if _timing:
            _tk("读输入框基线")
        # ★ 2026-10-07：空框实测 std≈0.015，但因为"串输入"等原因框里可能**本来就
        #   有残留** —— 那样 base 虚高会把阈值抬高、把"输入成功"误判成"空"。
        #   所以给 base 封顶：明显高于空框就按空框算，别让残留毒化判据。
        if base is not None and base > 0.08:
            print("[wxbridge] 注意：发送前输入框基线偏高（%s）—— 框里可能已有内容，"
                  "按空框算阈值" % base, flush=True)
            base = 0.03
        print("[wxbridge] /send 基线 stddev=%s" % base, flush=True)
        # 2026-10-04：输入法是**冷启动**时第一次 commit 常被系统丢掉 ——
        # 零点文案第一句就是这么失败的（两次输入都"还是空的"，直接放弃）。
        # 先空预热一次输入法，把切换/唤醒的时间挪到正式重试之前。
        ime_set(IME_OURS)
        time.sleep(1.5)
        # ⚠️ 2026-10-05 踩坑（别再加回来）：这里曾加过「一开头就 physical_input_taken()
        #    → release_physical_input()」，想把 3 次重试都留给正事。结果因为用户在面板上
        #   （物理屏）时几乎**每次都判定成"焦点被占"**，于是机器人每发一条消息都要
        #   `keyevent 3`（HOME）把 DSH 面板踢回桌面，发完再 `am start` 拉回来 ——
        #   用户看到的是「DSH 一直在返回桌面」，而且反复拉起 DSH 会让麦克风指示灯闪。
        #   当时的失败其实另有原因：**adb 断了**（桥接退化成命令守护）。
        #   现在的策略（用户 2026-10-05 拍板：「只在发不出去时短暂抢一下」）：
        #   循环前**不**主动松手；等 `input_focused()` 真的失败，才按下面的顺序
        #   优先松手（对症）、其次重建输入法。
        for attempt in range(1, input_retries + 1):
            # ★ 2026-10-08：tap 之前先看微信是不是正在开麦克风。
            #   录音里录的是**外放的抖音新闻**，转成文字会顶进输入框、被当成本次要发的话。
            _rec, _line = wechat_recording()
            if _rec:
                print("[wxbridge] ⚠️ tap 前就发现微信在录音：%s" % _line, flush=True)
                stop_wechat_recording()      # ★ BACK 停不掉，改点切换按钮
                _clear_input_box()
                return False, "微信正在录音（语音输入被误触），已停录并清空，这条没发"
            # ★ 别在「按住说话」上 tap —— 那一下等于按住麦克风（一样开麦）
            if in_voice_mode():
                leave_voice_mode()
            tap(*INPUT_BOX_POS[(attempt - 1) % len(INPUT_BOX_POS)])
            time.sleep(1.2)
            # ★★ 2026-10-08 最关键的一道闸门：tap 之后**立刻**查录音。
            #   万一坐标（或微信改版后的布局）又把麦克风套进去了，此刻微信刚开始录 ——
            #   趁它还没转文字，马上停录 + 清空。绝不能走到"点发送"那一步：
            #   那一下点的不是发送、而是**停止录音**，语音文字就是那一刻顶进输入框的。
            _rec2, _line2 = wechat_recording()
            if _rec2:
                print("[wxbridge] ⚠️ 点输入框把微信点开麦了（坐标压到麦克风）：%s" % _line2,
                      flush=True)
                stop_wechat_recording()
                _clear_input_box()
                return False, "点输入框误触了微信语音（已停录清空），这条没发"
            # ★ 2026-10-04 晚修：这里原来还有一次 ime_set(IME_OURS)，
            # 而**点完输入框再切输入法会让微信输入框立刻失焦** ——
            # 后面的 commit 就落空了。顺序必须是：ime_set（上面已做）
            # → tap → 直接 commit，中途绝不再碰输入法。
            # ★ 2026-10-09 再修一层：判定从"tap 后 1.2 秒查一次"改成**轮询等**
            #   （`_wait_input_focus`）。实测输入连接从"物理屏的陈旧窗口"
            #   （Launcher 的 DecorView / 面板的 WebView）交到虚拟屏微信手上要花点时间：
            #   手动 tap 之后 2 秒才查到 `mServedView=…MMEditText`，而这里 1.2 秒就
            #   下结论 → 连着三次换坐标重点、整条作废。日志里这种
            #   「点完输入框还没聚焦」已经攒了 25 条（11:59 那次就是图发出去了、
            #   配文没跟上）。
            if not _wait_input_focus(4.5):
                # ★ 2026-10-14（排查「字没能进到输入框」，3 天 15 次）：3.0 秒是
                #   **贴着实测下限**定的（下面那段注释说"要 2 秒上下"）—— 慢一点就
                #   三次全超时、整条放弃，日志里连着三行「点完输入框还没聚焦」。
                #   实证（10-14 17:33）：那一轮 3 次全灭，紧接着补发时**第 1 次也没**
                #   聚焦、第 2 次才成 —— 说明只是"需要多给一次机会/多等一会儿"，
                #   不是真的不可用。所以：等待 3.0 → **4.5 秒**、重试 3 → **4 次**。
                #   ⚠️ 代价只在**真的没聚焦**时才付（聚焦了第一次查就返回，约 0.3s），
                #   最坏 4×4.5≈18 秒，远小于 /send 的 180 秒超时（见 android.py）。
                # 2026-10-05：**两样折腾都不要做，直接换坐标重点**（都默认关，见文件顶部）：
                #   · 物理屏占着输入焦点 → 不 HOME、不 am start（会把 DSH 弄黑屏）；
                #   · 输入框点不进去 → 不 disable/enable DshIme（那是 DSH 自己的输入法，
                #     重建它会让 DSH 白屏、输入框起不来 —— 用户 2026-10-05 报
                #     「被 @ 时 DSH 白屏/黑屏、打不开新对话」就是这么来的）。
                # 重试只是换个输入框坐标再点一次，点不进就放弃这一条，下一轮再说。
                # ★ 2026-10-09（用户要求「先弹窗，我再放手，放手之后他再抢」）：
                #   预告 / 让位 / 抢屏**全部挪到 preflight_yield()** —— 它在 send_text
                #   一开头、`ime_set()` **之前**就做完了。老顺序是"先把你的键盘切走
                #   （ime_set），点输入框失败之后才弹窗告诉你" —— 用户报的就是这个。
                #   这里只在"发送途中才被占"（他中途开始打字）时补一次协商；
                #   一条消息最多抢一次（`_grab_tried`），别每次重试都折腾他的屏。
                if yield_reason() and not _grab_tried:
                    _grab_tried = True
                    _ok2, _prev2 = preflight_yield(force=force)
                    if not _ok2:
                        return False, ("你在用手机：微信抢不到输入焦点，这条没发"
                                       "（收起键盘 / 切走 / 锁屏，或在面板上点"
                                       "「抢一下再发」）")
                    if _prev2:
                        _released_phys = _prev2
                # ★ 2026-10-14（用户报「字没能进到输入框」的冷启动场景）：重试前
                #   **切走再切回输入法** = 重建 IME 连接。实证 21:50 —— 微信刚冷启动
                #   时 4 次按坐标重点**全失败**，而紧接着挂补发的那一轮第 1 次就成了：
                #   两轮唯一的差别，就是"新一轮开头重新切过一次输入法"。
                #   ⚠️ 两条不能走的路：① `rebuild_ime()` 是 disable/enable，会让
                #   **DSH 白屏**（2026-10-05 的坑，见它的说明）；② 再 `ime_set(IME_OURS)`
                #   一次没用 —— `ime_set` 见当前已经是它就直接 return（白做）。
                #   ⚠️ 只在**没聚焦**时做才安全：此刻输入框本来就没攥着焦点，切输入法
                #   不会把它"弄失焦"（那条禁忌针对的是 tap 之后别再碰输入法）。
                if not _ime_reswitched:
                    _ime_reswitched = True
                    # ★ 2026-10-15 修正（用户报「发着发着定位不到输入框，然后连续四次
                    #   一下屏」）—— ⚠️ **绝不能"切回用户的输入法"**：切回讯飞会让它把
                    #   键盘弹到**物理屏**，而物理屏前台常常正是 DSH，于是系统判定
                    #   「物理屏占着输入焦点」→ 机器人**去抢屏（按 HOME 把 DSH 踢回
                    #   桌面）+ 补一个 BACK** → 用户屏幕上连闪。
                    #   实测 2026-10-15 00:01~00:12，每一次失败重试都触发一遍，日志：
                    #     重建输入连接（切走 → 切回输入法）→ 输入法 DshIme → FlyIME
                    #     → 物理屏占着输入焦点（com.dshmobile.probe）→ 按 HOME → 补 BACK
                    #   改成**直接重发一次 `ime set`**（绕过 ime_set 的幂等短路）：
                    #   不换输入法、不弹键盘，只是让系统重建一次输入连接。
                    print("[wxbridge] /send 重建输入连接（重新 ime set 一次）…", flush=True)
                    adb("ime set %s" % IME_OURS, 20)
                    time.sleep(0.4)
                if REBUILD_IME and not _rebuilt_ime:
                    _rebuilt_ime = True
                    rebuild_ime()
                print("[wxbridge] /send 第%d次：点完输入框还没聚焦（焦点不在微信"
                      "输入框），换个坐标重点" % attempt, flush=True)
                continue
            commit(text, do_clear=True)
            time.sleep(1.4)
            # ★ 2026-10-07：把这张"注入后"的图**固定下来**，下面定位发送按钮直接复用
            #   —— 不然又白截一张（§46.24 的复用因为判定分支不同而没生效过）。
            _FILLED_SHOT[0] = "/tmp/wxbridge-filled-%d.jpg" % attempt
            filled, why = _input_filled(base=base, img=_FILLED_SHOT[0])
            if _timing:
                _tk("输入文字(第%d次)" % attempt)
            print("[wxbridge] /send 第%d次输入: %s（%s）"
                  % (attempt, "有字" if filled else "还是空的", why), flush=True)
            if filled:
                # ★ 2026-10-08：光「有字」不够 —— 必须确认框里就是我们要发的那条。
                #   否则串进来的别人的文字（抖音直播字幕等）会被当成回复发进群，
                #   见 _input_text_ok 的说明。
                _got = _ocr_input_box(_FILLED_SHOT[0], shoot=False, want=text)
                if not _input_text_ok(_got, text):
                    print("[wxbridge] /send 输入框内容对不上：要发「%s」（末尾「%s」）"
                          "，框里读到「%s」→ 已清空、不发送"
                          "（窗口 %s ｜ 框里是别人的字，或者只露了末尾几个字没读全）"
                          % (text[:24], text[-12:], _got[:24],
                             _LAST_INPUT_CROP[0]), flush=True)
                    _clear_input_box()
                    return False, ("输入框里的字不是要发的（读到 %r），已清空、没有点发送"
                                   % (_got[:20],))
                break
        else:
            print("[wxbridge] /send 放弃：字始终没能进到输入框（点输入框/输入法没生效）",
                  flush=True)
            # ★ 2026-10-07：**必须清空**才走人 —— 见 _clear_input_box() 的说明，
            #   否则输入框里的残留会被别人一个回车发到群里。
            _clear_input_box()
            return False, "字没能进到输入框（点输入框/输入法没生效），这条没发"

        if _timing:
            _tk("输入框内容二次校验")
        # ★ 2026-10-08：点「发送」之前**最后再查一次录音**。
        #   录音中时这一下点的不是"发送"、而是"停止录音" —— 微信会把录到的
        #   语音转成文字塞进输入框，别人的话就这么进了群（事故就是这么发生的）。
        _rec3, _line3 = wechat_recording()
        if _rec3:
            print("[wxbridge] ⚠️ 临发送前发现微信在录音：%s → 不点发送，改为停录 + 清空"
                  % _line3, flush=True)
            stop_wechat_recording()
            _clear_input_box()
            return False, "临发送前发现微信在录音（语音被误触），已停录清空，这条没发"
        if _timing:
            _tk("定位发送按钮")
        tap(*find_send_btn(_FILLED_SHOT[0] or None))   # ★ 复用上面那张图定位发送键
        if _timing:
            _tk("点发送")
        # ★ 2026-10-08 晚：点完发送**不再死等 1.7 秒**。实测大多数情况输入框
        #   0.5 秒内就空了（消息已上屏），白等的那 1.2 秒纯属浪费（占单条 11.37s 的 10%）。
        #   改成轮询：先 0.5s 看一眼、空了就走；没空再补两次，**上限仍是 1.7s** ——
        #   最坏情况与原来的 sleep(1.7) 完全一致，所以不会因"等得不够"而误判。
        #   （截图仍只由 input_stddev 做一次，两个判据共用，见下。）
        _after = "/tmp/wxbridge-after-send.jpg"
        sd2, ocr2 = None, ""
        for _w in (0.5, 0.6, 0.6):        # 累计 1.7s，与原 sleep(1.7) 等价
            time.sleep(_w)
            sd2 = input_stddev(_after)     # ★ 这一次截图，两个判据共用
            if sd2 is None:
                break                      # 截图失败 → 交给下面的判据按"没读到"处理
            ocr2 = _ocr_input_box(_after, shoot=False, wide=False)   # ★ 只看输入框
            if (base is not None
                    and sd2 < base + STDDEV_FILLED * 0.6 and not ocr2):
                break                      # 输入框已经空了 → 不必再等
        # 判据按顺序（对应 2026-10-03 实测：空框 0.071 / 填「？」0.163 / 发出后 0.063）：
        #   ① 读数明显落回基线附近（含噪），且输入框里 OCR 不到任何字 → 已发出；
        #   ② 读数低于「基线+半个填充阈值」这个中点 → 内容已经离开输入框 → 已发出；
        #   ③ 其余：只要 OCR 没读到字，就不认定为未发出（宁可乐观，绝不重发）。
        near_base = (sd2 is not None and base is not None
                     and sd2 < base + STDDEV_FILLED * 0.6)
        below_mid = (sd2 is not None and base is not None
                     and sd2 < base + (STDDEV_FILLED - base) * 0.5)
        ocr_clear = not ocr2
        if ocr_clear and (near_base or below_mid):
            print("[wxbridge] /send 发送后 stddev=%s（基线 %s）→ 成功（输入框已空）"
                  % (sd2, base), flush=True)
            _hud_sent(text)                      # ★ 2026-10-07 用户要求：成功也浮窗
            return True, ""
        if ocr_clear:
            print("[wxbridge] /send 发送后 stddev=%s（基线 %s）→ 判为已发出"
                  "（读数有漂移但输入框 OCR 不到字；只发一次，不再重试）"
                  % (sd2, base), flush=True)
            _hud_sent(text, kind="已发（不确定）")   # ★ 同上
            return True, ""
        print("[wxbridge] /send 明确失败：发送后输入框里仍有字 %r（stddev=%s，基线 %s）"
              " —— 这一条没发出去" % (ocr2, sd2, base), flush=True)
        _clear_input_box()      # ★ 2026-10-07：同上，别把残留留给下一个回车
        return False, "点了发送但输入框里还有字（%r），这条没发出去" % (ocr2,)
    finally:
        # ★ 2026-10-08 晚：补上收尾计时 —— 从「点发送」到自检出结果
        #   （含固定的 1.7 秒等待 + 一次截图 + 一次 OCR）。这是发送耗时里
        #   最后一段没量过的。放 finally 里是因为成功/失败/异常三条路都经过它。
        if _timing:
            _tk("发送后自检（含 1.7s 等待）")
        restore_physical_app(_released_phys)
        # ★ 2026-10-09 用户要求（原话「我不知道抢到什么时候才结束」）：抢屏必须
        #   **有头有尾** —— 预告在 `_hud_grab_start()`，这里补**结束信号**：
        #   恢复前台之后再浮一条 + 震一下，他才知道可以拿回手机了。
        #   只有他本来真的占着键盘（抢过）时才说，没抢就不打扰。
        if _released_phys:
            # ★ 2026-10-14：统一走 `_hud_released()`（只浮窗、文案固定）。
            #   原来还先调 `_hud_alarm_off()` 撤通知栏那条 —— 桥接已不再发它了。
            _hud_released("发消息")
        ime_set(saved)


# ---------------------------------------------------------------- 发图
# 预览页底部按钮 + 分享界面 activity（636x1400 虚拟屏，2026-10-03 实测）
# ⚠️ intent 指向 …/ShareImgUI，但**微信内部会转**：真正显示的是
#    · 「选择聊天」= com.tencent.mm/.ui.mvvm.MvvmContactListUI
#    · 预览/转发页 = com.tencent.mm/.ui.transmit.MsgRetransmitUI
#    所以这里接受的是一组，不是单个（只认 ShareImgUI 会一直判成"界面没起来"）。
SHARE_UI = ("com.tencent.mm/.ui.mvvm.MvvmContactListUI",
            "com.tencent.mm/.ui.transmit.MsgRetransmitUI",
            "com.tencent.mm/.ui.tools.ShareImgUI")
SHARE_SEND_BTN = (425, 1258)
SHARE_CANCEL_BTN = (207, 1258)


def _norm(s):
    """去掉书名号/空格，便于比会话名。"""
    return (s or "").replace(" ", "").replace("「", "").replace("」", "").strip()


def _share_row_match(t, target):
    """「选择聊天」列表行的兜底匹配 —— 比 ``wxreader.match_title`` 严一档。

    2026-10-04 实测的误判：列表里有一行 OCR 成「158, 89...」（一个数字群，
    可能是「158, 89年...」之类），而 ``match_title`` 里有一条"没有汉字 + 有省略号
    就放行"的分支 —— 那是给**标题栏**被压成「...111)」准备的 —— 于是它把这一行
    当成了目标群：发图选中错行 (350,1303)，直到预览页校验才发现「发送给：158,」，
    一次发图白跑 50 秒。

    列表行的身份证据必须是**汉字**：微信在这一页截断时也会留下头部/尾部的连续
    汉字（实测「示例... (111人)」），所以要求「与目标共享一段连续 ≥2 个汉字」。
    纯数字/符号行（「158, 89...」「a妈1922...」）一律不认。
    """
    s = _norm(t).replace("…", "...")
    n = _norm(target)
    if not s or not n:
        return False
    if s in n or n in s:
        return True
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", s):
        if m.group() in n:
            return True
    return False


def _share_hit_strength(t, target):
    """这一行 OCR 文字给「就是目标会话」提供了多硬的证据 —— 返回与目标名匹配的**最长连续汉字数**。

    ★ 2026-10-09 加。为什么要按"最长连续汉字"分级，而不是看它是不是完整含
    目标名前 4 个字：本机 OCR（v5 server）把横排里的群名读成**两块** ——
    「示例群」+「名字..」，谁都不含完整的「示例群名」。但这两块各自与目标名
    对得上 3 个连续汉字，合起来就是"示例群名"，证据其实很硬；
    而反例（列表里那个数字群「158, 89...」、或别的群「示例疾苦」）最多只对得上
    2 个连续汉字（"示例"），差一档。
    """
    s = _norm(t).replace("…", "...")
    n = _norm(target)
    if not s or not n:
        return 0
    best = 0
    for m in re.finditer(r"[\u4e00-\u9fff]+", s):
        run = m.group()
        for i in range(len(run)):
            for j in range(len(run), i + best, -1):   # 只找比已知更长的
                if run[i:j] in n:
                    best = j - i
                    break
    return best


def _share_find_row(img, target):
    """在「选择聊天」页里找目标会话，返回
    **(列表行, 横排行, 列表命中强度, 横排命中强度)**（两个行都可以是 None）。

    命中强度（★ 2026-10-09 加，给预览页校验当依据）：
      · ``"exact"`` —— 与目标名对得上 **≥3 个连续汉字**（`示例群` / `名字`），硬证据；
      · ``"loose"`` —— 只对得上 2 个汉字（`示例`），弱证据；
      · ``""`` —— 没命中。

    实测的坑：
      · OCR 会把群名拆块（`「示例群名` + `享乐实录」(112人)`）→ 取目标名前 4 个字
        当特征串，谁包含它就算谁；
      · 列表**上方**还有一排「最近转发」横排，同样的名字会再出现一次 ——
        所以要先定位「最近聊天」标题，优先在它下面找（否则找「文件传输助手」
        会命中横排里的「助手」）。
      · ⭐ 2026-10-04 补：但**不能把横排整个抛弃**。实测目标群不在「最近聊天」
        首屏时（列表里只有 6 个最近会话），横排里却明明白白露着它的名字
        （`「示例群` / `名字...`），老代码跳过横排 → 翻页翻到底也找不到 → 发图
        直接放弃。实测点击横排项**一样会进预览页**（activity =
        HalfScreenTransparentActivity），后面那层「发送给：」校验照样能拦住误选，
        所以把横排命中当**次选**返回是安全的。
    """
    key = _norm(target)[:4]
    if not key:
        return (None, None, "", "")
    words = _ocr_words(img)
    y_start = 150
    for t, cx, cy in words:
        if "最近聊天" in _norm(t):
            y_start = cy
            break
    best = horiz = None
    best_why = horiz_why = ""
    for t, cx, cy in words:
        # 精确特征串之外再挂一道行匹配兜底：这一页的群名同样会被系统截断成
        # 「示例... (111人)」（2026-10-03 的失败截图就是这个形态），
        # 拿「示例群名」四个字去比是永远比不上。
        # ⚠️ 这里**不能**直接用 wxreader.match_title：它对「无汉字 + 有省略号」的行
        #    会放行，2026-10-04 就这么误选中了列表里的「158, 89...」。
        if key in _norm(t):
            why = "exact"
        elif _share_row_match(t, target):
            # ⚠️ 不能一律记成 "loose"：`_share_row_match` 只要求 2 个连续汉字，
            #   而实测横排会读成「示例群」这种 3 字块 —— 那已经够硬了（见
            #   `_share_hit_strength` 的说明）。
            why = "exact" if _share_hit_strength(t, target) >= 3 else "loose"
        else:
            continue
        if cy > y_start:                     # 「最近聊天」列表里（首选）
            if best is None or cy < best[1]:
                best, best_why = (cx, cy), why
        else:                                # 「最近转发」横排里（次选）
            if horiz is None or cy < horiz[1]:
                horiz, horiz_why = (cx, cy), why
    return (best, horiz, best_why, horiz_why)


def _share_preview_target(img=None, words=None):
    """预览页里「发送给：」下面那一行的会话名。取不到给空串。

    words 可以传进来复用（同一张图只 OCR 一次，省 3~5 秒）。
    """
    words = words if words is not None else _ocr_words(img)
    y0 = None
    for t, cx, cy in words:
        if "发送给" in _norm(t):
            y0 = cy
            break
    cands = [w for w in words
             if (y0 is not None and y0 + 20 < w[2] < y0 + 220)
             or (y0 is None and 900 <= w[2] <= 1010)]
    if not cands:
        return ""
    cands.sort(key=lambda w: (-len(w[0]), w[2]))
    return cands[0][0]


# 预览页那个名字两边的装饰：引号 + 微信自己补的截断省略号。
_PREVIEW_DECOR = "「」『』【】《》\"'“”‘’ \t\r\n.…·"


def _preview_name_unreadable(shown, target):
    """预览页那行名字**信息量不足以自证身份**，但残留的字与目标不矛盾？

    ★ 2026-10-09 加（用户报「看看腹肌发不出去」）。病根是微信自己的 UI：
    这台机器上虚拟屏只有 636px 宽、字号又大，分享预览页那一行只画得下一个字 ——
    实测截图上是「「人…」，OCR 读成「人.」。而 `wxreader.match_title` 要求
    「省略号 ≥2 个点」才认截断（`.` 只有 1 个 → 不匹配）→ 每一次都判「≠ 目标」
    → 取消。同一次操作里 OCR 偶尔把它读成「人...」（3 个点）就侥幸能过 ——
    所以这个群是"有时能发、有时发不出去"。

    判据故意收紧到三条同时成立：剥掉装饰后 ① 非空；② **只剩 ≤2 个字**；
    ③ 是目标名的前缀。也就是"屏幕上根本没给出可辨识信息，但给出的一点与目标一致"。
    ⚠️ 它**不能单独当家**：调用方还要求上一步在「选择聊天」页里是 ``exact``
    精确命中（见 `send_image`）—— 两条证据合起来才放行。
    实测反例：「按住说出你的」（OCR 把输入框占位符当成了会话名，6 个字）照样被否掉。
    """
    core = _norm(shown).strip(_PREVIEW_DECOR).strip()
    if not core or len(core) > 2:
        return False
    return _norm(target).startswith(core)


def _share_btn(words, names, fallback):
    """在分享页底部按**文字**找按钮，找不到才回落到兜底坐标。

    2026-10-03 实测微信有两种分享布局：
      · 新版**半屏** HalfScreenTransparentActivity —— 按钮在 y≈1155（发送 x≈497）；
      · 老版全屏预览 MsgRetransmitUI —— 按钮在 y≈1258（发送 x≈425）。
    老代码把 (425,1258) 写死，半屏下这一点戳在按钮下方的空白里 → 图根本没发出去，
    界面还留在半屏页（表现为「明明说预览通过、却没发出来」）。所以改成认字点击。
    """
    cands = []
    for t, cx, cy in words:
        nt = _norm(t)
        if cy < 900:                 # 「发送给：」在上面，绝不能点
            continue
        if any(n in nt for n in names):
            cands.append((cy, cx))
    if cands:
        cands.sort()                 # 取最靠下的那个（底部按钮）
        return (cands[-1][1], cands[-1][0])
    return fallback


def _green_btn(img, y0=900, y1=1320):
    """半屏预览页那个「发送」按钮**没有文字**，只有一个微信绿的长圆角块。

    2026-10-04 实测（现场图 /tmp/wxbridge-share-done.jpg）：按钮是纯绿色、
    OCR 一个「发送」都读不到，于是 `_share_btn` 用了兜底坐标 (497,1160) ——
    而按钮实测 x 范围 **506~559**、y 980~1197，那一点正好戳在按钮左边缘外，
    结果是「预览校验通过、点了发送、图根本没发出去、界面还挂在半屏页」，
    机器人也一直待命。所以改成按颜色找：微信绿 ≈ #07C160。

    只在下半屏找（列表页/「发送给：」那一行的绿色头像都在 y<900）。
    返回中心 (x, y)；找不到给 None。
    """
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return None
    try:
        a = np.asarray(Image.open(img).convert("RGB"), dtype=np.int16)
    except Exception:
        return None
    if a.ndim != 3 or a.shape[0] <= y0:
        return None
    a = a[y0:min(y1, a.shape[0])]
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    m = (g > 150) & ((g - r) > 60) & ((g - b) > 60)
    if int(m.sum()) < 500:              # 太小就不像个按钮（实测约 1.1 万像素）
        return None
    ys, xs = np.nonzero(m)
    return (int(xs.mean()), int(ys.mean()) + y0)


def _cancel_btn(fallback):
    """失败收尾用：截一张图认「取消」，认不到就用兜底坐标。"""
    img = "/tmp/wxbridge-share-abandon.jpg"
    if not shot_wechat(img):
        return fallback
    act = screen_activity().lower()
    half = "halfscreen" in act
    return _share_btn(_ocr_words(img), ("取消",),
                      (265, 1090) if half else fallback)


def _default_chat():
    """机器人真正要待着的会话（config.yaml → target.group）。

    只用来给 `abandon_share` 当第二兜底：发图失败、目标会话又切不回去时，
    至少把界面送回机器人认得的群，避免它永久「待命中」。读不到就返回空串。
    """
    # ⚠️ 2026-10-13：v2.0.0 把用户数据搬到了 ../data/，这里原来写死
    #    `本文件目录/config.yaml` → 每次调用都 FileNotFoundError、兜底**彻底失效**
    #    （只在日志里刷一行错误）。后果不是"少个兜底"：发图一旦失败，界面会留在
    #    「选择聊天」页切不回群，机器人此后每轮读到的标题都是它 → **永久待命、
    #    什么消息都不回**（2026-10-03 踩过同款故障）。现在优先走
    #    `wxauto.paths.DATA_DIR`，并保留 app/ 老布局的回落。
    cands = []
    try:
        from wxauto.paths import DATA_DIR
        cands.append(os.path.join(str(DATA_DIR), "config.yaml"))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    last_exc = None
    for cfg in cands:
        try:
            with open(cfg, encoding="utf-8") as fh:
                for line in fh:
                    s = line.split("#", 1)[0].strip()
                    if s.startswith("group:"):
                        v = s.split(":", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        print("[wxbridge] 读 config.yaml 取目标群失败：%r" % (last_exc,), flush=True)
    return ""


def abandon_share(target="", fallback=""):
    """发图流程任何一步失败后的收尾：退出分享界面，并把会话切回能聊天的地方。

    ⚠️ 老版本这里只 `tap(*SHARE_CANCEL_BTN)` 就 return —— 取消按钮点空时界面会
    **留在「选择聊天」页**，机器人此后每轮读到的标题都是「选择聊天」，于是永久
    「待命中」什么都不回（2026-10-03 亲身踩：12:51 发图失败 → 13:00 一直装死，
    最后是用户反复点面板「启动」才发现的）。

    `fallback` 是第二兜底（机器人自己的目标群）：目标会话名找不到/切不回去时，
    至少也要把界面送回机器人认得的群里 —— 否则它照样永久待命（2026-10-03 实测：
    切回不存在的会话失败后界面停在搜索页，机器人读到的标题就是空的）。
    """
    for attempt in range(3):                 # 取消按钮点空会很常见，多试几次
        tap(*_cancel_btn(SHARE_CANCEL_BTN))
        time.sleep(1.2)
        act = screen_activity()
        if act not in SHARE_UI and "halfscreen" not in act.lower() and on_chat_page():
            print("[wxbridge] 发图收尾：第%d次点取消后退出了分享界面" % (attempt + 1,),
                  flush=True)
            break
    act = screen_activity()
    if act not in SHARE_UI and "halfscreen" not in act.lower() and on_chat_page():
        # ⚠️ read_title(img) 需要图片参数；老代码这里写的是 read_title()，一旦走到
        #    这个分支就 TypeError，HTTP 连接直接断（2026-10-04 发图回归时踩到）。
        _t = "/tmp/wxbridge-abandon-title.jpg"
        _title = ""
        if wxreader is not None and shot_wechat(_t):
            _title = wxreader.read_title(_t) or ""
        if not target or wxreader.match_title(_title, target):
            return True                      # 已经回到目标会话，不必再折腾
    if not back_to_list():
        print("[wxbridge] 发图收尾：没能退回会话列表（当前 %s）" % screen_activity(),
              flush=True)
    ok = False
    if target:
        ok = open_chat(target)              # ★ 无条件切回目标群：否则机器人永久待命
        print("[wxbridge] 发图收尾：切回「%s」%s" % (target, "成功" if ok else "失败"),
              flush=True)
    if not ok and fallback and fallback != target:
        ok2 = open_chat(fallback)
        print("[wxbridge] 发图收尾：兜底切回「%s」%s" % (fallback, "成功" if ok2 else "失败"),
              flush=True)
        ok = ok2
    return ok


def send_image(path, chat):
    """把本地图片发到指定会话 —— 走微信「分享图片」通道（2026-10-03 打通）。

    流程（先在文件传输助手验证通过）：
        am start SEND(image/png, file://…) -n …/.ui.tools.ShareImgUI
          → 「选择聊天」（activity = ShareImgUI）
          → OCR 找目标会话那一行 → 点它
          → 预览页（发送给：<会话> + 图片 + 取消/发送）
          → **校验「发送给」确实是目标**，不是就点取消
          → 点「发送」

    实测结论：
      · `file://` 直接可用，**不需要** content:// / MediaStore 绕一圈；
      · 预览页的会话名会被 OCR 读到，所以能做事前校验；
      · 发错群不可逆 —— 任何一步对不上都宁可放弃，绝不乱点。
    """
    # ⚠️ 一律转绝对路径：`file://` 后面跟相对路径（如 "表情包/OK.png"）在微信眼里是
    #    **非法 URI**，分享页根本起不来 —— 界面一直停在聊天页，8 轮循环全在 continue，
    #    最后报「选择列表里没找到」。2026-10-04 用相对路径测 HTTP 接口时踩到过一次。
    p = str(path or "").strip()
    if p:
        p = os.path.abspath(p)
    target = (chat or "").strip()
    if not p or not target or not os.path.exists(p):
        print("[wxbridge] /send_image 参数不对或图片不存在：%r %r" % (p, target), flush=True)
        return False, "参数不对或图片不存在"
    ensure_wechat_front()
    if not wechat_front():
        abandon_share(target, _default_chat())   # 收尾回群，别把界面丢在分享/搜索页
        # ★ 2026-10-09 晚：第三项 =「稍后可以再试」。这一条不是"这张图有问题"，而是
        #   "你正占用着手机的临时状态"（微信被拿到物理屏）—— 机器人据此把这张图挂进
        #   发图补发队列（见 wxauto/bot.py::_queue_resend_image），而不是直接丢掉。
        return False, "微信不在虚拟屏前台（你可能正在用手机）", True
    # 你在用手机的时候发图（要拉分享页、点来点去）最打扰人 —— 限频，每 15 秒最多一次
    # ★ 2026-10-11：跟发文字同一道保险 —— 屏幕上有两个会话窗口（悬浮小窗）时**不发图**。
    #   发图虽然是自己去列表里选会话（受当前会话影响小），但小窗顶在屏幕上时
    #   谁也不保证点下去会落到哪，宁可这一张不发（retryable=True，关掉小窗后补）。
    try:
        _nw = wx_main_windows()
    except Exception:
        _nw = None
    if _nw is not None and _nw > 1:
        print("[wxbridge] /send_image 屏幕上有 %d 个微信会话窗口（悬浮小窗）→ 这张不发"
              % _nw, flush=True)
        return False, "屏幕上有微信悬浮小窗（%d 个会话窗口）：这张图没发" % _nw, True
    # ★ 2026-10-13（用户报「发图片的时候机器人抢我键盘，怎么没有弹窗」）：
    #   发图这条链路**从来没有预告** —— 原来只有 `ensure_wechat_front()` +
    #   `wait_user_idle()`，而 `wait_user_idle()` 等满之后**不管你还挂不挂键盘
    #   都照做**（分享页一来就把前台抢走）。发文字那条路有 `preflight_yield()`
    #   （弹预告 + 等让位 + 抢不了就留给下一轮），发图却一路静默硬闯 ——
    #   这就是"没有弹窗"的根因。
    #   这里补一套**发图版**开场协商（口径 = 发文字那条路）：
    #     ① 先弹浮窗预告 + 震动，给你 IMG_HANG_SECONDS 秒；
    #     ② **到点直接抢** —— 不再检查你收没收手。
    #        ★ 2026-10-13 用户改口径（原话「三秒后直接抢，不用等我收手」）：
    #          一开始写的是"还在打字就放弃、下一轮补"，他不要那个 ——
    #          跟发文字的 `preflight_yield` 一致：提前打招呼，然后照做。
    _pf_now = time.time()
    if physical_ime_visible():
        if _pf_now - _IMG_ANNOUNCE_LAST[0] >= ANNOUNCE_GAP:
            _IMG_ANNOUNCE_LAST[0] = _pf_now
            # ★ 2026-10-14：原来这里**发两条重复的浮窗**（`_hud_fail_notice` 一条 +
            #   `notify_user` 一条，标题都是「要发图了」），还外加通知栏 —— 正是用户
            #   说的「两种通知方式」。现在只走统一的 `_hud_grab_start()`。
            try:
                _hud_grab_start("要借一下屏发张图：%.0f 秒后就动手（不改你打的字）"
                                % IMG_HANG_SECONDS,
                                detail="你可以先收一下键盘，让位更干净",
                                seconds=int(IMG_HANG_SECONDS) + 4)
            except Exception:
                pass
            _hud_buzz()
        wait_user_idle(hang=IMG_HANG_SECONDS)
        if physical_ime_visible():
            print("[wxbridge] /send_image 你还在打字 —— 按用户口径（3 秒后直接抢）照做",
                  flush=True)
    else:
        # 你没在打字：不弹预告、不打扰，但照样过一道短的让位（同原来）
        wait_user_idle()
    # ⚠️ 必须带 --display：不带的话这个 activity 会开在**物理屏**上，
    # 等于把微信从虚拟屏抢回用户手里（实测踩过：之后桥接就报
    # "物理屏正在用微信，不抢"，整个发图链路直接死掉）。
    # ★ 2026-10-08 审查修复（F3 配套）：读不到屏号就放弃这次发图（原来会拼出
    #   `--display None`，命令静默失败，而调用方以为分享页已经打开了）。
    _d = _require_did()
    if _d is None:
        abandon_share(target, _default_chat())
        return False, "读不到虚拟屏屏号（虚拟屏没跑？）", True
    adb("am start --display %d --windowingMode 5 "
        "-a android.intent.action.SEND -t image/png "
        "--eu android.intent.extra.STREAM file://%s "
        "-n com.tencent.mm/.ui.tools.ShareImgUI --grant-read-uri-permission"
        % (_d, p), 30)
    time.sleep(2.5)

    img = "/tmp/wxbridge-share-pick.jpg"
    pos = None
    horiz = None                             # 「最近转发」横排里的候选（次选）
    why_horiz = ""                           # 横排那一项的命中强度（exact / loose）
    hit_why = ""                             # 最终选中的那一项有多硬（预览页校验要用）
    # ★ 2026-10-09 晚（用户报「机器人有一些没发出去的」）：
    #   现场（22:00:19 那张「贴.png」）的真相不是"群名匹配不上"，而是**用户在物理屏
    #   用微信**（他当时正拿微信截图）—— 微信单实例，虚拟屏上就没有微信了，`shot_wechat`
    #   连着 8 轮全失败，循环白白跑满 ≈26 秒，最后还报成「列表里没找到（机器人不在群里？）」。
    #   现在：连续 3 轮拿不到画面就认定是这种占用，立刻收手并如实上报。
    shot_miss = 0        # 连续多少轮截不到微信画面（≈ 你的手机占用着微信）
    ui_miss = 0          # 分享页一直没起来（8 轮都判不是 SHARE_UI）
    for attempt in range(8):
        act = screen_activity()
        if act not in SHARE_UI:
            ui_miss += 1
            # 诊断用：2026-10-04 出现过「界面明明在分享页、这里却一直判不是」的情况，
            # 没有这行日志就只能瞎猜（每轮都静默 continue，8 轮后报"没找到"）。
            if attempt == 0:
                print("[wxbridge] /send_image 等分享页：当前界面=%r（不在 SHARE_UI）" % act,
                      flush=True)
            time.sleep(0.8)
            continue
        if shot_wechat(img):
            shot_miss = 0
            pos, h, why_best, why_h = _share_find_row(img, target)
            if h and not horiz:
                horiz, why_horiz = h, why_h
            if pos:
                hit_why = why_best
                break
            # ★ 2026-10-09：原来要等满 3 轮（attempt>=2）才肯用横排，实测那两轮**纯属白等** ——
            #   本机微信在「最近聊天」列表里把长群名压成「... （110人）」（一个特征汉字都不剩，
            #   `_share_row_match` 要求连续 ≥2 汉字，必然认不出来），而横排里
            #   「示例群 / 名字...」清清楚楚。改成第 2 轮就用横排，省 ~6 秒。
            #   安全性不变：进预览页后照旧要过「发送给：」那道校验（见下面 _preview_*）。
            if horiz and attempt >= 1:
                pos, hit_why = horiz, why_horiz
                print("[wxbridge] /send_image 最近聊天列表里没有「%s」，改用「最近转发」横排里的"
                      "会话 (%d,%d)（命中强度 %s）" % (target, pos[0], pos[1], why_horiz),
                      flush=True)
                break
        else:
            # ★ 截不到画面 = 微信已经不在虚拟屏上（被你拿到物理屏 / 你正在用手机）。
            #   连续 3 轮就收手：再等下去既发不出去，又把这一轮拖长（原来满 8 轮 ≈26 秒）。
            shot_miss += 1
            if shot_miss >= 3:
                print("[wxbridge] /send_image 连续 %d 轮截不到微信画面"
                      "（微信被拿到物理屏上去了 / 你正在用手机）→ 放弃，不白等" % shot_miss,
                      flush=True)
                break
        # 目标会话可能排在首屏下方：实测「文件传输助手」就在列表最底、只露出半行，
        # OCR 把它读成「性佳检助于」→ 永远找不到。第 3 轮起往下翻，一屏一屏找。
        if attempt >= 2:
            # ⚠️ 必须带 `-d <displayId>`：不带的话这条 swipe 打在**物理屏**上，虚拟屏毫无
            #    反应（2026-10-03 实测：连着几轮截图的哈希一模一样）。跟 tap() 同一机制。
            # ⚠️ 步长要小、时间要长：400ms 的快滑在微信列表里惯性极大，一次能滑过上千像素，
            #    直接把目标甩到屏幕上方（实测「文件传输助手」被滑没了）。350px/800ms 是
            #    实测既滚得动、又不飞过头的手感。
            _d = _require_did()
            if _d is not None:
                _k = screen_scale()
                adb("input -d %d swipe %d %d %d %d 800"
                    % (_d, int(318*_k), int(1000*_k), int(318*_k), int(650*_k)), 20)
            time.sleep(1.4)
        time.sleep(0.8)
    if not pos and horiz:
        # 「最近聊天」列表里翻遍了都没有，但上方「最近转发」横排里有这个会话 ——
        # 点它同样会进预览页（有「发送给：」校验兜底），比直接放弃强得多。
        print("[wxbridge] /send_image 最近聊天列表里没有「%s」，改用「最近转发」横排里的会话 "
              "(%d,%d)" % (target, horiz[0], horiz[1]), flush=True)
        pos, hit_why = horiz, why_horiz
    if not pos:
        if shot_miss >= 3:
            # ★ 见上面 shot_miss 的说明：这是"你占用着手机"的临时状态，不是群名问题。
            #   老代码在这里报「机器人不在群里？」，把一次临时占用说成了事故
            #   （2026-10-09 晚就是这么误报的）。
            abandon_share(target, _default_chat())
            return False, ("微信不在虚拟屏前台（你可能正在用手机 / 微信被拿到物理屏了），"
                           "截不到画面，这张图没发出去"), True
        if ui_miss >= 6:
            # 分享页根本没起来（同样是微信不在虚拟屏的典型表现）
            abandon_share(target, _default_chat())
            return False, "分享页没打开（微信不在虚拟屏前台？）", True
        print("[wxbridge] /send_image 选择列表里没找到「%s」，放弃（不乱点）"
              % target, flush=True)
        abandon_share(target, _default_chat())
        return False, "「选择聊天」列表里没找到「%s」（机器人不在群里？）" % target, True
    print("[wxbridge] /send_image 选中会话行 (%d,%d)" % pos, flush=True)
    tap(pos[0], pos[1])
    time.sleep(2.0)

    img = "/tmp/wxbridge-share-preview.jpg"
    if not shot_wechat(img):
        abandon_share(target, _default_chat())   # 截不到预览页也得把界面收回来
        # ★ 2026-10-11（用户报「又发癫了，发了两个图片」——同一张「咪.png」在群里
        #   出现两次）：这一条**必须 retryable=False**。上一行 `tap(pos)` 已经点过
        #   会话行了 —— 而微信这个分享页"点会话行"本身就可能是**确认发送**
        #   （半屏半分页布局下就是这样）。截图失败只说明"我这一刻看不见屏"，
        #   **不能推出"没发出去"**；补发就是让群里多一张一模一样的图。
        #   宁可漏这一张（用户看得出来、会说），也不重复发。
        return False, "截不到预览页（可能已经发出去了）—— 不补发", False
    act_now = screen_activity()
    half = "halfscreen" in act_now.lower()
    words = _ocr_words(img)                  # 同一张图只 OCR 一次，后面复用
    shown = _share_preview_target(words=words)
    name_ok = wxreader is not None and wxreader.match_title(shown, target)
    if not name_ok and hit_why == "exact" and _preview_name_unreadable(shown, target):
        # ★ 2026-10-09：预览页的名字被微信 UI 压成一个字（「人…」），屏幕上没有
        #   可辨识信息 —— 但上一步是在选择列表里**精确命中**目标名前 4 个字的，
        #   两条证据合起来足以确认。此前这种截图**每次都判失败**（用户报
        #   「看看腹肌发不出去：接口没回 ok」就是它）。
        name_ok = True
        print("[wxbridge] /send_image 预览页名字被压成「%s」（读不出全名），"
              "但上一步精确命中目标名 → 放行（截图 %s）" % (shown, img), flush=True)
    if not name_ok:
        print("[wxbridge] /send_image 预览页显示「%s」≠ 目标「%s」→ 取消并收回"
              % (shown, target), flush=True)
        abandon_share(target, _default_chat())
        # ★ 2026-10-11：`shown` 为空 = 屏幕上**读不到「发送给：」**，多半是这一步
        #   压根不在预览页（图已经发出去了 / 界面被收走了）—— 这种情况补发 = 重复发图，
        #   所以只有"确实停在预览页、只是名字对不上"才允许补发（retryable=bool(shown)）。
        return False, ("预览页上的会话名是「%s」，跟目标「%s」对不上 —— "
                       "没敢发（宁可不发也不能发错群）" % (shown, target)), bool(shown)
    print("[wxbridge] /send_image 预览校验通过（发送给：%s，布局=%s）"
          % (shown, "半屏" if half else act_now.split("/")[-1]), flush=True)
    # ⚠️ 半屏页的「发送」按钮**没有文字**（OCR 认不到），老兜底坐标 (497,1160) 又
    #    偏在按钮左边缘之外 → 点了等于没点（2026-10-04 实测）。改成：先认字，
    #    再按微信绿找按钮，最后才用实测过的兜底坐标。
    send_pos = _share_btn(words, ("发送",), None)
    if send_pos is None:
        send_pos = _green_btn(img)
    if send_pos is None:
        print("[wxbridge] /send_image 认「发送」失败（OCR 无字、也找不到绿色按钮），"
              "用兜底坐标", flush=True)
        send_pos = (532, 1088) if half else SHARE_SEND_BTN
    print("[wxbridge] /send_image 点发送 (%d,%d)" % send_pos, flush=True)
    tap(*send_pos)
    # ⚠️ 别 sleep 一下就截图：发送后微信还要过一层「半屏」过渡
    #    （HalfScreenTransparentActivity），那会儿 activity 不是聊天页、标题被读成
    #    「选择聊天」→ 图其实已经发出去了，却报失败（2026-10-03 实测）。
    #    轮询等界面真落到聊天页再判定。
    img = "/tmp/wxbridge-share-done.jpg"
    title, act = "", ""
    for _ in range(7):
        time.sleep(1.2)
        if not shot_wechat(img):
            continue
        act = screen_activity()
        if act in SHARE_UI or "halfscreen" in act.lower():
            continue                     # 还在过渡页，再等
        # ★ 2026-10-10：判据换 `_chat_title_now`（不依赖 activity）—— 从会话列表
        #   点进群时 activity 仍是 LauncherUI，老判据会把"其实已经发完"报成失败
        #   （而发图报失败可能触发补发 = 重复发图，见续26）。
        title = _chat_title_now(img)
        if title:
            break
    title = title or _chat_title_now(img)
    ok = bool(title and wxreader.match_title(title, target))
    print("[wxbridge] /send_image -> 界面=%s 标题=%r 匹配=%s"
          % (act or screen_activity(), title, ok), flush=True)
    if ok:
        _hud_sent(os.path.basename(p), kind="已发图")   # ★ 2026-10-07 用户要求
        _reclaim_input_focus()
    # ⚠️ 这里**绝不能**标 retryable：发送键已经点过了，报失败也可能是"其实发出去了"
    #   （半屏页过渡那一瞬间就是这么骗人的）—— 补发会造成重复发图，那是真事故。
    return ok, ("" if ok else "点完「发送」后没看到图进群（界面没回到「%s」）" % target), False


def _reclaim_input_focus():
    """发完图后，把输入连接从「半屏分享页」手里收回来。

    ★ 2026-10-09 加（用户报「发图很慢 / 发不出去」，查下来**配文也一直是坏的**）：
    半屏预览页上有一个「发消息」输入框，点完发送、半屏页关掉之后，它的输入会话
    还粘着 —— 紧接着发配文时 `/send` 会连报三次「点完输入框还没聚焦（焦点不在
    微信输入框）」然后放弃（实测 11:59 那次；10-08 两次美图配文同样如此）。
    手动点一下聊天输入框即可夺回：同日 adb 实测 `mServedView` **立刻**从
    Launcher / 半屏页的 DecorView 变成 `com.tencent.mm.ui.widget.MMEditText`。
    只在发图成功那条路上做，成本约 0.6 秒。
    """
    try:
        if tap(*INPUT_BOX_POS[0]):
            time.sleep(0.6)
    except Exception as exc:
        print("[wxbridge] 发图后收回输入焦点失败：%r" % (exc,), flush=True)


# ---------------------------------------------------------------- 图片消息取图
# ★ 2026-10-10（识图功能）：机器人要"看得见"群里发的照片，就得把那条图片消息
#   从截图上裁下来。**必须在算 bbox 的同一帧里裁** —— 隔一轮再裁，群里来条新
#   消息就把图顶上去了，坐标对不上（裁出来是别人的头像或者一片空白）。
#   所以取图跟着 /snapshot 一起做，不另开"按坐标裁"的接口。
IMG_CROP_DIR = "/tmp/wxbridge-img"
IMG_CROP_MIN_SIDE = 60      # 裁出来任一边小于它 => 不是照片（小图标/缩略图），不取
IMG_CROP_MAX_EDGE = 1024    # 长边上限，超过就缩（视觉模型不需要更大，也省流量）
IMG_CROP_QUALITY = 88
IMG_CROP_MAX_BYTES = 800 * 1024
IMG_CROP_KEEP_FRAMES = 3    # 只留最近几帧的裁剪文件，别把 /tmp 撑满
IMG_HASH_SIDE = 8           # 感知哈希边长（8x8 = 64 位，见 _img_ahash）
IMG_HASH_TRIM = 0.10        # 缩略前先裁掉四周 10%（边界抖动落在被裁掉的部分）
_IMG_SEQ = 0                # 帧号（snapshot 每跑一次 +1，给裁剪文件和清理用）


def _img_ahash(im):
    """裁剪图的感知哈希（aHash，8x8=64 位十六进制）—— 抗裁剪边界抖动。

    ★ 2026-10-10（用户报「机器人一直在识别阿兰发的图，好几次了」）：图片指纹
    原来用裁剪文件的**内容 md5**，而 ``wxreader.refine_image_box`` 的边界每帧
    会抖（同一张图实测读出 362x829 / 363x829 / 361x829），md5 对 1px 差异零容忍
    → 同一张图被反复当成"新图"送进视觉模型（17:24:40~17:25:30 连读 5 次）。

    两个参数都是实测定的（真机裁剪图 472x880 + 780 组表情包两两比）：

    | 方案 | 同图内容偏移 1~12px | 偏移 30px | 异图误合并(≤12) |
    |---|---|---|---|
    | 8x8 不裁边 | ≤3 | 15 | 8/780 |
    | 8x8 **裁边 10%** | ≤4 | 11 | **2/780** |
    | 16x16 及以上 | 1px 就翻好几位（**反而不稳**） | — | — |

    所以：8x8 + 缩略前裁掉四周 10%（边界抖动多半落在被裁掉的那圈里），
    判重按**汉明距离 ≤ 阈值**（见 ``state.IMG_HASH_MAX_DIST`` / ``State._img_dup``）。
    """
    from PIL import Image
    w, h = im.size
    tx = max(1, int(w * IMG_HASH_TRIM))
    ty = max(1, int(h * IMG_HASH_TRIM))
    if w - 2 * tx >= 8 and h - 2 * ty >= 8:
        im = im.crop((tx, ty, w - tx, h - ty))
    g = im.convert("L").resize((IMG_HASH_SIDE, IMG_HASH_SIDE), Image.LANCZOS)
    px = list(_pixels(g))
    avg = sum(px) / float(len(px))
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= 1 << i
    return "%016x" % bits


def _crop_message_image(src, bbox, is_self, seq, idx, prefix="f", prune=True):
    """把一条图片消息从当帧截图上裁下来，存成 JPEG。返回信息字典或 None。

    裁剪框过 ``wxreader.refine_image_box`` 精修 —— 检测到的气泡框常常只包住
    图片中间一条（照片边缘多是模糊/低饱和，白彩掩码抓不到），直接裁就给模型
    看一条横带。精修只扩不收。
    """
    try:
        from PIL import Image
        import numpy as np                          # 本文件里 np 一律局部导入
    except Exception as exc:                       # pragma: no cover
        print("[wxbridge] 取图跳过（PIL/numpy 不可用：%r）" % (exc,), flush=True)
        return None
    try:
        im = Image.open(src).convert("RGB")
        a = np.asarray(im, dtype=np.int16)
        x0, y0, x1, y1 = wxreader.refine_image_box(a, bbox, is_self=bool(is_self))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(im.width, x1), min(im.height, y1)
        if x1 - x0 < IMG_CROP_MIN_SIDE or y1 - y0 < IMG_CROP_MIN_SIDE:
            print("[wxbridge] 图片消息太小（%dx%d），不当照片取"
                  % (x1 - x0, y1 - y0), flush=True)
            return None
        crop = im.crop((x0, y0, x1, y1))
        if max(crop.size) > IMG_CROP_MAX_EDGE:
            crop.thumbnail((IMG_CROP_MAX_EDGE, IMG_CROP_MAX_EDGE), Image.LANCZOS)
        os.makedirs(IMG_CROP_DIR, exist_ok=True)
        path = os.path.join(IMG_CROP_DIR, "%s%d-%d.jpg" % (prefix, seq, idx))
        q = IMG_CROP_QUALITY
        while q >= 55:
            crop.save(path, quality=q, optimize=True)
            try:
                if os.path.getsize(path) <= IMG_CROP_MAX_BYTES:
                    break
            except OSError:
                break
            q -= 10
        if prune:
            _prune_img_crops(seq)
        md5 = hashlib.md5(open(path, "rb").read()).hexdigest()[:12]
        ahash = _img_ahash(crop)                 # 抗 1px 抖动的指纹（见 _img_ahash）
        print("[wxbridge] 取到图片消息 #%d：框 %s → %s，%dx%d %d 字节"
              % (idx, list(bbox), [x0, y0, x1, y1], crop.width, crop.height,
                 os.path.getsize(path)), flush=True)
        return {"path": path, "w": crop.width, "h": crop.height,
                "md5": md5, "hash": ahash}
    except Exception as exc:
        print("[wxbridge] 取图失败：%r" % (exc,), flush=True)
        return None


def _clean_backfill_crops():
    """清掉上一轮翻屏留下的 b* 裁剪图。

    ★ 2026-10-15：翻屏找图（``/backfill?images=1``）的裁剪文件**故意不参与**
      ``_prune_img_crops`` —— 那个函数是给"最新一帧"设计的（只留最近 3 帧），
      翻屏的图会被它当场删掉，bot 拿到路径也读不到。代价是它们要自己清。
    """
    try:
        for name in os.listdir(IMG_CROP_DIR):
            if name.startswith("b"):
                os.unlink(os.path.join(IMG_CROP_DIR, name))
    except OSError:
        pass


def _prune_img_crops(seq):
    """删掉太老的裁剪文件（只留最近 IMG_CROP_KEEP_FRAMES 帧的）。"""
    try:
        keep_from = seq - IMG_CROP_KEEP_FRAMES + 1
        for name in os.listdir(IMG_CROP_DIR):
            if not name.startswith("f"):
                continue
            try:
                n = int(name[1:].split("-")[0])
            except ValueError:
                continue
            if n < keep_from:
                os.unlink(os.path.join(IMG_CROP_DIR, name))
    except Exception:
        pass


# ★ 2026-10-10 省电：snapshot 的「画面没变就复用」单槽缓存（snapshot 在 _lock 内串行，
#   不需要额外加锁）。TTL=0 即关闭；环境变量 WX_SNAP_CACHE_TTL 可调（秒）。
try:
    SNAP_CACHE_TTL = float(os.environ.get("WX_SNAP_CACHE_TTL", "300"))
except ValueError:
    SNAP_CACHE_TTL = 300.0
_SNAP_CACHE = {"key": None, "t": 0.0, "out": None, "hit": 0, "miss": 0}


def snapshot(full=False, images=False):
    _ensure_screen_known()   # ★ 先定档位，调用方读到的坐标才是当前档的
    """截当前虚拟屏微信聊天页 -> 结构化消息。

    ``images=True`` 时，**同帧**把每条图片消息裁下来（见 _crop_message_image），
    在消息上多带 ``img_path`` / ``img_w`` / ``img_h`` / ``img_md5`` 四个字段。
    机器人读屏走这条路（它要识图）；面板/调试那些调用方不用带，省一次裁剪。
    """
    img = "/tmp/wxbridge-snap.jpg"
    ensure_wechat_front()
    if not wechat_front():
        return {"ok": False, "error": "微信不在虚拟屏前台"}
    if not shot_wechat(img):
        return {"ok": False, "error": "截图失败（微信不在虚拟屏前台）"}
    if wxreader is None:
        return {"ok": False, "error": "wxreader 不可用"}

    # ★ 2026-10-10 省电：画面一字节没变 → 直接复用上次的识别结果，不再整屏 OCR。
    #   实测静止的虚拟屏连抓 3 张，JPEG 字节逐位一致（md5 相同）；整屏 OCR 一次要烧
    #   约 17~35 秒 CPU，群里安静时机器人每 10~15 秒读一帧，绝大多数是白烧。
    #   同一张图的 OCR 结果是确定的，所以复用 = 和重跑完全等价，不改变任何决策。
    #   安全网：只缓存「读出了消息」的成功结果；超过 SNAP_CACHE_TTL 秒强制重读；
    #   带图的结果要求裁剪文件还在；key 含 full/images，不同形态不会串。
    _md5 = ""
    if SNAP_CACHE_TTL > 0:
        try:
            with open(img, "rb") as _fh:
                _md5 = hashlib.md5(_fh.read()).hexdigest()
        except OSError:
            _md5 = ""
    # ★ 2026-10-14：虚拟屏画面冻结检测 —— 每帧报一次，**必须放在缓存命中 return
    #   之前**（冻结时 md5 必然一直相同，缓存必然命中，放后面就永远轮不到）。
    _note_frame(_md5)
    _key = (bool(full), bool(images), _md5)
    _c = _SNAP_CACHE
    if (_md5 and _c["key"] == _key and _c["out"] is not None
            and time.time() - _c["t"] < SNAP_CACHE_TTL
            and all(os.path.exists(m["img_path"])
                    for m in _c["out"]["messages"] if m.get("img_path"))):
        _c["hit"] += 1
        return copy.deepcopy(_c["out"])
    _c["miss"] += 1

    try:
        out = wxreader.parse_snapshot(img)
    except Exception as exc:
        print("[wxbridge] parse_snapshot 失败：%r" % (exc,), flush=True)
        return {"ok": False, "error": "解析失败: %r" % (exc,)}

    msgs = [m for m in out["messages"] if m.get("type") != "time"]
    if not full:
        msgs = [m for m in msgs if m.get("type") != "system"]

    # 帧号：裁剪文件名用它排序，清理也靠它（进程重启后从 1 开始没关系，
    # _prune_img_crops 只删比自己老 3 帧的，重启后前几帧删不掉旧的也无妨）
    global _IMG_SEQ
    _IMG_SEQ += 1

    items = []
    for i, m in enumerate(msgs):
        item = {"text": m["text"], "is_self": bool(m["is_self"]),
                "type": m.get("type", "text"),
                "sender": m.get("sender", "")}
        if images and item["type"] == "image" and m.get("bbox"):
            got = _crop_message_image(img, m["bbox"], item["is_self"], _IMG_SEQ, i)
            if got:
                item["img_path"] = got["path"]
                item["img_w"] = got["w"]
                item["img_h"] = got["h"]
                item["img_md5"] = got["md5"]
                item["img_hash"] = got.get("hash", "")
        items.append(item)

    result = {
        "ok": True,
        "chat_title": out["chat_title"],
        "chat_title_raw": out.get("chat_title_raw", ""),
        "messages": items,
        "width": out["width"],
        "height": out["height"],
    }
    if _md5 and items:                      # 读空的结果不缓存（可能是 OCR 抽风）
        _c["key"], _c["t"], _c["out"] = _key, time.time(), copy.deepcopy(result)
    return result


# ------------------------------------------------------------------ 读聊天记录卡片
# 2026-10-10：用户要「让机器人能读到转发聊天记录里的内容」。真机实测（full 档
# 1272×2800，微信 8.0.x）三条结论，这条路的可行性就建立在这上面：
#   ① 聊天记录卡片是**单击**打开的 —— 不像文件卡片要点长按（菜单里还压着"删除"），
#      所以这条路上没有"误删群文件"那种不可逆风险；
#   ② 打开后 activity = com.tencent.mm/.feature.appmsg.ui.RecordDetailUI，
#      整页就是纯文字列表，OCR 一次全读出来（实测那条 5 条消息一字不差）；
#   ③ 按一次 `keyevent 4` 就回到点开前的聊天页。
#
# 但它仍然是"替用户点屏幕上的东西"，所以纪律跟别处一样：
#   ★ **只在被明确要求时调用**（机器人侧只有主人喊它才走这条路，群友 @ 不走）；
#   ★ 点开之后**必须确认真的进了 RecordDetailUI**，不是就立刻退出 ——
#     虚拟屏上可点的东西太多（图片、链接、小程序），点错一下就可能跳走。
RECORD_DETAIL_ACT = "RecordDetailUI"
RECORD_TITLE_RE = re.compile(r"的聊天记录\s*$")


def _ocr_rows_from_result(out):
    """把 ``wxreader.ocr_call`` 的返回变成 ``[(y0, x0, x1, y1, text)]``。

    ★ 为什么单独抽成纯函数：``ocr_call`` 返回的是 **``(结果列表, 耗时)`` 二元组**
      （见 wxreader.ocr_call）。直接 ``for item in res`` 遍历到的是"结果列表"和
      "耗时"这两样东西，一个词条都拿不到，而且**不报错**。
      2026-10-10 真机就踩了这个坑：屏幕上明明摆着那张卡片，``/read_record`` 却
      回"屏幕上没找到聊天记录卡片"。抽出来是为了能单测（test_read_record.py 第 3c 节）。
    """
    if isinstance(out, tuple):
        out = out[0] if out else None
    rows = []
    for item in (out or []):
        try:
            box, txt = item[0], str(item[1]).strip()
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
        except Exception:
            continue
        if not txt:
            continue
        rows.append((min(ys), min(xs), max(xs), max(ys), txt))
    rows.sort()
    return rows


def _record_ocr_rows(img):
    """整页 OCR -> [(y0, x0, x1, y1, text)]，按 y 排序。

    跟 wxreader.parse_snapshot 的区别：这里**不走气泡分析**。聊天记录详情页不是
    聊天页（没有绿气泡、昵称也不在头像列上），parse_snapshot 的昵称/气泡判据在
    那儿全都会误判 —— 实测它会把「左左」当头像列上的灰字丢掉。这里只要原始行。
    """
    if wxreader is None:
        return []
    try:
        out = wxreader.ocr_call(img)
    except Exception as exc:
        print("[wxbridge] 读聊天记录：OCR 失败 %r" % (exc,), flush=True)
        return []
    return _ocr_rows_from_result(out)


def _find_record_card(rows, match=""):
    """在会话页里找聊天记录卡片的标题行，返回 (y0,x0,x1,y1,title) 或 None。

    ★ 只认「以『的聊天记录』结尾」的行 —— 这是微信卡片标题的固定格式。
      绝不能宽松匹配"聊天记录"：群里有人打「昵称看一下聊天记录然后点评」，
      那一行也含这四个字（实测那张现场截图里两行同时存在），点上去就是把
      别人的消息点了一下。
    """
    cands = []
    for row in rows:
        t = row[4]
        if RECORD_TITLE_RE.search(t) or (match and match in t):
            cands.append(row)
    if not cands:
        return None
    return max(cands, key=lambda r: r[0])      # 最靠下的一条 = 屏幕上最新那张卡片


def _record_clean(rows, max_chars=4000):
    """详情页的 OCR 行 -> 一段可读文字。

    - 砍掉标题栏（标题 + 日期那一行）—— 它对点评没用，还容易跟正文混淆；
    - 同一条消息的左右两段合并（群昵称在左、时间戳在右，y 几乎相同、x 隔很远）。
    """
    keep = []
    for row in rows:
        y0, x0, x1, y1, t = row
        if y0 < 140:
            continue
        if re.fullmatch(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", t):
            continue
        keep.append([y0, x0, x1, y1, t])
    merged = []
    for row in sorted(keep):
        if merged:
            p = merged[-1]
            if abs(row[0] - p[0]) <= 14 and (row[1] - p[2]) > 300:
                p[4] = (p[4] + " " + row[4]).strip()
                p[2] = max(p[2], row[2])
                p[3] = max(p[3], row[3])
                continue
        merged.append(list(row))
    return "\n".join(r[4] for r in sorted(merged))[:max_chars]


# ---------------------------------------------------------------- 往回翻补读
# ★ 2026-10-10：漏消息的根子是「一屏只有 11 条」—— 轮询间隔里刷过去的消息
#   **从来没上过屏**，机器人永远看不到。用户对「自动补读」选的是**不要**，
#   但只要他叫，就走这条路：往上滚 N 屏把刚才刷过去的内容读回来，读完**必须**
#   切回最新 —— 否则接下来几轮读到的一直是历史，新消息反而全漏，比不补还糟。
BACKFILL_MAX = 4


def backfill(screens=2, chat="", images=False):
    """手动补读：往上滚 N 屏读回历史消息，最后切回最新。

    返回 ``{"ok": bool, "frames": [[消息…], …], "error": str}``；
    每条消息形如 ``{"text":…, "sender":…, "is_self":…, "type":…}``。

    ★ 2026-10-15（用户：「我要的是读张三的图片，需要翻屏幕」）：``images=True``
      时把每屏的**图片消息也裁出来**（多带 img_path/img_w/img_h/img_md5/img_hash），
      机器人被叫看图、而那张图已经被刷上去时，靠它把图捞回来。
      裁剪文件用 ``b`` 前缀（不参与 ``_prune_img_crops``，见 ``_clean_backfill_crops``）。
    """
    _ensure_screen_known()
    d = _require_did()
    if d is None:
        return {"ok": False, "frames": [], "error": "读不到虚拟屏号"}
    if not wechat_front():
        return {"ok": False, "frames": [], "error": "微信不在虚拟屏前台（截不到画面）"}
    if wxreader is None:
        return {"ok": False, "frames": [], "error": "wxreader 不可用"}
    try:
        n = int(screens)
    except (TypeError, ValueError):
        n = 2
    n = max(1, min(n, BACKFILL_MAX))

    img = "/tmp/wxbridge-backfill-size.jpg"
    if not shot(img):
        return {"ok": False, "frames": [], "error": "截图失败"}
    try:
        from PIL import Image
        W, H = Image.open(img).size
    except Exception:
        W, H = 1272, 2800

    bf_seq = int(time.time())
    if images:
        _clean_backfill_crops()      # 先清上一轮的（它们不会被自动清理）

    frames = []
    for i in range(n):
        # 「手指从上往下滑」才是看更早的消息（写反了会一直停在最新，见 SKILL.md）
        adb("input -d %d swipe %d %d %d %d 400"
            % (d, W // 2, int(H * 0.25), W // 2, int(H * 0.85)), 20)
        time.sleep(1.3)
        pth = "/tmp/wxbridge-backfill-%d.jpg" % i
        if not shot(pth):
            break
        try:
            snap = wxreader.parse_snapshot(pth)
        except Exception as exc:
            print("[wxbridge] 补读第%d屏解析失败：%r" % (i + 1, exc), flush=True)
            continue
        items = []
        for mi, m in enumerate(snap.get("messages") or []):
            if m.get("type") in ("time", "system"):
                continue
            item = {"text": m.get("text", ""),
                    "sender": m.get("sender", ""),
                    "is_self": bool(m.get("is_self")),
                    "type": m.get("type", "text")}
            # ★ 2026-10-15：被叫看图时把这一屏的图片一起裁回来（bot 侧要翻屏找某人的图）
            if images and item["type"] == "image" and m.get("bbox"):
                got = _crop_message_image(pth, m["bbox"], item["is_self"],
                                          bf_seq, mi, prefix="b", prune=False)
                if got:
                    item["img_path"] = got["path"]
                    item["img_w"] = got["w"]
                    item["img_h"] = got["h"]
                    item["img_md5"] = got["md5"]
                    item["img_hash"] = got.get("hash", "")
            items.append(item)
        frames.append(items)
        print("[wxbridge] 补读第%d屏：%d 条（标题 %r）"
              % (i + 1, len(items), snap.get("chat_title")), flush=True)

    back = False
    if chat:
        try:
            back = bool(open_chat(str(chat)))
        except Exception:
            back = False
    if not back:
        for _ in range(n + 1):      # 兜底：反向滑回底部
            adb("input -d %d swipe %d %d %d %d 400"
                % (d, W // 2, int(H * 0.85), W // 2, int(H * 0.25)), 20)
            time.sleep(0.8)
    print("[wxbridge] 补读完成：%d 屏，回最新=%s" % (len(frames), back), flush=True)
    return {"ok": True, "frames": frames, "back_to_latest": bool(back), "error": ""}


def read_record(match=""):
    """点开屏幕上那张聊天记录卡片，把里面的字读回来。

    返回 ``{"ok": bool, "title": str, "text": str, "error": str}``。
    调用方是机器人（``/read_record``）—— 它只在主人喊"读一下这个聊天记录"时调。
    """
    _ensure_screen_known()
    if wxreader is None:
        return {"ok": False, "title": "", "text": "", "error": "wxreader 不可用"}
    d = _require_did()
    if d is None:
        return {"ok": False, "title": "", "text": "", "error": "读不到虚拟屏号"}
    if not wechat_front():
        return {"ok": False, "title": "", "text": "",
                "error": "微信不在虚拟屏前台（截不到画面）"}
    img = "/tmp/wxbridge-record-scan.jpg"
    if not shot(img):
        return {"ok": False, "title": "", "text": "", "error": "截图失败"}
    rows = _record_ocr_rows(img)
    card = _find_record_card(rows, match)
    if card is None:
        return {"ok": False, "title": "", "text": "",
                "error": "屏幕上没找到聊天记录卡片（把它翻到最新一条再叫它）"}
    y0, x0, x1, y1, title = card
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    print("[wxbridge] 读聊天记录：点开卡片 %r @ (%d,%d)" % (title, cx, cy), flush=True)
    if not tap(cx, cy):
        return {"ok": False, "title": title, "text": "", "error": "点不动（读不到屏号）"}
    act1 = ""
    for _ in range(12):                      # 最多等 ~4.8 秒
        time.sleep(0.4)
        act1 = top_activity_of(d)
        if RECORD_DETAIL_ACT in act1:
            break
    if RECORD_DETAIL_ACT not in act1:
        # ⚠️ 没进详情页 —— 立刻退回，**绝不在这里补点第二下**（群里可点的东西太多）。
        adb("input -d %d keyevent 4" % d, 15)
        return {"ok": False, "title": title, "text": "",
                "error": "点开后不是聊天记录页（%s），已退出" % (act1 or "?")}
    time.sleep(0.8)                          # 等渲染稳定再截
    img2 = "/tmp/wxbridge-record-detail.jpg"
    if not shot(img2):
        adb("input -d %d keyevent 4" % d, 15)
        return {"ok": False, "title": title, "text": "",
                "error": "详情页截图失败（已退出）"}
    text = _record_clean(_record_ocr_rows(img2))
    # 退出：一次返回不够就再来一次 —— 别把用户留在详情页上
    adb("input -d %d keyevent 4" % d, 15)
    time.sleep(0.8)
    act2 = top_activity_of(d)
    if RECORD_DETAIL_ACT in act2:
        adb("input -d %d keyevent 4" % d, 15)
        time.sleep(0.8)
        act2 = top_activity_of(d)
    if not text:
        return {"ok": False, "title": title, "text": "",
                "error": "详情页没读到文字（可能整条都是图片）"}
    print("[wxbridge] 读聊天记录：%r 读到 %d 字（回到 %s）"
          % (title, len(text), act2 or "?"), flush=True)
    return {"ok": True, "title": title, "text": text, "error": "",
            "activity": act1, "after": act2}


def _find_sender_block(rows, sender, height=0):
    """在整屏 OCR 行里找「sender 发的那条消息」的可见文字块，返回 (y0,x0,x1,y1,label)。

    ★ 2026-10-15（用户要求「顺手也认『聊天记录』以外的卡片」）：聊天记录卡片有固定
      标题格式（「XX的聊天记录」，见 `_find_record_card`），转发图文 / 链接 / 公众号
      卡片**没有** —— 只能按位置找：群里**昵称在消息上方**，所以"昵称行下面、到下一个
      大空档之前的那些行"就是这条消息（卡片里的文字也会被 OCR 一起读出来），取并集矩形。

    ⚠️ 三条保守约束：
      · 只认**最靠下**的那条同名昵称行（屏幕上最新那条）；
      · 行数上限 10、碰到 ≥220px 的行间大空档就停（别把下一条消息并进来）；
      · 底部 10% 的 OCR 行（微信底部 tab 栏）一律不要 —— 否则并集会被拉到底部，
        点中心就点到「通讯录 / 发现」上去了。
    返回 None = 找不到（调用方就此收手，绝不乱点）。
    """
    s = (sender or "").strip()
    if not s or not rows:
        return None

    def _clean(t):
        return (t or "").strip().strip("：:")

    idx = [i for i, r in enumerate(rows) if _clean(r[4]) == s]
    if not idx:
        # 昵称可能被 OCR 读残/断词（「张三」读成「素问 阿兰」）→ 退一步做包含匹配，
        # 但要求"这一行基本就是这个昵称"（长出来的不超过 3 个字）。
        idx = [i for i, r in enumerate(rows)
               if s in _clean(r[4]) and len(_clean(r[4])) <= len(s) + 3]
    if not idx:
        return None
    i = idx[-1]
    name_y1 = rows[i][3]
    lim = int(height * 0.90) if height else 10 ** 9
    block = []
    for r in rows[i + 1:]:
        if r[3] <= name_y1:
            continue
        if r[3] > lim:
            break
        t = _clean(r[4])
        if not t:
            continue
        if block and (r[0] - block[-1][3]) > 220:
            break                      # 大空档 = 这条消息已经结束
        block.append(r)
        if len(block) >= 10:
            break
    if not block:
        return None
    y0 = min(r[0] for r in block)
    y1 = max(r[3] for r in block)
    x0 = min(r[1] for r in block)
    x1 = max(r[2] for r in block)
    if y1 - y0 < 20:                   # 太薄（多半只读到半行）→ 不敢点
        return None
    return (y0, x0, x1, y1, _clean(rows[i][4]))


IMG_HASH_SAME_DIST = 12                   # aHash 判"同一张图"的距离阈值（与 state 同值）
GALLERY_ACT = "gallery.ui.GalleryEntryUI"      # 微信图片查看器（点开一张图进这里）


def _ham(a, b):
    """两个 aHash 整数的汉明距离（判"是不是同一张图"）。"""
    if a is None or b is None or a < 0 or b < 0:
        return 999
    return bin(int(a) ^ int(b)).count("1")


def _dark_ratio(path):
    """截图里"暗像素"的比例 —— 微信图片查看器是**黑底**，聊天页是白/灰底。

    ★ 2026-10-15：判"点开后进没进看图页"**不能用 activity** —— 实测
      `screen_activity()`（读 topResumedActivity）在 freeform 下一直报
      `com.tencent.mm/.ui.LauncherUI`，哪怕画面已经是大图查看器（dumpsys 里
      GalleryEntryUI 明明在）。第一次实测就因此误判"没点开"。
      换成看画面本身：黑底 = 看图页。实测聊天页暗像素 < 5%，看图页 > 60%。
    """
    try:
        from PIL import Image
        im = Image.open(path).convert("L")
        im.thumbnail((64, 64))
        px = list(_pixels(im))
        return sum(1 for v in px if v < 50) / float(len(px) or 1)
    except Exception:
        return -1.0


def _shot_diff(a, b):
    """两张截图的 aHash 汉明距离 —— 判"画面是不是换了一页"。

    ★ 2026-10-15：判"点开后有没有进新页面"试过两条路都不行：
      · `screen_activity()`（freeform 下恒报 LauncherUI，见 `_dark_ratio` 的说明）；
      · 单看"是不是黑底"—— 转发卡片点开进的是**卡片详情页**（白底），不是图片查看器，
        实测两次都因此误判"没点开"。
      最能说明问题的其实是**画面本身有没有换**：同页面的细微变化（新消息/动画）
      aHash 距离很小，整页切换则很大。所以拿"点击前 vs 点击后"比一比。
    """
    try:
        from PIL import Image
        ha = int(_img_ahash(Image.open(a).convert("RGB")), 16)
        hb = int(_img_ahash(Image.open(b).convert("RGB")), 16)
        return _ham(ha, hb)
    except Exception:
        return -1


def _crop_pil(src, bbox):
    """从截图里裁一块出来（内存里，不落盘）—— 给 zoom_image 认图用。"""
    from PIL import Image
    x0, y0, x1, y1 = [int(v) for v in bbox]
    im = Image.open(src).convert("RGB")
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(im.width, x1); y1 = min(im.height, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return im.crop((x0, y0, x1, y1))


def zoom_image(img_hash="", sender=""):
    """点开屏幕上的那张图 → 截一张**放大图** → 立刻退回。

    ★ 2026-10-15（用户实测确认后才写的）：聊天页里那张缩略图上的小字，OCR 出来是
      乱码（实测「而明的汗(燃贸穴就口*不填它就田坝」）；而**点开进图片查看器后，
      大图清清楚楚** —— 同一次实测直接读出了「邵千惠故事馆 / 老公正和我闺蜜在卖力
      地打扑克，而我躲在床底下…」整段。所以被叫看图时先点开放大、截一张、马上退回，
      把大图交给视觉模型。

    选图：优先按 ``img_hash``（机器人那侧选中的那张，aHash 匹配，最稳，不受昵称抖动
    影响）；没给/匹配不上就退化为**屏幕上最靠下的那张别人的图**。

    返回 ``{"ok", "path", "w", "h", "sender", "error"}``。
    ⚠️ 只点**一次**；点完没进看图页就报错退出（绝不补点）；不管成没成，
      最后一次一定按返回退出 —— 别把用户的微信留在看图页上。
    """
    if wxreader is None:
        return {"ok": False, "path": "", "error": "wxreader 不可用"}
    d = _require_did()
    if d is None:
        return {"ok": False, "path": "", "error": "读不到虚拟屏号"}
    if not wechat_front():
        return {"ok": False, "path": "", "error": "微信不在虚拟屏前台（截不到画面）"}
    img = "/tmp/wxbridge-zoom-scan.jpg"
    if not shot(img):
        return {"ok": False, "path": "", "error": "截图失败"}
    try:
        snap = wxreader.parse_snapshot(img)
    except Exception as exc:
        return {"ok": False, "path": "", "error": "解析失败：%r" % (exc,)}
    msgs = [m for m in (snap.get("messages") or [])
            if m.get("type") == "image" and m.get("bbox")]
    if not msgs:
        return {"ok": False, "path": "", "error": "屏幕上没有可点开的图片"}

    want = -1
    if img_hash:
        try:
            want = int(str(img_hash).strip(), 16)
        except (TypeError, ValueError):
            want = -1

    hit = None
    best = None                     # (消息, bbox)，取最靠下的那张别人的图
    for m in msgs:
        try:
            bbox = [int(v) for v in m["bbox"]]
        except (TypeError, ValueError, KeyError):
            continue
        if not m.get("is_self"):
            if best is None or bbox[3] > best[1][3]:
                best = (m, bbox)
        if want < 0 or m.get("is_self"):
            continue
        try:
            pil = _crop_pil(img, bbox)
            # ⚠️ `_img_ahash` 返回的是 **16 位十六进制字符串**（"%016x"），
            #   这里要的是整数 —— 2026-10-15 第一次实测就是在这里 TypeError 的。
            h = int(_img_ahash(pil), 16) if pil is not None else -1
        except Exception:
            h = -1
        if h >= 0 and _ham(h, want) <= IMG_HASH_SAME_DIST:
            hit = (m, bbox)
            break
    pick = hit or best
    if pick is None:
        return {"ok": False, "path": "", "error": "屏幕上没有别人的图可点"}
    m, (x0, y0, x1, y1) = pick
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    before = screen_activity()
    print("[wxbridge] 点图放大：%s @ (%d,%d) 框(%d,%d-%d,%d)"
          % (os.path.basename(str(m.get("img_path") or "")), cx, cy, x0, y0, x1, y1),
          flush=True)
    if not tap(cx, cy):
        return {"ok": False, "path": "", "error": "点不动（读不到屏号）"}
    time.sleep(1.5)                         # 等大图渲染完
    out = "/tmp/wxbridge-zoom-%d.jpg" % int(time.time())
    if not shot(out):
        return {"ok": False, "path": "", "error": "放大页截图失败"}
    dark = _dark_ratio(out)
    diff = _shot_diff(img, out)          # img = 点击前那张 scan
    if dark < 0.25 and diff < 18:
        # ⚠️ **没进新页面时绝不按返回** —— 聊天页上按一次 BACK 会把微信退回
        #   会话列表/桌面，那比这条看不了严重得多。
        return {"ok": False, "path": "",
                "error": "点开后页面没变化（暗像素 %.2f / 画面差异 %s）"
                         % (dark, diff)}
    # 进了看图页 → 立刻退出（一次不够再来一次，但只用**画面**判，不看 activity）
    adb("input -d %d keyevent 4" % d, 15)
    time.sleep(0.9)
    back2 = "/tmp/wxbridge-zoom-back.jpg"
    if shot(back2) and _dark_ratio(back2) >= 0.25:
        adb("input -d %d keyevent 4" % d, 15)
        time.sleep(0.8)
    try:
        from PIL import Image
        W, H = Image.open(out).size
    except Exception:
        W, H = 0, 0
    print("[wxbridge] 点图放大成功：%s %dx%d（暗像素 %.2f / 画面差异 %s，页面 %s）"
          % (os.path.basename(out), W, H, dark, diff, before or "?"), flush=True)
    return {"ok": True, "path": out, "w": W, "h": H,
            "sender": str(m.get("sender") or ""), "error": ""}


def read_card(sender="", match=""):
    """点开屏幕上「那条消息」读整页 OCR —— 给聊天记录之外的卡片用。

    ★ 2026-10-15（用户：「读一下张三的图片」→ 那张其实是**转发的微博图文卡片**，
      读屏层只读出「@怪我笨图文 / 3.6万 / #如何判断…」这类碎片，视觉模型没图可看）。
      读不到就**点开它**：微信会把卡片内容整页渲染出来，再整页 OCR。
      用户选的是「加，并且顺手也认聊天记录以外的卡片」。

    与 ``read_record`` 的区别只在"怎么找到要点的那块"：
      · ``read_record`` 认标题格式（「XX的聊天记录」）；
      · 这里认**发送者昵称下面的那块**（`_find_sender_block`）；两者都找不到就不点。

    ⚠️ 安全护栏（一条都不能松）：
      · 点之前必须在**聊天页**；
      · 只点**一次** —— 点完页面没变就立刻报错退出，绝不补点第二下；
      · 读完整页 OCR **立刻返回**，最多退两次，别把用户留在详情页上。
    """
    if wxreader is None:
        return {"ok": False, "title": "", "text": "", "error": "wxreader 不可用"}
    d = _require_did()
    if d is None:
        return {"ok": False, "title": "", "text": "", "error": "读不到虚拟屏号"}
    if not wechat_front():
        return {"ok": False, "title": "", "text": "",
                "error": "微信不在虚拟屏前台（截不到画面）"}
    before = screen_activity()
    img = "/tmp/wxbridge-card-scan.jpg"
    if not shot(img):
        return {"ok": False, "title": "", "text": "", "error": "截图失败"}
    # ★ 2026-10-15（第一次真机实测就踩到）：判"在不在聊天页"**不能用 activity** ——
    #   微信从会话列表点进群时 topResumedActivity 仍然是 LauncherUI（freeform 窗口
    #   模式，读取层为此专门把判据换成了标题；交接说明也记过）。用它当护栏 = **永远
    #   拒绝**，这个功能根本进不去（当天 18:44 两次实测都被它挡回）。
    #   改成读**标题**：会话列表页读作「微信」，聊天页读作群名。
    t_now = ""
    try:
        t_now = str(wxreader.read_title(img) or "").strip()
    except Exception:
        t_now = ""
    if (not t_now) or ("微信" in t_now) or t_now in ("通讯录", "发现", "我"):
        return {"ok": False, "title": "", "text": "",
                "error": "当前不在某个聊天页（标题读作 %r / %s），不敢乱点"
                         % (t_now, before or "?")}
    rows = _record_ocr_rows(img)
    H = 0
    try:
        from PIL import Image
        H = Image.open(img).size[1]
    except Exception:
        H = 0
    card = _find_record_card(rows, match) or _find_sender_block(rows, sender, H)
    if card is None:
        return {"ok": False, "title": "", "text": "",
                "error": "屏幕上没找到「%s」发的那条（把它翻到屏幕上再叫它）"
                         % (sender or match or "?")}
    y0, x0, x1, y1, title = card
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    print("[wxbridge] 读卡片：点开 %r @ (%d,%d)（%d,%d-%d,%d）"
          % (title, cx, cy, x0, y0, x1, y1), flush=True)
    if not tap(cx, cy):
        return {"ok": False, "title": title, "text": "", "error": "点不动（读不到屏号）"}
    act1 = ""
    for _ in range(12):                      # 最多等 ~4.8 秒
        time.sleep(0.4)
        act1 = screen_activity()
        if act1 and act1 != before:
            break
    if not act1 or act1 == before:
        adb("input -d %d keyevent 4" % d, 15)
        return {"ok": False, "title": title, "text": "",
                "error": "点开后没进新页面（%s），已退出" % (act1 or "?")}
    time.sleep(0.9)
    img2 = "/tmp/wxbridge-card-detail.jpg"
    if not shot(img2):
        adb("input -d %d keyevent 4" % d, 15)
        return {"ok": False, "title": title, "text": "",
                "error": "详情页截图失败（已退出）"}
    text = _record_clean(_record_ocr_rows(img2))
    adb("input -d %d keyevent 4" % d, 15)
    time.sleep(0.8)
    act2 = screen_activity()
    if act2 and act2 != before:
        adb("input -d %d keyevent 4" % d, 15)
        time.sleep(0.8)
        act2 = screen_activity()
    if not text:
        return {"ok": False, "title": title, "text": "",
                "error": "读到的页面里没有文字（可能是纯图片卡片）"}
    print("[wxbridge] 读卡片：%r 读到 %d 字（页面 %s → 回到 %s）"
          % (title, len(text), act1, act2 or "?"), flush=True)
    return {"ok": True, "title": title, "text": text, "error": "",
            "activity": act1, "after": act2}


# 会话列表右上角放大镜的候选坐标。
# 2026-10-03 实测：微信在虚拟屏上被 freeform 窗口装饰挤下了一行 —— 屏幕上多出
# 一条「微…」的窗口标题栏，微信自己的标题栏（带放大镜）落到 y≈170，
# 老坐标 (508,47) 点在了装饰栏/加号附近，于是「点放大镜」永远打不开搜索页。
# 2026-10-04 实测：列表页标题栏渲染成重影后，放大镜也在两个高度各出现一次，
# 只有 (430,80) 那个是真控件（手工逐个试出来的），所以它排第一。
SEARCH_BTN_POS = ((430, 80), (508, 47), (455, 170), (455, 65))
# 搜索页的输入框（2026-10-04：搜索页不再自动聚焦，必须先点这里再提交）
SEARCH_INPUT_POS = ((350, 107), (300, 105), (400, 110))


def goto_search(tries=3):
    """点会话列表右上角的放大镜，直到真的进搜索页。

    虚拟屏前台会被 DSH 抢走，点击丢失是常态 —— 所以点完要轮询界面，
    没进去就重来，别假设一次就成。
    """
    for i in range(tries):
        if not back_to_list():
            return False
        ensure_wechat_front()
        if not wechat_front():
            continue
        bx, by = SEARCH_BTN_POS[i % len(SEARCH_BTN_POS)]
        tap(bx, by)
        for _ in range(6):
            time.sleep(0.4)
            if screen_activity() == WX_SEARCH:
                print("[wxbridge] 放大镜点 (%d,%d) 命中搜索页" % (bx, by), flush=True)
                return True
        print("[wxbridge] 第%d次点放大镜 (%d,%d) 没进搜索页（当前 %s）"
              % (i + 1, bx, by, screen_activity()), flush=True)
    return False


def _chat_title_now(img, diag=None):
    """当前**真的**停在某个聊天页吗？是就返回 OCR 读到的标题，否则返回 ""。

    ``diag`` 传一个 dict 时把「为什么读空」的原因写进 ``diag["why"]`` ——
    ★ 2026-10-16 加：这条函数读空时**一个字的日志都不打**，历史上日志里只能看到
      「标题读不出（''）」，看不出是截图坏了、停在搜索页、还是底部 tab 判绿了。
      三条判据任意一条不成立都返回 ""，没有 diag 时行为与以前完全一致。

    ★ 2026-10-10（切群改成"会话列表里直接点"之后补的）：判据与 `_already_in_chat`
      完全一致，抽出来给 `/where`、`/send` 的会话核对共用 —— 它们原来用
      `on_chat_page()`（activity == ChattingUI），而**从会话列表点进去**时
      activity 仍然是 LauncherUI（微信是单 Activity + fragment，只有搜索框那条路
      进去才是 ChattingUI）。不改的话，"列表点"这条路虽然能切进群，但进去以后
      `/send` 每次核对都读不出标题 → 判成"无法确认会话"→ **一条都发不出去**，
      而且会白白再切一次群（切群才要借键盘）。
    三条判据（缺一不可）：
      ① 不是搜索页（搜索页顶部就是搜索词，会被误读成会话标题）；
      ② 标题里有中文（防 OCR 读残时假成功）；
      ③ 底部「微信」tab 不绿（绿 = 其实还停在会话列表，标题读到的是第一行群名）。
    """
    if wxreader is None:
        if diag is not None:
            diag["why"] = "wxreader 未加载"
        return ""
    if screen_activity() == WX_SEARCH:
        if diag is not None:
            diag["why"] = "停在微信搜索页"
        return ""
    title = (wxreader.read_title(img) or "").strip()
    if not re.search(r"[\u4e00-\u9fff]", title):
        if diag is not None:
            diag["why"] = "标题条没有中文（OCR 读空或读残：%r）" % title
        return ""
    if _bottom_tab_green(img):
        if diag is not None:
            diag["why"] = "底部「微信」tab 判绿（其实停在会话列表）"
        return ""
    return title


# ★ 2026-10-16：会话校验改成**多帧投票** —— 起因是用户报「前一条发出去了，
#   后一条就不确定是不是在目标群」（08:46/08:48/08:49 连续三次）。
#   现场铁证：`/send` 说读不出（''）→ 它调 `open_chat(verify=True)` 去核对，
#   而 `open_chat` 里的 `_already_in_chat` **调的同一个 `_chat_title_now`** 却说
#   "已经在目标会话" → 紧接着 `/send` 再读一次又读不出。同一函数、同一屏幕、
#   相隔 1 秒，一次成功一次失败 = **读空是随机的瞬时故障**（抓到了画面切换的
#   半截帧 / 微信被挤下虚拟屏的那一瞬），不是"真的不在目标群"。
#   代价：这条被拒发 → 挂补发队列 → 几秒后补上（只是延迟，不是错发/漏发）。
#   为什么敢多帧投票：**确认在目标群只要有一帧铁证就够**，而"发错群"才是不可逆的
#   那件事 —— 所以规则是「任一帧读到目标群标题 → 放行；读到别的会话 → 立刻拒发；
#   全部读不出 → 走原来的切群 + 再核对」。第一帧就匹配时零额外开销。
_TITLE_VOTE_FRAMES = max(1, int(os.environ.get("WXBRIDGE_TITLE_VOTE_FRAMES", "3")))
_TITLE_VOTE_GAP = float(os.environ.get("WXBRIDGE_TITLE_VOTE_GAP", "0.6"))
_TITLE_FAIL_KEEP = max(0, int(os.environ.get("WXBRIDGE_TITLE_FAIL_KEEP", "20")))


def _title_probe(img, diag):
    """读一帧标题，返回 (标题, 失败原因)。截图失败也算读空。"""
    if not shot_wechat(img):
        diag["why"] = "截图失败（微信不在虚拟屏前台？）"
        return "", diag["why"]
    return _chat_title_now(img, diag=diag), diag.get("why", "")


def _keep_title_fail_frame(src, tag=""):
    """把读空的那一帧留一份现场（带时间戳），别让它被下一帧覆盖掉。

    历史教训：`/tmp/wxbridge-where.jpg` 每帧都覆盖同一个文件名，事后再想查
    "当时到底抓到了什么"已经无从查起。这里只保留最近 `_TITLE_FAIL_KEEP` 张。
    """
    if _TITLE_FAIL_KEEP <= 0:
        return ""
    try:
        import glob as _glob
        import shutil as _sh
        dst = time.strftime("/tmp/wxbridge-titlefail-%Y%m%d-%H%M%S")
        if tag:
            dst += "-" + str(tag)
        dst += ".jpg"
        _sh.copy(src, dst)
        old = sorted(_glob.glob("/tmp/wxbridge-titlefail-*.jpg"))
        for f in old[:-_TITLE_FAIL_KEEP]:
            try:
                os.remove(f)
            except OSError:
                pass
        return dst
    except Exception:
        return ""


def _title_vote(want, keep=False):
    """连抓 N 帧投票：现在到底在不在目标会话？

    返回 ``(verdict, title, notes)``：
      · ``True``  —— 至少一帧读到且匹配目标群（放行；立即返回，不做无谓的多抓）
      · ``False`` —— 读到了**别的**会话名（拒发，title 是读到的那个名字）
      · ``None``  —— 每一帧都读不出（交给调用方：先切回目标群再投票）
    """
    notes = []
    wrong = ""
    img = "/tmp/wxbridge-where.jpg"
    for i in range(_TITLE_VOTE_FRAMES):
        if i:
            time.sleep(_TITLE_VOTE_GAP)
        diag = {}
        title, _why = _title_probe(img, diag)
        # 口径与历史行为一致：只有 >=2 个字符的标题才算"读到了会话名"，
        # 单个汉字（OCR 读残，如把「目标群」读成「缺」）按"读不出"处理。
        _clean = title.replace("…", ".").strip(". ")
        if len(_clean) >= 2:
            if wxreader is not None and wxreader.match_title(title, want):
                return True, title, notes
            wrong = title
            notes.append("第%d帧读到别的会话「%s」" % (i + 1, title))
            break                      # 读到别的会话：立刻拒发，不继续抓
        if title:
            notes.append("第%d帧只读到 %r（读残）" % (i + 1, title))
        else:
            why = diag.get("why") or "原因未知"
            notes.append("第%d帧读不出（%s）" % (i + 1, why))
        if keep:
            _keep_title_fail_frame(img, "f%d" % (i + 1))
    return (False if wrong else None), wrong, notes


def _vsd_frames():
    """问虚拟屏守护：这块屏**累计出过多少帧**（读不到返回 None）。

    ★ 2026-10-16：`frames` 是守护的 ImageReader 回调里自增的（每来一个新帧 +1），
      **与有没有客户端取图无关**。
      ⚠️ 实测修正（别拿它单独当冻结证据）：**虚拟屏内容不变时 Android 本来就不产
      新帧** —— 实测静止时 2 秒不涨是常态，有变化时才涨。所以「frames 不涨」
      只能当"要不要继续复核"的信号，定案交给 `_disturb_and_verify()`。
    """
    try:
        import json as _json
        import urllib.request as _u
        url = "http://127.0.0.1:%d/list" % int(os.environ.get("VSD_PORT", "18250"))
        with _u.urlopen(url, timeout=4) as r:
            data = _json.loads(r.read().decode("utf-8"))
        _d = did()
        best = None
        for item in data:
            if _d is not None and int(item.get("displayId", -1)) == int(_d):
                return int(item.get("frames") or 0)
            if best is None and int(item.get("i", -1)) == int(SLOT):
                best = int(item.get("frames") or 0)
        return best
    except Exception:
        return None


def _vsd_other_display():
    """除当前操作的那块屏之外，另一块**虚拟屏**的 displayId（没有就 None）。

    给扰动复验用：要把微信 task 搬到别处再搬回，才能逼出一次必然的重绘。
    `displayId > 0` 同时把物理屏（0）排除掉 —— 绝不能把微信搬去用户正看着的屏。
    """
    try:
        import json as _json
        import urllib.request as _u
        url = "http://127.0.0.1:%d/list" % int(os.environ.get("VSD_PORT", "18250"))
        with _u.urlopen(url, timeout=4) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    _d = did()
    for item in data:
        try:
            i = int(item.get("displayId", -1))
        except Exception:
            continue
        if i > 0 and i != _d:
            return i
    return None


def _disturb_and_verify():
    """主动扰动一次，**这是判定"真冻结"唯一能定案的手段**（2026-10-16 用户拍板加上）。

    原理：画面 md5 不变、守护 `frames` 不涨，都可能只是"群里真的没新内容"
    （实测虚拟屏内容不变时 Android 本来就不产新帧）；只有**做一个必然引起重绘的
    动作**再看画面动不动，才能区分"静"和"冻"。
    动作：把微信 task 用 `cmd activity display move-stack` 搬到另一块虚拟屏、再搬回
    （不是 `am start` —— 同一 display 上重放窗口是 no-op，而它到别的 display 是
    真的重排 + 重绘；move-stack 也不解析 Intent、不动 Activity 栈，聊天页不会丢）。

    返回：``True`` 屏活着 / ``False`` 扰动后纹丝不动（真冻）/ ``None`` 做不了。
    ⚠️ 有副作用（几秒内微信会闪到另一块屏），所以只在"连续 20 轮画面不变 + 帧不涨
      + 连标题都读不出"之后才调用。
    """
    _home = did()
    _other = _vsd_other_display()
    _tid = _wx_root_task_id()
    if _home is None or _other is None or _tid is None:
        return None
    before = ""
    try:
        _p0 = "/tmp/wxbridge-disturb-before.jpg"
        if shot_wechat(_p0):
            with open(_p0, "rb") as fh:
                before = hashlib.md5(fh.read()).hexdigest()
    except Exception:
        before = ""
    _f0 = _vsd_frames()
    print("[wxbridge] 冻结复核：主动扰动 —— 微信 task %d 搬到屏 %d 再搬回"
          % (_tid, _other), flush=True)
    adb("cmd activity display move-stack %d %d" % (_tid, _other), 25)
    time.sleep(1.5)
    adb("cmd activity display move-stack %d %d" % (_tid, _home), 25)
    time.sleep(1.5)
    if not wechat_front():
        # 搬回来失败 = 微信正留在别的屏上，比"读不到画面"更严重，必须当场喊
        print("[wxbridge] ⚠️ 扰动后微信没回到屏 %s，再搬一次" % _home, flush=True)
        adb("cmd activity display move-stack %d %d" % (_tid, _home), 25)
        time.sleep(1.5)
        if not wechat_front():
            print("[wxbridge] ⚠️⚠️ 扰动后微信仍未回到屏 %s —— 需要人工恢复："
                  "bash /sdcard/dsh/wechat-bot/app/bot.sh start --restart" % _home,
                  flush=True)
    _f1 = _vsd_frames()
    after = ""
    try:
        _p1 = "/tmp/wxbridge-disturb-after.jpg"
        if shot_wechat(_p1):
            with open(_p1, "rb") as fh:
                after = hashlib.md5(fh.read()).hexdigest()
    except Exception:
        after = ""
    if _f0 is not None and _f1 is not None and _f1 > _f0:
        print("[wxbridge] 扰动复验：frames %d→%d（涨了）→ 屏活着" % (_f0, _f1),
              flush=True)
        return True
    if before and after and after != before:
        print("[wxbridge] 扰动复验：画面 md5 变了（%s→%s）→ 屏活着"
              % (before[:8], after[:8]), flush=True)
        return True
    print("[wxbridge] 扰动复验：frames %s→%s、画面 md5 %s→%s 都没动 → 真冻结"
          % (_f0, _f1, (before or "-")[:8], (after or "-")[:8]), flush=True)
    return False


WX_MAIN_ACTS = ("com.tencent.mm.ui.LauncherUI",
                "com.tencent.mm.ui.chatting.ChattingUI")


def wx_main_windows(dpy=None):
    """虚拟屏上现在显示着几个**微信主窗口**（会话列表 / 聊天页）。

    ★ 2026-10-11 事故（用户报「机器人把之前的话发给我妈了」）：
      现场截图 `/tmp/wxbridge-after-send.jpg` 显示 —— 目标群聊天页在底下，**另一个
      会话（妈妈）以小窗浮在上面**。而桥接的会话核对 `_chat_title_now()` 只读屏幕
      **顶部标题条**，读到的是底层的「目标群(110)」→ 判定"在目标群" → 接着 commit
      文字、点发送。可输入法连接挂的是**持有焦点的小窗**（不是被点到底层那个输入框），
      于是那条猫娘回复落进了妈妈的私聊。**核对通过 ≠ 文字会进这个会话。**

    这里数一遍 display <dpy> 上微信的"主窗口"（LauncherUI / ChattingUI）——
    **>1 就说明屏幕上同时摊着两个会话，会话归属不可信，一律拒发。**

    返回窗口数；读不到 dumpsys 时返回 ``None``（调用方按"不确定"处理，别当成 0）。
    """
    d = display_id() if dpy is None else int(dpy)
    out = adb("dumpsys window windows", 40)
    if not out or "Window #" not in out:
        return None
    n = 0
    for blk in re.split(r"(?=\n  Window #)", out):
        m = re.search(r"Window\{[0-9a-f]+ u\d+ ([\w\.]+)/([\w\.\$]+)\}", blk)
        if not m or m.group(1) != "com.tencent.mm" or m.group(2) not in WX_MAIN_ACTS:
            continue
        md = re.search(r"mDisplayId=(\d+)", blk)
        if not md or int(md.group(1)) != d:
            continue
        # ★ 2026-10-11 下午修正（差点写出一个"什么都不发"的机器人）：
        #   **必须过滤可见性**。微信的 LauncherUI 是宿主窗口，聊天页（ChattingUI）
        #   打开时它仍在窗口列表里、只是 mViewVisibility=0x8(GONE)。不加这道过滤，
        #   正常聊天页也会数出 2 个 → 机器人**什么都发不出去**。
        #   实测：正常聊天页 ChattingUI 可见(0x0) + LauncherUI GONE(0x8) = 1；
        #   事故现场（目标群 + 妈妈小窗）两个都可见 = 2。
        mv = re.search(r"mViewVisibility=(0x[0-9a-fA-F]+)", blk)
        if mv and int(mv.group(1), 16) != 0:
            continue
        n += 1
    return n


def _already_in_chat(name, img=None):
    """现在是不是**已经**在目标会话里？

    ★ 2026-10-10：`open_chat()` 现在先问它，早退就**不协商、不抢屏**。
      以前 `/open` 一进来就跑 `preflight_yield()`，于是"机器人本来就在目标群"
      这件事也要弹预告、问用户让位 —— 你在打字时它被判失败，机器人启动时切群
      失败就整个不启动（"为安全起见不往下跑"）。切群才需要借键盘；已经在群里
      什么都不用做。
    判据与 `_open_chat_inner` 里的早退**完全一致**（含"标题必须有中文"那道闸，
    防止 OCR 读残时假成功）。
    """
    name = (name or "").strip()
    if not name or wxreader is None:
        return False
    img = img or "/tmp/wxbridge-open.jpg"
    if not shot_wechat(img):
        return False
    # ★ 2026-10-10（用户报「我在物理屏把微信切到了目标群，机器人还需要切群吗」）：
    #   这里原来要求 `on_chat_page()`（activity == ChattingUI），实测**从会话列表
    #   点进去**时微信聊天页的 activity 仍然是 LauncherUI（微信是单 Activity +
    #   fragment），只有搜索框那条路进去才是 ChattingUI —— 于是"已经在群里"被判
    #   成"不在"，白白多切一次群（多借一次输入法 = 多抢一次键盘）。
    #   现在判据换成三条：① 不是搜索页（搜索页顶部是搜索词，会误判成会话标题）
    #   ② 标题能匹配目标名 ③ 底部「微信」tab 不绿（绿 = 其实还停在会话列表，
    #   标题读到的是列表第一行那个群名）。
    #   现在判据统一走 `_chat_title_now`（三条：不是搜索页 / 标题有中文 /
    #   底部「微信」tab 不绿）—— 两处口径完全一致，不会再各判各的。
    return bool(wxreader.match_title(_chat_title_now(img), name))


def _find_chat_row(img, name, ytop=150):
    """在**会话列表**里找目标会话那一行，返回 ``(x, y, 读到的文字)`` 或 None。

    ★ 2026-10-10（用户要求「试一下改直接点」）：加它是为了「切群不借输入法」。
      原来切群只有搜索框一条路，而搜索框必须临时切走用户的输入法 ——
      用户报的「机器人反复抢键盘」就是它。会话列表里直接点一下，全程不碰输入法。
    判据（宁可不点，也别点错人）：
      · 只认含目标名的行，且**文本长度不超过 名字+6 字**（消息预览通常更长）；
      · 取**最上面**那一行（用户的目标群是置顶的）；
      · 只在列表区域找（标题栏以下、底部 tab 栏以上）。
    """
    if wxreader is None or not name:
        return None
    try:
        from PIL import Image
        with Image.open(img) as im:
            _w, h = im.size
        rows = _ocr_rows_from_result(wxreader.ocr_call(img))
    except Exception:
        return None
    if not rows:
        return None
    ybot = int(h * 0.86)              # 底部 tab 栏别点进去
    want = name.strip()
    cands = []
    for (y0, x0, x1, y1, txt) in rows:
        t = (txt or "").strip()
        if y0 < ytop or y0 > ybot:
            continue
        if not re.search(r"[\u4e00-\u9fff]", t):
            continue
        if want not in t and t not in want:
            continue
        if len(t) > len(want) + 6:    # 太长的多半是消息预览，不是会话名
            continue
        cands.append((y0, x0, x1, y1, t))
    if not cands:
        return None
    cands.sort()
    y0, x0, x1, y1, t = cands[0]
    return int(x0), int((y0 + y1) / 2), t


def _list_scroll_top(img, times=2):
    """把会话列表滚回最顶部（手指从上往下滑 = 内容往下走 = 看到更早的会话）。

    ★ 2026-10-10：**置顶会话永远在列表最上面**，所以"回到顶部"等于保证它一定
      在视野里。只在首屏没找到目标会话时才调 —— 常态切群一次都不滑。
      返回 False 只表示截图读不出尺寸，调用方按"没找到"处理即可。
    """
    try:
        from PIL import Image
        with Image.open(img) as im:
            w, h = im.size
    except Exception:
        return False
    _d = _require_did()
    if _d is None:
        return False
    x = int(w / 2)
    for _ in range(max(1, int(times))):
        adb("input -d %d swipe %d %d %d %d 260"
            % (_d, x, int(h * 0.35), x, int(h * 0.72)), 15)
        time.sleep(0.5)
    return True


def _open_chat_by_list(name):
    """在会话列表里**直接点**目标会话（不借输入法）。成功返回 True。

    ★ 为什么不借输入法就不会抢键盘：`ime set` 是**全局**操作，切走的是用户
      在物理屏用的那个键盘。会话列表点一下不需要输入法。
      找不到（没置顶 / 不在首屏）就返回 False，调用方回退搜索框那条路。
    """
    if wxreader is None:
        return False
    if not back_to_list():
        return False
    img = "/tmp/wxbridge-list.jpg"
    if not shot_wechat(img):
        return False
    hit = _find_chat_row(img, name)
    if not hit:
        # ★ 2026-10-10：列表可能被滚过（置顶会话滑出首屏）—— 先滚回顶部再找一次。
        #   仍然找不到才回退搜索框（那条要借输入法 = 会抢键盘）。
        if _list_scroll_top(img):
            time.sleep(0.8)
            if shot_wechat(img):
                hit = _find_chat_row(img, name)
    if not hit:
        print("[wxbridge] /open 会话列表里没找到「%s」→ 改用搜索框" % name, flush=True)
        return False
    x, y, t = hit
    print("[wxbridge] /open 会话列表里直接点「%s」（读到 %r，点 %d,%d，不借输入法）"
          % (name, t, x, y), flush=True)
    tap(x, y)
    time.sleep(2.0)
    if not shot_wechat(img):
        return False
    title = wxreader.read_title(img)
    # 判据同 `_already_in_chat`：**不能**要求 activity == ChattingUI ——
    # 从会话列表点进去时它仍是 LauncherUI（实测，见那边的注释）。
    ok = (bool(wxreader.match_title(title, name))
          and screen_activity() != WX_SEARCH
          and not _bottom_tab_green(img))
    print("[wxbridge] /open(列表点) %s -> 标题=%r 匹配=%s" % (name, title, ok), flush=True)
    return ok


def _try_open_by_list(name):
    """只走「会话列表里直接点」这条路 —— **不借输入法、不协商、不弹预告、不抢屏**。

    ★ 2026-10-10（用户要求「直接在屏幕点击置顶的目标群，尽量不走搜索」）：
      `open_chat()` 现在先调它，成功就直接返回。以前不管走哪条路都先
      `preflight_yield()`：弹抢屏预告 + 震动（真要抢的时候还按 HOME 把用户的
      界面切到后台），可"列表里点一下"根本不需要键盘 —— 用户报的"机器人总在
      抢键盘"里，有一部分就是这种**空炮**。
    全程只在虚拟屏上动：截图 → 找行 → 点一下 → 校验标题。
    返回 False = 列表里没找到（没置顶 / 不在首屏），调用方回退搜索框那条路。
    """
    if wxreader is None:
        return False
    ensure_wechat_front()
    if not wechat_front():
        return False
    dismiss_popup()          # 模态弹窗挡着的话，后面全是白点
    return _open_chat_by_list(name)


def open_chat(name, verify=True):
    _ensure_screen_known()   # ★ 先定档位，调用方读到的坐标才是当前档的
    """切到指定会话（对外入口）。实现见 ``_open_chat_inner``。

    ★ 2026-10-09（用户要求「先弹窗，我再放手，放手之后他再抢」）：
      切群要在微信搜索框里打字 → 里面那句 `ime_set(IME_OURS)` 同样会把用户在
      物理屏上打字的键盘切走。所以这里跟 `/send` 一样先协商；真抢了屏的话，
      不管走哪条 return 都把它切回来（finally 兜住）。
    ★ 2026-10-10：**已经在目标会话里就直接放行**（不协商、不抢屏）—— 见
      `_already_in_chat` 的说明。
    """
    if verify and _already_in_chat(name):
        print("[wxbridge] /open %s：已经在目标会话，直接放行（不协商、不抢屏）" % name,
              flush=True)
        return True
    # ★ 2026-10-10（用户要求「直接在屏幕点击置顶的目标群，尽量不走搜索」）：
    #   先试**不借键盘**的那条路 —— 会话列表里点一下。成了就到此为止：
    #   不协商、不弹抢屏预告、不震动、不碰物理屏。
    #   以前不管走哪条路都先 `preflight_yield()`：明明列表点不需要键盘，却照样
    #   弹"要抢屏了"的预告 + 震动（真要抢时还会按 HOME 把用户的界面切到后台）。
    if _try_open_by_list(name):
        return True
    _pf_ok, _pf_prev = preflight_yield()
    if not _pf_ok:
        print("[wxbridge] /open 放弃：你在用手机，先不切群", flush=True)
        return False
    try:
        ok = _open_chat_inner(name, verify=verify)
        # ★ 2026-10-09（续13）：停在搜索页 → 退回列表**重试一次**。
        #   「搜索框没吃进字」是这条流程最常摔的一跤（输入法冷启动慢），而它
        #   重试一次就好了 —— 今天 12:14 实测：第一次 /open 报「没找到搜索结果」，
        #   人工按返回后再 /open 立刻成功。以前这里直接把 False 丢给调用方：
        #   机器人**启动时**切群失败 → 整个不启动（"为安全起见不往下跑"），
        #   微信就留在搜索页等人来救。
        #   只在"确实停在搜索页"时重试（别的失败原因不重复折腾）。
        if not ok and screen_activity() == WX_SEARCH:
            print("[wxbridge] /open 停在搜索页 → 退回会话列表重试一次", flush=True)
            back_to_list()
            time.sleep(0.8)
            ok = _open_chat_inner(name, verify=verify)
        return ok
    finally:
        if _pf_prev:
            restore_physical_app(_pf_prev)
            # ★ 2026-10-14：统一走 `_hud_released()`（和"发完消息"那条标题正文一致）
            _hud_released("切群")


def _open_chat_inner(name, verify=True):
    """切到指定会话：优先搜索框（已验证可行），进去后用标题校验。"""
    name = (name or "").strip()
    if not name or wxreader is None:
        return False
    ensure_wechat_front()
    if not wechat_front():
        return False
    # ★ 2026-10-09：这里原来是 `wait_user_idle()`（等 2 秒就"照做"、一个字都不提示）
    #   —— 预告 / 让位 / 抢屏现在统一由外面那层 open_chat() 的 preflight_yield()
    #   负责，而且它已经在 `ime_set()` **之前**跑完了。
    dismiss_popup()          # 开场先扫一眼：模态弹窗挡着的话，后面全是白点
    img = "/tmp/wxbridge-open.jpg"
    # 必须先确认真的停在聊天页：搜索页顶部就是搜索框，标题 OCR 会读到搜索词，
    # 只看标题会把「正在搜索」误判成「已经在这个会话里」。
    # ★ 2026-10-10：判据抽成 `_already_in_chat` —— open_chat() 那层也用它，
    #   保证"已经在目标会话"两处口径完全一致（含下面那道"标题必须有中文"的闸：
    #   2026-10-08 那次 /open("文件传输助手") 假成功、测试消息发进真群就是它）。
    if _already_in_chat(name, img=img):
        return True
    # ★ 2026-10-10（用户要求）：优先「在会话列表里直接点群名」—— 不借输入法、
    #   不抢键盘。找不到才回退下面的搜索框那条路（它才需要借输入法）。
    if _open_chat_by_list(name):
        return True
    # 必须先回到会话列表再点放大镜：在别的聊天页时右上角是「…」不是放大镜，
    # 直接点 (508,47) 会打开聊天信息页（踩过）。界面不认识时也只点返回箭头。
    if not goto_search():
        print("[wxbridge] /open 进不去搜索页（当前 %s），放弃"
              % screen_activity(), flush=True)
        return False

    saved = current_ime()
    try:
        # ⚠️ 2026-10-04 实测：搜索页**不再保证自动聚焦**（截图里搜索框是空的，
        #    下面还杵着「按住 说出你的问题」的语音入口）→ 直接 commit 一个字都进不去，
        #    表现为「/open 没找到搜索结果」，机器人启动时就卡在切群。
        #    所以先显式点一下搜索框，再提交。
        ime_set(IME_OURS)                # ★ 先切输入法……
        time.sleep(0.6)
        tap(*SEARCH_INPUT_POS[0])        # ★ 再点搜索框
        time.sleep(0.8)
        # ★ 点完**绝不再 ime_set** —— 切输入法会让输入框失焦，字就进不去了
        #   （交接说明「连着填几个输入框不会来回切，那会让输入框反复失焦」就是这条）
        commit(name, do_clear=True)
        time.sleep(1.6)
        # ★ 2026-10-10（用户报「机器人反复抢键盘，还连续抢了好几次」）：
        #   这里原来把输入法**还回去**，等 attempt==3 重试时再借一次 ——
        #   一次失败的 /open 因此要切 4 次输入法（用户数出来的"好几次"就是它）。
        #   现在整个 /open 只借一次、只在 finally 里还一次；重试就地重来
        #   （输入法已经就绪，反而更容易成功）。

        # 结果列表是异步渲染的，截一次不一定赶上 —— 多试几次。
        # 也不排除搜索框没吃进字：那就点一下搜索框重来一遍。
        pos = None
        for attempt in range(5):
            if attempt == 3:
                if screen_activity() != WX_SEARCH:
                    break                # 界面已经不是搜索页了，绝不乱输入
                tap(*SEARCH_INPUT_POS[0])
                time.sleep(0.5)
                commit(name, do_clear=True)     # 输入法还在借用中，不重切（见上）
                time.sleep(1.5)
                continue
            time.sleep(1.0)
            if not shot_wechat(img):
                break
            pos = wxreader.find_result_row(img)
            if pos:
                break
        if not pos:
            try:
                import shutil
                shutil.copy(img, "/tmp/wxbridge-open-fail.jpg")
            except Exception:
                pass
            print("[wxbridge] /open 没找到搜索结果：%s（界面=%s，已存 /tmp/wxbridge-open-fail.jpg）"
                  % (name, screen_activity()), flush=True)
            return False
        tap(pos[0], pos[1])
        time.sleep(2.0)
        if not shot_wechat(img):
            return False
        title = wxreader.read_title(img)
        ok = on_chat_page() and wxreader.match_title(title, name)
        print("[wxbridge] /open %s -> 界面=%s 标题=%r 匹配=%s"
              % (name, screen_activity(), title, ok), flush=True)
        if not ok and not verify:
            return True
        return ok
    finally:
        ime_set(saved)


# ------------------------------------------------------------ 自动归还（微信借到物理屏）
# 2026-10-04 用户要求：想随手拿手机看微信，不想「急停 → 看完 → 重启」折腾几十秒。
# 规则：
#   · 微信在虚拟屏 → 什么都不做；
#   · 微信在物理屏、且**用户正在看**（物理屏前台是微信）→ 绝对不碰；
#   · 微信两屏的前台都不是它，持续 RECLAIM_IDLE 秒 → 自动收回（搬回虚拟屏 +
#     切回目标群），机器人随之恢复读屏；
#   · 机器人没在跑 → 什么都不做（桥接可能只是面板留着，别去抢用户的微信）。
RECLAIM_IDLE = float(os.environ.get("WX_RECLAIM_IDLE", "5"))
# 看护线程的轮询间隔（秒）。**这个值直接决定常驻负载**：每一跳都要跑一次
# `dumpsys activity activities`（Android 上很重）。2026-10-05 用户报「手机卡卡的」，
# 实测 wxbridge 85 分钟烧了 27 分钟 CPU —— 主要就是这里 3 秒一跳。调到 10 秒后
# 调用次数降到 1/3；代价是「用户切走微信后自动收回」最坏慢 10 秒。
# ★ 2026-10-14（用户要求「自动归还的 10 秒改成 5 秒」）：⚠️ 光改 RECLAIM_IDLE **没用** ——
#   `idle` 是按**这个值累加**的（见 `reclaim_watch`：`idle += RECLAIM_TICK`），
#   粒度不降下去的话，第一跳就累加到 10 ≥ 5、**立刻**收回，等于"完全不缓冲"
#   （用户切出去看一眼别的 App 再切回来，微信已经被抢没了）。
#   所以两个**必须一起改**：IDLE 10→5、TICK 10→5。实际效果：用户切走后
#   0~5 秒收回（原来 0~10 秒）。
#   代价：dumpsys 频率翻倍 —— 2 倍负载，仍**远低于**当年 3 秒那一档。
#   真觉得费电/卡，把两个都调回去（WX_RECLAIM_IDLE=10 且 TICK 改回 10.0）。
RECLAIM_TICK = 5.0

# ★ 2026-10-09（续13）用户问「微信卡在搜索页能自动恢复吗」—— 之前**不能**：
#   今天 12:14 那次机器人启动切群失败（搜索框没吃进字），进程直接退出，
#   微信就留在搜索页，直到人工按返回。
#   这条阈值就是"在搜索页停多久算卡住"。正常 `/open` 在搜索页待 10~20 秒
#   （切输入法 + 输字 + 等结果），所以 45 秒足够宽；看护每 RECLAIM_TICK 跳一次，
#   实际最坏 45~55 秒脱困。设 0 = 关掉这个自愈。
SEARCH_STUCK_SECS = float(os.environ.get("WX_SEARCH_STUCK_SECS", "45"))


def _tops():
    """一次 dumpsys 取回 {displayId: topActivity} —— 比逐个 display 调用省一半 adb。"""
    out = adb('dumpsys activity activities | grep -E "Display #|topResumedActivity"', 25) or ""
    res, cur = {}, None
    for ln in out.splitlines():
        t = ln.strip()
        if t.startswith("Display #"):
            m = re.match(r"Display #(\d+)", t)
            cur = int(m.group(1)) if m else None
            continue
        if cur is not None and "topResumedActivity=" in t:
            m = re.search(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", t)
            if m:
                res[cur] = m.group(1)
    return res


def bot_running():
    """机器人主进程在不在跑。停了机器人就不许再抢用户的微信。"""
    try:
        return subprocess.run(["pgrep", "-f", "main_android.py"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def reclaim(force=False):
    """把微信收回虚拟屏**并切回目标群**（一步到位）。

    force=False 时带保护：用户正在物理屏看微信就什么都别做 ——
    宁可这轮不做，也绝不把用户手上的微信「变没」。
    """
    if wechat_front():
        return True
    if not force and physical_wechat_in_use():
        print("[wxbridge] 归还：物理屏正在用微信，先不动", flush=True)
        return False
    if not takeover():
        return False
    grp = _default_chat()
    if grp:
        open_chat(grp)
    print("[wxbridge] 归还完成：微信已在虚拟屏，会话切回「%s」" % (grp or "?"), flush=True)
    return True


def _wx_dpi_on_physical():
    """物理屏（Display #0）上微信任务当前的 density；微信不在物理屏 → None。"""
    out = adb("dumpsys activity activities", 25) or ""
    m = re.search(r"\nDisplay #0\b(.*?)(?=\nDisplay #\d|\Z)", out, re.S)
    seg = m.group(1) if m else ""
    for b in re.split(r"\* Task\{", seg)[1:]:
        if not re.search(r"A=\d+:com\.tencent\.mm\b", b):
            continue
        d = re.search(r"CurrentConfiguration=\{[^}]*?(\d+)dpi", b)
        if d:
            return int(d.group(1))
    return None


def _physical_dpi():
    try:
        m = re.search(r"Physical density:\s*(\d+)", adb("wm density", 15) or "")
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 560


def _fix_physical_wx_density():
    """把物理屏微信的脏 density 修回物理屏的值（用户报「微信字儿特别小」）。

    ★ 2026-10-08 根因：本机只有一个微信，机器人把它放在虚拟屏（636x1400 / 280dpi）。
      **用户从桌面点开它时，系统把任务搬到物理屏却不重算 density** ——
      微信于是按 280dpi 渲染（1264px 相当于 722dp，像平板），字看着特别小。
      实测修法（同 rescue-wechat.sh）：显式 `am start --display 0 --windowingMode 1`
      会触发系统重算配置，density 立刻回到 560。
    这个函数挂在 reclaim_watch 里，只在「物理屏正开着微信」时被调用，
    所以既不打扰用户看微信，也不需要 user 每次来叫我们。
    """
    dpi = _wx_dpi_on_physical()
    if dpi is None:
        return
    phys = _physical_dpi()
    if dpi == phys:
        return
    print("[wxbridge] 物理屏微信 density=%d（应为 %d，虚拟屏粘过来的）→ 重算配置"
          % (dpi, phys), flush=True)
    try:
        adb("am start --display 0 --windowingMode 1 -n com.tencent.mm/.ui.LauncherUI", 25)
        time.sleep(1.5)
        print("[wxbridge] 物理屏微信 density 已修为 %s" % (_wx_dpi_on_physical(),), flush=True)
    except Exception as exc:
        print("[wxbridge] 修物理屏微信 density 失败：%r" % (exc,), flush=True)


def _unstick_search(tops, dpy, since):
    """虚拟屏微信**卡在搜索页**超过 `SEARCH_STUCK_SECS` 秒 → 点返回箭头退回会话列表。

    ★ 2026-10-09（续13，用户报「微信卡在搜索页能自动恢复吗」）。返回新的"卡住
    起始时刻"（没卡住给 0，调用方存着下轮接着算）。

    为什么要这一条：`/open` 最常摔的一跤就是"搜索框没吃进字"（输入法冷启动慢），
    这时微信停在搜索页；如果 `/open` 是**机器人启动时**调的，失败会让机器人整个
    不启动（"为安全起见不往下跑"）→ 微信留在搜索页、没人管（今天 12:14 那次）。
    所以这一条**故意不看机器人在不在跑** —— 它要救的正是"机器人已经不在了"。

    安全性：
      · 只认 `WX_SEARCH` 这一个 activity，且**必须停够 `SEARCH_STUCK_SECS` 秒**；
      · 动作只有一下"点左上角返回箭头"（跟 `back_to_list` 同一个点，不输入任何文字）；
      · 要持 `_lock`，持锁后再确认一次界面 —— `/open` 正跑着的话会先让它跑完；
      · 用户自己在物理屏用微信时，虚拟屏上根本没有微信（只有一个实例）→ 不会误伤。
    """
    if SEARCH_STUCK_SECS <= 0 or dpy is None:
        return 0.0
    if tops.get(dpy, "") != WX_SEARCH:
        return 0.0
    now = time.time()
    if not since:
        return now
    if now - since < SEARCH_STUCK_SECS:
        return since
    with _lock:
        # 等锁的这段时间它可能自己走掉了（比如 `/open` 正好在跑）—— 持锁后重判
        if top_activity_of(dpy) != WX_SEARCH:
            return 0.0
        print("[wxbridge] 微信卡在搜索页 >%.0f 秒 → 自动脱困（点返回箭头）"
              % SEARCH_STUCK_SECS, flush=True)
        tap(*_sp(55, 60))                  # 左上角返回箭头（back_to_list 用的同一个点）
        time.sleep(0.9)
        after = top_activity_of(dpy)
        if after == WX_LAUNCHER:
            print("[wxbridge] 自动脱困成功：已退回会话列表", flush=True)
            try:
                _hud_notice("微信卡在搜索页，已经帮它退出来了", seconds=8,
                            title="微信机器人 · 自动脱困",
                            detail="下一轮它会自己重新切群")
            except Exception:
                pass
        else:
            print("[wxbridge] 自动脱困：点了一次返回仍是 %s（下轮再看）" % after,
                  flush=True)
    return 0.0


def _ime_unstick():
    """默认输入法**留在「DSH 输入」上没还回去** → 立刻还回用户自己的那个。

    ★ 2026-10-09（续14，用户原话「有时候我在桌面的时候仍然会卡输入法，机器人回复
    的时候桌面上仍然有一个输入法没按下去」）。

    为什么会有这一条：`ime set` 是**全局**的。机器人要在虚拟屏微信里打字，就得把
    **整个系统**的默认输入法换成「DSH 输入」—— 那是个**没有键盘**的输入法。
    所以借走的那几秒里，用户在任何界面点输入框都弹不出键盘。发完必须还回去，
    但这条路上有几处会漏：
      · adb 超时/断线 → `finally` 里那句 `ime_set(saved)` 根本没跑成；
      · 桥接被重启/被杀在中途（看护重启、容器重建）→ 状态就留在 DshIme 上；
      · `ime set` 静默失败（返回 ok 却没生效）—— 今天之前连返回都不看。
    用户感知到的就是"键盘没了 / 那个输入法没按下去"。

    这里每 RECLAIM_TICK 秒看一眼，**不管机器人在不在跑**（桥接重启后的残留正需要它）：
    只要默认输入法还是 DshIme、而且没有"正在借用"的登记 → 还回去。
    """
    cur = _ime_now()
    if cur != IME_OURS:
        if cur:
            _IME_USER_LAST[0] = cur        # 顺手记住用户自己的输入法
        return
    at = float(_IME_LEASE.get("at") or 0.0)
    if at and (time.time() - at) < IME_LEASE_MAX:
        return                             # 正在借（发送/切群途中）→ 别插手
    back = str(_IME_LEASE.get("back") or "") or _IME_USER_LAST[0] or IME_FALLBACK
    print("[wxbridge] 输入法还留在「DSH 输入」上（%s）→ 自动还回 %s"
          % ("借用超时 %.0fs" % (time.time() - at) if at else "没有借用登记", back),
          flush=True)
    ime_set(back)
    if _ime_now() == IME_OURS:
        print("[wxbridge] ⚠️ 自动还回没成功，下一轮再试", flush=True)
        return
    try:
        # 用户报的就是"不知道键盘怎么没了" —— 还回来的这一下要说一声。
        # ★ 2026-10-14：统一走 `_hud_released()`，标题正文和"借屏结束"完全一致，
        #   只有细节行说明这次是"打字借走了输入法没还"（原来是另一套「输入法已还回」）。
        _hud_released(detail="刚才它打字借走了输入法没还，现在恢复成 %s"
                             % back.split("/")[0])
    except Exception:
        pass


def reclaim_watch():
    """后台线程：用户看完微信又切走了，就替他把微信收回来。

    ⚠️ 缓冲时长 = ``RECLAIM_IDLE``（2026-10-04 用户定的 10 秒；★ 2026-10-14 用户
    看了「你把微信抢回手上 → 机器人读不到画面」的三次日志后，改成 **5 秒** ——
    他切走后更快恢复，代价是"切出去再马上切回来"可能被抓走，见常量处的说明）。
    他拿手机看一眼、按 home，几秒后机器人自己恢复，全程零操作。

    ★ 2026-10-09（续13）：多了一件"卡在搜索页就点返回"的活（`_unstick_search`），
    而且那一件**在 `bot_running()` 判断之前**做 —— 机器人没跑起来时微信卡住，
    正是最需要有人管的时候（今天 12:14 的故障）。
    ★ 2026-10-09（续14）：再多一件"输入法没还回去就替他还"（`_ime_unstick`），
    同样排在 `bot_running()` 之前 —— 桥接重启留下的残留没人管的话，
    用户的键盘就一直回不来（用户报的"桌面上还有一个输入法没按下去"）。
    """
    idle = 0.0
    search_since = 0.0
    while True:
        time.sleep(RECLAIM_TICK)
        try:
            try:
                _ime_unstick()
            except Exception as exc:
                print("[wxbridge] 输入法看护异常：%r" % (exc,), flush=True)
            tops = _tops()
            _d = did()
            if not tops:
                # ★ 2026-10-10：读不到两屏状态（adb 抖 / 连命令守护也断了）→ 什么都别做。
                #   「读不到」≠「两屏都没有微信」—— 照旧累加下去会把用户手上的微信
                #   夺回虚拟屏（用户报的「我退出微信，机器人又来抢」里最坏的一种）。
                #   口径同 physical_input_taken：读不到就保守，宁可这一轮不做。
                idle = 0.0
                continue
            try:
                _sweep_stray_keyboard(tops)
            except Exception as exc:
                print("[wxbridge] 看护扫键盘异常：%r" % (exc,), flush=True)
            search_since = _unstick_search(tops, _d, search_since)
            if not bot_running():
                idle = 0.0
                continue
            if _d is not None and tops.get(_d, "").startswith("com.tencent.mm"):
                idle = 0.0          # 微信好好待在虚拟屏
                continue
            if tops.get(0, "").startswith("com.tencent.mm"):
                idle = 0.0          # 用户正在物理屏看微信 —— 不打扰
                # ★ 顺手修分辨率：用户从虚拟屏点开的微信会粘 280dpi（字特别小）
                _fix_physical_wx_density()
                continue
            idle += RECLAIM_TICK
            if idle < RECLAIM_IDLE:
                continue
            idle = 0.0
            with _lock:
                # 等锁的这段时间用户可能又把微信拉回去了，持锁后再判一次
                if _tops().get(0, "").startswith("com.tencent.mm"):
                    continue
                print("[wxbridge] 自动归还：两屏都没有微信且持续 %.0fs，收回虚拟屏"
                      % RECLAIM_IDLE, flush=True)
                reclaim()
        except Exception as exc:
            print("[wxbridge] 自动归还线程异常：%r" % (exc,), flush=True)
            idle = 0.0


class Handler(BaseHTTPRequestHandler):
    # ⚠️ 客户端断连（BrokenPipe / ConnectionReset）**绝不能**把桥接拖垮。
    #    2026-10-03 实测：机器人一次请求超时后自己断开，桥接写响应时抛
    #    BrokenPipeError —— 异常冒到 handle_one_request 外面被 socketserver
    #    记成"处理请求时出错"，整条链路随后开始 connection refused，机器人连着
    #    好几轮「读不到画面」，看护进程只好反复重启桥接。这里全部就地吞掉。
    DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                  TimeoutError)

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except self.DISCONNECT as exc:
            print("[wxbridge] 客户端提前断开（忽略，不影响服务）：%r" % (exc,),
                  flush=True)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self.DISCONNECT as exc:
            # 多半是调用方超时/被杀，响应没人收 —— 记录一下即可，服务继续跑
            print("[wxbridge] 响应写不出去（客户端已断开）：%r" % (exc,), flush=True)
        except Exception as exc:
            print("[wxbridge] 响应异常（已兜住）：%r" % (exc,), flush=True)

    def do_GET(self):
        _ensure_screen_known()      # ★ 每次请求先定档位（half/full）
        # ⚠️ /health 必须**在锁外**先回答：`/snapshot`（3~4 秒）和 `/send` 全程持有
        #    全局锁，而看护线程探测只给 4 秒超时 —— 排队等锁就会被判「桥接没在跑」，
        #    然后看护把桥接整个重启掉，正在读屏的机器人当场 connection refused。
        #    探活接口本来也不该碰任何重活，这里只回一个缓存过的屏号。
        if self.path.startswith("/health"):
            # ★ 2026-10-08 审查修复（F3 配套）：探活只读缓存，绝不触发重探
            # ★ 2026-10-08 晚：带上 OCR 状态（只读自检结果，不在探活路径重探）。
            _h = {"ok": True, "displayId": display_id(cached_only=True),
                  "adb": adb_ok(), "ocr": _OCR_STATUS,
                  "adb_flips": _ADB_STAT["flips"],
                  "adb_remote_nonzero": _ADB_STAT["remote_nonzero"],
                  "adb_last_err": _ADB_STAT["last_err"],
                  "snap_cache": {"hit": _SNAP_CACHE["hit"],
                                 "miss": _SNAP_CACHE["miss"]}}
            if _OCR_STATUS is False:
                _h["ocr_error"] = str(_OCR_ERR)[:120]
            self._json(_h)
            return
        if self.path.startswith("/peek"):
            # ★ 2026-10-10：给机器人的「退避期间轻探」用 —— 只跑一次 dumpsys，
            #   看微信在不在虚拟屏前台。比 /where（截图 + OCR）便宜两个数量级。
            try:
                on = bool(wechat_front())
            except Exception:
                on = False
            self._json({"ok": True, "on_screen": on})
            return
        if self.path.startswith("/screen"):
            # ★ 2026-10-11：给机器人的「锁屏挂起」用 —— 只跑一次 dumpsys power，
            #   比 /where（截图 + OCR）便宜几个数量级，可以每轮都问。
            #   awake=None 表示读不到（调用方自己决定保守方向，见 screen_awake）。
            try:
                aw = screen_awake()
            except Exception:
                aw = None
            self._json({"ok": True, "awake": aw})
            return
        if self.path.startswith("/where"):
            # ★ 2026-10-08 审查修复（H4）：新增"我现在在哪个会话"的探针。
            #   原来 AndroidSender.is_ready() 是拿 /health 判断的，而 /health 只回
            #   桥接活没活 + 屏号，**跟会话毫无关系** —— 于是 bot.py 里那句
            #   "等待期间已离开目标群，取消本次发送"在手机端根本不成立：            #   会话被切走了它照样返回 True。这里在锁外只读地回一个结果。
            want = _default_chat()
            if not want:
                self._json({"ok": True, "in_target": None,
                            "chat_title": "", "target": ""})
                return
            try:
                _p = "/tmp/wxbridge-where-probe.jpg"
                title = _chat_title_now(_p) if shot_wechat(_p) else ""
                _clean = title.replace("…", ".").strip(". ")
                if len(_clean) < 2:
                    in_target = None      # 读不出 → 不确定（发送路径会 fail-closed）
                else:
                    in_target = bool(wxreader.match_title(title, want))
                self._json({"ok": True, "in_target": in_target,
                            "chat_title": title, "target": want})
            except Exception as exc:
                self._json({"ok": True, "in_target": None, "chat_title": "",
                            "target": want, "error": str(exc)[:120]})
            return
        with _lock:
            if self.path.startswith("/diag"):
                # 临时诊断窗：在**桥接进程内部**看 OCR 到底怎么了
                d = {"wxreader_file": getattr(wxreader, "__file__", None),
                     "CHAT_TOP": getattr(wxreader, "CHAT_TOP", None),
                     "engine_is_none": getattr(wxreader, "_ocr", "?") is None}
                f = "/tmp/wxbridge-tab-fail.jpg"
                try:
                    d["read_title_fail_img"] = wxreader.read_title(f)
                except Exception as e:
                    d["read_title_err"] = repr(e)
                try:
                    import numpy as _np2
                    from PIL import Image as _Im2
                    a = _np2.array(_Im2.open(f).convert("RGB"))
                    d["img_shape"] = list(a.shape)
                    top = getattr(wxreader, "CHAT_TOP", 72)
                    res = wxreader.ocr_call(a[:top])
                    d["crop_ocr_n"] = len(res[0]) if res and res[0] else 0
                    d["crop_ocr_first"] = (str(res[0][0][1]) if res and res[0] else None)
                except Exception as e:
                    d["crop_err"] = repr(e)
                self._json(d)
            elif self.path.startswith("/title"):
                img = "/tmp/wxbridge-title.jpg"
                ensure_wechat_front()
                if not wechat_front():
                    self._json({"ok": False, "error": "微信不在前台"})
                elif not shot_wechat(img):
                    self._json({"ok": False, "error": "截图失败（微信不在虚拟屏前台）"})
                else:
                    self._json({"ok": True, "chat_title": wxreader.read_title(img)})
            elif self.path.startswith("/snapshot"):
                self._json(snapshot(full="full=1" in self.path,
                                    images="images=1" in self.path))
            else:
                self._json({"ok": False, "error": "unknown path"}, 404)

    def do_POST(self):
        _ensure_screen_known()      # ★ 每次请求先定档位（half/full）
        if self.path.startswith("/takeover"):
            with _lock:
                ok = takeover()
            self._json({"ok": bool(ok), "physical": top_activity_of(0)})
            return
        if self.path.startswith("/reclaim"):
            # 手动「收回微信」按钮：搬回虚拟屏 + 切回目标群（一步到位）
            with _lock:
                ok = reclaim()
            self._json({"ok": bool(ok), "physical": top_activity_of(0)})
            return
        if self.path.startswith("/open"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                self._json({"ok": False, "error": "bad body"}, 400)
                return
            with _lock:
                ok = open_chat(str(payload.get("chat") or ""))
            self._json({"ok": bool(ok)})
            return
        if self.path.startswith("/backfill"):
            # ★ 2026-10-10：手动补读（往上翻 N 屏读回刷过去的消息，再切回最新）。
            #   body: {"screens": 2, "chat": "目标群"}
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            with _lock:
                out = backfill(payload.get("screens") or 2,
                               str(payload.get("chat") or ""),
                               bool(payload.get("images")))
            self._json(out)
            return
        if self.path.startswith("/read_record"):
            # ★ 2026-10-10：点开屏幕上那张聊天记录卡片，把里面的文字读回来。
            #   body 可选 {"match": "..."}；不传就只认「XX的聊天记录」这个标题格式。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            with _lock:
                out = read_record(str(payload.get("match") or ""))
            self._json(out)
            return
        if self.path.startswith("/zoom_image"):
            # ★ 2026-10-15：把屏幕上那张图点开放大、截一张、立刻退回（缩略图小字读不清时用）
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            with _lock:
                out = zoom_image(str(payload.get("img_hash") or ""),
                                 str(payload.get("sender") or ""))
            self._json(out)
            return
        if self.path.startswith("/read_card"):
            # ★ 2026-10-15：点开"某人的那条消息"读整页 OCR（卡片不是图片时用）
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                payload = {}
            with _lock:
                out = read_card(str(payload.get("sender") or ""),
                                str(payload.get("match") or ""))
            self._json(out)
            return
        if self.path.startswith("/send_image"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                self._json({"ok": False, "error": "bad body"}, 400)
                return
            with _lock:
                ok, err, retryable = send_image(str(payload.get("path") or ""),
                                                str(payload.get("chat") or ""))
            # ★ 2026-10-09：把**具体原因**透给面板 —— 原来只有 {"ok": false}，
            #   面板只好显示「发图失败：接口没回 ok」，用户根本不知道是哪种没发出去。
            # ★ 2026-10-09 晚：再加 retryable —— 机器人靠它区分"稍后可以再试"
            #   （你占用手机这类临时状态）与"别重试"（发送键已经点过了）。
            self._json({"ok": bool(ok), "error": "" if ok else str(err or ""),
                        "retryable": bool(retryable)})
            return
        if not self.path.startswith("/send"):
            self._json({"ok": False, "error": "unknown path"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            self._json({"ok": False, "error": "bad body"}, 400)
            return
        text = str(payload.get("text") or "")
        # force：面板上用户主动点的那一下才带 —— 允许抢一次物理屏焦点（见 send_text）
        force = bool(payload.get("force"))
        with _lock:
            ok, err = send_text(text, force=force)
        # ★ 2026-10-10：retryable 让机器人区分「你占用手机（可补发、不计熔断）」
        #   与真失败（见模块里 _SEND_RETRYABLE 的说明）。
        self._json({"ok": bool(ok), "error": err,
                    "retryable": bool(_SEND_RETRYABLE[0])})


# ★ 2026-10-08 晚：把 OCR 自检结果**记下来**，让 /health 能报出去。
#   起因：桥接在缺 PYTHONPATH 的环境里起来（改脚本时注释打断了续行），OCR 直接
#   ModuleNotFoundError，而 /health 照样回 {"ok":true,...} —— **瞎着却报健康**，
#   排查时差点被骗过去。现在 /health 多一个 ocr 字段（只读这里记下的结果，
#   绝不在探活路径上触发重探）。
_OCR_STATUS = None          # None=还没自检  True=可用  False=不可用
_OCR_ERR = ""


def ocr_selfcheck():
    """启动自检：OCR 到底能不能用？**必须一声喊出来**。

    2026-10-03 血的教训：启动脚本里一个续行符写错，PYTHONPATH 被赋给了 echo，
    桥接于是在没有 PYTHONPATH 的环境里起来 —— `import rapidocr_onnxruntime` 失败，
    而 read_title 里那个 `except Exception: return ""` 把 ImportError 静默吞成空字符串。
    表现是"机器人一启动就切群失败退出"，日志里一点线索都没有，挖了两小时。
    这里启动就跑一遍 OCR，能用/不能用都印在日志开头。
    """
    global _OCR_STATUS, _OCR_ERR
    if wxreader is None:
        print("[wxbridge] ❌ OCR 自检：wxreader 模块根本没导入进来 —— 查 PYTHONPATH！", flush=True)
        _OCR_STATUS, _OCR_ERR = False, "wxreader 模块没导入（查 PYTHONPATH）"
        return False
    try:
        import numpy as _np
        blank = _np.full((40, 200, 3), 255, dtype=_np.uint8)
        wxreader.ocr_call(blank)          # 空白图：只要引擎能跑通就算通过
        print("[wxbridge] ✅ OCR 自检通过（引擎可调用）", flush=True)
        _OCR_STATUS, _OCR_ERR = True, ""
        return True
    except Exception as exc:
        print("[wxbridge] ❌ OCR 自检失败：%r —— 查 PYTHONPATH / ocrlibs，"
              "否则后面所有「读标题」都会静默返回空" % (exc,), flush=True)
        _OCR_STATUS, _OCR_ERR = False, repr(exc)
        return False


def main():
    did = display_id()
    print("[wxbridge] 启动：slot=%d displayId=%s port=%d" % (SLOT, did, PORT), flush=True)
    ocr_selfcheck()
    # ★ 2026-10-09（续14）：启动先记一次"用户自己的输入法" —— 万一上一次是被杀在
    #   借用途中（默认输入法停在「DSH 输入」上），看护要还回的就是它。
    #   ⚠️ 这时候读到 DshIme 是**正常**的（上一轮没还完），所以别把它记成用户的。
    _boot_ime = _ime_now()
    if _boot_ime and _boot_ime != IME_OURS:
        _IME_USER_LAST[0] = _boot_ime
    print("[wxbridge] 默认输入法：%s（用户的输入法记作 %s）"
          % (_boot_ime or "?", _IME_USER_LAST[0] or IME_FALLBACK), flush=True)
    threading.Thread(target=reclaim_watch, daemon=True).start()
    print("[wxbridge] 自动归还已开启：两屏都没有微信且持续 %.0fs → 收回虚拟屏并切回群"
          % RECLAIM_IDLE, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
