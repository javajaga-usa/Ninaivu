"""Two computers: the console's page for them, and what they say to each other.

``/api/admin/mugil/...`` is the console's: this computer's part, the code the
Secondary shows, pairing from the Primary, and sending the originals. Admin
only, like the rest of the console.

``/api/mugil/...`` (:data:`mugil_peer_bp`) is the other computer's, never a
person's: no session, no cookie, answered only when this computer is the
Secondary, and never from the internet. On a single computer every one of
them is 404. Introducing and pairing need an open wait (mugil/meeting.py) or
a code shown on this console; everything after is signed with the key the
pairing agreed (mugil/pairing.py). They are on the family app's port as well
as the console's, because the family app is the one the house network
reaches. Their refusals are ``{"refused": …}``: read by the Primary and
written to its log, not shown on a page to translate.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_file

from .. import __version__
from ..mugil import PRIMARY, ROLES, SECONDARY, SINGLE, display_name, machines, meeting, pairing, role
from ..mugil.courier import Partner, PartnerError, confirmed
from ..mugil.hold import REASONS, Holding, Refused, folder_problem
from ..server import auth
from ..server.auth import current_user, require_admin
from ..storage import db
from ..words import refusal
from ._body import JSON_MAX_BYTES, json_body, read_at_most

log = logging.getLogger(__name__)

mugil_bp = Blueprint("mugil", __name__)
mugil_peer_bp = Blueprint("mugil_peer", __name__)

#: The code the Secondary's console is showing, and signatures already seen.
_codes = pairing.Codes()
_seen = pairing.Seen()


def _cfg():
    return current_app.config["MV_CONFIG"]


def _courier():
    return getattr(current_app.config["MV_SERVICES"], "mugil_courier", None)


def _welcome() -> meeting.Welcome:
    """The Secondary's wait for a Primary, one for the life of the process."""
    services = current_app.config["MV_SERVICES"]
    if getattr(services, "mugil_welcome", None) is None:
        services.mugil_welcome = meeting.Welcome()
    return services.mugil_welcome


def _introductions() -> meeting.Introductions:
    services = current_app.config["MV_SERVICES"]
    if getattr(services, "mugil_introductions", None) is None:
        services.mugil_introductions = meeting.Introductions()
    return services.mugil_introductions


def _name(cfg) -> str:
    return display_name(cfg)


def _adopter():
    return getattr(current_app.config["MV_SERVICES"], "mugil_adopter", None)


def _library_sizes(cfg) -> list[dict]:
    """On the Primary: each library folder, its name on the Secondary, and
    how many files and bytes it holds, as the index counts them."""
    out = []
    try:
        conn = db.connect(cfg.db_path)
    except Exception:                                            # noqa: BLE001 - nothing to count
        return out
    for root in list(cfg.libraries):
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM assets "
                           "WHERE root=? AND trashed=0", (str(root),)).fetchone()
        out.append({"root": str(root), "name": Path(root).name or "Library",
                    "files": int(row[0]), "bytes": int(row[1])})
    return out


def _start_sending() -> None:
    """Just paired: the first copy starts in a minute, not at the next
    look. A Secondary that is taking in a copy made by hand makes it wait."""
    courier = _courier()
    if courier is not None:
        courier.soon()


def _holding(cfg) -> Holding | None:
    folder = str(getattr(cfg, "mugil_originals_dir", "") or "")
    return Holding(folder) if folder else None


def _libraries_inside(folder: str, cfg) -> list[str]:
    """This computer's library folders inside *folder*, when the folder
    holds nothing else of Ninaivu's: not its state folder, and not itself
    inside a library."""
    try:
        target = Path(folder).expanduser().resolve()
        state = Path(cfg.state_dir).resolve()
    except OSError:
        return []
    if target == state or target in state.parents or state in target.parents:
        return []
    inside = []
    for root in cfg.roots:
        try:
            resolved = Path(root).expanduser().resolve()
        except OSError:
            continue
        if resolved == target or resolved in target.parents:
            return []                    # the folder is in a library, or is one
        if target in resolved.parents:
            inside.append(str(root))
    return inside


def _library_folder_chosen(folder: str, cfg) -> str | None:
    """The folder that keeps the originals when *folder* is a library folder
    itself: its parent, so the library is ``<originals>/<its name>``, the
    layout the Primary sends into. A library folder is one of this
    computer's library folders, or a folder carrying a library's id
    (``.ninaivu-library``: the Primary's library copied over by hand). None
    when *folder* is not one; empty when it is the top of its disk, so the
    folder above it is on another disk."""
    from ..storage import library_id                              # noqa: PLC0415
    try:
        target = Path(folder).expanduser()
        if not target.is_absolute():
            return None
        target = target.resolve()
        roots = {Path(r).expanduser().resolve() for r in cfg.roots}
    except OSError:
        return None
    if target not in roots and library_id.read_id(target) is None:
        return None
    parent = target.parent
    if parent == target or os.path.ismount(target):
        # The top of a disk: the folder above it is on another disk.
        return ""
    return str(parent)


def _holds_files(folder: str, holding: Holding | None = None) -> bool:
    """Whether *folder* already holds photographs or videos not yet known as
    kept: a library copied, or kept, there before pairing."""
    from ..mugil.adopt import walk                                # noqa: PLC0415
    try:
        kept = (holding or Holding(folder)).kept_paths()
    except Exception:                                    # noqa: BLE001 - nothing known kept
        kept = set()
    try:
        return any(rel not in kept for rel, _full in walk(Path(folder)))
    except OSError:
        return False


def _audit(what: str) -> None:
    auth.audit(db.connect(_cfg().db_path), current_user().id, "two_computers", what)


# -- the console ---------------------------------------------------------------

