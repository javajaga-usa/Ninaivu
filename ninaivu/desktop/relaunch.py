"""Restart Ninaivu from outside it: what the console's Restart button starts.

A server cannot restart itself - the new one needs the ports the old one is
still holding - so the console starts this, detached, and answers the browser.
This then does exactly what the desktop panel's Restart does: asks the server
to stop properly (active files finish, jobs checkpoint), waits for it to go,
and starts it again with the arguments it was running with.

    python -m ninaivu.desktop.relaunch [--mode standard|performance|power-saving]
                                      [--network on|off]

Output goes to restart.log in the log folder (.ninaivu-control, or logs/ in the
portable build), which the console points at if the
server does not come back.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

from ..utils.resources import MODES

#: How long a stop may take before giving up. A consolidation copying a large
#: video finishes that file first, and a slow USB disk can make that minutes.
STOP_PATIENCE = 20 * 60


def with_network(arguments, on):
    """The launch arguments with the network choice made explicit.

    Only the family app's binding is rewritten. An --admin-host somebody set
    on purpose - a console kept to this machine while the family app serves
    the house - is theirs and stays. Off is also enforced by the saved
    setting at start-up; saying --local-only here as well keeps the desktop
    panel's links pointing at this machine.
    """
    kept, skip = [], False
    for arg in arguments:
        if skip:
            skip = False
            continue
        if arg == '--host':
            skip = True
            continue
        if arg == '--local-only' or str(arg).startswith('--host='):
            continue
        kept.append(str(arg))
    return kept + (['--host', '0.0.0.0'] if on else ['--local-only'])


def say(message):
    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=MODES)
    parser.add_argument('--network', choices=('on', 'off'))
    args = parser.parse_args(argv)

    # The request that started this is still being answered; let it go out.
    time.sleep(1.0)
    from .control import Controller

    controller = Controller()
    # The running server's own mode, not the panel's last saved choice: the
    # server may have been started some other way.
    mode = args.mode or os.environ.get('NINAIVU_RESOURCE_MODE') or controller.mode
    say(f'restart requested (mode {mode}'
        + (f', network {args.network})' if args.network else ')'))
    deadline = time.monotonic() + STOP_PATIENCE
    while True:
        try:
            say(controller.stop())
            break
        except RuntimeError as exc:
            if 'did not accept' in str(exc) or time.monotonic() > deadline:
                raise
            say(f'still stopping: {exc}')
    # stop() re-reads the running settings, which can guess the mode back from
    # the worker count; the choice made in the console is the one to start with.
    # Without the running server's arguments the panel starts from its own
    # defaults, and the saved setting alone decides; rewriting an empty list
    # would replace those defaults with a lone flag.
    if args.network and controller.settings.get('arguments'):
        controller.settings['arguments'] = with_network(
            controller.settings['arguments'], args.network == 'on')
    controller.save_mode(mode)
    say(controller.start())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:                                             # noqa: BLE001
        say('restart failed\n' + traceback.format_exc())
        sys.exit(1)
