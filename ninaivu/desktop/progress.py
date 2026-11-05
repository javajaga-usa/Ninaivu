"""What the Control Panel's progress card says while Ninaivu is not serving yet.

A start can be held up before Ninaivu serves anything: an index moved or
switched to another folder, one brought in from another computer, the copy
taken before an update, a check or repair of the index, the old server
finishing what it had in hand during a restart. Each says in its note how far
it has got (server/starting.py). The card shows that, whoever began it: the
panel, the console's Restart, the tray, a start at sign-in.

The words are worked out here, without Tk, so that they are tested on every
computer; the card that draws them is in app.py.
"""
from __future__ import annotations

from .control import describe_wait

#: Steps whose words alone say little.
TITLES = {
    'starting': 'Starting Ninaivu',
    'starting Ninaivu again': 'Starting Ninaivu again',
    'opening the index': 'Opening the index',
    'finishing what is in hand': 'Finishing what is in hand',
    'waiting for Ninaivu to finish what is in hand': 'Finishing what is in hand',
}
#: What a step's detail line says when the step itself has none to give.
DETAILS = {
    'Starting Ninaivu': 'Loading its programs and settings',
    'Starting Ninaivu again': 'Loading its programs and settings',
    'Opening the index': 'Getting the library ready to serve',
    'Finishing what is in hand': 'Files being worked on are finished first, so nothing is left half done',
}
#: The card's small heading for each kind of wait.
KINDS = {'starting': 'Starting', 'restarting': 'Restarting', 'stopping': 'Stopping'}


def _first_up(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:]


def describe_step(step: str | None) -> tuple[str, str]:
    """The card's title and the line under it, for a start note's *step*.

    ``'checking the index: index.db'`` is ``('Checking the index',
    'index.db')``: what is done, then where it is at.
    """
    step = str(step or 'starting').strip()
    title, _, detail = step.partition(': ')
    title = TITLES.get(title, _first_up(title))
    if not detail:
        return title, DETAILS.get(title, '')
    first = detail.split()[0].rstrip(',')
    # A file's name stays as it is written: index.db, not Index.db.
    return title, detail if '.' in first else _first_up(detail)


def percent(share: float | None) -> str:
    """``'42%'``, or empty when how far is not known. Never 100% until done."""
    if share is None:
        return ''
    whole = int(max(0.0, min(1.0, float(share))) * 100)
    return f'{min(whole, 99)}%' if share < 1 else '100%'


def heading(kind: str, seconds: float | None) -> str:
    """``'Restarting · 3 minutes'``: the kind of wait and how long so far."""
    label = KINDS.get(kind, _first_up(kind or 'Starting'))
    if seconds is None or seconds < 1:
        return label
    return f'{label} · {describe_wait(seconds)}'


def remaining(began: tuple[float, float] | None, share: float | None, now: float) -> str:
    """``'About 3 minutes left'`` from how fast this step has gone so far.

    *began* is ``(when, how far)`` at the step's first word. Nothing is said
    until the step has gone on long enough and far enough for its pace to
    mean something, nor for a step that cannot say how far it has got.
    """
    if began is None or share is None or share >= 1:
        return ''
    since, first = began
    seconds, gone = now - since, share - first
    if seconds < MEASURE_SECONDS or gone < MEASURE_SHARE:
        return ''
    left = (1 - share) * seconds / gone
    if left < 60:
        return 'Less than a minute left'
    if left > 12 * 3600:
        return ''
    return f'About {describe_wait(left)} left'


#: How long, and how far, a step goes before its time left is estimated.
MEASURE_SECONDS = 8.0
MEASURE_SHARE = 0.03
