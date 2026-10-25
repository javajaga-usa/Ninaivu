"""How many pictures the underwear check would keep out of family search.

Reads the vectors already in the index (nothing is opened, nothing is
written) and scores every picture as media/revealing.py does, then prints
how many score at or over each line, beside how many the explicit check
already flags:

    python tools/try_revealing.py
    python tools/try_revealing.py --list 0.05 --count 60

``--list`` writes the paths of the highest-scoring pictures at or over that
line, and of the ones just under it, to ``revealing-check.txt`` in the
Ninaivu folder (``~/.ninaivu``), to look at in Finder or the console before
choosing the line. Needs the search model the library was indexed with.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LINES = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5)


def main(argv: list[str] | None = None) -> int:
    import numpy as np

    from ninaivu import ai
    from ninaivu.media import revealing
    from ninaivu.server.config import Config

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--list", type=float, default=None,
                        help="write the paths around this line to revealing-check.txt")
    parser.add_argument("--count", type=int, default=40, help="paths written on each side")
    args = parser.parse_args(argv)

    cfg = Config.load()
    engine = ai.build_engine(cfg)
    scorer = revealing.scorer_for(engine)
    if scorer is None:
        print(f"The search model here ({getattr(engine, 'model_id', 'none')}) is not one "
              "the underwear check reads.", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT a.id, a.root, a.rel_path, a.nsfw, a.visibility, e.vector FROM assets a "
        "JOIN embeddings e ON e.asset_id=a.id AND e.model=? "
        "WHERE a.kind='picture' AND a.trashed=0", (scorer.model_id,)).fetchall()
    if not rows:
        print("No picture in the index has a vector from this model.", file=sys.stderr)
        return 1
    scores = np.concatenate([
        scorer.score(np.frombuffer(b"".join(r[5] for r in rows[at:at + 5000]), dtype=np.float32))
        for at in range(0, len(rows), 5000)])
    nsfw = np.array([r[3] for r in rows]) == 1
    hidden = np.array([r[4] for r in rows]) >= 2
    family = ~nsfw & ~hidden
    print(f"{len(rows):,} pictures; {int(nsfw.sum()):,} already explicit, "
          f"{int(hidden.sum()):,} hidden")
    print(f"{'line':>6}  {'at or over':>10}  {'of them shown to the family':>28}")
    for line in LINES:
        over = scores >= line
        print(f"{line:>6}  {int(over.sum()):>10,}  {int((over & family).sum()):>28,}")
    if args.list is not None:
        order = np.argsort(-scores)
        above = [i for i in order if scores[i] >= args.list and family[i]][:args.count]
        below = [i for i in order if scores[i] < args.list and family[i]][:args.count]
        out = Path(cfg.db_path).parent / "revealing-check.txt"
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(f"At or over {args.list} (kept out of family search):\n")
            for i in above:
                handle.write(f"{scores[i]:.3f}  {Path(rows[i][1]) / rows[i][2]}\n")
            handle.write(f"\nJust under {args.list} (still found):\n")
            for i in below:
                handle.write(f"{scores[i]:.3f}  {Path(rows[i][1]) / rows[i][2]}\n")
        print(f"Paths written to {out}")
    close = getattr(engine, "close", None)
    if close is not None:
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
