"""The TV album: one album, offered to the televisions at home over DLNA.

A household that wants its photographs on the television should not need an
app for it. Nearly every smart TV (and VLC, and Kodi) already has a media
player that looks for *DLNA media servers* on the home network and lets the
remote walk through what they offer. So Ninaivu can be one — a small,
read-only one that offers exactly one album the administrator chose, the
*TV album* (``Config.tv_album``), and nothing else.

DLNA has no sign-in at all: whatever is on the home network may ask. That
decides everything here.

* **One album, at the family ceiling, always.** What is offered is worked out
  afresh on every request, the way a share link's is (api/api_share.py): the
  album's items that are photographs or videos, family-visible or less,
  not flagged, not in the bin, not the clip half of a live photo, inside the
  family's date limit and in a library folder that is connected. A hidden
  photograph, or a sound recording, that happens to be in the album is never
  listed and never served, whoever set the album.
* **The home network only.** A peer that is not a private or link-local
  address — or is one of the remote-access ranges (Tailscale, WireGuard),
  which are "away from home" (server/remote.py) — is refused, by the media
  server and by the announcements alike.
* **Addresses nobody can guess.** Every address this server answers starts
  with a random secret kept in the index's meta table, so a web page in a
  browser at home that guesses ``http://192.168.1.10:8200/…`` (DNS rebinding,
  a scan of the LAN) gets a 404. The TV learns the secret from the
  announcement, which only the home network hears. The console can make a
  new one, which forgets every address handed out before.
* **Copies, not originals, for photographs.** A photograph is sent as the
  same upright, metadata-free JPEG the share page sends (media/stills.py), so
  nothing about where it was taken goes with it and a TV never has to decode a
  HEIC or a raw file. Videos are sent as they are, with byte ranges so the
  remote can skip.

The pieces are the standard ones: SSDP (multicast on 239.255.255.250:1900)
to announce and answer searches; a device description; ContentDirectory with
``Browse`` and its three small companions; and a minimal ConnectionManager,
which some renderers refuse to talk to a server without. Nothing here blocks
start-up or can stop the server: a port or a multicast group that cannot be
had is a warning in the log and a line on the console.
"""

from __future__ import annotations

import email.utils
import hmac
import ipaddress
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
import uuid as uuid_mod
import xml.etree.ElementTree as ElementTree
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape, quoteattr

from .config import VIDEO_TYPES

log = logging.getLogger(__name__)

# --- the protocol's names ----------------------------------------------------

MEDIA_SERVER = "urn:schemas-upnp-org:device:MediaServer:1"
CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
ROOT_DEVICE = "upnp:rootdevice"

SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
#: How long a TV may remember the announcement, in seconds. Announced again
#: at half of it, as the specification asks.
MAX_AGE = 1800

#: Where the secret and the device's own id are kept (the meta table).
SECRET_KEY = "tv_album_secret"
UUID_KEY = "tv_album_uuid"

#: The root container: the album itself.
ROOT_ID = "0"
#: The most items one Browse answers with, whatever was asked. A renderer that
#: asks for "all" (0) is told the total and asks for the rest.
MAX_PAGE = 500
#: The largest SOAP request read. A Browse is a few hundred bytes.
MAX_BODY = 64 * 1024
#: The thumbnail a renderer shows in its grid: DLNA's JPEG_TN is at most 160.
THUMB_EDGE = 160

#: DLNA's flags for media that can be fetched in ranges (DLNA.ORG_OP=01) and
#: is a picture (interactive transfer) or a video (streaming transfer).
_IMAGE_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=00f00000000000000000000000000000"
_VIDEO_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
PHOTO_PROTOCOL = f"http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;{_IMAGE_FLAGS}"
THUMB_PROTOCOL = f"http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_TN;{_IMAGE_FLAGS}"


def video_protocol(mime: str) -> str:
    return f"http-get:*:{mime}:{_VIDEO_FLAGS}"


#: What GetProtocolInfo says this server can send.
SOURCE_PROTOCOLS = ",".join([PHOTO_PROTOCOL, THUMB_PROTOCOL, *sorted(
    {video_protocol(mime) for mime in VIDEO_TYPES.values()})])


# --- who may ask --------------------------------------------------------------

#: The private blocks a home network uses, and the link-local ones a TV falls
#: back to with no router. Loopback is this computer.
HOME_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128"))


def _address(value: Any):
    """An address as ``ipaddress`` reads it, an IPv4 one inside IPv6 unwrapped."""
    from . import remote                                     # noqa: PLC0415
    return remote._address(value)                            # noqa: SLF001


def home_peer(address: Any, outside: tuple = ()) -> bool:
    """Whether *address* is a device on the home network.

    Private, link-local and loopback addresses are; anything public is not.
    Neither are the remote-access ranges in *outside* — Tailscale's own ULA
    block is a private range, and a WireGuard tunnel's subnet usually is too,
    but a device on one of them is away from home, and the TV album is for
    the house.
    """
    ip = _address(address)
    if ip is None:
        return False
    if any(ip in net for net in outside if net.version == ip.version):
        return False
    return any(ip in net for net in HOME_NETWORKS if net.version == ip.version)


def outside_networks(cfg: Any) -> tuple:
    """The remote-access ranges, Tailscale's always among them."""
    from . import remote                                     # noqa: PLC0415
    nets = list(remote._networks(remote.TAILSCALE_NETWORKS))  # noqa: SLF001
    try:
        nets.extend(remote.resolve(cfg).outside_networks)
    except Exception:                                        # noqa: BLE001
        log.debug("tv album: remote access could not be resolved", exc_info=True)
    return tuple(nets)


# --- the secret and the device's id -----------------------------------------

