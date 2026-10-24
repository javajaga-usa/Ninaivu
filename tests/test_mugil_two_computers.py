"""Two computers: pairing them, and the originals kept on the Secondary.

The Primary's originals are only read; the Secondary keeps each file only
when the bytes that arrived have the fingerprint the Primary named, and
never writes over or removes one it keeps. A single computer is untouched:
every route between the two answers 404 there.
"""

import copy
import io
import os
import time
from pathlib import Path

import pytest

from conftest import FAMILY, login
from ninaivu.mugil import pairing, role
from ninaivu.mugil.courier import Courier, Partner, sent_record
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

    def read(self, _limit=None):
        return self._data


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


def test_a_secondary_without_its_folder_is_not_sent_to(primary, secondary, as_admin):
    _, courier = primary
    cfg, folder = secondary
    cfg.mugil_originals_dir = ""
    from ninaivu.mugil.courier import PartnerError
    with pytest.raises(PartnerError):
        run(courier)


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