@mugil_bp.get("/api/admin/mugil")
@require_admin
def mugil_status():
    cfg = _cfg()
    part = role(cfg)
    pair = pairing.load(cfg.state_dir)
    answer = {
        "role": part, "roles": list(ROLES), "name": _name(cfg),
        "paired": pair is not None, "pairing": pairing.public(pair),
        "partner_url": cfg.mugil_partner_url, "originals_dir": cfg.mugil_originals_dir,
        "copy_every_hours": cfg.mugil_copy_every_hours,
        "family_copy": bool(getattr(cfg, "mugil_family_copy", False)),
        "free_space": bool(getattr(cfg, "mugil_free_space", False)),
        "copy_mode": str(getattr(cfg, "mugil_copy_mode", "automatic") or "automatic"),
        "version": __version__,
    }
    other: dict = {}
    if part != SINGLE and pair is not None:
        other = machines.remembered(cfg.state_dir) or {}
        answer["partner_version"] = other.get("version")
        answer["same_version"] = machines.same_version(__version__, other.get("version"))
    if part == PRIMARY:
        courier = _courier()
        answer["sending"] = courier.status() if courier is not None else {}
        answer["confirmed"] = {root: confirmed(cfg.state_dir, root) for root in cfg.roots}
        maker = getattr(current_app.config["MV_SERVICES"], "mugil_display", None)
        answer["display"] = maker.status() if maker is not None else {}
        sender = getattr(current_app.config["MV_SERVICES"], "mugil_handover", None)
        answer["handover"] = sender.status() if sender is not None else {}
        answer["freeing"] = _freeing().status() if _freeing() is not None else {}
        from ..mugil import fetch                                # noqa: PLC0415
        answer["fetched"] = fetch.summary(cfg.state_dir)
        answer["libraries"] = _library_sizes(cfg)
        answer["secondary"] = {k: other.get(k) for k in ("copy_mode", "manual", "health")} \
            if pair is not None else {}
    elif part == SECONDARY:
        holding = _holding(cfg)
        answer["keeping"] = holding.summary() if holding is not None else {}
        inbox = _inbox(cfg)
        answer["phone_inbox"] = inbox.summary() if inbox is not None else {}
        from ..mugil import handover                             # noqa: PLC0415
        answer["handover"] = handover.summary(cfg.state_dir)
        answer["welcome"] = _welcome().status() if pair is None else {"waiting": False}
        from ..mugil import wake                                 # noqa: PLC0415
        answer["wake"] = wake.load(cfg.state_dir)
        beacon = getattr(current_app.config["MV_SERVICES"], "mugil_beacon", None)
        answer["listening"] = meeting.on_the_network(cfg) and not getattr(beacon, "problem", None)
        answer["manual"] = _adopter().status() if _adopter() is not None else {}
        answer["primary"] = machines.about_primary(cfg.state_dir) if pair is not None else {}
    if part != SINGLE:
        answer["on_the_network"] = meeting.on_the_network(cfg)
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/role")
@require_admin
def mugil_role():
    """This computer's part, and the settings that go with it."""
    cfg = _cfg()
    data = json_body()
    if not isinstance(data, dict) or set(data) - {"role", "originals_dir", "copy_every_hours",
                                                  "family_copy", "free_space", "copy_mode",
                                                  "library_into_originals"}:
        return jsonify({**refusal("Send role, originals_dir or copy_every_hours."), "status": 400}), 400
    if "copy_mode" in data and data["copy_mode"] not in ("automatic", "manual"):
        return jsonify({**refusal("Choose automatic or manual."), "status": 400}), 400
    for switch in ("family_copy", "free_space"):
        if switch in data and not isinstance(data[switch], bool):
            return jsonify({**refusal("Say true or false."), "status": 400}), 400
    wanted = str(data.get("role", role(cfg)) or SINGLE).lower()
    if wanted not in ROLES:
        return jsonify({**refusal("Choose single, primary or secondary."), "status": 400}), 400
    if wanted != role(cfg) and pairing.load(cfg.state_dir) is not None:
        return jsonify({**refusal("Unpair the two computers before changing this computer's part."),
                        "status": 409}), 409
    hours = data.get("copy_every_hours", cfg.mugil_copy_every_hours)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 <= hours <= 24 * 90:
        return jsonify({**refusal("Choose a number of hours from 0 (only when asked) to 2160."),
                        "status": 400}), 400
    folder = data.get("originals_dir", cfg.mugil_originals_dir)
    if not isinstance(folder, str):
        return jsonify({**refusal("Give the whole path to the folder."), "status": 400}), 400
    folder = folder.strip()
    check_found = False
    if wanted == SECONDARY and folder and folder != (cfg.mugil_originals_dir or ""):
        # The library folder itself chosen: its files are kept where they
        # are, and the folder above it keeps the originals.
        above = _library_folder_chosen(folder, cfg)
        if above == "":
            return jsonify({**refusal(
                "That library is the whole disk. Make a folder on the disk, move the library "
                "folders into it, and choose that folder: the originals are kept as "
                "<that folder>/<library folder name>."), "status": 400}), 400
        if above is not None:
            folder = above
    if wanted == SECONDARY and (folder or "originals_dir" in data) \
            and folder != (cfg.mugil_originals_dir or ""):
        problem = folder_problem(folder, list(cfg.roots), cfg.state_dir)
        inside = _libraries_inside(folder, cfg) if problem else []
        if problem and inside:
            # This computer's own library is in that folder: a one-computer
            # Ninaivu becoming the Secondary, its library to be the originals.
            # Done only when asked, and with the sidecars written first, so the
            # names and albums go with the photographs (storage/xmp_read.py).
            if data.get("library_into_originals") is not True:
                return jsonify({"error": problem, "status": 409, "library_inside": inside,
                                "originals_dir": folder}), 409
            writer = getattr(current_app.config["MV_SERVICES"], "xmp", None)
            if writer is not None and writer.running:
                return jsonify({**refusal("The sidecars are still being written. Wait for them "
                                          "to finish, so the names and albums go with the "
                                          "photographs."), "status": 409}), 409
            rest = [r for r in cfg.roots if r not in inside]
            problem = folder_problem(folder, rest, cfg.state_dir)
            if not problem:
                for root in inside:
                    cfg.remove_library(root)
                _audit(f"this computer's library {', '.join(inside)} is kept as originals in {folder}")
        if problem:
            return jsonify({"error": problem, "status": 400}), 400
        # Photographs already in the folder (the library kept as the
        # originals, or a copy made before this) are checked at once, so the
        # Primary sends only what is missing or different: those already
        # here are marked as in sync without crossing the network.
        check_found = bool(folder) and "copy_mode" not in data and _holds_files(folder)
    if wanted != PRIMARY and _courier() is not None:
        _courier().stop()
    if wanted == SECONDARY and role(cfg) != SECONDARY:
        # Choosing the part is what lets the Primary find and pair with it.
        _welcome().open()
    elif wanted != SECONDARY:
        _welcome().close()
    cfg.mugil_role, cfg.mugil_originals_dir = wanted, folder
    cfg.mugil_copy_every_hours = float(hours)
    if "family_copy" in data:
        cfg.mugil_family_copy = data["family_copy"]
    if "free_space" in data:
        cfg.mugil_free_space = data["free_space"]
        if not data["free_space"] and _freeing() is not None:
            _freeing().stop()
    if "copy_mode" in data and data["copy_mode"] != cfg.mugil_copy_mode:
        cfg.mugil_copy_mode = data["copy_mode"]
        if data["copy_mode"] == "manual" and _adopter() is not None:
            # Chosen now: the household is about to copy; the Primary waits.
            cfg.save()
            _adopter().copying()
        elif _adopter() is not None:
            _adopter().stop()
    if check_found:
        cfg.mugil_copy_mode = "manual"
        cfg.save()
        if _adopter() is not None:
            _adopter().copying()                 # a check of another folder stops first
            _adopter().finished_copying()
    cfg.save()
    _audit(f"this computer is {wanted}; originals folder {folder or '(none)'}; "
           f"first copy {cfg.mugil_copy_mode}")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/code")
@require_admin
def mugil_code():
    """A code for the other computer, good for ten minutes: on the
    Secondary, to type on the Primary with this computer's address; on the
    Primary, to type on the Secondary with this one's (``join``)."""
    cfg = _cfg()
    if role(cfg) == SINGLE:
        return jsonify({**refusal("Make this computer the Secondary first."), "status": 409}), 409
    if role(cfg) == PRIMARY and pairing.load(cfg.state_dir) is not None:
        return jsonify({**refusal("Unpair the two computers before changing this computer's part."),
                        "status": 409}), 409
    code = _codes.new()
    _audit("pairing code shown")
    return jsonify({"code": code, "expires_in": pairing.CODE_LIFE})


@mugil_bp.post("/api/admin/mugil/pair")
@require_admin
def mugil_pair():
    """On the Primary: pair with the Secondary at ``url``, shown ``code``."""
    cfg = _cfg()
    if role(cfg) != PRIMARY:
        return jsonify({**refusal("Make this computer the Primary first."), "status": 409}), 409
    data = json_body()
    if isinstance(data, dict) and isinstance(data.get("found"), str):
        return _pair_by_tap(cfg, data["found"])
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) \
            or not isinstance(data.get("code"), str):
        return jsonify({**refusal("Send the other computer's address and the code it shows."),
                        "status": 400}), 400
    code = pairing.normal_code(data["code"])
    try:
        partner = Partner(data["url"])
        private, hello = pairing.primary_hello(code)
        answer = partner.request("POST", "/api/mugil/pair",
                                 body={**hello, "name": _name(cfg)}, signed=False)
    except PartnerError as exc:
        log.warning("Pairing with %s did not work: %s", data["url"], exc)
        return jsonify({**refusal("The other computer did not answer. Check its address, and that its console is on the network."),
                        "status": 502}), 502
    key = pairing.primary_key(private, code, answer)
    if key is None:
        return jsonify({**refusal("The other computer did not prove it knew the code."),
                        "status": 502}), 502
    pairing.save(cfg.state_dir, {
        "key": key, "role": PRIMARY, "partner_name": str(answer.get("name") or ""),
        "partner_url": partner.url, "paired_at": time.time()})
    cfg.mugil_partner_url = partner.url
    cfg.save()
    _audit(f"paired with {partner.url}")
    _start_sending()
    return mugil_status()


