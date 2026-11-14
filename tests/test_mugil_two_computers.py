"""Two computers: pairing them, and the originals kept on the Secondary.

The Primary's originals are only read; the Secondary keeps each file only
when the bytes that arrived have the fingerprint the Primary named, and
never writes over or removes one it keeps. A single computer is untouched:
every route between the two answers 404 there.
"""

import copy
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FAMILY, login
from ninaivu.mugil import pairing, role
from ninaivu.mugil.courier import Courier, Partner, PartnerError, sent_record
from ninaivu.mugil.hold import LEDGER, SET_ASIDE, Holding, Refused, clean_path, hasher
from ninaivu.storage import db, fingerprints, library_id


def digest_of(data: bytes, algo: str = fingerprints.ALGO) -> str:
    fresh = hasher(algo)
    fresh.update(data)
    return fresh.hexdigest()


# --- the parts ------------------------------------------------------------------

def test_a_single_computer_is_single_by_default(cfg):
    assert cfg.mugil_role == "single"
    assert role(cfg) == "single"
    cfg.mugil_role = "something else"
    assert role(cfg) == "single"


def test_the_two_computers_agree_on_a_key_from_the_code():
    codes = pairing.Codes()
    code = codes.new()
    assert len(code) == 9 and code[4] == "-"
    private, hello = pairing.primary_hello(code.lower().replace("-", " "))
    found = pairing.secondary_answer(codes, hello)
    assert found is not None
    key, answer = found
    assert pairing.primary_key(private, code, answer) == key
    assert len(bytes.fromhex(key)) == 32
    # The key never crosses the network: only the two public halves do.
    assert key not in str(hello) + str(answer)


def test_a_code_is_good_once():
    codes = pairing.Codes()
    code = codes.new()
    assert pairing.secondary_answer(codes, pairing.primary_hello(code)[1]) is not None
    assert pairing.secondary_answer(codes, pairing.primary_hello(code)[1]) is None


def test_a_wrong_code_is_refused_and_five_void_the_right_one():
    codes = pairing.Codes()
    code = codes.new()
    for _ in range(pairing.CODE_TRIES):
        assert pairing.secondary_answer(codes, pairing.primary_hello("WRONG-CODE")[1]) is None
    assert pairing.secondary_answer(codes, pairing.primary_hello(code)[1]) is None


def test_a_code_expires():
    now = [1000.0]
    codes = pairing.Codes(clock=lambda: now[0])
    code = codes.new()
    now[0] += pairing.CODE_LIFE + 1
    assert pairing.secondary_answer(codes, pairing.primary_hello(code)[1]) is None


def test_an_answer_from_a_computer_without_the_code_is_refused():
    codes = pairing.Codes()
    code = codes.new()
    private, hello = pairing.primary_hello(code)
    _, answer = pairing.secondary_answer(codes, hello)
    assert pairing.primary_key(private, "AAAA-BBBB", answer) is None
    assert pairing.primary_key(private, code, {**answer, "proof": "0" * 64}) is None


def test_a_signature_is_checked_once_and_only_in_time():
    key = os.urandom(32).hex()
    seen = pairing.Seen()
    headers = pairing.signed_headers(key, "POST", "/api/mugil/have", "abc")
    assert pairing.verify(key, "POST", "/api/mugil/have", headers, seen) == "abc"
    # The same request again is a replay.
    assert pairing.verify(key, "POST", "/api/mugil/have", headers, seen) is None
    fresh = pairing.signed_headers(key, "POST", "/api/mugil/have", "abc")
    assert pairing.verify(key, "PUT", "/api/mugil/have", fresh, pairing.Seen()) is None
    assert pairing.verify(key, "POST", "/api/mugil/have?x=1", fresh, pairing.Seen()) is None
    tampered = {**fresh, pairing.DIGEST_HEADER: "abd"}
    assert pairing.verify(key, "POST", "/api/mugil/have", tampered, pairing.Seen()) is None
    late = time.time() + pairing.SKEW + 60
    assert pairing.verify(key, "POST", "/api/mugil/have", fresh, pairing.Seen(), now=late) is None
    assert pairing.verify(os.urandom(32).hex(), "POST", "/api/mugil/have", fresh,
                          pairing.Seen()) is None


def test_the_pairing_file_is_the_owners_alone(tmp_path):
    pairing.save(tmp_path, {"key": "ab" * 32, "role": "primary"})
    assert pairing.load(tmp_path)["key"] == "ab" * 32
    if os.name == "posix":
        assert (tmp_path / pairing.PAIR_FILE).stat().st_mode & 0o077 == 0
    assert "key" not in pairing.public(pairing.load(tmp_path))
    pairing.forget(tmp_path)
    assert pairing.load(tmp_path) is None


@pytest.mark.parametrize("rel, clean", [
    ("2024/10/31/a.jpg", "2024/10/31/a.jpg"), ("2024\\10\\a.jpg", "2024/10/a.jpg"),
    ("../a.jpg", None), ("/etc/passwd", None), ("C:/x.jpg", None), ("", None),
    ("a/./b.jpg", "a/b.jpg"), ("a/../b.jpg", None), (".ninaivu-originals.db", None),
])
def test_only_a_path_inside_a_library_is_taken(rel, clean):
    assert clean_path(rel) == clean


# --- keeping a file -----------------------------------------------------------

LIB = "library-id-1234"


def keep(holding, rel, data, *, mtime=1_600_000_000.0, digest=None, size=None):
    return holding.receive(LIB, "Archive", rel, size=len(data) if size is None else size,
                           algo=fingerprints.ALGO, digest=digest or digest_of(data),
                           mtime=mtime, body=io.BytesIO(data))


def test_a_file_is_kept_byte_for_byte_with_its_time(tmp_path):
    holding = Holding(tmp_path)
    data = b"\xff\xd8\xff\xe1 exif and all " + os.urandom(5000)
    assert keep(holding, "2024/10/31/a.jpg", data)["kept"] == "new"
    kept = tmp_path / "Archive" / "2024/10/31/a.jpg"
    assert kept.read_bytes() == data
    assert kept.stat().st_mtime == 1_600_000_000.0
    assert holding.have(LIB, [{"path": "2024/10/31/a.jpg", "size": len(data),
                               "algo": fingerprints.ALGO, "digest": digest_of(data)}]) == [True]
    assert holding.summary()["files"] == 1
    # Nothing half-written is left beside it.
    assert [p.name for p in kept.parent.iterdir()] == ["a.jpg"]


def test_a_file_that_is_not_the_one_named_is_not_kept(tmp_path):
    holding = Holding(tmp_path)
    data = os.urandom(3000)
    with pytest.raises(Refused) as refused:
        keep(holding, "a.jpg", data, digest=digest_of(b"something else"))
    assert refused.value.status == 422
    with pytest.raises(Refused):
        keep(holding, "b.jpg", data, size=len(data) + 10)
    folder = tmp_path / "Archive"
    assert not any(p.is_file() for p in folder.rglob("*")) if folder.exists() else True
    assert holding.have(LIB, [{"path": "a.jpg", "size": len(data), "algo": fingerprints.ALGO,
                               "digest": digest_of(data)}]) == [False]


def test_a_changed_file_is_kept_beside_the_older_one(tmp_path):
    holding = Holding(tmp_path)
    keep(holding, "a.jpg", b"first version")
    assert keep(holding, "a.jpg", b"first version")["kept"] == "already"
    assert keep(holding, "a.jpg", b"second version")["kept"] == "set_aside_older"
    assert (tmp_path / "Archive" / "a.jpg").read_bytes() == b"second version"
    older = list((tmp_path / SET_ASIDE).rglob("a.jpg"))
    assert [p.read_bytes() for p in older] == [b"first version"]


def test_a_path_outside_the_folder_is_refused(tmp_path):
    holding = Holding(tmp_path / "originals")
    (tmp_path / "originals").mkdir()
    for rel in ("../escape.jpg", "/abs.jpg"):
        with pytest.raises(Refused):
            keep(holding, rel, b"x")
    assert not (tmp_path / "escape.jpg").exists()


def test_two_libraries_of_the_same_name_keep_apart(tmp_path):
    holding = Holding(tmp_path)
    holding.receive("library-one-1", "Photos", "a.jpg", size=3, algo=fingerprints.ALGO,
                    digest=digest_of(b"one"), mtime=1.0, body=io.BytesIO(b"one"))
    holding.receive("library-two-2", "Photos", "a.jpg", size=3, algo=fingerprints.ALGO,
                    digest=digest_of(b"two"), mtime=1.0, body=io.BytesIO(b"two"))
    found = sorted(p.read_bytes() for p in tmp_path.rglob("a.jpg"))
    assert found == [b"one", b"two"]
    assert (tmp_path / LEDGER).exists()


# --- the two computers, end to end ------------------------------------------------

class _Answer:
    def __init__(self, response):
        self.status = response.status_code
        self._data = response.get_data()

    def read(self, limit=None):
        if limit is None:
            data, self._data = self._data, b""
        else:
            data, self._data = self._data[:limit], self._data[limit:]
        return data


class _Through:
    """http.client's connection, answered by a Flask test client."""

    def __init__(self, client):
        self.client = client

    def request(self, method, target, body=None, headers=None):
        path, _, query = target.partition("?")
        data = body.read() if hasattr(body, "read") else body
        # As waitress hands a request over: it ends the body itself, so
        # Flask's limit is the only one on the stream. Without this the test
        # client cut each body off at its length, and every original the real
        # server was sent was refused as too large.
        self.response = self.client.open(path, method=method, query_string=query,
                                          data=data, headers=headers,
                                          environ_overrides={"wsgi.input_terminated": True})

    def getresponse(self):
        return _Answer(self.response)

    def close(self):
        pass


