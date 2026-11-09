"""The Update window: how to update this Ninaivu, bringing a library across
from an older one beside it, and going back.

Ninaivu never asks the internet whether a newer version is out. Updating a
portable copy is three things the household does: download the newer
version, unpack it beside this folder, and open it; the newer one offers to
bring the library across (desktop/portable.py) and keeps a way back. Until
4.0 the Control Panel said all of that in one small line at its foot, and
the offer and the way back were plain message boxes. This draws them as one
window in the panel's own look, in three parts:

* :func:`how_to_update`, opened by **Update…** in the panel: the three steps,
  with the downloads page and this folder a press away, and, when this copy
  came from an older one, going back.
* :func:`offer_window`, opened by the newer copy at its first start: which
  versions, what comes across and where, and each step ticked off as it is
  done (the older one stopped, the library moved), then what to do next.

What the windows say is worked out apart from Tk (:func:`update_steps`,
:func:`what_comes`), so it is tested without a display.
"""
from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

try:                                    # what the windows say is read without Tk too
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk
except ImportError:                     # a Python built without it: no windows here
    tk = tkfont = ttk = None            # type: ignore[assignment]

from . import theme

#: Where the downloads are. Opened only when somebody presses the button.
DOWNLOADS = "https://github.com/javajaga-usa/Ninaivu/releases/latest"

#: A step's state, and the mark it is drawn with.
WAITING, DOING, DONE, FAILED, LATER = "waiting", "doing", "done", "failed", "later"
MARKS = {WAITING: "○", DOING: "◐", DONE: "✓", FAILED: "✕", LATER: "○"}
PLAIN_MARKS = {WAITING: "o", DOING: "...", DONE: "+", FAILED: "x", LATER: "o"}


# -- what the windows say ---------------------------------------------------------

def update_steps(version: str, beside: Path | str | None) -> list[dict[str, str]]:
    """The three steps of an update, for the copy of *version* whose folder
    is in *beside*."""
    where = str(beside) if beside else "the folder this one is in"
    return [
        {"title": "Download the newer version",
         "detail": "Ninaivu never looks for updates by itself. The button opens the "
                   "downloads page in your browser; take the one for this computer."},
        {"title": "Unpack it beside this folder",
         "detail": f"In {where}, on the same drive, next to Ninaivu {version}. "
                   "Keep this folder: it is the way back."},
        {"title": "Open the newer Ninaivu",
         "detail": "It offers to bring your library across, stops this one first, and "
                   "copies the index before it changes anything."},
    ]


def what_comes(older: Any, version: str) -> dict[str, Any]:
    """What bringing *older*'s library into *version* moves, and how, for the
    offer: its versions, a line per thing, and whether it moves or stays."""
    older_version = getattr(older, "version", None) or "an older version"
    state = Path(older.state)
    stays = not state.is_relative_to(Path(older.path))
    things = [("The index and settings", str(state))]
    models = Path(older.models)
    if models.is_dir() and not models.is_relative_to(state):
        things.append(("The AI models", str(models)))
    control = Path(older.control)
    if control.is_dir() and not control.is_relative_to(state):
        things.append(("The Control Panel's settings", str(control)))
    how = ("They stay where they are, and Ninaivu " + version + " takes them over."
           if stays else "They are moved, not copied, on the same drive: it takes a moment.")
    return {"from": older_version, "to": version, "folder": str(older.path),
            "things": things, "how": how, "stays": stays}


# -- the look ------------------------------------------------------------------------

