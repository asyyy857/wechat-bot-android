# -*- coding: utf-8 -*-
"""今日美图：每次触发从网上随机取一张图，取不到就退本地图库。

为什么不复用 stickers（表情包库）
--------------------------------
表情包是**本地固定图库**，挑图是纯文件操作、瞬时完成。美图不一样，有三道坎：

* 要**联网下载**（单张 1~5MB），可能慢、可能断、可能撞上失效的接口；
* 图源清一色返回 **webp**（2026-10-08 实测 t.alcy.cc / loliapi 全是 webp），
  而微信的「分享图片」通道不认 webp —— 必须转成 JPEG 再发；
* 原图动辄 1920×1200 / 1242×2688，直接发要等很久（发图本身就有 120 秒超时），
  顺手缩到长边 ``max_edge`` 能明显加快，也省流量。

所以这里单开一个模块：下载 → 嗅探格式 → 转 JPEG → 进缓存目录 → 返回路径。
整个过程有**单张超时**和**整体预算**，超了就退本地图库，绝不把机器人卡死
（主循环 12~20 秒一帧，卡一次就漏一大段群消息）。
"""

from __future__ import annotations

from .paths import APP_DIR, DATA_DIR
import glob
import hashlib
import io
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

from .netutil import fetch_hard
from typing import Dict, List, Optional

# 单张图的兜底上限（图片真实字节数），防止某个接口突然吐一个超大文件把内存顶爆
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
# 转出来的 JPEG 长边上限：1600 够手机屏看了，再大只是徒增发送时间
DEFAULT_MAX_EDGE = 1600
# 小于这个边长的图当"错误占位图"丢掉（有些接口挂了会返回一张小 logo）
DEFAULT_MIN_EDGE = 200
# 默认图源：分类名 -> 候选接口（每次随机挑分类、分类里再随机挑一条）
DEFAULT_SOURCES: Dict[str, List[str]] = {
    "二次元": [
        "https://t.alcy.cc/moe",
        "https://t.alcy.cc/ycy",
        "https://www.loliapi.com/acg/",
    ],
    "真人写真": [
        "https://pic.ltywl.top/mn/api.php",
    ],
}

