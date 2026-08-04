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
import contextlib
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
    'conn': None,           # the active client connection (for clean shutdown)
    'thread': None,
    'running': False,
    'dispatching': False,   # a dispatch is executing on the main thread
    'custom_event': None,
    'handler': None,
    'pending': {},          # job_id -> job dict
    'late_completions': 0,  # ops that finished after the client timed out
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
            # Re-entrancy guard: a long op that spins adsk.doEvents() (e.g.
            # cam_generate) can pump THIS queued custom event on the same main
            # thread. Refuse to dispatch inside another dispatch — it would
            # corrupt shared state and duplicate geometry.
            if _state.get('dispatching'):
                with _state['lock']:
                    job['response'] = {
                        'ok': False,
                        'error': 'Fusion main thread is busy with a long-running '
                                 'operation; retry when it finishes.'}
                    if job.get('abandoned'):
                        # The waiter already timed out — nobody will collect
                        # this rejection, so drop the job instead of leaking it.
                        _state['pending'].pop(job_id, None)
                    else:
                        job['event'].set()
                return
            _state['dispatching'] = True
            op = job['op']
            start = time.time()
            ok = True
            try:
                result = commands.dispatch(_state['app'], op, job['params'])
                job['response'] = {'ok': True, 'result': result}
            except Exception as exc:  # noqa: BLE001 - report any API error back to client
                ok = False
                code, retriable = commands.classify_error(exc)
                job['response'] = {
                    'ok': False,
                    'error': '{}: {}'.format(type(exc).__name__, exc),
                    'code': code,
                    'retriable': retriable,
                    'traceback': traceback.format_exc(),
                }
            finally:
                _state['dispatching'] = False
                elapsed_ms = (time.time() - start) * 1000.0
                try:
                    logutil.record(op, elapsed_ms, ok)
                    logutil.get_logger().info(
                        '%s %s %.1fms', op, 'ok' if ok else 'error', elapsed_ms)
                except Exception:
                    pass
                # The abandoned check must be atomic with the waiter's
                # timed-out-vs-completed decision, or a completion landing in
                # that window leaks the job (handler skips the pop, waiter
                # skips the collect).
                late = False
                with _state['lock']:
                    if job.get('abandoned'):
                        # The client already gave up (timeout). Don't touch a
                        # dead event; record the late completion so it isn't
                        # silent.
                        _state['late_completions'] = _state.get('late_completions', 0) + 1
                        _state['pending'].pop(job_id, None)
                        late = True
                    else:
                        job['event'].set()
                if late:
                    with contextlib.suppress(Exception):
                        logutil.get_logger().warning(
                            '%s finished %.1fms AFTER the client timed out', op, elapsed_ms)
        except Exception:
            # Last-ditch: never let an exception escape the handler.
            _state['dispatching'] = False
            if job_id is not None:
                job = _state['pending'].get(job_id)
                if job:
                    job['response'] = {'ok': False, 'error': 'handler crash',
                                       'traceback': traceback.format_exc()}
                    job['event'].set()


def _execute_on_main(op, params):
    job_id = _next_job_id()
    event = threading.Event()
    job = {'op': op, 'params': params, 'event': event, 'response': None}
    _state['pending'][job_id] = job
    try:
        fired = _state['app'].fireCustomEvent(EVENT_ID, job_id)
    except Exception as exc:
        _state['pending'].pop(job_id, None)
        return {'ok': False,
                'error': 'Could not reach the Fusion main thread (%s).' % exc}
    if fired is False:
        # Event id not registered (add-in stopping/stopped) — no handler will
        # ever run this job, so waiting the full timeout would just hang.
        _state['pending'].pop(job_id, None)
        return {'ok': False,
                'error': 'Fusion custom event is not registered — is the '
                         'add-in stopping or restarting?'}
    finished = event.wait(timeout=MAIN_THREAD_TIMEOUT)
    if finished:
        _state['pending'].pop(job_id, None)
        return job['response']
    # Timed out. The op may still be running on the main thread, so DON'T pop
    # the job blindly. Under the lock, either collect a response that landed
    # just as we timed out, or mark the job abandoned so the handler (whenever
    # it finishes) pops it — atomically, so neither side can skip both steps.
    with _state['lock']:
        if job.get('response') is not None:
            _state['pending'].pop(job_id, None)
            return job['response']
        job['abandoned'] = True
    return {'ok': False,
            'error': 'Timed out after %ds waiting for the Fusion main thread. '
                     'The operation may still be running — re-run get_state to '
                     'check the design before retrying.' % MAIN_THREAD_TIMEOUT}