def partner_through(client):
    class Through(Partner):
        def _connection(self):
            return _Through(client)
    return Through


@pytest.fixture()
def secondary(app, as_admin, tmp_path):
    """The app as the Secondary, keeping originals in a folder of its own."""
    cfg = app.config["MV_CONFIG"]
    folder = tmp_path / "n300" / "originals"
    folder.mkdir(parents=True)
    answer = as_admin.post("/api/admin/mugil/role",
                           json={"role": "secondary", "originals_dir": str(folder)})
    assert answer.status_code == 200, answer.get_json()
    return cfg, folder


@pytest.fixture()
def primary(secondary, app, as_admin, tmp_path):
    """A Primary over the same library, paired with the Secondary."""
    cfg = app.config["MV_CONFIG"]
    mine = copy.copy(cfg)
    mine.state_dir = tmp_path / "primary-state"
    mine.state_dir.mkdir()
    mine.mugil_role = "primary"
    code = as_admin.post("/api/admin/mugil/code").get_json()["code"]
    client = app.test_client()
    through = partner_through(client)
    partner = through("http://linux-pc:3000")
    private, hello = pairing.primary_hello(code)
    answer = partner.request("POST", "/api/mugil/pair", body={**hello, "name": "Mac mini"},
                             signed=False)
    key = pairing.primary_key(private, code, answer)
    assert key == pairing.load(cfg.state_dir)["key"]
    pairing.save(mine.state_dir, {"key": key, "role": "primary"})
    mine.mugil_partner_url = "http://linux-pc:3000"
    for root in cfg.roots:
        library_id.ensure_marker(root)
    courier = Courier(mine, lambda: db.connect(cfg.db_path), partner=through)
    return mine, courier


def run(courier):
    totals = {"sent": 0, "bytes": 0, "already": 0, "changed": 0, "failed": 0,
              "skipped_libraries": []}
    return {**courier.run(totals), **totals}


def test_the_originals_go_to_the_secondary_exactly(primary, secondary, scanned):
    mine, courier = primary
    _, folder = secondary
    cfg, conn, _ = scanned
    root = Path(cfg.roots[0])
    files = conn.execute("SELECT rel_path FROM assets WHERE root=? AND trashed=0",
                         (str(root),)).fetchall()
    before = {r["rel_path"]: ((root / r["rel_path"]).read_bytes(),
                              (root / r["rel_path"]).stat().st_mtime) for r in files}
    result = run(courier)
    assert result["complete"] is True, result
    assert result["sent"] == len(files) and result["failed"] == 0
    kept = folder / root.name
    for rel, (data, mtime) in before.items():
        assert (kept / rel).read_bytes() == data
        assert (kept / rel).stat().st_mtime == pytest.approx(mtime, abs=1e-3)
        # The original on the Primary is as it was.
        assert (root / rel).read_bytes() == data
        assert (root / rel).stat().st_mtime == mtime
    with sent_record(mine.state_dir) as record:
        assert record.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == len(files)
    # The next run sends nothing: every file is kept already.
    again = run(courier)
    assert again["sent"] == 0 and again["already"] == len(files)


def test_a_file_that_changed_on_the_primary_is_sent_again(primary, secondary, scanned):
    _, courier = primary
    _, folder = secondary
    cfg, conn, scanner = scanned
    run(courier)
    root = Path(cfg.roots[0])
    rel = conn.execute("SELECT rel_path FROM assets WHERE root=? AND rel_path LIKE '%.png'",
                       (str(root),)).fetchone()["rel_path"]
    from PIL import Image
    Image.new("RGB", (210, 210), (200, 10, 10)).save(root / rel)
    scanner._run(root, full=True)
    result = run(courier)
    assert result["sent"] == 1
    assert (folder / root.name / rel).read_bytes() == (root / rel).read_bytes()
    assert len(list((folder / SET_ASIDE).rglob(Path(rel).name))) == 1


def test_an_exact_copy_is_not_sent_twice(primary, secondary, scanned):
    import shutil
    _, courier = primary
    _, folder = secondary
    cfg, conn, scanner = scanned
    run(courier)
    root = Path(cfg.roots[0])
    rel = conn.execute("SELECT rel_path FROM assets WHERE root=? AND kind='picture' "
                       "AND trashed=0 LIMIT 1", (str(root),)).fetchone()["rel_path"]
    (root / "copies").mkdir(exist_ok=True)
    twin = root / "copies" / Path(rel).name
    shutil.copyfile(root / rel, twin)
    os.utime(twin, (1500000000, 1500000000))
    scanner._run(root, full=True)
    result = run(courier)
    assert result["sent"] == 1 and result.get("copied_there") == 1, result
    kept = folder / root.name / "copies" / twin.name
    assert kept.read_bytes() == twin.read_bytes()
    # Its own time, not the other copy's.
    assert kept.stat().st_mtime == 1500000000
    assert (folder / root.name / rel).stat().st_mtime == pytest.approx(
        (root / rel).stat().st_mtime, abs=1e-3)


def test_a_secondary_without_its_folder_is_waited_for(primary, secondary, monkeypatch):
    from ninaivu.mugil import courier as courier_module
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.05)
    _, courier = primary
    cfg, folder = secondary
    cfg.mugil_originals_dir = ""
    # The disk is plugged back in a little later.
    threading.Timer(0.3, setattr, (cfg, "mugil_originals_dir", str(folder))).start()
    result = run(courier)
    assert result["complete"] is True and result["sent"] > 0, result


def _away_for(through, times, status=0):
    """A Partner that does not answer the first *times* requests."""
    class Away(through):
        missed = 0

        def request(self, *args, **kwargs):
            if Away.missed < times:
                Away.missed += 1
                raise PartnerError("Nobody answered.", status)
            return super().request(*args, **kwargs)
    return Away


def test_a_secondary_that_is_down_is_waited_for(primary, secondary, scanned, monkeypatch):
    from ninaivu.mugil import courier as courier_module
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.01)
    mine, courier = primary
    cfg, conn, _ = scanned
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
    courier.partner = _away_for(courier.partner, 5)
    result = run(courier)
    assert result["complete"] is True, result
    assert result["sent"] == files and result["failed"] == 0


def test_the_secondary_going_away_in_the_middle_carries_on_from_there(primary, secondary,
                                                                      scanned, monkeypatch):
    from ninaivu.mugil import courier as courier_module
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.01)
    _, courier = primary
    cfg, conn, _ = scanned
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
    through = courier.partner

    class Restarts(through):
        puts = 0
        missed = 0

        def request(self, method, path, **kwargs):
            if method == "PUT":
                Restarts.puts += 1
            # Down after the first file, for three requests.
            if Restarts.puts >= 2 and Restarts.missed < 3:
                Restarts.missed += 1
                raise PartnerError("Connection refused.", 503)
            return super().request(method, path, **kwargs)
    courier.partner = Restarts
    result = run(courier)
    assert result["complete"] is True, result
    assert result["sent"] == files and result["failed"] == 0


def test_stopping_while_the_secondary_is_away_ends_the_run(primary, secondary, monkeypatch):
    from ninaivu.mugil import courier as courier_module
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.05)
    _, courier = primary
    courier.partner = _away_for(courier.partner, 10 ** 6)
    threading.Timer(0.3, courier.stop).start()
    started = time.monotonic()
    result = run(courier)
    assert result["complete"] is False and result["stopped"] is True
    assert time.monotonic() - started < 5


def test_a_secondary_that_refuses_is_not_waited_for(primary, secondary):
    _, courier = primary
    courier.partner = _away_for(courier.partner, 1, status=401)
    with pytest.raises(PartnerError):
        run(courier)


def test_the_console_says_the_mac_is_waiting(primary, secondary, monkeypatch):
    from ninaivu.mugil import courier as courier_module
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.05)
    _, courier = primary
    courier.partner = _away_for(courier.partner, 10 ** 6)
    seen = []
    threading.Timer(0.2, lambda: (seen.append(courier.status()["progress"]), courier.stop())).start()
    run(courier)
    assert seen and seen[0].get("secondary_away")


def test_a_request_not_signed_by_the_pair_is_refused(app, secondary):
    client = app.test_client()
    assert client.get("/api/mugil/hello").status_code == 401
    other = os.urandom(32).hex()
    headers = pairing.signed_headers(other, "GET", "/api/mugil/hello", pairing.body_digest(b""))
    assert client.get("/api/mugil/hello", headers=headers).status_code == 401
    body = b"stolen"
    put = client.put("/api/mugil/originals?library=x&name=y&path=a.jpg", data=body,
                     headers={**pairing.signed_headers(
                         other, "PUT", "/api/mugil/originals?library=x&name=y&path=a.jpg",
                         f"{fingerprints.ALGO}:{digest_of(body)}:6:1.0"),
                         "X-Requested-With": "ninaivu-mugil"})
    assert put.status_code == 401


def test_a_single_computer_answers_nothing_between_two(app, as_admin):
    client = app.test_client()
    for method, path in (("GET", "/api/mugil/hello"), ("POST", "/api/mugil/have"),
                         ("POST", "/api/mugil/pair"), ("PUT", "/api/mugil/originals")):
        answer = client.open(path, method=method, json={},
                             headers={"X-Requested-With": "ninaivu-mugil"})
        assert answer.status_code == 404, path


