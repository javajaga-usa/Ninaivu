"""Speech: what is said in a voice story, written down, on this computer.

Whisper, exported to ONNX and quantised to 8 bits by the sherpa-onnx project,
run through sherpa-onnx's own ONNX Runtime inside the model host. Two sizes
are offered on the AI models page (media/model_catalog.py):

``speech`` — large-v3
    The most accurate Whisper for Tamil, and the only one of the two that can
    also translate a Tamil story into English. About 2.5 GB in memory while it
    works; slower.
``speech-quick`` — turbo
    large-v3's encoder with a four-layer decoder: several times quicker, about
    as good at writing down Tamil and English, and no translation.

large-v3 is used whenever it is downloaded. The words a story was given carry
the version of the model that made them (:func:`version_of`), so downloading
large-v3 after turbo makes every story owed again, which is what brings the
English translations in.

How one story is read:

1. The recording (WebM, Ogg, MP4, MP3, WAV, FLAC, AAC — whatever a phone or a
   browser made) is turned into 16 kHz mono samples by ffmpeg, which every
   portable build carries since 1.12. A WAV is read without it.
2. The Silero speech detector finds where somebody is talking. Silence is not
   handed to Whisper, which invents words for silence ("Thank you for
   watching"), and a long story is cut at its pauses, into pieces of up to
   :data:`MAX_PIECE` seconds, rather than every 30 seconds mid-word.
3. Every piece is written down, Whisper choosing its language, and the
   story's language is the one most of its pieces were heard in. A household
   says which languages it speaks (``speech_languages``, Tamil and English
   unless set); Whisper sometimes hears Tamil as Malayalam or Hindi, so a
   language outside that list is taken to be the first one in it.
4. A piece heard as another language than the story's is written down again
   in the story's, so a story is one language from start to end.
5. A story not in English is translated into English too, piece by piece, by
   large-v3. Each piece keeps its start and end, for subtitles.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Sequence

from .host import Capability, Unavailable

log = logging.getLogger(__name__)

#: The capability's own version: the way stories are read, apart from the
#: model. Raised when a change here should make every story owed again.
TRANSCRIBE_VERSION = 1

#: The speech models, best first, each with its rank among them.
MODELS: tuple[str, ...] = ("speech", "speech-quick")
_RANK = {"speech": 2, "speech-quick": 1}
#: Which of them can translate into English.
TRANSLATES = frozenset({"speech"})

RATE = 16000
#: The longest piece handed to Whisper, in seconds. Whisper reads 30 seconds
#: at a time; a little less leaves room for the padding around each piece.
MAX_PIECE = 28.0
#: Silence kept either side of what the detector marked as speech, so the
#: first and last syllables are not clipped.
PADDING = 0.25
#: Pieces decoded together, which sherpa-onnx runs side by side.
BATCH = 4


def chosen_model() -> str | None:
    """The speech model the engine uses: the best one downloaded, or None."""
    from ..media import model_catalog                          # noqa: PLC0415
    return next((m for m in MODELS if model_catalog.installed(m)), None)


def version_of(model_id: str | None) -> int:
    """What words made by *model_id* are stamped with; 0 for no model."""
    if not model_id:
        return 0
    return 10 * TRANSCRIBE_VERSION + _RANK.get(model_id, 0)


def languages(value: str | Sequence[str] | None) -> list[str]:
    """The household's languages as Whisper's codes, in order: "ta, en" →
    ``["ta", "en"]``. Anything that is not a two- or three-letter code is
    dropped."""
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    found: list[str] = []
    for item in value or ():
        code = str(item).strip().lower()
        if 2 <= len(code) <= 3 and code.isalpha() and code not in found:
            found.append(code)
    return found


# ---------------------------------------------------------------------------
# Sound into samples
# ---------------------------------------------------------------------------

#: As media.LOCAL_ONLY, kept here so the model host need not import the media
#: package: a story that is really a concat or SDP file opens nothing else.
LOCAL_ONLY = ("-protocol_whitelist", "file")

#: What ffmpeg says of a file with no sound in it (a video filmed silent, a
#: screen recording): nothing to write down, not a failure.
_NO_SOUND = ("does not contain any stream", "Output file is empty", "matches no streams")


def read_samples(path: str | Path, ffmpeg: str | None = None, *,
                 timeout: float = 600.0, offset: float = 0.0,
                 length: float | None = None) -> Any:
    """The recording as 16 kHz mono float32 samples in [-1, 1]: from
    *offset* seconds in, and *length* seconds of it when that is given. A file with no sound
    is no samples."""
    import numpy as np                                         # noqa: PLC0415

    path = Path(path)
    offset = max(0.0, float(offset or 0))
    if ffmpeg:
        window = (["-ss", f"{offset:.3f}"] if offset else []) + \
            (["-t", f"{float(length):.3f}"] if length else [])
        done = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", *LOCAL_ONLY, *window, "-i", str(path), "-vn",
             "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
            capture_output=True, timeout=timeout, check=False)
        if done.returncode != 0:
            message = done.stderr.decode("utf-8", "replace").strip().splitlines()
            if any(sign in line for line in message for sign in _NO_SOUND):
                return np.zeros(0, dtype=np.float32)
            raise ValueError(f"ffmpeg could not read the recording: {message[-1] if message else done.returncode}")
        return np.frombuffer(done.stdout, dtype=np.float32).copy()
    if path.suffix.lower() != ".wav":
        raise Unavailable("ffmpeg is needed to read this recording; it is on the Home & extensions page")
    with wave.open(str(path), "rb") as handle:
        width, channels, rate = handle.getsampwidth(), handle.getnchannels(), handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("only 16-bit WAV can be read without ffmpeg")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(samples):
        count = int(round(len(samples) * RATE / rate))
        samples = np.interp(np.linspace(0, len(samples) - 1, count),
                            np.arange(len(samples)), samples).astype(np.float32)
    if offset or length:
        first = int(offset * RATE)
        samples = samples[first:first + int(float(length) * RATE)] if length else samples[first:]
    return samples


# ---------------------------------------------------------------------------
# Where somebody is talking
# ---------------------------------------------------------------------------

def even_pieces(total: int) -> list[tuple[int, int]]:
    """The recording in back-to-back pieces of :data:`MAX_PIECE` seconds, for
    when there is no speech detector to say where the pauses are."""
    step = int(MAX_PIECE * RATE)
    return [(start, min(total, start + step)) for start in range(0, total, step)]


def speech_spans(samples: Any, sherpa: Any, vad_model: str | None) -> list[tuple[int, int]] | None:
    """``(start, end)`` sample positions of speech; None when there is no
    detector to ask."""
    if not vad_model or not Path(vad_model).is_file() or sherpa is None:
        return None
    total = len(samples)
    config = sherpa.VadModelConfig()
    config.silero_vad.model = str(vad_model)
    config.silero_vad.min_silence_duration = 0.4
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.max_speech_duration = MAX_PIECE - 2 * PADDING
    config.sample_rate = RATE
    detector = sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=total / RATE + 10)
    window = int(config.silero_vad.window_size or 512)
    spans: list[tuple[int, int]] = []

    def drain() -> None:
        while not detector.empty():
            segment = detector.front
            spans.append((int(segment.start), int(segment.start) + len(segment.samples)))
            detector.pop()

    for start in range(0, total, window):
        detector.accept_waveform(samples[start:start + window])
        drain()
    detector.flush()
    drain()
    return spans


def pieces(spans: Sequence[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """Speech spans joined into pieces of up to :data:`MAX_PIECE` seconds,
    each padded a little and kept inside the recording.

    Whisper writes better with a sentence's neighbours than with one phrase
    at a time, so spans are joined while they fit; a pause between two of
    them stays in the piece, which costs nothing now the detector has said
    where the talking is.
    """
    limit = int(MAX_PIECE * RATE)
    pad = int(PADDING * RATE)
    joined: list[list[int]] = []
    for start, end in sorted(spans):
        start, end = max(0, start - pad), min(total, end + pad)
        if end <= start:
            continue
        if joined and end - joined[-1][0] <= limit:
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([start, min(end, start + limit)])
    return [(a, b) for a, b in joined]


# ---------------------------------------------------------------------------
# Whisper's loops
# ---------------------------------------------------------------------------

#: The same few characters, or the same few words, over and over: Whisper's
#: way of failing on a piece it cannot make out ("4-4-4-4-…", "the the the …").
_LOOP = re.compile(r"(.{1,24}?)(?:\s*\1){5,}", re.DOTALL)


def tidy(text: str) -> str:
    """*text* with any run of repeats cut to one, and its edges trimmed."""
    return _LOOP.sub(r"\1", text or "").strip()


# ---------------------------------------------------------------------------
# The capability
# ---------------------------------------------------------------------------

def _load(setup: dict[str, Any]) -> dict[str, Any]:
    try:
        import sherpa_onnx                                     # noqa: PLC0415
    except ImportError:
        raise Unavailable("the speech reader is not installed (Voice stories as words, "
                          "under Extras on the Home & extensions page)") from None
    from ..media import model_catalog                          # noqa: PLC0415
    from .devices import sherpa_provider                       # noqa: PLC0415
    model_id = chosen_model()
    if model_id is None:
        raise Unavailable("no speech model is downloaded (AI models page)")
    entry = model_catalog.MODELS[model_id]["files"][0]
    folder = model_catalog.models_root() / entry["folder"]
    vad = next((model_catalog.file_path(f) for f in model_catalog.MODELS[model_id]["files"]
                if f["name"] == "silero_vad.onnx"), None)
    threads = int(setup.get("threads") or max(1, (os.cpu_count() or 2) // 2))
    started = time.monotonic()
    recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(folder / "encoder.int8.onnx"), decoder=str(folder / "decoder.int8.onnx"),
        tokens=str(folder / "tokens.txt"), language="", task="transcribe",
        num_threads=threads, provider=sherpa_provider(quantised=True))
    log.warning("loaded %s on %d threads in %.1f s", model_id, threads, time.monotonic() - started)
    return {"sherpa": sherpa_onnx, "recognizer": recognizer, "model": model_id,
            "vad": str(vad) if vad and vad.is_file() else None,
            "setting": (None, None)}


def _set(kept: dict[str, Any], language: str, task: str) -> None:
    """Point the one loaded recognizer at a language and a task, without
    loading the model again (sherpa-onnx changes Whisper's prompt only)."""
    if kept["setting"] == (language, task):
        return
    recognizer = kept["recognizer"]
    config = recognizer.config
    config.model_config.whisper.language = language
    config.model_config.whisper.task = task
    recognizer.recognizer.set_config(config)
    kept["setting"] = (language, task)


def _decode(kept: dict[str, Any], samples: Any, spans: Sequence[tuple[int, int]],
            language: str, task: str) -> list[tuple[str, str]]:
    """``(text, heard language)`` for each piece."""
    _set(kept, language, task)
    recognizer = kept["recognizer"]
    answers: list[tuple[str, str]] = []
    for first in range(0, len(spans), BATCH):
        streams = []
        for start, end in spans[first:first + BATCH]:
            stream = recognizer.create_stream()
            stream.accept_waveform(RATE, samples[start:end])
            streams.append(stream)
        recognizer.decode_streams(streams)
        answers.extend((tidy(s.result.text), str(getattr(s.result, "lang", "") or ""))
                       for s in streams)
    return answers


def _language(heard: Sequence[str], allowed: Sequence[str]) -> str:
    """The story's language from what each piece was heard as."""
    counts: dict[str, int] = {}
    for code in heard:
        code = code.strip("<|>").lower()
        if code:
            counts[code] = counts.get(code, 0) + 1
    best = max(counts, key=lambda code: counts[code]) if counts else ""
    if allowed and best not in allowed:
        return allowed[0]
    return best or "en"


def transcribe(kept: dict[str, Any], *, path: str, ffmpeg: str | None = None,
               languages: Sequence[str] = ("ta", "en"), translate: bool = True,
               offset: float = 0.0, length: float | None = None) -> dict[str, Any]:
    """Write down one recording, or *length* seconds of it from *offset*
    (each piece's start and end are then still the recording's own). Plain
    values only, for the server."""
    started = time.monotonic()
    offset = max(0.0, float(offset or 0))
    samples = read_samples(path, ffmpeg, offset=offset, length=length)
    heard = speech_spans(samples, kept["sherpa"], kept["vad"])
    spans = even_pieces(len(samples)) if heard is None else pieces(heard, len(samples))
    result: dict[str, Any] = {
        "model": kept["model"], "version": version_of(kept["model"]),
        "seconds": round(len(samples) / RATE, 1), "language": "", "text": "",
        "english": "", "segments": []}
    if not spans:
        result["took"] = round(time.monotonic() - started, 1)
        return result
    allowed = list(languages)
    # Every piece written down once with Whisper choosing the language, which
    # is how it hears which language that is. Only the pieces it heard as
    # another language than the story's are written down again, in the
    # story's: on an ordinary story, none, so it is read once and not twice.
    heard = _decode(kept, samples, spans, "", "transcribe")
    language = _language([lang for _text, lang in heard], allowed)
    again = [index for index, (_text, lang) in enumerate(heard)
             if lang.strip("<|>").lower() != language]
    written = list(heard)
    if again:
        redone = _decode(kept, samples, [spans[i] for i in again], language, "transcribe")
        for index, answer in zip(again, redone):
            written[index] = answer
    english: list[tuple[str, str]] = []
    if translate and language != "en" and kept["model"] in TRANSLATES:
        english = _decode(kept, samples, spans, language, "translate")
    segments = []
    for index, (start, end) in enumerate(spans):
        text = written[index][0]
        said_in_english = english[index][0] if english else ""
        if not text and not said_in_english:
            continue
        segments.append({"start": round(offset + start / RATE, 2),
                         "end": round(offset + end / RATE, 2),
                         "text": text, "english": said_in_english})
    result.update(language=language, segments=segments,
                  text=" ".join(s["text"] for s in segments if s["text"]).strip(),
                  english=" ".join(s["english"] for s in segments if s["english"]).strip(),
                  took=round(time.monotonic() - started, 1))
    return result


TRANSCRIBE = Capability("transcribe", TRANSCRIBE_VERSION, _load, transcribe)