def secret(conn: sqlite3.Connection, fresh: bool = False) -> str:
    """This installation's secret, made on first use; *fresh* makes a new one."""
    from ..storage import db                                 # noqa: PLC0415
    value = None if fresh else db.get_meta(conn, SECRET_KEY)
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{16,}", value):
        value = secrets.token_urlsafe(18)
        db.set_meta(conn, SECRET_KEY, value)
        conn.commit()
    return value


def device_uuid(conn: sqlite3.Connection) -> str:
    """The id a TV knows this server by. Kept, so a TV that remembered the
    server finds the same one after a restart or a new secret."""
    from ..storage import db                                 # noqa: PLC0415
    value = db.get_meta(conn, UUID_KEY)
    try:
        return str(uuid_mod.UUID(str(value)))
    except ValueError:
        value = str(uuid_mod.uuid4())
        db.set_meta(conn, UUID_KEY, value)
        conn.commit()
        return value


# --- what the album offers ---------------------------------------------------

#: The columns a listing needs. Never the caption, the place or the tags: a
#: TV is told what it needs to lay a photograph out and play it.
_COLUMNS = ("id", "root", "rel_path", "filename", "ext", "kind", "size", "mtime",
            "captured_at", "date_key", "width", "height", "duration", "thumb", "rotation")


def _offered(conn: sqlite3.Connection, cfg: Any, album_id: int) -> tuple[str, list[Any]] | None:
    """The WHERE that is exactly what the TV album may show, or None for nothing.

    The same limits a share link of an album is held to (api_share's
    ``_share_assets``), with nobody's account behind it: the family ceiling,
    no flagged photographs, nothing in the bin, no administrator-only kinds
    (sound recordings), and the family's date limit. Photographs and videos
    only, and not the video half of a live photo, which the still stands for.
    """
    from . import auth, date_policy                          # noqa: PLC0415
    from ..storage import db                                 # noqa: PLC0415
    if not album_id:
        return None
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    policy = date_policy.for_role(conn, auth.ROLE_FAMILY)
    if policy[1] != "all" and not date_policy.allows(
            {"date_key": db.album_date(conn, album_id)}, policy):
        return None
    roots_sql, roots_params = db.roots_clause("a", libraries)
    where = [
        "a.id IN (SELECT asset_id FROM album_items WHERE album_id = ?)",
        roots_sql,
        "a.visibility <= ?",
        date_policy.sql("a", policy),
        "a.kind IN ('picture', 'video')",
        "a.trashed = 0", "a.nsfw = 0", "a.live_clip = 0",
    ]
    if guard := db.kind_guard("a", auth.VIS_FAMILY):
        where.append(guard)
    return " AND ".join(where), [int(album_id), *roots_params, auth.VIS_FAMILY]


def album_items(conn: sqlite3.Connection, cfg: Any, album_id: int, *,
                offset: int = 0, limit: int = MAX_PAGE) -> tuple[list[dict[str, Any]], int]:
    """One page of what the album offers, newest first, and how many in all."""
    offered = _offered(conn, cfg, album_id)
    if offered is None:
        return [], 0
    where, params = offered
    total = conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {', '.join('a.' + c for c in _COLUMNS)} FROM assets a WHERE {where} "
        "ORDER BY COALESCE(a.captured_at, a.mtime) DESC, a.id DESC LIMIT ? OFFSET ?",
        (*params, max(0, int(limit)), max(0, int(offset)))).fetchall()
    return [dict(row) for row in rows], int(total)


def album_item(conn: sqlite3.Connection, cfg: Any, album_id: int,
               asset_id: int) -> dict[str, Any] | None:
    """One item, if the album offers it now; every file request asks again."""
    offered = _offered(conn, cfg, album_id)
    if offered is None:
        return None
    where, params = offered
    row = conn.execute(
        f"SELECT {', '.join('a.' + c for c in _COLUMNS)} FROM assets a "
        f"WHERE a.id = ? AND {where}", (int(asset_id), *params)).fetchone()
    return dict(row) if row else None


def update_id(conn: sqlite3.Connection, cfg: Any, album_id: int) -> int:
    """A number that changes when what the album offers changes, so a TV
    knows to read it again (ContentDirectory's SystemUpdateID)."""
    offered = _offered(conn, cfg, album_id)
    if offered is None:
        return 0
    where, params = offered
    ids = ",".join(str(r[0]) for r in conn.execute(
        f"SELECT a.id FROM assets a WHERE {where} ORDER BY a.id", params))
    return zlib.crc32(f"{album_id}:{ids}".encode()) or 1


def album_name(conn: sqlite3.Connection, album_id: int) -> str | None:
    if not album_id:
        return None
    row = conn.execute("SELECT name FROM albums WHERE id=?", (int(album_id),)).fetchone()
    return row["name"] if row else None


def title_of(item: dict[str, Any]) -> str:
    """What the TV shows under a photograph: when it was taken, which is how a
    household finds one; the file's name when nobody knows."""
    when = item.get("captured_at")
    if when:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(when)))
        except (OverflowError, OSError, ValueError):
            pass
    return Path(str(item.get("filename") or "")).stem or f"Item {item.get('id')}"


def _iso_date(item: dict[str, Any]) -> str | None:
    when = item.get("captured_at") or item.get("mtime")
    if not when:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(when)))
    except (OverflowError, OSError, ValueError):
        return None


def _duration(seconds: Any) -> str | None:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:06.3f}"


def video_type(item: dict[str, Any]) -> str:
    ext = "." + str(item.get("ext") or "").lower().lstrip(".")
    return VIDEO_TYPES.get(ext, "video/mp4")


# --- XML: DIDL-Lite, the descriptions and SOAP --------------------------------

