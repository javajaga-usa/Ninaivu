"""The TV album: one album, on the home network, to a TV with no sign-in.

What is covered: a Browse answers with the album's photographs and videos and
pages through them; what is hidden, flagged, in the bin or a sound recording is
never listed or served, whatever the album holds; a wrong secret is a 404 and
a peer outside the house is refused; the SSDP answers have the shape TVs
read; a video is served in ranges; and with no album chosen nothing is served
at all. No multicast is sent: the SSDP builders are tested on their own.
"""

from __future__ import annotations

import re
import socket
import time
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.server import tv_album
from ninaivu.server.tv_album import TvAlbum
from ninaivu.storage import db

LAN_PEER = "192.168.1.20"
HOST = "192.168.1.10:8200"
SOAP_CDS = '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'


def _album(conn, ids, name="Living room"):
    album_id = db.create_album(conn, name)
    db.album_add(conn, album_id, ids)
    conn.commit()
    return album_id


def _picture_ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND trashed=0 ORDER BY id")]


def _add_file(conn, cfg, name: str, kind: str, data: bytes) -> int:
    root = Path(cfg.active_root)
    (root / "clips").mkdir(exist_ok=True)
    path = root / "clips" / name
    path.write_bytes(data)
    cur = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, mtime, "
        "captured_at, date_key, duration) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (str(root), f"clips/{name}", name, "clips", Path(name).suffix, kind, len(data),
         time.time(), time.time() + 3600, time.strftime("%Y-%m-%d"), 75.5))
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture()
def tv(scanned):
    cfg, conn, _ = scanned
    service = TvAlbum(cfg, lambda: db.connect(cfg.db_path))
    service.secret = tv_album.secret(conn)
    service.udn = tv_album.device_uuid(conn)
    return cfg, conn, service


def _browse(service, object_id="0", flag="BrowseDirectChildren", start=0, count=0,
            peer=LAN_PEER):
    body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f"<ObjectID>{object_id}</ObjectID><BrowseFlag>{flag}</BrowseFlag><Filter>*</Filter>"
        f"<StartingIndex>{start}</StartingIndex><RequestedCount>{count}</RequestedCount>"
        "<SortCriteria></SortCriteria></u:Browse></s:Body></s:Envelope>").encode()
    return service.respond("POST", f"/{service.secret}/ctl/cds", {"SOAPACTION": SOAP_CDS},
                           body, peer, HOST)


def _result(reply):
    assert reply.status == 200, reply.body
    envelope = ElementTree.fromstring(reply.body)
    values = {el.tag: el.text or "" for el in envelope.iter()
              if el.tag in ("Result", "NumberReturned", "TotalMatches", "UpdateID")}
    items = ElementTree.fromstring(values["Result"])
    return values, items


NS = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
      "dc": "http://purl.org/dc/elements/1.1/",
      "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}


def _ids(items):
    return [int(el.get("id")[1:]) for el in items.findall("d:item", NS)]


# --- Browse ------------------------------------------------------------------------

def test_browse_lists_the_album_newest_first(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)[:5]
    cfg.tv_album = _album(conn, ids)
    values, items = _result(_browse(service))
    assert values["TotalMatches"] == "5" and values["NumberReturned"] == "5"
    assert sorted(_ids(items)) == sorted(ids)
    order = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE id IN (%s) ORDER BY COALESCE(captured_at, mtime) DESC, "
        "id DESC" % ",".join(map(str, ids)))]
    assert _ids(items) == order
    first = items.find("d:item", NS)
    assert first.findtext("upnp:class", namespaces=NS) == "object.item.imageItem.photo"
    res = first.findall("d:res", NS)
    assert "DLNA.ORG_PN=JPEG_LRG" in res[0].get("protocolInfo")
    assert res[0].text == f"http://{HOST}/{service.secret}/photo/{order[0]}.jpg"
    assert any("JPEG_TN" in r.get("protocolInfo") for r in res)
    # And the album itself, as the root container.
    values, root = _result(_browse(service, flag="BrowseMetadata"))
    container = root.find("d:container", NS)
    assert container.get("id") == "0" and container.get("childCount") == "5"
    assert container.findtext("dc:title", namespaces=NS) == "Living room"


