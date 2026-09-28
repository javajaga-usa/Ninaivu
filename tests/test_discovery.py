"""Answering to a name instead of an address.

`http://192.168.1.24:5000` is fine for a computer and hopeless for a family.
These cover the part that can be tested without a second machine on the wire:
the name is validated before the server starts, the announcement withdraws
itself cleanly, and the name reaches the places that have to know about it —
the certificate above all, since using a name the certificate does not carry
produces a warning that the bare address would not.
"""

import pytest

from ninaivu.utils import discovery, tls


# ---------------------------------------------------------------------------
# Names people actually type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed,expected", [
    ("ninaivu", "ninaivu"),
    ("Ninaivu", "ninaivu"),
    ("  ninaivu  ", "ninaivu"),
    ("ninaivu.local", "ninaivu"),
    ("ninaivu.local.", "ninaivu"),
    ("NINAIVU.LOCAL", "ninaivu"),
    ("family-photos", "family-photos"),
    ("upstairs-pc", "upstairs-pc"),
    ("photos2", "photos2"),
])
def test_names_are_normalised(typed, expected):
    assert discovery.normalise_name(typed) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", ".local", "-ninaivu", "ninaivu-", "ninaivu photos",
    "ninaivu_photos", "héarth", "a" * 64, "ninaivu/photos", "ninaivu.home.arpa",
])
def test_impossible_names_are_refused_up_front(bad):
    """Better to fail at startup than to silently never resolve."""
    with pytest.raises(ValueError):
        discovery.normalise_name(bad)


def test_the_refusal_explains_itself():
    with pytest.raises(ValueError) as raised:
        discovery.normalise_name("my photos!")
    message = str(raised.value)
    assert "letters, digits and hyphens" in message
    assert "ninaivu" in message, "an example of a name that works"


# ---------------------------------------------------------------------------
# The announcement
# ---------------------------------------------------------------------------

zeroconf = pytest.importorskip("zeroconf")


def test_advertising_publishes_both_apps_under_one_hostname():
    """`ninaivu.local:5000` and `ninaivu.local:3000` have to be the same host."""
    announcement = discovery.advertise(
        "ninaivu-test", {"family": 5000, "admin": 3000}, ["192.168.1.24"])
    if announcement is None:
        pytest.skip("no multicast on this machine")

    try:
        assert announcement.hostname == "ninaivu-test.local"
        assert len(announcement._services) == 2
        servers = {service.server for service in announcement._services}
        assert servers == {"ninaivu-test.local."}, servers
        ports = {service.port for service in announcement._services}
        assert ports == {5000, 3000}
    finally:
        announcement.close()


def test_closing_withdraws_the_name():
    announcement = discovery.advertise("ninaivu-test2", {"family": 5000},
                                       ["192.168.1.24"])
    if announcement is None:
        pytest.skip("no multicast on this machine")
    announcement.close()
    assert announcement._services == []
    announcement.close()                         # idempotent


def test_it_is_a_context_manager():
    announcement = discovery.advertise("ninaivu-test3", {"family": 5000},
                                       ["192.168.1.24"])
    if announcement is None:
        pytest.skip("no multicast on this machine")
    with announcement as live:
        assert live.hostname == "ninaivu-test3.local"
    assert announcement._services == []


def test_no_addresses_means_no_announcement():
    """Announcing a name that points nowhere is worse than announcing none."""
    assert discovery.advertise("ninaivu", {"family": 5000}, []) is None


def test_a_bad_name_raises_rather_than_advertising_nonsense():
    with pytest.raises(ValueError):
        discovery.advertise("not a name", {"family": 5000}, ["192.168.1.24"])


def test_admin_name_gives_the_console_its_own_hostname():
    """ninaivu.local for the family app, ninaivu-test4-admin.local for the console."""
    announcement = discovery.advertise(
        "ninaivu-test4", {"family": 5000, "admin": 3000}, ["192.168.1.24"],
        admin_name="ninaivu-test4-admin")
    if announcement is None:
        pytest.skip("no multicast on this machine")

    try:
        assert announcement.hostname == "ninaivu-test4.local"
        assert announcement.hostnames == {
            "family": "ninaivu-test4.local",
            "admin": "ninaivu-test4-admin.local",
        }
        servers = {service.server for service in announcement._services}
        assert servers == {"ninaivu-test4.local.", "ninaivu-test4-admin.local."}, servers
    finally:
        announcement.close()


