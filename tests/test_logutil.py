"""Tests for telemetry accumulation in logutil."""
import logutil


def _reset():
    logutil._stats.clear()


def test_record_accumulates_calls_and_timing():
    _reset()
    logutil.record('extrude', 10.0, ok=True)
    logutil.record('extrude', 30.0, ok=True)
    snap = logutil.stats_snapshot()
    op = snap['ops']['extrude']
    assert op['calls'] == 2
    assert op['avg_ms'] == 20.0
    assert op['max_ms'] == 30.0
    assert op['errors'] == 0
    assert snap['total_calls'] == 2


def test_record_counts_errors():
    _reset()
    logutil.record('fillet', 5.0, ok=False)
    snap = logutil.stats_snapshot()
    assert snap['ops']['fillet']['errors'] == 1
    assert snap['total_errors'] == 1


def test_snapshot_reports_uptime_and_log_file():
    _reset()
    snap = logutil.stats_snapshot()
    assert 'uptime_s' in snap
    assert snap['log_file'].endswith('fusionmcp.log')
