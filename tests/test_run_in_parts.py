"""tools/run_in_parts.py: every test file runs, once, in order."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_in_parts", ROOT / "tools" / "run_in_parts.py")
run_in_parts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_in_parts)


def test_every_file_is_in_exactly_one_part_in_order():
    files = [f"tests/test_{i:03}.py" for i in range(431)]
    parts = run_in_parts.parts_of(files, 4)
    assert len(parts) == 4
    assert [f for part in parts for f in part] == files
    assert max(map(len, parts)) - min(map(len, parts)) <= 1


def test_more_parts_than_files_is_one_file_a_part():
    assert run_in_parts.parts_of(["a", "b"], 4) == [["a"], ["b"]]
    assert run_in_parts.parts_of([], 4) == []