# --------------------------------------------------------------------------- #
# Socket server loop (background thread)
# --------------------------------------------------------------------------- #
def _serve():
    # The socket is already bound (synchronously, in start_server) so bind
    # failures surface to the user instead of dying silently in this thread.
    sock = _state['server_sock']
    while _state['running']:
        try:
            conn, _addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        _handle_connection(conn)

    with contextlib.suppress(Exception):
        sock.close()


def _handle_connection(conn):
    _state['conn'] = conn
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
        if _state.get('conn') is conn:
            _state['conn'] = None
        with contextlib.suppress(Exception):
            conn.close()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def start_server(app):
    if _state['running']:
        return
    _state['app'] = app

    # Bind SYNCHRONOUSLY, before spawning the accept thread, so a port conflict
    # raises here and run() can surface it — instead of the thread failing
    # silently after the caller has already logged "listening".
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        # Windows: SO_REUSEADDR lets a SECOND listener bind a port another
        # SO_REUSEADDR socket is actively listening on, so the conflict check
        # below would never fire and connections would land on an arbitrary
        # Fusion instance. Exclusive mode makes the second bind fail loudly.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # POSIX: SO_REUSEADDR only skips the TIME_WAIT wait; it does NOT allow
        # binding over a live listener, so the conflict check still works.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
    except OSError as exc:
        with contextlib.suppress(Exception):
            sock.close()
        raise RuntimeError(
            'FusionMCP could not bind %s:%d — is another Fusion instance or a '
            'stale add-in already using it? (%s)' % (HOST, PORT, exc))
    sock.listen(1)
    sock.settimeout(1.0)
    _state['server_sock'] = sock

    try:
        custom_event = app.registerCustomEvent(EVENT_ID)
        handler = _ExecHandler()
        custom_event.add(handler)
    except Exception:
        # Don't leave the port bound with no working dispatch path — a client
        # could connect to a dead listener and hang until timeout. Also drop
        # the event registration: registerCustomEvent returns None when the id
        # is already registered (a failed earlier unregister), and leaving it
        # would make every retry fail the same way until Fusion restarts.
        with contextlib.suppress(Exception):
            sock.close()
        _state['server_sock'] = None
        with contextlib.suppress(Exception):
            app.unregisterCustomEvent(EVENT_ID)
        raise
    _state['custom_event'] = custom_event
    _state['handler'] = handler  # keep a strong reference or it gets GC'd

    with _state['lock']:
        _state['pending'].clear()
    # Deliberately do NOT reset _state['dispatching'] here: if the add-in is
    # restarted from Scripts & Add-Ins while a long op is still pumping
    # adsk.doEvents() (stop+start run nested inside that op's frame), the flag
    # is owned by the live dispatch — clearing it would re-open the
    # re-entrancy hole the guard exists to close. The dispatch's finally
    # block clears it on every path.
    _state['running'] = True
    thread = threading.Thread(target=_serve, name='FusionMCPServer', daemon=True)
    thread.start()
    _state['thread'] = thread
    with contextlib.suppress(Exception):
        logutil.get_logger().info('bridge started on %s:%s', HOST, PORT)


def stop_server():
    _state['running'] = False

    # Close the live client connection so a blocked handler thread unblocks and
    # the MCP client learns the add-in is gone (instead of hanging until the
    # main-thread timeout on its next call).
    conn = _state.get('conn')
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            conn.close()
    _state['conn'] = None

    sock = _state.get('server_sock')
    if sock is not None:
        with contextlib.suppress(Exception):
            sock.close()
    _state['server_sock'] = None

    # Wake the connection thread BEFORE joining it: closing conn does not wake
    # an Event, so a thread blocked in _execute_on_main's event.wait would
    # otherwise burn the full join timeout on Fusion's main thread and outlive
    # stop_server. Draining also drops the job dicts (op + full params) so
    # they don't leak past the server's lifetime.
    with _state['lock']:
        leftovers = list(_state['pending'].values())
        _state['pending'].clear()
    for job in leftovers:
        with contextlib.suppress(Exception):
            if job.get('response') is None:
                job['response'] = {'ok': False, 'error': 'Add-in stopping.'}
            job['event'].set()

    thread = _state.get('thread')
    if thread is not None:
        with contextlib.suppress(Exception):
            thread.join(timeout=3.0)

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
