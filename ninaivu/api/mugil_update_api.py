"""Two computers: the Primary sending the Secondary a newer Ninaivu.

``/api/admin/mugil/update`` is the console's, on the Primary: the person
gives it the Linux portable download of the version the Primary runs, and
it is sent on; asked, it says how the Secondary's update stands.
``/api/mugil/update`` is the Secondary's, spoken to only by the paired
Primary, signed like the originals it is sent (api/mugil_api.py). What is
done with the download is in mugil/update.py.
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, current_app, jsonify, request

from ..mugil import PRIMARY, SECONDARY, pairing, role, update
from ..mugil.courier import Partner, PartnerError
from ..server.auth import require_admin
from ..words import refusal
from .mugil_api import _audit, _cfg, _holding, _not_here, _only_from_home, _signed, _unsigned

log = logging.getLogger(__name__)

mugil_update_bp = Blueprint("mugil_update", __name__)
mugil_update_peer_bp = Blueprint("mugil_update_peer", __name__)
mugil_update_peer_bp.before_request(_only_from_home)


def _services():
    return current_app.config["MV_SERVICES"]


def _partner(cfg, timeout: float = 60) -> Partner:
    pair = pairing.load(cfg.state_dir)
    if pair is None or not cfg.mugil_partner_url:
        raise PartnerError("This computer is not paired with another.")
    return Partner(cfg.mugil_partner_url, pair["key"], timeout=timeout)


def _sender(cfg) -> update.Sender:
    services = _services()
    if getattr(services, "mugil_update_sender", None) is None:
        services.mugil_update_sender = update.Sender(cfg.state_dir, lambda: _partner(cfg))
    return services.mugil_update_sender


def _quiet(cfg) -> bool:
    """Between originals: none kept for a while, and no off-site copy going."""
    holding = _holding(cfg)
    try:
        last = holding.last_kept() if holding is not None else None
    except Exception:                                             # noqa: BLE001 - a ledger being written
        return False
    if last is not None and time.time() - last < update.QUIET:
        return False
    offsite = getattr(_services(), "offsite", None)
    return not (offsite is not None and getattr(offsite, "running", False))


def _updater(cfg) -> update.Updater:
    services = _services()
    if getattr(services, "mugil_updater", None) is None:
        services.mugil_updater = update.Updater(cfg.state_dir, quiet=lambda: _quiet(cfg))
    return services.mugil_updater


# -- the console, on the Primary -----------------------------------------------

@mugil_update_bp.get("/api/admin/mugil/update")
@require_admin
def mugil_update_status():
    cfg = _cfg()
    if role(cfg) != PRIMARY:
        return _not_here()
    answer = {"sending": _sender(cfg).status(), "secondary": None, "online": None}
    if pairing.load(cfg.state_dir) is not None and cfg.mugil_partner_url:
        try:
            answer["secondary"] = _partner(cfg, timeout=5).request("GET", "/api/mugil/update")
            answer["online"] = True
        except PartnerError:
            answer["online"] = False
    return jsonify(answer)


@mugil_update_bp.post("/api/admin/mugil/update")
@require_admin
def mugil_update_send():
    """The Linux portable download, as the request's body, to send on."""
    cfg = _cfg()
    if role(cfg) != PRIMARY or pairing.load(cfg.state_dir) is None:
        return jsonify({**refusal("Pair this computer with a Secondary first."), "status": 409}), 409
    size = request.content_length or 0
    if not 0 < size <= update.PACKAGE_MAX:
        return jsonify({**refusal("Choose the Linux portable download of Ninaivu."),
                        "status": 400}), 400
    # The download is larger than any upload the console takes otherwise.
    request.max_content_length = size + 1
    try:
        started = _sender(cfg).take(request.stream, size)
    except update.UpdateRefused as exc:
        return jsonify({"error": str(exc), "status": 400}), 400
    _audit(f"sending Ninaivu {started.get('to')} to the Secondary")
    return jsonify({"sending": started})


# -- the other computer, on the Secondary --------------------------------------

@mugil_update_peer_bp.put("/api/mugil/update")
def mugil_update_receive():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    vouched = _signed()
    if vouched is None:
        return _unsigned()
    size = request.content_length or 0
    request.max_content_length = size + 1
    try:
        answer = _updater(cfg).receive(request.stream, size, vouched)
    except update.UpdateRefused as exc:
        return jsonify({"refused": str(exc), "status": 409}), 409
    log.info("took a newer Ninaivu (%s) from the Primary", answer.get("to"))
    return jsonify(answer)


@mugil_update_peer_bp.get("/api/mugil/update")
def mugil_update_where():
    cfg = _cfg()
    if role(cfg) != SECONDARY:
        return _not_here()
    if _signed(pairing.body_digest(b"")) is None:
        return _unsigned()
    updater = _updater(cfg)
    return jsonify({**updater.status(), "can": updater.refusal() is None,
                    "why_not": updater.refusal()})
