"""UPnP / DLNA 媒体服务器：SSDP 发现 + 设备描述 + ContentDirectory/ConnectionManager。"""

import asyncio
import email.utils
import logging
import random
import socket
import struct
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from .library import fmt_dur
from .stream import DLNA_FLAGS_LIVE, DLNA_FLAGS_VOD

log = logging.getLogger("dlna")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
CD_TYPE = "urn:schemas-upnp-org:service:ContentDirectory:1"
CM_TYPE = "urn:schemas-upnp-org:service:ConnectionManager:1"
SERVER = "macOS/1.0 UPnP/1.0 bili-dlna/0.1"


# ---------------------------------------------------------------- XML 文档
def device_description(uuid, name):
    return """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>%s</deviceType>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <friendlyName>%s</friendlyName>
    <manufacturer>bili-dlna</manufacturer>
    <manufacturerURL>https://www.bilibili.com/</manufacturerURL>
    <modelDescription>Bilibili DLNA bridge</modelDescription>
    <modelName>bili-dlna</modelName>
    <modelNumber>0.1</modelNumber>
    <serialNumber>1</serialNumber>
    <UDN>uuid:%s</UDN>
    <iconList>
      <icon><mimetype>image/png</mimetype><width>120</width><height>120</height><depth>24</depth><url>/icon.png</url></icon>
    </iconList>
    <serviceList>
      <service>
        <serviceType>%s</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/dlna/cd.xml</SCPDURL>
        <controlURL>/dlna/cd/control</controlURL>
        <eventSubURL>/dlna/cd/event</eventSubURL>
      </service>
      <service>
        <serviceType>%s</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/dlna/cm.xml</SCPDURL>
        <controlURL>/dlna/cm/control</controlURL>
        <eventSubURL>/dlna/cm/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>""" % (DEVICE_TYPE, escape(name), uuid, CD_TYPE, CM_TYPE)


def _scpd(actions, variables):
    acts = []
    for name, args in actions:
        a = "".join("<argument><name>%s</name><direction>%s</direction>"
                    "<relatedStateVariable>%s</relatedStateVariable></argument>" % x for x in args)
        acts.append("<action><name>%s</name><argumentList>%s</argumentList></action>" % (name, a))
    vs = []
    for name, typ, events, allowed in variables:
        al = ("<allowedValueList>%s</allowedValueList>" % "".join(
            "<allowedValue>%s</allowedValue>" % v for v in allowed)) if allowed else ""
        vs.append('<stateVariable sendEvents="%s"><name>%s</name><dataType>%s</dataType>%s</stateVariable>'
                  % ("yes" if events else "no", name, typ, al))
    return ('<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            "<actionList>%s</actionList><serviceStateTable>%s</serviceStateTable></scpd>"
            % ("".join(acts), "".join(vs)))


CD_SCPD = _scpd(
    [("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID"),
                 ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"),
                 ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"),
                 ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"),
                 ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"),
                 ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
     ("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities")]),
     ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities")]),
     ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID")])],
    [("A_ARG_TYPE_ObjectID", "string", False, None),
     ("A_ARG_TYPE_BrowseFlag", "string", False, ["BrowseMetadata", "BrowseDirectChildren"]),
     ("A_ARG_TYPE_Filter", "string", False, None),
     ("A_ARG_TYPE_Index", "ui4", False, None),
     ("A_ARG_TYPE_Count", "ui4", False, None),
     ("A_ARG_TYPE_SortCriteria", "string", False, None),
     ("A_ARG_TYPE_Result", "string", False, None),
     ("A_ARG_TYPE_UpdateID", "ui4", False, None),
     ("SearchCapabilities", "string", False, None),
     ("SortCapabilities", "string", False, None),
     ("SystemUpdateID", "ui4", True, None)])

CM_SCPD = _scpd(
    [("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")]),
     ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
     ("GetCurrentConnectionInfo", [("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"),
                                   ("RcsID", "out", "A_ARG_TYPE_RcsID"),
                                   ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
                                   ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
                                   ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
                                   ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
                                   ("Direction", "out", "A_ARG_TYPE_Direction"),
                                   ("Status", "out", "A_ARG_TYPE_ConnectionStatus")])],
    [("SourceProtocolInfo", "string", True, None), ("SinkProtocolInfo", "string", True, None),
     ("CurrentConnectionIDs", "string", True, None),
     ("A_ARG_TYPE_ConnectionID", "i4", False, None), ("A_ARG_TYPE_RcsID", "i4", False, None),
     ("A_ARG_TYPE_AVTransportID", "i4", False, None), ("A_ARG_TYPE_ProtocolInfo", "string", False, None),
     ("A_ARG_TYPE_ConnectionManager", "string", False, None),
     ("A_ARG_TYPE_Direction", "string", False, ["Input", "Output"]),
     ("A_ARG_TYPE_ConnectionStatus", "string", False, ["OK", "Unknown"])])