class Look:
    """The panel's colours and type, for a window drawn before the panel is
    (the offer, at the first start) or by it (:meth:`of`)."""

    def __init__(self, root: tk.Misc, palette: dict[str, str] | None = None,
                 fonts: dict[str, tkfont.Font] | None = None) -> None:
        if palette is None:
            try:
                dark = theme.system_prefers_dark()
            except Exception:                                # noqa: BLE001 - the light look, then
                dark = False
            palette = theme.palette(theme.resolve("system", dark))
        if fonts is None:
            from .app import font_families                  # noqa: PLC0415
            ui, _mono = font_families(root)
            fonts = {
                "caps": tkfont.Font(root, family=ui, size=8, weight="bold"),
                "headline": tkfont.Font(root, family=ui, size=17, weight="bold"),
                "pill": tkfont.Font(root, family=ui, size=10, weight="bold"),
                "title": tkfont.Font(root, family=ui, size=11, weight="bold"),
                "body": tkfont.Font(root, family=ui, size=10),
                "small": tkfont.Font(root, family=ui, size=9),
                "button": tkfont.Font(root, family=ui, size=10, weight="bold"),
            }
        fonts.setdefault("headline", fonts.get("value") or fonts["title"])
        self.p, self.f = palette, fonts
        style = ttk.Style(root)
        style.configure("Update.TButton", font=self.f["button"], padding=(14, 5),
                        background=palette["surface"], foreground=palette["text"],
                        bordercolor=palette["border"], lightcolor=palette["surface"],
                        darkcolor=palette["surface"], focuscolor=palette["accent_line"])
        style.map("Update.TButton", background=[("active", palette["surface_hover"])])
        style.configure("UpdateAccent.TButton", font=self.f["button"], padding=(14, 5),
                        background=palette["accent_fill"], foreground=palette["accent_fg"],
                        bordercolor=palette["accent_fill"], lightcolor=palette["accent_fill"],
                        darkcolor=palette["accent_fill"], focuscolor=palette["accent_fg"])
        style.map("UpdateAccent.TButton",
                  background=[("disabled", palette["accent_fill_off"]),
                              ("active", palette["accent_fill_hover"])],
                  foreground=[("disabled", palette["accent_fg"])])

    @classmethod
    def of(cls, dashboard: Any) -> "Look":
        fonts = dict(dashboard.fonts)
        fonts["headline"] = fonts["value"]
        return cls(dashboard.root, dashboard.palette, fonts)

    # The pieces every part of the window is made of.

    def label(self, parent: tk.Misc, text: str = "", *, font: str = "body", fg: str = "text",
              bg: str = "bg", wrap: int = 520, **options: Any) -> tk.Label:
        return tk.Label(parent, text=text, font=self.f[font], fg=self.p[fg], bg=self.p[bg],
                        anchor="w", justify="left", wraplength=wrap, **options)

    def frame(self, parent: tk.Misc, *, bg: str = "bg", line: str | None = None,
              **options: Any) -> tk.Frame:
        extra = ({"highlightthickness": 1, "highlightbackground": self.p[line]} if line else {})
        return tk.Frame(parent, bg=self.p[bg], **extra, **options)

    def button(self, parent: tk.Misc, text: str, command: Callable[[], Any], *,
               accent: bool = False) -> ttk.Button:
        return ttk.Button(parent, text=text, command=command,
                          style="UpdateAccent.TButton" if accent else "Update.TButton")


def _mark(state: str) -> str:
    from .app import _plain                                 # noqa: PLC0415
    return (PLAIN_MARKS if _plain else MARKS)[state]


def _window(parent: tk.Misc, look: Look, title: str) -> tk.Toplevel:
    window = tk.Toplevel(parent, bg=look.p["bg"], padx=26, pady=22)
    window.title(title)
    window.resizable(False, False)
    try:
        if parent.winfo_viewable():
            window.transient(parent)
    except tk.TclError:
        pass
    return window


def _head(window: tk.Misc, look: Look, kicker: str, title: str) -> None:
    from .app import small_caps                             # noqa: PLC0415
    look.label(window, small_caps(kicker), font="caps", fg="accent2").pack(anchor="w")
    look.label(window, title, font="headline").pack(anchor="w", pady=(2, 10))


def _versions(window: tk.Misc, look: Look, before: str, after: str | None = None) -> None:
    row = look.frame(window)
    row.pack(anchor="w", pady=(0, 12))
    for text, colours in ((before, ("surface2", "text2", "border")),
                          *(((" → ", None), (after, ("accent_weak", "accent", "accent_line")))
                            if after else ())):
        if colours is None:
            look.label(row, text, font="pill", fg="text3").pack(side="left")
            continue
        bg, fg, line = colours
        pill = look.frame(row, bg=bg, line=line, padx=10, pady=2)
        pill.pack(side="left")
        look.label(pill, text, font="pill", fg=fg, bg=bg, wrap=0).pack()


