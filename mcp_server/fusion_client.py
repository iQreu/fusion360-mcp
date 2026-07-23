"""Persistent socket client to the FusionMCP add-in.

Holds one long-lived TCP connection (keep-alive) to avoid per-call connection
overhead. Thread-safe: a lock serialises requests so concurrent tool calls from
the MCP runtime don't interleave on the single connection.

Retry policy is deliberately asymmetric: if the request cannot even be delivered
(stale socket / connect failure), the op never ran, so we reconnect and send it
once more. But once the request is written to a live socket, a failure while
reading the reply is NOT retried — the op may already have executed, and blindly
resending would double-apply destructive edits (a second cut, delete, or move).
Such a failure surfaces as FusionNotConnected telling the caller to re-check
state before retrying.
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

    def _drop_socket(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- public ------------------------------------------------------------- #
    def call(self, op, params=None):
        with self._lock:
            self._id += 1
            request = {'id': 'c%d' % self._id, 'op': op, 'params': params or {}}
            # Phase 1 — deliver the request. A failure here means the op did not
            # run (stale socket or connect error), so reconnecting and sending
            # once more is safe.
            try:
                self._ensure()
                self._write(request)
            except (OSError, ConnectionError):
                self._drop_socket()
                try:
                    self._ensure()      # connect failure -> FusionNotConnected
                    self._write(request)
                except (OSError, ConnectionError) as exc:
                    self._drop_socket()
                    raise FusionNotConnected(
                        'Could not reach the FusionMCP add-in to send %r (%s). '
                        'Is Fusion running with the add-in started?' % (op, exc))
            # Phase 2 — read the reply. The request is now on a live socket; if
            # the read fails the op MAY have executed, so we must not resend.
            try:
                resp = self._read()
            except (OSError, ConnectionError) as exc:
                self._drop_socket()
                raise FusionNotConnected(
                    'Lost the connection to Fusion after sending %r; the '
                    'operation may or may not have completed. Re-run get_state / '
                    'query_entities to check the design before retrying. (%s)'
                    % (op, exc))
            if resp is None:
                self._drop_socket()
                raise FusionNotConnected(
                    'The FusionMCP add-in closed the connection while handling '
                    '%r; the operation may not have completed. Re-run get_state '
                    'to check before retrying.' % op)
            if not resp.get('ok'):
                msg = resp.get('error', 'unknown error')
                tb = resp.get('traceback')
                exc = FusionError(msg + (('\n\n' + tb) if tb else ''))
                exc.code = resp.get('code')          # structured error code
                exc.retriable = resp.get('retriable')
                raise exc
            return resp.get('result')

    def close(self):
        with self._lock:
            self._drop_socket()