_DIDL_OPEN = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
              'xmlns:dc="http://purl.org/dc/elements/1.1/" '
              'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
              'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">')


def didl(entries: list[str]) -> str:
    return _DIDL_OPEN + "".join(entries) + "</DIDL-Lite>"


def didl_container(title: str, children: int) -> str:
    return (f'<container id="{ROOT_ID}" parentID="-1" restricted="1" searchable="0" '
            f'childCount="{int(children)}"><dc:title>{escape(title)}</dc:title>'
            "<upnp:class>object.container.album.photoAlbum</upnp:class></container>")


def didl_item(item: dict[str, Any], media_base: str) -> str:
    """One photograph or video. *media_base* is ``http://host:port/<secret>``."""
    asset_id = int(item["id"])
    parts = [f'<item id="a{asset_id}" parentID="{ROOT_ID}" restricted="1">',
             f"<dc:title>{escape(title_of(item))}</dc:title>"]
    if date := _iso_date(item):
        parts.append(f"<dc:date>{date}</dc:date>")
    thumb = f"{media_base}/thumb/{asset_id}.jpg" if item.get("thumb") else None
    if thumb:
        parts.append(f'<upnp:albumArtURI dlna:profileID="JPEG_TN">{escape(thumb)}'
                     "</upnp:albumArtURI>")
    if item.get("kind") == "video":
        parts.append("<upnp:class>object.item.videoItem</upnp:class>")
        attrs = [f"protocolInfo={quoteattr(video_protocol(video_type(item)))}"]
        if item.get("size"):
            attrs.append(f'size="{int(item["size"])}"')
        if duration := _duration(item.get("duration")):
            attrs.append(f'duration="{duration}"')
        if item.get("width") and item.get("height"):
            attrs.append(f'resolution="{int(item["width"])}x{int(item["height"])}"')
        ext = "." + re.sub(r"[^a-z0-9]", "", str(item.get("ext") or "").lower()) \
            if item.get("ext") else ".mp4"
        url = f"{media_base}/video/{asset_id}{ext}"
        parts.append(f"<res {' '.join(attrs)}>{escape(url)}</res>")
    else:
        parts.append("<upnp:class>object.item.imageItem.photo</upnp:class>")
        parts.append(f"<res protocolInfo={quoteattr(PHOTO_PROTOCOL)}>"
                     f"{escape(f'{media_base}/photo/{asset_id}.jpg')}</res>")
    if thumb:
        parts.append(f"<res protocolInfo={quoteattr(THUMB_PROTOCOL)}>{escape(thumb)}</res>")
    parts.append("</item>")
    return "".join(parts)


def device_description(*, udn: str, name: str, version: str, base: str) -> str:
    """The device description a TV reads first. *base* is ``/<secret>``."""
    def service(kind: str, short: str, key: str) -> str:
        return (f"<service><serviceType>{kind}</serviceType>"
                f"<serviceId>urn:upnp-org:serviceId:{short}</serviceId>"
                f"<SCPDURL>{base}/{key}.xml</SCPDURL>"
                f"<controlURL>{base}/ctl/{key}</controlURL>"
                f"<eventSubURL>{base}/evt/{key}</eventSubURL></service>")

    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0" '
            'xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            f"<device><deviceType>{MEDIA_SERVER}</deviceType>"
            "<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>"
            f"<friendlyName>{escape(name)}</friendlyName>"
            "<manufacturer>Ninaivu</manufacturer>"
            "<modelDescription>The TV album of a Ninaivu family library</modelDescription>"
            "<modelName>Ninaivu TV album</modelName>"
            f"<modelNumber>{escape(version)}</modelNumber>"
            f"<UDN>uuid:{escape(udn)}</UDN>"
            "<serviceList>"
            + service(CONTENT_DIRECTORY, "ContentDirectory", "cds")
            + service(CONNECTION_MANAGER, "ConnectionManager", "cms")
            + "</serviceList></device></root>")


def _scpd(actions: list[tuple[str, list[tuple[str, str, str]]]],
          variables: list[tuple[str, str, bool, tuple[str, ...]]]) -> str:
    """A service description: actions as (name, [(argument, in|out, variable)]),
    state variables as (name, type, evented, allowed values)."""
    out = ['<?xml version="1.0" encoding="utf-8"?>'
           '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
           "<specVersion><major>1</major><minor>0</minor></specVersion><actionList>"]
    for name, arguments in actions:
        out.append(f"<action><name>{name}</name><argumentList>")
        for argument, direction, variable in arguments:
            out.append(f"<argument><name>{argument}</name><direction>{direction}</direction>"
                       f"<relatedStateVariable>{variable}</relatedStateVariable></argument>")
        out.append("</argumentList></action>")
    out.append("</actionList><serviceStateTable>")
    for name, kind, evented, allowed in variables:
        out.append(f'<stateVariable sendEvents="{"yes" if evented else "no"}">'
                   f"<name>{name}</name><dataType>{kind}</dataType>")
        if allowed:
            out.append("<allowedValueList>" + "".join(
                f"<allowedValue>{v}</allowedValue>" for v in allowed) + "</allowedValueList>")
        out.append("</stateVariable>")
    out.append("</serviceStateTable></scpd>")
    return "".join(out)


