"""The names Ninaivu's certificate is made out to."""
from ninaivu.utils import tls


def names_for(monkeypatch, host):
    monkeypatch.setattr(tls.socket, "gethostname", lambda: host)
    return tls.local_names()


def test_a_macs_name_is_not_given_a_second_local(monkeypatch):
    names = names_for(monkeypatch, "Family-Mac-Mini.local")
    assert "Family-Mac-Mini.local" in names
    assert not any(n.endswith(".local.local") for n in names)


def test_a_windows_name_gains_its_local_one(monkeypatch):
    names = names_for(monkeypatch, "DESKTOP-4F2")
    assert {"DESKTOP-4F2", "DESKTOP-4F2.local"} <= set(names)


def test_the_bare_computer_name_left_out_is_not_a_problem(tmp_path, monkeypatch, caplog):
    """A CA made on Windows, moved to a Mac with the library, does not cover the
    Mac's bare name. That was dropped at every start, and sat on the console's
    list of problems with nothing anybody could do. An unexpected name still warns."""
    import logging
    monkeypatch.setattr(tls.socket, "gethostname", lambda: "DESKTOP-4F2")
    tls.ensure_certificate(tmp_path)
    monkeypatch.setattr(tls.socket, "gethostname", lambda: "Family-Mac-Mini.local")
    with caplog.at_level(logging.INFO, logger="ninaivu.utils.tls"):
        tls.ensure_certificate(tmp_path, extra_hosts=("photos.example.com",))
    said = {(r.levelname, r.getMessage()) for r in caplog.records if r.name == "ninaivu.utils.tls"}
    assert ("INFO", "Ninaivu's certificate covers Family-Mac-Mini.local, not Family-Mac-Mini: "
            "its CA was made before this computer had that name") in said
    warned = [m for level, m in said if level == "WARNING"]
    # Any warning may mention the unexpected name; none may mention the Mac's
    # bare name. (A machine whose own address is outside the CA's ranges adds
    # a warning of its own, so this is about what was said, not in which order.)
    assert any("photos.example.com" in m for m in warned), warned
    assert not any("Family-Mac-Mini" in m for m in warned), warned
