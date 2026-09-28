"""Just enough of a WebSocket client to hear ComfyUI's progress messages.

The standard library has no WebSocket client and Ninaivu does not take a
dependency for one feature, so this implements the part of RFC 6455 a
progress listener needs: the opening handshake, reading text frames (binary
preview frames are read and discarded), answering pings and noticing a close.
It never sends data of its own.

Plain ``ws://`` only. An ``https`` ComfyUI address is rare on a home network,
and progress is a nicety: the caller falls back to polling when this is not
available.
"""
from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import struct

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: Largest single message kept. ComfyUI's preview frames can be large; they are
#: skipped rather than refused, but a text message this size is not progress.
MAX_MESSAGE = 1024 * 1024
MAX_FRAME = 64 * 1024 * 1024


class WebSocketError(OSError):
    """The connection could not be made or was not a WebSocket."""


class Listener:
    def __init__(self, host: str, port: int | None, path: str, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port or 80), timeout=timeout)
        try:
            self._handshake(host, port, path)
        except Exception:
            self.sock.close()
            raise
        self._buffer = b""

    def _handshake(self, host: str, port: int | None, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        netloc = f"{host}:{port}" if port else host
        request = (f"GET {path} HTTP/1.1\r\nHost: {netloc}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("The server closed the connection during the handshake.")
            head += chunk
            if len(head) > 16384:
                raise WebSocketError("The handshake response was too long.")
        header, _, rest = head.partition(b"\r\n\r\n")
        lines = header.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in f"{lines[0]} ":
            raise WebSocketError(f"The server did not switch to WebSocket: {lines[0] if lines else ''}")
        fields = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            fields[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()
        if fields.get("sec-websocket-accept") != expected:
            raise WebSocketError("The server's WebSocket handshake did not match.")
        self._buffer = rest

    # -- reading -----------------------------------------------------------
    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self._buffer)))
            if not chunk:
                raise WebSocketError("The WebSocket connection closed.")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def receive(self, wait: float) -> str | None:
        """The next text message, or None if none arrives within *wait* seconds."""
        message = b""
        while True:
            if not self._buffer:
                ready, _, _ = select.select([self.sock], [], [], wait)
                if not ready:
                    return None
            first, second = self._read(2)
            fin, opcode = first & 0x80, first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            if length > MAX_FRAME:
                raise WebSocketError("A WebSocket frame was larger than Ninaivu accepts.")
            mask = self._read(4) if second & 0x80 else b""
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise WebSocketError("The server closed the WebSocket.")
            if opcode == 0x9:
                self._send(0xA, payload[:125])
                continue
            if opcode in (0x2, 0xA) or (opcode == 0x0 and not message):
                continue                        # previews, pongs, continued binary
            if opcode in (0x1, 0x0):
                if len(message) + len(payload) <= MAX_MESSAGE:
                    message += payload
                if fin:
                    return message.decode("utf-8", errors="replace")
            wait = 5.0                          # the rest of a fragmented message

    def _send(self, opcode: int, payload: bytes) -> None:
        """Client frames must be masked (RFC 6455 §5.3)."""
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
