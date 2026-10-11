"""External audit 2026-10-11, OBS-01: secrets a message or traceback carries
by accident are masked before they reach the log file or the console's
Activity tab.

Each case plants an unmistakable sentinel and looks for it in what was
written, so a rule that stops matching is caught, not just a rule removed.
"""
from __future__ import annotations

import logging

import pytest

from ninaivu.utils import logs, redact

S = "SENTINEL9f8e7d"


@pytest.mark.parametrize("line, kept", [
    (f"could not reach https://anna:{S}@nas.local/dav/photos", "https://anna:***@nas.local"),
    (f"upload failed: https://bucket.example/x.jpg?X-Amz-Signature={S}&X-Amz-Date=20261011",
     "X-Amz-Date=20261011"),
    (f"GET /callback?state=abc&code={S} -> 400", "state=abc"),
    (f"login with password={S} refused", "password=***"),
    (f'{{"refresh_token": "{S}", "expires_in": 3599}}', '"expires_in": 3599'),
    (f"headers: Authorization: Bearer {S}", "Bearer ***"),
    (f"basic auth Basic {S}== rejected", "Basic ***"),
    (f"smtp_password: {S}", "smtp_password: ***"),
    (f"api_key={S} for gemini", "for gemini"),
])
def test_each_kind_of_secret_is_masked_and_the_rest_of_the_line_kept(line, kept):
    out = redact.redact(line)
    assert S not in out
    assert kept in out


def test_ordinary_lines_are_left_alone():
    for line in ("scanned 4,812 files in 37.2 s", "keyframes=8 for clip.mp4",
                 "token bucket refilled", "C:\\Users\\anna\\Pictures\\2024\\IMG_0001.JPG",
                 "https://ninaivu.local:8443/console"):
        assert redact.redact(line) == line


def test_a_webhook_whose_token_is_in_its_path_is_hidden_by_value():
    from types import SimpleNamespace

    hook = f"https://hooks.slack.example/services/T000/B000/{S}"
    redact.hide_settings(SimpleNamespace(notify_webhook=hook, notify_smtp_password="",
                                         notify_webhook_format="json", notify_smtp_host="mail"))
    try:
        assert S not in redact.redact(f"webhook {hook} answered 500")
        assert "mail" in redact.redact("host mail")             # not a secret, not hidden
    finally:
        redact.hide([])


def test_saving_the_settings_hides_a_new_mail_password_at_once(cfg):
    cfg.notify_smtp_password = S + "-mail"
    cfg.save()
    try:
        assert S not in redact.redact(f"SMTP AUTH failed for {S}-mail")
    finally:
        redact.hide([])


def test_the_file_and_the_activity_tab_never_see_a_secret(tmp_path):
    handler = logging.FileHandler(tmp_path / "x.log", encoding="utf-8")
    handler.addFilter(logs._redacting)
    root = logging.getLogger()
    root.addHandler(handler)
    if logs._problems not in root.handlers:
        root.addHandler(logs._problems)
        added = True
    else:
        added = False
    logs.forget()
    log = logging.getLogger("ninaivu.test.redaction")
    try:
        log.warning("off-site copy to %s failed", f"https://key:{S}@s3.example/bucket")
        try:
            raise RuntimeError(f"401 for token={S}")
        except RuntimeError:
            log.exception("upload stopped")
    finally:
        root.removeHandler(handler)
        handler.close()
        if added:
            root.removeHandler(logs._problems)
    written = (tmp_path / "x.log").read_text(encoding="utf-8")
    shown = repr(logs.recent(10))
    logs.forget()
    assert "off-site copy" in written and "upload stopped" in written and "RuntimeError" in written
    assert S not in written, "a secret reached the log file"
    assert "off-site copy" in shown and "RuntimeError" in shown
    assert S not in shown, "a secret reached the Activity tab"
