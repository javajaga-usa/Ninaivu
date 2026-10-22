"""A calm voice that reads a letter aloud, in the model host.

Meta's MMS text-to-speech models (Pratap et al., 2023), one per language,
each a small VITS model of about 36 million parameters that reads the
letters of its own script. Both are downloaded only from the AI models page
(media/model_catalog.py):

``voice-tamil`` — MMS-TTS Tamil
    As Meta publishes it on Hugging Face, run by the transformers package on
    PyTorch (the editing models' packages, under Extras).
``voice-english`` — MMS-TTS English
    The ONNX export the sherpa-onnx project publishes on GitHub, run by
    sherpa-onnx, the package voice stories are written down with.

The voices are the models' own; neither is, or can be made, the voice of
anybody in the family. Both are licensed for non-commercial use, which a
household's own library is.

The server writes nothing but the words: it names the letter's sentences
(media/letters.py) and a file to write, and the host answers with the
reading as a WAV beside it and when each sentence starts and ends, for the
page to light the sentence being read. A Tamil sentence with an English
word in it is read by both voices in turn, when both are downloaded.
"""
from __future__ import annotations

import importlib.util
import logging
import time
import wave
from pathlib import Path
from typing import Any, Sequence

from ..words import said
from .host import Capability, Unavailable

log = logging.getLogger(__name__)

#: The catalogue entries, by language.
MODELS = {"ta": "voice-tamil", "en": "voice-english"}
#: Slower than the models' own pace: a letter is read, not announced.
CALM = 0.88
#: Silence between two runs of different script in one sentence, seconds.
RUN_GAP = 0.08

#: What each voice needs that may not be installed, by language.
_PACKAGES = {"ta": ("torch", "transformers"), "en": ("sherpa_onnx",)}


def model_folder(language: str) -> Path:
    from ..media import model_catalog                          # noqa: PLC0415
    entry = model_catalog.MODELS[MODELS[language]]
    return model_catalog.models_root() / entry["files"][0]["folder"]


def ready(language: str) -> str | None:
    """Why *language* cannot be read aloud on this computer, or None."""
    from ..media import model_catalog                          # noqa: PLC0415
    if language not in MODELS:
        return said("there is no voice for this language")
    if any(importlib.util.find_spec(p) is None for p in _PACKAGES[language]):
        if language == "ta":
            return said("the Tamil voice needs PyTorch and transformers (Letters read aloud in Tamil, under Extras on the Home & extensions page)")
        return said("the English voice needs sherpa-onnx (Voice stories as words, under Extras on the Home & extensions page)")
    if MODELS[language] not in model_catalog.MODELS or not model_catalog.installed(MODELS[language]):
        if language == "ta":
            return said("the Tamil voice is not downloaded (AI models page)")
        return said("the English voice is not downloaded (AI models page)")
    return None


class _TorchVoice:
    """MMS as Meta publishes it, through transformers."""

    def __init__(self, folder: Path, threads: int) -> None:
        import torch                                           # noqa: PLC0415
        from transformers import VitsModel, VitsTokenizer      # noqa: PLC0415
        torch.set_num_threads(max(1, threads))
        self.torch = torch
        self.tokenizer = VitsTokenizer.from_pretrained(str(folder))
        self.model = VitsModel.from_pretrained(str(folder)).eval()
        self.model.speaking_rate = CALM
        self.rate = int(self.model.config.sampling_rate)
        self.romanize = None
        if getattr(self.tokenizer, "is_uroman", False):
            # The model was trained on its language written in Latin letters
            # (uroman); the words are turned into those letters first.
            try:
                import uroman                                  # noqa: PLC0415
            except ImportError as exc:
                raise Unavailable(said("the Tamil voice needs the uroman package (pip install uroman)")) from exc
            self.romanize = uroman.Uroman().romanize_string
        self.vocabulary = None if self.romanize else set(self.tokenizer.get_vocab())

    def say(self, text: str) -> Any:
        import numpy as np                                     # noqa: PLC0415
        if self.romanize is not None:
            text = self.romanize(text)
        inputs = self.tokenizer(text, return_tensors="pt")
        if inputs["input_ids"].shape[-1] == 0:
            return np.zeros(0, dtype=np.float32)
        # The same sentence is read the same way every time.
        self.torch.manual_seed(0)
        with self.torch.no_grad():
            wave_out = self.model(**inputs).waveform
        return wave_out[0].float().cpu().numpy().astype(np.float32)