def test_without_admin_name_both_roles_share_one_hostname():
    announcement = discovery.advertise(
        "ninaivu-test5", {"family": 5000, "admin": 3000}, ["192.168.1.24"])
    if announcement is None:
        pytest.skip("no multicast on this machine")

    try:
        assert announcement.hostnames == {
            "family": "ninaivu-test5.local",
            "admin": "ninaivu-test5.local",
        }
    finally:
        announcement.close()


# ---------------------------------------------------------------------------
# The name has to reach the certificate
# ---------------------------------------------------------------------------

def test_the_name_can_be_written_into_the_certificate(tmp_path):
    """Using a name the certificate does not carry warns; the address does not."""
    from cryptography import x509

    cert, _ = tls.ensure_certificate(tmp_path, extra_hosts=["ninaivu.local"])
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    names = loaded.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "ninaivu.local" in names


def test_the_cli_offers_a_name_and_a_way_to_switch_it_off():
    from ninaivu.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args([]).name == "ninaivu"
    assert parser.parse_args(["--name", "family-photos"]).name == "family-photos"
    assert parser.parse_args(["--no-mdns"]).no_mdns is True
    assert parser.parse_args([]).no_mdns is False


def test_the_cli_offers_a_separate_name_for_the_admin_console():
    """Unset by default — main() derives NAME-admin unless this is given."""
    from ninaivu.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args([]).admin_name is None
    assert parser.parse_args(["--admin-name", "upstairs-admin"]).admin_name == \
        "upstairs-admin"


def test_filter_reachable_addresses_prioritizes_gateway(monkeypatch):
    from ninaivu.utils import netinfo

    monkeypatch.setattr(netinfo, "gateway_addresses", lambda: ["192.168.0.145"])
    filtered = discovery.filter_reachable_addresses(["192.168.0.145", "172.31.144.1"])
    assert filtered == ["192.168.0.145"]

    # When no gateway matches, fall back safely
    monkeypatch.setattr(netinfo, "gateway_addresses", lambda: [])
    assert discovery.filter_reachable_addresses(["192.168.0.145", "172.31.144.1"]) == [
        "192.168.0.145", "172.31.144.1"
    ]


def test_announcement_dynamic_ip_update(monkeypatch):
    from ninaivu.utils import netinfo
    import socket

    monkeypatch.setattr(netinfo, "gateway_addresses", lambda: [])
    announcement = discovery.advertise(
        "ninaivu-dynamic", {"family": 443}, ["192.168.1.50"], scheme="https"
    )
    if announcement is None:
        pytest.skip("no multicast on this machine")

    try:
        assert announcement.addresses == ["192.168.1.50"]
        assert announcement._services[0].type == "_https._tcp.local."
        assert announcement._services[0].addresses == [socket.inet_aton("192.168.1.50")]

        # Simulate dynamic IP change (e.g. DHCP lease renewal)
        updated = announcement.update_addresses(["192.168.1.80"])
        assert updated is True
        assert announcement.addresses == ["192.168.1.80"]
        assert announcement._services[0].addresses == [socket.inet_aton("192.168.1.80")]

        # Same addresses -> no-op
        assert announcement.update_addresses(["192.168.1.80"]) is False
    finally:
        announcement.close()


# ---------------------------------------------------------------------------
# Two Ninaivus, one name: the newest run keeps it
# ---------------------------------------------------------------------------

def test_the_later_start_outranks_the_earlier_and_a_tie_has_one_winner():
    assert discovery.claim_rank("200.5", "a") > discovery.claim_rank("100.0", "z")
    # Started in the same instant: the id decides, so exactly one side yields.
    first, second = discovery.claim_rank("100.0", "a"), discovery.claim_rank("100.0", "b")
    assert first != second and (first < second) != (second < first)


def test_a_ninaivu_that_does_not_say_when_it_started_is_never_yielded_to():
    """An older Ninaivu cannot be made to yield, but it must not win either."""
    assert discovery.claim_rank(None, "x") is None
    assert discovery.claim_rank("yesterday", "x") is None