def _pair_by_tap(cfg, ident: str):
    """The tap on a Secondary that was found and introduced to."""
    met = _introductions().get(ident)
    if met is None:
        return jsonify({**refusal("Look for the Secondary again: that one is no longer waiting."),
                        "status": 409}), 409
    key = pairing.tap_key(met["private"], met["theirs"], met["half"], met["theirs"])
    try:
        answer = Partner(met["url"], timeout=10, ca=met.get("ca")).request(
            "POST", "/api/mugil/pair",
            body={"tap": True, "public": met["half"].hex(), "proof": pairing.tap_proof(key)},
            signed=False)
    except PartnerError as exc:
        log.warning("Pairing with %s did not work: %s", met["url"], exc)
        return jsonify({**refusal("Look for the Secondary again: that one is no longer waiting."),
                        "status": 409}), 409
    pairing.save(cfg.state_dir, {
        "key": key, "role": PRIMARY, "partner_name": str(answer.get("name") or met["name"]),
        "partner_url": met["url"], "number": met["number"], "paired_at": time.time(),
        **({"partner_ca": met["ca"]} if met.get("ca") else {})})
    _introductions().clear()
    cfg.mugil_partner_url = met["url"]
    cfg.save()
    _audit(f"paired with {met['url']} ({met['number']})")
    _start_sending()
    return mugil_status()


