"""Talk about this photo: questions about the photograph open in the viewer.

"What does the banner say?", "Which temple is this?", "என்ன விழா இது?": the
person asks, and the on-device writing model looks at the photograph itself
(Gemma 3 12B with its vision part, runtime/llm.py ``look``) and answers in a
few sentences, in the language the question was asked in.

What the model is given, apart from the picture, is only what the asker may
already see on that photograph in the viewer: when it was taken (when the
date is the camera's or the family's, not a file's), the town, the names of
the people in it as the family named them, the description the engine wrote
and the family's own caption. It is told to use those names and no others,
to say so when it cannot tell, and to copy writing as it is written.

Nothing here is kept: not the question, not the answer. The route is
api/api_photo_talk.py; the setting is ``photo_talk``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

#: The longest question, in characters.
MAX_QUESTION = 300
#: Earlier turns of the same talk handed back with a question, at most, and
#: how much of each answer is kept.
MAX_TURNS = 4
MAX_ANSWER = 1200
#: The longest answer asked for, in tokens: four sentences of Tamil fit.
ANSWER_TOKENS = 320

#: Where a date comes from that says nothing about when it was taken.
_FILE_DATES = ("mtime", "created")
_TAMIL = re.compile(r"[஀-௿]")


def language_of(text: str) -> str:
    """``"ta"`` for a question with Tamil letters in it, else ``"en"``."""
    return "ta" if _TAMIL.search(text or "") else "en"


def names_in(faces: Iterable[dict[str, Any]]) -> list[str]:
    """The named people in a photograph, left to right, each once."""
    placed: list[tuple[float, str]] = []
    for face in faces:
        name = str(face.get("person_name") or "").strip()
        if not name:
            continue
        box = face.get("bbox")
        if isinstance(box, str):
            try:
                box = json.loads(box)
            except ValueError:
                box = None
        left = float(box[0]) if isinstance(box, (list, tuple)) and box else 0.0
        placed.append((left, name))
    out: list[str] = []
    for _, name in sorted(placed, key=lambda p: p[0]):
        if name not in out:
            out.append(name)
    return out


def notes(row: dict[str, Any], *, names: Sequence[str], description: str | None) -> list[str]:
    """What is known of the photograph, as short English lines for the model."""
    lines: list[str] = []
    date = str(row.get("date_key") or "").strip()
    if date and row.get("date_source") not in _FILE_DATES:
        lines.append(f"Taken: {date}")
    place = ", ".join(p for p in (row.get("city"), row.get("country")) if p)
    if place:
        lines.append(f"Place: {place}")
    if names:
        lines.append("People in it, as the family named them, left to right: " + ", ".join(names))
    if description:
        lines.append(f"What the photo engine saw in it: {description.strip()}")
    caption = str(row.get("caption") or "").strip()
    if caption and row.get("caption_source") == "manual":
        lines.append(f"The family's caption: {caption}")
    return lines


def system_prompt(language: str, known: Sequence[str], household: str = "") -> str:
    tongue = "Tamil, in Tamil script" if language == "ta" else "English"
    family = f"the {household} family's" if household else "a family's"
    parts = [
        f"You are looking at one photograph from {family} own collection, kept on their "
        "own computer. Answer the person's question about this photograph.",
        f"Answer in {tongue}, in at most four short sentences, plainly and warmly, "
        "without lists, headings or bold.",
        "Say only what you can see in the photograph or what the notes below say. "
        "When you cannot tell, say so in a few words rather than guessing.",
        "Never give anybody a name the notes do not give. Do not guess anyone's age, "
        "religion, caste or what their body looks like.",
        "When asked what writing says, copy it exactly as it is written, in its own "
        "script, then say what it means if it is in another language.",
    ]
    if known:
        parts.append("Notes about this photograph:\n" + "\n".join(f"- {line}" for line in known))
    return "\n".join(parts)


def earlier(turns: Any) -> list[tuple[str, str]]:
    """The earlier questions and answers of this talk, as the page sends them,
    trimmed and capped; anything not shaped as a turn is dropped."""
    if not isinstance(turns, list):
        return []
    out: list[tuple[str, str]] = []
    for turn in turns[-MAX_TURNS:]:
        if not isinstance(turn, dict):
            continue
        asked = str(turn.get("q") or "").strip()[:MAX_QUESTION]
        said = str(turn.get("a") or "").strip()[:MAX_ANSWER]
        if asked and said:
            out.append((asked, said))
    return out


def model_messages(question: str, *, language: str, known: Sequence[str],
                   turns: Sequence[tuple[str, str]] = (), household: str = "") -> list[dict[str, str]]:
    """The chat handed to ``look``: the photograph goes with the first question."""
    messages = [{"role": "system", "content": system_prompt(language, known, household)}]
    for asked, answered in turns:
        messages.append({"role": "user", "content": asked})
        messages.append({"role": "assistant", "content": answered})
    messages.append({"role": "user", "content": question})
    return messages


_MARKS = re.compile(r"(\*\*|__|^#+\s*|^\s*[-*•]\s+)", re.MULTILINE)


def cleaned(text: str) -> str:
    """The model's answer as plain sentences: no markdown, no runs of blank lines."""
    out = _MARKS.sub("", text or "").strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out[:MAX_ANSWER]