CONTENT_DIRECTORY_SCPD = _scpd(
    [("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities")]),
     ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities")]),
     ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID")]),
     ("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID"),
                 ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"),
                 ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"),
                 ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"),
                 ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"),
                 ("UpdateID", "out", "A_ARG_TYPE_UpdateID")])],
    [("SearchCapabilities", "string", False, ()),
     ("SortCapabilities", "string", False, ()),
     ("SystemUpdateID", "ui4", True, ()),
     ("A_ARG_TYPE_ObjectID", "string", False, ()),
     ("A_ARG_TYPE_Result", "string", False, ()),
     ("A_ARG_TYPE_BrowseFlag", "string", False, ("BrowseMetadata", "BrowseDirectChildren")),
     ("A_ARG_TYPE_Filter", "string", False, ()),
     ("A_ARG_TYPE_SortCriteria", "string", False, ()),
     ("A_ARG_TYPE_Index", "ui4", False, ()),
     ("A_ARG_TYPE_Count", "ui4", False, ()),
     ("A_ARG_TYPE_UpdateID", "ui4", False, ())])

CONNECTION_MANAGER_SCPD = _scpd(
    [("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"),
                          ("Sink", "out", "SinkProtocolInfo")]),
     ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
     ("GetCurrentConnectionInfo", [
         ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"),
         ("RcsID", "out", "A_ARG_TYPE_RcsID"),
         ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
         ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
         ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
         ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
         ("Direction", "out", "A_ARG_TYPE_Direction"),
         ("Status", "out", "A_ARG_TYPE_ConnectionStatus")])],
    [("SourceProtocolInfo", "string", True, ()),
     ("SinkProtocolInfo", "string", True, ()),
     ("CurrentConnectionIDs", "string", True, ()),
     ("A_ARG_TYPE_ConnectionStatus", "string", False,
      ("OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown")),
     ("A_ARG_TYPE_ConnectionManager", "string", False, ()),
     ("A_ARG_TYPE_Direction", "string", False, ("Input", "Output")),
     ("A_ARG_TYPE_ProtocolInfo", "string", False, ()),
     ("A_ARG_TYPE_ConnectionID", "i4", False, ()),
     ("A_ARG_TYPE_AVTransportID", "i4", False, ()),
     ("A_ARG_TYPE_RcsID", "i4", False, ())])


def soap_response(service: str, action: str, values: list[tuple[str, Any]]) -> bytes:
    body = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in values)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action}Response xmlns:u="{service}">{body}</u:{action}Response>'
            "</s:Body></s:Envelope>").encode("utf-8")


def soap_fault(code: int, description: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
            "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
            '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f"<errorCode>{int(code)}</errorCode>"
            f"<errorDescription>{escape(description)}</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>").encode("utf-8")


class SoapError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


def parse_soap(body: bytes, soap_action: str | None) -> tuple[str, dict[str, str]]:
    """The action asked for and its arguments, from a SOAP request.

    The action's name comes from the body, which every client sends; the
    SOAPACTION header only stands in when the body has none.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise SoapError(402, "The request could not be read") from exc
    found = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Body":
            found = next(iter(element), None)
            break
    if found is None:
        raise SoapError(401, "No action was asked for")
    action = found.tag.rsplit("}", 1)[-1]
    if not action and soap_action:
        action = soap_action.strip('"').rsplit("#", 1)[-1]
    arguments = {child.tag.rsplit("}", 1)[-1]: (child.text or "") for child in found}
    return action, arguments


def _whole(arguments: dict[str, str], name: str) -> int:
    try:
        value = int(str(arguments.get(name, "0")).strip() or "0")
    except ValueError as exc:
        raise SoapError(402, f"{name} is not a number") from exc
    if value < 0:
        raise SoapError(402, f"{name} cannot be negative")
    return value


# --- SSDP ---------------------------------------------------------------------

def _notification_types(udn: str) -> list[tuple[str, str]]:
    """Every (NT or ST, USN) this server answers for."""
    return [(ROOT_DEVICE, f"uuid:{udn}::{ROOT_DEVICE}"),
            (f"uuid:{udn}", f"uuid:{udn}"),
            (MEDIA_SERVER, f"uuid:{udn}::{MEDIA_SERVER}"),
            (CONTENT_DIRECTORY, f"uuid:{udn}::{CONTENT_DIRECTORY}"),
            (CONNECTION_MANAGER, f"uuid:{udn}::{CONNECTION_MANAGER}")]


def server_header() -> str:
    from .. import __version__                              # noqa: PLC0415
    system = re.sub(r"[^A-Za-z0-9._-]", "", platform.system()) or "OS"
    release = re.sub(r"[^A-Za-z0-9._-]", "", platform.release()) or "1.0"
    return f"{system}/{release} UPnP/1.0 Ninaivu/{__version__}"


def search_replies(search_target: str, udn: str, location: str,
                   server: str | None = None) -> list[bytes]:
    """The answers to one M-SEARCH, one datagram each; none when it is not
    looking for anything this server is."""
    wanted = search_target.strip()
    targets = _notification_types(udn)
    if wanted != "ssdp:all":
        targets = [(nt, usn) for nt, usn in targets if nt == wanted]
    server = server or server_header()
    date = email.utils.formatdate(usegmt=True)
    return [("HTTP/1.1 200 OK\r\n"
             f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
             f"DATE: {date}\r\n"
             "EXT:\r\n"
             f"LOCATION: {location}\r\n"
             f"SERVER: {server}\r\n"
             f"ST: {nt}\r\n"
             f"USN: {usn}\r\n"
             "Content-Length: 0\r\n\r\n").encode("ascii") for nt, usn in targets]


def notify_messages(kind: str, udn: str, location: str,
                    server: str | None = None) -> list[bytes]:
    """The announcements: *kind* is ``ssdp:alive`` or ``ssdp:byebye``."""
    server = server or server_header()
    out = []
    for nt, usn in _notification_types(udn):
        lines = ["NOTIFY * HTTP/1.1", f"HOST: {SSDP_GROUP}:{SSDP_PORT}", f"NT: {nt}",
                 f"NTS: {kind}", f"USN: {usn}"]
        if kind == "ssdp:alive":
            lines += [f"CACHE-CONTROL: max-age={MAX_AGE}", f"LOCATION: {location}",
                      f"SERVER: {server}"]
        out.append(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    return out


def parse_search(data: bytes) -> str | None:
    """The search target of an M-SEARCH datagram, or None if it is not one."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:                                        # noqa: BLE001
        return None
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not lines[0].upper().startswith("M-SEARCH * HTTP/1."):
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().upper()] = value.strip()
    if headers.get("MAN", "").strip('"') != "ssdp:discover":
        return None
    return headers.get("ST") or None