def partner_address(text: str, default_port: int) -> str:
    """What was typed for the other computer, as an address to reach it at:
    ``mac-mini``, ``100.64.1.2:8080`` and ``http://mac-mini.tail1234.ts.net``
    all do. Without a port, the family app's (the one the house network
    reaches; the console may answer this computer only)."""
    text = str(text or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    from urllib.parse import urlsplit                            # noqa: PLC0415
    parts = urlsplit(text)
    try:
        has_port = parts.port is not None
    except ValueError:
        return text
    if not has_port and parts.hostname and parts.scheme == "http" and default_port \
            and default_port != 80:
        text = f"{parts.scheme}://{_host(parts.hostname)}:{default_port}"
    return text


@mugil_bp.post("/api/admin/mugil/join")
@require_admin
def mugil_join():
    """On the Secondary: pair with the Primary at ``url``, with the code
    its console shows. The other way round from :func:`mugil_pair`, for a
    Secondary set up first, or one the Primary cannot find."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return jsonify({**refusal("Make this computer the Secondary first."), "status": 409}), 409
    if pairing.load(cfg.state_dir) is not None:
        return jsonify({**refusal("This computer is paired already."), "status": 409}), 409
    folder = str(getattr(cfg, "mugil_originals_dir", "") or "")
    problem = folder_problem(folder, list(cfg.roots), cfg.state_dir) if folder \
        else "Choose the originals folder first."
    if problem:
        return jsonify({"error": problem, "status": 409}), 409
    data = json_body()
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) \
            or not isinstance(data.get("code"), str) or not data["url"].strip():
        return jsonify({**refusal("Send the Primary's address and the code it shows."),
                        "status": 400}), 400
    code = pairing.normal_code(data["code"])
    url = partner_address(data["url"], int(getattr(cfg, "port", 0) or 0))
    try:
        partner = Partner(url, timeout=15)
        https, ca = _own_tls(cfg)
        private, hello = pairing.primary_hello(code, ca)
        answer = partner.request("POST", "/api/mugil/join", signed=False, body={
            **hello, "name": _name(cfg), "port": int(getattr(cfg, "port", 0) or 0),
            **({"https": True} if https else {})})
    except PartnerError as exc:
        log.warning("Pairing with the Primary at %s did not work: %s", url, exc)
        if exc.status == 403:
            return jsonify({**refusal("That code is not the one the Primary shows, or it has "
                                      "expired. Show a new code on the Primary and type it again."),
                            "status": 403}), 403
        if exc.status == 409:
            return jsonify({**refusal("The Primary is paired with another computer already. "
                                      "Unpair it there first."), "status": 409}), 409
        if exc.status == 404:
            return jsonify({**refusal("The computer at that address is not a Primary yet. On it, "
                                      "choose Two computers, this is the Primary."),
                            "status": 409}), 409
        # Switched off, asleep, another network: this computer keeps
        # waiting, so the Primary can pair with it when it is back.
        if not _welcome().waiting:
            _welcome().open()
        return jsonify({**refusal("The Primary did not answer at that address. Check that it is "
                                  "on and on this network. This computer keeps waiting, so the "
                                  "Primary can also find it on its Two computers page."),
                        "status": 502, "waiting": True}), 502
    key = pairing.primary_key(private, code, answer)
    if key is None:
        return jsonify({**refusal("The other computer did not prove it knew the code."),
                        "status": 502}), 502
    from urllib.parse import urlsplit                            # noqa: PLC0415
    pairing.save(cfg.state_dir, {
        "key": key, "role": SECONDARY, "partner_name": str(answer.get("name") or "")[:200],
        "partner_address": urlsplit(partner.url).hostname or "", "partner_url": partner.url,
        "paired_at": time.time()})
    machines.remember(cfg.state_dir, {
        "machine": {"name": str(answer.get("name") or "")[:200], "role": PRIMARY},
        "version": str(answer.get("version") or "")[:40] or None})
    _welcome().close()
    _audit(f"paired with the Primary at {partner.url}")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/manual")
@require_admin
def mugil_manual():
    """On the Secondary copied to by hand: ``{"done": true}`` when the copy
    is made (it is checked, then the Primary sends what is missing), or
    ``{"done": false}`` to copy more first (the Primary waits)."""
    cfg = _cfg()
    adopter = _adopter()
    if role(cfg) != SECONDARY or adopter is None:
        return jsonify({**refusal("Make this computer the Secondary first."), "status": 409}), 409
    if str(getattr(cfg, "mugil_copy_mode", "")) != "manual":
        return jsonify({**refusal("Choose to copy the library by hand first."), "status": 409}), 409
    data = json_body()
    if not isinstance(data, dict) or not isinstance(data.get("done"), bool):
        return jsonify({**refusal("Say true or false."), "status": 400}), 400
    if data["done"]:
        folder = str(getattr(cfg, "mugil_originals_dir", "") or "")
        problem = folder_problem(folder, list(cfg.roots), cfg.state_dir) if folder \
            else "Choose the originals folder first."
        if problem:
            return jsonify({"error": problem, "status": 409}), 409
        adopter.finished_copying()
        _audit("the copy made by hand is finished; checking it")
    else:
        adopter.copying()
        _audit("copying more by hand; the Primary waits")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/welcome")
@require_admin
def mugil_welcome():
    """On the Secondary: wait half an hour for a Primary to find it and pair."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return jsonify({**refusal("Make this computer the Secondary first."), "status": 409}), 409
    if pairing.load(cfg.state_dir) is not None:
        return jsonify({**refusal("Unpair the two computers before changing this computer's part."),
                        "status": 409}), 409
    _welcome().open()
    _audit("waiting for a Primary to pair")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/look")
@require_admin
def mugil_look():
    """On the Primary: the Secondaries on the house network and the tailnet,
    each introduced to, with the number both screens show."""
    cfg = _cfg()
    if role(cfg) != PRIMARY:
        return jsonify({**refusal("Make this computer the Primary first."), "status": 409}), 409
    found = []
    for other in meeting.discover():
        item = {k: other[k] for k in ("id", "name", "address", "waiting")}
        if other["waiting"]:
            # A Secondary with Use HTTPS on says so, and hands over the
            # authority its certificate comes from: the number covers it.
            scheme = "https" if other.get("https") else "http"
            ca = other.get("ca") if other.get("https") else None
            url = f"{scheme}://{_host(other['address'])}:{other['port']}"
            private, half = pairing.new_half()
            try:
                answer = Partner(url, timeout=5, ca=ca).request(
                    "POST", "/api/mugil/introduce",
                    body={"public": half.hex(), "name": _name(cfg)}, signed=False)
                theirs = bytes.fromhex(str(answer.get("public", "")))
                if len(theirs) != 32:
                    raise ValueError("public")
            except (PartnerError, ValueError) as exc:
                log.info("Two computers: %s did not take the introduction: %s", url, exc)
                item["waiting"] = False
                item["unreachable"] = True
            else:
                item["number"] = _introductions().keep(other["id"], private, half, theirs,
                                                       url, other["name"], ca=ca)
        found.append(item)
    return jsonify({"found": found})


def _host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _own_tls(cfg) -> tuple[bool, str | None]:
    """Whether this computer's family app speaks HTTPS (set at start,
    __main__), and the home authority its certificate comes from, when it is
    Ninaivu's own (not one given with --cert)."""
    https = bool(current_app.config.get("MV_HTTPS"))
    ca = meeting.own_ca(cfg) if https and current_app.config.get("MV_SERVE_LOCAL_CA") else None
    return https, ca


@mugil_bp.post("/api/admin/mugil/unpair")
@require_admin
def mugil_unpair():
    from ..mugil import free                                     # noqa: PLC0415
    cfg = _cfg()
    # Originals let go here are on the Secondary only: unpaired, nothing
    # would know where they are.
    gone = free.summary(db.connect(cfg.db_path))
    if gone["files"]:
        return jsonify({**refusal("{count} originals are kept only on the Secondary. Bring them "
                                  "back first, then unpair.", count=gone["files"]),
                        "status": 409}), 409
    if _courier() is not None:
        _courier().stop()
    pairing.forget(cfg.state_dir)
    machines.forget(cfg.state_dir)
    from ..mugil import wake                                     # noqa: PLC0415
    wake.forget(cfg.state_dir)
    _audit("unpaired")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/send")
@require_admin
def mugil_send():
    """On the Primary: send the originals the Secondary does not keep yet."""
    courier = _courier()
    if courier is None:
        return jsonify({**refusal("Make this computer the Primary first."), "status": 409}), 409
    started = courier.start()
    if not started.get("started"):
        return jsonify({"error": started.get("reason"), "status": 409}), 409
    _audit("send the originals now")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/stop")
@require_admin
def mugil_stop():
    if _courier() is not None:
        _courier().stop()
    return mugil_status()


# -- the other computer --------------------------------------------------------

@mugil_peer_bp.before_request
def _only_from_home():
    """The other computer is at home or on the tailnet, never the internet."""
    from ..server import remote                                    # noqa: PLC0415
    if remote.from_the_internet(_cfg(), request):
        return _not_here()
    return None


def _not_here():
    return jsonify({"refused": "Not found", "status": 404}), 404


def _target() -> str:
    query = request.query_string.decode("latin-1")
    return request.path + (f"?{query}" if query else "")


def _signed(digest_of_body: str | None = None):
    """The fingerprint the request was signed with, or None when it was not
    signed by the computer this one is paired with."""
    pair = pairing.load(_cfg().state_dir)
    if pair is None:
        return None
    vouched = pairing.verify(pair["key"], request.method, _target(), request.headers, _seen)
    if vouched is None:
        _note_replaced(pair)
    if vouched is None or (digest_of_body is not None and vouched != digest_of_body):
        return None
    _primary_seen(pair)
    return vouched


def _note_replaced(pair: dict) -> None:
    """A request signed with the key a move replaced comes from the computer
    that was the Primary before the index moved: it is told so (_unsigned)."""
    old = pair.get("replaced_key")
    if not old or time.time() - float(pair.get("replaced_at") or 0) > pairing.REPLACED_KEPT_FOR:
        return
    if pairing.verify(old, request.method, _target(), request.headers, pairing.Seen()):
        g.mugil_replaced = float(pair.get("replaced_at") or 0)


#: When the Primary last spoke to this Secondary, by state folder; written
#: down at most once a minute.
_last_written: dict[str, float] = {}


def _primary_seen(pair: dict) -> None:
    now = time.time()
    state = str(_cfg().state_dir)
    if now - _last_written.get(state, 0.0) < 60:
        return
    _last_written[state] = now
    if pair.get("role") == SECONDARY:
        _follow_the_primary(pair, state)
    machines.remember(state, {
        "machine": {"name": pair.get("partner_name") or "", "role": PRIMARY,
                    "version": str(request.headers.get("X-Ninaivu-Version", ""))[:40]},
        "address": request.remote_addr or "",
        "version": str(request.headers.get("X-Ninaivu-Version", ""))[:40] or None})


def _follow_the_primary(pair: dict, state: str) -> None:
    """Where the Primary is now, from where its signed requests come: a new
    address from the router, or the index moved to another computer. What
    this computer asks of the Primary goes there, without anybody typing it."""
    address = request.remote_addr or ""
    if not address or address == pair.get("partner_address"):
        return
    moved = {**pair, "partner_address": address}
    url = str(pair.get("partner_url") or "")
    if url:
        from urllib.parse import urlsplit                        # noqa: PLC0415
        try:
            port = urlsplit(url).port
        except ValueError:
            port = None
        if port:
            moved["partner_url"] = f"{urlsplit(url).scheme or 'http'}://{_host(address)}:{port}"
    try:
        pairing.save(state, moved)
    except OSError as exc:
        log.warning("Could not note the Primary's new address: %s", exc)


def _unsigned():
    if g.get("mugil_replaced"):
        # 409, not 401: not a stranger, a Primary that has been replaced.
        return jsonify({"refused": "Another computer has taken over as the Primary: the index "
                                   "was moved to it. This computer no longer sends to the "
                                   "Secondary; unpair it on its Two computers page.",
                        "replaced": True, "status": 409}), 409
    return jsonify({"refused": "This request is not signed by the computer this one is paired with.",
                    "status": 401}), 401


@mugil_peer_bp.post("/api/mugil/move")
def mugil_move_answer():
    """On the Secondary: the Primary has moved to another computer (its index
    was imported there, pairing and all), and agrees a new key, over the old
    one, so the computer it left can no longer act as the Primary
    (pairing.py, "Moving the Primary")."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    pair = pairing.load(cfg.state_dir)
    if pair is None or _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        hello = json.loads(raw)
    except ValueError:
        hello = None
    if not isinstance(hello, dict):
        return jsonify({"refused": "Send it as JSON.", "status": 400}), 400
    found = pairing.secondary_move_answer(pair["key"], hello)
    if found is None:
        return jsonify({"refused": "Send the new half of the key.", "status": 400}), 400
    key, answer = found
    name = str(hello.get("name") or pair.get("partner_name") or "")[:200]
    moved = {**pair, "key": key, "replaced_key": pair["key"], "replaced_at": time.time(),
             "partner_name": name, "partner_address": request.remote_addr or "",
             "moved_at": time.time()}
    try:
        port = int(hello.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if 0 < port < 65536 and request.remote_addr:
        # Where the Primary is now, for what this computer asks of it.
        old = str(pair.get("partner_url") or "")
        scheme = "https" if old.startswith("https://") else "http"
        moved["partner_url"] = f"{scheme}://{_host(request.remote_addr)}:{port}"
    pairing.save(cfg.state_dir, moved)
    machines.remember(cfg.state_dir, {
        "machine": {"name": name, "role": PRIMARY},
        "address": request.remote_addr or "",
        "version": str(request.headers.get("X-Ninaivu-Version", ""))[:40] or None})
    auth.audit(db.connect(cfg.db_path), None, "two_computers",
               f"the Primary moved to {name or request.remote_addr}")
    log.warning("The Primary moved to %s (%s); the computer it left no longer acts as one",
                name or "another computer", request.remote_addr)
    return jsonify({**answer, "name": _name(cfg)})


@mugil_peer_bp.post("/api/mugil/pair")
def mugil_pair_answer():
    """On the Secondary: the Primary proving it knows the code."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    hello = json_body()
    if not isinstance(hello, dict):
        return jsonify({"refused": "Send the pairing request as JSON.", "status": 400}), 400
    if pairing.load(cfg.state_dir) is not None:
        return jsonify({"refused": "This computer is paired already.", "status": 409}), 409
    if hello.get("tap"):
        accepted = _welcome().accept(str(hello.get("public", "")), str(hello.get("proof", "")))
        if accepted is None:
            return jsonify({"refused": "This computer is not waiting for that Primary.",
                            "status": 403}), 403
        key, met = accepted
        pairing.save(cfg.state_dir, {
            "key": key, "role": SECONDARY, "partner_name": met["name"],
            "partner_address": request.remote_addr or "", "number": met["number"],
            "paired_at": time.time()})
        auth.audit(db.connect(cfg.db_path), None, "two_computers",
                   f"paired with {request.remote_addr} ({met['number']})")
        return jsonify({"name": _name(cfg)})
    found = pairing.secondary_answer(_codes, hello)
    if found is None:
        return jsonify({"refused": "That code is not the one this computer is showing, or it has expired.",
                        "status": 403}), 403
    key, answer = found
    pairing.save(cfg.state_dir, {
        "key": key, "role": SECONDARY, "partner_name": str(hello.get("name") or "")[:200],
        "partner_address": request.remote_addr or "", "paired_at": time.time()})
    auth.audit(db.connect(cfg.db_path), None, "two_computers",
               f"paired with {request.remote_addr}")
    return jsonify({**answer, "name": _name(cfg)})


@mugil_bp.get("/api/admin/mugil/checklist")
@require_admin
def mugil_checklist():
    """The Mugil checklist, read from what is set up now (mugil/checklist.py)."""
    from ..mugil import checklist                                # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) == SINGLE:
        return _not_here()
    return jsonify(checklist.checklist(cfg, db.connect(cfg.db_path),
                                       current_app.config["MV_SERVICES"]))


@mugil_bp.get("/api/admin/mugil/machines")
@require_admin
def mugil_machines():
    """The two computers side by side; the Primary asks the Secondary afresh."""
    cfg = _cfg()
    part = role(cfg)
    pair = pairing.load(cfg.state_dir)
    other = None
    online = None
    if part == PRIMARY and pair is not None and cfg.mugil_partner_url:
        try:
            hello = Partner(cfg.mugil_partner_url, pair["key"], timeout=5).request(
                "GET", "/api/mugil/hello")
            machines.remember_hello(cfg.state_dir, hello)
            online = True
        except PartnerError:
            online = False
    if pair is not None:
        other = machines.remembered(cfg.state_dir)
    mine = machines.this_machine(cfg)
    theirs = (other or {}).get("version") or ((other or {}).get("machine") or {}).get("version")
    return jsonify({"this": mine, "paired": pair is not None, "other": other, "online": online,
                    "same_version": machines.same_version(mine["version"], theirs)})


@mugil_bp.get("/api/admin/mugil/optimize")
@require_admin
def mugil_optimize():
    """Which originals the Primary could let go, and why the rest stay.
    Only reads (mugil/optimize.py); nothing is freed."""
    from ..mugil import optimize                                 # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != PRIMARY:
        return _not_here()
    conn = db.connect(cfg.db_path)
    try:
        return jsonify(optimize.plan(conn, cfg.state_dir, list(cfg.roots)))
    finally:
        conn.close()


def _freeing():
    return getattr(current_app.config["MV_SERVICES"], "mugil_free", None)


@mugil_bp.get("/api/admin/mugil/free")
@require_admin
def mugil_free_status():
    cfg = _cfg()
    if role(cfg) != PRIMARY or _freeing() is None:
        return _not_here()
    return jsonify(_freeing().status())


@mugil_bp.post("/api/admin/mugil/free")
@require_admin
def mugil_free_start():
    """Let go the originals the plan calls ready, each checked again on both
    computers first (mugil/free.py). Only with the switch on, and only when
    the request says it means it."""
    cfg = _cfg()
    if role(cfg) != PRIMARY or _freeing() is None:
        return _not_here()
    data = json_body()
    if not isinstance(data, dict) or data.get("confirm") is not True:
        return jsonify({**refusal("Say that you mean it: the originals will be on the Secondary only."),
                        "status": 400}), 400
    try:
        answer = _freeing().start()
    except PartnerError as exc:
        return jsonify({**refusal(str(exc)), "status": exc.status or 409}), exc.status or 409
    _audit("freeing space: letting go originals the Secondary keeps")
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/free/stop")
@require_admin
def mugil_free_stop():
    cfg = _cfg()
    if role(cfg) != PRIMARY or _freeing() is None:
        return _not_here()
    _freeing().stop()
    return jsonify(_freeing().status())


@mugil_bp.post("/api/admin/mugil/bring-back")
@require_admin
def mugil_bring_back():
    """Put let-go originals back on this computer, from the Secondary: one
    (root and path), or every one."""
    from ..mugil import free                                     # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != PRIMARY:
        return _not_here()
    data = json_body()
    if not isinstance(data, dict) or not (data.get("all") is True or data.get("path")):
        return jsonify({**refusal("Send a root and a path, or all."), "status": 400}), 400
    conn = db.connect(cfg.db_path)
    wanted = free.let_go(conn) if data.get("all") is True else [
        {"root": str(data.get("root", "")), "rel_path": str(data["path"])}]
    back = missed = 0
    for item in wanted:
        if free.bring_back(cfg, conn, item["root"], item["rel_path"]):
            back += 1
        else:
            missed += 1
    left = free.summary(conn)
    _audit(f"brought back {back} originals from the Secondary")
    return jsonify({"back": back, "missed": missed, "let_go": left})


def _rebuild():
    return getattr(current_app.config["MV_SERVICES"], "mugil_rebuild", None)


@mugil_bp.get("/api/admin/mugil/rebuild")
@require_admin
def mugil_rebuild_status():
    """The libraries the Secondary keeps, to rebuild here, and how a rebuild
    is going (mugil/rebuild.py)."""
    from ..mugil import rebuild                                  # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != PRIMARY or _rebuild() is None:
        return _not_here()
    answer: dict = {**_rebuild().status(), "libraries": [], "away": False}
    if pairing.load(cfg.state_dir) is not None and cfg.mugil_partner_url:
        try:
            answer["libraries"] = rebuild.libraries(cfg)
        except PartnerError:
            answer["away"] = True
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/rebuild")
@require_admin
def mugil_rebuild_start():
    """Copy one library's originals back from the Secondary into a folder
    here: an empty one, or the library's own."""
    from .api_library import _can_be_library, _root_refusal      # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != PRIMARY or _rebuild() is None:
        return _not_here()
    data = json_body()
    if not isinstance(data, dict) or not data.get("library") or not data.get("folder"):
        return jsonify({**refusal("Send the library and the folder."), "status": 400}), 400
    folder = Path(str(data["folder"])).expanduser()
    if folder.is_dir() and not _can_be_library(folder.resolve(), cfg):
        return jsonify({"error": _root_refusal(folder, cfg), "status": 403}), 403
    try:
        answer = _rebuild().start(str(data["library"]), str(folder))
    except PartnerError as exc:
        return jsonify({**refusal(str(exc)), "status": exc.status or 409}), exc.status or 409
    _audit(f"rebuilding a library from the Secondary into {folder}")
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/rebuild/stop")
@require_admin
def mugil_rebuild_stop():
    cfg = _cfg()
    if role(cfg) != PRIMARY or _rebuild() is None:
        return _not_here()
    _rebuild().stop()
    return jsonify(_rebuild().status())


# -- the files only the Secondary has (mugil/survey.py, mugil/bring_in.py) -------

def _bring_in():
    return getattr(current_app.config["MV_SERVICES"], "mugil_bring_in", None)


def _survey(cfg):
    """The Secondary's look for its own files, one per originals folder."""
    from ..mugil.survey import Survey                            # noqa: PLC0415
    services = current_app.config["MV_SERVICES"]
    holding = _holding(cfg)
    if holding is None:
        return None
    current = getattr(services, "mugil_survey", None)
    if current is None or current.folder != holding.folder:
        current = Survey(cfg.state_dir, holding.folder)
        services.mugil_survey = current
    return current


def _only_answer(cfg) -> dict:
    """What the Secondary has that this computer does not, and bringing it in."""
    from ..mugil import bring_in                                 # noqa: PLC0415
    courier = _courier()
    answer: dict = {"bring": _bring_in().status(), "found": None, "away": False,
                    "why_not": bring_in.ready(cfg, courier)}
    if pairing.load(cfg.state_dir) is not None and cfg.mugil_partner_url:
        try:
            answer["found"] = _bring_in().found()
        except PartnerError:
            answer["away"] = True
    last = (courier.status().get("last") or {}) if courier is not None else {}
    asked = _bring_in().asked_at
    found = answer["found"] or {}
    # Looked at after the last complete run, and finished: what it found
    # holds now. Before that, a file still to be sent could look like the
    # Secondary's own.
    answer["looked_since_run"] = bool(asked and asked >= float(last.get("finished") or 0))
    answer["can_bring"] = bool(
        answer["why_not"] is None and answer["looked_since_run"] and found.get("finished")
        and not found.get("running") and not found.get("error")
        and not _bring_in().running)
    return answer


@mugil_bp.get("/api/admin/mugil/only-on-secondary")
@require_admin
def mugil_only_status():
    cfg = _cfg()
    if role(cfg) != PRIMARY or _bring_in() is None:
        return _not_here()
    return jsonify(_only_answer(cfg))


@mugil_bp.post("/api/admin/mugil/only-on-secondary/look")
@require_admin
def mugil_only_look():
    from ..mugil import bring_in                                 # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != PRIMARY or _bring_in() is None:
        return _not_here()
    why = bring_in.ready(cfg, _courier())
    if why:
        return jsonify({**refusal(why), "status": 409}), 409
    try:
        found = _bring_in().look()
    except PartnerError as exc:
        return jsonify({**refusal(str(exc)), "status": exc.status or 409}), exc.status or 409
    return jsonify({"found": found})


@mugil_bp.post("/api/admin/mugil/only-on-secondary/bring")
@require_admin
def mugil_only_bring():
    cfg = _cfg()
    if role(cfg) != PRIMARY or _bring_in() is None:
        return _not_here()
    if not _only_answer(cfg).get("can_bring"):
        return jsonify({**refusal("Look on the Secondary first, after a complete run of "
                                  "sending the originals."), "status": 409}), 409
    try:
        answer = _bring_in().start()
    except PartnerError as exc:
        return jsonify({**refusal(str(exc)), "status": exc.status or 409}), exc.status or 409
    _audit("bringing in the files only the Secondary has")
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/only-on-secondary/stop")
@require_admin
def mugil_only_stop():
    cfg = _cfg()
    if role(cfg) != PRIMARY or _bring_in() is None:
        return _not_here()
    _bring_in().stop()
    return jsonify(_bring_in().status())


@mugil_peer_bp.post("/api/mugil/survey")
def mugil_survey_start():
    """On the Secondary: look for the files the Primary has not named."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(read_at_most(JSON_MAX_BYTES))) is None:
        return _unsigned()
    survey = _survey(cfg)
    if survey is None or folder_problem(str(survey.folder), list(cfg.roots), cfg.state_dir):
        return jsonify({"refused": "This computer has no originals folder it can read.",
                        "status": 503}), 503
    return jsonify(survey.start())


@mugil_peer_bp.get("/api/mugil/survey")
def mugil_survey_status():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    survey = _survey(cfg)
    if survey is None:
        return jsonify({"refused": "This computer has no originals folder it can read.",
                        "status": 503}), 503
    return jsonify(survey.status())


@mugil_peer_bp.get("/api/mugil/survey/items")
def mugil_survey_items():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    survey = _survey(cfg)
    if survey is None or survey.running:
        return jsonify({"refused": "The look for this computer's own files is not finished.",
                        "status": 409}), 409
    kinds = tuple(k for k in str(request.args.get("kinds", "")).split(",") if k)
    try:
        limit = int(request.args.get("limit", "500"))
    except ValueError:
        limit = 500
    library = request.args.get("library")
    return jsonify({"items": survey.items(kinds or ("only", "other_version"), library,
                                          request.args.get("after", ""), limit)})


@mugil_peer_bp.get("/api/mugil/survey/file")
def mugil_survey_file():
    """On the Secondary: one of its own files, as it is on the disk."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    survey = _survey(cfg)
    found = survey.file(request.args.get("path", "")) if survey is not None else None
    if found is None:
        return jsonify({"refused": "That file is not here as it was found.", "status": 404}), 404
    path, row = found
    response = send_file(path, mimetype="application/octet-stream", conditional=False,
                         max_age=0)
    response.headers["X-Mugil-Digest"] = f"{row['algo']}:{row['digest']}"
    response.headers["Cache-Control"] = "no-store"
    return response


@mugil_peer_bp.post("/api/mugil/introduce")
def mugil_introduce():
    """On the Secondary: a Primary that found it, giving its public half."""
    cfg = _cfg()
    if role(cfg) != SECONDARY or pairing.load(cfg.state_dir) is not None:
        return _not_here()
    hello = json_body()
    if not isinstance(hello, dict):
        return jsonify({"refused": "Send the introduction as JSON.", "status": 400}), 400
    _, ca = _own_tls(cfg)
    answer = _welcome().introduce(str(hello.get("public", "")), str(hello.get("name", "")),
                                  request.remote_addr or "", pairing.ca_fingerprint(ca))
    if answer is None:
        return jsonify({"refused": "This computer is not waiting for a Primary.", "status": 403}), 403
    return jsonify({**answer, "name": _name(cfg)})


@mugil_peer_bp.get("/api/mugil/hello")
def mugil_hello():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    # The originals disk mounted at another path, or moved to a computer with
    # another system: found again by its id before saying it is away.
    from ..mugil.hold import find_moved                          # noqa: PLC0415
    find_moved(cfg)
    holding = _holding(cfg)
    problem = (folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir)
               if holding is not None else "no folder")
    keeping = holding.summary() if holding is not None and problem is None else {}
    return jsonify({"role": SECONDARY, "name": _name(cfg), "version": __version__,
                    "originals_ready": problem is None, "keeping": keeping,
                    "machine": machines.this_machine(cfg),
                    # Where phones reach its phone inbox (mugil/inbox.py).
                    "family_port": int(getattr(cfg, "port", 0) or 0),
                    "health": machines.keeping_health(holding, keeping)
                    if holding is not None else {},
                    # Whether it sends the off-site copy itself (cloud/offsite.py).
                    "offsite": machines.offsite_summary(
                        getattr(current_app.config["MV_SERVICES"], "offsite", None)),
                    # How the originals first arrive (mugil/adopt.py): the
                    # Primary waits while a copy made by hand is not ready.
                    "copy_mode": str(getattr(cfg, "mugil_copy_mode", "automatic") or "automatic"),
                    "manual": _manual_brief(cfg)})