def test_the_part_cannot_change_while_paired(primary, as_admin):
    answer = as_admin.post("/api/admin/mugil/role", json={"role": "single"})
    assert answer.status_code == 409
    assert as_admin.post("/api/admin/mugil/unpair").status_code == 200
    assert as_admin.post("/api/admin/mugil/role", json={"role": "single"}).status_code == 200


def test_the_originals_folder_cannot_be_inside_the_library(app, as_admin, scanned):
    cfg, _, _ = scanned
    inside = Path(cfg.roots[0]) / "misc"
    answer = as_admin.post("/api/admin/mugil/role",
                           json={"role": "secondary", "originals_dir": str(inside)})
    assert answer.status_code == 400


def test_the_console_shows_the_part_but_never_the_key(primary, as_admin):
    body = as_admin.get("/api/admin/mugil").get_json()
    assert body["role"] == "secondary" and body["paired"] is True
    assert "key" not in body["pairing"]
    assert body["keeping"]["folder"]


def test_only_an_admin_can_change_the_part(app, people):
    client = login(app.test_client(), *FAMILY)
    assert client.post("/api/admin/mugil/role", json={"role": "secondary"}).status_code in (401, 403)
    assert client.get("/api/admin/mugil").status_code in (401, 403)


def test_a_file_bigger_than_an_upload_is_still_kept(primary, secondary, app, scanned):
    """The family app's upload limit is not the Secondary's: a 6 GB video
    has to go. Here every photograph is bigger than the limit is set to."""
    _, courier = primary
    _, folder = secondary
    cfg, _, scanner = scanned
    root = Path(cfg.roots[0])
    from PIL import Image
    noise = Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
    noise.save(root / "misc" / "big.png")
    scanner._run(root, full=True)
    assert (root / "misc" / "big.png").stat().st_size > 400_000
    app.config["MAX_CONTENT_LENGTH"] = 100_000
    result = run(courier)
    assert result["failed"] == 0 and result["sent"] > 0
    assert (folder / root.name / "misc" / "big.png").read_bytes() == \
        (root / "misc" / "big.png").read_bytes()
    # Everything else still keeps the limit.
    assert app.test_client().post("/api/auth/login", data=b"x" * 200_000,
                                  content_type="application/json").status_code == 413


def test_a_run_needs_a_pairing(cfg, scanned):
    courier = Courier(cfg, lambda: db.connect(cfg.db_path))
    assert courier.start()["started"] is False
    cfg.mugil_role = "primary"
    assert "Pair" in courier.ready()
    assert courier.owed() is False


# --- finding each other, and one tap -----------------------------------------------

from ninaivu.mugil import meeting  # noqa: E402


def test_both_sides_show_the_same_number_and_one_tap_pairs():
    welcome = meeting.Welcome()
    welcome.open()
    private, half = pairing.new_half()
    answer = welcome.introduce(half.hex(), "Mac mini", "192.168.1.20")
    theirs = bytes.fromhex(answer["public"])
    assert pairing.match_number(half, theirs) == answer["number"]
    assert welcome.status()["introduced"][0]["number"] == answer["number"]
    key = pairing.tap_key(private, theirs, half, theirs)
    assert welcome.accept(half.hex(), "0" * 64) is None
    accepted = welcome.accept(half.hex(), pairing.tap_proof(key))
    assert accepted is not None and accepted[0] == key
    assert accepted[1]["name"] == "Mac mini"
    # One pairing per wait: the wait is over.
    assert welcome.waiting is False
    assert welcome.introduce(pairing.new_half()[1].hex(), "Another", "x") is None


def test_a_look_alike_shows_a_different_number():
    welcome = meeting.Welcome()
    welcome.open()
    _, real = pairing.new_half()
    _, impostor = pairing.new_half()
    secondary = bytes.fromhex(welcome.introduce(real.hex(), "Mac mini", "a")["public"])
    # The Primary was answered by a device with halves of its own.
    _, fake_secondary = pairing.new_half()
    assert pairing.match_number(real, fake_secondary) != pairing.match_number(real, secondary)
    assert welcome.accept(impostor.hex(), "0" * 64) is None


def test_the_wait_ends_by_itself():
    now = [1000.0]
    welcome = meeting.Welcome(clock=lambda: now[0])
    welcome.open()
    now[0] += meeting.WAIT + 1
    assert welcome.waiting is False
    assert welcome.introduce(pairing.new_half()[1].hex(), "Mac", "a") is None


def test_the_secondary_answers_the_question_on_the_network(cfg):
    cfg.mugil_role = "secondary"
    cfg.host = "0.0.0.0"
    welcome = meeting.Welcome()
    welcome.open()
    beacon = meeting.Beacon(cfg, welcome, lambda: "Linux PC")
    beacon.keep()
    try:
        found = []
        for _ in range(20):
            found = meeting.discover(listen=0.5, targets=["127.0.0.1"])
            if found:
                break
        assert found and found[0]["name"] == "Linux PC" and found[0]["waiting"] is True
        assert found[0]["port"] == cfg.port
        cfg.mugil_role = "single"
        time.sleep(6)
        assert meeting.discover(listen=0.5, targets=["127.0.0.1"]) == []
    finally:
        beacon.stop(join=True)


def test_a_primary_found_pairs_with_one_tap(app, secondary, as_admin):
    cfg, _ = secondary
    client = app.test_client()
    status = as_admin.get("/api/admin/mugil").get_json()
    assert status["welcome"]["waiting"] is True        # choosing the part opened it
    private, half = pairing.new_half()
    introduced = client.post("/api/mugil/introduce", json={"public": half.hex(), "name": "Mac mini"},
                             headers={"X-Requested-With": "ninaivu-mugil"})
    assert introduced.status_code == 200, introduced.get_json()
    theirs = bytes.fromhex(introduced.get_json()["public"])
    number = pairing.match_number(half, theirs)
    shown = as_admin.get("/api/admin/mugil").get_json()["welcome"]["introduced"]
    assert [i["number"] for i in shown] == [number]
    key = pairing.tap_key(private, theirs, half, theirs)
    tapped = client.post("/api/mugil/pair", json={"tap": True, "public": half.hex(),
                                                  "proof": pairing.tap_proof(key)},
                         headers={"X-Requested-With": "ninaivu-mugil"})
    assert tapped.status_code == 200, tapped.get_json()
    assert pairing.load(cfg.state_dir)["key"] == key
    assert pairing.load(cfg.state_dir)["number"] == number
    # Signed with that key, the Primary is answered.
    headers = pairing.signed_headers(key, "GET", "/api/mugil/hello", pairing.body_digest(b""))
    assert client.get("/api/mugil/hello", headers=headers).status_code == 200
    # And nobody else can introduce themselves now.
    again = client.post("/api/mugil/introduce", json={"public": pairing.new_half()[1].hex()},
                        headers={"X-Requested-With": "ninaivu-mugil"})
    assert again.status_code == 404


def test_a_tap_without_an_open_wait_is_refused(app, secondary, as_admin):
    app.config["MV_SERVICES"].mugil_welcome.close()
    client = app.test_client()
    private, half = pairing.new_half()
    answer = client.post("/api/mugil/introduce", json={"public": half.hex()},
                         headers={"X-Requested-With": "ninaivu-mugil"})
    assert answer.status_code == 403
    assert as_admin.post("/api/admin/mugil/welcome").status_code == 200
    assert client.post("/api/mugil/introduce", json={"public": half.hex()},
                       headers={"X-Requested-With": "ninaivu-mugil"}).status_code == 200


def test_the_family_app_lets_the_primary_in_when_the_library_is_closed(app, secondary):
    cfg, _ = secondary
    cfg.open_browsing = False
    _, half = pairing.new_half()
    answer = app.test_client().post("/api/mugil/introduce", json={"public": half.hex()},
                                    headers={"X-Requested-With": "ninaivu-mugil"})
    assert answer.status_code == 200


def test_the_family_app_carries_the_routes_between_two(tmp_path, cfg, scanned):
    from ninaivu import build_services, create_home_app
    services = build_services(cfg)
    try:
        home = create_home_app(services)
        assert "/api/mugil/introduce" in {r.rule for r in home.url_map.iter_rules()}
        assert "/api/admin/mugil" not in {r.rule for r in home.url_map.iter_rules()}
    finally:
        services.stop(timeout=5.0)


@pytest.fixture()
def two_apps(tmp_path, cfg, scanned, monkeypatch):
    """A Secondary and a Primary, each its own Ninaivu, the Primary's requests
    answered by the Secondary's app."""
    from ninaivu import create_app
    from ninaivu.api import mugil_api
    from ninaivu.server import auth
    from conftest import ADMIN
    from ninaivu.server.config import Config

    other = Config()
    other.state_dir = tmp_path / "secondary-state"
    other.roots = []
    other.active_root = ""
    other.ai_enabled, other.ai_engine, other.watch = False, "off", False
    other.ai_models_dir = str(tmp_path / "ai-models")
    other.ensure_dirs()
    conn = db.init_db(other.db_path)
    auth.init_auth_schema(conn)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    secondary_app = create_app(other)

    primary_conn = scanned[1]
    auth.bootstrap_admin(primary_conn, ADMIN[0], ADMIN[1], "Dad")
    primary_app = create_app(cfg)
    primary_app.config["MV_SCANNER"].stop(join=True)

    monkeypatch.setattr(mugil_api, "Partner", partner_through(secondary_app.test_client()))
    monkeypatch.setattr(meeting, "discover", lambda **_: [
        {"id": "linux-pc-id", "address": "192.168.1.30", "port": 80, "name": "Linux PC",
         "waiting": True}])
    yield primary_app, secondary_app, other
    for application in (primary_app, secondary_app):
        application.config["MV_SERVICES"].stop(timeout=5.0)


