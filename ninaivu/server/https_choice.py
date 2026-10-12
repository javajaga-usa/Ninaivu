"""Whether Ninaivu serves over HTTP or HTTPS.

HTTP unless the administrator chose HTTPS. The choice lives in config.json
(``Config.https``: ``"on"`` or ``"off"``), is made on the console's Server
page, and is honoured at every start whoever started the server — the desktop
panel, start.py, a service manager or a container. Two things on the command
line still decide on their own, because they are deliberate: ``--cert``/
``--key`` (somebody's own certificate, so HTTPS) and ``--http``.

Until a choice is recorded, an install that has run on HTTPS before — it has
Ninaivu's own certificate in its state folder, or it was started with
``--https`` — keeps HTTPS, so the phones and bookmarks pointed at its
``https://`` addresses go on working after an update. Everything else starts
on HTTP. The first start writes down what it decided, so it never changes by
itself afterwards.
"""

from __future__ import annotations

from typing import Any

ON, OFF = "on", "off"


def saved(cfg: Any) -> bool | None:
    """The recorded choice, or None when there is none yet."""
    value = str(getattr(cfg, "https", "") or "").strip().lower()
    if value == ON:
        return True
    if value == OFF:
        return False
    return None


def has_own_certificate(cfg: Any) -> bool:
    """Whether Ninaivu's own CA is in the state folder: it has served HTTPS."""
    try:
        from ..utils import tls                                  # noqa: PLC0415
        return tls.ca_certificate_path(cfg.state_dir).is_file()
    except Exception:                                            # noqa: BLE001
        return False


def expected(cfg: Any) -> bool:
    """What the next ordinary start will do: the recorded choice, or for an
    install with none yet, whether it has served HTTPS before."""
    choice = saved(cfg)
    return has_own_certificate(cfg) if choice is None else choice


def decide(cfg: Any, *, https_flag: bool = False, http_flag: bool = False,
           cert: bool = False) -> tuple[bool, str | None]:
    """``(use_https, fixed_by)`` for this start, recording a first decision.

    *fixed_by* names what decided over the recorded choice — ``"cert"`` or
    ``"http-flag"`` — so the console can say why its switch does nothing;
    None when the recorded choice decided.
    """
    if cert:
        return True, "cert"
    if http_flag:
        return False, "http-flag"
    choice = saved(cfg)
    if choice is None:
        choice = bool(https_flag) or has_own_certificate(cfg)
        cfg.https = ON if choice else OFF
        try:
            cfg.save()
        except Exception:                                        # noqa: BLE001
            pass                    # decided again next start, the same way
    return choice, None


def port_for(port: int, use_https: bool) -> int:
    """The family app's port, moved to the new scheme's standard one when it
    was on the other's: a switch from HTTPS to HTTP leaves ``http://name``
    working, not ``http://name:443``."""
    if use_https and port == 80:
        return 443
    if not use_https and port == 443:
        return 80
    return port