SOURCE_PROTOCOLS = ",".join([
    "http-get:*:video/mp2t:" + DLNA_FLAGS_VOD,
    "http-get:*:video/mp2t:*",
    "http-get:*:image/jpeg:*",
])


def soap_envelope(action, service, fields):
    body = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in fields)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            '<u:%sResponse xmlns:u="%s">%s</u:%sResponse></s:Body></s:Envelope>'
            % (action, service, body, action))


def soap_fault(code, desc):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
            '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
            '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>%d</errorCode>'
            '<errorDescription>%s</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>'
            % (code, escape(desc)))


def didl(nodes):
    out = ['<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
           'xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
           'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">']
    for n in nodes:
        art = ('<upnp:albumArtURI dlna:profileID="JPEG_TN">%s</upnp:albumArtURI>' % escape(n.cover)
               if n.cover else "")
        if n.container:
            out.append('<container id=%s parentID=%s restricted="1" searchable="0" childCount="%d">'
                       '<dc:title>%s</dc:title><upnp:class>object.container.storageFolder</upnp:class>%s'
                       '</container>' % (quoteattr(n.id), quoteattr(n.parent), n.child_count,
                                         escape(n.title), art))
        else:
            flags = DLNA_FLAGS_LIVE if n.live else DLNA_FLAGS_VOD
            attrs = ' protocolInfo="http-get:*:%s:%s"' % (n.mime, flags)
            if n.duration:
                attrs += ' duration="%s"' % fmt_dur(n.duration)
            creator = "<dc:creator>%s</dc:creator><upnp:artist>%s</upnp:artist>" % (
                escape(n.author), escape(n.author)) if n.author else ""
            out.append('<item id=%s parentID=%s restricted="1"><dc:title>%s</dc:title>%s'
                       '<upnp:class>object.item.videoItem</upnp:class>%s<res%s>%s</res></item>'
                       % (quoteattr(n.id), quoteattr(n.parent), escape(n.title), creator, art,
                          attrs, escape(n.url)))
    out.append("</DIDL-Lite>")
    return "".join(out)


# ---------------------------------------------------------------- SOAP 处理
class ContentDirectory:
    def __init__(self, library, state):
        self.library = library
        self.state = state

    @staticmethod
    def parse_soap(body):
        root = ET.fromstring(body)
        for el in root.iter():
            if el.tag.endswith("Body"):
                act = list(el)[0]
                name = act.tag.split("}")[-1]
                args = {c.tag.split("}")[-1]: (c.text or "") for c in act}
                return name, args
        raise ValueError("无效 SOAP")

    async def handle(self, req, service):
        try:
            action, args = self.parse_soap(req.body)
        except Exception:
            return await req.send(400, soap_fault(402, "Invalid Args"), "text/xml; charset=\"utf-8\"")
        if service == "cm":
            if action == "GetProtocolInfo":
                xml = soap_envelope(action, CM_TYPE, [("Source", SOURCE_PROTOCOLS), ("Sink", "")])
            elif action == "GetCurrentConnectionIDs":
                xml = soap_envelope(action, CM_TYPE, [("ConnectionIDs", "0")])
            elif action == "GetCurrentConnectionInfo":
                xml = soap_envelope(action, CM_TYPE, [
                    ("RcsID", -1), ("AVTransportID", -1), ("ProtocolInfo", ""),
                    ("PeerConnectionManager", ""), ("PeerConnectionID", -1),
                    ("Direction", "Output"), ("Status", "OK")])
            else:
                return await req.send(500, soap_fault(401, "Invalid Action"), 'text/xml; charset="utf-8"')
            return await req.send(200, xml, 'text/xml; charset="utf-8"')

        if action == "Browse":
            xml = await self.browse(args)
        elif action == "GetSearchCapabilities":
            xml = soap_envelope(action, CD_TYPE, [("SearchCaps", "")])
        elif action == "GetSortCapabilities":
            xml = soap_envelope(action, CD_TYPE, [("SortCaps", "")])
        elif action == "GetSystemUpdateID":
            xml = soap_envelope(action, CD_TYPE, [("Id", self.state.update_id)])
        elif action == "X_GetFeatureList":  # 三星等设备会问
            xml = soap_envelope(action, CD_TYPE, [("FeatureList", "")])
        else:
            return await req.send(500, soap_fault(401, "Invalid Action"), 'text/xml; charset="utf-8"')
        await req.send(200, xml, 'text/xml; charset="utf-8"')

    async def browse(self, args):
        oid = args.get("ObjectID", "0") or "0"
        flag = args.get("BrowseFlag", "BrowseDirectChildren")
        start = int(args.get("StartingIndex") or 0)
        count = int(args.get("RequestedCount") or 0)
        if flag == "BrowseMetadata":
            nodes = [await self.library.lookup(oid)]
            total = 1
        else:
            try:
                nodes = await self.library.children(oid)
            except Exception as e:
                log.warning("浏览 %s 失败: %s", oid, e)
                from .library import Node
                nodes = [Node(oid + "/err", "⚠️ 加载失败：%s" % e, container=True)]
            total = len(nodes)
            nodes = nodes[start:start + count] if count else nodes[start:]
        log.info("Browse %s %s [%d+%d] -> %d/%d", oid, flag, start, count, len(nodes), total)
        return soap_envelope("Browse", CD_TYPE, [
            ("Result", didl(nodes)), ("NumberReturned", len(nodes)),
            ("TotalMatches", total), ("UpdateID", self.state.update_id)])


