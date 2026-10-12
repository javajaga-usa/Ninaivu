"""External audit 2026-10-11, PERF-01: on the smallest web-thread setting the
console's status streams must not take every thread.

At two threads the stream limit was two, so two console tabs held both of
Waitress's threads and nothing else, Stop included, was answered. Here a real
Waitress serves the real app on two threads, the console opens streams until
it is refused, and an ordinary request is still answered promptly.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

from conftest import ADMIN


def _sign_in(port: int) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps({"username": ADMIN[0], "password": ADMIN[1]})
    conn.request("POST", "/api/auth/login", body=body, headers={
        "Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
    response = conn.getresponse()
    response.read()
    assert response.status == 200, response.status
    cookies = [v.split(";", 1)[0] for k, v in response.getheaders() if k.lower() == "set-cookie"]
    conn.close()
    return "; ".join(cookies)


def _open_stream(port: int, cookie: str) -> tuple[socket.socket, int]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                 f"Cookie: {cookie}\r\nAccept: text/event-stream\r\n\r\n".encode())
    head = b""
    while b"\r\n" not in head:
        piece = sock.recv(200)
        if not piece:
            break
        head += piece
    status = int(head.split(b" ", 2)[1]) if head else 0
    return sock, status


def test_with_every_stream_allowed_open_two_threads_still_answer_the_console(app, people):
    from waitress.server import create_server

    from ninaivu.api import _streams

    cfg = app.config["MV_CONFIG"]
    cfg.server_threads = 2

    server = create_server(app, host="127.0.0.1", port=0, threads=2, channel_timeout=60)
    port = server.effective_port
    loop = threading.Thread(target=server.run, daemon=True)
    loop.start()
    streams: list[socket.socket] = []
    try:
        cookie = _sign_in(port)
        refused = False
        for _ in range(4):
            sock, status = _open_stream(port, cookie)
            if status == 429:
                sock.close()
                refused = True
                break
            assert status == 200, status
            streams.append(sock)
        assert refused, "the console was never told to poll instead"
        assert len(streams) == _streams.limit(cfg) == 1

        began = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/auth/session", headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie})
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status == 200
        assert time.monotonic() - began < 5, "an ordinary request waited on the streams"
    finally:
        for sock in streams:
            sock.close()
        # A closed stream lets its thread go at its next write; wake it now
        # rather than at the next 20-second keep-alive.
        deadline = time.monotonic() + 10
        while _streams.held() and time.monotonic() < deadline:
            app.config["MV_SCANNER"]._notify({"running": False})
            time.sleep(0.1)
        server.close()
        loop.join(5)
    assert _streams.held() == 0
