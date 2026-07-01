"""Logging and per-operation telemetry for the FusionMCP add-in.

Fusion's built-in ``app.log`` only writes a single line to the text palette, so
this module adds a rotating file log plus lightweight timing stats that
``op_server_info`` reports back to the client. Everything degrades gracefully:
if the log file cannot be opened (locked temp dir, permissions) we fall back to
in-memory stats only and never raise into a handler.
"""
import logging
import os
import time
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.environ.get('TEMP', os.getcwd()), 'FusionMCP')
LOG_FILE = os.path.join(LOG_DIR, 'fusionmcp.log')

_logger = None
_started_at = time.time()

# op name -> {"calls", "errors", "total_ms", "max_ms"}
_stats = {}


def get_logger():
    """Return the shared logger, configuring the rotating file handler once."""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger('fusionmcp')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000,
                                      backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
    except Exception:
        # No file handler available — records still flow to any parent handlers
        # and telemetry keeps working in memory.
        pass
    _logger = logger
    return logger


def record(op, elapsed_ms, ok):
    """Accumulate timing/error stats for one dispatched operation."""
    entry = _stats.get(op)
    if entry is None:
        entry = {'calls': 0, 'errors': 0, 'total_ms': 0.0, 'max_ms': 0.0}
        _stats[op] = entry
    entry['calls'] += 1
    if not ok:
        entry['errors'] += 1
    entry['total_ms'] += elapsed_ms
    if elapsed_ms > entry['max_ms']:
        entry['max_ms'] = elapsed_ms


def stats_snapshot():
    """Return a JSON-serialisable summary of telemetry so far."""
    ops = {}
    total_calls = 0
    total_errors = 0
    for op, e in _stats.items():
        total_calls += e['calls']
        total_errors += e['errors']
        ops[op] = {
            'calls': e['calls'],
            'errors': e['errors'],
            'avg_ms': round(e['total_ms'] / e['calls'], 2) if e['calls'] else 0.0,
            'max_ms': round(e['max_ms'], 2),
        }
    return {
        'uptime_s': round(time.time() - _started_at, 1),
        'total_calls': total_calls,
        'total_errors': total_errors,
        'log_file': LOG_FILE,
        'ops': ops,
    }
