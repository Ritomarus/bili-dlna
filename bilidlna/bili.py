"""哔哩哔哩 Web 接口客户端（仅标准库）。

所有网络请求都是同步的 urllib 调用，由上层用 asyncio.to_thread 包装。
"""

import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.cookies import SimpleCookie

log = logging.getLogger("bili")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 视频编码 id
CODEC_AVC, CODEC_HEVC, CODEC_AV1 = 7, 12, 13
CODEC_NAMES = {CODEC_AVC: "avc", CODEC_HEVC: "hevc", CODEC_AV1: "av1"}

QN_NAMES = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P", 16: "360P",
}


class BiliError(Exception):
    def __init__(self, code, message):
        super().__init__("B站接口错误 %s: %s" % (code, message))
        self.code = code


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").replace("&quot;", '"').replace("&amp;", "&")


def _fix_url(u):
    if u and u.startswith("//"):
        return "https:" + u
    return (u or "").replace("http://", "https://", 1)


def parse_duration(v):
    """把 '12:34' / '1:02:03' / 秒数 统一为秒(int)。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    parts = [int(p) for p in str(v).split(":") if p.isdigit()]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


class Video:
    """列表里的一个视频条目（未必知道 cid）。"""

    __slots__ = ("bvid", "cid", "title", "cover", "duration", "author", "pages")

    def __init__(self, bvid, title, cover="", duration=0, author="", cid=0, pages=1):
        self.bvid = bvid
        self.cid = int(cid or 0)
        self.title = _strip_html(title)
        self.cover = _fix_url(cover)
        self.duration = parse_duration(duration)
        self.author = author or ""
        self.pages = int(pages or 1)


class LiveRoom:
    __slots__ = ("room_id", "title", "cover", "uname", "online")

    def __init__(self, room_id, title, cover="", uname="", online=0):
        self.room_id = int(room_id)
        self.title = _strip_html(title)
        self.cover = _fix_url(cover)
        self.uname = uname or ""
        self.online = online or 0


class BiliClient:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.cookie_file = os.path.join(data_dir, "cookies.json")
        self.cookies = {}
        self._lock = threading.Lock()
        self._wbi_key = None
        self._wbi_time = 0
        self.mid = 0
        self.uname = ""
        self.vip = False
        self._load_cookies()

    # ---------------- cookie ----------------
    def _load_cookies(self):
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                self.cookies = json.load(f)
        except (OSError, ValueError):
            self.cookies = {}
        # 旧版本自己生成的 b_nut 与 buvid3 不匹配，会导致搜索被风控
        self.cookies.pop("b_nut", None)

    def _save_cookies(self):
        tmp = self.cookie_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cookies, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.cookie_file)
        try:
            os.chmod(self.cookie_file, 0o600)
        except OSError:
            pass

    def cookie_header(self):
        return "; ".join("%s=%s" % kv for kv in self.cookies.items())

    @property
    def logged_in(self):
        return bool(self.cookies.get("SESSDATA"))

    def logout(self):
        with self._lock:
            for k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
                self.cookies.pop(k, None)
            self._save_cookies()
        self.mid, self.uname, self.vip = 0, "", False

    # ---------------- http ----------------
    def request(self, url, params=None, headers=None, raw=False, timeout=10):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        h = {
            "User-Agent": UA,
            "Referer": REFERER,
            "Origin": "https://www.bilibili.com",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.cookies:
            h["Cookie"] = self.cookie_header()
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise BiliError(e.code, "HTTP %s %s" % (e.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError) as e:  # 超时、DNS、连接失败
            raise BiliError(-1, "网络请求失败（%s）%s" % (getattr(e, "reason", e), url.split("?")[0]))
        with resp:
            body = resp.read()
            enc = resp.headers.get("Content-Encoding", "")
            if enc == "gzip":
                body = gzip.decompress(body)
            elif enc == "deflate":
                body = zlib.decompress(body, -zlib.MAX_WBITS)
            self._absorb_set_cookie(resp.headers.get_all("Set-Cookie") or [])
        if raw:
            return body
        return json.loads(body.decode("utf-8"))

    def _absorb_set_cookie(self, lines):
        changed = False
        for line in lines:
            c = SimpleCookie()
            try:
                c.load(line)
            except Exception:
                continue
            for k, m in c.items():
                if m.value and m.value != "deleted":
                    if self.cookies.get(k) != m.value:
                        self.cookies[k] = m.value
                        changed = True
        if changed:
            with self._lock:
                self._save_cookies()

    def api(self, url, params=None, wbi=False):
        if wbi:
            params = self.wbi_sign(params or {})
        data = self.request(url, params)
        code = data.get("code", 0)
        if code != 0:
            raise BiliError(code, data.get("message") or data.get("msg"))
        return data.get("data")

    # ---------------- 初始化 / 风控 ----------------
    def ensure_buvid(self):
        if self.cookies.get("buvid3"):
            return
        d = self.request("https://api.bilibili.com/x/frontend/finger/spi")["data"]
        self.cookies["buvid3"] = d["b_3"]
        self.cookies["buvid4"] = d["b_4"]
        with self._lock:
            self._save_cookies()

    def refresh_nav(self):
        """刷新登录状态和 wbi key。"""
        self.ensure_buvid()
        data = self.request("https://api.bilibili.com/x/web-interface/nav")
        d = data.get("data") or {}
        img = d.get("wbi_img") or {}
        key = lambda u: u.rsplit("/", 1)[-1].split(".")[0]
        if img.get("img_url"):
            orig = key(img["img_url"]) + key(img["sub_url"])
            self._wbi_key = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]
            self._wbi_time = time.time()
        if d.get("isLogin"):
            self.mid = d.get("mid", 0)
            self.uname = d.get("uname", "")
            self.vip = (d.get("vipStatus") == 1)
        else:
            self.mid, self.uname, self.vip = 0, "", False
        return d

    def wbi_sign(self, params):
        if not self._wbi_key or time.time() - self._wbi_time > 3600:
            self.refresh_nav()
        p = dict(params)
        p["wts"] = int(time.time())
        p = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(p.items())}
        query = urllib.parse.urlencode(p)
        p["w_rid"] = hashlib.md5((query + self._wbi_key).encode()).hexdigest()
        return p

    # ---------------- 扫码登录 ----------------
    def qr_generate(self):
        d = self.api("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        return d["url"], d["qrcode_key"]

    def qr_poll(self, key):
        """返回 (状态码, 文字)。0 = 成功；86101 未扫码；86090 已扫码未确认；86038 过期。"""
        d = self.api("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                     {"qrcode_key": key})
        code = d.get("code")
        if code == 0:
            # 有些情况下 Set-Cookie 没有带全，从回跳 url 里补齐
            q = urllib.parse.parse_qs(urllib.parse.urlparse(d.get("url", "")).query)
            for k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
                if k in q and not self.cookies.get(k):
                    self.cookies[k] = q[k][0]
            with self._lock:
                self._save_cookies()
            self.refresh_nav()
        return code, d.get("message", "")

    # ---------------- 视频列表 ----------------
    def recommend(self, ps=30, idx=1):
        d = self.api("https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd",
                     {"web_location": 1430650, "fresh_type": 4, "ps": ps,
                      "fresh_idx": idx, "fresh_idx_1h": idx, "brush": idx,
                      "feed_version": "V8"}, wbi=True)
        out = []
        for it in d.get("item") or []:
            if it.get("goto") != "av" or not it.get("bvid"):
                continue
            out.append(Video(it["bvid"], it.get("title"), it.get("pic"), it.get("duration"),
                             (it.get("owner") or {}).get("name"), it.get("cid")))
        return out

    def popular(self, pn=1, ps=40):
        d = self.api("https://api.bilibili.com/x/web-interface/popular", {"pn": pn, "ps": ps})
        return [self._archive(a) for a in d.get("list") or []]

    def ranking(self, rid=0):
        d = self.api("https://api.bilibili.com/x/web-interface/ranking/v2",
                     {"rid": rid, "type": "all"})
        return [self._archive(a) for a in d.get("list") or []]

    @staticmethod
    def _archive(a):
        return Video(a["bvid"], a.get("title"), a.get("pic"), a.get("duration"),
                     (a.get("owner") or {}).get("name"), a.get("cid"), a.get("videos", 1))

    def dynamic_videos(self, offset=""):
        d = self.api("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                     {"type": "video", "offset": offset, "timezone_offset": -480,
                      "features": "itemOpusStyle"})
        out = []
        for it in d.get("items") or []:
            mods = it.get("modules") or {}
            major = ((mods.get("module_dynamic") or {}).get("major") or {})
            arc = major.get("archive")
            if not arc or not arc.get("bvid"):
                continue
            out.append(Video(arc["bvid"], arc.get("title"), arc.get("cover"),
                             arc.get("duration_text"),
                             (mods.get("module_author") or {}).get("name")))
        return out

    def watch_later(self):
        d = self.api("https://api.bilibili.com/x/v2/history/toview")
        return [self._archive(a) for a in d.get("list") or []]

    def history(self):
        d = self.api("https://api.bilibili.com/x/web-interface/history/cursor",
                     {"ps": 30, "type": "archive"})
        out = []
        for it in d.get("list") or []:
            h = it.get("history") or {}
            if h.get("business") != "archive" or not h.get("bvid"):
                continue
            out.append(Video(h["bvid"], it.get("title"), it.get("cover"), it.get("duration"),
                             it.get("author_name"), h.get("cid"), it.get("videos", 1)))
        return out

    def fav_folders(self):
        if not self.mid:
            self.refresh_nav()
        d = self.api("https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                     {"up_mid": self.mid, "type": 2})
        return [(f["id"], f["title"], f.get("media_count", 0)) for f in (d or {}).get("list") or []]

    def fav_videos(self, media_id, pages=3):
        out = []
        for pn in range(1, pages + 1):
            d = self.api("https://api.bilibili.com/x/v3/fav/resource/list",
                         {"media_id": media_id, "pn": pn, "ps": 20, "platform": "web"})
            for m in d.get("medias") or []:
                if m.get("type") != 2 or not m.get("bvid") or m.get("attr", 0) & 1:
                    continue  # 跳过失效视频
                out.append(Video(m["bvid"], m.get("title"), m.get("cover"), m.get("duration"),
                                 (m.get("upper") or {}).get("name"), m.get("ugc", {}).get("first_cid", 0),
                                 m.get("page", 1)))
            if not d.get("has_more"):
                break
        return out

    def _search(self, kind, keyword, page=1):
        for attempt in range(3):
            params = self.wbi_sign({"search_type": kind, "keyword": keyword, "page": page})
            data = self.request("https://api.bilibili.com/x/web-interface/wbi/search/type", params,
                                headers={"Referer": "https://search.bilibili.com/all"})
            d = data.get("data") or {}
            if data.get("code") == 0 and ("v_voucher" not in d or d.get("result")):
                return d
            # 触发风控（-352 / v_voucher）：当前 buvid 被标记，换一个新的再试
            log.info("搜索触发风控 (code=%s)，更换 buvid 重试", data.get("code"))
            with self._lock:
                for k in ("buvid3", "buvid4", "b_nut"):
                    self.cookies.pop(k, None)
            self.ensure_buvid()
            time.sleep(0.8)
        raise BiliError(-352, "搜索太频繁触发了 B 站风控，请稍后再进入")

    def search(self, keyword, page=1):
        d = self._search("video", keyword, page)
        return [Video(r["bvid"], r.get("title"), r.get("pic"), r.get("duration"), r.get("author"))
                for r in d.get("result") or [] if r.get("bvid")]

    def view(self, bvid):
        return self.api("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})

    # ---------------- 播放地址 ----------------
    def playurl(self, bvid, cid):
        return self.api("https://api.bilibili.com/x/player/wbi/playurl",
                        {"bvid": bvid, "cid": cid, "qn": 127, "fnval": 4048, "fnver": 0,
                         "fourk": 1, "gaia_source": "", "from_client": "BROWSER"}, wbi=True)

    # ---------------- 直播 ----------------
    def live_following(self):
        d = self.api("https://api.live.bilibili.com/xlive/web-ucenter/v1/xfetter/GetWebList",
                     {"page": 1, "page_size": 50})
        out = []
        for r in (d or {}).get("rooms") or []:
            out.append(LiveRoom(r["room_id"], r.get("title"),
                                r.get("keyframe") or r.get("cover_from_user") or r.get("face"),
                                r.get("uname")))
        return out

    def live_hot(self, parent_area=0):
        d = self.api("https://api.live.bilibili.com/room/v1/Area/getListByAreaID",
                     {"areaId": parent_area, "sort": "online", "pageSize": 40, "page": 1})
        out = []
        for r in d or []:
            out.append(LiveRoom(r["roomid"], r.get("title"),
                                r.get("user_cover") or r.get("system_cover") or r.get("cover"),
                                r.get("uname"), r.get("online")))
        return out

    def live_search(self, keyword):
        d = self._search("live_room", keyword)
        return [LiveRoom(r["roomid"], r.get("title"), r.get("user_cover") or r.get("cover"),
                         _strip_html(r.get("uname")), r.get("online"))
                for r in d.get("result") or [] if r.get("live_status") == 1]

    def live_room_info(self, room_id):
        d = self.api("https://api.live.bilibili.com/room/v1/Room/get_info", {"room_id": room_id})
        return LiveRoom(d["room_id"], d.get("title"), d.get("user_cover") or d.get("keyframe"),
                        "", d.get("online")), d.get("live_status") == 1

    def live_playurl(self, room_id, qn=10000):
        """返回 [(format_name, codec_name, qn, [url,...]), ...]，按低延迟/兼容性优先排序。"""
        d = self.api("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
                     {"room_id": room_id, "protocol": "0,1", "format": "0,1,2",
                      "codec": "0,1", "qn": qn, "platform": "web", "ptype": 8,
                      "dolby": 5, "panorama": 1})
        if d.get("live_status") != 1:
            raise BiliError(-1, "主播未开播")
        streams = []
        pinfo = ((d.get("playurl_info") or {}).get("playurl") or {})
        for s in pinfo.get("stream") or []:
            for f in s.get("format") or []:
                for c in f.get("codec") or []:
                    urls = [ui["host"] + c["base_url"] + ui.get("extra", "")
                            for ui in c.get("url_info") or []]
                    # 优先使用官方 CDN，避开 PCDN（mcdn）节点
                    urls.sort(key=lambda u: ("mcdn" in u, "szbdyd" in u))
                    streams.append((f["format_name"], c["codec_name"], c.get("current_qn"), urls))
        return streams


def pick_dash(play, prefer_codecs=(CODEC_HEVC, CODEC_AVC, CODEC_AV1), max_qn=127):
    """从 playurl 返回中选出 (video, audio)。video/audio 为 dict，含 urls 列表。"""
    dash = play.get("dash")
    if not dash:
        return None, None
    videos = [v for v in dash.get("video") or [] if v.get("id", 0) <= max_qn]
    if not videos:
        videos = dash.get("video") or []
    best_qn = max(v["id"] for v in videos)
    cands = [v for v in videos if v["id"] == best_qn]
    rank = {c: i for i, c in enumerate(prefer_codecs)}
    cands.sort(key=lambda v: rank.get(v.get("codecid"), 99))
    video = cands[0]

    audios = list(dash.get("audio") or [])
    flac = (dash.get("flac") or {}).get("audio")
    dolby = (dash.get("dolby") or {}).get("audio") or []
    # 优先普通 AAC 最高码率（兼容性最好）
    audio = max(audios, key=lambda a: a.get("bandwidth", 0)) if audios else (flac or (dolby[0] if dolby else None))

    def urls(x):
        us = [x.get("baseUrl") or x.get("base_url")] + list(x.get("backupUrl") or x.get("backup_url") or [])
        us = [u for u in us if u]
        # upos 官方节点优先，PCDN（mcdn / IP:端口）放最后
        us.sort(key=lambda u: (("mcdn" in u) or bool(re.match(r"https?://\d+\.\d+\.\d+\.\d+", u)),
                               "upos-" not in u))
        return us

    video = dict(video, urls=urls(video))
    if audio:
        audio = dict(audio, urls=urls(audio))
    return video, audio