def test_browse_pages_through_a_large_album(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)
    assert len(ids) >= 6
    cfg.tv_album = _album(conn, ids)
    seen = []
    for start in range(0, len(ids), 4):
        values, items = _result(_browse(service, start=start, count=4))
        assert values["TotalMatches"] == str(len(ids))
        assert int(values["NumberReturned"]) == len(_ids(items)) <= 4
        seen += _ids(items)
    assert seen == list(dict.fromkeys(seen)) and sorted(seen) == sorted(ids)
    values, items = _result(_browse(service, start=len(ids) + 10, count=4))
    assert values["NumberReturned"] == "0" and values["TotalMatches"] == str(len(ids))


def test_titles_from_file_names_are_escaped(tv):
    cfg, conn, service = tv
    asset = _add_file(conn, cfg, "a<b>&c.mp4", "video", b"x" * 100)
    conn.execute("UPDATE assets SET captured_at=NULL WHERE id=?", (asset,))
    conn.commit()
    cfg.tv_album = _album(conn, [asset])
    _, items = _result(_browse(service))
    item = items.find("d:item", NS)
    assert item.findtext("dc:title", namespaces=NS) == "a<b>&c"
    assert item.findtext("upnp:class", namespaces=NS) == "object.item.videoItem"
    res = item.find("d:res", NS)
    assert res.get("duration") == "0:01:15.500" and res.get("size") == "100"
    assert res.text.endswith(f"/video/{asset}.mp4")


def test_one_item_and_a_missing_one(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)[:2]
    cfg.tv_album = _album(conn, ids)
    values, items = _result(_browse(service, object_id=f"a{ids[0]}", flag="BrowseMetadata"))
    assert _ids(items) == [ids[0]]
    outside = _picture_ids(conn)[3]
    reply = _browse(service, object_id=f"a{outside}", flag="BrowseMetadata")
    assert reply.status == 500 and b"<errorCode>701</errorCode>" in reply.body


def test_the_small_actions_answer(tv):
    cfg, conn, service = tv
    cfg.tv_album = _album(conn, _picture_ids(conn)[:2])

    def call(service_key, action):
        body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action} xmlns:u="urn:x"/></s:Body></s:Envelope>').encode()
        return service.respond("POST", f"/{service.secret}/ctl/{service_key}", {}, body,
                               LAN_PEER, HOST)

    first = call("cds", "GetSystemUpdateID")
    assert first.status == 200 and re.search(rb"<Id>\d+</Id>", first.body)
    assert b"<SearchCaps></SearchCaps>" in call("cds", "GetSearchCapabilities").body
    assert b"<SortCaps></SortCaps>" in call("cds", "GetSortCapabilities").body
    info = call("cms", "GetProtocolInfo")
    assert info.status == 200 and b"JPEG_TN" in info.body and b"video/mp4" in info.body
    assert call("cds", "Search").status == 500
    # The update id moves when the album does.
    db.album_add(conn, cfg.tv_album, [_picture_ids(conn)[4]])
    conn.commit()
    assert call("cds", "GetSystemUpdateID").body != first.body
    for path in ("device.xml", "cds.xml", "cms.xml"):
        reply = service.respond("GET", f"/{service.secret}/{path}", {}, b"", LAN_PEER, HOST)
        assert reply.status == 200
        ElementTree.fromstring(reply.body)
    device = service.respond("GET", f"/{service.secret}/device.xml", {}, b"", LAN_PEER, HOST)
    assert b"urn:schemas-upnp-org:device:MediaServer:1" in device.body
    assert f"/{service.secret}/ctl/cds".encode() in device.body


# --- what is never shown ---------------------------------------------------------------

def test_hidden_flagged_trashed_and_sound_are_never_listed_or_served(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)[:6]
    hidden, flagged, binned, kept = ids[0], ids[1], ids[2], ids[3]
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
    conn.execute("UPDATE assets SET nsfw=1 WHERE id=?", (flagged,))
    conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (binned,))
    conn.commit()
    sound = _add_file(conn, cfg, "call.mp3", "audio", b"ID3" + b"\0" * 64)
    cfg.tv_album = _album(conn, [*ids, sound])
    values, items = _result(_browse(service))
    listed = _ids(items)
    assert kept in listed
    for never in (hidden, flagged, binned, sound):
        assert never not in listed
        for kind in ("photo", "thumb", "video"):
            ext = ".mp3" if kind == "video" else ".jpg"
            reply = service.respond("GET", f"/{service.secret}/{kind}/{never}{ext}", {}, b"",
                                    LAN_PEER, HOST)
            assert reply.status == 404, (never, kind)
        reply = _browse(service, object_id=f"a{never}", flag="BrowseMetadata")
        assert reply.status == 500
    assert values["TotalMatches"] == str(len(listed)) == "3"


