from types import SimpleNamespace
from werkzeug.test import Client
from werkzeug.wrappers import Response
from ninaivu.server.host_router import HostRouter, admin_hostname


def test_exact_admin_hostname_routes_without_bypassing_auth():
    home=Response('family')
    admin=Response('sign in',status=401)
    client=Client(HostRouter(home,admin,'ninaivu-admin.local'),Response)
    assert client.get('/',base_url='https://ninaivu.local').text=='family'
    assert client.get('/',base_url='https://NINAIVU-ADMIN.local').status_code==401
    assert client.get('/',base_url='https://ninaivu-admin.local.evil.test').text=='family'
    assert client.get('/',headers={'X-Forwarded-Host':'ninaivu-admin.local'}).text=='family'


def test_restricted_admin_bind_and_disabled_names_are_respected():
    cfg=SimpleNamespace(host='0.0.0.0',admin_host='0.0.0.0')
    args=SimpleNamespace(no_admin=False,no_mdns=False,name='ninaivu',admin_name=None)
    assert admin_hostname(cfg,args)=='ninaivu-admin.local'
    cfg.admin_host='127.0.0.1';assert admin_hostname(cfg,args) is None
    cfg.admin_host=cfg.host;args.admin_name='ninaivu';assert admin_hostname(cfg,args) is None
    args.admin_name=None;args.no_admin=True;assert admin_hostname(cfg,args) is None
