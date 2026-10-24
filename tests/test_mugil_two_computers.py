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
        self.response = self.client.open(path, method=method, query_string=query,
                                          data=data, headers=headers)

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


def test_the_primary_makes_display_copies_after_sending(two_apps, tmp_path):
    _first, services, _ = _paired(two_apps, tmp_path)
    maker = services.mugil_display
    assert maker.wanted()
    while maker.run_once():
        pass
    status = maker.status()
    assert status["owed"] == 0
    assert status["made"] + status["kept"] + status["larger"] + status["failed"] > 0
    assert status["failed"] == 0, status
    assert first_made_is_avif(services.cfg.state_dir) if status["made"] else True


def first_made_is_avif(state_dir):
    from ninaivu.mugil import display
    with display.ledger(state_dir) as conn:
        row = conn.execute("SELECT file, kind FROM copies WHERE outcome='made' LIMIT 1").fetchone()
    return row["file"].endswith(".avif" if row["kind"] == "photo" else ".mp4")


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
