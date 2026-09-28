"""Download the local image pipeline's safetensors and the background segmentation model (20 GB cap)."""
import argparse
import json
from pathlib import Path
from huggingface_hub import HfApi, hf_hub_download
try:
    from huggingface_hub.errors import RevisionNotFoundError
except ImportError:  # older huggingface_hub versions exposed this under .utils instead
    from huggingface_hub.utils import RevisionNotFoundError

ROOT = Path(__file__).resolve().parents[1] / '.ai-models'
FOLDERS = {'feature_extractor', 'safety_checker', 'scheduler', 'text_encoder', 'tokenizer', 'unet', 'vae'}

#: Two interchangeable checkpoints for the same StableDiffusionInstructPix2PixPipeline — same
#: architecture and speed either way. MagicBrush is a fine-tune of the original on MagicBrush,
#: a dataset of human-annotated precise edits, and follows instructions more accurately as a
#: result (though it is not stronger for style transfer or edits over a large photo region —
#: that stays a limitation of this model family). Its only safetensors upload lives on an open
#: pull request rather than the main branch; instruct-pix2pix has safetensors on main directly.
IMAGE_MODELS = {
    'magicbrush': {'repo': 'vinesmsuic/magicbrush-jul7', 'revision': 'refs/pr/2', 'folder': 'magicbrush'},
    'instructpix2pix': {'repo': 'timbrooks/instruct-pix2pix', 'revision': None, 'folder': 'instruct-pix2pix'},
}
#: Background removal/blur. A single small ONNX file — no diffusers/torch pipeline involved.
SEGMENT_REPO = 'briaai/RMBG-1.4'
SEGMENT_FILE = 'onnx/model.onnx'


def selected(name):
    return (name == 'model_index.json' or (
        name.split('/')[0] in FOLDERS and '.fp16.' not in name
        and name.endswith(('.json', '.txt', '.safetensors'))))


def image_model_info(choice):
    """Resolve (info, revision actually used) for `choice`, falling back from a PR
    revision to main only if the PR was merged/closed — never falling back to a
    revision that lacks safetensors, which would silently mean loading pickle weights."""
    spec = IMAGE_MODELS[choice]
    api = HfApi()
    revision = spec['revision']
    try:
        info = api.model_info(spec['repo'], revision=revision, files_metadata=True)
    except RevisionNotFoundError:
        info = api.model_info(spec['repo'], files_metadata=True)
        revision = info.sha
    return info, revision or info.sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--image-model', choices=sorted(IMAGE_MODELS), default='magicbrush',
                         help='Which Generative AI checkpoint to install (default: magicbrush, '
                              'a fine-tune of instructpix2pix that follows edit instructions more '
                              'accurately at the same size and speed)')
    parser.add_argument('--skip-segmentation', action='store_true',
                         help='Do not fetch the background removal/blur model')
    args = parser.parse_args()
    spec = IMAGE_MODELS[args.image_model]
    info, revision = image_model_info(args.image_model)
    files = [f for f in info.siblings if selected(f.rfilename)]
    if any(f.size is None for f in files):
        raise SystemExit('Cannot verify every model file size; nothing downloaded.')
    if not files:
        raise SystemExit(f'No safetensors files found for {spec["repo"]}@{revision}; nothing downloaded. '
                          'Refusing to fall back to pickle (.bin/.ckpt) weights.')
    size = sum(f.size for f in files)
    target = ROOT / spec['folder']
    # hf_hub_download keeps the file's repo-relative path under local_dir, so the
    # download lands at rmbg-1.4/onnx/model.onnx -- point the setting at where it
    # actually goes, or segmentation reports itself uninstalled after a good fetch.
    segment_target = ROOT / 'rmbg-1.4' / SEGMENT_FILE
    segment_info, segment_size = None, 0
    if not args.skip_segmentation:
        segment_info = HfApi().model_info(SEGMENT_REPO, files_metadata=True)
        segment_files = [f for f in segment_info.siblings if f.rfilename == SEGMENT_FILE]
        if not segment_files or segment_files[0].size is None:
            raise SystemExit('Cannot verify the segmentation model file size; nothing downloaded.')
        segment_size = segment_files[0].size
    existing = sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()) if ROOT.exists() else 0
    needed = sum(f.size for f in files if not (target / f.rfilename).exists())
    if segment_info and not segment_target.exists():
        needed += segment_size
    # Reserve 3 GB for the Qwen model plus metadata. No runtime/package files here.
    if existing + needed + 3_000_000_000 > 20_000_000_000:
        raise SystemExit('Download would exceed the 20 GB model budget including language-model reserve.')
    print(json.dumps({'repository': spec['repo'], 'revision': revision, 'bytes': size, 'files': len(files),
                       'destination': str(target),
                       'segmentation_repository': SEGMENT_REPO if segment_info else None,
                       'segmentation_bytes': segment_size,
                       'segmentation_destination': str(segment_target) if segment_info else None}), flush=True)
    if not args.download:
        return
    for f in files:
        print(f'Downloading {f.rfilename}', flush=True)
        hf_hub_download(spec['repo'], f.rfilename, revision=revision, local_dir=target)
    settings_path = ROOT / 'settings.json'
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    except ValueError:
        settings = {}
    settings.setdefault('language_model', 'qwen3:4b')
    settings['image_model'] = str(target)
    settings['budget_bytes'] = 20_000_000_000
    if segment_info:
        print(f'Downloading {SEGMENT_FILE}', flush=True)
        hf_hub_download(SEGMENT_REPO, SEGMENT_FILE, revision=segment_info.sha, local_dir=segment_target.parent)
        settings['segmentation_model'] = str(segment_target)
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f'Local image model ({args.image_model}) installed. Runtime configuration written.', flush=True)


if __name__ == '__main__':
    main()