def test_only_the_chosen_album(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)
    cfg.tv_album = _album(conn, ids[:2])
    _album(conn, ids[2:], name="Elsewhere")
    reply = service.respond("GET", f"/{service.secret}/photo/{ids[3]}.jpg", {}, b"",
                            LAN_PEER, HOST)
    assert reply.status == 404
    reply = service.respond("GET", f"/{service.secret}/photo/{ids[0]}.jpg", {}, b"",
                            LAN_PEER, HOST)
    assert reply.status == 200 and reply.headers["Content-Type"] == "image/jpeg"
    from PIL import Image
    with Image.open(reply.path) as image:
        assert image.format == "JPEG"
        assert not image.getexif()                 # nothing of where or with what
    thumb = service.respond("GET", f"/{service.secret}/thumb/{ids[0]}.jpg", {}, b"",
                            LAN_PEER, HOST)
    assert thumb.status == 200 and thumb.headers["Content-Type"] == "image/jpeg"
    with Image.open(thumb.path) as image:
        assert image.format == "JPEG" and max(image.size) <= 160


def test_the_family_date_limit_applies(tv):
    import json
    from ninaivu.server import date_policy
    cfg, conn, service = tv
    ids = _picture_ids(conn)
    cfg.tv_album = _album(conn, ids)
    db.set_meta(conn, date_policy.KEY, json.dumps(dict(
        date_policy.DEFAULTS, cutoff="2100-01-01", family="after")))
    conn.commit()
    values, _ = _result(_browse(service))
    assert values["TotalMatches"] == "0"


# --- who may ask --------------------------------------------------------------

def test_a_wrong_secret_is_not_found(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)[:2]
    cfg.tv_album = _album(conn, ids)
    for path in ("/device.xml", "/nope/device.xml", f"/{service.secret[:-1]}x/device.xml",
                 f"/x{service.secret}/photo/{ids[0]}.jpg", f"/{service.secret}/../device.xml"):
        assert service.respond("GET", path, {}, b"", LAN_PEER, HOST).status == 404, path
    assert service.respond("GET", f"/{service.secret}/device.xml", {}, b"",
                           LAN_PEER, HOST).status == 200


def test_a_new_secret_forgets_the_old_addresses(tv):
    cfg, conn, service = tv
    cfg.tv_album = _album(conn, _picture_ids(conn)[:1])
    old = service.secret
    new = service.new_secret()
    assert new != old and tv_album.secret(conn) == new
    assert service.respond("GET", f"/{old}/device.xml", {}, b"", LAN_PEER, HOST).status == 404
    assert service.respond("GET", f"/{new}/device.xml", {}, b"", LAN_PEER, HOST).status == 200


@pytest.mark.parametrize("peer", ["8.8.8.8", "2001:4860::8888", "100.101.102.103",
                                  "fd7a:115c:a1e0::1", "::ffff:8.8.4.4", "not an address"])
def test_a_peer_outside_the_house_is_refused(tv, peer):
    cfg, conn, service = tv
    cfg.tv_album = _album(conn, _picture_ids(conn)[:1])
    service._outside = tv_album.outside_networks(cfg)
    reply = service.respond("GET", f"/{service.secret}/device.xml", {}, b"", peer, HOST)
    assert reply.status == 403
    assert _browse(service, peer=peer).status == 403


@pytest.mark.parametrize("peer", ["192.168.0.5", "10.1.2.3", "172.20.0.9", "169.254.10.1",
                                  "127.0.0.1", "fe80::1", "fd12:3456::1", "::ffff:192.168.1.4"])
def test_home_addresses_are_let_in(peer):
    assert tv_album.home_peer(peer, tv_album.outside_networks(object()))


def test_a_wireguard_range_is_away_from_home():
    import ipaddress
    tunnel = (ipaddress.ip_network("10.8.0.0/24"),)
    assert not tv_album.home_peer("10.8.0.4", tunnel)
    assert tv_album.home_peer("10.9.0.4", tunnel)


