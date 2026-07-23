"""op_mesh_compare statistics — pure-Python math over a faked PolygonMesh."""
import commands
import pytest


class FakePoly:
    def __init__(self, dists_cm):
        self._d = dists_cm

    def compareWith(self, other):
        return self._d


class FakeMeshBody:
    def __init__(self, dists_cm):
        self.mesh = FakePoly(dists_cm)


def _tokens(dists_cm):
    a = commands._registry.add('msh', FakeMeshBody(dists_cm))
    b = commands._registry.add('msh', FakeMeshBody([0.0]))
    return a, b


def test_stats_and_units():
    # 0.01/-0.02/0.03 cm -> 0.1/0.2/0.3 mm absolute deviations
    a, b = _tokens([0.01, -0.02, 0.03])
    out = commands.op_mesh_compare(None, {'mesh_a': a, 'mesh_b': b,
                                          'tolerance': 0.25})
    assert out['nodes'] == 3
    assert out['max_mm'] == 0.3
    assert abs(out['mean_mm'] - 0.2) < 1e-9
    assert out['signed_min_mm'] == -0.2
    assert out['signed_max_mm'] == 0.3
    assert out['within_tolerance'] == round(2 / 3, 4)
    assert out['tolerance_mm'] == 0.25


def test_percentiles_sorted():
    a, b = _tokens([0.05, 0.01, -0.03, 0.02, 0.04])
    out = commands.op_mesh_compare(None, {'mesh_a': a, 'mesh_b': b})
    assert out['p50_mm'] <= out['p90_mm'] <= out['p99_mm'] <= out['max_mm']


def test_empty_result_raises():
    a, b = _tokens([])
    with pytest.raises(RuntimeError, match='no data'):
        commands.op_mesh_compare(None, {'mesh_a': a, 'mesh_b': b})


def test_missing_api_raises():
    class Bare:
        pass
    a = commands._registry.add('msh', Bare())
    b = commands._registry.add('msh', Bare())
    with pytest.raises(RuntimeError, match='July 2026'):
        commands.op_mesh_compare(None, {'mesh_a': a, 'mesh_b': b})