def _steps(window: tk.Misc, look: Look, steps: list[dict[str, str]], *,
           numbered: bool) -> list[tuple[tk.Label, tk.Label]]:
    """A card with a row per step: its number (or mark), title and detail;
    returns each row's mark and detail labels, to change as the steps go."""
    card = look.frame(window, bg="surface", line="border", padx=16, pady=10)
    card.pack(fill="x", pady=(0, 12))
    rows = []
    for n, step in enumerate(steps, start=1):
        row = look.frame(card, bg="surface")
        row.pack(fill="x", pady=6)
        mark = look.label(row, str(n) if numbered else _mark(WAITING), font="title",
                          fg="accent" if numbered else "text3", bg="surface", wrap=0, width=2)
        mark.pack(side="left", anchor="n")
        words = look.frame(row, bg="surface")
        words.pack(side="left", fill="x", expand=True, padx=(8, 0))
        look.label(words, step["title"], font="title", bg="surface", wrap=440).pack(anchor="w")
        detail = look.label(words, step.get("detail", ""), font="small", fg="text2", bg="surface",
                            wrap=440)
        detail.pack(anchor="w")
        if step.get("action"):
            text, command = step["action"]
            look.button(words, text, command).pack(anchor="w", pady=(6, 0))
        rows.append((mark, detail))
    return rows


# -- updating: the steps, and going back ------------------------------------------------

def how_to_update(parent: tk.Misc, look: Look, *, version: str, folder: Path | None,
                  go_back: tuple[str, str, Callable[[], Any]] | None = None,
                  open_url: Callable[[str], Any] = webbrowser.open,
                  open_folder: Callable[[Path], Any] | None = None) -> tk.Toplevel:
    """The window **Update…** opens. *go_back* is ``(version, folder, ask)``
    when this copy's library came from an older one beside it."""
    if open_folder is None:
        from .tray import open_file as open_folder          # noqa: PLC0415
    window = _window(parent, look, "Update Ninaivu")
    _head(window, look, "Update", "Updating Ninaivu")
    _versions(window, look, f"This is Ninaivu {version}")
    beside = folder.parent if folder is not None else None
    steps = update_steps(version, beside)
    steps[0]["action"] = ("Open the downloads page", lambda: open_url(DOWNLOADS))
    if beside is not None:
        steps[1]["action"] = ("Show the folder", lambda: open_folder(beside))
    _steps(window, look, steps, numbered=True)
    if go_back is not None:
        older, where, ask = go_back
        back = look.frame(window, bg="surface", line="border", padx=16, pady=12)
        back.pack(fill="x", pady=(0, 12))
        look.label(back, f"Going back to Ninaivu {older}", font="title", bg="surface").pack(anchor="w")
        look.label(back, f"This library came from {where}. Going back gives that version the "
                         "index as it was before this update; a copy of everything done since "
                         "is kept first.", font="small", fg="text2", bg="surface",
                   wrap=470).pack(anchor="w", pady=(2, 8))

        def back_now() -> None:
            window.destroy()
            ask()
        look.button(back, f"Go back to {older}…", back_now).pack(anchor="w")
    foot = look.frame(window)
    foot.pack(fill="x", pady=(4, 0))
    look.label(foot, "Your photographs are never touched.", font="small", fg="text3",
               wrap=300).pack(side="left")
    close = look.button(foot, "Close", window.destroy, accent=True)
    close.pack(side="right")
    window.bind("<Escape>", lambda _e: window.destroy())
    close.focus_set()
    return window


# -- bringing the library across ------------------------------------------------------------