def test_choosing_the_parts_is_all_it_takes(two_apps, tmp_path):
    from conftest import ADMIN
    primary_app, secondary_app, other = two_apps
    second = login(secondary_app.test_client(), *ADMIN)
    first = login(primary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    assert second.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(folder)}).status_code == 200
    assert first.post("/api/admin/mugil/role", json={"role": "primary"}).status_code == 200

    found = first.post("/api/admin/mugil/look").get_json()["found"]
    assert [f["name"] for f in found] == ["Linux PC"]
    number = found[0]["number"]
    # The Secondary's console shows the same number.
    shown = second.get("/api/admin/mugil").get_json()["welcome"]["introduced"]
    assert [i["number"] for i in shown] == [number]

    paired = first.post("/api/admin/mugil/pair", json={"found": "linux-pc-id"})
    assert paired.status_code == 200, paired.get_json()
    assert paired.get_json()["paired"] is True
    primary_cfg = primary_app.config["MV_CONFIG"]
    assert pairing.load(primary_cfg.state_dir)["key"] == pairing.load(other.state_dir)["key"]
    assert second.get("/api/admin/mugil").get_json()["paired"] is True


def _paired(two_apps, tmp_path):
    from conftest import ADMIN
    primary_app, secondary_app, other = two_apps
    second = login(secondary_app.test_client(), *ADMIN)
    first = login(primary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    second.post("/api/admin/mugil/role", json={"role": "secondary", "originals_dir": str(folder)})
    first.post("/api/admin/mugil/role", json={"role": "primary"})
    first.post("/api/admin/mugil/look")
    assert first.post("/api/admin/mugil/pair", json={"found": "linux-pc-id"}).status_code == 200
    services = primary_app.config["MV_SERVICES"]
    for root in services.cfg.roots:
        library_id.ensure_marker(root)
    services.mugil_courier.partner = partner_through(secondary_app.test_client())
    result = run(services.mugil_courier)
    assert result["complete"] is True and result["sent"] > 0, result
    return first, services, folder


def test_an_original_not_on_the_primary_is_sent_for(two_apps, tmp_path, scanned, monkeypatch):
    from ninaivu.mugil import fetch
    primary_app, secondary_app, _ = two_apps
    first, services, _ = _paired(two_apps, tmp_path)
    monkeypatch.setattr(fetch, "Partner", partner_through(secondary_app.test_client()))
    cfg, conn, _ = scanned
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE kind='picture' AND trashed=0 "
                       "LIMIT 1").fetchone()
    path = Path(row["root"]) / row["rel_path"]
    data, mtime = path.read_bytes(), path.stat().st_mtime
    path.unlink()                      # as if its disk were unplugged
    answer = first.get(f"/api/file/{row['id']}")
    assert answer.status_code == 200
    assert answer.get_data() == data
    kept = fetch.original(cfg, row["root"], row["rel_path"])
    assert kept.read_bytes() == data
    assert kept.stat().st_mtime == pytest.approx(mtime, abs=1e-3)
    assert not os.access(kept, os.W_OK) or os.geteuid() == 0
    # Nothing is put back into the library by it.
    assert not path.exists()
    # An edited copy cannot be saved beside a folder that is not here.
    refused = first.post(f"/api/asset/{row['id']}/edited-copy", data=b"x",
                         headers={"Content-Type": "image/jpeg", "X-Requested-With": "test"})
    assert refused.status_code == 409


def test_an_original_the_secondary_does_not_keep_is_missing(two_apps, tmp_path, scanned,
                                                            monkeypatch):
    from ninaivu.mugil import fetch
    primary_app, secondary_app, other = two_apps
    first, _services, folder = _paired(two_apps, tmp_path)
    monkeypatch.setattr(fetch, "Partner", partner_through(secondary_app.test_client()))
    cfg, conn, _ = scanned
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE kind='picture' AND trashed=0 "
                       "LIMIT 1").fetchone()
    (Path(row["root"]) / row["rel_path"]).unlink()
    # Gone from the Secondary's disk too: nothing to send.
    for kept in folder.rglob(Path(row["rel_path"]).name):
        kept.unlink()
    assert first.get(f"/api/file/{row['id']}").status_code == 404


def test_one_computer_never_sends_for_an_original(cfg, scanned):
    from ninaivu.mugil import fetch
    _, conn, _ = scanned
    row = conn.execute("SELECT root, rel_path FROM assets LIMIT 1").fetchone()
    assert fetch.original(cfg, row["root"], row["rel_path"]) is None


def test_the_primary_makes_no_display_copies(two_apps, tmp_path):
    """The smaller copies were poor to look at: nothing makes one, the page
    is told nothing about them, and what an earlier version left is removed."""
    from ninaivu.mugil import free
    first, services, _ = _paired(two_apps, tmp_path)
    assert not hasattr(services, "mugil_display")
    assert "display" not in first.get("/api/admin/mugil").get_json()
    state = Path(services.cfg.state_dir)
    (state / "display" / "ab").mkdir(parents=True)
    (state / "display" / "ab" / "abcd.avif").write_bytes(b"small")
    (state / "video-trial").mkdir()
    for name in ("mugil-display.db", "mugil-display.db-wal", "video-trial.db", "compress-queue.json"):
        (state / name).write_bytes(b"x")
    assert free.clear_old_copies(state) is True
    for name in ("display", "video-trial", "mugil-display.db", "mugil-display.db-wal",
                 "video-trial.db", "compress-queue.json"):
        assert not (state / name).exists(), name
    assert free.clear_old_copies(state) is False


def _let_go(services, conn, rows):
    """Put *rows* (root, rel_path) in the state 4.0.0's Free space left them
    in: written down as let go, with the sizes the Secondary confirmed, and
    removed from this computer. Nothing in Ninaivu does this any more."""
    from ninaivu.mugil import fetch
    for root, rel in rows:
        record = fetch.record_of(services.cfg.state_dir, root, rel)
        assert record, rel
        conn.execute("INSERT OR REPLACE INTO mugil_let_go(root, rel_path, size, mtime, algo, digest, "
                     "let_go_at) VALUES (?,?,?,?,?,?,?)",
                     (root, rel, record["size"], record["mtime"], record["algo"], record["digest"],
                      time.time()))
        (Path(root) / rel).unlink()
    conn.commit()


def test_an_original_on_the_secondary_only_is_shown_as_it_is(two_apps, tmp_path, scanned, monkeypatch):
    """A let-go photograph is sent for from the Secondary and shown as it
    is. While the Secondary cannot send it there is nothing to show: no
    smaller copy stands in for it any more."""
    from ninaivu.mugil import fetch
    _primary_app, secondary_app, _ = two_apps
    first, services, _ = _paired(two_apps, tmp_path)
    cfg = services.cfg
    conn = db.connect(cfg.db_path)
    row = next(r for r in conn.execute(
        "SELECT id, root, rel_path FROM assets WHERE kind='picture' AND trashed=0")
        if fetch.record_of(cfg.state_dir, r["root"], r["rel_path"]))
    original = (Path(row["root"]) / row["rel_path"]).read_bytes()
    _let_go(services, conn, [(row["root"], row["rel_path"])])
    monkeypatch.setattr(fetch, "Partner", partner_through(secondary_app.test_client()))
    shown = first.get(f"/api/file/{row['id']}")
    assert shown.status_code == 200 and shown.get_data() == original
    # The Secondary away: not found, here or as a download.
    monkeypatch.setattr(fetch, "original", lambda *a, **k: None)
    assert first.get(f"/api/file/{row['id']}").status_code == 404
    assert first.get(f"/api/download/{row['id']}").status_code == 404


# --- the two computers side by side ---------------------------------------------------

def test_the_console_shows_both_computers(two_apps, tmp_path, monkeypatch):
    from ninaivu import __version__
    from ninaivu.api import mugil_api
    from ninaivu.mugil import machines
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    monkeypatch.setattr(mugil_api, "Partner", partner_through(secondary_app.test_client()))
    answer = first.get("/api/admin/mugil/machines").get_json()
    assert answer["online"] is True and answer["same_version"] is True
    assert answer["this"]["role"] == "primary" and answer["this"]["version"] == __version__
    theirs = answer["other"]
    assert theirs["machine"]["role"] == "secondary"
    assert theirs["health"]["files"] > 0 and theirs["health"]["reachable"] is True
    assert theirs["health"]["last_kept"]
    # The Secondary remembers the Primary, and which version it said it runs.
    seen = machines.remembered(other.state_dir)
    assert seen["version"] == __version__ and seen["machine"]["role"] == "primary"


def test_a_secondary_that_is_away_is_shown_as_last_seen(two_apps, tmp_path, monkeypatch):
    from ninaivu.api import mugil_api
    _primary_app, secondary_app, _ = two_apps
    first, _services, _ = _paired(two_apps, tmp_path)
    monkeypatch.setattr(mugil_api, "Partner", partner_through(secondary_app.test_client()))
    first.get("/api/admin/mugil/machines")
    monkeypatch.setattr(mugil_api, "Partner", _away_for(partner_through(secondary_app.test_client()),
                                                        10 ** 6))
    answer = first.get("/api/admin/mugil/machines").get_json()
    assert answer["online"] is False
    assert answer["other"]["seen_at"] and answer["other"]["machine"]["role"] == "secondary"


