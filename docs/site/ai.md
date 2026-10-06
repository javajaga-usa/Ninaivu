# AI

Everything on this page runs on your own computer, and no photograph, face or
name is sent anywhere by it. The one exception is an extension you switch on
that says otherwise on its switch (Gemini, below). The models themselves are
downloaded once, when you turn their feature on.

## Two kinds of computer

Ninaivu is tested on two kinds of machine, and works out which yours is when
it starts (**System → Performance** says which, and why):

| | Basic | Full |
| --- | --- | --- |
| The machine | anything else: a mini PC, an old laptop, a single-board computer, a desktop without a graphics card or without the image model | a graphics processor with at least 6 GB of memory, or Apple silicon, or any computer with 8 GB of memory once search by description is added |
| The gallery, albums, sharing, roles | yes | yes |
| Faces, grouped and named | yes | yes |
| Sideways photographs put right | yes | yes |
| Places named from the location | yes | yes |
| Text read from photographs (with the text reader) | yes, slowly | yes |
| Search by words in names, dates, places and people | yes | yes |
| Mugil, the encrypted copy | yes | yes |
| Tags, "the beach at sunset" search and descriptions from the image model | — | yes |
| Finding visually similar photographs (`S` in the viewer) | — | yes |
| Videos described from several moments | — | yes |
| Creative Studio: generative edits, object removal, upscaling | — | with a graphics card |

On a Basic machine, *auto* means the light engine, so it never tries to load
the large models and starts quickly. Choosing the image model outright
(`--ai clip`, or `ai_engine` on **All settings**) still loads it. Setting
`hardware_tier` on **All settings** overrides the measurement.

## Adding search by description

It is not installed by default: it is about 2 GB, plus the model itself.
Add it from the first-day walk-through or **Settings → Extras → Search by
description**, then restart Ninaivu. On a Full machine it is used from then
on; the model's weights are fetched once at that first start, and after that
Ninaivu tells the model libraries to make no request at all
(`HF_HUB_OFFLINE`).

## What each pass does

**AI → AI models** has a switch for each, saying which model it needs and
whether that model is here.

- **Faces** — a 37 MB detector and matcher. Groups are named on **Faces**;
  name one and the rest follow.
- **Places** — an 11 MB list of places, downloaded once from GeoNames; the
  photograph's own location becomes "Paris" or "the beach". The map uses a
  built-in outline of the world; street-map tiles from OpenStreetMap are
  off unless `map_tiles` is switched on, because each tile asked for shows
  roughly where a photograph was taken.
- **Sideways photographs** — a 77 MB model that says which way up a
  photograph goes. By default it looks by itself after every scan and turns
  what it finds straight away, as one batch that **Undo** on **Review →
  Straighten** puts back; switch that off and what it finds waits there for
  approval.
- **Text in photographs** — the text reader from **Extras**; slow on a
  processor.
- **The image model** (Full, once added) — CLIP or SigLIP: tags, "the beach
  at sunset" search, descriptions, visually similar photographs.
- **Videos** (Full) — described from several moments rather than the poster
  frame.

## Sudar

**Sudar** (சுடர், *glow*) edits in the browser: light, colour, straightening,
crops, looks, and suggestions measured from the picture. "Make it warmer" in
plain words. The original is never changed; a copy is saved beside it, and so
can any result a tool makes — a cut-out, a colourised print — a family
member's copy waiting for the administrator's approval like an upload. The
**Photo Studio** (*Edit photo* in the viewer) renders through the same engine,
so a slider means the same thing in both; [the family
guide](guide-family.md#the-photo-studio) has every panel.

Its **Skin** and **Hair** tools work on every person in the photograph, each
judged against their own skin, using the face model from **AI models**. They
leave a bindi, sindoor or sacred ash exactly as it is, and have no setting that
lightens a complexion. Hair the colour of what is behind it is found with the
help of *Background removal (RMBG-1.4)* when that model is installed, and
painted in by hand otherwise; **Add hair** draws new hair from the strands
beside it, on this machine. See [the family guide](guide-family.md#skin-and-hair).

Its **Enhance tools** each need a model, and the card says which:

- **Upscale** — *Upscale ×4 (Real-ESRGAN)*, from **AI models**.
- **Restore faces** — *Restore faces (GFPGAN 1.4)*, from **AI models**.
- **Colourise** — a DDColor ONNX model. The catalogue cannot pin one yet, so
  this is the one model placed by hand: put `ddcolor.onnx` in the `onnx`
  subfolder of the models folder (**AI models** shows the folder), or point
  `NINAIVU_COLORIZE_MODEL` at the file. With Restore faces too, **Revive old
  photo** does both in one tap.

Each runs on this machine and nothing leaves it; with the Creative Studio
extension, an AI server workflow can take any of them instead.

## Extensions

Anything that cannot keep the core's two promises — nothing leaves the house
unless you chose where, and it runs on a small machine — is an extension,
off until switched on under **System → Settings → Extensions** (it takes
effect when Ninaivu next starts), and its switch says what it does:

- **Creative Studio** — generative edits and the heavy tools with large
  models, on this computer with a GPU or on a ComfyUI server at home. The
  AI server page refuses an address on the internet, so nothing leaves the
  house (a Tailscale address counts as home).
- **Gemini** — edits and descriptions from Google's models. A re-encoded copy
  of the photograph goes to Google when, and only when, a request names it.
  Only an administrator can send one, unless *Let family members send
  photographs to extensions that go outside the house* is ticked under
  Settings → Extensions.

Setting up extensions is covered in the [technical administrator guide](../admin-guide.md).