def test_the_newer_of_two_runs_keeps_the_name_and_the_older_steps_back():
    """The case that took a real library offline: a second machine on the same
    network answering for the same hostname. Two live announcements in one
    process see each other over multicast exactly as two machines would."""
    import time as _time
    older = discovery.advertise("ninaivu-claim-test", {"family": 5000}, ["192.168.1.24"])
    if older is None:
        pytest.skip("no multicast on this machine")
    newer = None
    try:
        _time.sleep(0.05)                          # strictly later
        newer = discovery.advertise("ninaivu-claim-test", {"family": 5000}, ["192.168.1.25"])
        assert newer is not None
        assert newer.started > older.started

        deadline = _time.time() + 20
        while _time.time() < deadline and "ninaivu-claim-test.local" not in older.yielded:
            _time.sleep(0.2)

        assert older.yielded.get("ninaivu-claim-test.local") == ["192.168.1.25"], older.yielded
        assert older._services == [], "the older run withdrew its records for the name"
        # The newer run saw the older one too, and kept the name.
        _time.sleep(1.0)
        assert newer.yielded == {}
        assert [s.server for s in newer._services] == ["ninaivu-claim-test.local."]
    finally:
        older.close()
        if newer is not None:
            newer.close()


def test_a_run_ignores_its_own_records_and_differently_named_ninaivus():
    import time as _time
    solo = discovery.advertise("ninaivu-claim-solo", {"family": 5000}, ["192.168.1.24"])
    if solo is None:
        pytest.skip("no multicast on this machine")
    other = None
    try:
        _time.sleep(0.05)
        other = discovery.advertise("ninaivu-claim-elsewhere", {"family": 5000}, ["192.168.1.26"])
        _time.sleep(4.0)
        assert solo.yielded == {} and len(solo._services) == 1
    finally:
        solo.close()
        if other is not None:
            other.close()


# ---------------------------------------------------------------------------
# A name given up comes back when the newer run has gone
# ---------------------------------------------------------------------------

def test_the_older_run_takes_the_name_back_when_the_newer_one_stops(monkeypatch):
    """What left ninaivu.local answering nothing: another computer ran Ninaivu for
    an evening, took the name, and was switched off. The Mac serving the
    library had stepped back for good, and phones lost the family app until it
    was restarted."""
    import time as _time
    # 192.168.1.25 is not a real machine here: it "answers" for as long as the
    # newer run is open, and stops when that run is closed — what a computer
    # switched off does. Both runs publish under the same service name, so the
    # newer run's goodbye is not always delivered as a removal to the older
    # run's browser; the periodic check is the backstop, shortened here.
    alive = {"newer": True}
    monkeypatch.setattr(discovery.Announcement, "_still_answering",
                        staticmethod(lambda claimant: alive["newer"]))
    monkeypatch.setattr(discovery.Announcement, "RECHECK_SECONDS", 0.5)
    older = discovery.advertise("ninaivu-reclaim-test", {"family": 5000}, ["192.168.1.24"])
    if older is None:
        pytest.skip("no multicast on this machine")
    newer = None
    try:
        _time.sleep(0.05)
        newer = discovery.advertise("ninaivu-reclaim-test", {"family": 5000}, ["192.168.1.25"])
        assert newer is not None
        deadline = _time.time() + 20
        while _time.time() < deadline and "ninaivu-reclaim-test.local" not in older.yielded:
            _time.sleep(0.2)
        assert older._services == [], "stepped back first"
        _time.sleep(2.0)                       # several checks: it stays back
        assert older._services == [], "took the name back while the newer run answered"

        alive["newer"] = False
        newer.close()
        newer = None
        deadline = _time.time() + 20
        while _time.time() < deadline and not older._services:
            _time.sleep(0.2)
        assert [s.server for s in older._services] == ["ninaivu-reclaim-test.local."]
        assert older.yielded == {}
    finally:
        older.close()
        if newer is not None:
            newer.close()


def test_one_missed_connection_at_the_goodbye_does_not_hand_the_name_back(monkeypatch):
    """Stepping back withdraws this run's own record, and that goodbye reaches
    its own browser as a removal of the shared service name. One connection to
    the newer run that happened to fail at that moment used to hand the name
    straight back, and the two runs traded it — while the newer run's record
    was still being published for anyone to see."""
    import time as _time
    monkeypatch.setattr(discovery.Announcement, "_still_answering",
                        staticmethod(lambda claimant: False))
    older = discovery.advertise("ninaivu-nottrade-test", {"family": 5000}, ["192.168.1.24"])
    if older is None:
        pytest.skip("no multicast on this machine")
    newer = None
    try:
        _time.sleep(0.05)
        newer = discovery.advertise("ninaivu-nottrade-test", {"family": 5000}, ["192.168.1.25"])
        assert newer is not None
        deadline = _time.time() + 20
        while _time.time() < deadline and "ninaivu-nottrade-test.local" not in older.yielded:
            _time.sleep(0.2)
        assert "ninaivu-nottrade-test.local" in older.yielded, "never stepped back"
        _time.sleep(3.0)                       # long enough for the goodbye to land
        assert older._services == [], (
            "the name was taken back on one missed connection while the newer "
            "run still published its record")
    finally:
        older.close()
        if newer is not None:
            newer.close()


