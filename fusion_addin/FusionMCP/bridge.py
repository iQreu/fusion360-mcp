"""Socket server + main-thread dispatch bridge for FusionMCP.

A background thread accepts a persistent TCP connection from the MCP server
process and reads length-prefixed JSON requests. Because the Fusion API may
only be called on the main UI thread, each request is handed to the main thread
via a registered CustomEvent; the background thread blocks on a threading.Event
until the main-thread handler stores a response.

Wire protocol (both directions): 4-byte big-endian unsigned length + UTF-8 JSON.
    request  : {"id": str, "op": str, "params": {...}}
    response : {"id": str, "ok": true,  "result": {...}}
             | {"id": str, "ok": false, "error": str, "traceback": str}
"""
import json
import socket
import struct
import threading
import time
import traceback

import adsk.core
import commands
import logutil

HOST = '127.0.0.1'
PORT = 9123
EVENT_ID = 'FusionMCPExecEvent'
MAIN_THREAD_TIMEOUT = 300  # seconds a single op may run on the main thread

_state = {
    'app': None,
    'server_sock': None,
    'thread': None,
    'running': False,
    'custom_event': None,
    'handler': None,
    'pending': {},          # job_id -> job dict
    'lock': threading.Lock(),
    'counter': 0,
}


def _next_job_id():
    with _state['lock']:
        _state['counter'] += 1
        return 'job%d' % _state['counter']


# --------------------------------------------------------------------------- #
# Framing helpers
# --------------------------------------------------------------------------- #
def _recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(conn):
    header = _recv_exact(conn, 4)
    if header is None:
        return None
    (length,) = struct.unpack('>I', header)
    body = _recv_exact(conn, length)
    if body is None:
        return None
    return json.loads(body.decode('utf-8'))


def _write_frame(conn, obj):
    data = json.dumps(obj).encode('utf-8')
    conn.sendall(struct.pack('>I', len(data)) + data)


# --------------------------------------------------------------------------- #
# Main-thread execution via CustomEvent
# --------------------------------------------------------------------------- #
class _ExecHandler(adsk.core.CustomEventHandler):
    """Runs on Fusion's main thread when fireCustomEvent is called."""

    def notify(self, args):
        job_id = None
        try:
            job_id = args.additionalInfo
            job = _state['pending'].get(job_id)
            if not job:
                return
            op = job['op']
            start = time.time()
            ok = True
            try:
                result = commands.dispatch(_state['app'], op, job['params'])
                job['response'] = {'ok': True, 'result': result}
            except Exception as exc:  # noqa: BLE001 - report any API error back to client
                ok = False
                job['response'] = {
                    'ok': False,
                    'error': '{}: {}'.format(type(exc).__name__, exc),
                    'traceback': traceback.format_exc(),
                }
            finally:
                elapsed_ms = (time.time() - start) * 1000.0
                try:
                    logutil.record(op, elapsed_ms, ok)
                    logutil.get_logger().info(
                        '%s %s %.1fms', op, 'ok' if ok else 'error', elapsed_ms)
                except Exception:
                    pass
                job['event'].set()
        except Exception:
            # Last-ditch: never let an exception escape the handler.
            if job_id is not None:
                job = _state['pending'].get(job_id)
                if job:
                    job['response'] = {'ok': False, 'error': 'handler crash',
                                       'traceback': traceback.format_exc()}
                    job['event'].set()


def _execute_on_main(op, params):
    job_id = _next_job_id()
    event = threading.Event()
    _state['pending'][job_id] = {'op': op, 'params': params,
                                 'event': event, 'response': None}
    try:
        _state['app'].fireCustomEvent(EVENT_ID, job_id)
        finished = event.wait(timeout=MAIN_THREAD_TIMEOUT)
        if not finished:
            return {'ok': False, 'error': 'Timed out waiting for Fusion main thread'}
        return _state['pending'][job_id]['response']
    finally:
        _state['pending'].pop(job_id, None)


# --------------------------------------------------------------------------- #
# Socket server loop (background thread)
# --------------------------------------------------------------------------- #
def _serve():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
    except OSError:
        # Port busy (stale add-in?). Give up quietly; user can restart Fusion.
        return
    sock.listen(1)
    sock.settimeout(1.0)
    _state['server_sock'] = sock

    while _state['running']:
        try:
            conn, _addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        _handle_connection(conn)

    try:
        sock.close()
    except Exception:
        pass


def _handle_connection(conn):
    try:
        conn.settimeout(None)
        # Small request/response frames: disable Nagle to avoid ~40ms
        # delayed-ACK stalls and keep per-call latency minimal.
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        while _state['running']:
            req = _read_frame(conn)
            if req is None:
                break  # client disconnected
            resp = _execute_on_main(req.get('op'), req.get('params') or {})
            resp = dict(resp or {'ok': False, 'error': 'no response'})
            resp['id'] = req.get('id')
            _write_frame(conn, resp)
    except (ConnectionError, OSError):
        pass
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def start_server(app):
    if _state['running']:
        return
    _state['app'] = app

    custom_event = app.registerCustomEvent(EVENT_ID)
    handler = _ExecHandler()
    custom_event.add(handler)
    _state['custom_event'] = custom_event
    _state['handler'] = handler  # keep a strong reference or it gets GC'd

    _state['running'] = True
    thread = threading.Thread(target=_serve, name='FusionMCPServer', daemon=True)
    thread.start()
    _state['thread'] = thread
    try:
        logutil.get_logger().info('bridge started on %s:%s', HOST, PORT)
    except Exception:
        pass


def stop_server():
    _state['running'] = False

    sock = _state.get('server_sock')
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass
    _state['server_sock'] = None

    app = _state.get('app')
    if app is not None and _state.get('custom_event') is not None:
        try:
            _state['custom_event'].remove(_state['handler'])
        except Exception:
            pass
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass

    _state['custom_event'] = None
    _state['handler'] = None
    _state['thread'] = None
