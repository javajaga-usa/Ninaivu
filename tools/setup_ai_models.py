"""Download the local image-editing model and the background segmentation model (20 GB cap).

    python tools/setup_ai_models.py               # what would be downloaded, and where
    python tools/setup_ai_models.py --download    # download it

Both come from the catalogue Admin -> AI models uses (ninaivu/media/model_catalog.py,
the "generative" and "segmentation" entries): every file pinned to a repository
commit and a SHA-256, and a download that does not match is discarded. This tool
used to fetch whatever a branch or an open pull request held that day, with no
hash, so two households running it a week apart could get different files.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from ninaivu.media import model_catalog  # noqa: E402
from fetch_ai_models import fetch  # noqa: E402

#: The catalogue entry for each checkpoint this tool can install. MagicBrush is a
#: fine-tune of InstructPix2Pix on human-annotated precise edits: the same
#: architecture and speed, and it follows instructions more accurately.
IMAGE_MODELS = {'magicbrush': 'generative'}
SEGMENTATION = 'segmentation'
BUDGET = 20_000_000_000
#: Kept free for the language model (about 3 GB) and metadata.
RESERVE = 3_000_000_000


def folder_bytes(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) if root.exists() else 0


def needed_bytes(model_id):
    return sum(entry['bytes'] for entry in model_catalog.MODELS[model_id]['files']
               if not (model_catalog.file_path(entry).is_file()
                       and model_catalog.file_path(entry).stat().st_size == entry['bytes']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--image-model', choices=['magicbrush', 'instructpix2pix'], default='magicbrush',
                        help='Which Generative AI checkpoint to install (only magicbrush is pinned)')
    parser.add_argument('--skip-segmentation', action='store_true',
                        help='Do not fetch the background removal/blur model')
    args = parser.parse_args(argv)
    if args.image_model not in IMAGE_MODELS:
        raise SystemExit(
            f'{args.image_model} is not fetched by this tool any more: it is not in the pinned '
            'catalogue, so its files could not be checked. Use magicbrush, or download it yourself '
            'and point "image_model" in settings.json at its folder.')
    wanted = [IMAGE_MODELS[args.image_model]] + ([] if args.skip_segmentation else [SEGMENTATION])
    root = model_catalog.models_root()
    needed = sum(needed_bytes(model_id) for model_id in wanted)
    if folder_bytes(root) + needed + RESERVE > BUDGET:
        raise SystemExit('Download would exceed the 20 GB model budget including language-model reserve.')
    print(json.dumps({'models': wanted, 'destination': str(root), 'bytes_to_download': needed,
                      'files': sum(len(model_catalog.MODELS[m]['files']) for m in wanted)}), flush=True)
    if not args.download:
        return 0
    if not all([fetch(model_id) for model_id in wanted]):
        return 1
    for model_id in wanted:
        model_catalog.apply_settings(model_id)
    settings_path = model_catalog.settings_path()
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    except ValueError:
        settings = {}
    settings.setdefault('language_model', 'qwen3:4b')
    settings['budget_bytes'] = BUDGET
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f'Local image model ({args.image_model}) installed. Runtime configuration written.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
