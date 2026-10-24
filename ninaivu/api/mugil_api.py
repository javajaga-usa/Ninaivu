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
import time

from flask import Blueprint, current_app, jsonify, request

from .. import __version__
from ..mugil import PRIMARY, ROLES, SECONDARY, SINGLE, display_name, meeting, pairing, role
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


def _holding(cfg) -> Holding | None:
    folder = str(getattr(cfg, "mugil_originals_dir", "") or "")
    return Holding(folder) if folder else None


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
    }
    if part == PRIMARY:
        courier = _courier()
        answer["sending"] = courier.status() if courier is not None else {}
        answer["confirmed"] = {root: confirmed(cfg.state_dir, root) for root in cfg.roots}
    elif part == SECONDARY:
        holding = _holding(cfg)
        answer["keeping"] = holding.summary() if holding is not None else {}
        answer["welcome"] = _welcome().status() if pair is None else {"waiting": False}
        beacon = getattr(current_app.config["MV_SERVICES"], "mugil_beacon", None)
        answer["listening"] = meeting.on_the_network(cfg) and not getattr(beacon, "problem", None)
    if part != SINGLE:
        answer["on_the_network"] = meeting.on_the_network(cfg)
    return jsonify(answer)


@mugil_bp.post("/api/admin/mugil/role")
@require_admin
def mugil_role():
    """This computer's part, and the settings that go with it."""
    cfg = _cfg()
    data = json_body()
    if not isinstance(data, dict) or set(data) - {"role", "originals_dir", "copy_every_hours"}:
        return jsonify({**refusal("Send role, originals_dir or copy_every_hours."), "status": 400}), 400
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
    if wanted == SECONDARY and (folder or "originals_dir" in data) \
            and folder != (cfg.mugil_originals_dir or ""):
        problem = folder_problem(folder, list(cfg.roots), cfg.state_dir)
        if problem:
            return jsonify({"error": problem, "status": 400}), 400
    if wanted != PRIMARY and _courier() is not None:
        _courier().stop()
    if wanted == SECONDARY and role(cfg) != SECONDARY:
        # Choosing the part is what lets the Primary find and pair with it.
        _welcome().open()
    elif wanted != SECONDARY:
        _welcome().close()
    cfg.mugil_role, cfg.mugil_originals_dir = wanted, folder
    cfg.mugil_copy_every_hours = float(hours)
    cfg.save()
    _audit(f"this computer is {wanted}; originals folder {folder or '(none)'}")
    return mugil_status()


@mugil_bp.post("/api/admin/mugil/code")
@require_admin
def mugil_code():
    """On the Secondary: a code to type on the Primary, good for ten minutes."""
    if role(_cfg()) != SECONDARY:
        return jsonify({**refusal("Make this computer the Secondary first."), "status": 409}), 409
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
    return mugil_status()


def _pair_by_tap(cfg, ident: str):
    """The tap on a Secondary that was found and introduced to."""
    met = _introductions().get(ident)
    if met is None:
        return jsonify({**refusal("Look for the Secondary again: that one is no longer waiting."),
                        "status": 409}), 409
    key = pairing.tap_key(met["private"], met["theirs"], met["half"], met["theirs"])
    try:
        answer = Partner(met["url"], timeout=10).request(
            "POST", "/api/mugil/pair",
            body={"tap": True, "public": met["half"].hex(), "proof": pairing.tap_proof(key)},
            signed=False)
    except PartnerError as exc:
        log.warning("Pairing with %s did not work: %s", met["url"], exc)
        return jsonify({**refusal("Look for the Secondary again: that one is no longer waiting."),
                        "status": 409}), 409
    pairing.save(cfg.state_dir, {
        "key": key, "role": PRIMARY, "partner_name": str(answer.get("name") or met["name"]),
        "partner_url": met["url"], "number": met["number"], "paired_at": time.time()})
    _introductions().clear()
    cfg.mugil_partner_url = met["url"]
    cfg.save()
    _audit(f"paired with {met['url']} ({met['number']})")
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
            url = f"http://{_host(other['address'])}:{other['port']}"
            private, half = pairing.new_half()
            try:
                answer = Partner(url, timeout=5).request(
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
                                                       url, other["name"])
        found.append(item)
    return jsonify({"found": found})


def _host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


@mugil_bp.post("/api/admin/mugil/unpair")
@require_admin
def mugil_unpair():
    cfg = _cfg()
    if _courier() is not None:
        _courier().stop()
    pairing.forget(cfg.state_dir)
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
    if vouched is None or (digest_of_body is not None and vouched != digest_of_body):
        return None
    return vouched


def _unsigned():
    return jsonify({"refused": "This request is not signed by the computer this one is paired with.",
                    "status": 401}), 401


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


@mugil_peer_bp.post("/api/mugil/introduce")
def mugil_introduce():
    """On the Secondary: a Primary that found it, giving its public half."""
    cfg = _cfg()
    if role(cfg) != SECONDARY or pairing.load(cfg.state_dir) is not None:
        return _not_here()
    hello = json_body()
    if not isinstance(hello, dict):
        return jsonify({"refused": "Send the introduction as JSON.", "status": 400}), 400
    answer = _welcome().introduce(str(hello.get("public", "")), str(hello.get("name", "")),
                                  request.remote_addr or "")
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
    holding = _holding(cfg)
    problem = (folder_problem(str(holding.folder), list(cfg.roots), cfg.state_dir)
               if holding is not None else "no folder")
    return jsonify({"role": SECONDARY, "name": _name(cfg), "version": __version__,
                    "originals_ready": problem is None,
                    "keeping": holding.summary() if holding is not None and problem is None else {}})


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
    items = [i for i in data["items"] if isinstance(i, dict)]
    return jsonify({"have": holding.have(str(data.get("library", "")), items)})


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
    request.max_content_length = size_n
    try:
        kept = holding.receive(request.args.get("library", ""), request.args.get("name", ""),
                               request.args.get("path", ""), size=size_n, algo=algo,
                               digest=digest, mtime=mtime_f, body=request.stream)
    except Refused as exc:
        status, said = REASONS.get(exc.why, (400, "The file was not kept."))
        return jsonify({"refused": said, "status": status}), status
    return jsonify(kept)