def _manual_brief(cfg) -> dict:
    from ..mugil import adopt                                    # noqa: PLC0415
    if not adopt.manual(cfg):
        return {"state": adopt.READY}
    status = _adopter().status() if _adopter() is not None else {"state": adopt.state(cfg)}
    return {"state": status.get("state"), "progress": status.get("progress") or {}}


@mugil_peer_bp.post("/api/mugil/have")
def mugil_have():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    holding = _holding(cfg)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or holding is None:
        return jsonify({"refused": "Send the library and its items.", "status": 400}), 400
    if folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir):
        # Asked of an empty folder where the disk was, the answer would be
        # "none of them", and every original would be sent again onto it.
        return jsonify({"refused": "This computer has no originals folder it can read.",
                        "status": 503}), 503
    from ..mugil import adopt                                    # noqa: PLC0415
    if adopt.state(cfg) != adopt.READY:
        # A copy made by hand that is not checked yet: everything would look
        # missing, and be sent again over the network.
        return jsonify({"refused": "The Secondary is still taking in a copy made by hand.",
                        "status": 425}), 425
    items = [i for i in data["items"] if isinstance(i, dict)]
    return jsonify({"have": holding.have(str(data.get("library", "")), items,
                                         name=str(data.get("name", ""))),
                    "elsewhere": holding.kept_elsewhere(items)})


