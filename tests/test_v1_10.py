"""v1.10.0 capabilities that are testable off-Fusion: validate_only dry runs,
sketch_status open-endpoint detection, the native-fastener fallback and the
server-side electronics BOM aggregation."""
import commands
import pytest


# --------------------------------------------------------------------------- #
# _validate_or_add (sweep/loft/shell validate_only)
# --------------------------------------------------------------------------- #
class _Counted:
    def __init__(self, count):
        self.count = count


class _Body:
    def __init__(self, name='B', faces=6, edges=12):
        self.name = name
        self.faces = _Counted(faces)
        self.edges = _Counted(edges)


class _Bodies:
    def __init__(self, bodies):
        self._b = bodies
        self.count = len(bodies)

    def item(self, i):
        return self._b[i]

    def __iter__(self):
        return iter(self._b)


class _Feat:
    def __init__(self, deletable=True):
        self.bodies = _Bodies([_Body()])
        self._deletable = deletable
        self.deleted = False

    def deleteMe(self):
        if self._deletable:
            self.deleted = True
            return True
        return False


class _Feats:
    def __init__(self, feat=None, raise_=None):
        self._feat, self._raise = feat, raise_

    def add(self, fin):
        if self._raise is not None:
            raise self._raise
        return self._feat


def test_validate_only_reports_invalid_input_as_answer_not_error():
    out = commands._validate_or_add(
        _Feats(raise_=RuntimeError('profile is open')), object(), 'sweep')
    assert out == {'valid': False, 'kind': 'sweep',
                   'error': 'profile is open'}


def test_validate_only_removes_the_test_feature_and_keeps_no_tokens():
    feat = _Feat(deletable=True)
    out = commands._validate_or_add(_Feats(feat=feat), object(), 'loft')
    assert feat.deleted
    assert out['valid'] is True and out['committed'] is False
    assert out['bodies'] == 1 and out['faces'] == 6
    assert 'feature' not in out  # no token for geometry that no longer exists


def test_validate_only_undeletable_feature_is_reported_as_real():
    # If deleteMe() refuses, the feature stays in the timeline — the result
    # must carry real tokens, not pretend the design is untouched.
    feat = _Feat(deletable=False)
    out = commands._validate_or_add(_Feats(feat=feat), object(), 'shell')
    assert out['valid'] is True and out['committed'] is True
    assert out['feature'].startswith('ftr')
    assert len(out['bodies']) == 1
    assert 'note' in out


# --------------------------------------------------------------------------- #
# op_sketch_status: open-endpoint census
# --------------------------------------------------------------------------- #
class _Pt:
    def __init__(self, x, y):
        self.x, self.y, self.z = x, y, 0.0


class _Curve:
    def __init__(self, x0, y0, x1, y1, construction=False):
        self.isConstruction = construction
        self.startSketchPoint = type('P', (), {'geometry': _Pt(x0, y0)})()
        self.endSketchPoint = type('P', (), {'geometry': _Pt(x1, y1)})()


class _Circle:
    # Closed curve: no start/end sketch points at all.
    isConstruction = False


class _Sketch:
    def __init__(self, curves, name='Sketch1', constrained=False, profiles=0):
        self.sketchCurves = curves
        self.name = name
        self.isFullyConstrained = constrained
        self.profiles = _Counted(profiles)


def _status_of(sketch):
    tok = commands._registry.add('skt', sketch)
    return commands.op_sketch_status(None, {'sketch': tok})['sketches'][0]


def test_sketch_status_finds_open_chain_endpoints_in_mm():
    # L-chain (cm in sketch space): (0,0)->(1,0)->(1,1). The shared corner is
    # touched twice; the two free ends are the open endpoints, reported in mm.
    entry = _status_of(_Sketch([_Curve(0, 0, 1, 0), _Curve(1, 0, 1, 1)]))
    assert entry['curves'] == 2
    assert entry['open_endpoint_count'] == 2
    assert entry['open_endpoints_mm'] == [[0.0, 0.0], [10.0, 10.0]]


def test_sketch_status_closed_loop_and_circle_have_no_open_ends():
    square = [_Curve(0, 0, 1, 0), _Curve(1, 0, 1, 1),
              _Curve(1, 1, 0, 1), _Curve(0, 1, 0, 0)]
    entry = _status_of(_Sketch(square + [_Circle()], profiles=2))
    assert entry['open_endpoint_count'] == 0
    assert entry['curves'] == 5  # the circle still counts as a curve
    assert entry['profiles'] == 2


def test_sketch_status_ignores_construction_geometry():
    entry = _status_of(_Sketch([_Curve(0, 0, 1, 0, construction=True)]))
    assert entry['curves'] == 0
    assert entry['construction_curves'] == 1
    assert entry['open_endpoint_count'] == 0


# --------------------------------------------------------------------------- #
# Wiring: new ops are dispatchable, diagnostics stay read-only
# --------------------------------------------------------------------------- #
def test_diagnostics_ops_are_read_only():
    # Read-only ops must not bump the mutation generation (get_state cache).
    assert {'design_diagnostics', 'sketch_status'} <= commands._READ_ONLY_OPS
    for op in ('timeline_builder', 'corner_closure', 'cam_setup',
               'cam_suppress', 'create_appearance'):
        assert op not in commands._READ_ONLY_OPS  # these mutate


def test_native_fastener_falls_back_when_api_is_absent():
    # The fake adsk has no FastenerOccurrenceDefinition: the probe must bail
    # out with None (parametric fallback) before touching the app at all.
    assert commands._native_fastener(None, 'M6', 20.0) is None


# --------------------------------------------------------------------------- #
# Server-side electronics BOM aggregation (pure Python, needs the mcp SDK
# only because server.py imports it at module level)
# --------------------------------------------------------------------------- #
def test_ecad_bom_rows_groups_and_backfills_attributes():
    pytest.importorskip('mcp', reason='MCP SDK not installed')
    import server

    parts = [
        {'name': 'R2', 'value': '10k', 'package': '0603'},
        {'name': 'R1', 'value': '10k', 'package': '0603',
         'attributes': {'Mpn': 'RC0603FR-0710KL', 'MFR': 'Yageo'}},
        {'name': 'C1', 'value': '100n', 'package': '0603',
         'attributes': {'Manufacturer': 'TDK'}},
    ]
    rows = server._ecad_bom_rows(parts, group=True)
    assert len(rows) == 2
    top = rows[0]  # sorted by quantity desc
    assert top['quantity'] == 2
    assert top['designators'] == ['R1', 'R2']
    # Attributes come from whichever part in the group carries them, even when
    # the first-seen part has none.
    assert top['mpn'] == 'RC0603FR-0710KL'
    assert top['manufacturer'] == 'Yageo'
    assert rows[1]['manufacturer'] == 'TDK'
    assert len(server._ecad_bom_rows(parts, group=False)) == 3
