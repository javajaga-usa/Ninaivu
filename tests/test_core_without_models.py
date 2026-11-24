"""The core never needs a model package.

Ninaivu's core (the library, the index, people, sharing, copies, the family
app and the console) must start and answer with no AI package installed; a
model feature that is missing, deleted or broken turns itself off, and
nothing else. Two guards:

* no module imports a model package at its top level, so importing it never
  loads one (a package is imported inside the function that uses it);
* the whole app starts, and both faces answer, with every model package
  made unimportable.

OpenCV is not a model package here: it is a recommended requirement of the
core (poster frames, classical image work), and several modules import it
at the top.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "ninaivu"

#: Packages that carry a model, or run one.
MODEL_PACKAGES = frozenset({
    "torch", "torchvision", "onnxruntime", "open_clip", "insightface", "transformers",
    "sherpa_onnx", "llama_cpp", "diffusers", "timm", "safetensors", "coremltools",
    "ctranslate2", "faster_whisper", "accelerate", "sentencepiece", "tokenizers",
    "huggingface_hub", "tensorflow",
})


def _top_level_imports(tree: ast.Module):
    """Imports that run when the module is imported: not inside a function or class."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.lineno, node.module or ""
        elif isinstance(node, (ast.If, ast.Try)):
            stack += node.body + node.orelse
            for handler in getattr(node, "handlers", []):
                stack += handler.body
            stack += getattr(node, "finalbody", [])


def test_no_module_imports_a_model_package_at_its_top_level():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, name in _top_level_imports(tree):
            if name.split(".")[0] in MODEL_PACKAGES:
                found.append(f"{path.relative_to(ROOT)}:{line} imports {name}")
    assert not found, "\n".join(found)


def test_the_app_starts_and_answers_with_no_model_package_installed(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        BLOCKED = {sorted(MODEL_PACKAGES)!r}

        # None in sys.modules is how Python says "not installed": an import
        # raises ImportError and importlib.util.find_spec answers None.
        for name in BLOCKED:
            sys.modules[name] = None

        from pathlib import Path
        from ninaivu import create_home_app, create_admin_app, build_services
        from ninaivu.server.config import Config

        base = Path({str(tmp_path)!r})
        cfg = Config()
        cfg.state_dir = base / "state"
        cfg.active_root = str(base / "library")
        cfg.roots = [cfg.active_root]
        Path(cfg.active_root).mkdir()
        cfg.watch = False
        cfg.ai_enabled = False
        cfg.ensure_dirs()
        services = build_services(cfg)
        services.scanner.stop()
        for app in (create_home_app(services), create_admin_app(services)):
            client = app.test_client()
            assert client.get("/").status_code == 200
        loaded = sorted(m for m, mod in sys.modules.items()
                        if m.split(".")[0] in BLOCKED and mod is not None)
        assert not loaded, loaded
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True,
                          cwd=tmp_path, timeout=300)
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr[-3000:]
