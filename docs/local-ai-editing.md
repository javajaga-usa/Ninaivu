# Advanced local AI editing

The diffusion-model engine described here is part of the **Creative Studio
extension** (`extensions/creative-studio`, installed with `[local]`); the
built-in engine is the core's.

Open a full-size photo → AI Playground → Ask AI to Edit. Select an engine:

- **Built-in**: compound requests, relative adjustments, numerical settings,
  cinematic/vintage/golden-hour/monochrome looks, shadows, highlights, and crops.
  Runs in the browser without a model. Example: “Lift the shadows, reduce
  highlights, make it slightly warmer, and crop to 4:5”.
- **Local AI**: the local Ollama model turns freer wording into a validated
  plan. It receives only the prompt and current adjustments, not photographs.
  The complete plan is shown before applying, and applies as one undo step.
- **Generative AI**: sends a PNG preview and instruction to the household's
  own Ninaivu server. A local image model generates a separate result to review
  and download. Example: “Turn this scene into a watercolor painting”. It can
  attempt scene/object changes, but cannot guarantee precise removals or
  preservation of every surrounding detail — for precise, localized removals
  use **Remove object** instead, which is built for that. CPU generation may
  take from under a minute to several minutes depending on `image_edit_steps`
  and the machine. Output size matches `image_edit_max_side`; it is labelled
  AI-generated and never replaces the original or edit history.
