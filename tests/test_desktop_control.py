import json
from types import SimpleNamespace

import pytest
from ninaivu.utils.resources import budget, environment
from ninaivu.desktop.control import Controller, discharge_watts
from ninaivu.utils import power


def test_resource_modes_are_bounded_and_ordered():
    standard=budget('standard',16);fast=budget('performance',16);saving=budget('power-saving',16)
    assert saving['compute_threads'] < standard['compute_threads'] < fast['compute_threads']
    assert saving['workers'] < standard['workers'] < fast['workers']
    assert budget('nonsense',1)['mode']=='standard'
    assert all(budget(mode,1)['workers']==1 for mode in ('standard','performance','power-saving'))
    assert environment('power-saving')['OMP_NUM_THREADS']=='2'


def test_only_valid_battery_discharge_is_reported_in_watts():
    assert discharge_watts({'DischargeRate':12500,'Discharging':True,'PowerOnline':False,'Relative':False})==12.5
    for row in [{'DischargeRate':12500,'Discharging':True,'Relative':True},
                {'DischargeRate':12500,'Discharging':True,'PowerOnline':True},
                {'DischargeRate':4294967295,'Discharging':True},
                {'DischargeRate':0,'Discharging':True},{}]:
        assert discharge_watts(row) is None


def test_custom_certificate_setup_does_not_open_old_local_ca(monkeypatch):
    from ninaivu.desktop.app import Dashboard
    from ninaivu.utils import tls
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.root = None
    dashboard.controller = SimpleNamespace(settings={'arguments': ['--cert=custom.pem']})
    notices = []
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.showinfo',
                        lambda title, message, **kwargs: notices.append(message))
    monkeypatch.setattr(tls, 'ca_certificate_path',
                        lambda *_: pytest.fail('Must not open an unrelated CA'))
    dashboard.open_certificate()
    assert 'custom certificate' in notices[0]


@pytest.mark.parametrize('mode,throttled',[('standard',False),('performance',False),('power-saving',True)])
def test_modes_use_real_windows_power_hints(monkeypatch,mode,throttled):
    calls=[];monkeypatch.setattr(power.sys,'platform','win32')
    monkeypatch.setattr(power,'_set_windows_execution_speed_throttled',lambda value:calls.append(value) or True)
    policy=power.PowerPolicy(profile=mode);policy.start();assert calls[-1] is throttled
    policy.archive_started();assert calls[-1] is (mode=='power-saving')
    policy.archive_finished();assert calls[-1] is throttled


def control(tmp_path,monkeypatch):
    monkeypatch.setattr(Controller,'record',lambda _:None)
    # Records here name made-up processes; pid 42 may be a real one.
    from ninaivu.desktop import control as module
    monkeypatch.setattr(module,'_server_process',lambda record:None)
    return Controller(root=tmp_path,cfg=SimpleNamespace(state_dir=tmp_path/'state',host='127.0.0.1',port=5000,admin_port=3000,ai_engine='off'))


def test_mode_persistence_and_stopped_transition(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch)
    assert c.mode=='standard';c.apply_mode('power-saving')
    assert json.loads(c.settings_path.read_text())['mode']=='power-saving'
    assert Controller(root=tmp_path,cfg=c.cfg).mode=='power-saving'
    with pytest.raises(ValueError):c.save_mode('turbo')


def test_desktop_https_urls_match_defaults_and_explicit_ports(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch);c.cfg.port=80
    assert c.server_url()=='https://127.0.0.1'
    c.settings.update(arguments=['--cert=custom.crt','--key=custom.key'],port=8443)
    assert c.server_url()=='https://127.0.0.1:8443'
    c.settings.update(arguments=['--port','80'],port=80)
    assert c.server_url()=='http://127.0.0.1'