# --- ranges ---------------------------------------------------------------------

def test_a_video_is_served_in_ranges(tv):
    cfg, conn, service = tv
    data = bytes(range(256)) * 40
    clip = _add_file(conn, cfg, "party.mp4", "video", data)
    cfg.tv_album = _album(conn, [clip])
    url = f"/{service.secret}/video/{clip}.mp4"

    whole = service.respond("GET", url, {}, b"", LAN_PEER, HOST)
    assert whole.status == 200 and whole.length == len(data)
    assert whole.headers["Accept-Ranges"] == "bytes"
    assert whole.headers["Content-Type"] == "video/mp4"
    assert whole.headers["transferMode.dlna.org"] == "Streaming"

    part = service.respond("GET", url, {"Range": "bytes=100-199"}, b"", LAN_PEER, HOST)
    assert part.status == 206 and (part.offset, part.length) == (100, 100)
    assert part.headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    tail = service.respond("GET", url, {"Range": "bytes=-10"}, b"", LAN_PEER, HOST)
    assert (tail.status, tail.offset, tail.length) == (206, len(data) - 10, 10)
    rest = service.respond("GET", url, {"Range": "bytes=10000-"}, b"", LAN_PEER, HOST)
    assert (rest.status, rest.offset, rest.length) == (206, 10000, len(data) - 10000)
    beyond = service.respond("GET", url, {"Range": f"bytes={len(data)}-"}, b"", LAN_PEER, HOST)
    assert beyond.status == 416 and beyond.headers["Content-Range"] == f"bytes */{len(data)}"
    # A photograph's address does not serve the video, and the other way round.
    assert service.respond("GET", f"/{service.secret}/photo/{clip}.jpg", {}, b"",
                           LAN_PEER, HOST).status == 404


