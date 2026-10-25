"""Ninaivu's own gentle music for memory films, made on this computer.

An iPhone memory plays soft music under the photographs without anybody
choosing a song; a memory film here does the same. The music is not a
recording: it is written and played by this module, note by note, each time
a film is made, so nothing is downloaded, nothing is bundled from anybody
else and there is no licence to keep beyond Ninaivu's own. A family can
still bring their own song or use one from the library (api/api_films.py).

Each piece is a few lines of description (:data:`PIECES`): a key, a scale, a
tempo, the chords and which instruments play. :func:`write` composes a piece
to the film's length (an opening on the chords alone, a tune that comes and
goes in four-bar phrases with room to breathe, and a last chord that rings
out at the very end) and plays it with simple instruments drawn from sine
waves: a soft piano, a warm pad, a plucked string with a veena's slide into
the note, a tanpura's drone, a round bass and, in the livelier pieces, a
quiet shaker and kick. A *seed* (the film's own number) changes the tune and
the patterns, so two films of the same piece are not the same music.

Because the beat is known rather than heard, :func:`beats` gives the film's
cuts the exact beat (media/films.py moves cuts onto it), and the music is
written to end where the film ends instead of being cut off.

Needs numpy, as the beat finder does. Pure computation: no network, no
model, a few seconds for a four-minute film.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

#: Bumped whenever the music a seed makes changes, so a kept preview is made
#: again.
VERSION = 1
RATE = 48000

#: The pieces, as a family sees them in the Make a film screen. ``pace`` is
#: the film pace (media/films.py PACES) a piece is chosen for on its own.
#: ``root`` is the MIDI note of the key, ``scale`` its steps in semitones,
#: ``chords`` the scale degrees (0-based) each two bars rest on, or None for
#: a drone that never moves.
PIECES: dict[str, dict[str, Any]] = {
    "morning": {"title": "Morning light", "pace": "gentle", "bpm": 66, "root": 60,
                "scale": (0, 2, 4, 5, 7, 9, 11), "chords": (0, 4, 5, 3),
                "parts": ("pad", "arpeggio", "piano"), "pattern": "slow"},
    "courtyard": {"title": "Courtyard veena", "pace": "gentle", "bpm": 72, "root": 62,
                  "scale": (0, 2, 4, 7, 9), "chords": None,
                  "parts": ("drone", "veena"), "pattern": "slow"},
    "together": {"title": "Together", "pace": "steady", "bpm": 84, "root": 65,
                 "scale": (0, 2, 4, 5, 7, 9, 11), "chords": (5, 3, 0, 4),
                 "parts": ("pad", "arpeggio", "piano", "bass"), "pattern": "flowing"},
    "festival": {"title": "Festival day", "pace": "lively", "bpm": 104, "root": 67,
                 "scale": (0, 2, 4, 5, 7, 9, 11), "chords": (0, 3, 5, 4),
                 "parts": ("pad", "pluck", "piano", "bass", "beat"), "pattern": "lively"},
}
#: The piece a film gets when nobody chose one, by its pace.
FOR_PACE = {"gentle": "morning", "steady": "together", "lively": "festival"}
DEFAULT = "morning"
#: The level the finished piece is brought to: soft, as music under a film is.
PEAK = 0.5
LOUDEST_RMS = 0.11
#: The last chord's ring, from where it starts to the end of the film.
LAST_CHORD = 5.0
#: The shortest piece written, so even a tiny film has an opening and an end.
SHORTEST = 12.0


def piece_for(name: str | None, pace: str | None = None) -> str:
    """A known piece's name: *name* itself, or the one for *pace* when
    *name* is ``"auto"``, missing or unknown."""
    if name in PIECES:
        return str(name)
    return FOR_PACE.get(pace or "", DEFAULT)


def listing() -> list[dict[str, str]]:
    """The pieces for the Make a film screen."""
    return [{"name": name, "title": p["title"], "pace": p["pace"]} for name, p in PIECES.items()]


def beat_seconds(name: str) -> float:
    return 60.0 / float(PIECES[piece_for(name)]["bpm"])


def beats(name: str, seconds: float) -> list[float]:
    """Every beat of *name* in its first *seconds*: the piece is written on a
    steady tempo from its first beat at nought."""
    step = beat_seconds(name)
    return [round(k * step, 3) for k in range(int(max(0.0, seconds) / step) + 1)]


# ---------------------------------------------------------------------------
# Instruments: each gives one note, mono, at unit loudness
# ---------------------------------------------------------------------------

def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _t(seconds: float) -> Any:
    import numpy as np                                          # noqa: PLC0415
    return np.arange(max(1, int(seconds * RATE)), dtype=np.float32) / RATE


def _ends(t: Any, attack: float, release: float, length: float) -> Any:
    """Rises over *attack* and falls over *release* before *length*, so no
    note starts or stops with a click."""
    import numpy as np                                          # noqa: PLC0415
    rise = np.clip(t / max(attack, 1e-3), 0, 1)
    fall = np.clip((length - t) / max(release, 1e-3), 0, 1)
    return rise * fall


def piano(midi: float, length: float) -> Any:
    """A soft piano: a few slightly stretched harmonics, the high ones
    fading first, as a felt hammer on a string."""
    import numpy as np                                          # noqa: PLC0415
    ring = min(length + 1.2, 3.2)
    t = _t(ring)
    f = _hz(midi)
    out = np.zeros_like(t)
    for k in range(1, 7):
        fk = f * k * math.sqrt(1 + 0.0004 * k * k)
        if fk > RATE / 2.4:
            break
        out += (1 / k ** 1.5) * np.exp(-t * (1.3 + 0.9 * k)) * np.sin(2 * np.pi * fk * t)
    return out * _ends(t, 0.006, 0.25, ring)


def pluck(midi: float, length: float, *, slide: bool = False) -> Any:
    """A plucked string; with *slide*, it reaches the note from a little
    below, as a veena player's finger slides into it."""
    import numpy as np                                          # noqa: PLC0415
    ring = min(length + 0.8, 2.6)
    t = _t(ring)
    f = _hz(midi)
    bend = np.ones_like(t)
    if slide:
        bend = 2 ** ((-1.6 * np.exp(-t / 0.05)) / 12)
    phase = 2 * np.pi * f * np.cumsum(bend) / RATE
    out = np.zeros_like(t)
    for k in range(1, 9):
        if f * k > RATE / 2.4:
            break
        out += (1 / k) * np.exp(-t * (2.0 + 1.4 * k)) * np.sin(k * phase)
    return out * _ends(t, 0.003, 0.2, ring)