def test_different_versions_are_said(two_apps, tmp_path, monkeypatch):
    from ninaivu.api import mugil_api
    from ninaivu.mugil import machines
    _primary_app, secondary_app, _ = two_apps
    first, services, _ = _paired(two_apps, tmp_path)
    monkeypatch.setattr(mugil_api, "Partner", _away_for(partner_through(secondary_app.test_client()),
                                                        10 ** 6))
    machines.remember(services.cfg.state_dir, {"machine": {"name": "Linux PC", "role": "secondary",
                                                           "version": "1.0.0"}, "version": "1.0.0"})
    assert first.get("/api/admin/mugil/machines").get_json()["same_version"] is False
    first.post("/api/admin/mugil/unpair")
    assert machines.remembered(services.cfg.state_dir) is None


def test_the_disks_own_verdict_is_read_when_it_can_be(tmp_path, monkeypatch):
    from ninaivu.mugil import machines
    monkeypatch.setattr(machines.shutil, "which", lambda name: "/usr/sbin/smartctl")
    monkeypatch.setattr(machines.sys, "platform", "linux")

    def runner(cmd, **_):
        if cmd[0] == "df":
            return SimpleNamespace(stdout="Filesystem\n/dev/sdb1\n", returncode=0)
        return SimpleNamespace(stdout='{"smart_status": {"passed": false}, "model_name": "N300"}',
                               returncode=4)
    verdict = machines.smart_verdict(tmp_path, runner=runner)
    assert verdict == {"passed": False, "device": "/dev/sdb1", "model": "N300"}
    assert machines.smart_verdict(tmp_path, runner=lambda *a, **k: SimpleNamespace(
        stdout="", returncode=1)) is None


# --- telling somebody ------------------------------------------------------------------

def test_somebody_is_told_when_the_secondary_is_away_a_day(primary, secondary):
    from ninaivu.mugil import courier as courier_module, machines
    mine, courier = primary
    sent = []
    courier.notify = lambda event, summary, detail: sent.append((event, summary))
    now = time.time()
    # Seen just now: nothing to say.
    assert courier.look_after_the_secondary(now) == []
    seen = machines.remembered(mine.state_dir)
    assert seen and seen["health"]["reachable"] is True
    # Away since, and asked again a day later without an answer.
    courier.partner = _away_for(courier.partner, 10 ** 6)
    later = seen["seen_at"] + courier_module.AWAY_TOO_LONG + 1
    assert courier.look_after_the_secondary(later) == ["secondary_away"]
    assert sent[-1][0] == "secondary_away"


def test_somebody_is_told_when_its_disk_is_failing_or_full(primary, secondary):
    from ninaivu.mugil import machines
    mine, courier = primary
    sent = []
    courier.notify = lambda event, summary, detail: sent.append(event)
    courier._asked_at = time.time()           # not asked again in this test
    machines.remember(mine.state_dir, {"machine": {"name": "Linux PC"}, "health": {
        "smart": {"passed": False}, "free_bytes": 10, "total_bytes": 100}})
    assert courier.look_after_the_secondary() == ["secondary_disk"]
    machines.remember(mine.state_dir, {"machine": {"name": "Linux PC"}, "health": {
        "free_bytes": 1, "total_bytes": 100}})
    assert courier.look_after_the_secondary() == ["secondary_disk"]
    machines.remember(mine.state_dir, {"machine": {"name": "Linux PC"}, "health": {
        "free_bytes": 50, "total_bytes": 100, "smart": {"passed": True}}})
    assert courier.look_after_the_secondary() == []
    assert sent == ["secondary_disk", "secondary_disk"]


def test_one_computer_tells_nobody_about_a_secondary(cfg, scanned):
    courier = Courier(cfg, lambda: db.connect(cfg.db_path))
    assert courier.look_after_the_secondary() == []


# --- Optimize Mac storage: the plan only --------------------------------------------------

def test_there_is_no_plan_to_free_space_any_more(two_apps, tmp_path, scanned):
    first, _services, _ = _paired(two_apps, tmp_path)
    assert first.get("/api/admin/mugil/optimize").status_code == 404
    assert "free_space" not in first.get("/api/admin/mugil").get_json()


def test_nothing_is_let_go_any_more(two_apps, tmp_path, scanned):
    """4.0.1: the Primary keeps its originals. The switch is gone, and a
    page left open from before is told so."""
    first, services, _ = _paired(two_apps, tmp_path)
    before = sorted(p.name for p in Path(scanned[0].roots[0]).rglob("*") if p.is_file())
    first.post("/api/admin/mugil/role", json={"role": "primary", "free_space": True})
    assert services.cfg.mugil_free_space is False
    answer = first.post("/api/admin/mugil/free", json={"confirm": True})
    assert answer.status_code == 410 and "removed" in answer.get_json()["error"]
    with pytest.raises(PartnerError):
        services.mugil_free.start()
    assert sorted(p.name for p in Path(scanned[0].roots[0]).rglob("*") if p.is_file()) == before


def _some_let_go(two_apps, tmp_path, scanned, monkeypatch, n=3):
    from ninaivu.mugil import fetch
    _primary_app, secondary_app, _ = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    cfg = scanned[0]
    through = partner_through(secondary_app.test_client())
    monkeypatch.setattr(fetch, "Partner", through)
    services.mugil_free.partner = through
    conn = db.connect(cfg.db_path)
    rows = [(r["root"], r["rel_path"]) for r in conn.execute(
        "SELECT root, rel_path FROM assets WHERE trashed=0 ORDER BY id")
        if fetch.record_of(services.cfg.state_dir, r["root"], r["rel_path"])][:n]
    assert len(rows) == n
    originals = {rel: (Path(root) / rel).read_bytes() for root, rel in rows}
    mtimes = {rel: (Path(root) / rel).stat().st_mtime for root, rel in rows}
    _let_go(services, conn, rows)
    return first, services, conn, originals, mtimes


def test_every_original_let_go_comes_back(two_apps, tmp_path, scanned, monkeypatch):
    """A library that used Free space in 4.0.0: its originals are still
    found, and are brought back as one piece of work with its progress."""
    from ninaivu.mugil import free
    first, services, conn, originals, mtimes = _some_let_go(two_apps, tmp_path, scanned, monkeypatch)
    cfg = scanned[0]
    assert free.summary(conn)["files"] == len(originals)
    # A rescan does not take them for missing files.
    present = [p.relative_to(cfg.roots[0]).as_posix() for p in Path(cfg.roots[0]).rglob("*")
               if p.is_file()]
    db.delete_missing(conn, cfg.roots[0], present, force=True)
    conn.commit()
    rel = next(iter(originals))
    row = conn.execute("SELECT id FROM assets WHERE root=? AND rel_path=?",
                       (cfg.roots[0], rel)).fetchone()
    assert row is not None
    assert first.get(f"/api/file/{row['id']}").get_data() == originals[rel]
    assert first.get(f"/api/download/{row['id']}").get_data() == originals[rel]
    status = first.get("/api/admin/mugil").get_json()["freeing"]
    assert status["let_go"]["files"] == len(originals) and status["on"] is False

    started = first.post("/api/admin/mugil/bring-back", json={"all": True})
    assert started.status_code == 200, started.get_json()
    services.mugil_free._thread.join(timeout=60)
    done = first.get("/api/admin/mugil").get_json()["freeing"]
    assert done["back"] == len(originals) and done["missed"] == 0 and not done["error"], done
    assert done["let_go"]["files"] == 0 and done["running"] is False
    for rel, data in originals.items():
        path = Path(cfg.roots[0]) / rel
        assert path.read_bytes() == data
        assert path.stat().st_mtime == pytest.approx(mtimes[rel], abs=1e-3)


def test_bringing_back_stops_before_the_disk_is_full(two_apps, tmp_path, scanned, monkeypatch):
    from ninaivu.mugil import free
    first, services, conn, originals, _ = _some_let_go(two_apps, tmp_path, scanned, monkeypatch)
    monkeypatch.setattr(free, "_room_for", lambda root, size: False)
    done = services.mugil_free.run()
    assert done["back"] == 0 and "nearly full" in done["error"], done
    assert free.summary(conn)["files"] == len(originals)
    for rel in originals:
        assert not (Path(scanned[0].roots[0]) / rel).exists()


def test_what_the_secondary_cannot_send_stays_let_go(two_apps, tmp_path, scanned, monkeypatch):
    from ninaivu.mugil import fetch, free
    first, services, conn, originals, _ = _some_let_go(two_apps, tmp_path, scanned, monkeypatch)
    monkeypatch.setattr(fetch, "original", lambda *a, **k: None)      # the Secondary is away
    done = services.mugil_free.run()
    assert done["back"] == 0 and done["missed"] == len(originals), done
    assert free.summary(conn)["files"] == len(originals)


def test_the_secondary_reads_its_copy_again_before_saying_yes(tmp_path):
    holding = Holding(tmp_path)
    data = b"a photograph"
    keep(holding, "a.jpg", data)
    asked = {"size": len(data), "algo": fingerprints.ALGO, "digest": digest_of(data)}
    assert holding.same(LIB, "a.jpg", **asked) is True
    kept = tmp_path / "Archive" / "a.jpg"
    os.chmod(kept, 0o644)
    kept.write_bytes(b"x" * len(data))
    assert holding.same(LIB, "a.jpg", **asked) is False
    assert holding.same(LIB, "b.jpg", **asked) is False