def local_address_towards(peer: str) -> str | None:
    """This computer's address on the network *peer* is on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer, SSDP_PORT))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _ipv4_home_addresses() -> list[str]:
    from ..utils import tls                                  # noqa: PLC0415
    try:
        found = tls.lan_addresses()
    except Exception:                                        # noqa: BLE001
        found = []
    out = []
    for address in found:
        ip = _address(address)
        if ip is not None and ip.version == 4 and not ip.is_loopback and home_peer(ip):
            out.append(str(ip))
    return out


class Announcer:
    """SSDP: answers searches, and says hello and goodbye.

    Runs on one daemon thread. The socket is shared (SO_REUSEADDR) so that
    another media server on this computer, or Windows' own SSDP service, can
    listen beside it; if it cannot be had at all, the media server still runs
    and a TV can be pointed at it by address.
    """

    def __init__(self, udn: str, port: int, location_path: str,
                 allowed: Callable[[str], bool]) -> None:
        self.udn = udn
        self.port = int(port)
        self.location_path = location_path
        self.allowed = allowed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._addresses: list[str] = []
        self.problem: str | None = None

    def location(self, address: str) -> str:
        return f"http://{address}:{self.port}{self.location_path}"

    def start(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", SSDP_PORT))
        except OSError as exc:
            self.problem = f"could not listen for TVs on UDP port {SSDP_PORT}: {exc}"
            log.warning("tv album: %s; a TV can still be pointed at it by address",
                        self.problem)
            return False
        self._addresses = _ipv4_home_addresses()
        joined = 0
        for address in self._addresses or ["0.0.0.0"]:
            try:
                mreq = socket.inet_aton(SSDP_GROUP) + socket.inet_aton(address)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError:
                continue
        if not joined:
            sock.close()
            self.problem = "could not join the multicast group TVs search on"
            log.warning("tv album: %s; a TV can still be pointed at it by address",
                        self.problem)
            return False
        sock.settimeout(1.0)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tv-album-ssdp", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(5)
        self._thread = None
        self._announce("ssdp:byebye")
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _announce(self, kind: str) -> None:
        for address in self._addresses:
            try:
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            except OSError:
                continue
            try:
                sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                  socket.inet_aton(address))
                for message in notify_messages(kind, self.udn, self.location(address)):
                    sender.sendto(message, (SSDP_GROUP, SSDP_PORT))
            except OSError as exc:
                log.debug("tv album: could not announce on %s: %s", address, exc)
            finally:
                sender.close()

    def _run(self) -> None:
        # Twice at the start: UDP is allowed to lose one.
        self._announce("ssdp:alive")
        next_announce = time.monotonic() + 3
        repeated = False
        while not self._stop.is_set():
            if time.monotonic() >= next_announce:
                if repeated:
                    self._addresses = _ipv4_home_addresses() or self._addresses
                self._announce("ssdp:alive")
                next_announce = time.monotonic() + MAX_AGE / 2
                repeated = True
            try:
                data, peer = self._sock.recvfrom(4096)       # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(1)
                continue
            try:
                self._answer(data, peer)
            except Exception:                                # noqa: BLE001
                log.debug("tv album: could not answer a search", exc_info=True)

    def _answer(self, data: bytes, peer: tuple[str, int]) -> None:
        target = parse_search(data)
        if target is None or not self.allowed(peer[0]):
            return
        address = local_address_towards(peer[0])
        if address is None:
            return
        for reply in search_replies(target, self.udn, self.location(address)):
            self._sock.sendto(reply, peer)                   # type: ignore[union-attr]


# --- answering HTTP -------------------------------------------------------------

@dataclass
class Reply:
    """An answer: a body, or a file (a piece of one, for a range)."""
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    path: Path | None = None
    offset: int = 0
    length: int = 0


def _plain(status: int, text: str = "") -> Reply:
    body = (text or {403: "Only on the home network", 404: "Not found",
                     405: "Not allowed", 413: "Too large", 416: "Out of range",
                     500: "Something went wrong"}.get(status, "")).encode()
    return Reply(status, {"Content-Type": "text/plain; charset=utf-8"}, body)


def _xml(body: str | bytes, status: int = 200) -> Reply:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return Reply(status, {"Content-Type": 'text/xml; charset="utf-8"'}, data)


def byte_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """(first, last) of a ``Range: bytes=`` header; None for the whole file;
    False when the range cannot be met (416). Several ranges at once are
    answered with the whole file, which the standard allows."""
    if not header:
        return None
    match = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not match:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix == 0 or size == 0:
            return False
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)


def file_reply(path: Path, mime: str, range_header: str | None,
               features: str | None = None) -> Reply:
    try:
        size = path.stat().st_size
    except OSError:
        return _plain(404)
    headers = {"Content-Type": mime, "Accept-Ranges": "bytes",
               "Cache-Control": "private, max-age=3600",
               "transferMode.dlna.org": "Interactive" if mime.startswith("image/")
               else "Streaming"}
    if features:
        headers["contentFeatures.dlna.org"] = features
    wanted = byte_range(range_header, size)
    if wanted is False:
        headers["Content-Range"] = f"bytes */{size}"
        return Reply(416, headers)
    if wanted is None:
        headers["Content-Length"] = str(size)
        return Reply(200, headers, path=path, offset=0, length=size)
    start, end = wanted
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return Reply(206, headers, path=path, offset=start, length=end - start + 1)


def _source_file(item: dict[str, Any]) -> Path | None:
    """The original on disk, refusing anything outside its library folder."""
    try:
        root = Path(item["root"]).resolve()
        path = (root / item["rel_path"]).resolve()
    except (OSError, KeyError, TypeError, ValueError):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


class TvAlbum:
    """The TV album's media server: started, stopped and asked about from
    Services and the console."""

    def __init__(self, cfg: Any, connect: Callable[[], sqlite3.Connection]) -> None:
        self.cfg = cfg
        self.connect = connect
        self._lock = threading.RLock()
        self._http: _Server | None = None
        self._http_thread: threading.Thread | None = None
        self._announcer: Announcer | None = None
        self._outside: tuple = ()
        self.secret = ""
        self.udn = ""
        self.problem: str | None = None
        #: Set once Ninaivu is stopping: a start still on its way is not made.
        self._closed = False

    # -- the settings ---------------------------------------------------------
    @property
    def album_id(self) -> int:
        try:
            return max(0, int(getattr(self.cfg, "tv_album", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @property
    def port(self) -> int:
        try:
            return int(getattr(self.cfg, "tv_port", 8200) or 8200)
        except (TypeError, ValueError):
            return 8200

    def _why_not(self) -> str | None:
        """Why the TV album cannot run as things are set, or None."""
        if not self.album_id:
            return None
        if not getattr(self.cfg, "network_access", True) or str(
                getattr(self.cfg, "host", "0.0.0.0")) in ("127.0.0.1", "::1", "localhost"):
            return "Network access is off, so no TV could reach it"
        if album_name(self.connect(), self.album_id) is None:
            return "The chosen album is no longer there"
        return None

    # -- running ----------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._http is not None

    def start(self, *, announce: bool = True) -> bool:
        """Start serving if a TV album is chosen. Never raises: what went wrong
        is logged and kept in :attr:`problem` for the console."""
        with self._lock:
            if self._http is not None:
                return True
            if self._closed:
                return False
            self.problem = None
            if not self.album_id:
                return False
            try:
                self.problem = self._why_not()
                if self.problem:
                    log.warning("tv album: not started: %s", self.problem)
                    return False
                conn = self.connect()
                self.secret = secret(conn)
                self.udn = device_uuid(conn)
                self._outside = outside_networks(self.cfg)
                server = _Server(("0.0.0.0", self.port), _Handler)
                server.album = self
            except OSError as exc:
                self.problem = f"Port {self.port} could not be opened ({exc.strerror or exc})"
                log.warning("tv album: not started: %s", self.problem)
                return False
            except Exception as exc:                         # noqa: BLE001
                self.problem = f"It could not start ({exc})"
                log.warning("tv album: not started", exc_info=True)
                return False
            self._http = server
            self._http_thread = threading.Thread(target=server.serve_forever,
                                                 kwargs={"poll_interval": 0.5},
                                                 name="tv-album-http", daemon=True)
            self._http_thread.start()
            if announce:
                self._announcer = Announcer(self.udn, self.port, f"/{self.secret}/device.xml",
                                            self.allowed)
                if not self._announcer.start():
                    self.problem = self._announcer.problem
                    self._announcer = None
            log.info("tv album: serving album %s on port %s", self.album_id, self.port)
            return True

    def stop(self) -> None:
        with self._lock:
            announcer, self._announcer = self._announcer, None
            server, self._http = self._http, None
            thread, self._http_thread = self._http_thread, None
        if announcer is not None:
            announcer.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(5)

    def close(self) -> None:
        """Stop for good: Ninaivu is stopping."""
        with self._lock:
            self._closed = True
        self.stop()

    def restart(self) -> bool:
        """After a change of album, port or secret."""
        self.stop()
        return self.start()

    def new_secret(self) -> str:
        """Forget every address handed out so far. A TV finds the new one in
        the next announcement, which a restart sends at once."""
        with self._lock:
            self.secret = secret(self.connect(), fresh=True)
            was_running = self.running
        if was_running:
            self.restart()
        return self.secret

    def allowed(self, address: Any) -> bool:
        return home_peer(address, self._outside)

    def addresses(self) -> list[str]:
        """Where a TV, VLC or Kodi can be pointed by hand."""
        if not self.secret:
            return []
        return [f"http://{a}:{self.port}/{self.secret}/device.xml"
                for a in _ipv4_home_addresses()]

    def status(self) -> dict[str, Any]:
        conn = self.connect()
        album_id = self.album_id
        _, total = album_items(conn, self.cfg, album_id, limit=0) if album_id else ([], 0)
        return {
            "album_id": album_id or None,
            "album_name": album_name(conn, album_id),
            "items": total,
            "port": self.port,
            "running": self.running,
            "announcing": self._announcer is not None,
            "addresses": self.addresses() if self.running else [],
            "problem": self.problem if album_id else None,
        }

    # -- answering --------------------------------------------------------------
    def friendly_name(self) -> str:
        from .config import house_name                       # noqa: PLC0415
        return f"{house_name(self.cfg)} TV album"

    def respond(self, method: str, target: str, headers: Any, body: bytes,
                peer: str, host: str) -> Reply:
        """Answer one request. *host* is ``address:port`` as this server was
        reached, which the media addresses in a listing are built on."""
        if not self.allowed(peer):
            return _plain(403)
        album_id = self.album_id
        if not album_id or not self.secret:
            return _plain(404)
        parts = [unquote(p) for p in urlsplit(target).path.split("/") if p]
        if len(parts) < 2 or not hmac.compare_digest(parts[0].encode(), self.secret.encode()):
            return _plain(404)
        route = parts[1:]
        reading = method in ("GET", "HEAD")
        if route == ["device.xml"] and reading:
            from .. import __version__                       # noqa: PLC0415
            return _xml(device_description(udn=self.udn or "0", name=self.friendly_name(),
                                           version=__version__, base=f"/{self.secret}"))
        if route == ["cds.xml"] and reading:
            return _xml(CONTENT_DIRECTORY_SCPD)
        if route == ["cms.xml"] and reading:
            return _xml(CONNECTION_MANAGER_SCPD)
        if len(route) == 2 and route[0] == "ctl" and route[1] in ("cds", "cms"):
            if method != "POST":
                return _plain(405)
            return self._control(route[1], body, headers.get("SOAPACTION"),
                                 f"http://{host}/{self.secret}", album_id)
        if len(route) == 2 and route[0] == "evt" and route[1] in ("cds", "cms"):
            # Some renderers will not browse until they have subscribed. Nothing
            # is ever sent: the update id they read with each Browse is enough.
            if method == "SUBSCRIBE":
                return Reply(200, {"SID": headers.get("SID") or f"uuid:{uuid_mod.uuid4()}",
                                   "TIMEOUT": "Second-1800", "Content-Length": "0"})
            if method == "UNSUBSCRIBE":
                return Reply(200, {"Content-Length": "0"})
            return _plain(405)
        if len(route) == 2 and route[0] in ("photo", "thumb", "video"):
            if not reading:
                return _plain(405)
            match = re.fullmatch(r"(\d{1,18})(\.[a-z0-9]{1,5})?", route[1].lower())
            if not match:
                return _plain(404)
            return self._media(route[0], int(match.group(1)), headers, album_id)
        return _plain(404)

    def _control(self, service: str, body: bytes, soap_action: str | None,
                 media_base: str, album_id: int) -> Reply:
        kind = CONTENT_DIRECTORY if service == "cds" else CONNECTION_MANAGER
        try:
            action, arguments = parse_soap(body, soap_action)
            if service == "cms":
                values = self._connection_manager(action, arguments)
            else:
                values = self._content_directory(action, arguments, media_base, album_id)
        except SoapError as exc:
            return _xml(soap_fault(exc.code, exc.description), status=500)
        return _xml(soap_response(kind, action, values))

    @staticmethod
    def _connection_manager(action: str, arguments: dict[str, str]) -> list[tuple[str, Any]]:
        if action == "GetProtocolInfo":
            return [("Source", SOURCE_PROTOCOLS), ("Sink", "")]
        if action == "GetCurrentConnectionIDs":
            return [("ConnectionIDs", "0")]
        if action == "GetCurrentConnectionInfo":
            if arguments.get("ConnectionID", "0").strip() not in ("0", ""):
                raise SoapError(706, "No such connection")
            return [("RcsID", -1), ("AVTransportID", -1), ("ProtocolInfo", ""),
                    ("PeerConnectionManager", ""), ("PeerConnectionID", -1),
                    ("Direction", "Output"), ("Status", "OK")]
        raise SoapError(401, "Invalid action")

    def _content_directory(self, action: str, arguments: dict[str, str],
                           media_base: str, album_id: int) -> list[tuple[str, Any]]:
        conn = self.connect()
        if action == "GetSearchCapabilities":
            return [("SearchCaps", "")]
        if action == "GetSortCapabilities":
            return [("SortCaps", "")]
        if action == "GetSystemUpdateID":
            return [("Id", update_id(conn, self.cfg, album_id))]
        if action != "Browse":
            raise SoapError(401, "Invalid action")
        object_id = arguments.get("ObjectID", "").strip()
        flag = arguments.get("BrowseFlag", "").strip()
        start = _whole(arguments, "StartingIndex")
        count = _whole(arguments, "RequestedCount")
        count = MAX_PAGE if count == 0 else min(count, MAX_PAGE)
        version = update_id(conn, self.cfg, album_id)
        if flag not in ("BrowseMetadata", "BrowseDirectChildren"):
            raise SoapError(402, "BrowseFlag is BrowseMetadata or BrowseDirectChildren")
        if object_id == ROOT_ID:
            if flag == "BrowseMetadata":
                _, total = album_items(conn, self.cfg, album_id, limit=0)
                name = album_name(conn, album_id) or self.friendly_name()
                return [("Result", didl([didl_container(name, total)])),
                        ("NumberReturned", 1), ("TotalMatches", 1), ("UpdateID", version)]
            items, total = album_items(conn, self.cfg, album_id, offset=start, limit=count)
            return [("Result", didl([didl_item(i, media_base) for i in items])),
                    ("NumberReturned", len(items)), ("TotalMatches", total),
                    ("UpdateID", version)]
        match = re.fullmatch(r"a(\d{1,18})", object_id)
        item = album_item(conn, self.cfg, album_id, int(match.group(1))) if match else None
        if item is None:
            raise SoapError(701, "No such object")
        if flag == "BrowseMetadata":
            return [("Result", didl([didl_item(item, media_base)])),
                    ("NumberReturned", 1), ("TotalMatches", 1), ("UpdateID", version)]
        return [("Result", didl([])), ("NumberReturned", 0), ("TotalMatches", 0),
                ("UpdateID", version)]

    def _media(self, kind: str, asset_id: int, headers: Any, album_id: int) -> Reply:
        item = album_item(self.connect(), self.cfg, album_id, asset_id)
        if item is None:
            return _plain(404)
        range_header = headers.get("Range")
        if kind == "video":
            if item.get("kind") != "video":
                return _plain(404)
            path = _source_file(item)
            if path is None:
                return _plain(404)
            mime = video_type(item)
            return file_reply(path, mime, range_header,
                              video_protocol(mime).split(":", 3)[3])
        if kind == "thumb":
            path = self._thumbnail(item)
            return (file_reply(path, "image/jpeg", range_header,
                               THUMB_PROTOCOL.split(":", 3)[3])
                    if path else _plain(404))
        if item.get("kind") != "picture":
            return _plain(404)
        path = self._photo(item)
        return (file_reply(path, "image/jpeg", range_header, PHOTO_PROTOCOL.split(":", 3)[3])
                if path else _plain(404))

    def _photo(self, item: dict[str, Any]) -> Path | None:
        """The upright, metadata-free viewing copy the share page sends (the
        index's turn baked in), from the same store: made once, then read."""
        from ..media import media, stills, upright           # noqa: PLC0415
        source = _source_file(item)
        if source is None:
            return None
        store = stills.StillStore(self.cfg.state_dir,
                                  getattr(self.cfg, "rendition_cache_mb",
                                          stills.DEFAULT_CACHE_MB))
        turn = int(item.get("rotation") or 0) % 360
        variant = f"-t{turn}" if turn else ""

        @contextmanager
        def orient(path: Path) -> Iterator[Any]:
            with media._open_oriented(path) as image:       # noqa: SLF001
                yield upright.apply(image, turn) if turn else image

        return (store.ready(int(item["id"]), source, variant=variant)
                or store.build(int(item["id"]), source, orient=orient, variant=variant))

    def _thumbnail(self, item: dict[str, Any]) -> Path | None:
        """A JPEG of at most 160 pixels, from the gallery's smallest thumbnail
        (already upright). TVs read JPEG, and the gallery's may be WebP."""
        from PIL import Image                                # noqa: PLC0415

        from ..media import media                            # noqa: PLC0415
        if not item.get("thumb"):
            return None
        sizes = tuple(getattr(self.cfg, "thumb_sizes", (256,)) or (256,))
        source = Path(self.cfg.thumbs_dir) / media.thumb_file(
            item["thumb"], min(sizes), getattr(self.cfg, "thumb_format", "WEBP"))
        try:
            stamp = int(source.stat().st_mtime)
        except OSError:
            return None
        folder = thumbs_dir(self.cfg.state_dir)
        target = folder / f"{int(item['id'])}-{stamp}.jpg"
        if target.is_file():
            return target
        try:
            folder.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".{threading.get_ident()}.part")
            with Image.open(source) as image:
                frame = image.convert("RGB")
                frame.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
                frame.save(temporary, "JPEG", quality=85)
            temporary.replace(target)
        except Exception as exc:                             # noqa: BLE001
            log.debug("tv album: no thumbnail for %s: %s", item.get("id"), exc)
            return None
        for older in folder.glob(f"{int(item['id'])}-*.jpg"):
            if older != target:
                older.unlink(missing_ok=True)
        return target

    def forget_thumbnails(self) -> None:
        """When the album changes: the old album's small copies go with it."""
        shutil.rmtree(thumbs_dir(self.cfg.state_dir), ignore_errors=True)


