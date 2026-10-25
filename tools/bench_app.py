#!/usr/bin/env python3
"""How long does each page of the family app and the console take to answer?

    python tools/bench_app.py --index ~/.ninaivu/index.db   # a copy of a real index
    python tools/bench_app.py [library_size]                # a made-up one (default 200,000)

Every GET route both apps answer is asked for in turn, as a signed-in
administrator and as a family member, and the time each took is printed,
slowest first, with the statements that took longest inside the slowest.

With ``--index`` the index is copied first (SQLite's own backup, so a server
using it can go on) into a throwaway folder beside a copy of its config.json,
and only the copy is read. Nothing is started: no scan, no watcher, no
backup, no model, so nothing reads or writes the library itself. Without it
a library of made-up rows is built: no files, but the rows, faces and
descriptions a real one has.

    BENCH_ONLY=/api/segments   time only the routes whose path has this in it
    BENCH_PROFILE=/api/facets  profile the routes whose path has this in it
    BENCH_REPEAT=3             ask each route this many times (best is shown)
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADMIN = ("benchadmin", "bench-admin-pass-1")
FAMILY = ("benchfamily", "bench-family-pass-1")

#: Routes not asked for: they stream, send a file, start something, or reach
#: outside this computer. What is left only reads the index and answers.
SKIP = (
    "/events", "/api/thumb", "/api/file", "/api/preview", "/api/download",
    "/api/proxy", "/api/stream", "/api/live-video", "/enhance", "/api/recycle/thumb",
    "/dav", "/api/mugil", "/api/admin/shutdown", "/api/share/", "/share/", "/ask/",
    "/api/ask/", "/static/", "/api/faces/thumb", "/api/face/", "/thumb", "/api/admin/reveal",
    "/api/admin/backups/verify", "/api/admin/digest/preview", "/api/cloud/auth",
    "/api/films/music/", "/audio", "/video", "/frame", "/export", "/zip", "/pdf",
    "/download", "/api/stories/", "/api/letters/", "/api/colour/", "/api/books/",
    "/api/films/", "/image", "/api/tv-album/", "/tv", "/crt", "/cert", "/qr",
    "/api/ai-playground", "/api/photo-talk", "/api/admin/extensions",
)

#: Searches and filters the gallery sends, each asked for as well.
QUERIES = (
    "/api/segments?sort=date_desc",
    "/api/segments?sort=date_desc&kind=video",
    "/api/assets?limit=200&offset=0&sort=date_desc",
    "/api/assets?limit=200&offset=100000&sort=date_desc",
    "/api/assets?limit=200&q=beach",
    "/api/assets?limit=200&q=2019",
    "/api/assets?limit=200&favourite=1",
    "/api/suggest?q=ma",
    "/api/geo/clusters?zoom=3",
    "/api/geo/places",
    "/api/status?stats=0",
    "/api/memories/on-this-day",
)


def copy_index(source: Path, work: Path) -> Path:
    """The index, copied page by page while whoever has it open carries on."""
    state = work / "state"
    state.mkdir(parents=True)
    target = state / "index.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    started = time.perf_counter()
    src.backup(dst, pages=4096)
    dst.close()
    src.close()
    print(f"  copied {source} ({target.stat().st_size / 1e9:.2f} GB) "
          f"in {time.perf_counter() - started:.0f} s", flush=True)
    config = source.parent / "config.json"
    if config.is_file():
        settings = json.loads(config.read_text(encoding="utf-8"))
        # Nothing that would reach another machine or write anywhere else.
        for key in ("replica_dir", "replica_of", "index_file", "cloud_enabled",
                    "offsite_enabled", "mail_enabled"):
            settings.pop(key, None)
        (state / "config.json").write_text(json.dumps(settings), encoding="utf-8")
    return state


def made_up_index(work: Path, size: int) -> Path:
    """A library of *size* rows with faces, people and descriptions, no files."""
    from PIL import Image
    from ninaivu.server.config import Config
    from ninaivu.server import auth
    from ninaivu.storage import db
    from ninaivu.media.scanner import Scanner

    state = work / "state"
    root = work / "Library"
    rng = random.Random(7)
    for i in range(40):
        folder = root / f"{2005 + i % 20}" / f"{1 + i % 12:02d}" / f"{1 + i % 28:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        img = Image.frombytes("RGB", (32, 24), rng.randbytes(32 * 24 * 3)).resize((640, 480))
        exif = img.getexif()
        exif[0x9003] = f"{2005 + i % 20}:{1 + i % 12:02d}:{1 + i % 28:02d} 10:00:00"
        img.save(folder / f"IMG_{i:04d}.jpg", "JPEG", exif=exif)
    cfg = Config()
    cfg.state_dir = state
    cfg.roots = [str(root)]
    cfg.active_root = str(root)
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.watch = False
    cfg.min_media_bytes = 0
    cfg.workers = 2
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    Scanner(cfg)._run(root, full=True)
    template = [dict(r) for r in conn.execute("SELECT * FROM assets")]
    cols = [c for c in template[0] if c != "id"]
    words = ("beach temple wedding birthday cake garden river mountain school "
             "train festival kolam saree dog cat market rain lamp").split()
    places = [("India", "Chennai"), ("India", "Madurai"), ("USA", "Boston"),
              ("India", "Coimbatore"), ("Singapore", "Singapore"), (None, None)]
    rows = []
    for n in range(len(template), size):
        row = dict(template[n % len(template)])
        year = 1990 + n % 36
        month = 1 + (n // 36) % 12
        day = 1 + (n // 432) % 28
        folder = f"{year}/{month:02d}/{day:02d}"
        row.update(
            rel_path=f"{folder}/IMG_{n:06d}.jpg", filename=f"IMG_{n:06d}.jpg",
            folder=folder, captured_at=datetime(year, month, day, n % 24).timestamp(),
            date_key=f"{year}-{month:02d}-{day:02d}",
            kind="video" if n % 9 == 0 else "picture",
            ext=".mp4" if n % 9 == 0 else ".jpg",
            phash=f"{rng.getrandbits(64):016x}", dup_group=None,
            tags=" ".join(rng.sample(words, 3)),
            visibility=2 if n % 7 == 0 else 1,
            gps_lat=12.9 + rng.random() if n % 3 == 0 else None,
            gps_lon=80.1 + rng.random() if n % 3 == 0 else None,
            size=rng.randint(200_000, 9_000_000),
        )
        row["country"], row["city"] = places[n % len(places)]
        rows.append(tuple(row[c] for c in cols))
    with db._write_lock:
        conn.executemany(f"INSERT INTO assets ({', '.join(cols)}) VALUES "
                         f"({', '.join('?' for _ in cols)})", rows)
        for p in range(24):
            conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES (?, ?, 0)",
                         (p + 1, f"Person {p + 1}" if p < 18 else ""))
        ids = [r[0] for r in conn.execute("SELECT id FROM assets WHERE kind='picture'")]
        faces = []
        for n, asset in enumerate(ids):
            for _ in range(n % 3):
                person = rng.randint(1, 24) if rng.random() < 0.6 else None
                faces.append((asset, person, "auto" if person else "none",
                              "[0.1,0.1,0.3,0.3]", rng.randbytes(2048)))
        conn.executemany("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                         "VALUES (?, ?, ?, ?, ?)", faces)
        try:
            conn.executemany(
                "INSERT INTO descriptions(asset_id, text, model, version, made_at) "
                "VALUES (?, ?, 'captions-large', 1, 0)",
                [(a, f"A photograph of a {' and a '.join(rng.sample(words, 2))}.")
                 for a in ids[: int(len(ids) * 0.85)]])
        except sqlite3.OperationalError:
            pass
        conn.commit()
    db.refresh_statistics(conn, force=True)
    (state / "config.json").write_text(json.dumps(
        {"roots": [str(root)], "active_root": str(root), "watch": False,
         "ai_enabled": False, "ai_engine": "off"}), encoding="utf-8")
    db.close_all()
    print(f"  made up {size:,} rows, {len(faces):,} faces", flush=True)
    return state


def build_apps(state: Path):
    os.environ["NINAIVU_STATE_DIR"] = str(state)
    from ninaivu import Services, create_admin_app, create_home_app
    from ninaivu.server import auth, date_policy
    from ninaivu.server.config import Config
    from ninaivu.storage import db

    cfg = Config.load()
    cfg.state_dir = state
    cfg.watch = False
    cfg.debug = False
    started = time.perf_counter()
    services = Services(cfg)
    home = create_home_app(services)
    admin = create_admin_app(services)
    print(f"  services and both apps built in {(time.perf_counter() - started) * 1000:.0f} ms")
    conn = db.connect(cfg.db_path)
    auth.init_auth_schema(conn)
    for (name, password), role in ((ADMIN, auth.ROLE_ADMIN), (FAMILY, auth.ROLE_FAMILY)):
        if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            auth.create_user(conn, name, password, display_name=name, role=role)
    if not db.get_meta(conn, date_policy.KEY):
        db.set_meta(conn, date_policy.KEY, json.dumps(dict(
            date_policy.DEFAULTS, admin="all", family="all", guest="all")))
    return services, home, admin


def signed_in(app, who):
    client = app.test_client()
    answer = client.post("/api/auth/login", json={"username": who[0], "password": who[1]})
    if answer.status_code != 200:
        raise SystemExit(f"could not sign in as {who[0]}: {answer.status_code} "
                         f"{answer.get_data(as_text=True)[:200]}")
    return client


def routes(app, sample_id: int, person_id: int | None, album_id: int | None) -> list[str]:
    found = []
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or ()) or rule.endpoint == "static":
            continue
        path = rule.rule
        if any(s in path for s in SKIP):
            continue
        args = {}
        for name in rule.arguments:
            conv = rule._converters.get(name)
            kind = type(conv).__name__ if conv else ""
            if "Integer" not in kind:
                break
            if "asset" in name or name == "id":
                args[name] = sample_id
            elif "person" in name and person_id:
                args[name] = person_id
            elif "album" in name and album_id:
                args[name] = album_id
            else:
                break
        else:
            try:
                found.append(rule.build(args, append_unknown=False)[1])
            except Exception:                         # noqa: BLE001
                pass
    return sorted(set(found))


def ask(client, path: str, repeat: int, conn_path: Path):
    from ninaivu.storage import db
    best = None
    first = None
    status = 0
    statements: list[tuple[float, str]] = []
    for attempt in range(repeat):
        seen: list[tuple[float, str]] = []
        conn = db.connect(conn_path)
        if attempt == 0:
            conn.set_trace_callback(lambda sql, seen=seen: seen.append((time.perf_counter(), sql)))
        profile = os.environ.get("BENCH_PROFILE")
        prof = cProfile.Profile() if profile and profile in path and attempt == 0 else None
        started = time.perf_counter()
        try:
            if prof:
                prof.enable()
            answer = client.get(path)
            # A stream (an event feed) never ends: only its first part.
            body = next(iter(answer.response), b"") if answer.is_streamed else answer.get_data()
            answer.close()
            status = answer.status_code
        except Exception as exc:                      # noqa: BLE001
            status = f"raised {type(exc).__name__}: {exc}"[:80]
            body = b""
        finally:
            if prof:
                prof.disable()
        took = time.perf_counter() - started
        conn.set_trace_callback(None)
        if attempt == 0:
            seen.append((time.perf_counter(), "<end>"))
            statements = sorted(((seen[i + 1][0] - seen[i][0], seen[i][1])
                                 for i in range(len(seen) - 1)), reverse=True)[:6]
            first_count = len(seen) - 1
        if prof:
            out = io.StringIO()
            pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(40)
            print(out.getvalue())
        first = took if first is None else first
        best = took if best is None else min(best, took)
    return first, best, status, len(body), statements, first_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("size", nargs="?", type=int, default=200_000)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--keep", type=Path, help="work here and leave it behind")
    parser.add_argument("--state", type=Path, help="a state folder made by an earlier --keep")
    args = parser.parse_args()
    repeat = max(1, int(os.environ.get("BENCH_REPEAT", "2")))
    only = os.environ.get("BENCH_ONLY", "")
    work = args.keep or Path(tempfile.mkdtemp(prefix="ninaivu-bench-app-"))
    try:
        if args.state:
            state = args.state
        elif args.index:
            source = args.index.expanduser().resolve()
            state = copy_index(source, work)
        else:
            state = made_up_index(work, args.size)
        services, home, admin = build_apps(state)
        from ninaivu.storage import db
        conn = db.connect(services.cfg.db_path)
        total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        sample = conn.execute("SELECT id FROM assets WHERE kind='picture' "
                              "ORDER BY id LIMIT 1 OFFSET ?", (total // 2,)).fetchone()
        person = conn.execute("SELECT person_id FROM faces WHERE person_id IS NOT NULL "
                              "GROUP BY person_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        album = conn.execute("SELECT id FROM albums LIMIT 1").fetchone()
        sample_id = sample[0] if sample else 1
        print(f"  {total:,} rows in the index\n", flush=True)
        results = []
        for face, app, who in (("family app, admin", home, ADMIN),
                               ("family app, family member", home, FAMILY),
                               ("console", admin, ADMIN)):
            client = signed_in(app, who)
            paths = routes(app, sample_id, person and person[0], album and album[0])
            if face.startswith("family"):
                paths += list(QUERIES)
            for path in paths:
                if only and only not in path:
                    continue
                first, took, status, size, statements, count = ask(
                    client, path, repeat, services.cfg.db_path)
                results.append((first, took, face, path, status, size, statements, count))
                if first > 1.0:
                    print(f"  … {first * 1000:8.0f} ms  {face:<26} {path}", flush=True)
        results.sort(key=lambda r: r[0], reverse=True)
        print(f"\n  {'first ms':>9} {'best ms':>8}  {'sql':>5}  {'status':<6} {'kB':>7}  "
              "where / route")
        for first, took, face, path, status, size, _, count in results:
            print(f"  {first * 1000:9.1f} {took * 1000:8.1f}  {count:5d}  {str(status):<6} "
                  f"{size / 1024:7.1f}  {face:<26} {path}")
        print("\n  inside the slowest (the first time each was asked):")
        for first, _, face, path, _, _, statements, _ in results[:20]:
            print(f"\n  {first * 1000:8.1f} ms  {face}  {path}")
            for spent, sql in statements[:5]:
                print(f"      {spent * 1000:8.1f} ms  {' '.join(sql.split())[:160]}")
        return 0
    finally:
        try:
            from ninaivu.storage import db
            db.close_all()
        except Exception:                             # noqa: BLE001
            pass
        if not args.keep and not args.state:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