# --- phone uploads while the Primary sleeps -----------------------------------------------

def _png(colour):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(out, "PNG")
    return out.getvalue()


def _basic(key):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"dad:{key}".encode()).decode()}


def test_a_phone_sends_to_the_secondary_and_the_primary_collects(two_apps, tmp_path, scanned):
    from ninaivu.api import api_webdav
    from ninaivu.media import phone_backup, phone_keys
    from ninaivu.server import auth
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    cfg = scanned[0]
    conn = db.connect(cfg.db_path)
    phone_keys.init_schema(conn)
    dad = auth.get_user_by_username(conn, "dad") if hasattr(auth, "get_user_by_username") else None
    dad_id = dad.id if dad else conn.execute("SELECT id FROM users WHERE username='dad'").fetchone()[0]
    _row, key = phone_keys.make(conn, dad_id, "Dad's phone")
    _gone, revoked = phone_keys.make(conn, dad_id, "Old phone")
    phone_keys.revoke(conn, _gone["id"])
    phone = secondary_app.test_client()
    # Before the Primary has handed its keys over, the Secondary knows none.
    assert phone.open("/dav/", method="PROPFIND", headers=_basic(key)).status_code == 401
    services.mugil_courier.partner = partner_through(secondary_app.test_client())
    run(services.mugil_courier)
    # The Mac's address says the always-on computer's.
    assert first.get("/api/phone-keys").get_json()["always_on"] is True

    data = _png((10, 200, 30))
    sent = phone.put("/dav/2024/06/IMG_0001.png", data=data,
                     headers={**_basic(key), "X-OC-Mtime": "1717200000"})
    assert sent.status_code == 201, sent.get_data(as_text=True)
    assert phone.put("/dav/2024/06/IMG_0002.png", data=data, headers=_basic(revoked)).status_code == 401
    assert phone.head("/dav/2024/06/IMG_0001.png", headers=_basic(key)).status_code == 200
    listing = phone.open("/dav/2024/06/", method="PROPFIND", headers=_basic(key))
    assert listing.status_code == 207 and b"IMG_0001.png" in listing.get_data()
    # A phone key adds and does nothing else, there as here.
    assert phone.get("/dav/2024/06/IMG_0001.png", headers=_basic(key)).status_code == 403
    second = login(secondary_app.test_client(), *__import__("conftest").ADMIN)
    assert second.get("/api/admin/mugil").get_json()["phone_inbox"]["waiting"] == 1

    # The Mac wakes: the next run collects it as the phone backup it is.
    result = run(services.mugil_courier)
    assert result["inbox"]["collected"] == 1, result
    rows = conn.execute("SELECT * FROM phone_backups WHERE state IN ('staged','done')").fetchall()
    assert len(rows) == 1 and rows[0]["sha256"] and rows[0]["size"] == len(data)
    assert phone_keys.path_row(conn, _row["id"], "2024/06/IMG_0001.png") is not None
    waiting = second.get("/api/admin/mugil").get_json()["phone_inbox"]
    assert waiting["waiting"] == 0 and waiting["collected"] == 1
    assert not [p for p in (folder / ".ninaivu-phone-inbox" / "files").rglob("*") if p.is_file()]
    # The phone is still told it is safely home, and sending it again is no new file.
    assert phone.head("/dav/2024/06/IMG_0001.png", headers=_basic(key)).status_code == 200
    assert run(services.mugil_courier)["inbox"]["collected"] == 0

    # An app that sends under a temporary name and renames it when whole
    # (FolderSync's way): kept there too, and collected under its real name.
    other_photo = _png((200, 30, 10))
    assert phone.put("/dav/2024/06/IMG_0003.png.tmp", data=other_photo,
                     headers=_basic(key)).status_code == 201
    assert second.get("/api/admin/mugil").get_json()["phone_inbox"]["waiting"] == 0
    moved = phone.open("/dav/2024/06/IMG_0003.png.tmp", method="MOVE",
                       headers={**_basic(key), "Destination": "/dav/2024/06/IMG_0003.png"})
    assert moved.status_code == 201, moved.get_data(as_text=True)
    assert phone.head("/dav/2024/06/IMG_0003.png", headers=_basic(key)).status_code == 200
    assert second.get("/api/admin/mugil").get_json()["phone_inbox"]["waiting"] == 1
    assert run(services.mugil_courier)["inbox"]["collected"] == 1
    assert not list((folder / ".ninaivu-phone-inbox" / "held").iterdir())
    api_webdav.cancel_pending()
    assert phone_backup.SAFE


def test_a_one_computer_inbox_is_unchanged(app, people):
    from ninaivu.mugil import machines
    assert machines.phone_inbox_url(app.config["MV_CONFIG"]) is None


def _sent_files(services):
    with sent_record(services.cfg.state_dir) as conn:
        return {r["rel_path"]: (r["root"], r["size"], r["mtime"])
                for r in conn.execute("SELECT root, rel_path, size, mtime FROM sent")}


def test_a_new_primary_rebuilds_the_library_from_the_secondary(two_apps, tmp_path, scanned,
                                                                monkeypatch):
    from ninaivu.mugil import rebuild
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    through = partner_through(secondary_app.test_client())
    monkeypatch.setattr(rebuild, "Partner", through)
    services.mugil_rebuild.partner = through
    root = scanned[0].roots[0]
    library = library_id.read_id(root)
    sent = _sent_files(services)
    assert sent

    offered = first.get("/api/admin/mugil/rebuild").get_json()
    assert [(lib["id"], lib["was_at"], lib["files"]) for lib in offered["libraries"]] == [
        (library, root, len(sent))]

    crowded = tmp_path / "crowded"
    crowded.mkdir()
    (crowded / "note.txt").write_text("mine")
    assert first.post("/api/admin/mugil/rebuild", json={
        "library": library, "folder": str(crowded)}).status_code == 400

    new = tmp_path / "new-mac" / "Library"
    new.mkdir(parents=True)
    started = first.post("/api/admin/mugil/rebuild", json={"library": library, "folder": str(new)})
    assert started.status_code == 200, started.get_json()
    services.mugil_rebuild._thread.join(timeout=30)
    done = services.mugil_rebuild.status()
    assert done["complete"] is True and done["copied"] == len(sent) and not done["error"], done
    for rel, (from_root, size, mtime) in sent.items():
        copied = new / rel
        assert copied.read_bytes() == (Path(from_root) / rel).read_bytes()
        assert copied.stat().st_size == size
        assert abs(copied.stat().st_mtime - mtime) < 0.001
    assert library_id.read_id(new) == library
    assert str(new.resolve()) in services.cfg.roots
    assert not list(new.rglob("*.ninaivu-partial-*"))

    # Again: nothing is copied twice, and a file that is not the kept one stays.
    rel = sorted(sent)[0]
    (new / rel).write_bytes(b"something else")
    again = services.mugil_rebuild.run(library, str(new))
    assert again["already"] == len(sent) - 1 and again["left_alone"] == 1, again
    assert (new / rel).read_bytes() == b"something else"


