"""What each person sees (server/privacy_view.py): the gate's answer for every
profile, and why the rest is withheld, for administrators only."""
import pytest

from ninaivu.server import auth, privacy_view, visibility
from ninaivu.storage import db


def report_for(body, name):
    return next(p for p in body["people"] if p["name"] == name)


@pytest.fixture()
def conn(people):
    return people["conn"]


def ids(conn):
    return [r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE trashed = 0 AND live_clip = 0 AND raw_twin = 0 ORDER BY id")]


def test_only_an_administrator_is_told(as_admin, as_family, as_guest, anon):
    answer = as_admin.get("/api/privacy/people")
    assert answer.status_code == 200
    body = answer.get_json()
    assert [p["role"] for p in body["people"]] == ["admin", "family", "guest"]
    assert body["reasons"] == list(privacy_view.REASONS)
    for other in (as_family, as_guest, anon):
        assert other.get("/api/privacy/people").status_code in (401, 403)


def test_counts_add_up_and_each_withheld_item_has_one_reason(as_admin, conn):
    every = ids(conn)
    assert len(every) >= 6
    a, b, c, d, e = every[:5]
    conn.execute("UPDATE assets SET nsfw = 1 WHERE id = ?", (a,))
    conn.execute("UPDATE assets SET visibility = 2, vis_source = ? WHERE id = ?", (db.SCREEN_SOURCE, b))
    conn.execute("UPDATE assets SET visibility = 2, vis_source = 'item' WHERE id = ?", (c,))
    # Explicit *and* hidden is counted once, as explicit (the first reason asked).
    conn.execute("UPDATE assets SET visibility = 2, vis_source = 'item', nsfw = 1 WHERE id = ?", (d,))
    conn.execute("UPDATE assets SET trashed = 1 WHERE id = ?", (e,))
    conn.commit()
    body = as_admin.get("/api/privacy/people").get_json()
    total = len(every) - 1

    admin = report_for(body, "Dad")
    assert admin["admin"] and admin["total"] == total and admin["sees"] == total
    assert admin["withheld_total"] == 0
    assert admin["admin_sees_hidden"] == 3 and admin["admin_sees_explicit"] == 2

    maya = report_for(body, "Maya")
    assert maya["withheld"]["explicit"] == 2
    assert maya["withheld"]["screens"] == 1
    assert maya["withheld"]["hidden"] == 1
    assert maya["sees"] + maya["withheld_total"] + maya["own_private"] == total
    assert not set(maya["sample"]) & {a, b, c, d, e}


def test_a_guest_sees_only_what_is_shared_with_guests(as_admin, conn):
    every = ids(conn)
    conn.execute("UPDATE assets SET visibility = 1")
    conn.execute("UPDATE assets SET visibility = 0 WHERE id = ?", (every[0],))
    conn.commit()
    guest = report_for(as_admin.get("/api/privacy/people").get_json(), "Neighbour")
    assert guest["sees"] == 1 and guest["sample"] == [every[0]]
    assert guest["withheld"]["hidden"] == len(every) - 1


def test_somebody_elses_only_to_me_is_withheld_and_ones_own_is_not(as_admin, conn, people):
    every = ids(conn)
    conn.execute("UPDATE assets SET visibility = 2, private_to = ? WHERE id = ?",
                 (people["admin"].id, every[0]))
    conn.execute("UPDATE assets SET visibility = 2, private_to = ? WHERE id = ?",
                 (people["family"].id, every[1]))
    conn.commit()
    maya = report_for(as_admin.get("/api/privacy/people").get_json(), "Maya")
    assert maya["withheld"]["private"] == 1
    assert maya["own_private"] == 1
    assert maya["withheld"]["hidden"] == 0


def test_a_sub_folder_keeps_the_rest_out(as_admin, conn, people):
    folders = [r[0] for r in conn.execute("SELECT DISTINCT folder FROM assets WHERE trashed = 0")]
    if len(folders) < 2:
        pytest.skip("the sample library has one folder")
    keep = sorted(folders)[0]
    conn.execute("UPDATE users SET scope = ? WHERE id = ?", (keep, people["family"].id))
    conn.commit()
    maya = report_for(as_admin.get("/api/privacy/people").get_json(), "Maya")
    assert maya["sub_folder"] == keep
    assert maya["withheld"]["folders"] > 0


def test_recordings_are_for_administrators_only(as_admin, conn):
    if not db.ADMIN_ONLY_KINDS:
        pytest.skip("no administrator-only kinds")
    every = ids(conn)
    conn.execute("UPDATE assets SET kind = ? WHERE id = ?", (db.ADMIN_ONLY_KINDS[0], every[0]))
    conn.commit()
    body = as_admin.get("/api/privacy/people").get_json()
    assert report_for(body, "Maya")["withheld"]["recordings"] == 1
    assert report_for(body, "Dad")["withheld"]["recordings"] == 0


def test_the_report_matches_the_gate_one_by_one(conn, people, app):
    cfg = app.config["MV_CONFIG"]
    every = ids(conn)
    conn.execute("UPDATE assets SET nsfw = 1 WHERE id = ?", (every[0],))
    conn.execute("UPDATE assets SET visibility = 2, vis_source = 'item' WHERE id = ?", (every[1],))
    conn.commit()
    for user in auth.list_users(conn):
        viewer = visibility.for_person(user, cfg, conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM assets WHERE trashed = 0 AND live_clip = 0 AND raw_twin = 0")]
        allowed = sum(1 for r in rows if viewer.may_see(r, own=False))
        assert privacy_view.report(conn, viewer, user)["sees"] == allowed, user.role


def test_a_kept_count_follows_every_change_the_household_makes(as_admin, conn):
    """The counts are kept between asks (0.6 s a person on 200,000 items); a
    hide, an explicit mark and a bin must still show on the very next ask."""
    every = ids(conn)
    first = as_admin.get("/api/privacy/people").get_json()
    assert as_admin.get("/api/privacy/people").get_json() == first      # kept
    maya = report_for(first, "Maya")["sees"]
    conn.execute("UPDATE assets SET visibility = 2, vis_source = 'item' WHERE id = ?", (every[0],))
    conn.commit()
    after = as_admin.get("/api/privacy/people").get_json()
    assert report_for(after, "Maya")["sees"] == maya - 1
    assert report_for(after, "Dad")["admin_sees_hidden"] == report_for(first, "Dad")["admin_sees_hidden"] + 1
    conn.execute("UPDATE assets SET nsfw = 1 WHERE id = ?", (every[1],))
    conn.commit()
    again = as_admin.get("/api/privacy/people").get_json()
    assert report_for(again, "Maya")["withheld"]["explicit"] == 1
    conn.execute("UPDATE assets SET trashed = 1 WHERE id = ?", (every[2],))
    conn.commit()
    last = as_admin.get("/api/privacy/people").get_json()
    assert report_for(last, "Dad")["total"] == report_for(again, "Dad")["total"] - 1
