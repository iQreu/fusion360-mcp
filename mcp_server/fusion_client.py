"""Persistent socket client to the FusionMCP add-in.

Holds one long-lived TCP connection (keep-alive) to avoid per-call connection
overhead. Thread-safe: a lock serialises requests so concurrent tool calls from
the MCP runtime don't interleave on the single connection. Reconnects once
transparently if Fusion was restarted.
"""
import json
import socket
import struct
import threading

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 9123


class FusionError(RuntimeError):
    """Raised when the add-in reports an error executing an op."""


class FusionNotConnected(RuntimeError):
    """Raised when the add-in socket is unreachable."""


class FusionClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=310):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._lock = threading.Lock()
        self._id = 0

    # -- low level ---------------------------------------------------------- #
    def _connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=8)
        except OSError as exc:
            raise FusionNotConnected(
                'Cannot reach the FusionMCP add-in on {}:{}. Is Fusion 360 running '
                'with the FusionMCP add-in started? ({})'.format(self.host, self.port, exc))
        sock.settimeout(self.timeout)
        # Disable Nagle: our frames are small and strictly request/response,
        # so delayed-ACK batching only adds latency.
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock

    def _ensure(self):
        if self._sock is None:
            self._connect()

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _write(self, obj):
        data = json.dumps(obj).encode('utf-8')
        self._sock.sendall(struct.pack('>I', len(data)) + data)

    def _read(self):
        header = self._recv_exact(4)
        if header is None:
            return None
        (length,) = struct.unpack('>I', header)
        body = self._recv_exact(length)
        if body is None:
            return None
        return json.loads(body.decode('utf-8'))

    def _roundtrip(self, request):
        self._ensure()
        self._write(request)
        return self._read()

    # -- public ------------------------------------------------------------- #
    def call(self, op, params=None):
        with self._lock:
            self._id += 1
            request = {'id': 'c%d' % self._id, 'op': op, 'params': params or {}}
            try:
                resp = self._roundtrip(request)
            except (OSError, ConnectionError):
                # Connection went stale (Fusion restarted?). Reconnect once.
                self._sock = None
                resp = self._roundtrip(request)

            if resp is None:
                self._sock = None
                raise FusionNotConnected('Connection closed by the FusionMCP add-in')
            if not resp.get('ok'):
                msg = resp.get('error', 'unknown error')
                tb = resp.get('traceback')
                raise FusionError(msg + (('\n\n' + tb) if tb else ''))
            return resp.get('result')

    def close(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
