# Gemini for Sudar

An extension for Ninaivu: edits, descriptions and adjustment plans from
Google's Gemini models, inside the Sudar photo studio.

**What leaves the house when it is on:** a re-encoded copy of the photograph
(1024 px on its long side, without metadata) and the person's instruction go
to Google's Generative Language API. What comes back is shown as a separate
preview; the original is never changed.

It runs only when a request from the Sudar page names it as the provider.
Nothing else in Ninaivu ever sends anything to Google.

## Install and turn on

```sh
pip install -e extensions/gemini          # from a checkout
```

Then in the console, **System → Extensions**, switch Gemini on and restart
Ninaivu. Paste an API key under **AI models → Gemini** (or set
`NINAIVU_GEMINI_KEY` in the server's environment, which takes precedence).
The key is kept in the models' `settings.json`, readable by the server's
account only; the console never shows more than its last four characters.

## Tests

The tests live in the main suite (`tests/test_gemini_*.py`) and skip
themselves when this package is not installed.
