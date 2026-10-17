"""点播 / 直播流处理。

点播链路：
  播放器 --GET /vod/..ts (Range)--> 本服务 --ffmpeg -c copy--> /up/<key>/v|a (本机中继) --> B站 CDN

  * /up/ 中继：给 CDN 请求补上 Referer，支持 Range；断流时从断点续传，
    并在主地址/备用地址之间切换；地址过期(403)时自动重新获取。
  * /vod/：把 DASH 的音视频两条流用 ffmpeg 合成 MPEG-TS（不转码）。
    - 读取 m4s 的 sidx 分段索引，得到每个关键帧的时间和字节位置：
      跳转时音视频都从同一个关键帧开始（不会出现"有画面没声音"），
      并按真实码率分布把"播放器请求的字节位置"换算成时间。
    - 每一路播放是一个"会话"：一个 ffmpeg 持续输出到内存窗口，
      播放器断开重连、按字节续传时直接从窗口里返回完全相同的字节，
      不会因为重新 seek 造成时间戳倒退或跳跃。
    - 会话只比播放器超前 READAHEAD 字节，没有读者 IDLE 秒后自动结束，
      所以播放器探测完就断开的连接不会在后台继续下载。
    对外宣称一个估算的文件大小；保留原始时间戳(-copyts)，播放器按 PCR 二分查找也能收敛。
    也支持 DLNA TimeSeekRange 按时间跳转。

直播链路：
  播放器 --GET /live/<room>.ts--> 本服务 --ffmpeg -c copy--> B站直播 CDN (FLV)
  上游断开时重新获取直播地址并重启 ffmpeg，保持与播放器的连接不断。
"""

import asyncio
import bisect
import itertools
import logging
import re
import struct
import time

from . import bili
from .httpd import http_get

log = logging.getLogger("stream")

TS_PACKET = 188
NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184
SOURCE_TTL = 80 * 60  # B站播放地址约 120 分钟过期，提前刷新

READAHEAD = 12 << 20   # 会话最多比最快的读者超前这么多字节
BACKLOG = 48 << 20     # 读者位置之前保留这么多字节，供断开重连续传
REUSE_SLACK = 8 << 20   # 请求位置在已产出数据之后这个范围内，等待而不是新开会话
IDLE_TIMEOUT = 20      # 会话没有读者多少秒后结束

DLNA_FLAGS_VOD = "DLNA.ORG_OP=11;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
DLNA_FLAGS_LIVE = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=8D700000000000000000000000000000"


def cdn_headers():
    return {"User-Agent": bili.UA, "Referer": bili.REFERER}


def ffmpeg_headers_arg():
    return "".join("%s: %s\r\n" % kv for kv in cdn_headers().items())


