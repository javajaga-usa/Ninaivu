import sys
import pytest
from werkzeug.wrappers import Response
from ninaivu.server.http import make_threaded_server


@pytest.mark.skipif(sys.platform!='win32',reason='Windows exclusive socket regression')
def test_windows_cannot_bind_two_servers_to_the_same_port():
    server=make_threaded_server('127.0.0.1',0,Response('test'))
    try:
        with pytest.raises((OSError,SystemExit)):
            duplicate=make_threaded_server('127.0.0.1',server.server_port,Response('other'))
            duplicate.server_close()
    finally:server.server_close()
