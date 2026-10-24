"""Introducing the two computers to each other, and signing what they say.

**The code.** The Secondary's console shows a code of eight letters and
digits (:meth:`Codes.new`), good for ten minutes and once. An administrator
types it on the Primary with the Secondary's address.

**The key.** Each side makes a fresh X25519 key pair and sends the public
half, with proof that it knows the code (an HMAC of the code over what it
sends). The shared secret the two work out from the halves, mixed with the
code, is the key. Somebody watching the network sees only the public halves
and learns nothing; somebody standing in the middle cannot prove they know a
code they were never shown. Five wrong proofs and every waiting code is void.

**The signature.** Every request after that carries the time and an
HMAC-SHA256 of the method, the path and query, the time, a random word of
its own, and the fingerprint of the body (:func:`sign`). The receiver refuses one more than five minutes
off its own clock, one whose signature it has already seen, and one whose
body does not match the fingerprint it was signed with.

The key is kept apart from the settings, in ``mugil-pair.json`` in the state
folder, readable only by the user Ninaivu runs as: settings are shown on the
Advanced page and copied into the index backup; the key is never shown.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..utils.files import atomic_write

#: Where the pairing is kept, in the state folder.
PAIR_FILE = "mugil-pair.json"

#: How long a code shown on the Secondary can be used, in seconds.
CODE_LIFE = 10 * 60

#: Wrong proofs before every waiting code is void.
CODE_TRIES = 5

#: Letters and digits that cannot be mistaken for each other when read out.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

#: How far a signed request's time may be from this computer's clock.
SKEW = 5 * 60

#: What the key is derived for, so it is never the same as any other use.
INFO = b"ninaivu-mugil-pairing-1"

TIME_HEADER = "X-Mugil-Time"
NONCE_HEADER = "X-Mugil-Nonce"
DIGEST_HEADER = "X-Mugil-Digest"
SIGNATURE_HEADER = "X-Mugil-Signature"


def normal_code(code: str) -> str:
    """The code as it is compared: upper case, without spaces or dashes."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _proof(code: str, *parts: bytes) -> str:
    return hmac.new(normal_code(code).encode(), b"\0".join(parts), hashlib.sha256).hexdigest()


# -- the code, on the Secondary ----------------------------------------------

class Codes:
    """The codes the Secondary's console has shown and not yet used up."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._waiting: dict[str, float] = {}
        self._wrong = 0

    def new(self) -> str:
        """A fresh code, shown as ``ABCD-EFGH``. One at a time: a new one
        replaces any still waiting."""
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        with self._lock:
            self._waiting = {code: self._clock() + CODE_LIFE}
            self._wrong = 0
        return f"{code[:4]}-{code[4:]}"

    def waiting(self) -> list[str]:
        now = self._clock()
        with self._lock:
            self._waiting = {c: t for c, t in self._waiting.items() if t > now}
            return list(self._waiting)

    def take(self, matches) -> str | None:
        """The waiting code *matches* accepts, used up; None, counted as a
        wrong try, when there is none."""
        for code in self.waiting():
            if matches(code):
                with self._lock:
                    self._waiting.pop(code, None)
                return code
        with self._lock:
            self._wrong += 1
            if self._wrong >= CODE_TRIES:
                self._waiting.clear()
        return None


# -- the key -----------------------------------------------------------------

def _raw(public: X25519PublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _derive(private: X25519PrivateKey, theirs: bytes, code: str,
            primary_half: bytes, secondary_half: bytes, info: bytes = INFO) -> str:
    shared = private.exchange(X25519PublicKey.from_public_bytes(theirs))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=primary_half + secondary_half,
                info=info).derive(shared + normal_code(code).encode()).hex()


# -- pairing with a tap, after the two found each other (mugil/meeting.py) -----

#: What a key agreed with a tap is derived for: never the same as a code's.
TAP_INFO = b"ninaivu-mugil-tap-1"


def new_half() -> tuple[X25519PrivateKey, bytes]:
    private = X25519PrivateKey.generate()
    return private, _raw(private.public_key())


def match_number(primary_half: bytes, secondary_half: bytes) -> str:
    """Six digits both screens show, worked out from the two public halves.

    A device pretending to be one of the two has halves of its own, so the
    numbers on the two real screens would not agree."""
    digest = hashlib.sha256(b"ninaivu-mugil-match\0" + primary_half + secondary_half).digest()
    number = int.from_bytes(digest[:8], "big") % 1_000_000
    return f"{number // 1000:03d} {number % 1000:03d}"


def tap_key(private: X25519PrivateKey, theirs: bytes, primary_half: bytes,
            secondary_half: bytes) -> str:
    return _derive(private, theirs, "", primary_half, secondary_half, TAP_INFO)


def tap_proof(key: str) -> str:
    """The Primary's proof, sent with the tap, that it holds the key."""
    return hmac.new(bytes.fromhex(key), b"the primary taps pair", hashlib.sha256).hexdigest()