- **Google Gemini** is an extension, not part of the core: turning it on
  means a re-encoded copy of the photograph is sent to Google. See
  `extensions/gemini/README.md`. Nothing in the core ever sends a photograph
  anywhere.


  **Which checkpoint**: `tools/setup_ai_models.py` installs **MagicBrush** by
  default — a fine-tune of the original InstructPix2Pix on
  [MagicBrush](https://huggingface.co/datasets/osunlp/MagicBrush), a dataset
  of human-annotated precise edits. Same architecture, same size, same speed
  as the original; it follows edit instructions more accurately because of
  what it was trained on, not because it's a bigger model. Its own model card
  cautions it "may not perform well on style transfer or other editings on
  [a] large region" — for those, `--image-model instructpix2pix` installs the
  original instead, which is the more general-purpose (if less precise) of
  the two. Neither is a strong model by the standards of a cloud image editor;
  both are small enough to run on CPU. `image_edit_max_side` (default 512,
  256–768) and `image_edit_steps` (default 12, 4–30) in `.ai-models/settings.json`
  trade time for detail — CPU time scales roughly with side² × steps, so
  raising either makes generation noticeably slower. There is no setting that
  makes this engine both faster and higher quality at once on integrated
  graphics; pick the side of that tradeoff that matters more for a given photo.

## Background removal and blur

**Remove background** and **Blur background** use a local segmentation model
(BRIA RMBG-1.4, ONNX) to find the subject in the current edit (adjustments and
crop already applied), then composite the result in the browser — a plain
cutout with a transparent background, or the original subject over a blurred
copy of the same photo. Asking for it in **Ask AI to Edit** — “remove the
background”, “blur the background” — opens the same tool. Blur strength is a
slider with instant local preview; only the initial subject/background split
needs the server. Results are separate downloads, like Generative AI; your
original and adjustment history are unchanged. Mistakes are most visible at
hair and edges — always check before using a cutout or blur.

## Remove object

Paint over a stray object, blemish, watermark, or other small-to-medium
distraction and choose **Remove selected area**. This uses classical inpainting
(OpenCV, already part of Ninaivu's `opencv-python-headless` dependency — no
model download, no setup) to fill the area from its surroundings. It suits
localized selections, not whole subjects or large scene changes; for those,
Generative AI's scene-level edits are a closer (if less precise) fit. You can
paint and apply more than once in the same session to clean up several spots,
then download the result at the resolution it was processed at.

## Dedicated 20 GB model storage

The local installation uses `.ai-models/` beneath the repository (ignored by
Git). Qwen3 4B uses approximately 2.5 GB. The selected image-edit float32
pipeline, including its safety checker, uses approximately 5.5 GB, whichever
of MagicBrush or InstructPix2Pix you chose — they're the same size. The
RMBG-1.4 background segmentation model is a single ONNX file of roughly 44 MB.
The setup utility checks the model budget before downloading and excludes
duplicate single-file checkpoints, fp16 variants and pickle weights — MagicBrush's
only safetensors upload lives on [an open pull request](https://huggingface.co/vinesmsuic/magicbrush-jul7/discussions/2)
rather than its main branch, so the setup tool downloads from that specific
revision; it never falls back to the unscanned `.bin` checkpoint on main.
Runtime libraries are separate from the model-space budget.

This machine has approximately 24 GB system memory and AMD Radeon 880M
integrated graphics. The image pipeline runs on the graphics card in half
precision when the card can genuinely run a kernel, and on the CPU in float32
otherwise — the choice is made by trying one small multiply, because a ROCm
build carrying no kernels for the installed card still reports itself as
available and only fails once real work starts. A larger GPU-oriented image
model fitting on disk would not imply that it can run efficiently here.

## Setup and restart

1. Install the optional dependencies:
   `.venv\Scripts\python.exe -m pip install -r requirements/requirements-ai-editing.txt -r requirements-ai-segmentation.txt`
   (skip the second file if you only want Generative AI, not background removal/blur;
   `opencv-python-headless` from the core `requirements.txt` already covers Remove object).
2. Install Ollama for Windows if absent. Start it with `OLLAMA_MODELS` set to
   the absolute `.ai-models/ollama` path, `OLLAMA_HOST=127.0.0.1:11434`, and
   `OLLAMA_NO_CLOUD=1`. Download `qwen3:4b` using `ollama pull qwen3:4b`.
3. Run `.venv\Scripts\python.exe tools/setup_ai_models.py --download`.
   This downloads the MagicBrush image-edit model and the segmentation model,
   and writes the local settings file. Without `--download`, the command only
   reports sizes and selected file count. Pass `--image-model instructpix2pix`
   for the original checkpoint instead, or `--skip-segmentation` to skip the
   background model. Re-running with a different `--image-model` installs the
   other checkpoint alongside the first (each in its own folder) and switches
   `image_model` in settings.json to the one just downloaded.
4. Start Ninaivu as usual (`python launcher/start.py <folder>`, or the service you
   installed). Start the local Ollama service yourself, or let the desktop
   tray (`python -m ninaivu.desktop.tray`) start it for you. Restart
   an already running server to load new application code. No model is
   downloaded during an edit request.

Environment overrides: `NINAIVU_EDIT_MODEL` selects an installed local Ollama
model; `NINAIVU_IMAGE_EDIT_MODEL` selects a complete local pipeline directory
(MagicBrush, InstructPix2Pix, or any other checkpoint compatible with
`StableDiffusionInstructPix2PixPipeline`); `NINAIVU_SEGMENT_MODEL` selects the
background segmentation ONNX file; `NINAIVU_IMAGE_EDIT_MAX_SIDE` (256–768,
default 512) and `NINAIVU_IMAGE_EDIT_STEPS` (4–30, default 12) tune the
resolution/time and step-count/time tradeoffs for Generative AI — the same
values can instead go in `.ai-models/settings.json` as `image_edit_max_side`
and `image_edit_steps`; the environment variable wins if both are set. Empty
values disable the respective engine. The language model is unloaded after
each response to leave memory for image inference. Remove object needs no
model or setting — it runs whenever `opencv-python-headless` is installed.
Only family/admin profiles can use server inference; browser adjustments
remain available according to the existing viewer controls.

## Privacy and validation

Images are never sent to an external inference service. Generation accepts a
bounded PNG preview, processes it in memory, and returns a no-store PNG. Both
input and output must pass the installed local image safety checker; missing
checkers disable generation. Prompt checks restrict abusive edits. These are
safeguards, not a claim that automatic classifiers detect every harmful input.
Model-produced adjustments are schema-checked and shown for review, never
executed as code or shell commands. Unknown or partly unsupported built-in
requests leave the whole image unchanged.

References: [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[Qwen3 4B](https://ollama.com/library/qwen3:4b),
[InstructPix2Pix model](https://huggingface.co/timbrooks/instruct-pix2pix),
[MagicBrush dataset](https://huggingface.co/datasets/osunlp/MagicBrush),
[MagicBrush diffusers checkpoint](https://huggingface.co/vinesmsuic/magicbrush-jul7),
[Diffusers image editing API](https://huggingface.co/docs/diffusers/api/pipelines/pix2pix),
[RMBG-1.4 model](https://huggingface.co/briaai/RMBG-1.4),
[OpenCV inpainting](https://docs.opencv.org/4.x/df/d3d/tutorial_py_inpainting.html).
# Skin and Hair

Use **Skin & Hair** in the AI editor, or ask for skin retouching or hair work in
Ask AI. This opens the same tools as the Photo Studio's **Skin** and **Hair**
panels, for the people in the photograph:

- **Every face is found and judged against its own skin**, by the face detector
  the People page already uses, on this computer. **Everyone** and each person
  have their own sliders; **Improve faces** sets each person's to what was
  measured about them.
- **Skin:** face light (for a face in shadow), skin tone (turns a colour cast
  back towards skin, by hue only), even out the light, calm the shine, even out
  the tone, brighten under-eyes, soften, richness. Eyes, brows, lips and teeth
  are not touched, and a bindi, kumkum, sindoor, sacred ash or sandal paste is
  left exactly as it is.
- **Hair and beard:** strands and shine, cover grey, and colour. Hair that cannot
  be told from what is behind it is declined rather than guessed; paint it in
  from the Photo Studio.
- **Nothing lightens skin.** There is no fairness, whitening or "porcelain"
  setting. Face light is an exposure change for a face in shadow, the same
  number of stops for skin of any colour.
- **Interactive split comparison and export:** a draggable before/after split,
  direct application to the editing canvas, or a full-resolution PNG.

Everything is arithmetic on the pixels that were photographed, done in the
browser. The one request the tools make — where are the faces? — goes to this
computer (`POST /api/portrait/analyse`), is answered by the face model, and is
not stored.

The small local image-edit model (MagicBrush or InstructPix2Pix) remains
experimental for whole-image transformations. It may alter faces and details and
is not a dependable method for precise portrait touchups — that is what the Skin
and Hair tools above are for.
# Generation controls

In AI Playground, choose **Generative AI** to reveal quality, photo fidelity, an optional
“avoid” prompt, and a variation seed. Draft uses half the configured steps (at least four);
Balanced uses the configured steps; Detailed uses at least 20 and at most 30. Detailed takes
longer and does not increase the configured pixel limit or guarantee a better result.

Preserve uses stronger image guidance to encourage similarity; Balanced and Creative allow
progressively larger changes. These are model guidance controls, not a guarantee that faces
or unselected areas remain identical. Use the dedicated portrait studio for precise touchups.
Keep the seed fixed when comparing settings; **New variation** chooses a fresh seed for the next
generation. Downloaded PNGs record seed, steps, and image guidance, without embedding the photo
request or avoid prompt. Reproduction also requires the same model, input, prompts, and runtime.

Photos are padded to the model grid and cropped back after generation, preserving preview
proportions. Processing remains on the Ninaivu server with local weights and safety checking.

For far better generative edits than this model gives on a CPU, run them on a PC with a graphics
card instead: see [AI server: image edits on a ComfyUI PC](ai-server.md).