def pad(midi: float, length: float) -> Any:
    """A warm pad: three voices a few cents apart, so the chord shimmers,
    rising and falling slowly."""
    import numpy as np                                          # noqa: PLC0415
    ring = length + 1.0
    t = _t(ring)
    f = _hz(midi)
    out = np.zeros_like(t)
    for cents in (-6, 0, 6):
        fc = f * 2 ** (cents / 1200)
        for k in range(1, 5):
            out += (1 / (k * k)) * np.sin(2 * np.pi * fc * k * t + k * cents)
    swell = 1 + 0.12 * np.sin(2 * np.pi * 0.23 * t)
    return out * swell * _ends(t, min(1.4, ring / 3), min(1.6, ring / 2.5), ring) / 3


def drone(midi: float, length: float) -> Any:
    """A tanpura string: a long-ringing pluck whose harmonics swell after
    it is struck, as the bridge's thread makes them."""
    import numpy as np                                          # noqa: PLC0415
    ring = length + 1.5
    t = _t(ring)
    f = _hz(midi)
    out = np.zeros_like(t)
    for k in range(1, 11):
        if f * k > RATE / 2.4:
            break
        bloom = 1 - np.exp(-t * (1.5 + 0.3 * k))
        out += (1 / k ** 0.9) * (0.4 + bloom) * np.exp(-t * (0.5 + 0.12 * k)) \
            * np.sin(2 * np.pi * f * k * t)
    return out * _ends(t, 0.01, 0.8, ring) / 2


def bass(midi: float, length: float) -> Any:
    import numpy as np                                          # noqa: PLC0415
    ring = length + 0.3
    t = _t(ring)
    f = _hz(midi)
    out = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
    return out * np.exp(-t * 0.9) * _ends(t, 0.012, 0.2, ring)


def kick(length: float = 0.35) -> Any:
    import numpy as np                                          # noqa: PLC0415
    t = _t(length)
    sweep = 48 + 70 * np.exp(-t / 0.04)
    return np.sin(2 * np.pi * np.cumsum(sweep) / RATE) * np.exp(-t / 0.09) \
        * _ends(t, 0.002, 0.05, length)


def shaker(rng: Any, length: float = 0.09) -> Any:
    import numpy as np                                          # noqa: PLC0415
    t = _t(length)
    noise = rng.standard_normal(t.size).astype(np.float32)
    bright = np.diff(noise, prepend=0.0)                       # the low end taken out
    return bright * np.exp(-t / 0.025) * _ends(t, 0.004, 0.02, length) / 3


# ---------------------------------------------------------------------------
# Writing the piece
# ---------------------------------------------------------------------------