def test_the_server_answers_over_http_from_this_computer(tv):
    """End to end on a real socket (loopback counts as home); no multicast."""
    cfg, conn, service = tv
    data = b"0123456789" * 1000
    clip = _add_file(conn, cfg, "clip.mp4", "video", data)
    cfg.tv_album = _album(conn, [clip, *_picture_ids(conn)[:2]])
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    cfg.tv_port = probe.getsockname()[1]
    probe.close()
    assert service.start(announce=False), service.problem
    try:
        base = f"http://127.0.0.1:{cfg.tv_port}/{service.secret}"
        with urllib.request.urlopen(f"{base}/device.xml", timeout=10) as response:
            assert b"MediaServer:1" in response.read()
        request = urllib.request.Request(f"{base}/video/{clip}.mp4",
                                         headers={"Range": "bytes=5-14"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 206 and response.read() == data[5:15]
        request = urllib.request.Request(f"{base}/video/{clip}.mp4", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.headers["Content-Length"] == str(len(data))
            assert response.read() == b""
        status = service.status()
        assert status["running"] and status["items"] == 3
    finally:
        service.stop()
    assert not service.running


def test_a_port_in_use_is_a_warning_not_a_crash(tv):
    cfg, conn, service = tv
    cfg.tv_album = _album(conn, _picture_ids(conn)[:1])
    holder = socket.socket()
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    cfg.tv_port = holder.getsockname()[1]
    try:
        assert service.start(announce=False) is False
        assert service.problem and str(cfg.tv_port) in service.problem
    finally:
        holder.close()
        service.stop()


# --- off ----------------------------------------------------------------------

def test_with_no_album_nothing_is_served(tv):
    cfg, conn, service = tv
    ids = _picture_ids(conn)[:2]
    _album(conn, ids)
    cfg.tv_album = 0
    assert service.start() is False and not service.running
    for path in (f"/{service.secret}/device.xml", f"/{service.secret}/photo/{ids[0]}.jpg"):
        assert service.respond("GET", path, {}, b"", LAN_PEER, HOST).status == 404
    assert _browse(service).status == 404


def test_off_by_default_and_in_the_settings():
    from ninaivu.server import settings_groups
    from ninaivu.server.config import Config
    assert Config().tv_album == 0
    assert Config().tv_port not in (Config().port, Config().admin_port)
    assert settings_groups.group_of("tv_album") and settings_groups.group_of("tv_port")
    with pytest.raises(settings_groups.BadValue):
        settings_groups.apply(Config(), {"tv_album": 3})


# --- SSDP ---------------------------------------------------------------------

def test_an_msearch_is_answered_for_what_this_server_is():
    udn = "11111111-2222-3333-4444-555555555555"
    location = "http://192.168.1.10:8200/s3cret/device.xml"
    for target in ("upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1",
                   "urn:schemas-upnp-org:service:ContentDirectory:1"):
        [reply] = tv_album.search_replies(target, udn, location, server="Test/1 UPnP/1.0 X/1")
        text = reply.decode("ascii")
        assert text.startswith("HTTP/1.1 200 OK\r\n") and text.endswith("\r\n\r\n")
        headers = dict(line.split(": ", 1) if ": " in line else (line.rstrip(":"), "")
                       for line in text.split("\r\n")[1:] if line)
        assert headers["ST"] == target
        assert headers["USN"] == f"uuid:{udn}::{target}"
        assert headers["LOCATION"] == location
        assert headers["CACHE-CONTROL"] == "max-age=1800"
        assert "EXT" in headers and "SERVER" in headers and "DATE" in headers
    everything = tv_album.search_replies("ssdp:all", udn, location)
    assert len(everything) == 5
    assert any(f"USN: uuid:{udn}\r\n".encode() in r for r in everything)
    assert tv_album.search_replies("urn:schemas-upnp-org:device:MediaRenderer:1",
                                   udn, location) == []


def test_an_msearch_is_read_and_anything_else_ignored():
    search = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
              b'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n')
    assert tv_album.parse_search(search) == "ssdp:all"
    assert tv_album.parse_search(search.replace(b"ssdp:discover", b"other")) is None
    assert tv_album.parse_search(b"NOTIFY * HTTP/1.1\r\nNT: x\r\n\r\n") is None
    assert tv_album.parse_search(b"\xff\xfe") is None


def test_the_announcements():
    udn = "11111111-2222-3333-4444-555555555555"
    alive = tv_album.notify_messages("ssdp:alive", udn, "http://h/s/device.xml")
    bye = tv_album.notify_messages("ssdp:byebye", udn, "http://h/s/device.xml")
    assert len(alive) == len(bye) == 5
    assert all(m.startswith(b"NOTIFY * HTTP/1.1\r\n") for m in alive + bye)
    assert all(b"LOCATION: http://h/s/device.xml" in m for m in alive)
    assert all(b"NTS: ssdp:byebye" in m and b"LOCATION" not in m for m in bye)


# --- the console ----------------------------------------------------------------

def test_the_console_sets_and_reads_the_tv_album(app, people, scanned, monkeypatch):
    cfg, conn, _ = scanned
    client = login(app.test_client(), *ADMIN)
    family = login(app.test_client(), *FAMILY)
    album_id = _album(conn, _picture_ids(conn)[:3])
    started = []
    monkeypatch.setattr(TvAlbum, "start", lambda self, **_: started.append(1) or False)

    off = client.get("/api/admin/tv-album")
    assert off.status_code == 200
    assert off.get_json()["album_id"] is None and off.get_json()["running"] is False
    assert family.get("/api/admin/tv-album").status_code in (401, 403)
    assert family.put("/api/admin/tv-album", json={"album_id": album_id}).status_code in (401, 403)

    assert client.put("/api/admin/tv-album", json={"album_id": 999999}).status_code == 404
    assert client.put("/api/admin/tv-album", json={"album_id": "x"}).status_code == 400
    assert client.put("/api/admin/tv-album", json={"album_id": True}).status_code == 400
    assert client.put("/api/admin/tv-album", json={"port": 80}).status_code == 400
    assert client.put("/api/admin/tv-album", json=[1]).status_code == 400

    chosen = client.put("/api/admin/tv-album", json={"album_id": album_id, "port": 8300})
    assert chosen.status_code == 200, chosen.get_json()
    body = chosen.get_json()
    assert body["album_id"] == album_id and body["items"] == 3 and body["port"] == 8300
    assert body["album_name"] == "Living room"
    assert cfg.tv_album == album_id and cfg.tv_port == 8300 and started

    services = app.config["MV_SERVICES"]
    before = tv_album.secret(conn)
    assert client.post("/api/admin/tv-album/secret").status_code == 200
    assert tv_album.secret(conn) != before

    assert client.put("/api/admin/tv-album", json={"album_id": 0}).status_code == 200
    assert cfg.tv_album == 0 and not services.tv_album.running
