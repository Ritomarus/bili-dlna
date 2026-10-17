"""极简 asyncio HTTP/1.1 服务器 + 异步 HTTP GET 客户端（仅标准库）。

自己实现而不是用框架，是为了完全控制流式响应：DLNA 播放器对
Transfer-Encoding、Content-Length、Range 的处理很挑剔。
"""

import asyncio
import email.utils
import logging
import ssl
import time
import urllib.parse

log = logging.getLogger("httpd")

STATUS_TEXT = {
    200: "OK", 206: "Partial Content", 301: "Moved Permanently", 302: "Found",
    400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    412: "Precondition Failed", 416: "Range Not Satisfiable", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable",
}
SERVER_NAME = "bili-dlna/0.1 UPnP/1.0 DLNADOC/1.50"


class Request:
    def __init__(self, method, target, version, headers, body, reader, writer):
        self.method = method
        self.version = version
        self.headers = headers  # 小写键
        self.body = body
        self.reader = reader
        self.writer = writer
        u = urllib.parse.urlsplit(target)
        self.path = urllib.parse.unquote(u.path)
        self.raw_path = u.path
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.client = peer[0]
        self.headers_sent = False
        self.keep_alive = (version == "HTTP/1.1" and headers.get("connection", "").lower() != "close")

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default)

    async def start(self, status=200, headers=None, length=None):
        """发送响应头。length=None 表示长度未知（以关闭连接结束，不用 chunked）。"""
        h = {"Server": SERVER_NAME, "Date": email.utils.formatdate(usegmt=True)}
        if headers:
            h.update(headers)
        if length is not None:
            h["Content-Length"] = str(length)
        else:
            self.keep_alive = False
        h["Connection"] = "keep-alive" if self.keep_alive else "close"
        lines = ["HTTP/1.1 %d %s" % (status, STATUS_TEXT.get(status, "OK"))]
        lines += ["%s: %s" % kv for kv in h.items()]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        self.headers_sent = True
        await self.writer.drain()

    async def write(self, data):
        if self.method == "HEAD" or not data:
            return
        # 注意：对端断开后 asyncio 的 write()/drain() 不会报错，只会默默丢数据。
        # 必须主动检查，否则播放器探测完就断开的连接会在后台把整个视频下载完。
        if self.writer.transport.is_closing():
            raise ConnectionResetError("客户端已断开")
        self.writer.write(data)
        await self.writer.drain()
        if self.writer.transport.is_closing():
            raise ConnectionResetError("客户端已断开")

    async def send(self, status=200, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        h = {"Content-Type": ctype}
        if headers:
            h.update(headers)
        await self.start(status, h, len(body))
        await self.write(body)


class HTTPServer:
    def __init__(self, handler):
        self.handler = handler
        self.server = None

    async def serve(self, host, port):
        self.server = await asyncio.start_server(self._conn, host, port, reuse_address=True,
                                                 limit=1 << 16)
        return self.server

    async def _conn(self, reader, writer):
        try:
            while True:
                req = await self._read_request(reader, writer)
                if req is None:
                    break
                t0 = time.time()
                try:
                    await self.handler(req)
                except (ConnectionError, asyncio.IncompleteReadError, BrokenPipeError):
                    break
                except Exception:
                    log.exception("处理请求出错 %s %s", req.method, req.path)
                    if not req.headers_sent:
                        try:
                            await req.send(500, "internal error")
                        except Exception:
                            pass
                    break
                finally:
                    log.debug("%s %s %s %.0fms", req.client, req.method, req.path,
                              (time.time() - t0) * 1000)
                if not req.headers_sent or not req.keep_alive:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _read_request(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=120)
        except asyncio.TimeoutError:
            return None
        if not line:
            return None
        parts = line.decode("latin-1").strip().split()
        if len(parts) != 3:
            return None
        method, target, version = parts
        headers = {}
        while True:
            hl = await reader.readline()
            if hl in (b"\r\n", b"\n", b""):
                break
            k, _, v = hl.decode("utf-8", "replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        body = b""
        n = int(headers.get("content-length", "0") or 0)
        if n:
            body = await reader.readexactly(n)
        return Request(method.upper(), target, version, headers, body, reader, writer)


# ---------------------------------------------------------------------------
# 异步 HTTP GET 客户端（用于从 B 站 CDN 拉流，天然有 TCP 背压）
# ---------------------------------------------------------------------------

_ssl_ctx = ssl.create_default_context()


class UpstreamResponse:
    def __init__(self, status, headers, reader, writer):
        self.status = status
        self.headers = headers
        self.reader = reader
        self.writer = writer
        self.remaining = int(headers["content-length"]) if "content-length" in headers else None
        self.chunked = "chunked" in headers.get("transfer-encoding", "").lower()

    async def read(self, n=1 << 18):
        if self.chunked:
            return await self._read_chunk()
        if self.remaining is not None:
            if self.remaining <= 0:
                return b""
            n = min(n, self.remaining)
        data = await self.reader.read(n)
        if self.remaining is not None:
            self.remaining -= len(data)
            if not data and self.remaining > 0:
                raise ConnectionError("上游连接提前关闭，还差 %d 字节" % self.remaining)
        return data

    async def _read_chunk(self):
        line = await self.reader.readline()
        size = int(line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            await self.reader.readline()
            return b""
        data = await self.reader.readexactly(size)
        await self.reader.readline()
        return data

    async def body(self, limit=1 << 22):
        out = bytearray()
        while len(out) < limit:
            b = await self.read()
            if not b:
                break
            out += b
        return bytes(out)

    def close(self):
        # 直接 abort：Python 3.9 的 SSL 优雅关闭会等待对端 close_notify，
        # 经过代理 TUN 时可能永远等不到，导致连接一直挂着。
        try:
            self.writer.transport.abort()
        except Exception:
            pass


async def http_get(url, headers=None, timeout=10, max_redirects=5):
    for _ in range(max_redirects + 1):
        u = urllib.parse.urlsplit(url)
        https = u.scheme == "https"
        port = u.port or (443 if https else 80)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, port, ssl=_ssl_ctx if https else None,
                                    server_hostname=u.hostname if https else None,
                                    limit=1 << 20),
            timeout)
        try:
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            h = {"Host": u.netloc, "Connection": "close", "Accept": "*/*"}
            if headers:
                h.update(headers)
            req = "GET %s HTTP/1.1\r\n%s\r\n\r\n" % (path, "\r\n".join("%s: %s" % kv for kv in h.items()))
            writer.write(req.encode("utf-8"))
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout)
            if not status_line:
                raise ConnectionError("上游无响应")
            status = int(status_line.split()[1])
            rh = {}
            while True:
                hl = await asyncio.wait_for(reader.readline(), timeout)
                if hl in (b"\r\n", b"\n", b""):
                    break
                k, _, v = hl.decode("latin-1").partition(":")
                rh[k.strip().lower()] = v.strip()
        except BaseException:
            writer.transport.abort()
            raise
        if status in (301, 302, 303, 307, 308) and "location" in rh:
            writer.transport.abort()
            url = urllib.parse.urljoin(url, rh["location"])
            continue
        return UpstreamResponse(status, rh, reader, writer)
    raise ConnectionError("重定向次数过多")
