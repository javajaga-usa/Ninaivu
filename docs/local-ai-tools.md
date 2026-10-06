# Local AI models: object removal, upscaling, face restoration and search

Ninaivu can run four optional AI models on the Ninaivu machine itself. None of
them ship with Ninaivu — they are large and each has its own licence — so a fresh
checkout has none, and they are downloaded on request. Photographs never leave
the machine for these tools.

| Model | Used for | Download | Licence | Runs on |
| --- | --- | --- | --- | --- |
| LaMa | **Remove Object** in the Playground | 198 MB | Apache-2.0 | CPU (about 2 s) |
| Real-ESRGAN ×4 | **Upscale** in the Playground's Enhance tools | 35 MB | BSD-3-Clause | Graphics card via DirectML, else CPU |
| GFPGAN 1.4 | **Restore Faces** in the Enhance tools | 325 MB | Apache-2.0 | Graphics card via DirectML, else CPU |
| SigLIP 2 (ViT-B/16) | AI search, tags and the explicit-content check | 1.46 GB | Apache-2.0 | Graphics card through CUDA/ROCm when usable, else CPU |
| MagicBrush | **Ask AI to Edit** (generative editing) | 5.48 GB | CreativeML Open RAIL-M | Graphics card in half precision when usable, else CPU |
| RMBG-1.4 | Background removal and blur | 176 MB | BRIA community licence (non-commercial) | Graphics card via DirectML, else CPU |

Real-ESRGAN and GFPGAN come from ONNX conversions in `facefusion/models-3.0.0`,
whose repository states no licence of its own; the licences above are those of
the upstream models.

## On a fresh checkout

1. **Install the Python packages** (once):

   ```bash
   pip install -r requirements/requirements-ai-local.txt
   pip install "torch>=2.0" --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements/requirements-ai.txt
   ```

   The first line is enough for object removal, upscaling and face restoration.
   On Windows it installs the DirectML build of ONNX Runtime, which also uses
   the graphics card (AMD, Intel or NVIDIA). The other two are for search.

2. **Download the models**, either:

   - in the admin console: **Admin → AI models → Download** on each model, or
   - from a terminal:

     ```bash
     python tools/fetch_ai_models.py            # list, with sizes and licences
     python tools/fetch_ai_models.py --get all  # or: --get lama upscale restore siglip2
     ```

   Every file is pinned to a repository revision and a SHA-256 checksum; a
   download that does not match is discarded. Models go to `.ai-models/` in the
   project folder (ignored by git); an installed copy keeps them in a folder of
   the user's own that upgrades leave alone (the technical administrator guide
   lists it); `NINAIVU_AI_MODELS_DIR` or the `ai_models_dir` setting moves them.

3. **Restart Ninaivu** if you downloaded SigLIP 2. The tools pick up their models
   at once; search loads its model when Ninaivu starts.

## How each tool behaves

- **Remove Object** uses LaMa when it is installed and the built-in OpenCV method
  otherwise. LaMa works on a square around the painted area and only the painted
  pixels are written back, so the rest of the photo is untouched.
- **Upscale** makes the photo four times larger (up to 2,048 px in, 8,192 px out),
  working in overlapping tiles so large photos fit in graphics memory.
- **Restore Faces** finds faces with OpenCV's detector and restores each with
  GFPGAN, blending it back with a soft edge. Crops are not aligned to facial
  landmarks, so strongly turned faces improve less than front-on ones. A photo
  with no detectable face is refused.
- If an AI server workflow is assigned to Upscale or Restore Faces (Admin → AI
  server), the server does that job instead.

## SigLIP 2 search

With `clip_model` left at its default, `auto`, Ninaivu uses SigLIP 2 when it is
installed and the smaller ViT-B-32 otherwise. The two models' vectors cannot be
mixed, so the first scan after switching clears the old ones and re-tags the
library in the background; search results return as photos are processed (hours
for a large library on a CPU).

SigLIP scores on a different scale from CLIP, so Ninaivu uses thresholds measured
for it: a search match floor of 0.075 and a tag threshold of 0.085 (cosine), and
for the explicit-content check the model's own probability for the unsafe
descriptions, flagged at 0.01. On ordinary photographs, faces and flat colours
that probability was at most 0.0003. **How reliably it catches explicit
photographs was not measured**; check the Visibility tab after the first re-tag
if this matters for your library.

Licences and sources are listed on each model in the console, and in
`ninaivu/media/model_catalog.py`.