def test_desktop_named_urls_share_https_port(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch);c.cfg.host='0.0.0.0';c.cfg.port=443
    c.settings.update(arguments=['--https'],port=443,admin_port=3000)
    assert c.server_url()=='https://ninaivu.local'
    assert c.server_url(True)=='https://ninaivu-admin.local'
    c.settings['arguments']+=['--admin-host','127.0.0.1']
    assert c.server_url(True)=='https://127.0.0.1:3000'


def test_running_mode_change_stops_before_start(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch);order=[]
    monkeypatch.setattr(c,'record',lambda:{'pid':1})
    monkeypatch.setattr(c,'stop',lambda:order.append('stop'))
    monkeypatch.setattr(c,'start',lambda:order.append('start'))
    c.apply_mode('performance');assert order==['stop','start']


def test_refused_shutdown_never_force_kills(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch)
    monkeypatch.setattr(c,'record',lambda:{'pid':42,'admin_port':3000,'token':'synthetic'})
    monkeypatch.setattr(c,'capture_running_settings',lambda:None)
    from tools import stop
    monkeypatch.setattr(stop,'ask_to_stop',lambda *_,**__:False)
    monkeypatch.setattr(stop,'end_it',lambda *_:pytest.fail('Must not force-kill from desktop UI'))
    with pytest.raises(RuntimeError,match='No process'):
        c.stop()


def test_https_stop_uses_configured_transport(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch);c.settings['arguments']=['--https']
    calls=[]
    monkeypatch.setattr(c,'record',lambda:None if calls else {'pid':42,'admin_port':3000,'token':'synthetic'})
    monkeypatch.setattr(c,'capture_running_settings',lambda:None)
    from tools import stop
    monkeypatch.setattr(stop,'ask_to_stop',lambda *args:calls.append(args) or True)
    assert c.stop()=='Ninaivu stopped cleanly.'
    assert calls==[(3000,'synthetic',10.0,'https')]


def test_stop_waits_for_owned_launcher_before_restart(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch);calls=[]
    monkeypatch.setattr(c,'record',lambda:None if calls else {'pid':42,'admin_port':3000,'token':'synthetic'})
    monkeypatch.setattr(c,'capture_running_settings',lambda:None)
    states=iter([None,None,0]);c.started=SimpleNamespace(poll=lambda:next(states))
    from tools import stop
    from ninaivu.desktop import control as module
    monkeypatch.setattr(stop,'ask_to_stop',lambda *args,**kwargs:calls.append(args) or True)
    sleeps=[];monkeypatch.setattr(module.time,'sleep',lambda n:sleeps.append(n))
    assert c.stop()=='Ninaivu stopped cleanly.'
    assert c.started is None and len(sleeps)==2


def test_stop_waits_for_the_server_to_let_go_of_the_state_folder(tmp_path,monkeypatch):
    """The run file goes before the process ends and releases its lock. A panel
    that did not start the server has no handle on it, and a restart started
    the next one in between, which was refused, leaving no family app."""
    c=control(tmp_path,monkeypatch);calls=[]
    monkeypatch.setattr(c,'record',lambda:None if calls else {'pid':4242,'admin_port':3000,'token':'synthetic'})
    monkeypatch.setattr(c,'capture_running_settings',lambda:None)
    from tools import stop
    from ninaivu.desktop import control as module
    server=SimpleNamespace(name='the server')
    monkeypatch.setattr(module,'_server_process',lambda record:server if record['pid']==4242 else None)
    lets_go=iter([False,False,True]);monkeypatch.setattr(module,'_has_ended',lambda p:p is server and next(lets_go))
    monkeypatch.setattr(stop,'ask_to_stop',lambda *args,**kwargs:calls.append(args) or True)
    sleeps=[];monkeypatch.setattr(module.time,'sleep',lambda n:sleeps.append(n))
    assert c.stop()=='Ninaivu stopped cleanly.'
    assert len(sleeps)==2


