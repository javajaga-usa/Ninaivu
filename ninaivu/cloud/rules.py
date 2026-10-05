"""What is left out of the cloud backup, because the household said so.

Everything in the library goes to Drive unless something keeps it back. Until
now the only thing that could was being hidden. But a library swept up from
old drives is not all family photographs: it has films somebody downloaded,
lecture recordings, a folder of things nobody would miss — and by size those
are most of it. Sending a terabyte of them first, at the speed a home
connection uploads, is weeks during which the photographs behind them are not
backed up at all.

So there are rules, and they are deliberately few:

* **which kinds** — everything, everything but video, or photographs only;
* **how large** — nothing over so many megabytes;
* **which folders** — not these folders of the library, or anything in them;
* **which names** — not a file whose name or path has one of these words in it.

A file a rule keeps back is *set aside*, not failed and not forgotten: it is
listed with the rule that holds it, it is not retried, and the moment the rule
is taken away it is owed again like any other file.

**A rule never reaches into Drive.** What has already been uploaded stays
uploaded, whatever the rules say afterwards. Taking something out of Drive is
a separate act, done in Google Drive itself.

The same rule is asked in three places, and has to give the same answer in
each: as SQL over the index, when the library is offered to the queue; as SQL
over the queue, when a rule changes and what is already waiting has to be set
aside or released; and in Python, per file, just before it is sent — because a
rule can change while a queue drains, and the later answer is the right one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Rules", "KINDS", "REASON", "clean_folders", "clean_words"]

#: What may go, by kind.
KINDS = ("all", "no_video", "pictures")

#: How a row the rules set aside is recognised again, to release it.
REASON = "kept back by a backup rule"

#: More than this many of either is a list nobody is maintaining by hand.
MAX_ENTRIES = 200


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean_folders(value: Any) -> list[str]:
    """Folder paths inside a library, as the index writes them."""
    out: list[str] = []
    for entry in _entries(value):
        parts = [p for p in entry.replace("\\", "/").split("/") if p and p != "."]
        if not parts or ".." in parts:
            continue
        path = "/".join(parts)
        if path.lower() not in {o.lower() for o in out}:
            out.append(path)
    return out[:MAX_ENTRIES]


def clean_words(value: Any) -> list[str]:
    out: list[str] = []
    for entry in _entries(value):
        # One letter would match nearly every path there is.
        if len(entry) >= 2 and entry.lower() not in {o.lower() for o in out}:
            out.append(entry)
    return out[:MAX_ENTRIES]


def _entries(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and str(v).strip()]


@dataclass(frozen=True)
class Rules:
    kinds: str = "all"
    #: Megabytes. 0 is no limit.
    max_mb: int = 0
    folders: tuple[str, ...] = ()
    words: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg) -> "Rules":
        kinds = str(getattr(cfg, "cloud_kinds", "all") or "all")
        return cls(
            kinds=kinds if kinds in KINDS else "all",
            max_mb=max(0, int(getattr(cfg, "cloud_max_mb", 0) or 0)),
            folders=tuple(clean_folders(getattr(cfg, "cloud_skip_folders", []) or [])),
            words=tuple(clean_words(getattr(cfg, "cloud_skip_words", []) or [])))

    @property
    def any(self) -> bool:
        return bool(self.kinds != "all" or self.max_mb or self.folders or self.words)

    def public(self) -> dict[str, Any]:
        return {"kinds": self.kinds, "max_mb": self.max_mb,
                "folders": list(self.folders), "words": list(self.words)}

    # -- one file ---------------------------------------------------------

    def why(self, rel_path: str, size: int = 0, kind: str = "") -> str | None:
        """The rule that keeps this file back, in words, or None if none does."""
        kind = str(kind or "")
        if self.kinds == "no_video" and kind == "video":
            return f"{REASON}: videos are not backed up"
        if self.kinds == "pictures" and kind != "picture":
            return f"{REASON}: only photographs are backed up"
        if self.max_mb and int(size or 0) > self.max_mb * 1024 * 1024:
            return f"{REASON}: larger than {self.max_mb:,} MB"
        path = str(rel_path or "").replace("\\", "/")
        lowered = path.lower()
        for folder in self.folders:
            if lowered.startswith(folder.lower() + "/"):
                return f"{REASON}: in the folder {folder}"
        for word in self.words:
            if word.lower() in lowered:
                return f"{REASON}: “{word}” is in its name"
        return None

    # -- many files -------------------------------------------------------

    def held(self, prefix: str = "") -> tuple[str, list[Any]]:
        """SQL that is true of a row the rules keep back, and its arguments.

        Over any table with ``kind``, ``size`` and ``rel_path`` — the index
        and the queue both have them. ``("0", [])`` when there are no rules.
        The SQL and :meth:`why` must agree: LIKE without a collation is
        case-insensitive for ASCII, which is what ``lower()`` gives there.
        """
        p = prefix
        parts: list[str] = []
        args: list[Any] = []
        if self.kinds == "no_video":
            parts.append(f"{p}kind = 'video'")
        elif self.kinds == "pictures":
            parts.append(f"{p}kind != 'picture'")
        if self.max_mb:
            parts.append(f"{p}size > ?")
            args.append(self.max_mb * 1024 * 1024)
        for folder in self.folders:
            parts.append(f"{p}rel_path LIKE ? ESCAPE '\\'")
            args.append(_like(folder) + "/%")
        for word in self.words:
            parts.append(f"{p}rel_path LIKE ? ESCAPE '\\'")
            args.append("%" + _like(word) + "%")
        if not parts:
            return "0", []
        return "(" + " OR ".join(parts) + ")", args

    def apply(self, conn) -> dict[str, int]:
        """Bring the queue into line with the rules, now.

        What is waiting and is now kept back is set aside; what was set aside
        by a rule that no longer holds is owed again. A file part-way up is
        left to finish or be stopped by the upload itself, and nothing that
        has been uploaded is touched.
        """
        held, args = self.held()
        released = conn.execute(
            "UPDATE cloud_uploads SET state='pending', error='', attempts=0 "
            f"WHERE state='skipped' AND error LIKE ? AND NOT {held}",
            (REASON + "%", *args)).rowcount
        set_aside = 0
        if self.any:
            # The reason is written per row by the uploader when it meets one;
            # in bulk, the rule's name is enough to release it by later.
            set_aside = conn.execute(
                "UPDATE cloud_uploads SET state='skipped', error=?, resume_url='' "
                f"WHERE state='pending' AND {held}", (REASON, *args)).rowcount
        conn.commit()
        return {"set_aside": int(set_aside), "released": int(released)}

    def preview(self, conn) -> dict[str, int]:
        """What these rules would hold of what is still to go, and how much
        already in Drive they would have held."""
        held, args = self.held()
        waiting = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cloud_uploads "
            f"WHERE (state IN ('pending', 'uploading') OR (state='skipped' AND error LIKE ?)) "
            f"AND {held}", (REASON + "%", *args)).fetchone()
        still = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cloud_uploads "
            f"WHERE (state IN ('pending', 'uploading') OR (state='skipped' AND error LIKE ?)) "
            f"AND NOT {held}", (REASON + "%", *args)).fetchone()
        sent = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cloud_uploads "
            f"WHERE state='done' AND {held}", args).fetchone()
        return {"held": int(waiting[0]), "held_bytes": int(waiting[1]),
                "to_go": int(still[0]), "to_go_bytes": int(still[1]),
                "already_sent": int(sent[0]), "already_sent_bytes": int(sent[1])}