# ---------------------------------------------------------------- SSDP
class SSDP(asyncio.DatagramProtocol):
    def __init__(self, uuid, location_fn, iface_ip):
        self.uuid = uuid
        self.location_fn = location_fn
        self.iface_ip = iface_ip
        self.transport = None
        self.targets = ["upnp:rootdevice", "uuid:" + uuid, DEVICE_TYPE, CD_TYPE, CM_TYPE]

    def usn(self, nt):
        return "uuid:%s" % self.uuid if nt.startswith("uuid:") else "uuid:%s::%s" % (self.uuid, nt)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            return
        if not text.startswith("M-SEARCH"):
            return
        headers = {}
        for line in text.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
        st = headers.get("ST", "")
        if st == "ssdp:all":
            sts = self.targets
        elif st in self.targets:
            sts = [st]
        else:
            return
        try:
            mx = min(max(int(headers.get("MX", "1")), 1), 3)
        except ValueError:
            mx = 1
        log.debug("M-SEARCH %s from %s", st, addr)
        asyncio.get_event_loop().call_later(random.uniform(0, mx * 0.3), self._reply, sts, addr)

    def _reply(self, sts, addr):
        for st in sts:
            msg = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nDATE: %s\r\nEXT:\r\n"
                   "LOCATION: %s\r\nSERVER: %s\r\nST: %s\r\nUSN: %s\r\nContent-Length: 0\r\n\r\n"
                   % (email.utils.formatdate(usegmt=True), self.location_fn(), SERVER, st, self.usn(st)))
            try:
                self.transport.sendto(msg.encode(), addr)
            except Exception as e:
                log.debug("ssdp reply err %s", e)

    def notify(self, nts="ssdp:alive"):
        if not self.transport:
            return
        for nt in self.targets:
            msg = "NOTIFY * HTTP/1.1\r\nHOST: %s:%d\r\nNT: %s\r\nNTS: %s\r\nUSN: %s\r\n" % (
                SSDP_ADDR, SSDP_PORT, nt, nts, self.usn(nt))
            if nts == "ssdp:alive":
                msg += "CACHE-CONTROL: max-age=1800\r\nLOCATION: %s\r\nSERVER: %s\r\n" % (
                    self.location_fn(), SERVER)
            msg += "\r\n"
            try:
                self.transport.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
            except Exception as e:
                log.debug("ssdp notify err %s", e)


async def start_ssdp(uuid, location_fn, iface_ip):
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", SSDP_PORT))
    mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(iface_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 4))
    sock.setblocking(False)
    proto = SSDP(uuid, location_fn, iface_ip)
    await loop.create_datagram_endpoint(lambda: proto, sock=sock)

    async def announcer():
        for _ in range(3):
            proto.notify()
            await asyncio.sleep(0.5)
        while True:
            await asyncio.sleep(30)
            proto.notify()

    asyncio.ensure_future(announcer())
    return proto
