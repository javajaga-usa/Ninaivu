"""Nothing at the administrator-only level is read by AI.

Hidden is where a household keeps what is not for the gallery: a passport, a
bank statement, a medical letter, and every screenshot and document the
automatic rule finds. The tagger, the text reader, the face finder and the
outside describer leave those alone (tests/test_gemini_media.py checks
the last), and anything an earlier pass made of
them (tags, a caption, words read from the page, a search vector, faces) is
taken back the moment they are hidden.
"""

import json

import numpy as np

from ninaivu.media import ocr, screens
from ninaivu.media.scanner import AI_VERSION, Scanner
from ninaivu.server import auth
from ninaivu.storage import db


def open_db(cfg):
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return conn


def ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' ORDER BY id")]


def read_by_ai(conn, asset_id, *, ocr_text="passport number K1234567",
               caption="document, text", tags=("document", "text")):
    """What the passes leave behind on an item they have read."""
    conn.execute(
        "UPDATE assets SET tags=?, caption=?, ocr_text=?, ai_version=?, "
        "ocr_version=1, face_version=1 WHERE id=?",
        (json.dumps(list(tags)), caption, ocr_text, AI_VERSION, asset_id))
    conn.execute("INSERT INTO faces(asset_id, bbox, embedding, source) VALUES(?,?,?,?)",
                 (asset_id, "[0,0,1,1]", b"", "none"))
    conn.commit()
    db.store_embedding(conn, asset_id, "fake", 2,
                       np.array([1, 0], dtype=np.float32).tobytes())


def state(conn, asset_id):
    row = dict(conn.execute(
        "SELECT tags, caption, ocr_text, ai_version, ocr_version, face_version "
        "FROM assets WHERE id=?", (asset_id,)).fetchone())
    row["vector"] = conn.execute("SELECT 1 FROM embeddings WHERE asset_id=?",
                                 (asset_id,)).fetchone() is not None
    row["faces"] = [r["source"] for r in conn.execute(
        "SELECT source FROM faces WHERE asset_id=? ORDER BY id", (asset_id,))]
    return row


def found_by_words(conn, words):
    return [r[0] for r in conn.execute(
        "SELECT rowid FROM assets_fts WHERE assets_fts MATCH ?", (words,))]


# -- hiding takes back what was read --------------------------------------------

def test_hiding_an_item_takes_back_what_ai_read(scanned):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    doc, photo = ids(conn)[:2]
    read_by_ai(conn, doc)
    read_by_ai(conn, photo)
    assert doc in found_by_words(conn, "K1234567")

    db.set_visibility(conn, [doc], 2, source="item", record_undo=False)

    gone = state(conn, doc)
    assert json.loads(gone["tags"]) == []
    assert gone["caption"] is None and gone["ocr_text"] is None
    assert (gone["ai_version"], gone["ocr_version"], gone["face_version"]) == (0, 0, 0)
    assert not gone["vector"] and gone["faces"] == []
    assert doc not in found_by_words(conn, "K1234567")
    # A family photograph keeps everything.
    kept = state(conn, photo)
    assert kept["ocr_text"] and kept["vector"] and kept["faces"] == ["none"]


def test_an_admins_own_words_and_confirmed_faces_stay(scanned):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    doc = ids(conn)[0]
    read_by_ai(conn, doc)
    conn.execute("UPDATE assets SET tags='[\"visa\"]', tags_source='manual', "
                 "caption='For the visa form', caption_source='manual' WHERE id=?", (doc,))
    conn.execute("INSERT INTO faces(asset_id, bbox, embedding, source) VALUES(?,?,?,?)",
                 (doc, "[0,0,1,1]", b"", "confirmed"))
    conn.commit()

    db.set_visibility(conn, [doc], 2, source="item", record_undo=False)

    after = state(conn, doc)
    assert json.loads(after["tags"]) == ["visa"]
    assert after["caption"] == "For the visa form"
    assert after["faces"] == ["confirmed"]
    assert after["ocr_text"] is None and not after["vector"]


def test_a_description_from_the_file_is_not_taken_for_the_taggers(scanned):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    doc = ids(conn)[0]
    read_by_ai(conn, doc, caption="Grandad's discharge papers, 1946")
    db.set_visibility(conn, [doc], 2, source="item", record_undo=False)
    assert state(conn, doc)["caption"] == "Grandad's discharge papers, 1946"


def test_a_hidden_folder_takes_back_what_was_read_in_it(scanned, library):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    folder = conn.execute("SELECT folder FROM assets WHERE id=?",
                          (ids(conn)[0],)).fetchone()["folder"]
    inside = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE folder=?", (folder,))]
    for asset_id in ids(conn):
        read_by_ai(conn, asset_id)

    db.set_folder_visibility(conn, str(library), folder, 2, record_undo=False)

    for asset_id in ids(conn):
        assert (state(conn, asset_id)["ocr_text"] is None) == (asset_id in inside)


