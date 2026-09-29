import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _models_stay_off_this_machine(request, tmp_path: Path):
    """No test may reach the developer's own AI model folder.

    Every model resolves through ``model_catalog.models_root()``, which
    defaults to the ``.ai-models`` folder beside the application — and on a
    machine that has been running Ninaivu, that folder has the real models in
    it. The suite then quietly started loading a 77 MB orientation model and
    running inference on every fixture photograph, which took the full run
    from twelve minutes to over forty and made it depend on what happened to
    be on disk. An empty folder per test is what the suite always assumed it
    had, back when these models lived in the state directory.

    Tests that are *about* the model folder configure it themselves.
    """
    from ninaivu.media import model_catalog

    if request.node.get_closest_marker("real_models"):
        yield                       # it means the ones on this machine
        return
    before = model_catalog._root
    model_catalog.configure(tmp_path / "ai-models")
    yield
    model_catalog.configure(before)


@pytest.fixture(autouse=True)
def _sign_in_limits_start_empty():
    """Rate limits and PIN lockouts are process-wide; one test's guesses must
    not pause the next test's profile."""
    from ninaivu.api import accounts_api
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()
    yield
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()


@pytest.fixture(autouse=True)
def _archive_media_floor():
    """Reset the engine's size floor around every test.

    ``MIN_MEDIA_BYTES`` is a module global that ``archive.configure()`` writes,
    so one test setting it leaked into every test that ran afterwards — the
    engine suite passed only when something else happened to run first and set
    it to 0. Tests that are *about* the floor set it themselves.
    """
    from ninaivu.archive import scanner as archive_scanner

    before = archive_scanner.MIN_MEDIA_BYTES
    archive_scanner.MIN_MEDIA_BYTES = 0
    yield
    archive_scanner.MIN_MEDIA_BYTES = before


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    """A small library with distinct folders, so scope can be tested."""
    root = tmp_path / "lib"
    base = datetime(2023, 5, 12, 10, 30)

    for i in range(6):
        when = base + timedelta(days=i * 30)
        folder = root / when.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (400 + i * 20, 300), (20 * i, 90, 200 - 10 * i))
        exif = img.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "TestCam"
        exif[0x010F] = "Acme"
        img.save(folder / f"shot{i}.jpg", "JPEG", exif=exif)

    # A folder an admin would plausibly want hidden.
    private = root / "private"
    private.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        Image.new("RGB", (300, 200), (200, 30 * i, 10)).save(private / f"secret{i}.jpg")

    # A folder to scope a guest to.
    shared = root / "shared/holiday"
    shared.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        Image.new("RGB", (320, 240), (10, 200, 30 * i)).save(shared / f"beach{i}.jpg")

    loose = root / "misc"
    loose.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 200), (10, 10, 10)).save(loose / "plain.png")

    shutil.copy(root / "2023/05/12/shot0.jpg", root / "2023/05/12/shot0_copy.jpg")
    (root / "notes.txt").write_text("not media")
    return root


@pytest.fixture()
def cfg(tmp_path: Path, library: Path):
    from ninaivu.server.config import Config

    config = Config()
    config.state_dir = tmp_path / "state"
    config.roots = [str(library)]
    config.active_root = str(library)
    config.ai_enabled = False
    config.ai_engine = "off"
    # Services points every model at this; see `_models_stay_off_this_machine`
    # for why it must not be allowed to fall back to the real folder.
    config.ai_models_dir = str(tmp_path / "ai-models")
    config.watch = False
    # The cloud's plumbing is tested with a fake Drive and no key; the tests
    # about encryption (test_cloud_encryption.py) make a key and switch it on
    # themselves, and test_the_backup_is_encrypted_by_default checks the
    # shipped default.
    config.cloud_encrypt = False
    config.workers = 2
    config.open_browsing = True
    # The fixtures draw 400x300 solid-colour JPEGs a few kilobytes each. Real
    # photographs are never that small, which is exactly why the 60 KB floor
    # exists — but applying it here would mean the whole suite indexed nothing.
    # Tests that care about the floor set it themselves.
    config.min_media_bytes = 0
    config.ensure_dirs()
    return config


@pytest.fixture()
def scanned(cfg):
    """Config with a completed synchronous scan."""
    from ninaivu.server import auth
    from ninaivu.storage import db
    from ninaivu.media.scanner import Scanner

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    # Legacy role/scope tests exercise the unrestricted date-range setting.
    # test_date_access removes this setting to verify the production defaults.
    from ninaivu.server import date_policy
    import json
    db.set_meta(conn, date_policy.KEY, json.dumps(dict(
        date_policy.DEFAULTS, admin="all", family="all", guest="all")))
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=True)
    return cfg, conn, scanner


@pytest.fixture()
def app(scanned):
    from ninaivu import create_app

    cfg, _, _ = scanned
    cfg.watch = False
    application = create_app(cfg)
    application.config["MV_SCANNER"].stop()
    yield application
    # Everything the app started, stopped — not only the scanner. A background
    # thread that outlived its test went on running into the next one, and
    # because the Archive's database path is set process-wide, one that opened
    # its first connection late landed on the *next* test's brand-new
    # archive.db and held it while that test tried to set it up. That was the
    # "database is locked" a full run produced now and then and a single file
    # never did.
    services = application.config.get("MV_SERVICES")
    if services is not None:
        services.stop(timeout=5.0)


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Role fixtures
# ---------------------------------------------------------------------------

ADMIN = ("dad", "correcthorse1")
FAMILY = ("maya", "summerdays24")
GUEST = ("neighbour", "visitingpass9")


def login(client, username, password):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return client


@pytest.fixture()
def people(app, scanned):
    """One profile per role."""
    from ninaivu.server import auth

    _, conn, _ = scanned
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    family = auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                              role=auth.ROLE_FAMILY, created_by=admin.id)
    guest = auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                             role=auth.ROLE_GUEST, created_by=admin.id)
    return {"admin": admin, "family": family, "guest": guest, "conn": conn}


@pytest.fixture()
def as_admin(app, people):
    return login(app.test_client(), *ADMIN)


@pytest.fixture()
def as_family(app, people):
    return login(app.test_client(), *FAMILY)


@pytest.fixture()
def as_guest(app, people):
    return login(app.test_client(), *GUEST)


@pytest.fixture()
def anon(app, people):
    """Not signed in at all — open browsing."""
    return app.test_client()


def ids_of(client, path="/api/assets?limit=200"):
    return [item["id"] for item in client.get(path).get_json()["items"]]
