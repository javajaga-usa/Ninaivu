"""Threaded HTTP/TLS listener with exclusive Windows port ownership."""
import socket
import sys
from werkzeug.serving import ThreadedWSGIServer


class ExclusiveThreadedServer(ThreadedWSGIServer):
    allow_reuse_address = sys.platform != 'win32'
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == 'win32':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_threaded_server(host, port, app, ssl_files=None):
    return ExclusiveThreadedServer(host, port, app, ssl_context=ssl_files)