class _OnnxVoice:
    """MMS as sherpa-onnx exports it."""

    def __init__(self, folder: Path, threads: int) -> None:
        import sherpa_onnx                                     # noqa: PLC0415
        model = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(folder / "model.onnx"), tokens=str(folder / "tokens.txt")),
            num_threads=max(1, threads), provider="cpu")
        self.tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=model,
                                                                     max_num_sentences=1))
        self.rate = int(self.tts.sample_rate)
        tokens = (folder / "tokens.txt").read_text(encoding="utf-8").splitlines()
        # Each line is "<character> <id>"; the character may itself be a space.
        self.vocabulary = {line.rsplit(" ", 1)[0] for line in tokens if " " in line}

    def say(self, text: str) -> Any:
        import numpy as np                                     # noqa: PLC0415
        made = self.tts.generate(text, sid=0, speed=CALM)
        return np.asarray(made.samples, dtype=np.float32)


_ENGINES = {"ta": _TorchVoice, "en": _OnnxVoice}


def _load(setup: dict[str, Any]) -> dict[str, Any]:
    if all(ready(language) for language in MODELS):
        raise Unavailable(ready("ta") or "")
    return {"threads": int(setup.get("threads") or 2), "voices": {}, "failed": {}}


def _voice(kept: dict[str, Any], language: str) -> Any:
    """*language*'s voice, loaded the first time it is asked for."""
    voices, failed = kept["voices"], kept["failed"]
    if language in voices:
        return voices[language]
    if language in failed:
        raise Unavailable(failed[language])
    why = ready(language)
    if why:
        raise Unavailable(why)
    started = time.monotonic()
    try:
        voices[language] = _ENGINES[language](model_folder(language), kept["threads"])
    except Unavailable as exc:
        failed[language] = str(exc)
        raise
    except Exception as exc:                                   # noqa: BLE001 - a broken model or package
        log.warning("the %s voice could not be loaded: %s", language, exc)
        failed[language] = said("the voice could not be loaded; see the log")
        raise Unavailable(failed[language]) from exc
    log.warning("%s voice loaded in %.1f s", language, time.monotonic() - started)
    return voices[language]


def _resample(samples: Any, rate: int, to: int) -> Any:
    """*samples* at *to* samples a second (linear: both voices are 16 kHz
    today, so this runs only if a later model is not)."""
    import numpy as np                                         # noqa: PLC0415
    if rate == to or not len(samples):
        return samples
    length = int(round(len(samples) * to / rate))
    return np.interp(np.linspace(0, len(samples) - 1, length),
                     np.arange(len(samples)), samples).astype(np.float32)


def speak(kept: dict[str, Any], *, sentences: Sequence[dict[str, Any]], language: str,
          out: str) -> dict[str, Any]:
    """Read *sentences* (media/letters.py ``sentences``) aloud in *language*
    and write the reading to *out* as a 16-bit mono WAV.

    Answers with each sentence's ``start`` and ``end`` in seconds, the whole
    ``duration``, and which voices read it. A run in the other script is read
    by the other voice when it is downloaded, and quietly left out when not.
    """
    import numpy as np                                         # noqa: PLC0415

    from ..media import letters                                # noqa: PLC0415
    main = _voice(kept, language)
    rate = main.rate
    pieces: list[Any] = []
    timings: list[dict[str, Any]] = []
    used = {language}
    at = 0
    started = time.monotonic()
    for sentence in sentences:
        text = str(sentence.get("text") or "")
        begin = at
        for script, part in letters.runs(text):
            voice = main
            if script != language:
                try:
                    voice = _voice(kept, script)
                    used.add(script)
                except Unavailable:
                    voice = main
            words = letters.spoken(part, script if voice is not main else language,
                                   voice.vocabulary)
            if not words:
                continue
            samples = _resample(voice.say(words), voice.rate, rate)
            if not len(samples):
                continue
            if pieces and at > begin:
                gap = np.zeros(int(RUN_GAP * rate), dtype=np.float32)
                pieces.append(gap)
                at += len(gap)
            pieces.append(samples)
            at += len(samples)
        pause = float(sentence.get("pause") or 0.0)
        if at > begin:
            timing = {"text": text, "start": round(begin / rate, 3), "end": round(at / rate, 3)}
            if pause >= letters.PARAGRAPH_PAUSE:
                timing["paragraph"] = True          # the page starts a new paragraph after it
            timings.append(timing)
        if pause > 0 and at > 0:
            gap = np.zeros(int(pause * rate), dtype=np.float32)
            pieces.append(gap)
            at += len(gap)
    if not timings:
        raise ValueError("nothing in these words can be read aloud")
    audio = np.concatenate(pieces)
    peak = float(np.max(np.abs(audio))) or 1.0
    # A little under full scale, the same for every letter.
    audio = np.clip(audio * (0.89 / peak), -1.0, 1.0)
    with wave.open(out, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((audio * 32767).astype("<i2").tobytes())
    duration = round(len(audio) / rate, 3)
    log.info("read %d sentences aloud (%.1f s of speech) in %.1f s", len(timings), duration,
             time.monotonic() - started)
    return {"segments": timings, "duration": duration, "voices": sorted(used),
            "version": letters.VOICE_VERSION}


SPEAK = Capability("speak", 1, _load, speak)
