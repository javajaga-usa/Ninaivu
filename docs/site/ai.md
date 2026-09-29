# AI

Everything runs on your own computer. No photograph, face or name is sent to
anyone by any of this.

## Two kinds of computer

Ninaivu is tested on two kinds of machine, and works out which yours is when
it starts (**System → Performance** says which, and why):

| | Basic | Full |
| --- | --- | --- |
| The machine | 2 GB of memory, no graphics processor: a mini PC, an old laptop, a single-board computer | a graphics processor, or Apple silicon |
| The gallery, albums, sharing, roles | yes | yes |
| Faces, grouped and named | yes | yes |
| Sideways photographs put right | yes | yes |
| Places named from the location | yes | yes |
| Search by words in names, dates, places and people | yes | yes |
| Mugil, the encrypted copy | yes | yes |
| Tags, natural-language search and descriptions from the image model | — | yes |
| Videos described from several moments | — | yes |
| Text read from photographs | — | yes |
| Creative Studio: generative edits, object removal, upscaling | — | yes |

A Basic machine never tries to load the large models, so it starts quickly
and stays quick. Setting `hardware_tier` on **All settings** overrides the
measurement.

## What each pass does

**AI → AI models** has a switch for each, saying which model it needs and
whether that model is here.

- **Faces** — a 37 MB detector and matcher. Groups are named on **Faces**;
  name one and the rest follow.
- **Places** — an 11 MB list of places; the photograph's own location
  becomes "Paris" or "the beach".
- **Sideways photographs** — a 77 MB model that says which way up a
  photograph goes; what it finds waits on **Review → Straighten** for
  approval, and by default it looks by itself after every scan.
- **Text in photographs** — the text reader from **Extras**; slow on a
  processor.
- **The image model** (Full) — CLIP or SigLIP: tags, "the beach at sunset"
  search, descriptions. Downloaded once; after that Ninaivu tells the model
  libraries to make no request at all (`HF_HUB_OFFLINE`).
- **Videos** (Full) — described from several moments rather than the poster
  frame.

## Sudar

**Sudar** (சுடர், *glow*) edits in the browser: light, colour, straightening,
crops, looks, and suggestions measured from the picture. "Make it warmer" in
plain words. The original is never changed; a copy is saved beside it.

## Extensions

Anything that cannot keep the core's two promises — nothing leaves the house
unless you chose where, and it runs on a small machine — is an extension,
off until switched on under **AI models → Extensions**, and its switch says
what it does:

- **Creative Studio** — generative edits and the heavy tools with large
  models, on this computer with a GPU or on a ComfyUI server in the house.
  Nothing leaves the house.
- **Gemini** — edits and descriptions from Google's models. A copy of the
  photograph goes to Google when, and only when, a request names it.

[More on extensions.](https://github.com/javajaga-usa/Ninaivu/tree/main/extensions)
