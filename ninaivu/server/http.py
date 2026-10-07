"""Threaded HTTP/TLS listener with exclusive Windows port ownership."""
import socket
import sys
import threading

from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

#: Connections served at once when nothing else is said: Werkzeug starts a
#: thread for each, and with nothing bounding them a handful of video players
#: and phones was enough to make the machine start threads until it could not.
DEFAULT_CONNECTIONS = 32
#: Seconds a connection may sit without sending or taking a byte. A browser
#: keeps idle connections open for minutes, and each holds one of the
#: connections above; without this a few phones left on the gallery were
#: enough to keep the next visitor waiting.
IDLE_SECONDS = 30


class IdleClosingHandler(WSGIRequestHandler):
    timeout = IDLE_SECONDS


class ExclusiveThreadedServer(ThreadedWSGIServer):
    allow_reuse_address = sys.platform != 'win32'
    allow_reuse_port = False
    #: Set per server by make_threaded_server.
    max_connections = DEFAULT_CONNECTIONS

    def server_bind(self):
        if sys.platform == 'win32':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        # At most max_connections threads. A connection beyond them waits in
        # the listening queue until one ends, rather than costing a thread
        # (and a database connection) of its own.
        slots = self.__dict__.setdefault(
            '_slots', threading.BoundedSemaphore(self.max_connections))
        # In slices, because this is the serving loop's own thread: waiting
        # outright would leave a stop waiting for a slot as well.
        while not slots.acquire(timeout=0.5):
            if getattr(self, '_BaseServer__shutdown_request', False):
                self.shutdown_request(request)
                return
        try:
            super().process_request(request, client_address)
        except BaseException:
            slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_threaded_server(host, port, app, ssl_files=None, threads=None):
    """The listener HTTPS uses. *threads* is the Tuning page's web threads:
    with keep-alive, a browser holds several connections open while idle, so
    the bound is a few connections for each thread, never fewer than 32."""
    server = ExclusiveThreadedServer(host, port, app, handler=IdleClosingHandler,
                                     ssl_context=ssl_files)
    if threads:
        server.max_connections = max(DEFAULT_CONNECTIONS, 4 * int(threads))
    return server