class _Track:
    """A stereo buffer notes are added into at a time, loudness and place
    (-1 left to 1 right)."""

    def __init__(self, seconds: float) -> None:
        import numpy as np                                      # noqa: PLC0415
        self.np = np
        self.left = np.zeros(int(seconds * RATE) + RATE * 4, dtype=np.float32)
        self.right = np.zeros_like(self.left)

    def add(self, sound: Any, at: float, gain: float, pan: float = 0.0) -> None:
        start = int(at * RATE)
        if start >= self.left.size:
            return
        sound = sound[: self.left.size - start]
        angle = (pan + 1) * math.pi / 4
        self.left[start:start + sound.size] += sound * (gain * math.cos(angle))
        self.right[start:start + sound.size] += sound * (gain * math.sin(angle))


def _reverb(signal: Any, seed: int, wet: float = 0.22, length: float = 2.2) -> Any:
    """A room around the instruments: the signal convolved, block by block,
    with a decaying noise tail of its own on each side."""
    import numpy as np                                          # noqa: PLC0415
    rng = np.random.default_rng(seed)
    t = np.arange(int(length * RATE), dtype=np.float32) / RATE
    tail = rng.standard_normal(t.size).astype(np.float32) * np.exp(-t / 0.55)
    # Darker as it dies away: a simple running average of the noise.
    tail = np.convolve(tail, np.ones(6, dtype=np.float32) / 6, mode="same")
    tail[: int(0.012 * RATE)] = 0                               # a short gap before the room
    tail /= np.sqrt(np.sum(tail * tail))
    block = 1 << 16
    size = 1 << int(math.ceil(math.log2(block + tail.size)))
    spectrum = np.fft.rfft(tail, size)
    out = np.zeros(signal.size + tail.size, dtype=np.float32)
    for first in range(0, signal.size, block):
        piece = signal[first:first + block]
        if not piece.any():
            continue
        wetted = np.fft.irfft(np.fft.rfft(piece, size) * spectrum, size)
        end = min(out.size, first + piece.size + tail.size)
        out[first:end] += wetted[: end - first].astype(np.float32)
    return signal + wet * out[: signal.size]


_RHYTHMS = {
    # Beats each note of a bar of the tune lasts; None is a rest.
    "slow": ((2, 2), (3, 1), (4,), (2, None, 1), (1, 1, 2)),
    "flowing": ((1, 1, 2), (2, 1, 1), (2, 2), (1, 1, 1, 1), (3, 1)),
    "lively": ((1, 1, 1, 1), (0.5, 0.5, 1, 2), (1, 1, 2), (2, 1, 1), (1, 0.5, 0.5, 2)),
}