def test_a_process_exited_but_not_collected_has_ended():
    import psutil
    from ninaivu.desktop import control as module
    class Zombie:
        def is_running(self): return True
        def status(self): return psutil.STATUS_ZOMBIE
    class Busy(Zombie):
        def status(self): return psutil.STATUS_RUNNING
    class Gone(Zombie):
        def is_running(self): raise psutil.NoSuchProcess(4242)
    assert module._has_ended(Zombie()) and module._has_ended(Gone()) and module._has_ended(None)
    assert not module._has_ended(Busy())


def test_start_preserves_server_args_and_sets_budgets(tmp_path,monkeypatch):
    c=control(tmp_path,monkeypatch);c.settings['arguments']=['--host','0.0.0.0','--port','80','--workers','8']
    from ninaivu.desktop import control as module
    path=tmp_path/'.venv'/('Scripts/python.exe' if module.sys.platform=='win32' else 'bin/python')
    path.parent.mkdir(parents=True);path.touch()
    calls=[]
    def popen(command,**kwargs):
        calls.append((command,kwargs));return SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(module.subprocess,'Popen',popen)
    monkeypatch.setattr(c,'_start_ollama',lambda _:None)
    monkeypatch.setattr(c,'capture_running_settings',lambda:None)
    monkeypatch.setattr(c,'record',lambda:{'pid':42} if calls else None)
    c.mode='power-saving';c.start()
    assert calls[0][0][-2:]==['--workers','1']
    assert calls[0][1]['env']['NINAIVU_RESOURCE_MODE']=='power-saving'
    assert '--host' in calls[0][0] and '0.0.0.0' in calls[0][0]
    # Outlives the panel on a Mac, as it does on Windows.
    assert calls[0][1]['start_new_session'] is True


def test_monitor_sample_handles_missing_psutil(tmp_path, monkeypatch):
    from ninaivu.desktop import control as module
    c = control(tmp_path, monkeypatch)
    monitor = module.Monitor(c)
    monkeypatch.setattr(module, 'psutil', None)
    data = monitor.sample()
    assert data['running'] is False
    assert data['cpu'] == 0.0
    assert data['threads'] == 0


def test_dashboard_initialization():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tkinter display not available")
    try:
        root.withdraw()
        from ninaivu.desktop.app import Dashboard
        dashboard = Dashboard(root)
        assert dashboard is not None
        assert dashboard.mode_description.get() != ""
        # Bottom logs should not be visible by default
        assert dashboard.logs_visible is False
        assert len(dashboard.split.panes()) == 1
        assert dashboard.log_button_text.get() == "View logs"

        # Clicking View logs makes them visible
        dashboard.open_logs()
        assert dashboard.logs_visible is True
        assert len(dashboard.split.panes()) == 2
        assert dashboard.log_button_text.get() == "Hide logs"

        # Clicking Hide logs hides them again
        dashboard.hide_logs()
        assert dashboard.logs_visible is False
        assert len(dashboard.split.panes()) == 1
        assert dashboard.log_button_text.get() == "View logs"

        # Toggling alternates
        dashboard.toggle_logs()
        assert dashboard.logs_visible is True
        assert len(dashboard.split.panes()) == 2
        assert dashboard.log_button_text.get() == "Hide logs"
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Installing Ninaivu's certificate where browsers trust it
# ---------------------------------------------------------------------------

class FakeCertutil:
    """Plays certutil over an in-memory set of stores; never touches Windows."""

    def __init__(self, root=False, ca=False, add_ok=True):
        self.stores = {"Root": root, "CA": ca}
        self.add_ok = add_ok
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if "-addstore" in args:
            if self.add_ok:
                self.stores["Root"] = True
            return SimpleNamespace(returncode=0 if self.add_ok else 1)
        store = args[args.index("-store") + 1]
        return SimpleNamespace(returncode=0 if self.stores[store] else 1)


@pytest.fixture()
def ninaivu_ca(tmp_path):
    from ninaivu.utils import tls
    tls.ensure_certificate(tmp_path)
    return tls.ca_certificate_path(tmp_path)


