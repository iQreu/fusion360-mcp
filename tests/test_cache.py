"""Tests for the get_state/query_entities cache invalidation in dispatch."""
import commands


def _stub_dispatch(monkeypatch):
    # Replace handlers with no-op stubs so dispatch runs without Fusion.
    stub = {'get_state': lambda app, p: {'ok': 1},   # read-only
            'extrude': lambda app, p: {'ok': 1}}      # mutating
    monkeypatch.setattr(commands, 'DISPATCH', stub)


def test_read_only_op_keeps_cache(monkeypatch):
    _stub_dispatch(monkeypatch)
    commands._state_cache[('gs', 0, None, False)] = {'cached': True}
    before = commands._mutation_gen
    commands.dispatch(None, 'get_state', {})
    assert commands._state_cache  # cache survived
    assert commands._mutation_gen == before


def test_mutating_op_clears_cache_and_bumps_generation(monkeypatch):
    _stub_dispatch(monkeypatch)
    commands._state_cache[('gs', 0, None, False)] = {'cached': True}
    before = commands._mutation_gen
    commands.dispatch(None, 'extrude', {})
    assert commands._state_cache == {}          # cache invalidated
    assert commands._mutation_gen == before + 1


def test_read_only_set_covers_inspection_ops():
    for op in ('ping', 'server_info', 'get_state', 'query_entities',
               'measure', 'bounding_box', 'interference', 'timeline'):
        assert op in commands._READ_ONLY_OPS