class _FakeZeroconf:
    def __init__(self):
        self.registered = []

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    def unregister_service(self, info):
        pass

    def get_service_info(self, *_args, **_kwargs):
        return None


def _given_up(monkeypatch, answers):
    """An announcement that gave ninaivu.local up to a run at 192.168.0.57,
    which answers each check with the next of *answers*."""
    from zeroconf import ServiceInfo
    import socket as _socket
    run = discovery.Announcement("ninaivu", ["192.168.0.229"])
    run._zc = _FakeZeroconf()
    mine = ServiceInfo("_https._tcp.local.", "Ninaivu family._https._tcp.local.",
                       addresses=[_socket.inet_aton("192.168.0.100")], port=443,
                       properties={"role": "family"}, server="ninaivu.local.")
    run._given_up["ninaivu.local"] = [mine]
    run._claimants["ninaivu.local"] = {
        "key": ("_https._tcp.local.", "Ninaivu family (2)._https._tcp.local."),
        "rank": (run.started + 60, "theirs"), "addresses": ["192.168.0.57"],
        "port": 443, "misses": 0}
    run.yielded["ninaivu.local"] = ["192.168.0.57"]
    replies = iter(answers)
    monkeypatch.setattr(discovery.Announcement, "_still_answering",
                        staticmethod(lambda claimant: next(replies)))
    return run


def test_the_name_comes_back_after_the_newer_run_misses_several_checks(monkeypatch):
    """A computer switched off sends no goodbye; its records linger in caches
    for over an hour. Not answering at its address is what shows it has gone."""
    run = _given_up(monkeypatch, [False, True, False, False, False])
    for _ in range(4):
        run._check_claimants()
    assert run._zc.registered == [], "one answer in between starts the count again"
    run._check_claimants()
    assert [s.server for s in run._zc.registered] == ["ninaivu.local."]
    assert run._zc.registered[0].parsed_addresses() == ["192.168.0.229"], "at today's address"
    assert run.yielded == {} and run._claimants == {} and run._given_up == {}


def test_a_withdrawn_record_gives_the_name_back_only_if_the_run_is_gone(monkeypatch):
    run = _given_up(monkeypatch, [True, False])
    key = run._claimants["ninaivu.local"]["key"]
    run._claimant_left(*key)
    assert run._zc.registered == [], "still answering at its address: keep out of its way"
    run._claimant_left(*key)
    assert [s.server for s in run._zc.registered] == ["ninaivu.local."]


def test_a_run_that_cannot_be_asked_is_never_taken_from_by_checking(monkeypatch):
    """A second run on the same computer gives no address of its own; only its
    withdrawing its records gives the name back."""
    run = _given_up(monkeypatch, [None] * 10)
    for _ in range(10):
        run._check_claimants()
    assert run._zc.registered == []


def test_a_run_newer_still_is_the_one_watched():
    import socket as _socket
    from zeroconf import ServiceInfo
    run = discovery.Announcement("ninaivu", ["192.168.0.229"])
    run._zc = _FakeZeroconf()
    run._claimants["ninaivu.local"] = {"key": ("t", "a"), "rank": (run.started + 1, "a"),
                                      "addresses": ["192.168.0.57"], "port": 443, "misses": 2}
    newest = ServiceInfo("_https._tcp.local.", "Ninaivu family (3)._https._tcp.local.",
                         addresses=[_socket.inet_aton("192.168.0.60")], port=443,
                         properties={"started": f"{run.started + 5:.6f}", "instance": "c"},
                         server="ninaivu.local.")
    run._zc.get_service_info = lambda *_a, **_k: newest
    run._consider_claim("_https._tcp.local.", newest.name)
    watched = run._claimants["ninaivu.local"]
    assert watched["addresses"] == ["192.168.0.60"] and watched["misses"] == 0
    assert run.yielded["ninaivu.local"] == ["192.168.0.60"]
