"""入口：组装 HTTP 路由、DLNA、流处理。"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import subprocess
import time
import uuid as uuidlib
import zlib

from . import bili, dlna
from .httpd import HTTPServer, http_get
from .library import Library, State
from .stream import StreamManager
from .web import ADMIN_HTML

log = logging.getLogger("app")

IMG_HOSTS = ("hdslb.com", "bilibili.com", "biliimg.com")


def _is_lan(ip):
    """局域网私有地址，排除代理软件 TUN 模式常用的 198.18.0.0/15。"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback and a not in ipaddress.ip_network("198.18.0.0/15") \
        and not a.is_link_local


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))  # 不会真的发包，只用于选择出口网卡
        ip = s.getsockname()[0]
    except OSError:
        ip = ""
    finally:
        s.close()
    if _is_lan(ip):
        return ip
    # 开着 Clash / Surge 等 TUN 模式时，默认出口是虚拟网卡，改为枚举物理网卡
    cands = []
    for cmd in (["ifconfig"], ["ip", "-4", "addr"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        iface = ""
        for line in out.splitlines():
            m = re.match(r"^(?:\d+:\s*)?([A-Za-z0-9_.-]+):", line)
            if m:
                iface = m.group(1)
            m = re.search(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", line)
            if m and _is_lan(m.group(1)) and not re.match(r"(utun|tun|tap|docker|br-|veth|bridge|vmnet|llw|awdl)", iface):
                cands.append((not iface.startswith(("en", "eth", "wl")), m.group(1)))
        if cands:
            break
    if not cands:
        try:
            cands = [(0, x) for x in socket.gethostbyname_ex(socket.gethostname())[2] if _is_lan(x)]
        except OSError:
            pass
    return sorted(cands)[0][1] if cands else (ip or "127.0.0.1")


def make_icon(size=120):
    """生成一个 B 站粉色的圆角方块 PNG 作为设备图标。"""
    rows = []
    r = size // 5
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx = max(r - x, x - (size - 1 - r), 0)
            dy = max(r - y, y - (size - 1 - r), 0)
            inside = dx * dx + dy * dy <= r * r
            tv = (size * 0.25 < x < size * 0.75 and size * 0.35 < y < size * 0.72
                  and not (size * 0.32 < x < size * 0.68 and size * 0.42 < y < size * 0.65))
            row += bytes((255, 255, 255) if tv else ((251, 114, 153) if inside else (255, 255, 255)))
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


class App:
    def __init__(self, args):
        self.args = args
        self.ip = args.host or lan_ip()
        self.port = args.port
        os.makedirs(args.data, exist_ok=True)
        self.client = bili.BiliClient(args.data)
        self.state = State(os.path.join(args.data, "state.json"))
        self.uuid = self._load_uuid()
        self.library = Library(self.client, self.state, self.base)
        self.streams = StreamManager(self.client, self.state.settings, "http://127.0.0.1:%d" % self.port)
        self.cd = dlna.ContentDirectory(self.library, self.state)
        self.icon = make_icon()
        self.img_cache = {}

    def base(self):
        return "http://%s:%d" % (self.ip, self.port)

    def _load_uuid(self):
        p = os.path.join(self.args.data, "uuid")
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            u = str(uuidlib.uuid4())
            with open(p, "w") as f:
                f.write(u)
            return u

    async def json_reply(self, req, obj, status=200):
        await req.send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                       {"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- 路由
    async def handle(self, req):
        p = req.path
        m = re.match(r"^/vod/(BV[0-9A-Za-z]{10})/(\d+)(?:/.*)?$", p)
        if m and req.method in ("GET", "HEAD"):
            try:
                return await self.streams.handle_vod(req, m.group(1), int(m.group(2)))
            except bili.BiliError as e:
                log.warning("点播失败 %s: %s", m.group(1), e)
                if not req.headers_sent:
                    return await req.send(502, str(e))
                return
        m = re.match(r"^/live/(\d+)(?:/.*)?$", p)
        if m and req.method in ("GET", "HEAD"):
            try:
                return await self.streams.handle_live(req, int(m.group(1)))
            except bili.BiliError as e:
                log.warning("直播失败 %s: %s", m.group(1), e)
                if not req.headers_sent:
                    return await req.send(502, str(e))
                return
        m = re.match(r"^/up/([^/]+)/([va])$", p)
        if m:
            if req.client not in ("127.0.0.1", "::1"):
                return await req.send(403, "forbidden")
            return await self.streams.handle_relay(req, m.group(1), m.group(2))

        if p.startswith("/dlna/"):
            return await self.handle_dlna(req)
        if p == "/img":
            return await self.handle_img(req)
        if p == "/icon.png":
            return await req.send(200, self.icon, "image/png")
        if p == "/" or p == "/index.html":
            return await req.send(200, ADMIN_HTML, "text/html; charset=utf-8")
        if p.startswith("/api/"):
            try:
                return await self.handle_api(req)
            except (bili.BiliError, ValueError) as e:
                return await self.json_reply(req, {"error": str(e)}, 200)
        await req.send(404, "not found")

    async def handle_dlna(self, req):
        p = req.path
        xml = 'text/xml; charset="utf-8"'
        if p == "/dlna/description.xml":
            return await req.send(200, dlna.device_description(self.uuid, self.args.name), xml)
        if p == "/dlna/cd.xml":
            return await req.send(200, dlna.CD_SCPD, xml)
        if p == "/dlna/cm.xml":
            return await req.send(200, dlna.CM_SCPD, xml)
        if p in ("/dlna/cd/control", "/dlna/cm/control") and req.method == "POST":
            return await self.cd.handle(req, p.split("/")[2])
        if p.endswith("/event"):
            if req.method == "SUBSCRIBE":
                sid = req.header("sid") or "uuid:" + str(uuidlib.uuid4())
                return await req.start(200, {"SID": sid, "TIMEOUT": "Second-1800"}, 0)
            if req.method == "UNSUBSCRIBE":
                return await req.start(200, {}, 0)
        await req.send(404, "not found")

    async def handle_img(self, req):
        url = req.query.get("u", "")
        host = re.match(r"https?://([^/]+)", url)
        if not host or not host.group(1).endswith(IMG_HOSTS):
            return await req.send(403, "forbidden")
        hit = self.img_cache.get(url)
        if not hit:
            r = await http_get(url, {"User-Agent": bili.UA}, timeout=8)
            try:
                data = await r.body(limit=4 << 20)
            finally:
                r.close()
            if r.status != 200:
                return await req.send(502, "image error")
            hit = (r.headers.get("content-type", "image/jpeg"), data)
            if len(self.img_cache) > 500:
                self.img_cache.clear()
            self.img_cache[url] = hit
        await req.send(200, hit[1], hit[0], {"Cache-Control": "max-age=86400"})

    async def handle_api(self, req):
        p = req.path
        body = json.loads(req.body or b"{}") if req.method == "POST" else {}
        st = self.state
        if p == "/api/status":
            return await self.json_reply(req, {
                "name": self.args.name, "base": self.base(), "logged_in": self.client.logged_in,
                "uname": self.client.uname, "vip": self.client.vip, "settings": st.settings,
                "searches": st.data["searches"], "added": st.data["added"]})
        if p == "/api/qr":
            url, key = await asyncio.to_thread(self.client.qr_generate)
            return await self.json_reply(req, {"url": url, "key": key})
        if p == "/api/qr/poll":
            code, text = await asyncio.to_thread(self.client.qr_poll, req.query.get("key", ""))
            text = {0: "登录成功", 86101: "等待扫码…", 86090: "已扫码，请在手机上确认",
                    86038: "二维码已过期，请刷新页面重试"}.get(code, text)
            if code == 0:
                self.library.invalidate()
                st.save()  # 让播放器感知目录变化
            return await self.json_reply(req, {"code": code, "text": text})
        if p == "/api/logout":
            self.client.logout()
            self.library.invalidate()
            st.save()
            return await self.json_reply(req, {"ok": True})
        if p == "/api/add":
            item = await self.library.add(body.get("text", ""))
            return await self.json_reply(req, {"id": item["id"], "title": item.get("title")})
        if p == "/api/added/del":
            i = int(body.get("i", -1))
            if 0 <= i < len(st.data["added"]):
                st.data["added"].pop(i)
                st.save()
            return await self.json_reply(req, {"ok": True})
        if p == "/api/search":
            kw = (body.get("kw") or "").strip()
            if kw and kw not in st.data["searches"]:
                st.data["searches"].insert(0, kw)
                st.data["searches"] = st.data["searches"][:30]
                st.save()
            return await self.json_reply(req, {"ok": True})
        if p == "/api/search/del":
            i = int(body.get("i", -1))
            if 0 <= i < len(st.data["searches"]):
                st.data["searches"].pop(i)
                st.save()
            return await self.json_reply(req, {"ok": True})
        if p == "/api/settings":
            for k in ("max_qn", "codec", "live_codec", "vr_tags"):
                if k in body:
                    st.settings[k] = body[k]
            st.save()
            self.streams.sources.clear()
            self.library.invalidate()
            return await self.json_reply(req, {"ok": True})
        return await self.json_reply(req, {"error": "unknown api"}, 404)

    # ---------------------------------------------------------------- 启动
    async def run(self):
        server = HTTPServer(self.handle)
        await server.serve("0.0.0.0", self.port)
        try:
            await asyncio.to_thread(self.client.refresh_nav)
        except Exception as e:
            log.warning("初始化 B 站接口失败（稍后重试）: %s", e)
        if not self.args.no_ssdp:
            try:
                await dlna.start_ssdp(self.uuid, lambda: self.base() + "/dlna/description.xml", self.ip)
            except OSError as e:
                log.error("SSDP 启动失败（播放器将无法自动发现本服务）: %s", e)
        log.info("=" * 60)
        log.info("DLNA 服务器「%s」已启动", self.args.name)
        log.info("管理页面（扫码登录 / 添加视频）：%s", self.base())
        log.info("登录状态：%s", ("已登录 " + self.client.uname) if self.client.logged_in else "未登录（最高 480P）")
        log.info("=" * 60)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.streams.shutdown()


def main():
    ap = argparse.ArgumentParser(description="把哔哩哔哩变成 DLNA 媒体服务器，供 Quest 上的 4XVR / Skybox 播放")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--host", help="对外宣告的局域网 IP（默认自动检测）")
    ap.add_argument("--name", default="Bilibili", help="播放器里显示的服务器名称")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
                    help="保存登录信息和设置的目录")
    ap.add_argument("--no-ssdp", action="store_true", help="不广播 SSDP（调试用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(App(args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