@mugil_peer_bp.post("/api/mugil/originals/copy")
def mugil_copy_kept():
    """On the Secondary: keep an original by copying the same content kept
    here at another path, instead of having it sent again."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        data = json.loads(raw)
        size_n, mtime_f = int(data["size"]), float(data["mtime"])
    except (ValueError, TypeError, KeyError):
        return jsonify({"refused": "The file is not described.", "status": 400}), 400
    holding = _holding(cfg)
    problem = (folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir)
               if holding is not None else "no folder")
    if problem is not None:
        return jsonify({"refused": "This computer has no originals folder it can write to.",
                        "status": 503}), 503
    try:
        kept = holding.receive_from_kept(
            str(data.get("library", "")), str(data.get("name", "")), str(data.get("path", "")),
            size=size_n, algo=str(data.get("algo", "")), digest=str(data.get("digest", "")),
            mtime=mtime_f)
    except Refused as exc:
        status, said = REASONS.get(exc.why, (400, "The file was not kept."))
        return jsonify({"refused": said, "status": status}), status
    return jsonify(kept)


@mugil_peer_bp.post("/api/mugil/originals/verify")
def mugil_verify():
    """On the Secondary: read a kept original again, end to end, and say
    whether it is the one the Primary describes."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        data = json.loads(raw)
        size_n = int(data["size"])
    except (ValueError, TypeError, KeyError):
        return jsonify({"refused": "The file is not described.", "status": 400}), 400
    holding = _holding(cfg)
    if holding is None or folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir):
        return jsonify({"refused": "This computer has no originals folder it can read.",
                        "status": 503}), 503
    same = holding.same(str(data.get("library", "")), str(data.get("path", "")), size=size_n,
                        algo=str(data.get("algo", "")), digest=str(data.get("digest", "")))
    return jsonify({"same": same})


