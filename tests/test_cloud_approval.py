"""Large files wait for an administrator's approval before they go to the cloud."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, login
from ninaivu import build_services, create_admin_app
from ninaivu.cloud import approvals, store
from ninaivu.server import auth
from ninaivu.storage import copies

BIG = "misc/big-noise.png"


@pytest.fixture()
def console(scanned):
    cfg, conn, scanner = scanned
    cfg.watch = False
    noise = (np.random.default_rng(3).random((1200, 1200, 3)) * 255).astype("uint8")
    Image.fromarray(noise).save(Path(cfg.active_root) / BIG)       # about 4 MB
    scanner._run(Path(cfg.active_root), full=False)
    cfg.cloud_approval_mb = 1                                        # 1 MB, for the test
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    services.cloud.queue_library()
    yield client, cfg, services, conn
    services.stop(timeout=5.0)


def _state(conn, rel=BIG):
    row = conn.execute("SELECT state, error, id FROM cloud_uploads WHERE rel_path=?", (rel,)).fetchone()
    return row["state"], row["error"], row["id"]


def test_one_gigabyte_is_the_limit_unless_changed():
    from ninaivu.server.config import Config
    assert Config().cloud_approval_mb == 1024


def test_a_large_file_is_held_just_before_it_would_go(console):
    client, cfg, services, conn = console
    row = dict(conn.execute("SELECT * FROM cloud_uploads WHERE rel_path=?", (BIG,)).fetchone())
    engine = services.cloud.engine()
    assert engine._one(conn, None, row) == "skipped"                   # noqa: SLF001
    state, error, _ = _state(conn)
    assert state == store.SKIPPED and error.startswith(approvals.REASON)
    small = dict(conn.execute("SELECT * FROM cloud_uploads WHERE rel_path='misc/plain.png'").fetchone())
    assert services.cloud._kept_back(small) is None                    # noqa: SLF001


def test_what_is_already_queued_is_held_at_once_and_released_by_approval(console):
    client, cfg, services, conn = console
    # Held as it was queued (the fixture queued the library), so nothing is left to hold.
    assert _state(conn)[0] == store.SKIPPED
    assert services.cloud.apply_approvals()["held"] == 0
    listed = client.get("/api/cloud/approvals").get_json()
    assert [i["path"] for i in listed["items"]] == [BIG] and listed["waiting"] == 1
    upload_id = listed["items"][0]["id"]
    after = client.post("/api/cloud/approvals", json={"ids": [upload_id], "decision": "approved"}).get_json()
    assert after["waiting"] == 0
    assert _state(conn)[0] == store.PENDING
    row = dict(conn.execute("SELECT * FROM cloud_uploads WHERE rel_path=?", (BIG,)).fetchone())
    assert services.cloud._kept_back(row) is None, "approved: it may go"   # noqa: SLF001


def test_an_approval_is_for_that_file_at_that_size(console):
    client, cfg, services, conn = console
    services.cloud.apply_approvals()
    upload_id = _state(conn)[2]
    client.post("/api/cloud/approvals", json={"ids": [upload_id], "decision": "approved"})
    with open(Path(cfg.active_root) / BIG, "ab") as grow:
        grow.write(b"\0" * 1000)
    row = dict(conn.execute("SELECT * FROM cloud_uploads WHERE rel_path=?", (BIG,)).fetchone())
    assert services.cloud._kept_back(row).startswith(approvals.REASON), "changed: asked again"  # noqa: SLF001


def test_a_declined_file_stays_out_and_is_listed_as_declined(console):
    client, cfg, services, conn = console
    services.cloud.apply_approvals()
    upload_id = _state(conn)[2]
    client.post("/api/cloud/approvals", json={"ids": [upload_id], "decision": "declined"})
    state, error, _ = _state(conn)
    assert state == store.SKIPPED and error.startswith(approvals.DECLINED)
    row = dict(conn.execute("SELECT * FROM cloud_uploads WHERE rel_path=?", (BIG,)).fetchone())
    assert services.cloud._kept_back(row).startswith(approvals.DECLINED)   # noqa: SLF001
    declined = client.get("/api/cloud/approvals?declined=1").get_json()
    assert [i["path"] for i in declined["items"]] == [BIG]
    reasons = copies.summary(conn, [cfg.active_root])["single_reasons"]
    assert reasons["declined"]["files"] == 1


def test_raising_the_limit_releases_and_zero_turns_it_off(console):
    client, cfg, services, conn = console
    services.cloud.apply_approvals()
    client.post("/api/cloud/approvals", json={"limit_mb": 0})
    assert cfg.cloud_approval_mb == 0 and _state(conn)[0] == store.PENDING
    client.post("/api/cloud/approvals", json={"limit_mb": 1})
    assert _state(conn)[0] == store.SKIPPED


def test_only_an_administrator_decides(console, scanned):
    client, cfg, services, conn = console
    assert client.post("/api/cloud/approvals", json={"ids": ["x"], "decision": "approved"}).status_code == 400
    assert client.post("/api/cloud/approvals", json={"ids": [1], "decision": "maybe"}).status_code == 400
    assert client.post("/api/cloud/approvals", json={"limit_mb": -5}).status_code == 400
    # A family member cannot sign in to the console at all, and the family app
    # has no such route.
    from ninaivu import create_home_app
    family = login(create_home_app(services).test_client(), *FAMILY)
    assert family.get("/api/cloud/approvals").status_code in (401, 403, 404)
    assert family.post("/api/cloud/approvals", json={"limit_mb": 0}).status_code in (401, 403, 404)
    assert cfg.cloud_approval_mb == 1


def test_the_status_says_how_many_wait(console):
    client, cfg, services, conn = console
    services.cloud.apply_approvals()
    status = services.cloud.status()["approvals"]
    assert status["limit_mb"] == 1 and status["waiting"] == 1 and status["waiting_bytes"] > 1024 * 1024


def test_queueing_the_library_again_leaves_a_held_file_held(console):
    """The queue is offered the library again and again; a file waiting for
    approval must not be put back in it each time."""
    client, cfg, services, conn = console
    services.cloud.apply_approvals()
    services.cloud.queue_library()
    assert _state(conn)[0] == store.SKIPPED
    client.post("/api/cloud/approvals", json={"ids": [_state(conn)[2]], "decision": "approved"})
    services.cloud.queue_library()
    assert _state(conn)[0] == store.PENDING


def test_a_declined_file_stays_declined_when_the_library_is_queued_again(console):
    client, cfg, services, conn = console
    client.post("/api/cloud/approvals", json={"ids": [_state(conn)[2]], "decision": "declined"})
    services.cloud.queue_library()
    state, error, _ = _state(conn)
    assert state == store.SKIPPED and error.startswith(approvals.DECLINED), error
    assert client.get("/api/cloud/approvals").get_json()["waiting"] == 0
