-- Ninaivu v3.7.0: the index schema as init_db at that tag made it, one statement per object (a virtual table's own shadow tables left out).
PRAGMA user_version = 0;
CREATE TABLE album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (album_id, asset_id)
);
CREATE TABLE albums (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    cover_id   INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    -- Who made it. NULL means "made before Ninaivu recorded this", which is
    -- treated as everyone's, so upgrading does not orphan existing albums.
    created_by INTEGER
);
CREATE TABLE ask_answers (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES ask_questions(id) ON DELETE CASCADE,
    face_id     INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    -- What the answerer called themselves, if anything. Not an account, and
    -- not checked: it is there so the review can say "Paati says".
    answered_by TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    -- pending | accepted | rejected | closed (another answer for the same
    -- face was accepted)
    status      TEXT NOT NULL DEFAULT 'pending',
    person_id   INTEGER REFERENCES people_clusters(id) ON DELETE SET NULL,
    reviewed_by INTEGER,
    reviewed_at REAL
);
CREATE TABLE ask_faces (
    question_id      INTEGER NOT NULL REFERENCES ask_questions(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    face_id          INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    asked_visibility INTEGER NOT NULL,
    PRIMARY KEY (question_id, position)
);
CREATE TABLE ask_questions (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    -- A profile id, without a foreign key: the index is also opened where
    -- there is no users table, and a key to one made every delete of a
    -- photograph fail there. auth.delete_user clears it instead, as it does
    -- for share links, so a reused id cannot bring a link back to life.
    created_by  INTEGER,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    show_photo  INTEGER NOT NULL DEFAULT 0,
    revoked_at  REAL
);
CREATE TABLE assets (
    id              INTEGER PRIMARY KEY,
    root            TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    filename        TEXT NOT NULL,
    folder          TEXT NOT NULL DEFAULT '',
    ext             TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    mtime           REAL    NOT NULL DEFAULT 0,
    captured_at     REAL,
    date_key        TEXT NOT NULL DEFAULT '',
    date_source     TEXT NOT NULL DEFAULT 'mtime',
    width           INTEGER,
    height          INTEGER,
    duration        REAL,
    orientation     INTEGER DEFAULT 1,
    camera          TEXT,
    lens            TEXT,
    iso             INTEGER,
    f_number        REAL,
    exposure        TEXT,
    focal_length    REAL,
    gps_lat         REAL,
    gps_lon         REAL,
    country         TEXT,
    city            TEXT,
    thumb           TEXT,
    blurhash        TEXT,
    color           TEXT,
    phash           TEXT,
    dup_group       TEXT,
    -- Focus and exposure, measured at a fixed size so one threshold holds
    -- across cameras. NULL means never measured (video, audio, or a scan
    -- with quality_scan off) -- distinct from a genuinely dark photograph.
    sharpness       REAL,
    brightness      REAL,
    quality         TEXT NOT NULL DEFAULT '[]',
    quality_version INTEGER NOT NULL DEFAULT 0,
    -- Which pass of the video keyframe tagger judged this clip. Videos are
    -- tagged from several moments rather than one poster frame, and that
    -- sampling evolves independently of the image tagger's AI_VERSION.
    keyframe_version INTEGER NOT NULL DEFAULT 0,
    -- Words read out of the picture itself: a shop sign, a menu, the back of
    -- a postcard. Indexed for search, never shown as a caption -- it is what
    -- the photograph contains, not what it is about.
    ocr_text        TEXT,
    ocr_version     INTEGER NOT NULL DEFAULT 0,
    -- Which pass of the place-name lookup filled `city` and `country`.
    place_version   INTEGER NOT NULL DEFAULT 0,
    -- The auto-album this belongs to, rebuilt after every scan. Not written
    -- by the asset upsert: it is derived from the whole library at once, not
    -- from one file, and a rescan must not blank it on the way past.
    occasion_id     INTEGER,
    tags            TEXT NOT NULL DEFAULT '[]',
    caption         TEXT,
    nsfw            INTEGER NOT NULL DEFAULT 0,
    nsfw_score      REAL NOT NULL DEFAULT 0,
    -- Who decided the three above: 'auto' (the scan or the AI pass) or
    -- 'manual' (an admin, by hand). A manual value outlives every rescan and
    -- re-tag, the way a manual rotation or date does.
    tags_source     TEXT NOT NULL DEFAULT 'auto',
    caption_source  TEXT NOT NULL DEFAULT 'auto',
    nsfw_source     TEXT NOT NULL DEFAULT 'auto',
    -- How much the picture looks like a screenshot, a document or a screen,
    -- 0..1, and which version of that judgement made it (media/screens.py).
    screen_score    REAL,
    screen_version  INTEGER NOT NULL DEFAULT 0,
    -- How much the picture looks like people in nothing but their underwear,
    -- 0..1, and which version of that judgement made it (media/revealing.py).
    revealing       REAL NOT NULL DEFAULT 0,
    revealing_version INTEGER NOT NULL DEFAULT 0,
    -- 0 public (guests too) · 1 family · 2 hidden (admins only)
    visibility      INTEGER NOT NULL DEFAULT 1,
    vis_source      TEXT NOT NULL DEFAULT 'default',
    -- "Only to me": the profile that took this item out of everybody else's
    -- view, and what its visibility was before, to put back when they bring
    -- it back. While set, the item's visibility is held at hidden (2), so
    -- every listing that already keeps hidden items from family members and
    -- guests keeps this one from them too; only its owner reaches it, from
    -- their own "Only to me" view (see :func:`private_owner_clause`).
    private_to      INTEGER,
    private_was     INTEGER,
    private_was_src TEXT,
    -- Clockwise turn applied to get this the right way up, and who decided:
    -- exif · faces · ai · manual · none. Thumbnails are baked with it already
    -- applied; this is what the browser needs to show the original to match.
    rotation        INTEGER NOT NULL DEFAULT 0,
    rot_source      TEXT NOT NULL DEFAULT 'none',
    live_video_path TEXT,
    is_live         INTEGER NOT NULL DEFAULT 0,
    -- Set on the *clip* half of a live photo, so the pair counts as the one
    -- thing the household took rather than as a photograph and a video.
    live_clip       INTEGER NOT NULL DEFAULT 0,
    -- A RAW taken together with a JPEG or HEIC of the same frame: the picture
    -- carries the RAW's path, the RAW is marked as the twin. One shot, shown
    -- once, with the RAW kept beside it to download.
    raw_path        TEXT,
    raw_twin        INTEGER NOT NULL DEFAULT 0,
    -- Photo stacks (``photo_stacks``, storage/stacks.py): the frames of a
    -- burst and a photograph's edited copies share a key; the one shown
    -- carries how many there are, the rest are marked as stacked under it.
    -- Written only by storage/stacks.py, never by a scan.
    stack_key       TEXT,
    stacked         INTEGER NOT NULL DEFAULT 0,
    stack_count     INTEGER NOT NULL DEFAULT 0,
    trashed         INTEGER NOT NULL DEFAULT 0,
    ai_version      INTEGER NOT NULL DEFAULT 0,
    face_version    INTEGER NOT NULL DEFAULT 0,
    indexed_at      REAL NOT NULL DEFAULT 0,
    -- Set the moment an admin says "keep" on the largest-files screen. NULL
    -- forever means nobody has looked at this file there yet -- it is not a
    -- judgement about the file, only about whether it still needs asking
    -- about, so nothing else in Ninaivu ever reads or changes it.
    large_file_reviewed_at REAL,
    -- When this file was first indexed. Unlike indexed_at (the thumbnail's
    -- version, moved by an edit, a turn or a redrawn tile) it never changes
    -- on a rescan: "what is new" without the change journal reads it.
    added_at        REAL, location_inferred INTEGER NOT NULL DEFAULT 0,
    UNIQUE(root, rel_path)
);
CREATE VIRTUAL TABLE assets_fts USING fts5(
    filename, folder, tags, caption, camera, ocr_text, city,
    content='assets', content_rowid='id', tokenize='unicode61'
);
CREATE TABLE bitrot_records (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    root          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    expected_hash TEXT,
    actual_hash   TEXT,
    -- baseline   first sighting; nothing to compare against yet
    -- verified   the bytes are the same as last time
    -- changed    different bytes, but the file was edited (mtime/size moved)
    -- corrupt    different bytes with the file otherwise untouched
    -- missing    not on disk
    -- unreadable on disk but could not be opened
    status        TEXT NOT NULL,
    -- What the file looked like when this fingerprint was taken. Without
    -- these, a changed hash is ambiguous: an edit and bit rot produce exactly
    -- the same evidence, and reporting an edit as corruption teaches people
    -- to ignore the warning.
    file_mtime    REAL,
    file_size     INTEGER,
    checked_at    REAL NOT NULL
);
CREATE TABLE capsule_items (
    capsule_id  INTEGER NOT NULL REFERENCES capsules(id) ON DELETE CASCADE,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (capsule_id, asset_id)
);
CREATE TABLE capsules (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    letter      TEXT NOT NULL DEFAULT '',
    -- The local day it opens, YYYY-MM-DD.
    opens_on    TEXT NOT NULL,
    -- Who it is for; NULL is everybody in the family.
    for_user    INTEGER,
    created_by  INTEGER,
    -- The name it was sealed under, as the household reads it.
    from_name   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    -- When whoever it is for first opened it (for everybody: the first of
    -- them). Until then the family app says it is waiting to be opened.
    opened_at   REAL
);
CREATE TABLE colour_copies (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
    made_by    INTEGER,
    made_at    REAL NOT NULL
);
CREATE TABLE content_fingerprints (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    taken_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE descriptions (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    text       TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 0,
    failures   INTEGER NOT NULL DEFAULT 0,
    tried      INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    made_at    REAL NOT NULL
);
CREATE VIRTUAL TABLE descriptions_fts USING fts5(
    text, content='descriptions', content_rowid='asset_id', tokenize='unicode61'
);
CREATE TABLE embeddings (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
);
CREATE TABLE erased (
    root      TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    erased_at REAL NOT NULL,
    PRIMARY KEY (root, rel_path, size)
);
CREATE TABLE face_rejections (
    face_id    INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (face_id, person_id)
);
CREATE TABLE faces (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    person_id   INTEGER REFERENCES people_clusters(id) ON DELETE SET NULL,
    -- How this face came to be attached to that person:
    --   none      nobody yet
    --   auto      the matcher was confident enough to decide alone
    --   confirmed a person said yes
    -- Only 'confirmed' rows build a centroid, so one bad automatic guess
    -- cannot teach itself into the model of who somebody is.
    source      TEXT NOT NULL DEFAULT 'none',
    confidence  REAL NOT NULL DEFAULT 0,
    cluster_key TEXT,
    bbox        TEXT NOT NULL,
    landmarks   TEXT,
    det_score   REAL NOT NULL DEFAULT 0,
    sharpness   REAL NOT NULL DEFAULT 0,
    quality     REAL NOT NULL DEFAULT 0,
    embedding   BLOB NOT NULL,
    thumb       TEXT,
    model       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE faces_ignored (
    face_id    INTEGER PRIMARY KEY REFERENCES faces(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE faces_not_carried (
    id         INTEGER PRIMARY KEY,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    bbox       TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE handover_claims (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    requested_at  REAL NOT NULL,
    due_at        REAL NOT NULL,
    state         TEXT NOT NULL DEFAULT 'waiting',
    ended_at      REAL NOT NULL DEFAULT 0,
    ended_by      INTEGER
);
CREATE TABLE handover_plan (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    plan          TEXT NOT NULL DEFAULT '{}',
    wait_days     INTEGER NOT NULL DEFAULT 7,
    reviewed_at   REAL NOT NULL DEFAULT 0,
    reviewed_by   INTEGER,
    destinations  TEXT NOT NULL DEFAULT '',
    code_hash     TEXT NOT NULL DEFAULT '',
    code_made_at  REAL NOT NULL DEFAULT 0,
    code_made_by  INTEGER
);
CREATE TABLE letter_words (
    asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    words       TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'ta',
    source      TEXT NOT NULL DEFAULT 'typed',   -- 'read' by the text reader, or 'typed'
    written_by  INTEGER,
    written_at  REAL NOT NULL,
    reading     TEXT NOT NULL DEFAULT ''         -- {"key", "duration", "segments", "voices"}
);
CREATE TABLE library_journal (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL    NOT NULL,
    kind      TEXT    NOT NULL,
    asset_id  INTEGER,
    root      TEXT    NOT NULL DEFAULT '',
    rel_path  TEXT    NOT NULL DEFAULT '',
    folder    TEXT    NOT NULL DEFAULT '',
    old_path  TEXT    NOT NULL DEFAULT '',
    item_kind TEXT    NOT NULL DEFAULT '',
    size      INTEGER NOT NULL DEFAULT 0,
    digest    TEXT    NOT NULL DEFAULT '',
    visibility INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE life_story_words (
    person_id   INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    language    TEXT NOT NULL,
    facts       TEXT NOT NULL,      -- media.life_story.fingerprint
    written     TEXT NOT NULL,      -- {"opening", "chapters": [{"title", "text"}]}
    model       TEXT NOT NULL DEFAULT '',
    written_at  REAL NOT NULL,
    PRIMARY KEY (person_id, language, facts)
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE mugil_let_go (
    root      TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    algo      TEXT NOT NULL,
    digest    TEXT NOT NULL,
    let_go_at REAL NOT NULL,
    PRIMARY KEY (root, rel_path)
);
CREATE TABLE occasions (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    key        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    place      TEXT,
    started_at REAL NOT NULL DEFAULT 0,
    ended_at   REAL NOT NULL DEFAULT 0,
    days       INTEGER NOT NULL DEFAULT 1,
    count      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(root, key)
);
CREATE TABLE pending_uploads (
    id INTEGER PRIMARY KEY,
    storage_key TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    root TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    uploaded_by INTEGER,
    uploaded_at REAL NOT NULL,
    record TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    reviewed_at REAL,
    asset_id INTEGER
);
CREATE TABLE people_clusters (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    avatar_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    created_at      REAL NOT NULL,
    -- The mean of every *confirmed* face, re-normalised. Suggestions are
    -- ranked against this, so it is rebuilt on every confirm and reject
    -- rather than cached and left to drift.
    centroid        BLOB,
    face_count      INTEGER NOT NULL DEFAULT 0,
    confirmed_count INTEGER NOT NULL DEFAULT 0,
    cover_face_id   INTEGER,
    updated_at      REAL NOT NULL DEFAULT 0
);
CREATE TABLE person_born (
    person_id  INTEGER PRIMARY KEY REFERENCES people_clusters(id) ON DELETE CASCADE,
    year       INTEGER NOT NULL,
    set_by     INTEGER,            -- a profile id
    set_at     REAL NOT NULL
);
CREATE TABLE person_relations (
    id          INTEGER PRIMARY KEY,
    person_a    INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    person_b    INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('parent', 'spouse')),
    created_by  INTEGER,            -- a profile id; see ask_family.SCHEMA
    created_at  REAL NOT NULL,
    CHECK (person_a <> person_b),
    UNIQUE (person_a, person_b, kind)
);
CREATE TABLE photo_books (
    id           INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    subtitle     TEXT NOT NULL DEFAULT '',
    source_kind  TEXT NOT NULL DEFAULT '',
    source_id    INTEGER,
    template     TEXT NOT NULL DEFAULT 'plain',
    size         TEXT NOT NULL DEFAULT 'a4-portrait',
    bleed        INTEGER NOT NULL DEFAULT 0,
    captions     INTEGER NOT NULL DEFAULT 0,
    item_ids     TEXT NOT NULL DEFAULT '[]',
    pages        INTEGER NOT NULL DEFAULT 0,
    file         TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'building',
    error        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE TABLE recycled (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER,
    root        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    thumb       TEXT,
    bin_path    TEXT NOT NULL,
    deleted_at  REAL NOT NULL,
    deleted_by  INTEGER,
    restored_at REAL
, metadata TEXT);
CREATE TABLE scan_runs (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    total      INTEGER NOT NULL DEFAULT 0,
    processed  INTEGER NOT NULL DEFAULT 0,
    added      INTEGER NOT NULL DEFAULT 0,
    updated    INTEGER NOT NULL DEFAULT 0,
    removed    INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE screening_holds (
    asset_id       INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    was_visibility INTEGER,
    was_source     TEXT
);
CREATE TABLE screening_requests (
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    -- A profile id, without a foreign key, as for ask_questions: the index is
    -- also opened where there is no users table. auth.delete_user clears it,
    -- and the request stays: what was asked about is still worth a look.
    user_id    INTEGER,
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE shares (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    scope       TEXT NOT NULL DEFAULT 'album', -- 'album' | 'asset'
    target_id   INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    expires_at  REAL,
    password    TEXT,
    view_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE smart_albums (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    rules       TEXT NOT NULL DEFAULT '{}',
    shared      INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER,
    created_at  REAL NOT NULL
);
CREATE TABLE stack_choices (
    key        TEXT PRIMARY KEY,
    -- The photograph somebody put on top, or NULL for the sharpest.
    cover      INTEGER,
    -- 1: shown as separate photographs, as somebody asked.
    apart      INTEGER NOT NULL DEFAULT 0,
    chosen_by  INTEGER,
    chosen_at  REAL NOT NULL
);
CREATE TABLE stories (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    file        TEXT NOT NULL,
    mime        TEXT NOT NULL,
    duration    REAL,
    size        INTEGER NOT NULL DEFAULT 0,
    speaker     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    created_by  INTEGER,
    created_at  REAL NOT NULL
);
CREATE TABLE stories_gone (
    file     TEXT PRIMARY KEY,
    gone_at  REAL NOT NULL
);
CREATE TABLE stories_left (
    file        TEXT PRIMARY KEY,
    root        TEXT NOT NULL DEFAULT '',
    rel_path    TEXT NOT NULL DEFAULT '',
    speaker     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    created_at  REAL
);
CREATE TABLE story_words (
    story_id   INTEGER PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    language   TEXT NOT NULL DEFAULT '',
    words      TEXT NOT NULL DEFAULT '',
    english    TEXT NOT NULL DEFAULT '',
    segments   TEXT NOT NULL DEFAULT '[]',
    model      TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 0,
    failures   INTEGER NOT NULL DEFAULT 0,
    tried      INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    made_at    REAL NOT NULL
);
CREATE TABLE video_moments (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    at       REAL NOT NULL,
    vector   BLOB NOT NULL,
    PRIMARY KEY (asset_id, at)
);
CREATE TABLE video_moments_done (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    model    TEXT NOT NULL DEFAULT '',
    version  INTEGER NOT NULL DEFAULT 0,
    mtime    REAL NOT NULL DEFAULT 0,
    moments  INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    error    TEXT NOT NULL DEFAULT '',
    made_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE visibility_batches (
    id          INTEGER PRIMARY KEY,
    created_at  REAL NOT NULL,
    created_by  INTEGER,
    scope       TEXT NOT NULL,          -- 'folder' | 'items'
    root        TEXT,
    folder      TEXT,
    visibility  INTEGER NOT NULL,
    affected    INTEGER NOT NULL DEFAULT 0,
    exposed     INTEGER NOT NULL DEFAULT 0,   -- became visible to more people
    had_rule    INTEGER NOT NULL DEFAULT 0,   -- was there a folder rule before?
    prior_rule  INTEGER,                      -- …and what was it
    undone_at   REAL
);
CREATE TABLE visibility_undo (
    batch_id   INTEGER NOT NULL REFERENCES visibility_batches(id) ON DELETE CASCADE,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    visibility INTEGER NOT NULL,
    vis_source TEXT NOT NULL,
    PRIMARY KEY (batch_id, asset_id)
);
CREATE INDEX idx_album_items_asset ON album_items(asset_id);
CREATE INDEX idx_ask_answers_status ON ask_answers(status, question_id);
CREATE INDEX idx_ask_faces_face ON ask_faces(face_id);
CREATE INDEX idx_assets_aiver    ON assets(ai_version);
CREATE INDEX idx_assets_audio    ON assets(root, id) WHERE kind='audio';
CREATE INDEX idx_assets_date     ON assets(root, date_key, trashed, nsfw, visibility, kind, live_clip, raw_twin, stacked);
CREATE INDEX idx_assets_dupgroup ON assets(dup_group) WHERE dup_group IS NOT NULL;
CREATE INDEX idx_assets_filter   ON assets(root, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
CREATE INDEX idx_assets_folder   ON assets(root, folder);
CREATE INDEX idx_assets_gallery  ON assets(root, COALESCE(captured_at, mtime) DESC, id DESC, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
CREATE INDEX idx_assets_gps      ON assets(root, gps_lat, gps_lon, trashed, nsfw, visibility, kind, date_key, (thumb IS NOT NULL));
CREATE INDEX idx_assets_kind     ON assets(root, kind);
CREATE INDEX idx_assets_live     ON assets(root, is_live);
CREATE INDEX idx_assets_name     ON assets(root, filename COLLATE NOCASE, id, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
CREATE INDEX idx_assets_occasion ON assets(occasion_id);
CREATE INDEX idx_assets_phash    ON assets(phash);
CREATE INDEX idx_assets_private  ON assets(private_to) WHERE private_to IS NOT NULL;
CREATE INDEX idx_assets_quality  ON assets(quality_version) WHERE kind='picture';
CREATE INDEX idx_assets_raws     ON assets(root) WHERE COALESCE(raw_path,'')<>'';
CREATE INDEX idx_assets_size     ON assets(root, size, id, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
CREATE INDEX idx_assets_stack    ON assets(stack_key) WHERE stack_key IS NOT NULL;
CREATE INDEX idx_assets_stills   ON assets(root) WHERE is_live=1;
CREATE INDEX idx_assets_unread   ON assets(root, trashed) WHERE thumb IS NOT NULL AND width IS NULL;
CREATE INDEX idx_assets_vis      ON assets(root, visibility);
CREATE INDEX idx_bitrot_asset    ON bitrot_records(asset_id, id);
CREATE INDEX idx_bitrot_status   ON bitrot_records(status, checked_at DESC);
CREATE INDEX idx_capsules_for ON capsules(for_user, opens_on);
CREATE INDEX idx_colour_copies_source ON colour_copies(source_id);
CREATE INDEX idx_embeddings_asset ON embeddings(asset_id);
CREATE INDEX idx_embeddings_model ON embeddings(model);
CREATE INDEX idx_faces_asset    ON faces(asset_id);
CREATE INDEX idx_faces_cluster  ON faces(cluster_key);
CREATE INDEX idx_faces_person   ON faces(person_id, asset_id);
CREATE INDEX idx_faces_unnamed  ON faces(person_id, quality DESC);
CREATE INDEX idx_fingerprints_digest ON content_fingerprints(digest, size);
CREATE INDEX idx_handover_claims_state ON handover_claims(state, due_at);
CREATE INDEX idx_journal_asset ON library_journal(asset_id);
CREATE INDEX idx_journal_at ON library_journal(at);
CREATE INDEX idx_pending_uploads_status ON pending_uploads(status, id);
CREATE INDEX idx_person_relations_b ON person_relations(person_b);
CREATE INDEX idx_photo_books_owner ON photo_books(owner_id, created_at DESC);
CREATE INDEX idx_recycled_when   ON recycled(restored_at, deleted_at DESC);
CREATE UNIQUE INDEX idx_screening_asset_user
    ON screening_requests(asset_id, user_id);
CREATE INDEX idx_screening_created
    ON screening_requests(created_at);
CREATE INDEX idx_shares_target   ON shares(scope, target_id);
CREATE INDEX idx_stories_asset ON stories(asset_id);
CREATE INDEX idx_visundo_asset   ON visibility_undo(asset_id);
CREATE TRIGGER album_items_generation_delete AFTER DELETE ON album_items BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'albums_generation'; END;
CREATE TRIGGER album_items_generation_insert AFTER INSERT ON album_items BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'albums_generation'; END;
CREATE TRIGGER album_items_generation_update AFTER UPDATE ON album_items WHEN OLD.album_id IS NOT NEW.album_id OR OLD.asset_id IS NOT NEW.asset_id OR OLD.added_at IS NOT NEW.added_at BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'albums_generation'; END;
CREATE TRIGGER assets_ad AFTER DELETE ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, filename, folder, tags, caption,
                           camera, ocr_text, city)
    VALUES ('delete', old.id, old.filename, old.folder, old.tags, old.caption,
            old.camera, old.ocr_text, old.city);
END;
CREATE TRIGGER assets_ai AFTER INSERT ON assets BEGIN
    INSERT INTO assets_fts(rowid, filename, folder, tags, caption, camera, ocr_text, city)
    VALUES (new.id, new.filename, new.folder, new.tags, new.caption, new.camera,
            new.ocr_text, new.city);
END;
CREATE TRIGGER assets_au AFTER UPDATE OF
    filename, folder, tags, caption, camera, ocr_text, city ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, filename, folder, tags, caption,
                           camera, ocr_text, city)
    VALUES ('delete', old.id, old.filename, old.folder, old.tags, old.caption,
            old.camera, old.ocr_text, old.city);
    INSERT INTO assets_fts(rowid, filename, folder, tags, caption, camera, ocr_text, city)
    VALUES (new.id, new.filename, new.folder, new.tags, new.caption, new.camera,
            new.ocr_text, new.city);
END;
CREATE TRIGGER assets_generation_delete AFTER DELETE ON assets BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_generation'; END;
CREATE TRIGGER assets_generation_insert AFTER INSERT ON assets BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_generation'; END;
CREATE TRIGGER assets_generation_update AFTER UPDATE OF root, rel_path, folder, kind, size, visibility, trashed, nsfw, nsfw_source, revealing, date_key, camera, dup_group, is_live, live_clip, raw_twin, stacked, stack_count ON assets WHEN OLD.root IS NOT NEW.root OR OLD.rel_path IS NOT NEW.rel_path OR OLD.folder IS NOT NEW.folder OR OLD.kind IS NOT NEW.kind OR OLD.size IS NOT NEW.size OR OLD.visibility IS NOT NEW.visibility OR OLD.trashed IS NOT NEW.trashed OR OLD.nsfw IS NOT NEW.nsfw OR OLD.nsfw_source IS NOT NEW.nsfw_source OR OLD.revealing IS NOT NEW.revealing OR OLD.date_key IS NOT NEW.date_key OR OLD.camera IS NOT NEW.camera OR OLD.dup_group IS NOT NEW.dup_group OR OLD.is_live IS NOT NEW.is_live OR OLD.live_clip IS NOT NEW.live_clip OR OLD.raw_twin IS NOT NEW.raw_twin OR OLD.stacked IS NOT NEW.stacked OR OLD.stack_count IS NOT NEW.stack_count BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_generation'; END;
CREATE TRIGGER assets_high_water AFTER DELETE ON assets
WHEN OLD.id >= COALESCE((SELECT CAST(value AS INTEGER) FROM meta
                         WHERE key='assets_high_water'), 0)
BEGIN
    INSERT OR REPLACE INTO meta(key, value) VALUES ('assets_high_water', OLD.id);
END;
CREATE TRIGGER assets_layout_generation_update AFTER UPDATE OF width, height, indexed_at, rotation, color, duration, thumb ON assets WHEN OLD.width IS NOT NEW.width OR OLD.height IS NOT NEW.height OR OLD.indexed_at IS NOT NEW.indexed_at OR OLD.rotation IS NOT NEW.rotation OR OLD.color IS NOT NEW.color OR OLD.duration IS NOT NEW.duration OR OLD.thumb IS NOT NEW.thumb BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_layout_generation'; END;
CREATE TRIGGER assets_occasions_generation_update AFTER UPDATE OF occasion_id, thumb ON assets WHEN OLD.occasion_id IS NOT NEW.occasion_id OR (OLD.thumb IS NULL) <> (NEW.thumb IS NULL) BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'occasions_generation'; END;
CREATE TRIGGER assets_places_generation_update AFTER UPDATE OF gps_lat, gps_lon, city, country, thumb ON assets WHEN OLD.gps_lat IS NOT NEW.gps_lat OR OLD.gps_lon IS NOT NEW.gps_lon OR OLD.city IS NOT NEW.city OR OLD.country IS NOT NEW.country OR (OLD.thumb IS NULL) <> (NEW.thumb IS NULL) BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'places_generation'; END;
CREATE TRIGGER assets_scan_generation_delete AFTER DELETE ON assets BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_scan_generation'; END;
CREATE TRIGGER assets_scan_generation_insert AFTER INSERT ON assets BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_scan_generation'; END;
CREATE TRIGGER assets_scan_generation_update AFTER UPDATE OF root, rel_path, kind, trashed, nsfw, visibility, vis_source, is_live, live_video_path, live_clip, raw_path, raw_twin, screen_score, screen_version, captured_at, mtime, gps_lat, gps_lon, city ON assets WHEN OLD.root IS NOT NEW.root OR OLD.rel_path IS NOT NEW.rel_path OR OLD.kind IS NOT NEW.kind OR OLD.trashed IS NOT NEW.trashed OR OLD.nsfw IS NOT NEW.nsfw OR OLD.visibility IS NOT NEW.visibility OR OLD.vis_source IS NOT NEW.vis_source OR OLD.is_live IS NOT NEW.is_live OR OLD.live_video_path IS NOT NEW.live_video_path OR OLD.live_clip IS NOT NEW.live_clip OR OLD.raw_path IS NOT NEW.raw_path OR OLD.raw_twin IS NOT NEW.raw_twin OR OLD.screen_score IS NOT NEW.screen_score OR OLD.screen_version IS NOT NEW.screen_version OR OLD.captured_at IS NOT NEW.captured_at OR OLD.mtime IS NOT NEW.mtime OR OLD.gps_lat IS NOT NEW.gps_lat OR OLD.gps_lon IS NOT NEW.gps_lon OR OLD.city IS NOT NEW.city BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_scan_generation'; END;
CREATE TRIGGER assets_seen_generation_delete AFTER DELETE ON assets BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_seen_generation'; END;
CREATE TRIGGER assets_seen_generation_update AFTER UPDATE OF visibility, nsfw, trashed ON assets WHEN OLD.visibility IS NOT NEW.visibility OR OLD.nsfw IS NOT NEW.nsfw OR OLD.trashed IS NOT NEW.trashed BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_seen_generation'; END;
CREATE TRIGGER assets_tags_generation_update AFTER UPDATE OF tags ON assets WHEN OLD.tags IS NOT NEW.tags BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'assets_tags_generation'; END;
CREATE TRIGGER capsule_items_go_with_asset AFTER DELETE ON assets
BEGIN DELETE FROM capsule_items WHERE asset_id = OLD.id; END;
CREATE TRIGGER capsule_items_go_with_capsule AFTER DELETE ON capsules
BEGIN DELETE FROM capsule_items WHERE capsule_id = OLD.id; END;
CREATE TRIGGER colour_copies_follow_asset AFTER DELETE ON assets
BEGIN DELETE FROM colour_copies WHERE asset_id = OLD.id OR source_id = OLD.id; END;
CREATE TRIGGER descriptions_ad AFTER DELETE ON descriptions BEGIN
    INSERT INTO descriptions_fts(descriptions_fts, rowid, text)
    VALUES ('delete', old.asset_id, old.text);
END;
CREATE TRIGGER descriptions_ai AFTER INSERT ON descriptions BEGIN
    INSERT INTO descriptions_fts(rowid, text) VALUES (new.asset_id, new.text);
END;
CREATE TRIGGER descriptions_au AFTER UPDATE OF text ON descriptions BEGIN
    INSERT INTO descriptions_fts(descriptions_fts, rowid, text)
    VALUES ('delete', old.asset_id, old.text);
    INSERT INTO descriptions_fts(rowid, text) VALUES (new.asset_id, new.text);
END;
CREATE TRIGGER descriptions_follow_asset AFTER DELETE ON assets
BEGIN DELETE FROM descriptions WHERE asset_id = OLD.id; END;
CREATE TRIGGER embeddings_generation_delete AFTER DELETE ON embeddings BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'embeddings_generation'; END;
CREATE TRIGGER embeddings_generation_insert AFTER INSERT ON embeddings BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'embeddings_generation'; END;
CREATE TRIGGER face_groups_generation_hide AFTER INSERT ON faces_ignored BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'face_groups_generation'; END;
CREATE TRIGGER face_groups_generation_show AFTER DELETE ON faces_ignored BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'face_groups_generation'; END;
CREATE TRIGGER face_groups_generation_update AFTER UPDATE OF cluster_key ON faces WHEN OLD.cluster_key IS NOT NEW.cluster_key BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'face_groups_generation'; END;
CREATE TRIGGER faces_generation_delete AFTER DELETE ON faces BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'faces_generation'; END;
CREATE TRIGGER faces_generation_insert AFTER INSERT ON faces BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'faces_generation'; END;
CREATE TRIGGER faces_generation_update AFTER UPDATE OF asset_id, person_id, quality, source ON faces WHEN OLD.asset_id IS NOT NEW.asset_id OR OLD.person_id IS NOT NEW.person_id OR OLD.quality IS NOT NEW.quality OR OLD.source IS NOT NEW.source BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'faces_generation'; END;
CREATE TRIGGER keep_private_hidden
            AFTER UPDATE OF visibility ON assets
            WHEN NEW.private_to IS NOT NULL AND NEW.visibility < 2
            BEGIN UPDATE assets SET visibility=2, vis_source='private' WHERE id=NEW.id; END;
CREATE TRIGGER occasions_generation_delete AFTER DELETE ON occasions BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'occasions_generation'; END;
CREATE TRIGGER occasions_generation_insert AFTER INSERT ON occasions BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'occasions_generation'; END;
CREATE TRIGGER occasions_generation_update AFTER UPDATE ON occasions WHEN OLD.id IS NOT NEW.id OR OLD.root IS NOT NEW.root OR OLD.key IS NOT NEW.key OR OLD.title IS NOT NEW.title OR OLD.place IS NOT NEW.place OR OLD.started_at IS NOT NEW.started_at OR OLD.ended_at IS NOT NEW.ended_at OR OLD.days IS NOT NEW.days OR OLD.count IS NOT NEW.count BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'occasions_generation'; END;
CREATE TRIGGER people_generation_delete AFTER DELETE ON people_clusters BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'faces_generation'; END;
CREATE TRIGGER people_generation_insert AFTER INSERT ON people_clusters BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'faces_generation'; END;
CREATE TRIGGER people_generation_update AFTER UPDATE OF name, cover_face_id, avatar_asset_id ON people_clusters WHEN OLD.name IS NOT NEW.name OR OLD.cover_face_id IS NOT NEW.cover_face_id OR OLD.avatar_asset_id IS NOT NEW.avatar_asset_id BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'faces_generation'; END;
CREATE TRIGGER revoke_album_shares AFTER DELETE ON albums
            BEGIN UPDATE shares SET target_id=-1 WHERE scope='album' AND target_id=OLD.id; END;
CREATE TRIGGER revoke_asset_shares AFTER DELETE ON assets
            BEGIN UPDATE shares SET target_id=-1 WHERE scope='asset' AND target_id=OLD.id; END;
CREATE TRIGGER revoke_asset_shares_on_hide
            AFTER UPDATE OF visibility, nsfw ON assets
            WHEN (NEW.visibility >= 2 AND OLD.visibility < 2)
              OR (COALESCE(NEW.nsfw, 0) <> 0 AND COALESCE(OLD.nsfw, 0) = 0)
            BEGIN UPDATE shares SET target_id=-1 WHERE scope='asset' AND target_id=NEW.id; END;
CREATE TRIGGER stacks_loosen_on_change
AFTER UPDATE OF trashed, visibility, nsfw, private_to, dup_group, root, folder, date_key, kind, live_clip, raw_twin ON assets
WHEN OLD.stack_key IS NOT NULL AND (OLD.trashed IS NOT NEW.trashed OR OLD.visibility IS NOT NEW.visibility OR OLD.nsfw IS NOT NEW.nsfw OR OLD.private_to IS NOT NEW.private_to OR OLD.dup_group IS NOT NEW.dup_group OR OLD.root IS NOT NEW.root OR OLD.folder IS NOT NEW.folder OR OLD.date_key IS NOT NEW.date_key OR OLD.kind IS NOT NEW.kind OR OLD.live_clip IS NOT NEW.live_clip OR OLD.raw_twin IS NOT NEW.raw_twin)
BEGIN UPDATE assets SET stacked = 0, stack_count = 0 WHERE stack_key = OLD.stack_key; END;
CREATE TRIGGER stacks_loosen_on_delete
AFTER DELETE ON assets WHEN OLD.stack_key IS NOT NULL
BEGIN UPDATE assets SET stacked = 0, stack_count = 0 WHERE stack_key = OLD.stack_key; END;
CREATE TRIGGER stories_follow_asset AFTER DELETE ON assets
BEGIN DELETE FROM stories WHERE asset_id = OLD.id; END;
CREATE TRIGGER stories_leave_file AFTER DELETE ON stories
BEGIN
    INSERT OR IGNORE INTO stories_gone(file, gone_at)
    VALUES (OLD.file, CAST(strftime('%s', 'now') AS REAL));
END;
CREATE TRIGGER stories_note_asset BEFORE DELETE ON assets
BEGIN
    INSERT OR REPLACE INTO stories_left(file, root, rel_path, speaker, text, created_at)
    SELECT file, OLD.root, OLD.rel_path, speaker, text, created_at
    FROM stories WHERE asset_id = OLD.id;
END;
CREATE TRIGGER story_words_follow_story AFTER DELETE ON stories
BEGIN DELETE FROM story_words WHERE story_id = OLD.id; END;
CREATE TRIGGER video_moments_follow_asset AFTER DELETE ON assets
BEGIN
    DELETE FROM video_moments WHERE asset_id = OLD.id;
    DELETE FROM video_moments_done WHERE asset_id = OLD.id;
END;
