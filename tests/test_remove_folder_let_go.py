"""Removing a library folder whose originals were let go of (two computers).

Those originals are kept only on the Secondary and are sent for by their
rows here: removing the folder forgets the rows, and the console used to say
only "your files are not touched". Now it says how many are away first, and
removes the folder only once that is agreed to (audit of 2026-10-25).
"""

from __future__ import annotations

from pathlib import Path

from ninaivu.media.scanner import Scanner

from test_nested_library_folders import nested, photo  # noqa: F401 - a fixture used by name

REL = "2023/05/06/s0.jpg"


def _away_folder(nested) -> str:  # noqa: F811
    other = Path(nested["master"]).parent / "Elsewhere"
    photo(other / REL, 120)
    photo(other / "2023/05/06/s1.jpg", 160)
    nested["cfg"].roots.append(str(other))
    Scanner(nested["cfg"])._run(other, full=True)               # noqa: SLF001
    nested["conn"].execute(
        "INSERT INTO mugil_let_go (root, rel_path, size, mtime, algo, digest, let_go_at) "
        "VALUES (?,?,1,1.0,'blake3','x',1.0)", (str(other), REL))
    nested["conn"].commit()
    return str(other)


def test_removing_a_folder_says_how_many_originals_are_away(nested):  # noqa: F811
    other = _away_folder(nested)
    # Agreeing to anything else asked about the folder is not agreeing to this.
    response = nested["console"].delete("/api/admin/libraries",
                                        query_string={"path": other, "force": 1})
    assert response.status_code == 409
    body = response.get_json()
    assert body["let_go"] == 1 and "only on the Secondary" in body["error"]
    assert body["params"] == {"count": "1", "name": "Elsewhere"}
    assert other in nested["cfg"].roots

    response = nested["console"].delete("/api/admin/libraries",
                                        query_string={"path": other, "let_go": 1})
    assert response.status_code == 200, response.get_json()
    assert other not in nested["cfg"].roots
    assert not nested["conn"].execute("SELECT 1 FROM assets WHERE root=?", (other,)).fetchone()


def test_a_folder_with_nothing_away_is_not_asked_about(nested):  # noqa: F811
    other = _away_folder(nested)
    nested["conn"].execute("DELETE FROM mugil_let_go")
    nested["conn"].commit()
    response = nested["console"].delete("/api/admin/libraries", query_string={"path": other})
    assert response.status_code == 200, response.get_json()


def test_a_folded_folder_still_brings_them_back_instead(nested, monkeypatch):  # noqa: F811
    # The Archive is inside the Master: its let-go originals come back first
    # (nested_roots.bring_back_first), so the warning is not shown for it.
    from ninaivu.mugil import free
    nested["conn"].execute(
        "INSERT INTO mugil_let_go (root, rel_path, size, mtime, algo, digest, let_go_at) "
        "VALUES (?,?,1,1.0,'blake3','x',1.0)", (nested["archive"], "2024/01/02/p0.jpg"))
    nested["conn"].commit()
    monkeypatch.setattr(free, "bring_back", lambda cfg, conn, root, rel, **kw: False)
    response = nested["console"].delete("/api/admin/libraries",
                                        query_string={"path": nested["archive"], "force": 1})
    assert response.status_code == 409
    assert "held" in response.get_json() and "let_go" not in response.get_json()