def test_a_scan_takes_back_what_an_earlier_release_read(scanned, library):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    doc = ids(conn)[0]
    # Hidden before this release, and read all the same.
    conn.execute("UPDATE assets SET visibility=2, vis_source='item' WHERE id=?", (doc,))
    conn.commit()
    read_by_ai(conn, doc)
    Scanner(cfg)._run(library, full=True)
    assert state(conn, doc)["ocr_text"] is None and not state(conn, doc)["vector"]


def test_a_picture_found_to_be_a_document_loses_its_vector(scanned, monkeypatch):
    """The search vector is how a document is recognised; once it has been,
    the vector goes with everything else."""
    cfg, _, _ = scanned
    conn = open_db(cfg)
    monkeypatch.setitem(screens.THRESHOLDS, "fake/screens", 0.8)

    class FakeEngine:
        model_id = "fake/screens"
        semantic = True
        name = "fake"

        def embed_texts(self, texts):
            return np.eye(len(texts), dtype=np.float32)

    scanner = Scanner(cfg)
    scanner.ai = FakeEngine()
    width = len(screens.SCREEN_PROMPTS) + len(screens.PHOTO_PROMPTS)
    doc, photo = ids(conn)[:2]
    for asset_id, prompt in ((doc, screens.SCREEN_PROMPTS.index("a scanned document")),
                             (photo, len(screens.SCREEN_PROMPTS))):
        vector = np.zeros(width, dtype=np.float32)
        vector[prompt] = 1.0
        db.store_embedding(conn, asset_id, "fake/screens", width, vector.tobytes())

    scanner._judge_screens(conn, str(cfg.active_root), ids=[doc, photo])

    assert state(conn, doc)["vector"] is False
    assert conn.execute("SELECT vis_source FROM assets WHERE id=?",
                        (doc,)).fetchone()[0] == "screen"
    assert state(conn, photo)["vector"] is True


# -- the passes leave hidden items alone -----------------------------------------

class RecordingEngine:
    """Tags whatever it is handed, and remembers what that was."""

    model_id = "fake/tagger"
    semantic = False
    name = "fake"
    device = "cpu"

    def __init__(self):
        self.seen = []

    def analyse(self, paths, **_):
        self.seen += [str(p) for p in paths]
        return [{"tags": ["document"], "caption": "document"} for _ in paths]


def test_tagging_skips_hidden_items(scanned, library):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    doc, *rest = ids(conn)
    db.set_visibility(conn, [doc], 2, source="item", record_undo=False)
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    engine = RecordingEngine()
    scanner = Scanner(cfg, ai=engine)
    scanner._tag(conn, str(library))

    thumb = conn.execute("SELECT thumb FROM assets WHERE id=?", (doc,)).fetchone()[0]
    assert engine.seen and not any(thumb in seen for seen in engine.seen)
    assert state(conn, doc)["ai_version"] == 0
    assert all(state(conn, i)["ai_version"] == AI_VERSION for i in rest)


def test_text_is_never_read_from_a_hidden_picture(scanned, library, monkeypatch):
    cfg, _, _ = scanned
    cfg.ocr_enabled = True
    conn = open_db(cfg)
    doc, photo = ids(conn)[:2]
    db.set_visibility(conn, [doc], 2, source="item", record_undo=False)
    read = []
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read", lambda path, **_: read.append(str(path)) or {"text": "words"})

    Scanner(cfg)._read_text(conn, str(library))

    assert read
    assert state(conn, doc)["ocr_text"] is None
    assert state(conn, photo)["ocr_text"] == "words"


def test_faces_are_not_looked_for_in_hidden_pictures(scanned, library):
    cfg, _, _ = scanned
    conn = open_db(cfg)
    doc = ids(conn)[0]
    before = db.count_assets_needing_faces(conn, str(library), 1)
    db.set_visibility(conn, [doc], 2, source="item", record_undo=False)
    assert db.count_assets_needing_faces(conn, str(library), 1) == before - 1
    assert doc not in [r["id"] for r in db.assets_needing_faces(conn, str(library), 1)]
    assert db.face_stats(conn, str(library))["photos_pending"] == before - 1


def test_a_hidden_item_does_not_count_as_waiting_for_analysis(scanned, library):
    from ninaivu.server import capacity

    cfg, _, _ = scanned
    cfg.ai_enabled = True
    conn = open_db(cfg)
    conn.execute("UPDATE assets SET ai_version=?", (AI_VERSION,))
    conn.commit()
    db.set_visibility(conn, [ids(conn)[0]], 2, source="item", record_undo=False)
    assert capacity.backlog(conn, cfg)["analysis"] == 0