def thumbs_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "tv-album" / "thumbs"


# --- the HTTP server itself ---------------------------------------------------

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second program take a port that is in
    # use; elsewhere it only allows a quick restart.
    allow_reuse_address = os.name != "nt"
    album: TvAlbum

    def verify_request(self, request, client_address) -> bool:  # noqa: ANN001
        # Refused before a byte is read: nothing outside the house is answered.
        return self.album.allowed(client_address[0])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Ninaivu-TV"
    #: A TV that stops reading is let go of, not waited on for ever.
    timeout = 60

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("tv album: %s %s", self.address_string(), format % args)

    def _handle(self, method: str) -> None:
        from ..storage import db                             # noqa: PLC0415
        album: TvAlbum = self.server.album                   # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            reply = _plain(413)
            self.close_connection = True
        else:
            body = self.rfile.read(length) if length > 0 else b""
            local = self.connection.getsockname()
            host = f"{local[0]}:{local[1]}"
            try:
                reply = album.respond(method, self.path, self.headers, body,
                                      self.client_address[0], host)
            except Exception:                                # noqa: BLE001
                log.exception("tv album: could not answer %s", self.path)
                reply = _plain(500)
            finally:
                db.end_open_transactions()
                db.close_all()
        self._send(method, reply)

    def _send(self, method: str, reply: Reply) -> None:
        self.send_response(reply.status)
        headers = dict(reply.headers)
        if reply.path is None:
            headers.setdefault("Content-Length", str(len(reply.body)))
        headers.setdefault("Server", server_header())
        headers.setdefault("Date", email.utils.formatdate(usegmt=True))
        headers["EXT"] = ""
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if method == "HEAD":
            return
        try:
            if reply.path is not None:
                with open(reply.path, "rb") as fh:
                    fh.seek(reply.offset)
                    left = reply.length
                    while left > 0:
                        chunk = fh.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            elif reply.body:
                self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def do_GET(self) -> None:                                # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:                               # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:                               # noqa: N802
        self._handle("POST")

    def do_SUBSCRIBE(self) -> None:                          # noqa: N802
        self._handle("SUBSCRIBE")

    def do_UNSUBSCRIBE(self) -> None:                        # noqa: N802
        self._handle("UNSUBSCRIBE")


__all__ = ["TvAlbum", "home_peer", "search_replies", "notify_messages", "parse_search",
           "album_items", "byte_range", "secret"]