def test_the_certificate_goes_straight_into_trusted_root_and_is_checked(ninaivu_ca):
    from ninaivu.utils import tls
    certutil = FakeCertutil()
    trusted, message = tls.trust_ca_on_windows(ninaivu_ca, runner=certutil)
    assert trusted and "Trusted Root" in message
    assert ["certutil.exe", "-user", "-addstore", "Root", str(ninaivu_ca)] in certutil.calls
    assert any("-store" in c and "Root" in c for c in certutil.calls), "it checks, not assumes"


def test_a_copy_stranded_in_intermediate_is_pointed_out(ninaivu_ca):
    """Exactly the state that left a real browser showing Ninaivu as offline."""
    from ninaivu.utils import tls
    trusted, message = tls.trust_ca_on_windows(ninaivu_ca, runner=FakeCertutil(ca=True))
    assert trusted and "Intermediate" in message


def test_a_declined_install_is_reported_not_claimed(ninaivu_ca):
    from ninaivu.utils import tls
    trusted, message = tls.trust_ca_on_windows(ninaivu_ca, runner=FakeCertutil(add_ok=False))
    assert not trusted and "did not add" in message


def test_a_certificate_that_cannot_be_read_is_not_installed(tmp_path):
    from ninaivu.utils import tls
    junk = tmp_path / "ninaivu-ca.crt"
    junk.write_text("not a certificate")
    certutil = FakeCertutil()
    trusted, _ = tls.trust_ca_on_windows(junk, runner=certutil)
    assert not trusted and certutil.calls == []


def _dashboard_for(state_dir, monkeypatch, platform='win32'):
    from ninaivu.desktop.app import Dashboard
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.root = None
    dashboard.controller = SimpleNamespace(settings={'arguments': []},
                                           cfg=SimpleNamespace(state_dir=state_dir))
    notices = []
    dashboard.notice = SimpleNamespace(set=notices.append)
    # sys.platform rather than os.name: pathlib reads os.name, and set to "nt"
    # on a Mac or Linux it builds Windows paths that cannot exist there.
    monkeypatch.setattr('ninaivu.desktop.app.sys.platform', platform)
    return dashboard, notices


def test_the_control_panel_installs_after_asking_and_says_so(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch)
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.askyesno', lambda *a, **k: True)
    monkeypatch.setattr(tls, 'trust_ca_on_windows', lambda path: (True, 'Installed in Trusted Root.'))
    opened = []
    monkeypatch.setattr('ninaivu.desktop.app.os.startfile', opened.append, raising=False)
    dashboard.open_certificate()
    assert opened == [], "no certificate window when the install worked"
    assert notices and notices[0].startswith('Installed in Trusted Root.')


def test_saying_no_changes_nothing(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch)
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.askyesno', lambda *a, **k: False)
    monkeypatch.setattr(tls, 'trust_ca_on_windows', lambda path: pytest.fail('must not install'))
    dashboard.open_certificate()
    assert notices == []


def test_if_it_fails_the_window_opens_with_the_store_spelled_out(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch)
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.askyesno', lambda *a, **k: True)
    monkeypatch.setattr(tls, 'trust_ca_on_windows', lambda path: (False, 'Windows did not add it.'))
    opened = []
    monkeypatch.setattr('ninaivu.desktop.app.os.startfile', opened.append, raising=False)
    dashboard.open_certificate()
    assert opened == [ninaivu_ca]
    assert 'Place all certificates in the following store' in notices[0]
    assert 'Automatically select' in notices[0]


def test_on_a_mac_the_certificate_is_trusted_after_asking(tmp_path, ninaivu_ca, monkeypatch):
    """Not imported through Keychain Access, which offers keychains that take no
    certificates and refuses one already there — "unable to import", untrusted."""
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, platform='darwin')
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.askyesno', lambda *a, **k: True)
    monkeypatch.setattr(tls, 'trust_ca_on_mac', lambda path: (True, 'Trusted for websites.'))
    ran = []
    monkeypatch.setattr('ninaivu.desktop.app.subprocess.run', lambda command, **kw: ran.append(command))
    monkeypatch.setattr('ninaivu.desktop.app.webbrowser.open',
                        lambda *a, **k: pytest.fail('opened in the browser'))
    dashboard.open_certificate()
    assert ran == [], "nothing to open when trusting worked"
    assert notices[0].startswith('Trusted for websites.')