def test_a_rebuild_waits_for_a_secondary_that_is_away(two_apps, tmp_path, scanned, monkeypatch):
    from ninaivu.mugil import rebuild
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    through = partner_through(secondary_app.test_client())
    calls = {"n": 0}

    class Flaky(through):
        def request(self, method, path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PartnerError("down", 503)
            return super().request(method, path, **kwargs)

    monkeypatch.setattr(rebuild, "AWAY_FIRST", 0.01)
    services.mugil_rebuild.partner = Flaky
    new = tmp_path / "rebuilt"
    new.mkdir()
    done = services.mugil_rebuild.run(library_id.read_id(scanned[0].roots[0]), str(new))
    assert done.get("complete") is True and done["copied"] > 0, done


def test_unpairing_waits_until_let_go_originals_are_back(two_apps, tmp_path, scanned):
    first, services, folder = _paired(two_apps, tmp_path)
    conn = db.connect(scanned[0].db_path)
    conn.execute("INSERT INTO mugil_let_go(root, rel_path, size, mtime, algo, digest, let_go_at) "
                 "VALUES ('/r', 'a.jpg', 1, 0, 'blake3', 'x', 0)")
    conn.commit()
    answer = first.post("/api/admin/mugil/unpair")
    assert answer.status_code == 409
    assert pairing.load(services.cfg.state_dir) is not None
    conn.execute("DELETE FROM mugil_let_go")
    conn.commit()
    assert first.post("/api/admin/mugil/unpair").status_code == 200


def test_only_a_primary_rebuilds(app, secondary, as_admin):
    assert as_admin.get("/api/admin/mugil/rebuild").status_code == 404
    assert as_admin.post("/api/admin/mugil/rebuild", json={"library": "x", "folder": "/x"}
                         ).status_code == 404


def test_the_checklist_reads_what_is_set_up(two_apps, tmp_path, scanned):
    from ninaivu.mugil import checklist
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    answer = first.get("/api/admin/mugil/checklist").get_json()
    items = {i["id"]: i for i in answer["items"]}
    assert items["paired"]["status"] == "done"
    assert items["secondary_seen"]["status"] == "done"
    assert items["originals"]["status"] == "done", items["originals"]
    assert items["originals"]["confirmed"] == items["originals"]["total"] > 0
    assert items["offsite"]["status"] == "todo"
    assert items["catchup"]["status"] == "todo" and items["wake"]["status"] == "off"
    assert answer["done"] < answer["of"]
    # A Secondary not heard from for two days is a problem, not a tick.
    later = time.time() + 2 * 86400
    seen = {i["id"]: i for i in checklist.primary(
        services.cfg, db.connect(scanned[0].db_path), services, now=later,
        network_wake_on=lambda: None, sign_in=lambda: None)}
    assert seen["secondary_seen"]["status"] == "problem"
    # The catch-up on, and macOS not set to wake for the network: said so.
    services.cfg.daily_catchup = True
    asleep = {i["id"]: i for i in checklist.primary(
        services.cfg, db.connect(scanned[0].db_path), services,
        network_wake_on=lambda: False, sign_in=lambda: True)}
    assert asleep["wake"]["status"] == "problem" and asleep["sign_in"]["status"] == "done"
    second = login(secondary_app.test_client(), *__import__("conftest").ADMIN)
    there = {i["id"]: i for i in second.get("/api/admin/mugil/checklist").get_json()["items"]}
    assert there["originals_folder"]["status"] == "done" and there["paired"]["status"] == "done"
    assert there["primary_seen"]["status"] == "done"


def test_one_computer_has_no_checklist(app, as_admin):
    assert as_admin.get("/api/admin/mugil/checklist").status_code == 404


def test_the_secondary_wakes_the_primary_before_its_catch_up(two_apps, tmp_path, monkeypatch):
    import datetime as dt
    from ninaivu.mugil import wake
    primary_app, secondary_app, other = two_apps
    monkeypatch.setattr(wake, "hardware_addresses", lambda: ["a4:83:e7:01:02:03"])
    first, services, folder = _paired(two_apps, tmp_path)
    # The catch-up off: the Secondary knows the address, and wakes nobody at night.
    asked = wake.load(other.state_dir)
    assert asked["macs"] == ["a4:83:e7:01:02:03"] and asked["at"] == ""
    services.cfg.daily_catchup, services.cfg.daily_catchup_at = True, "02:00"
    assert run(services.mugil_courier)["complete"] is True
    asked = wake.load(other.state_dir)
    assert asked["at"] == "01:55"
    assert wake.told(services.cfg.state_dir)["at"] == "01:55"

    sent = []
    waker = wake.Waker(other, sender=lambda packet, to: sent.append((packet, to)))
    # 01:56 on the Primary's clock, by the offset it sent: built from this
    # computer's local time instead, the test failed on the night clocks go
    # back, when 01:56 comes twice and the first one is not the one sent.
    offset = dt.timezone(dt.timedelta(seconds=wake.load(other.state_dir)["utc_offset"]))
    night = dt.datetime.now(offset).replace(hour=1, minute=56, second=0,
                                           microsecond=0).timestamp()
    waker.clock = lambda: night - 120
    assert waker.look() is False and not sent
    waker.clock = lambda: night
    assert waker.look() is True
    assert sent and all(p == b"\xff" * 6 + bytes.fromhex("a483e7010203") * 16 for p, _ in sent)
    assert {port for _, (_, port) in sent} == {7, 9}
    count = len(sent)
    assert waker.look() is False and len(sent) == count, "once a night"
    # Hours later (the Secondary was off at the time): not that night any more.
    waker.clock = lambda: night + 86400 + 3 * 3600
    assert waker.look() is False

    second = login(secondary_app.test_client(), *__import__("conftest").ADMIN)
    monkeypatch.setattr(wake, "send", lambda macs, **_: len(macs) * 2)
    now = second.post("/api/admin/mugil/wake")
    assert now.status_code == 200 and now.get_json()["sent"] == 2
    # Unpaired, neither keeps the wake.
    assert second.post("/api/admin/mugil/unpair").status_code == 200
    assert wake.load(other.state_dir) == {}


def test_a_wake_packet_is_the_address_sixteen_times():
    from ninaivu.mugil import wake
    assert wake.clean_mac("A4-83-E7-01-02-03") == "a4:83:e7:01:02:03"
    assert wake.clean_mac("00:00:00:00:00:00") is None and wake.clean_mac("nope") is None
    packet = wake.magic_packet("a4:83:e7:01:02:03")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6


def test_the_always_on_secondary_can_be_the_one_that_sends_the_off_site_copy(two_apps, tmp_path):
    from ninaivu.cloud import keyring
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    keyring.create(other.state_dir, "correct horse battery", "correct horse battery")
    other.offsite_enabled, other.offsite_kind = True, "folder"
    other.offsite_folder = str(tmp_path / "away-disk")
    Path(other.offsite_folder).mkdir()
    assert run(services.mugil_courier)["complete"] is True      # its hello says so
    items = {i["id"]: i for i in first.get("/api/admin/mugil/checklist").get_json()["items"]}
    assert items["offsite"]["status"] == "done" and items["offsite"]["sent_by"] == "secondary"
    second = login(secondary_app.test_client(), *__import__("conftest").ADMIN)
    there = {i["id"]: i for i in second.get("/api/admin/mugil/checklist").get_json()["items"]}
    assert there["offsite"]["status"] == "done"
    status = second.get("/api/offsite").get_json()
    assert status["from_the_originals"] is True and status["files"] > 0


# --- sending faster -------------------------------------------------------------

def test_several_originals_are_sent_at_once(primary, secondary, scanned, monkeypatch):
    """One at a time, the network sat idle while the Secondary synced each
    file to its disk; a few are under way together now."""
    from ninaivu.mugil import courier as courier_module

    mine, courier = primary
    cfg, conn, _ = scanned
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                         (cfg.roots[0],)).fetchone()[0]
    real = Courier._send_file
    lock = threading.Lock()
    now = {"at_once": 0, "most": 0}

    def slow(self, *args):
        with lock:
            now["at_once"] += 1
            now["most"] = max(now["most"], now["at_once"])
        try:
            time.sleep(0.05)
            return real(self, *args)
        finally:
            with lock:
                now["at_once"] -= 1

    monkeypatch.setattr(Courier, "_send_file", slow)
    result = run(courier)
    assert result["complete"] is True and result["sent"] == files
    assert 1 < now["most"] <= courier_module.SEND_AT_ONCE


def test_originals_sent_at_once_are_kept_when_windows_answers_the_long_form(
        primary, secondary, scanned, monkeypatch):
    r"""On Windows, ``realpath`` of a path not there yet answers ``\\?\C:\...``
    when another thread makes its folder between its two looks; with four
    originals arriving into the same new folder, one each run was refused as
    "not one a library holds". Here realpath answers that way for every path
    that does not exist yet, and every original is still kept."""
    from ninaivu.utils import files

    real = os.path.realpath

    def windows(path, *args, **kwargs):
        answer = real(path, *args, **kwargs)
        if answer.startswith("\\\\?\\") or os.path.exists(answer):
            return answer                 # on Windows the race itself may answer so
        return "\\\\?\\" + answer

    monkeypatch.setattr(files.os.path, "realpath", windows)
    assert files.real_path("/no/such/place") == real("/no/such/place")
    assert files.stays_inside("/no/such", "/no/such/place/photo.jpg")
    assert not files.stays_inside("/no/such", "/elsewhere/photo.jpg")
    mine, courier = primary
    cfg, conn, _ = scanned
    files_there = conn.execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                               (cfg.roots[0],)).fetchone()[0]
    result = run(courier)
    assert result["complete"] is True and result["sent"] == files_there, result


def test_a_refusal_part_way_keeps_what_was_kept(primary, secondary, scanned, monkeypatch):
    mine, courier = primary
    real = Courier._send_file
    calls = {"n": 0}
    lock = threading.Lock()

    def full_after_three(self, *args):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n > 3:
            raise PartnerError("The Secondary's disk is full.", 507)
        return real(self, *args)

    monkeypatch.setattr(Courier, "_send_file", full_after_three)
    with pytest.raises(PartnerError, match="full"):
        run(courier)
    with sent_record(mine.state_dir) as record:
        kept = record.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
    assert kept == 3


def test_files_on_their_way_in_share_the_reserve(tmp_path, monkeypatch):
    from ninaivu.mugil import hold

    monkeypatch.setattr(hold.shutil, "disk_usage",
                        lambda folder: SimpleNamespace(free=hold.RESERVE + 100))
    with hold._room_for(tmp_path, 80):
        with pytest.raises(Refused):
            with hold._room_for(tmp_path, 80):
                pass
        with hold._room_for(tmp_path, 20):
            pass
    with hold._room_for(tmp_path, 80):           # let go once it is in
        pass


# --- moving the Primary to another computer ---------------------------------

def test_the_primary_takes_over_when_its_index_moves(primary, secondary, scanned):
    """The index, pairing and all, brought to a new computer: the first time
    it reaches the Secondary it agrees a new key, and the computer it came
    from is told it has been replaced instead of going on as the Primary."""
    mine, courier = primary
    cfg, _ = secondary
    old_key = pairing.load(mine.state_dir)["key"]
    assert pairing.mark_moved(mine.state_dir) is True

    result = run(courier)
    assert result["complete"] is True

    now_primary = pairing.load(mine.state_dir)
    now_secondary = pairing.load(cfg.state_dir)
    assert "moved_in_at" not in now_primary
    assert now_primary["key"] == now_secondary["key"] != old_key
    assert now_secondary["replaced_key"] == old_key
    assert "replaced_key" not in pairing.public(now_secondary)

    # The computer the index came from, still holding the old key.
    client_partner = courier.partner("http://linux-pc:3000", old_key)
    with pytest.raises(PartnerError) as refused:
        client_partner.request("GET", "/api/mugil/hello")
    assert refused.value.status == 409 and "taken over" in str(refused.value)
    # And the new one goes on as before.
    assert run(courier)["complete"] is True