# 浏览器 UA：有些图站对空 UA / python-urllib 直接 403
_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Mobile Safari/537.36")

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class MeituFetcher:
    """取一张能直接发给微信的美图（返回本地 JPEG 绝对路径）。"""

    def __init__(self, cfg=None, root=None, logger=None) -> None:
        self.logger = logger
        # cfg 可以是 dict，也可以是 config.py 里那种支持属性访问的对象；两种都读
        get = self._getter(cfg)
        self.enabled = bool(get("enabled", True))
        self.use_network = bool(get("use_network", True))
        raw_sources = get("sources", None)
        self.sources: Dict[str, List[str]] = {}
        # ⚠️ 这里**不能**只判 isinstance(dict)：config.py 的 NS 会把嵌套 dict 也包成
        #    NS 对象（cfg.meitu.sources 拿到的是 NS 而不是 dict），只认 dict 的话
        #    用户配的图源会被静默忽略、悄悄退回内置默认值 —— 2026-10-08 写这个
        #    模块时就是这么踩的。所以统一走 _as_dict()，两种都吃。
        for k, v in self._as_dict(raw_sources).items():
            if isinstance(v, str):
                v = [v]
            elif not isinstance(v, (list, tuple)):
                v = list(self._as_dict(v).values())    # 极端情况：列表也被包了一层
            urls = [str(u).strip() for u in (v or []) if str(u).strip()]
            if urls:
                self.sources[str(k)] = urls
        if not self.sources:
            self.sources = {k: list(v) for k, v in DEFAULT_SOURCES.items()}

        base = Path(root) if root else DATA_DIR
        self.cache_dir = self._mkpath(base, get("cache_dir", "美图缓存"))
        self.local_dir = self._mkpath(base, get("local_dir", "美图库"))

        # 本地图集：名字 -> 目录（2026-10-08 用户要求「看看腿」）。
        # 规则里写 meitu_source: 看看腿 就**只**从这个目录随机挑、不联网 ——
        # 这类图集是用户自己一张张攒的，混进网图就串味了。
        # 目录可以是绝对路径（用户把图放在手机相册里，如
        # /sdcard/DCIM/MyAlbums/看看腿），也可以是相对本项目的路径。
        # ⚠️ 一个名字可以对应**多个目录**（写成列表）：手机相册很容易变成
        #   「看看腿」+「看看腿_1」这种一对 —— ColorOS 建同名相册时会自动加后缀，
        #   用户根本分不清自己加到了哪个（2026-10-08 就真发生了：配置指的那个
        #   被腾空，图全在 _1 里）。都配上就无论他加哪个都能发到。
        #   路径还可以带通配符（如 ``/sdcard/DCIM/MyAlbums/看看腿*``）——
        #   ColorOS 会一个接一个地生 看看腿_1 / _2 / _3…，通配一次配好就不用再管。
        #   ⚠️ 这里只存**原始写法**，通配符留到每次发图时再展开（local_set_dirs）。
        #   用户是「先要功能、后建相册」的（2026-10-08：他说"我会创一个看看腹肌的
        #   图库，到时候你就发出去"，而当时目录还不存在）—— 如果启动时展开一次：
        #   ① 目录不存在 → glob 空 → 整个图集消失 → 规则会掉到**网络图源**上去发
        #      网图，完全不是他要的；
        #   ② 他建好目录后还得再重启一次才生效。
        #   延迟展开两个问题一起解决。
        self._base = base
        self._local_set_specs: Dict[str, List[str]] = {}
        for k, v in self._as_dict(get("local_sets", None)).items():
            vals = list(v) if isinstance(v, (list, tuple)) else [v]
            specs = [str(x).strip() for x in vals if str(x or "").strip()]
            if specs:
                self._local_set_specs[str(k).strip()] = specs

        self.timeout = float(get("timeout", 12) or 12)
        # 一次触发最多在网络上花多久（秒）。超过就退本地 —— 别让机器人干等
        self.budget = float(get("budget", 25) or 25)
        # 一次触发最多试几个候选接口（接口挂了就换下一个，但别无线换）
        self.max_tries = int(get("max_tries", 3) or 3)
        self.max_bytes = int(get("max_bytes", DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES)
        self.max_edge = int(get("max_edge", DEFAULT_MAX_EDGE) or DEFAULT_MAX_EDGE)
        self.min_edge = int(get("min_edge", DEFAULT_MIN_EDGE) or DEFAULT_MIN_EDGE)
        self.quality = int(get("quality", 85) or 85)
        # 缓存里最多留几张（按 mtime 淘汰最旧的）
        self.keep_cached = int(get("keep_cached", 40) or 40)
        self.rng = random.Random()
        # 源文件 → 内容标识的缓存（见 _content_id），key 的作用是文件变了自动重算
        self._id_cache: Dict[Path, tuple] = {}

    # ------------------------------------------------------------ 小工具
    @staticmethod
    def _getter(cfg):
        if cfg is None:
            return lambda k, d=None: d
        if isinstance(cfg, dict):
            return lambda k, d=None: cfg.get(k, d)
        return lambda k, d=None: getattr(cfg, k, d)

    @staticmethod
    def _as_dict(obj) -> Dict[str, object]:
        """dict / config.NS 都能当字典读（NS 会把嵌套 dict 也包成 NS）。"""
        if isinstance(obj, dict):
            return dict(obj)
        d = getattr(obj, "__dict__", None)
        return dict(d) if isinstance(d, dict) else {}

    @staticmethod
    def _mkpath(base: Path, name) -> Path:
        p = Path(str(name))
        return p if p.is_absolute() else (base / p)

    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)

    # ------------------------------------------------------------ 对外
    def fetch(self, source: str = "",
              avoid: Optional[List[str]] = None) -> Optional[Path]:
        """取一张图。

        * ``source`` 指定分类（如 ``二次元``），留空 = 所有分类混池
        * ``avoid`` 里的文件名尽量避开（用来"每次换一张"）
        * 网络取不到 → 退本地图库；本地也没有 → 返回 None
        """
        if not self.enabled:
            return None
        avoid = list(avoid or [])
        want = str(source or "").strip()

        # ① 本地图集优先：source 指到某个图集（如「看看腿」）就只挑那个目录
        if want in self._local_set_specs:
            return self._pick_from_dirs(self.local_set_dirs(want), avoid,
                                        f"本地图集「{want}」")

        # ② 网络图源
        pool = self._pool(want)
        if self.use_network and pool:
            p = self._fetch_network(pool, avoid)
            if p is not None:
                return p

        # ③ 网络不给力 → 本地兜底图库（美图库/）
        return self._pick_from_dirs([self.local_dir], avoid, "本地美图库兜底")

    # ------------------------------------------------------------ 网络
    def _pool(self, source: str) -> List[str]:
        """要试的候选接口列表。"""
        want = str(source or "").strip()
        if want:
            for name, urls in self.sources.items():
                if name == want or want in name:
                    return list(urls)
            self._log(f"美图分类「{want}」没配图源，改用全部", "warning")
        out: List[str] = []
        for urls in self.sources.values():
            out.extend(urls)
        return out

    def _fetch_network(self, pool: List[str], avoid: List[str]) -> Optional[Path]:
        started = time.time()
        urls = list(pool)
        self.rng.shuffle(urls)
        for idx, url in enumerate(urls[:max(1, self.max_tries)]):
            left = self.budget - (time.time() - started)
            if left <= 3:
                self._log(f"美图下载预算用完（{self.budget:.0f}s），不再试下一个源", "warning")
                break
            try:
                raw = self._download(url, timeout=min(self.timeout, left))
                path = self._to_jpeg(raw, avoid=avoid)
                if path is None:
                    continue
                self._log(f"美图取到了：{path.name}（{len(raw)//1024}KB ← {url}）")
                return path
            except Exception as exc:                       # 网络问题一律换下一个源
                self._log(f"美图图源失败（{type(exc).__name__}: {exc}）：{url}", "warning")
        return None

    def _download(self, url: str, timeout: float) -> bytes:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*",
            "Referer": url,
        })
        def _read(resp):
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            # 有些接口挂掉会返回 HTML 错误页（HTTP 200），早点识破
            if ctype and not ctype.startswith("image/"):
                raise ValueError(f"返回的不是图片（Content-Type={ctype}）")
            return resp.read(self.max_bytes + 1)

        data = fetch_hard(req, max(4.0, timeout), _read)
        if not data:
            raise ValueError("下载到 0 字节")
        if len(data) > self.max_bytes:
            raise ValueError(f"图片超过 {self.max_bytes // 1024 // 1024}MB 上限")
        return data

    def _to_jpeg(self, raw: bytes, avoid: List[str],
                 strict_avoid: bool = True) -> Optional[Path]:
        """webp/png/gif → JPEG，缩到 max_edge，写进缓存目录。

        ``strict_avoid=False`` 用在本地图集上：那边已经在挑选阶段过滤过一次了，
        这里再硬拦一次会让**只有一两张图的图集**第二次就发不出来
        （用户自己攒的「看看腿」就是这种，重复发同一张也比发不出去强）。
        """
        from PIL import Image                          # 延迟导入：不触发这个功能就不吃内存
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()                                 # 动图只取第一帧
        except Exception as exc:
            raise ValueError(f"不是能解析的图片（{exc}）")

        w, h = img.size
        if min(w, h) < self.min_edge:
            raise ValueError(f"图片太小（{w}×{h}），当错误占位图丢掉")

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        # 内容指纹做文件名：同一张图重复抓到会命中同名文件，
        # 于是 avoid 列表能直接把它挡掉（这是"每次换一张"的兜底）
        name = f"meitu_{hashlib.md5(raw).hexdigest()[:12]}.jpg"
        if strict_avoid and name in avoid:
            self._log(f"这张刚发过（{name}），换一张")
            return None

        if max(w, h) > self.max_edge:
            img.thumbnail((self.max_edge, self.max_edge), Image.LANCZOS)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        out = self.cache_dir / name
        img.save(out, "JPEG", quality=self.quality, optimize=True)
        self._prune_cache()
        return out

    def _prune_cache(self) -> None:
        """缓存只留最近 keep_cached 张，别把手机存储吃满。"""
        keep = max(4, self.keep_cached)
        try:
            files = [p for p in self.cache_dir.iterdir()
                     if p.is_file() and p.suffix.lower() in IMG_EXTS]
        except OSError:
            return
        if len(files) <= keep:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            try:
                p.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ 本地图库
    def is_local_set(self, name: str) -> bool:
        """这个名字是不是配成了本地图集。

        用来区分「取不到图时要不要在群里回一句话」：网络美图取不到，回一句
        "博主跑路了"是有用的（告诉群友机器人收到了）；而本地图集空着多半是
        **用户还没往里放图**，那种时候往群里说一句反而莫名其妙，静默跳过更好。
        """
        return str(name or "").strip() in self._local_set_specs

    def local_set_dirs(self, name: str) -> List[Path]:
        """把图集名解析成实际目录列表（**每次调用都重新展开通配符**）。

        这样用户建好相册/往里加图，机器人下一张就能读到，不用重启。
        通配符一个都没匹配到时返回空列表 —— 调用方会报"目录不存在"并静默跳过，
        **不会**掉到网络图源上去。
        """
        out: List[Path] = []
        for s in self._local_set_specs.get(str(name or "").strip(), []):
            if any(ch in s for ch in "*?["):
                out.extend(Path(m) for m in sorted(glob.glob(s)))
            else:
                out.append(self._mkpath(self._base, s))
        return out

    def _content_id(self, p: Path) -> str:
        """源文件 → 它的**发布标识**（就是 _to_jpeg 会生成的缓存名，按内容 md5）。

        ★ 2026-10-08 用户问「发图是随机发的吗」时实测抓到的 bug：
        bot 那边 ``avoid`` 记的是**缓存文件名**（``meitu_<md5>.jpg``），
        而这里原来拿 ``p.name``（源文件名，如 ``HT7EOEHb0AAQQVc.jpg``）去比 ——
        两边永远对不上，于是**「最近发过的不重复」完全没生效**
        （实测 100 次里出现了连着两次发同一张）。

        现在两边统一按内容 md5 比。算过的按 (大小, mtime) 缓存，
        文件被改/替换了自动重算；相册里同一张图存成两个文件名
        （``X.jpg`` / ``X_1.jpg``）也会被认成同一张，不会连着发两遍。
        """
        try:
            st = p.stat()
        except OSError:
            return ""
        key = (st.st_size, int(st.st_mtime))
        hit = self._id_cache.get(p)
        if hit and hit[0] == key:
            return hit[1]
        try:
            cid = "meitu_" + hashlib.md5(p.read_bytes()).hexdigest()[:12] + ".jpg"
        except OSError:
            return ""
        self._id_cache[p] = (key, cid)
        return cid

    @staticmethod
    def _is_junk(p: Path) -> bool:
        """手机回收站 / 隐藏文件不算图。

        ⚠️ 2026-10-08 实测踩到的：ColorOS 把用户删掉的图**原地改名**成
        ``.trashed-1794055597-Image_1791090441234_119.jpg`` 留在相册目录里，
        后缀还是 .jpg —— 不过滤的话**用户已经删掉的图照样会被发进群**。
        顺带把 ``.`` 开头的隐藏文件一起挡掉（缩略图缓存之类）。
        """
        n = p.name
        return n.startswith(".") or n.startswith("trashed-")

    def _pick_from_dirs(self, dirs: List[Path], avoid: List[str],
                        why: str) -> Optional[Path]:
        """从若干个本地目录里随机挑一张图（本地图集 / 网络兜底都走这里）。

        三条口径：
        · **多个目录会合并成一个池子**再随机（相册「看看腿」/「看看腿_1」那种）；
        · 本地图统一转成 JPEG 进缓存 —— 发送路径统一，顺手把大图缩小，
          免得用户投一张 8MB 原图把发图流程拖到超时；
        · ``avoid``（最近发过的文件名）只是**尽量**避开：过滤完一张不剩时，
          就忽略它重新用全部图 —— 用户自己攒的图集常常只有一两张，
          硬过滤会让它"发过一次就再也不发了"（表情包库 pick() 也是这个口径）。
        """
        all_imgs: List[Path] = []
        for d in dirs:
            if not d.is_dir():
                self._log(f"目录不存在：{d}（{why}）", "warning")
                continue
            all_imgs.extend(p for p in sorted(d.iterdir())
                            if p.is_file() and p.suffix.lower() in IMG_EXTS
                            and not self._is_junk(p))
        if not all_imgs:
            self._log(f"目录里没有可用图片：{[str(d) for d in dirs]}（{why}）",
                      "warning")
            return None
        # ⚠️ 比的是**内容标识**而不是文件名 —— avoid 里是 bot 记的缓存名
        #    （meitu_<md5>.jpg），见 _content_id 的说明（这里踩过坑）
        pool = [p for p in all_imgs if self._content_id(p) not in avoid] or all_imgs
        random.shuffle(pool)
        for src in pool[:3]:
            try:
                # strict_avoid=False：上面已经过滤过了，这里再拦一次会让
                # 小图集发不出第二张
                path = self._to_jpeg(src.read_bytes(), avoid=avoid,
                                     strict_avoid=False)
                if path is not None:
                    self._log(f"{why}：{src.name} → {path.name}")
                    return path
            except Exception as exc:
                self._log(f"本地图用不了（{src.name}：{exc}）", "warning")
        return None