def compose(name: str, seconds: float, seed: int = 0) -> tuple[Any, Any]:
    """The piece *name*, *seconds* long, as two float32 arrays (left, right)."""
    import numpy as np                                          # noqa: PLC0415

    piece = PIECES[piece_for(name)]
    rng = np.random.default_rng([VERSION, int(seed) & 0xFFFFFFFF, sum(map(ord, name))])
    seconds = max(SHORTEST, float(seconds))
    beat = 60.0 / piece["bpm"]
    bar = beat * 4
    scale, root, parts = piece["scale"], piece["root"], piece["parts"]
    # The last chord starts on a bar line and rings to the end of the film.
    last = max(bar * 2, math.floor((seconds - LAST_CHORD) / bar) * bar)
    bars = int(round(last / bar))
    track = _Track(seconds)
    cache: dict[tuple[Any, ...], Any] = {}

    def note(kind: str, midi: float, length: float, **kw: Any) -> Any:
        key = (kind, midi, round(length, 2), tuple(sorted(kw.items())))
        if key not in cache:
            cache[key] = {"piano": piano, "pluck": pluck, "pad": pad, "drone": drone,
                          "bass": bass}[kind](midi, length, **kw)
        return cache[key]

    def degree(d: int, octave: int = 0) -> int:
        return root + scale[d % len(scale)] + 12 * (octave + d // len(scale))

    def chord_at(n: int) -> int:
        chords = piece["chords"]
        return 0 if not chords else chords[(n // 2) % len(chords)]

    def triad(d: int) -> list[int]:
        return [degree(d), degree(d + 2), degree(d + 4)]

    for n in range(bars + 1):
        at = n * bar
        ending = n == bars
        chord = 0 if ending else chord_at(n)
        tones = triad(chord) if piece["chords"] else [root, root + 7]
        hold = (seconds - at) if ending else bar * (2 if n % 2 == 0 else 1)
        if "pad" in parts and (ending or n % 2 == 0):
            for midi in tones:
                track.add(note("pad", midi - 12, min(hold, bar * 2)), at, 0.05, pan=-0.2)
        if "drone" in parts:
            # Pa, Sa, Sa, low Sa: the tanpura's turn, once every bar.
            for k, midi in enumerate((root - 5, root, root, root - 12)):
                if ending and k:
                    break
                track.add(note("drone", midi - 12, bar * 1.2 if not ending else hold),
                          at + k * beat, 0.07, pan=(-0.35, 0.3, 0.35, -0.3)[k])
        if "bass" in parts and n >= 1:
            low = degree(chord, -2)
            track.add(note("bass", low, beat * 2 if not ending else hold), at, 0.16)
            if not ending and piece["pattern"] == "lively":
                track.add(note("bass", low, beat), at + beat * 2.5, 0.1)
        if ending:
            for k, midi in enumerate(tones + [degree(0, 1)]):
                kind = "pluck" if "veena" in parts else "piano"
                track.add(note(kind, midi, hold), at + k * 0.06, 0.12, pan=-0.3 + 0.2 * k)
            break
        # The chords played one note at a time, from the first bar on.
        if ("arpeggio" in parts or "pluck" in parts) and n >= 1:
            kind = "piano" if "arpeggio" in parts else "pluck"
            # Up and back down, an octave under the tune.
            order = [m - 12 for m in (*tones, tones[0] + 12, tones[2], tones[1])]
            step = beat if piece["pattern"] == "slow" else beat / 2
            count = int(round(bar / step))
            for k in range(count):
                if piece["pattern"] == "slow" and k % 4 == 3 and rng.random() < 0.4:
                    continue
                midi = order[k % len(order)]
                gain = 0.07 * (1.15 if k % 4 == 0 else 1.0) * (0.85 + 0.3 * rng.random())
                track.add(note(kind, midi, step * 1.5), at + k * step
                          + rng.normal(0, 0.004), gain, pan=0.25 if k % 2 else -0.05)
        if "beat" in parts and n >= 2:
            for k in range(4):
                if k in (0, 2):
                    track.add(kick(), at + k * beat, 0.16)
                for half in (0.0, 0.5):
                    track.add(shaker(rng), at + (k + half) * beat,
                              0.05 if half else 0.03, pan=0.4)

    # The tune: four-bar phrases from the third bar, a rest after every two.
    tune = "veena" if "veena" in parts else "piano" if "piano" in parts else None
    if tune:
        rhythms = _RHYTHMS[piece["pattern"]]
        here = 2                               # the third of the key
        for n in range(2, bars):
            if (n - 2) % 12 >= 8:              # the phrase breathes
                continue
            rhythm = rhythms[int(rng.integers(len(rhythms)))]
            t = n * bar
            chord = chord_at(n)
            for k, length in enumerate(rhythm):
                if length is None:
                    t += beat
                    continue
                if k == 0 and piece["chords"]:
                    # On the bar line, a note of the chord nearest the tune.
                    choices = [chord + i for i in (0, 2, 4)]
                    choices = [c + len(scale) * round((here - c) / len(scale)) for c in choices]
                    here = min(choices, key=lambda c: abs(c - here))
                else:
                    here += int(rng.choice((-2, -1, -1, 0, 1, 1, 2)))
                here = min(max(here, 0), len(scale) + 4)
                midi = degree(here)
                seconds_long = length * beat
                if tune == "veena":
                    sound = note("pluck", midi, seconds_long, slide=bool(k == 0 or length >= 2))
                else:
                    sound = note("piano", midi, seconds_long)
                gain = 0.11 if tune == "piano" else 0.14
                track.add(sound, t + rng.normal(0, 0.005), gain * (0.85 + 0.25 * rng.random()),
                          pan=0.1)
                t += seconds_long

    size = int(seconds * RATE)
    left = _reverb(track.left, seed * 2 + 1)[:size]
    right = _reverb(track.right, seed * 2 + 2)[:size]
    # The last half second fades to nothing, so the film never ends on a cut.
    tail = min(size, int(0.5 * RATE))
    fade = np.linspace(1, 0, tail, dtype=np.float32)
    left[-tail:] *= fade
    right[-tail:] *= fade
    loudest = max(float(np.abs(left).max()), float(np.abs(right).max()), 1e-6)
    rms = math.sqrt(float(np.mean(left * left) + np.mean(right * right)) / 2) or 1e-6
    gain = min(PEAK / loudest, LOUDEST_RMS / rms)
    return left * gain, right * gain


def write(name: str, seconds: float, path: Path | str, seed: int = 0) -> Path:
    """Write the piece *name*, *seconds* long, to *path* as a 16-bit stereo
    WAV and return *path*."""
    import numpy as np                                          # noqa: PLC0415

    left, right = compose(name, seconds, seed)
    pcm = np.empty(left.size * 2, dtype="<i2")
    pcm[0::2] = np.clip(left * 32767, -32768, 32767).astype("<i2")
    pcm[1::2] = np.clip(right * 32767, -32768, 32767).astype("<i2")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())
    return path