def test_on_a_mac_a_failed_trust_shows_the_file_and_the_last_step(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, platform='darwin')
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.askyesno', lambda *a, **k: True)
    monkeypatch.setattr(tls, 'trust_ca_on_mac', lambda path: (False, 'macOS did not trust it.'))
    ran = []
    monkeypatch.setattr('ninaivu.desktop.app.subprocess.run', lambda command, **kw: ran.append(command))
    dashboard.open_certificate()
    assert ran == [['open', '-R', str(ninaivu_ca)]]
    assert 'Always Trust' in notices[0] and 'already' in notices[0]


def test_on_a_mac_saying_no_changes_nothing(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, platform='darwin')
    monkeypatch.setattr('ninaivu.desktop.app.messagebox.askyesno', lambda *a, **k: False)
    monkeypatch.setattr(tls, 'trust_ca_on_mac', lambda path: pytest.fail('must not trust'))
    dashboard.open_certificate()
    assert notices == []


class FakeSecurity:
    """The `security` command, answering as a Mac would."""

    def __init__(self, *, trusted=False, accepts=True, namesakes=()):
        self.trusted, self.accepts, self.namesakes = trusted, accepts, namesakes
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        verb = command[1]
        if verb == 'add-trusted-cert':
            self.trusted = self.accepts
            return SimpleNamespace(returncode=0 if self.accepts else 1, stdout='', stderr='')
        if verb == 'verify-cert':
            return SimpleNamespace(returncode=0 if self.trusted else 1, stdout='', stderr='')
        if verb == 'find-certificate':
            lines = ''.join(f'SHA-1 hash: {h}\n' for h in self.namesakes)
            return SimpleNamespace(returncode=0, stdout=lines, stderr='')
        raise AssertionError(command)


def test_the_mac_helper_trusts_for_websites_only_and_checks(ninaivu_ca):
    from ninaivu.utils import tls
    security = FakeSecurity()
    trusted, message = tls.trust_ca_on_mac(ninaivu_ca, runner=security)
    assert trusted and 'Trusted' in message
    added = next(c for c in security.calls if c[1] == 'add-trusted-cert')
    assert added[added.index('-r') + 1] == 'trustRoot'
    assert added[added.index('-p') + 1] == 'ssl', "for websites, not code signing"
    assert added[-1] == str(ninaivu_ca)


def test_the_mac_helper_leaves_an_already_trusted_certificate_alone(ninaivu_ca):
    from ninaivu.utils import tls
    security = FakeSecurity(trusted=True)
    trusted, message = tls.trust_ca_on_mac(ninaivu_ca, runner=security)
    assert trusted and 'already' in message
    assert not any(c[1] == 'add-trusted-cert' for c in security.calls)


def test_the_mac_helper_does_not_claim_a_declined_password(ninaivu_ca):
    from ninaivu.utils import tls
    trusted, message = tls.trust_ca_on_mac(ninaivu_ca, runner=FakeSecurity(accepts=False))
    assert not trusted and 'did not trust' in message


def test_the_mac_helper_mentions_another_certificate_with_the_same_name(ninaivu_ca):
    """Found on the Mac this was written from: two "Ninaivu local CA"s in the
    System keychain, one of them not this Ninaivu's."""
    from ninaivu.utils import tls
    ours = tls.ca_thumbprint(ninaivu_ca)
    security = FakeSecurity(namesakes=(ours, '0FFF12040C512F26AB3C24A77EE15B554B432592'))
    trusted, message = tls.trust_ca_on_mac(ninaivu_ca, runner=security)
    assert trusted and '1 other certificate called "Ninaivu local CA"' in message