def fmt_npt(t):
    return "%d:%02d:%06.3f" % (t // 3600, t % 3600 // 60, t % 60)


def parse_npt(s):
    s = s.strip()
    if ":" in s:
        sec = 0.0
        for p in s.split(":"):
            sec = sec * 60 + float(p)
        return sec
    return float(s or 0)


def parse_sidx(data, index_end):
    """解析 sidx box，返回 [(开始时间秒, 开始字节), ...] 以及末尾 (总时长, 总字节)。"""
    size, typ = struct.unpack(">I4s", data[:8])
    if typ != b"sidx":
        raise ValueError("不是 sidx")
    ver = data[8]
    off = 12
    _ref_id, timescale = struct.unpack(">II", data[off:off + 8])
    off += 8
    if ver == 0:
        ept, first = struct.unpack(">II", data[off:off + 8])
        off += 8
    else:
        ept, first = struct.unpack(">QQ", data[off:off + 16])
        off += 16
    _, count = struct.unpack(">HH", data[off:off + 4])
    off += 4
    t, pos = ept / timescale, index_end + 1 + first
    points = []
    for i in range(count):
        ref, dur, _sap = struct.unpack(">III", data[off + i * 12:off + i * 12 + 12])
        points.append((t, pos))
        t += dur / timescale
        pos += ref & 0x7FFFFFFF
    points.append((t, pos))
    return points


class Track:
    def __init__(self, info):
        self.info = info
        self.urls = list(info["urls"])
        self.size = 0
        self.good = 0  # 当前优先使用的 url 下标
        sb = info.get("SegmentBase") or info.get("segment_base") or {}
        self.index_range = sb.get("indexRange") or sb.get("index_range") or ""
        self.points = []  # sidx 分段：[(时间, 字节)]，每段都从关键帧开始

    def ordered_urls(self):
        return self.urls[self.good:] + self.urls[:self.good]

    def bytes_at(self, t, duration):
        """播放到 t 秒时，该轨道累计的字节数（分段内线性插值）。"""
        if len(self.points) < 2:
            return self.size * min(max(t / duration, 0), 1) if duration else 0
        times = [p[0] for p in self.points]
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            return self.points[0][1]
        if i >= len(self.points) - 1:
            return self.points[-1][1]
        (t0, b0), (t1, b1) = self.points[i], self.points[i + 1]
        return b0 + (b1 - b0) * (t - t0) / (t1 - t0 if t1 > t0 else 1)

    def keyframe_before(self, t):
        """不晚于 t 的最近关键帧（分段起点）时间；没有索引时返回 t 本身。"""
        if len(self.points) < 2:
            return t
        times = [p[0] for p in self.points[:-1]]
        i = bisect.bisect_right(times, t + 1e-6) - 1
        return times[max(i, 0)]


class VodSource:
    def __init__(self, bvid, cid):
        self.bvid, self.cid = bvid, cid
        self.key = "%s_%s" % (bvid, cid)
        self.video = None
        self.audio = None
        self.duration = 0.0
        self.fetched = 0
        self.qn = 0
        self.codecid = 0
        self.desc = ""

    @property
    def expired(self):
        return time.time() - self.fetched > SOURCE_TTL

    def ts_bytes_at(self, t):
        # 实测 TS 封装开销：视频 ≈2.3% + 每秒约 6KB（每帧 PES 头/填充、PAT/PMT），
        # AAC 音频帧很小，开销 ≈21.5%。再留 1.5% 余量，末尾多出的部分补空包。
        d = self.duration
        v = self.video.bytes_at(t, d)
        a = self.audio.bytes_at(t, d) if self.audio else 0
        return (v * 1.023 + a * 1.215 + min(t, d) * 6000) * 1.015

    def estimated_ts_size(self):
        return int(self.ts_bytes_at(self.duration)) // TS_PACKET * TS_PACKET

    def time_at(self, byte):
        """ts_bytes_at 的反函数（二分查找）。"""
        lo, hi = 0.0, self.duration
        for _ in range(40):
            mid = (lo + hi) / 2
            if self.ts_bytes_at(mid) <= byte:
                lo = mid
            else:
                hi = mid
        return lo


class Session:
    """一路 ffmpeg 输出 + 内存滑动窗口。字节位置都用"对外宣称的文件"里的绝对位置。"""

    _ids = itertools.count(1)

    def __init__(self, manager, src, start_byte, start_time):
        self.id = next(self._ids)
        self.manager = manager
        self.src = src
        self.start_byte = start_byte
        self.start_time = start_time
        self.base = start_byte       # buf[0] 对应的绝对位置
        self.buf = bytearray()
        self.end = start_byte        # 已产出数据的末尾（绝对位置）
        self.done = False
        self.readers = {}            # reader_id -> 当前绝对位置
        self.last_pos = start_byte   # 最近一个读者读到的位置（没有读者时用它限流）
        self.stops = []              # 读者断开时所在的位置，用于识别"续传"
        self.last_active = time.time()
        self.cond = asyncio.Condition()
        self.proc = None
        self.task = asyncio.ensure_future(self._run())

    def __repr__(self):
        return "会话#%d[%s @%.1fs]" % (self.id, self.src.key, self.start_time)

    def covers(self, pos):
        if pos < self.base:
            return False
        return pos <= self.end + REUSE_SLACK and not (self.done and pos > self.end)

    def _frontier(self):
        return max(self.readers.values()) if self.readers else self.last_pos

    async def _run(self):
        src, mgr = self.src, self.manager
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        # 分段起点就是关键帧。实际帧时间戳比索引时间略晚（B 帧显示延迟），
        # 所以视频往后偏 0.3 秒（分段最短 0.5 秒），保证落在这个关键帧而不是上一个；
        # 音频从关键帧时间开始，这样一开始就有声音。
        seek = self.start_time > 0.05
        vss = ["-ss", "%.3f" % (self.start_time + 0.3)] if seek else []
        ass = ["-ss", "%.3f" % self.start_time] if seek else []
        cmd += vss + ["-i", "%s/up/%s/v" % (mgr.local_base, src.key)]
        if src.audio:
            cmd += ass + ["-i", "%s/up/%s/a" % (mgr.local_base, src.key)]
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c", "copy", "-copyts", "-f", "mpegts", "-mpegts_copyts", "1", "pipe:1"]
        err = b""
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            err_task = asyncio.ensure_future(self.proc.stderr.read())
            while True:
                async with self.cond:
                    while self.end - self._frontier() >= READAHEAD:
                        await self.cond.wait()
                chunk = await self.proc.stdout.read(1 << 18)
                if not chunk:
                    break
                async with self.cond:
                    self.buf += chunk
                    self.end += len(chunk)
                    self._trim()
                    self.cond.notify_all()
            await self.proc.wait()
            err = await err_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("%s 出错", self)
        finally:
            self._kill()
            if self.proc:
                await self.proc.wait()  # 在事件循环关闭前回收 ffmpeg，避免退出时报错
            async with self.cond:
                self.done = True
                self.cond.notify_all()
            msg = err.decode("utf-8", "replace").strip()
            if msg and self.proc and self.proc.returncode not in (0, -9):
                log.warning("%s ffmpeg: %s", self, msg[-400:])

    def _trim(self):
        low = (min(self.readers.values()) if self.readers else self.last_pos) - BACKLOG
        cut = low - self.base
        if cut > (8 << 20):  # 攒够 8MB 再裁，避免频繁移动内存
            del self.buf[:cut]
            self.base += cut

    def _kill(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass

    def close(self):
        self._kill()
        self.task.cancel()

    async def serve(self, req, pos, length):
        """从绝对位置 pos 开始给客户端写 length 字节，不足部分补空包。"""
        rid = object()
        sent = 0
        async with self.cond:
            self.readers[rid] = pos
            self.cond.notify_all()
        try:
            while sent < length:
                async with self.cond:
                    while pos >= self.end and not self.done:
                        await self.cond.wait()
                    if pos < self.end:
                        lo = pos - self.base
                        hi = min(self.end, pos + (1 << 18), pos + (length - sent)) - self.base
                        data = bytes(self.buf[lo:hi])
                    else:
                        data = None
                if data is None:
                    break
                await req.write(data)
                pos += len(data)
                sent += len(data)
                async with self.cond:
                    self.readers[rid] = pos
                    self.last_pos = max(self.last_pos, pos)
                    self.cond.notify_all()
            if sent < length:
                # 估算的文件大小比实际输出大，末尾用 TS 空包补齐，保证 Content-Length 一致
                block = NULL_PACKET * 512
                while sent < length:
                    piece = block[:length - sent]
                    await req.write(piece)
                    sent += len(piece)
        finally:
            async with self.cond:
                self.readers.pop(rid, None)
                self.stops = (self.stops + [pos])[-8:]
                self.last_active = time.time()
                self.cond.notify_all()


class StreamManager:
    def __init__(self, client, settings, local_base):
        self.client = client
        self.settings = settings  # dict，可在运行时修改
        self.local_base = local_base  # 例如 http://127.0.0.1:8200
        self.sources = {}
        self.sessions = {}  # source key -> [Session]
        self._locks = {}
        self.cid_cache = {}
        self._reaper = None

    # ------------------------------------------------------------------ 源
    async def resolve_cid(self, bvid, page=1):
        key = (bvid, page)
        if key not in self.cid_cache:
            v = await asyncio.to_thread(self.client.view, bvid)
            pages = v.get("pages") or []
            idx = min(max(page, 1), len(pages)) - 1
            self.cid_cache[key] = pages[idx]["cid"]
        return self.cid_cache[key]

    async def source(self, bvid, cid, force=False):
        key = "%s_%s" % (bvid, cid)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            src = self.sources.get(key)
            if src and not force and not src.expired:
                return src
            src = src or VodSource(bvid, cid)
            await self._fill(src)
            self.sources[key] = src
            return src

    async def _fill(self, src):
        play = await asyncio.to_thread(self.client.playurl, src.bvid, src.cid)
        prefer = {
            "hevc": (bili.CODEC_HEVC, bili.CODEC_AVC, bili.CODEC_AV1),
            "avc": (bili.CODEC_AVC, bili.CODEC_HEVC, bili.CODEC_AV1),
            "av1": (bili.CODEC_AV1, bili.CODEC_HEVC, bili.CODEC_AVC),
        }.get(self.settings.get("codec", "hevc"))
        max_qn = int(self.settings.get("max_qn", 120))
        if src.qn:  # 刷新过期地址时保持同一清晰度/编码，保证文件大小不变
            max_qn = src.qn
            prefer = (src.codecid,) + tuple(c for c in prefer if c != src.codecid)
        v, a = bili.pick_dash(play, prefer, max_qn)
        if not v:
            raise bili.BiliError(-1, "该视频没有 DASH 流（可能是付费或地区限制）")
        old_v, old_a = src.video, src.audio
        src.video = Track(v)
        src.audio = Track(a) if a else None
        src.qn, src.codecid = v["id"], v.get("codecid", 0)
        dash = play.get("dash") or {}
        src.duration = float(dash.get("duration") or play.get("timelength", 0) / 1000)
        src.fetched = time.time()
        for new, old in ((src.video, old_v), (src.audio, old_a)):
            if new and old and old.size:  # 同一个文件，沿用已知的大小和索引
                new.size, new.points = old.size, old.points
        tracks = [t for t in (src.video, src.audio) if t and not t.size]
        await asyncio.gather(*(self._load_index(t) for t in tracks))
        src.desc = "%s %s %dx%d" % (bili.QN_NAMES.get(src.qn, src.qn),
                                    bili.CODEC_NAMES.get(src.codecid, "?"),
                                    v.get("width", 0), v.get("height", 0))
        log.info("播放源 %s: %s, 时长 %.0fs, 视频 %.1fMB(%d段), 音频 %.1fMB", src.key, src.desc,
                 src.duration, src.video.size / 1e6, max(len(src.video.points) - 1, 0),
                 (src.audio.size if src.audio else 0) / 1e6)

    async def _load_index(self, track):
        """一次 Range 请求同时拿到文件总大小和 sidx 分段索引。"""
        rng = track.index_range or "0-0"
        lo, hi = (int(x) for x in rng.split("-"))
        last = None
        for i, url in enumerate(track.urls):
            try:
                r = await http_get(url, dict(cdn_headers(), Range="bytes=%s" % rng), timeout=6)
                try:
                    cr = r.headers.get("content-range", "")
                    body = await asyncio.wait_for(r.body(limit=hi - lo + 1), 6) if r.status == 206 else b""
                finally:
                    r.close()
                if r.status == 206 and "/" in cr:
                    track.size = int(cr.rsplit("/", 1)[1])
                    track.good = i
                    if track.index_range:
                        try:
                            track.points = parse_sidx(body, hi)
                        except Exception as e:
                            log.warning("解析分段索引失败（退化为按比例跳转）: %s", e)
                    return
                last = "HTTP %s" % r.status
            except Exception as e:  # noqa
                last = e
        raise ConnectionError("所有 CDN 地址都不可用: %s" % last)

    # ------------------------------------------------------------ /up/ 中继
    async def handle_relay(self, req, key, kind):
        src = self.sources.get(key)
        if not src:
            return await req.send(404, "unknown source")
        track = src.video if kind == "v" else src.audio
        if not track:
            return await req.send(404, "no track")
        size = track.size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", req.header("range"))
        if m:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):  # 末尾 N 字节
                start = max(0, size - int(m.group(2)))
        if start >= size:
            return await req.send(416, "", headers={"Content-Range": "bytes */%d" % size})
        headers = {"Content-Type": "video/mp4", "Accept-Ranges": "bytes"}
        if m:
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        await req.start(206 if m else 200, headers, end - start + 1)
        if req.method == "HEAD":
            return

        pos = start
        failures = 0
        refreshed = False
        while pos <= end:
            urls = track.ordered_urls()
            url = urls[failures % len(urls)]
            try:
                r = await http_get(url, dict(cdn_headers(), Range="bytes=%d-%d" % (pos, end)),
                                   timeout=8)
            except Exception as e:
                r, err = None, e
            if r is None or r.status not in (200, 206):
                status = r.status if r else err
                if r:
                    r.close()
                failures += 1
                log.warning("CDN 请求失败 (%s) %s", status, url.split("?")[0])
                if failures >= len(urls) and not refreshed:
                    refreshed = True  # 地址过期：重新获取播放地址
                    src = await self.source(src.bvid, src.cid, force=True)
                    track = src.video if kind == "v" else src.audio
                    failures = 0
                elif failures >= 2 * len(urls) + 2:
                    raise ConnectionError("CDN 持续失败")
                await asyncio.sleep(0.2)
                continue
            if r.status == 200 and pos > 0:
                r.close()  # 服务器不支持 Range，换下一个
                failures += 1
                continue
            track.good = track.urls.index(url) if url in track.urls else track.good
            try:
                while pos <= end:
                    try:
                        chunk = await asyncio.wait_for(r.read(1 << 18), timeout=15)
                    except (ConnectionError, OSError, asyncio.TimeoutError,
                            asyncio.IncompleteReadError) as e:
                        log.warning("CDN 断流 @%d (%s)，续传", pos, e)
                        failures += 1
                        break
                    if not chunk:
                        if pos <= end:
                            failures += 1
                        break
                    chunk = chunk[:end - pos + 1]
                    await req.write(chunk)  # 客户端断开会在这里抛出，直接结束
                    pos += len(chunk)
            finally:
                r.close()

    # ------------------------------------------------------------ /vod/ 点播
    def _session_for(self, src, pos, seek_time=None):
        """找一个能覆盖 pos 的现有会话；没有就新开一个。"""
        lst = [s for s in self.sessions.get(src.key, []) if not (s.done and not s.readers and pos > s.end)]
        self.sessions[src.key] = lst
        cands = [s for s in lst if s.covers(pos)]
        if cands:
            # 优先：刚好有读者在 pos 处断开的会话（播放器续传）；
            # 其次：数据已经覆盖 pos 的会话里最近活跃的；最后：还差一点就产出到 pos 的会话。
            def rank(s):
                return (pos in s.stops, s.base <= pos <= s.end, bool(s.readers), s.last_active)
            return max(cands, key=rank), False
        t = src.time_at(pos) if seek_time is None else seek_time
        k = src.video.keyframe_before(t)
        s = Session(self, src, pos, k)
        lst.append(s)
        self._ensure_reaper()
        return s, True

    async def shutdown(self):
        """退出前结束所有会话和 ffmpeg。"""
        sessions = [s for lst in self.sessions.values() for s in lst]
        for s in sessions:
            s.close()
        await asyncio.gather(*(s.task for s in sessions), return_exceptions=True)

    def _ensure_reaper(self):
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.ensure_future(self._reap())

    async def _reap(self):
        while any(self.sessions.values()):
            await asyncio.sleep(5)
            now = time.time()
            for key, lst in list(self.sessions.items()):
                keep = []
                for s in lst:
                    if not s.readers and now - s.last_active > IDLE_TIMEOUT:
                        s.close()
                        log.debug("结束 %s", s)
                    else:
                        keep.append(s)
                if keep:
                    self.sessions[key] = keep
                else:
                    del self.sessions[key]

    async def handle_vod(self, req, bvid, cid):
        if not cid:
            cid = await self.resolve_cid(bvid, int(req.query.get("p", 1)))
        src = await self.source(bvid, int(cid))
        total = src.estimated_ts_size()
        dur = src.duration
        headers = {
            "Content-Type": "video/mp2t",
            "Accept-Ranges": "bytes",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": DLNA_FLAGS_VOD,
        }
        start_byte, seek_time = 0, None
        status = 200
        tsr = req.header("timeseekrange.dlna.org")
        m = re.match(r"bytes=(\d*)-(\d*)", req.header("range"))
        if tsr:
            mt = re.match(r"npt=([\d:.]+)-", tsr.strip())
            want = min(parse_npt(mt.group(1)), max(dur - 1, 0)) if mt else 0.0
            seek_time = src.video.keyframe_before(want)
            start_byte = int(src.ts_bytes_at(seek_time)) // TS_PACKET * TS_PACKET
            headers["TimeSeekRange.dlna.org"] = "npt=%s-%s/%s bytes=%d-%d/%d" % (
                fmt_npt(seek_time), fmt_npt(dur), fmt_npt(dur), start_byte, total - 1, total)
        elif m and (m.group(1) or m.group(2)):
            if m.group(1):
                start_byte = int(m.group(1))
            else:
                start_byte = max(0, total - int(m.group(2)))
            if start_byte >= total:
                return await req.send(416, "", headers={"Content-Range": "bytes */%d" % total})
            headers["Content-Range"] = "bytes %d-%d/%d" % (start_byte, total - 1, total)
            status = 206
        length = total - start_byte
        await req.start(status, headers, length)
        if req.method == "HEAD":
            return
        session, new = self._session_for(src, start_byte, seek_time)
        log.info("点播 %s [%s] byte %d -> %s%s", src.key, src.desc, start_byte, session,
                 "（新）" if new else "（复用缓存）")
        await session.serve(req, start_byte, length)

    async def _pipe_ffmpeg(self, req, cmd):
        """运行 ffmpeg 并把 stdout 写给客户端（直播用），返回已写字节数。"""
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        err_task = asyncio.ensure_future(proc.stderr.read())
        sent = 0
        try:
            while True:
                chunk = await proc.stdout.read(1 << 17)
                if not chunk:
                    break
                await req.write(chunk)
                sent += len(chunk)
            return sent
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            err = (await err_task).decode("utf-8", "replace").strip()
            if err and proc.returncode not in (0, -9):
                log.warning("ffmpeg: %s", err[-500:])

    # ------------------------------------------------------------ /live/ 直播
    def _pick_live(self, streams):
        codec = self.settings.get("live_codec", "avc")
        fmt_rank = {"flv": 0, "ts": 1, "fmp4": 2}
        best = max(s[2] or 0 for s in streams)
        cands = [s for s in streams if (s[2] or 0) == best and s[3]]
        cands.sort(key=lambda s: (fmt_rank.get(s[0], 9), s[1] != codec))
        return cands[0]

    async def handle_live(self, req, room_id):
        headers = {
            "Content-Type": "video/mp2t",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": DLNA_FLAGS_LIVE,
            "Cache-Control": "no-cache",
        }
        streams = await asyncio.to_thread(self.client.live_playurl, room_id)
        await req.start(200, headers, None)
        if req.method == "HEAD":
            return
        fails = 0
        while fails < 6:
            fmt, codec, qn, urls = self._pick_live(streams)
            url = urls[fails % len(urls)]
            log.info("直播 %s: %s/%s qn=%s %s", room_id, fmt, codec, qn, url.split("?")[0])
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-fflags", "nobuffer", "-probesize", "1000000", "-analyzeduration", "1500000",
                   "-rw_timeout", "8000000",
                   "-headers", ffmpeg_headers_arg(), "-i", url,
                   "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", "pipe:1"]
            t0 = time.time()
            sent = await self._pipe_ffmpeg(req, cmd)
            # 正常播放了一段时间后断开，不算失败
            fails = 0 if (time.time() - t0 > 20 and sent > 1 << 20) else fails + 1
            log.info("直播 %s 上游断开（已发送 %.1fMB），重连 #%d", room_id, sent / 1e6, fails)
            await asyncio.sleep(0.3 if fails < 2 else 1.5)
            try:
                streams = await asyncio.to_thread(self.client.live_playurl, room_id)
            except bili.BiliError as e:
                log.info("直播 %s 结束: %s", room_id, e)
                return