def test_a_move_answer_without_the_old_key_is_not_taken():
    private, hello = pairing.move_hello()
    found = pairing.secondary_move_answer("11" * 32, hello)
    assert found is not None
    _, answer = found
    assert pairing.primary_move_key(private, "22" * 32, answer) is None
    assert pairing.primary_move_key(private, "11" * 32, answer) == found[0]


def test_a_full_export_carries_the_pairing_and_a_backup_away_does_not(tmp_path):
    import tarfile
    from ninaivu.storage import backup

    state = tmp_path / "state"
    state.mkdir()
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(state / "index.db")) as conn, conn:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
    (state / "config.json").write_text("{}")
    pairing.save(state, {"key": "ab" * 32, "role": "primary"})
    with sent_record(state) as record:
        record.execute("INSERT INTO libraries (root, library, name) VALUES ('/p', 'lib-1', 'p')")

    def names(bundle):
        with tarfile.open(bundle) as tar:
            return {m.name for m in tar.getmembers()}

    full = backup.snapshot(state, tmp_path / "out", full=True)
    away = backup.snapshot(state, tmp_path / "away", leave_out=backup.KEPT_HOME)
    assert {"state/mugil-pair.json", "state/mugil-sent.db"} <= names(full)
    assert "state/mugil-pair.json" not in names(away)


def test_an_index_is_not_brought_to_the_secondary(tmp_path):
    import json
    from ninaivu.storage import full_export

    state = tmp_path / "state"
    state.mkdir()
    (state / "config.json").write_text(json.dumps({"mugil_role": "secondary"}))
    with pytest.raises(full_export.Refused) as refused:
        full_export.look(tmp_path / "anything.tar.gz", state)
    assert "Secondary" in str(refused.value.said)


def test_the_originals_folder_is_found_again_on_another_system(tmp_path):
    """The N300 moved from a Mac (/Volumes/…) to Linux (/media/<user>/…):
    found by its disk's id, not left saying the originals cannot be reached."""
    import json
    from ninaivu.mugil import hold

    state = tmp_path / "state"
    state.mkdir()
    old = "/Volumes/N300/Originals"
    new = tmp_path / "media" / "jaga" / "N300" / "Originals"
    new.mkdir(parents=True)
    (new / hold.DISK_ID).write_text("disk-1\n")
    (state / hold.KNOWN_DISKS).write_text(json.dumps({old: "disk-1"}))
    saved = []
    cfg = SimpleNamespace(state_dir=state, mugil_originals_dir=old,
                          save=lambda: saved.append(True))
    assert hold.find_moved(cfg, look=lambda folder: [new]) == str(new)
    assert cfg.mugil_originals_dir == str(new) and saved
    # A copy of the disk beside it: two with the id, nothing is taken.
    copy = tmp_path / "media" / "jaga" / "Copy" / "Originals"
    copy.mkdir(parents=True)
    (copy / hold.DISK_ID).write_text("disk-1\n")
    cfg.mugil_originals_dir = old
    assert hold.find_moved(cfg, look=lambda folder: [new, copy]) is None
    assert cfg.mugil_originals_dir == old


def test_a_rebuild_writes_each_name_as_the_index_spells_it(tmp_path):
    """A library from a Mac's older disk: names in the decomposed form. The
    Secondary keeps them composed; written that way on Linux they were new
    files to the index, and the photographs it knew went missing."""
    import sqlite3
    import unicodedata
    from contextlib import closing
    from ninaivu.mugil import rebuild

    base = tmp_path / "Library"
    base.mkdir()
    decomposed = unicodedata.normalize("NFD", "தொகுப்பு/கொண்டாட்டம்.jpg")
    assert decomposed != unicodedata.normalize("NFC", decomposed)
    index = tmp_path / "index.db"
    with closing(sqlite3.connect(index)) as conn, conn:
        conn.execute("CREATE TABLE assets (root TEXT, rel_path TEXT)")
        conn.executemany("INSERT INTO assets VALUES (?, ?)",
                         [(str(base), decomposed), (str(base), "plain/IMG_1.HEIC")])
    spelled = rebuild.index_spellings(SimpleNamespace(db_path=index), base)
    assert spelled == {unicodedata.normalize("NFC", decomposed): decomposed}
    assert rebuild.index_spellings(SimpleNamespace(db_path=tmp_path / "none.db"), base) == {}


# --- seamless: nobody re-pairs, unpairs or types an address -------------------

def test_a_replaced_primary_steps_down_by_itself(primary, secondary, scanned, app):
    """The index moved to another computer, which took over: the one it left
    becomes a single computer again, its library as it was, instead of being
    refused at every run until somebody unpairs it."""
    mine, courier = primary
    old_key = pairing.load(mine.state_dir)["key"]
    # The new computer: the same index and pairing, and it takes over.
    moved = copy.copy(mine)
    moved.state_dir = mine.state_dir.parent / "moved-state"
    moved.state_dir.mkdir()
    pairing.save(moved.state_dir, {"key": old_key, "role": "primary", "moved_in_at": time.time()})
    newcomer = Courier(moved, courier.connect_db, partner=courier.partner)
    assert run(newcomer)["complete"] is True

    told = []
    courier.notify = lambda event, summary, detail: told.append(event)
    courier.cfg.save = lambda: None
    courier._run_logged()
    assert courier.last.get("stepped_down") is True
    assert mine.mugil_role == "single" and mine.mugil_partner_url == ""
    assert pairing.load(mine.state_dir) is None
    assert told == ["primary_replaced"]


def test_the_primary_finds_its_secondary_at_a_new_address(primary, secondary, scanned,
                                                          monkeypatch):
    """The router gave the Secondary another address: found on the network,
    proven by the pairing's key, and spoken to there from now on."""
    from ninaivu.mugil import courier as courier_module, meeting

    mine, courier = primary
    through = courier.partner

    class Moved(through):
        def request(self, method, path, **kwargs):
            if self.url == "http://linux-pc:3000":
                raise PartnerError("The other computer did not answer: refused", 0)
            return super().request(method, path, **kwargs)

    courier.partner = Moved
    mine.save = lambda: None
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.01)
    monkeypatch.setattr(meeting, "discover", lambda **_: [
        {"id": "x", "address": "192.168.1.77", "port": 8080, "name": "Linux PC", "waiting": False}])
    result = run(courier)
    assert result["complete"] is True
    assert mine.mugil_partner_url == "http://192.168.1.77:8080"
    assert pairing.load(mine.state_dir)["partner_url"] == "http://192.168.1.77:8080"


def test_a_stranger_on_the_network_is_not_taken_for_the_secondary(primary, secondary,
                                                                  monkeypatch):
    from ninaivu.mugil import meeting

    mine, courier = primary
    through = courier.partner

    class Nobody(through):
        def request(self, method, path, **kwargs):
            if self.url != "http://linux-pc:3000":
                raise PartnerError("This request is not signed by the computer this one "
                                   "is paired with.", 401)
            raise PartnerError("The other computer did not answer: refused", 0)

    courier.partner = Nobody
    monkeypatch.setattr(meeting, "discover", lambda **_: [
        {"id": "y", "address": "192.168.1.66", "port": 8080, "name": "Other", "waiting": False}])
    partner = Nobody("http://linux-pc:3000", "ab" * 32)
    assert courier._find_again(partner, now=10_000.0) is False
    assert partner.url == "http://linux-pc:3000" and mine.mugil_partner_url == "http://linux-pc:3000"
    # And it does not look again at once.
    assert courier._find_again(partner, now=10_001.0) is False


def test_the_secondary_follows_the_primary_to_a_new_address(app, secondary):
    from ninaivu.api import mugil_api

    cfg, _ = secondary
    pairing.save(cfg.state_dir, {"key": "ab" * 32, "role": "secondary",
                                 "partner_address": "192.168.1.10",
                                 "partner_url": "http://192.168.1.10:8080"})
    with app.test_request_context("/api/mugil/hello", environ_base={"REMOTE_ADDR": "100.64.0.9"}):
        mugil_api._follow_the_primary(pairing.load(cfg.state_dir), str(cfg.state_dir))
    now = pairing.load(cfg.state_dir)
    assert now["partner_address"] == "100.64.0.9"
    assert now["partner_url"] == "http://100.64.0.9:8080"


def test_the_move_answers_only_the_paired_primary(app, secondary, client):
    """/api/mugil/move agrees a new key: unsigned, or on a computer that is
    not the Secondary, it is refused before anything is read."""
    cfg, _ = secondary
    private, hello = pairing.move_hello()
    # Not paired yet.
    assert client.post("/api/mugil/move", json=hello).status_code == 401
    pairing.save(cfg.state_dir, {"key": "ab" * 32, "role": "secondary"})
    # Paired, but not signed, or signed with another key.
    assert client.post("/api/mugil/move", json=hello).status_code == 401
    headers = pairing.signed_headers("cd" * 32, "POST", "/api/mugil/move",
                                     pairing.body_digest(__import__("json").dumps(hello).encode()))
    assert client.post("/api/mugil/move", json=hello, headers=headers).status_code == 401
    assert pairing.load(cfg.state_dir)["key"] == "ab" * 32
    # On a computer that is not the Secondary.
    cfg.mugil_role = "single"
    assert client.post("/api/mugil/move", json=hello).status_code == 404
