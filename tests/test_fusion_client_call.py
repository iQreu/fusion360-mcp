"""Tests for FusionClient.call() — the two-phase no-resend retry policy.

Phase 1 (delivery): a write failure means the op never ran, so ONE
reconnect+resend is safe. Phase 2 (reply): after the request reached a live
socket, a read failure must NOT resend — the op may already have executed and
a blind retry would double-apply destructive edits.
"""
import json
import struct

import pytest
from fusion_client import FusionClient, FusionError, FusionNotConnected


class _ScriptedSock:
    """Socket double: optionally fails the next sendall/recv, records writes."""

    def __init__(self, send_exc=None, recv_exc=None):
        self.send_exc = send_exc
        self.recv_exc = recv_exc
        self.sent = bytearray()
        self.send_count = 0  # completed request frames (sendall is 1 per frame)
        self.rx = bytearray()

    def queue_response(self, obj):
        data = json.dumps(obj).encode('utf-8')
        self.rx.extend(struct.pack('>I', len(data)) + data)

    def sendall(self, data):
        if self.send_exc is not None:
            exc, self.send_exc = self.send_exc, None
            raise exc
        self.send_count += 1
        self.sent.extend(data)

    def recv(self, n):
        if self.recv_exc is not None:
            exc, self.recv_exc = self.recv_exc, None
            raise exc
        chunk = bytes(self.rx[:n])
        del self.rx[:n]
        return chunk

    def close(self):
        pass


def _client_with(*socks):
    """A FusionClient whose _connect hands out the given sockets in order."""
    c = FusionClient()
    seq = list(socks)

    def connect():
        if not seq:
            raise OSError('scripted: no more sockets')
        c._sock = seq.pop(0)

    c._connect = connect
    return c


def test_success_returns_result():
    s = _ScriptedSock()
    s.queue_response({'id': 'c1', 'ok': True, 'result': {'pong': True}})
    c = _client_with(s)
    assert c.call('ping') == {'pong': True}
    assert s.send_count == 1


def test_write_failure_reconnects_and_resends_exactly_once():
    dead = _ScriptedSock(send_exc=ConnectionResetError('stale keep-alive'))
    live = _ScriptedSock()
    live.queue_response({'id': 'c1', 'ok': True, 'result': {'ok': 1}})
    c = _client_with(dead, live)
    assert c.call('ping') == {'ok': 1}
    assert dead.send_count == 0   # first write died before a frame completed
    assert live.send_count == 1   # exactly one resend on the fresh socket


def test_two_write_failures_raise_not_connected():
    dead1 = _ScriptedSock(send_exc=ConnectionResetError())
    dead2 = _ScriptedSock(send_exc=ConnectionResetError())
    c = _client_with(dead1, dead2)
    with pytest.raises(FusionNotConnected) as ei:
        c.call('extrude', {'distance': 5})
    assert 'Could not reach' in str(ei.value)
    assert c._sock is None


def test_read_failure_does_not_resend():
    s = _ScriptedSock(recv_exc=ConnectionResetError('dropped mid-op'))
    # Honeypot: a perfectly good second socket with a queued success reply. A
    # mutant that reconnects+resends after the read failure would consume it
    # and RETURN success instead of raising — merely asserting the error
    # message on a one-socket script would not catch that mutant (its
    # reconnect fails and it falls back to the same message).
    live = _ScriptedSock()
    live.queue_response({'id': 'c1', 'ok': True, 'result': {'resent': True}})
    c = _client_with(s, live)
    with pytest.raises(FusionNotConnected) as ei:
        c.call('delete', {'token': 'bdy1'})
    # The signature guidance for the destructive-retry hazard:
    assert 'may or may not have completed' in str(ei.value)
    assert s.send_count == 1      # written once...
    assert live.send_count == 0   # ...and NEVER resent, even with a live socket
    assert c._sock is None        # poisoned socket dropped


def test_peer_close_before_reply_does_not_resend():
    s = _ScriptedSock()           # empty rx -> recv b'' -> _read() None
    live = _ScriptedSock()        # honeypot, as above
    live.queue_response({'id': 'c1', 'ok': True, 'result': {'resent': True}})
    c = _client_with(s, live)
    with pytest.raises(FusionNotConnected):
        c.call('ping')
    assert s.send_count == 1
    assert live.send_count == 0
    assert c._sock is None


def test_error_response_carries_structured_code_and_retriable():
    s = _ScriptedSock()
    s.queue_response({'id': 'c1', 'ok': False, 'error': "KeyError: 'bdy9'",
                      'code': 'stale_token', 'retriable': False})
    c = _client_with(s)
    with pytest.raises(FusionError) as ei:
        c.call('delete', {'token': 'bdy9'})
    assert ei.value.code == 'stale_token'
    assert ei.value.retriable is False
