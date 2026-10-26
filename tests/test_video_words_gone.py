"""What was said in videos is gone: an index from before keeps none of it."""

import sqlite3

from ninaivu.storage import db


def test_the_words_written_down_before_are_dropped(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path).close()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE video_words (asset_id INTEGER, at REAL, text TEXT);"
        "CREATE TABLE video_words_done (asset_id INTEGER PRIMARY KEY);"
        "CREATE TRIGGER video_words_follow_asset AFTER DELETE ON assets "
        "BEGIN DELETE FROM video_words WHERE asset_id = OLD.id; END;"
        "INSERT INTO video_words VALUES (1, 12.0, 'happy birthday');")
    conn.commit()
    conn.close()
    db.init_db(path).close()
    conn = sqlite3.connect(path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert not {"video_words", "video_words_done", "video_words_follow_asset"} & names