def primary_hello(code: str) -> tuple[X25519PrivateKey, dict[str, str]]:
    """What the Primary sends: its public half and its proof of the code."""
    private = X25519PrivateKey.generate()
    half = _raw(private.public_key())
    return private, {"public": half.hex(), "proof": _proof(code, b"primary", half)}


def secondary_answer(code_book: Codes, hello: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    """On the Secondary: the key and the answer to send back, or None when
    the Primary did not prove it knows a waiting code."""
    try:
        theirs = bytes.fromhex(str(hello.get("public", "")))
        proof = str(hello.get("proof", ""))
    except ValueError:
        return None
    if len(theirs) != 32:
        return None
    code = code_book.take(lambda c: hmac.compare_digest(_proof(c, b"primary", theirs), proof))
    if code is None:
        return None
    private = X25519PrivateKey.generate()
    half = _raw(private.public_key())
    key = _derive(private, theirs, code, theirs, half)
    return key, {"public": half.hex(), "proof": _proof(code, b"secondary", half, theirs)}


def primary_key(private: X25519PrivateKey, code: str, answer: dict[str, Any]) -> str | None:
    """On the Primary: the key, or None when the answer did not come from a
    computer that knew the code."""
    mine = _raw(private.public_key())
    try:
        theirs = bytes.fromhex(str(answer.get("public", "")))
    except ValueError:
        return None
    if len(theirs) != 32 or not hmac.compare_digest(
            _proof(code, b"secondary", theirs, mine), str(answer.get("proof", ""))):
        return None
    return _derive(private, theirs, code, mine, theirs)


# -- keeping the pairing -----------------------------------------------------

def load(state_dir: str | Path) -> dict[str, Any] | None:
    """The pairing this computer is in, or None."""
    try:
        data = json.loads((Path(state_dir) / PAIR_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("key") else None


def save(state_dir: str | Path, data: dict[str, Any]) -> None:
    path = Path(state_dir) / PAIR_FILE
    atomic_write(path, json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:                                     # pragma: no cover - Windows
        pass


def forget(state_dir: str | Path) -> None:
    (Path(state_dir) / PAIR_FILE).unlink(missing_ok=True)


def public(data: dict[str, Any] | None) -> dict[str, Any]:
    """What the console may show of a pairing: everything but the key."""
    return {k: v for k, v in (data or {}).items() if k != "key"}


# -- signing -----------------------------------------------------------------

def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sign(key: str, method: str, target: str, stamp: str, nonce: str, digest: str) -> str:
    message = "\n".join((method.upper(), target, stamp, nonce, digest)).encode()
    return hmac.new(bytes.fromhex(key), message, hashlib.sha256).hexdigest()


def signed_headers(key: str, method: str, target: str, digest: str) -> dict[str, str]:
    # The random word makes two identical requests in the same second two
    # signatures, so the second is not taken for a replay of the first.
    stamp, nonce = str(int(time.time())), secrets.token_hex(12)
    return {TIME_HEADER: stamp, NONCE_HEADER: nonce, DIGEST_HEADER: digest,
            SIGNATURE_HEADER: sign(key, method, target, stamp, nonce, digest)}


class Seen:
    """Signatures already accepted, for as long as their time is good."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def first_time(self, signature: str, now: float) -> bool:
        with self._lock:
            self._seen = {s: t for s, t in self._seen.items() if t > now - 2 * SKEW}
            if signature in self._seen:
                return False
            self._seen[signature] = now
            return True


def verify(key: str, method: str, target: str, headers, seen: Seen,
           now: float | None = None) -> str | None:
    """The body fingerprint a well-signed request vouches for, or None."""
    stamp = str(headers.get(TIME_HEADER, ""))
    nonce = str(headers.get(NONCE_HEADER, ""))
    digest = str(headers.get(DIGEST_HEADER, ""))
    signature = str(headers.get(SIGNATURE_HEADER, ""))
    now = time.time() if now is None else now
    try:
        if abs(now - int(stamp)) > SKEW:
            return None
    except ValueError:
        return None
    if not digest or len(nonce) < 16 or not hmac.compare_digest(
            sign(key, method, target, stamp, nonce, digest), signature):
        return None
    if not seen.first_time(signature, now):
        return None
    return digest
