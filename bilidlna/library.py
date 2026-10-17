"""把 B 站内容组织成 DLNA 目录树。

对象 ID 用 "/" 拼接路径，父 ID 直接去掉最后一段即可得到：
  0                      根
  0/rec                  推荐
  0/fav/123              收藏夹 123
  0/rec/v~BVxxx~456      视频条目（cid=456，0 表示播放时再解析）
  0/fav/123/bv~BVxxx     多 P 视频（容器）
  0/fav/123/bv~BVxxx/p~BVxxx~789~2   分 P
  0/live/hot/lv~12345    直播间
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse

from . import bili

log = logging.getLogger("library")

CACHE_TTL = 180  # 列表缓存秒数；播放器翻页时会重复 Browse 同一个目录

VR_RULES = [
    # (正则, 标签)  标签按 4XVR / Skybox 的文件名约定
    (re.compile(r"VR\s*180|180\s*°|180度|180VR|VR.*(?<!\d)180(?!\d)|(?<!\d)180(?!\d).*VR", re.I), "180"),
    (re.compile(r"360\s*°|360度|全景|VR\s*360|360\s*VR|360全景", re.I), "360"),
]
STEREO_RULES = [
    (re.compile(r"左右|SBS|side[- ]?by[- ]?side|\bLR\b|半宽", re.I), "LR"),
    (re.compile(r"上下|\bTB\b|\bOU\b|top[- ]?bottom", re.I), "TB"),
]


def vr_tag(title):
    """根据标题猜测 VR / 3D 格式，返回例如 '_180_LR'、'_360'、'_LR'，普通视频返回 ''。"""
    tags = []
    for rx, t in VR_RULES:
        if rx.search(title):
            tags.append(t)
            break
    stereo = ""
    for rx, t in STEREO_RULES:
        if rx.search(title):
            stereo = t
            break
    if not stereo and tags and tags[0] == "180":
        stereo = "LR"  # VR180 绝大多数是左右 3D
    if stereo:
        tags.append(stereo)
    return "".join("_" + t for t in tags)


def fmt_dur(sec):
    sec = int(sec or 0)
    return "%d:%02d:%02d.000" % (sec // 3600, sec % 3600 // 60, sec % 60)


class Node:
    __slots__ = ("id", "title", "container", "child_count", "url", "cover", "duration",
                 "author", "mime", "live", "size")

    def __init__(self, id, title, container=False, child_count=0, url="", cover="",
                 duration=0, author="", mime="video/mp2t", live=False, size=0):
        self.id = id
        self.title = title
        self.container = container
        self.child_count = child_count
        self.url = url
        self.cover = cover
        self.duration = duration
        self.author = author
        self.mime = mime
        self.live = live
        self.size = size

    @property
    def parent(self):
        return self.id.rsplit("/", 1)[0] if "/" in self.id else "-1"


class State:
    """持久化的用户数据：搜索词、手动添加的视频/直播间、设置。"""

    DEFAULTS = {
        "searches": ["360全景", "VR180 3D", "4K 风景"],
        "added": [],  # [{"type":"bv","id":"BV..","title":..}, {"type":"live","id":123,..}]
        "settings": {"max_qn": 120, "codec": "hevc", "live_codec": "avc", "vr_tags": True},
    }

    def __init__(self, path):
        self.path = path
        self.data = json.loads(json.dumps(self.DEFAULTS))
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k == "settings":
                    self.data["settings"].update(v)
                else:
                    self.data[k] = v
        except (OSError, ValueError):
            pass
        self.update_id = int(time.time()) % 100000

    @property
    def settings(self):
        return self.data["settings"]

    def save(self):
        self.update_id += 1
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


class Library:
    def __init__(self, client, state, base_url_fn):
        self.client = client
        self.state = state
        self.base_url = base_url_fn  # 返回 http://<局域网IP>:<端口>
        self._cache = {}

    # ------------------------------------------------------------ 工具
    async def _cached(self, key, fn, *args, ttl=CACHE_TTL):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = await asyncio.to_thread(fn, *args)
        self._cache[key] = (time.time(), val)
        return val

    def invalidate(self, prefix=""):
        for k in list(self._cache):
            if k.startswith(prefix):
                del self._cache[k]

    def img(self, url, w=480, h=270):
        if not url:
            return ""
        if "hdslb.com" in url and "@" not in url:
            url += "@%dw_%dh.jpg" % (w, h)
        return "%s/img?u=%s" % (self.base_url(), urllib.parse.quote(url, safe=""))

    def _file(self, title, tag):
        name = re.sub(r'[\\/:*?"<>|#%&\s]+', "_", title)[:60].strip("_") or "video"
        return urllib.parse.quote(name + tag + ".ts")

    def video_node(self, parent, v):
        tag = vr_tag(v.title) if self.state.settings.get("vr_tags", True) else ""
        title = v.title + ("  [%s]" % tag.strip("_").replace("_", " ") if tag else "")
        if v.pages > 1:
            return Node("%s/bv~%s" % (parent, v.bvid), "%s（%dP）" % (title, v.pages),
                        container=True, child_count=v.pages, cover=self.img(v.cover),
                        author=v.author)
        return Node("%s/v~%s~%d" % (parent, v.bvid, v.cid), title,
                    url="%s/vod/%s/%d/%s" % (self.base_url(), v.bvid, v.cid, self._file(v.title, tag)),
                    cover=self.img(v.cover), duration=v.duration, author=v.author)

    def live_node(self, parent, r):
        title = "%s - %s" % (r.uname, r.title) if r.uname else r.title
        tag = vr_tag(r.title) if self.state.settings.get("vr_tags", True) else ""
        return Node("%s/lv~%d" % (parent, r.room_id), "🔴 " + title + ("  [%s]" % tag.strip("_") if tag else ""),
                    url="%s/live/%d/%s" % (self.base_url(), r.room_id, self._file(r.title, tag)),
                    cover=self.img(r.cover), author=r.uname, live=True)

    def folder(self, id, title, count=20):
        return Node(id, title, container=True, child_count=count)

    # ------------------------------------------------------------ 浏览
    def root(self):
        return Node("0", "Bilibili", container=True, child_count=10)

    async def children(self, oid):
        """返回 oid 的子节点列表。"""
        parts = oid.split("/")
        last = parts[-1]
        login = self.client.logged_in
        if oid == "0":
            out = [self.folder("0/rec", "推荐"),
                   self.folder("0/hot", "热门"),
                   self.folder("0/rank", "排行榜"),
                   self.folder("0/vr", "VR / 3D 专区", 3)]
            if login:
                out += [self.folder("0/dyn", "关注动态"),
                        self.folder("0/later", "稍后再看"),
                        self.folder("0/fav", "收藏夹"),
                        self.folder("0/hist", "历史记录")]
            out += [self.folder("0/live", "直播", 3 if login else 2),
                    self.folder("0/search", "搜索", len(self.state.data["searches"])),
                    self.folder("0/added", "手动添加", len(self.state.data["added"]))]
            if not login:
                out.append(Node("0/login", "（未登录，最高 480P：电脑浏览器打开 %s 扫码登录）" % self.base_url(),
                                container=True, child_count=0))
            return out

        if last.startswith("bv~"):
            bvid = last[3:]
            view = await self._cached("view:" + bvid, self.client.view, bvid, ttl=3600)
            out = []
            for p in view.get("pages") or []:
                t = "P%d %s" % (p["page"], p.get("part") or view.get("title"))
                tag = vr_tag(view.get("title", "") + " " + (p.get("part") or ""))
                out.append(Node("%s/p~%s~%d~%d" % (oid, bvid, p["cid"], p["page"]), t,
                                url="%s/vod/%s/%d/%s" % (self.base_url(), bvid, p["cid"], self._file(t, tag)),
                                cover=self.img(view.get("pic")), duration=p.get("duration", 0),
                                author=(view.get("owner") or {}).get("name", "")))
            return out

        c = self.client
        if oid == "0/rec":
            # 每次进入推荐（缓存过期后）都是新的一批
            vids = await self._cached("rec", lambda: c.recommend(30, 1) + c.recommend(30, 2), ttl=60)
        elif oid == "0/hot":
            vids = await self._cached("hot", lambda: c.popular(1, 50) + c.popular(2, 50))
        elif oid == "0/rank":
            vids = await self._cached("rank", c.ranking, ttl=600)
        elif oid == "0/dyn":
            vids = await self._cached("dyn", c.dynamic_videos)
        elif oid == "0/later":
            vids = await self._cached("later", c.watch_later, ttl=30)
        elif oid == "0/hist":
            vids = await self._cached("hist", c.history, ttl=30)
        elif oid == "0/fav":
            folders = await self._cached("fav", c.fav_folders, ttl=300)
            return [self.folder("0/fav/%d" % fid, "%s（%d）" % (t, n), n) for fid, t, n in folders]
        elif len(parts) == 3 and parts[1] == "fav":
            vids = await self._cached("fav:" + last, c.fav_videos, int(last), ttl=60)
        elif oid == "0/vr":
            return [self.folder("0/vr/360", "360° 全景"),
                    self.folder("0/vr/180", "VR180 3D"),
                    self.folder("0/vr/3d", "3D 左右格式")]
        elif len(parts) == 3 and parts[1] == "vr":
            kw = {"360": "360度全景视频", "180": "VR180", "3d": "3D左右格式"}[last]
            vids = await self._cached("vr:" + last, c.search, kw, ttl=1800)
        elif oid == "0/live":
            out = []
            if login:
                out.append(self.folder("0/live/follow", "我关注的（正在直播）"))
            out += [self.folder("0/live/hot", "热门直播"),
                    self.folder("0/live/vr", "VR / 全景直播")]
            return out
        elif oid == "0/live/follow":
            rooms = await self._cached("live:follow", c.live_following, ttl=30)
            return [self.live_node(oid, r) for r in rooms]
        elif oid == "0/live/hot":
            rooms = await self._cached("live:hot", c.live_hot, ttl=60)
            return [self.live_node(oid, r) for r in rooms]
        elif oid == "0/live/vr":
            rooms = await self._cached("live:vr", c.live_search, "VR", ttl=120)
            return [self.live_node(oid, r) for r in rooms]
        elif oid == "0/search":
            return [self.folder("0/search/%d" % i, "🔍 " + kw)
                    for i, kw in enumerate(self.state.data["searches"])]
        elif len(parts) == 3 and parts[1] == "search":
            i = int(last)
            kws = self.state.data["searches"]
            if i >= len(kws):
                return []
            kw = kws[i]
            vids = await self._cached("search:" + kw, c.search, kw, ttl=600)
        elif oid == "0/added":
            return await self._added()
        else:
            return []
        return [self.video_node(oid, v) for v in vids]

    async def _added(self):
        out = []
        for it in self.state.data["added"]:
            if it["type"] == "live":
                r = bili.LiveRoom(it["id"], it.get("title", "直播间 %s" % it["id"]),
                                  it.get("cover", ""), it.get("uname", ""))
                out.append(self.live_node("0/added", r))
            else:
                v = bili.Video(it["id"], it.get("title", it["id"]), it.get("cover", ""),
                               it.get("duration", 0), it.get("author", ""), it.get("cid", 0),
                               it.get("pages", 1))
                out.append(self.video_node("0/added", v))
        return out

    async def lookup(self, oid):
        """BrowseMetadata：找到 oid 自身的节点。"""
        if oid == "0":
            return self.root()
        parent = oid.rsplit("/", 1)[0]
        try:
            for n in await self.children(parent):
                if n.id == oid:
                    return n
        except Exception:
            log.exception("lookup %s", oid)
        return Node(oid, oid.rsplit("/", 1)[-1], container=True)

    # ------------------------------------------------------------ 手动添加
    async def add(self, text):
        """接受 BV号 / av号 / 视频链接 / 直播间号 / 直播链接 / b23.tv 短链。"""
        text = text.strip()
        if "b23.tv" in text:
            text = await asyncio.to_thread(self._expand_short, text)
        m = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", text) or re.fullmatch(r"(?:live|直播|room)?\s*(\d{1,12})", text, re.I)
        if m:
            room, living = await asyncio.to_thread(self.client.live_room_info, int(m.group(1)))
            item = {"type": "live", "id": room.room_id, "title": room.title, "cover": room.cover}
        else:
            m = re.search(r"(BV[0-9A-Za-z]{10})", text)
            if m:
                bvid = m.group(1)
            else:
                m = re.search(r"av(\d+)", text, re.I)
                if not m:
                    raise ValueError("无法识别：请输入 BV 号、视频链接或直播间号")
                v = await asyncio.to_thread(self.client.api,
                                            "https://api.bilibili.com/x/web-interface/view",
                                            {"aid": m.group(1)})
                bvid = v["bvid"]
            v = await asyncio.to_thread(self.client.view, bvid)
            pages = v.get("pages") or []
            item = {"type": "bv", "id": bvid, "title": v.get("title"), "cover": v.get("pic"),
                    "duration": v.get("duration", 0), "author": (v.get("owner") or {}).get("name"),
                    "cid": pages[0]["cid"] if len(pages) == 1 else 0, "pages": len(pages)}
        added = [x for x in self.state.data["added"] if not (x["type"] == item["type"] and x["id"] == item["id"])]
        self.state.data["added"] = [item] + added[:99]
        self.state.save()
        return item

    def _expand_short(self, url):
        import urllib.request
        m = re.search(r"https?://b23\.tv/\S+", url)
        req = urllib.request.Request(m.group(0) if m else url, headers={"User-Agent": bili.UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.geturl()
