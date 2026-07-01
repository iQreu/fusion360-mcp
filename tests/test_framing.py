"""Tests for the length-prefixed JSON framing in fusion_client."""
import struct

from fusion_client import FusionClient


class _FakeSock:
    """In-memory socket: sendall appends; recv drains from the front."""
    def __init__(self):
        self.buf = bytearray()

    def sendall(self, data):
        self.buf.extend(data)

    def recv(self, n):
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk


def test_write_prefixes_big_endian_length():
    c = FusionClient()
    c._sock = _FakeSock()
    c._write({'op': 'ping'})
    header = c._sock.buf[:4]
    (length,) = struct.unpack('>I', header)
    assert length == len(c._sock.buf) - 4


def test_write_then_read_round_trips():
    c = FusionClient()
    c._sock = _FakeSock()
    payload = {'id': 'c1', 'op': 'extrude', 'params': {'distance': 10}}
    c._write(payload)
    assert c._read() == payload


def test_read_returns_none_on_closed_connection():
    c = FusionClient()
    c._sock = _FakeSock()  # empty -> recv yields b''
    assert c._read() is None


def test_recv_exact_reassembles_partial_chunks():
    c = FusionClient()
    c._sock = _FakeSock()
    c._sock.buf.extend(b'abcdef')
    assert c._recv_exact(6) == b'abcdef'