#: The most files one test restore asks the Secondary to read back.
READ_BACK_MOST = 10


@mugil_peer_bp.post("/api/mugil/originals/read-back")
def mugil_read_back():
    """On the Secondary: read a few kept originals again, end to end, and
    report the fingerprint of what was read, for the Primary's test restore
    (copies.SecondaryDestination.restore_test). Changes nothing."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        items = json.loads(raw)["items"]
    except (ValueError, TypeError, KeyError):
        items = None
    if not isinstance(items, list) or len(items) > READ_BACK_MOST:
        return jsonify({"refused": f"Name at most {READ_BACK_MOST} files.", "status": 400}), 400
    holding = _holding(cfg)
    if holding is None or folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir):
        return jsonify({"refused": "This computer has no originals folder it can read.",
                        "status": 503}), 503
    read = []
    for item in items:
        item = item if isinstance(item, dict) else {}
        library, rel = str(item.get("library", "")), str(item.get("path", ""))
        read.append({"library": library, "path": rel, **(holding.read_back(library, rel)
                                                          or {"missing": True})})
    return jsonify({"read": read})


def _inbox(cfg):
    from ..mugil.inbox import Inbox                              # noqa: PLC0415
    holding = _holding(cfg)
    if holding is None or folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir):
        return None
    return Inbox(holding.folder)


def _no_inbox():
    return jsonify({"refused": "This computer has no originals folder it can write to.",
                    "status": 503}), 503


@mugil_peer_bp.put("/api/mugil/inbox/keys")
def mugil_inbox_keys():
    """On the Secondary: the Primary's phone keys, as hashes, for the phone
    inbox to sign phones in with while the Primary is away."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        keys = json.loads(raw)["keys"]
    except (ValueError, TypeError, KeyError):
        keys = None
    if not isinstance(keys, list):
        return jsonify({"refused": "Send the keys.", "status": 400}), 400
    inbox = _inbox(cfg)
    if inbox is None:
        return _no_inbox()
    return jsonify({"keys": inbox.set_keys(k for k in keys if isinstance(k, dict))})


@mugil_peer_bp.get("/api/mugil/inbox")
def mugil_inbox_list():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    inbox = _inbox(cfg)
    if inbox is None:
        return _no_inbox()
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        after = 0
    return jsonify({"items": inbox.waiting(after=after)})


@mugil_peer_bp.get("/api/mugil/inbox/file")
def mugil_inbox_file():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    inbox = _inbox(cfg)
    if inbox is None:
        return _no_inbox()
    try:
        found = inbox.file_of(int(request.args.get("id", "")))
    except ValueError:
        found = None
    if found is None:
        return jsonify({"refused": "That file is not waiting here.", "status": 404}), 404
    response = send_file(found[0], mimetype="application/octet-stream", conditional=False,
                         max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


@mugil_peer_bp.post("/api/mugil/inbox/taken")
def mugil_inbox_taken():
    """On the Secondary: the Primary has filed these, so the copies here go."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        ids = [int(i) for i in json.loads(raw)["ids"]]
    except (ValueError, TypeError, KeyError):
        return jsonify({"refused": "Send the ids.", "status": 400}), 400
    inbox = _inbox(cfg)
    if inbox is None:
        return _no_inbox()
    return jsonify({"taken": inbox.taken(ids)})


@mugil_peer_bp.get("/api/mugil/kept")
def mugil_kept_list():
    """On the Secondary: the originals kept of one library, a page at a time,
    for a new Primary rebuilding it (mugil/rebuild.py)."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    holding = _holding(cfg)
    if holding is None or folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir):
        return jsonify({"refused": "This computer has no originals folder it can read.",
                        "status": 503}), 503
    try:
        limit = int(request.args.get("limit", "500"))
    except ValueError:
        limit = 500
    return jsonify({"items": holding.kept_page(request.args.get("library", ""),
                                               request.args.get("after", ""), limit)})


@mugil_peer_bp.put("/api/mugil/wake")
def mugil_wake_asked():
    """On the Secondary: how to wake the Primary, and when (mugil/wake.py)."""
    from ..mugil import wake                                     # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        asked = json.loads(raw)
    except ValueError:
        asked = None
    if not isinstance(asked, dict):
        return jsonify({"refused": "Send the addresses and the time.", "status": 400}), 400
    kept = wake.remember(cfg.state_dir, asked)
    return jsonify({"macs": len(kept["macs"]), "at": kept["at"]})


@mugil_bp.post("/api/admin/mugil/wake")
@require_admin
def mugil_wake_now():
    """On the Secondary: wake the Primary now."""
    from ..mugil import wake                                     # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY or pairing.load(cfg.state_dir) is None:
        return _not_here()
    if not wake.load(cfg.state_dir).get("macs"):
        return jsonify({**refusal("The Primary has not said how to wake it yet. It does on its next run."),
                        "status": 409}), 409
    sent = wake.wake_now(cfg.state_dir)
    _audit("woke the Primary")
    return jsonify({"sent": sent, "wake": wake.load(cfg.state_dir)})


@mugil_peer_bp.get("/api/mugil/originals")
def mugil_give():
    """On the Secondary: an original the Primary sends for, as it is kept."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    holding = _holding(cfg)
    found = holding.kept_file(request.args.get("library", ""), request.args.get("path", ""),
                              request.args.get("digest", "")) if holding is not None else None
    if found is None:
        return jsonify({"refused": "This computer does not keep that original.", "status": 404}), 404
    path, kept = found
    response = send_file(path, mimetype="application/octet-stream", conditional=False,
                         max_age=0)
    response.headers["X-Mugil-Digest"] = f"{kept['algo']}:{kept['digest']}"
    response.headers["Cache-Control"] = "no-store"
    return response


@mugil_peer_bp.put("/api/mugil/handover")
def mugil_handover_receive():
    """On the Secondary: one file of the family app's copy, kept if whole."""
    from ..mugil import handover                                 # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    vouched = _signed()
    if vouched is None:
        return _unsigned()
    try:
        algo, digest, size = vouched.split(":", 2)
        size_n = int(size)
    except ValueError:
        return jsonify({"refused": "The file is not described.", "status": 400}), 400
    if request.content_length != size_n:
        return jsonify({"refused": "The file is not the size it was said to be.", "status": 400}), 400
    # One byte over, as for an original (mugil_receive): the reader asks for
    # one more to see the file ends where it was said to.
    request.max_content_length = size_n + 1
    try:
        kept = handover.receive(cfg.state_dir, request.args.get("path", ""), algo=algo,
                                digest=digest, size=size_n, body=request.stream)
    except ValueError:
        return jsonify({"refused": "That is not a file the family app's copy holds, or it "
                                   "did not arrive whole.", "status": 400}), 400
    except OSError:
        return jsonify({"refused": "The file could not be written here.", "status": 507}), 507
    return jsonify({"kept": kept})


@mugil_peer_bp.post("/api/mugil/handover/remove")
def mugil_handover_remove():
    from ..mugil import handover                                 # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        paths = json.loads(raw).get("paths")
    except (ValueError, AttributeError):
        paths = None
    if not isinstance(paths, list):
        return jsonify({"refused": "Send the paths.", "status": 400}), 400
    return jsonify({"removed": handover.remove(cfg.state_dir, [str(p) for p in paths])})


@mugil_peer_bp.post("/api/mugil/handover/withdraw")
def mugil_handover_withdraw():
    """On the Secondary: the family view was unticked on the Primary. Stop
    serving it and remove the hand-over and the view's copy of it; the
    originals are not touched (mugil/family_view.py)."""
    from ..mugil import handover                                 # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    view = getattr(current_app.config.get("MV_SERVICES"), "mugil_family_view", None)
    if view is not None:
        return jsonify({"removed": view.withdraw()})
    return jsonify({"removed": handover.clear_copy(cfg.state_dir)})