def offer_window(root: tk.Misc, look: Look, older: Any, version: str,
                 bring: Callable[..., dict[str, Any]], stop: Callable[[Path], Any]) -> bool:
    """The newer copy's first start: offer to bring *older*'s library here.

    *bring* is ``portable.bring_here`` and *stop* the way the older Ninaivu
    is stopped; each step is ticked off as it is done. Waits for the window
    to close. True when the library was brought, False when it was not.
    """
    about = what_comes(older, version)
    window = _window(root, look, "Bring your library across")
    _head(window, look, "Update", "Bring your library across")
    _versions(window, look, f"Ninaivu {about['from']}", f"Ninaivu {version}")
    said = look.label(window, f"Ninaivu {about['from']} is in the folder beside this one, "
                              f"{about['folder']}. Bring its library here?", fg="text2")
    said.pack(anchor="w", pady=(0, 10))

    things = look.frame(window, bg="surface", line="border", padx=16, pady=10)
    things.pack(fill="x", pady=(0, 10))
    look.label(things, "What comes across", font="title", bg="surface").pack(anchor="w", pady=(0, 4))
    for name, where in about["things"]:
        row = look.frame(things, bg="surface")
        row.pack(fill="x", pady=1)
        look.label(row, name, font="small", fg="text", bg="surface", wrap=0, width=24).pack(side="left")
        look.label(row, where, font="small", fg="text2", bg="surface", wrap=300).pack(side="left")
    look.label(things, about["how"], font="small", fg="text3", bg="surface",
               wrap=470).pack(anchor="w", pady=(6, 0))

    steps = [{"title": f"Stop Ninaivu {about['from']}", "detail": "If it is running."},
             {"title": "Bring the library here",
              "detail": "Its folder keeps its program, and a note saying where the library went."},
             {"title": "Copy the index before the first start",
              "detail": "When you press Start, so going back stays possible."}]
    rows = _steps(window, look, steps, numbered=False)
    problem = look.label(window, "", fg="danger")
    problem.pack(anchor="w")
    note = look.label(window, "Your photographs are not touched. Going back, in the Control "
                              "Panel's Update window, returns everything to the older folder.",
                      font="small", fg="text3")
    note.pack(anchor="w", pady=(0, 12))
    buttons = look.frame(window)
    buttons.pack(fill="x")
    result = {"brought": False}

    def set_step(n: int, state: str, detail: str | None = None) -> None:
        mark, words = rows[n]
        mark.configure(text=_mark(state), fg=look.p[{DONE: "success", FAILED: "danger",
                                                      DOING: "accent"}.get(state, "text3")])
        if detail is not None:
            words.configure(text=detail)

    # Tk is drawn only from its own thread: the move's thread hands each
    # change over, and this takes them in as the window waits.
    handed: queue.Queue[Callable[[], Any]] = queue.Queue()

    def from_worker(call: Callable[[], Any]) -> None:
        handed.put(call)

    def take_in() -> None:
        while True:
            try:
                handed.get_nowait()()
            except queue.Empty:
                break
        try:
            window.after(80, take_in)
        except tk.TclError:
            pass                                             # the window is closed

    def finished(done: dict[str, Any] | BaseException) -> None:
        if isinstance(done, BaseException):
            set_step(1 if rows[0][0].cget("text") == _mark(DONE) else 0, FAILED)
            problem.configure(text=str(done))
            yes.configure(text="Try again", state="normal")
            no.configure(text="Close", state="normal")
            return
        result["brought"] = True
        set_step(1, DONE)
        set_step(2, LATER)
        said.configure(text=f"Your library is here, with Ninaivu {version}. Press Start: the "
                            "first start copies the index before this version changes anything."
                       + (" It also starts when you sign in, as the older one did."
                          if done.get("sign_in") else ""))
        yes.pack_forget()
        no.configure(text="Done", style="UpdateAccent.TButton", state="normal")
        no.focus_set()

    def go() -> None:
        yes.configure(state="disabled")
        no.configure(state="disabled")
        problem.configure(text="")
        set_step(0, DOING)

        def stopped(state: Path) -> None:
            stop(state)
            from_worker(lambda: (set_step(0, DONE), set_step(1, DOING)))

        def work() -> None:
            try:
                done = bring(older, stop_older=stopped)
            except Exception as exc:                         # noqa: BLE001 - said in the window
                done = exc
            from_worker(lambda: finished(done))
        threading.Thread(target=work, name="ninaivu-bring-across", daemon=True).start()

    def close() -> None:
        if str(no.cget("state")) == "disabled":
            return                                           # a move under way finishes first
        window.destroy()

    no = look.button(buttons, "Not now", close)
    no.pack(side="right")
    yes = look.button(buttons, "Bring my library across", go, accent=True)
    yes.pack(side="right", padx=(0, 8))
    window.bind("<Escape>", lambda _e: close())
    window.protocol("WM_DELETE_WINDOW", close)
    take_in()
    yes.focus_set()
    try:
        window.grab_set()
    except tk.TclError:
        pass
    root.wait_window(window)
    return result["brought"]