@mugil_peer_bp.put("/api/mugil/originals")
def mugil_receive():
    """On the Secondary: one original, kept only if it is the one named."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    vouched = _signed()
    if vouched is None:
        return _unsigned()
    try:
        algo, digest, size, mtime = vouched.split(":", 3)
        size_n, mtime_f = int(size), float(mtime)
    except ValueError:
        return jsonify({"refused": "The file is not described.", "status": 400}), 400
    if request.content_length != size_n:
        return jsonify({"refused": "The file is not the size it was said to be.", "status": 400}), 400
    holding = _holding(cfg)
    problem = (folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir)
               if holding is not None else "no folder")
    if problem is not None:
        return jsonify({"refused": "This computer has no originals folder it can write to.",
                        "status": 503}), 503
    # A video is bigger than any upload the family app takes; this is the one
    # route that reads a body as large as the file, and only when signed.
    # (None would fall back to the app's limit: Flask reads it that way.)
    # One byte over the file: holding.receive asks for one more to be sure
    # nothing follows it, and waitress (which ends the body itself) leaves
    # Flask's limit as the only one on the stream, so at exactly the size that
    # last read was refused as too large and no original was ever kept.
    request.max_content_length = size_n + 1
    try:
        kept = holding.receive(request.args.get("library", ""), request.args.get("name", ""),
                               request.args.get("path", ""), size=size_n, algo=algo,
                               digest=digest, mtime=mtime_f, body=request.stream)
    except Refused as exc:
        status, said = REASONS.get(exc.why, (400, "The file was not kept."))
        return jsonify({"refused": said, "status": status}), status
    return jsonify(kept)


@mugil_peer_bp.post("/api/mugil/join")
def mugil_join_answer():
    """On the Primary: a Secondary proving it knows the code this console
    shows (:func:`mugil_join`)."""
    cfg = _cfg()
    if role(cfg) != PRIMARY:
        return _not_here()
    hello = json_body()
    if not isinstance(hello, dict):
        return jsonify({"refused": "Send the pairing request as JSON.", "status": 400}), 400
    if pairing.load(cfg.state_dir) is not None:
        return jsonify({"refused": "This computer is paired already.", "status": 409}), 409
    try:
        port = int(hello.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        return jsonify({"refused": "Say which port the Secondary answers on.", "status": 400}), 400
    found = pairing.secondary_answer(_codes, hello)
    if found is None:
        return jsonify({"refused": "That code is not the one this computer is showing, or it has expired.",
                        "status": 403}), 403
    key, answer = found
    # The address it came from: the one this computer can reach it at, in
    # the scheme its family app speaks. Its authority was in the proof.
    scheme = "https" if hello.get("https") else "http"
    url = f"{scheme}://{_host(request.remote_addr or '')}:{port}"
    ca = hello.get("ca") if hello.get("https") and pairing.ca_fingerprint(hello.get("ca")) else None
    pairing.save(cfg.state_dir, {
        "key": key, "role": PRIMARY, "partner_name": str(hello.get("name") or "")[:200],
        "partner_url": url, "paired_at": time.time(), **({"partner_ca": ca} if ca else {})})
    cfg.mugil_partner_url = url
    cfg.save()
    auth.audit(db.connect(cfg.db_path), None, "two_computers",
               f"paired with the Secondary at {url}, with the code shown here")
    _start_sending()
    return jsonify({**answer, "name": _name(cfg), "version": __version__})


@mugil_peer_bp.put("/api/mugil/about-primary")
def mugil_about_primary():
    """On the Secondary: what the Primary says of itself at each run (its
    libraries and their sizes, its version, its kind of fingerprint), so this
    console can say where a copy made by hand goes and whether it fits."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    raw = read_at_most(JSON_MAX_BYTES)
    if _signed(pairing.body_digest(raw)) is None:
        return _unsigned()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return jsonify({"refused": "Send it as JSON.", "status": 400}), 400
    libraries = [{"name": str(lib.get("name") or "")[:200], "root": str(lib.get("root") or "")[:1000],
                  "files": int(lib.get("files") or 0), "bytes": int(lib.get("bytes") or 0)}
                 for lib in (data.get("libraries") or [])[:50] if isinstance(lib, dict)]
    algo = str(data.get("algo") or "")[:20]
    machines.remember_primary(cfg.state_dir, {"libraries": libraries, "algo": algo,
                                              "version": str(data.get("version") or "")[:40]})
    if algo:
        from ..mugil import adopt                                # noqa: PLC0415
        adopt.remember_algo(cfg.state_dir, algo)
    return jsonify({"ok": True})


# -- the census, the plan and the proof (mugil/topology.py) ---------------------

def _topology():
    """The census, plan and proof of this computer, one for the life of the
    process; made when first asked, so a computer that never uses them pays
    nothing."""
    from ..mugil.topology import Topology                       # noqa: PLC0415
    services = current_app.config["MV_SERVICES"]
    current = getattr(services, "mugil_topology", None)
    if current is None:
        cfg = _cfg()
        # Partner looked up when asked, so it is the one this module has then.
        current = Topology(cfg, lambda: db.connect(cfg.db_path),
                           partner=lambda *a, **k: Partner(*a, **k))
        services.mugil_topology = current
    return current


def _refused(exc: Exception):
    return jsonify({**refusal(str(exc)), "status": 409}), 409


@mugil_bp.get("/api/admin/mugil/topology")
@require_admin
def mugil_topology_status():
    """Where this computer stands, its censuses, and the change under way:
    its plan's totals and its proof."""
    return jsonify(_topology().status())


@mugil_bp.post("/api/admin/mugil/census")
@require_admin
def mugil_census_take():
    """Take a census of this computer, on any computer, single too: the
    one taken before pairing is what the proof compares against."""
    from ..mugil.topology import Refused                         # noqa: PLC0415
    data = json_body()
    data = data if isinstance(data, dict) else {}
    try:
        answer = _topology().take_census(read_all=data.get("read_all") is True)
    except Refused as exc:
        return _refused(exc)
    _audit("census taken")
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/topology/plan")
@require_admin
def mugil_topology_plan():
    """On the Primary: plan a change (``{"transition": "pair"}``)."""
    from ..mugil.topology import Refused                         # noqa: PLC0415
    data = json_body()
    if not isinstance(data, dict) or not isinstance(data.get("transition"), str):
        return jsonify({**refusal("Say which change to plan."), "status": 400}), 400
    try:
        answer = _topology().plan(data["transition"], data.get("part") or None)
    except Refused as exc:
        return _refused(exc)
    _audit(f"plan for {data['transition']} started")
    return jsonify(answer)


@mugil_bp.get("/api/admin/mugil/topology/plan")
@require_admin
def mugil_topology_plan_read():
    """A page of the plan: ``outcome``, ``on`` (primary or secondary),
    ``after`` (a line number) and ``limit``."""
    try:
        after = max(0, int(request.args.get("after", "0")))
        limit = max(1, min(int(request.args.get("limit", "500")), 2000))
    except ValueError:
        return jsonify({**refusal("Give whole numbers."), "status": 400}), 400
    return jsonify(_topology().plan_page(outcome=request.args.get("outcome") or None,
                                         side=request.args.get("on") or None,
                                         after=after, limit=limit))


@mugil_bp.post("/api/admin/mugil/topology/prove")
@require_admin
def mugil_topology_prove():
    """On the Primary: take both censuses again and prove nothing was lost."""
    from ..mugil.topology import Refused                         # noqa: PLC0415
    try:
        answer = _topology().prove()
    except Refused as exc:
        return _refused(exc)
    _audit("proof started")
    return jsonify(answer)


@mugil_peer_bp.post("/api/mugil/census")
def mugil_census_start():
    """On the Secondary: take a census, for the Primary's plan or proof."""
    from ..mugil.topology import Refused                         # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(read_at_most(JSON_MAX_BYTES))) is None:
        return _unsigned()
    try:
        return jsonify(_topology().take_census())
    except Refused as exc:
        return _refused(exc)


@mugil_peer_bp.get("/api/mugil/census")
def mugil_census_status():
    """On the Secondary: its censuses, and the one being taken."""
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    answer = _topology().status()
    answer.pop("record", None)
    return jsonify(answer)


@mugil_peer_bp.get("/api/mugil/census/file")
def mugil_census_file():
    """On the Secondary: one of its own censuses, as it is on the disk."""
    from ..mugil.topology import census_file                     # noqa: PLC0415
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    path = census_file(cfg.state_dir, request.args.get("name", ""))
    if path is None:
        return jsonify({"refused": "No census of that name is kept here.", "status": 404}), 404
    response = send_file(path, mimetype="application/gzip", conditional=False, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response
