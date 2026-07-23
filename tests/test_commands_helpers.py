"""Pure-Python helpers in commands.py (no live Fusion): path handling, error
classification, endpoint geometry, component resolution."""
import commands


def test_swap_ext_handles_forward_slashes_and_dotted_dirs():
    # A forward-slash path with a dot in a directory must not be truncated.
    assert commands._swap_ext('C:/jobs/rev1.2/bracket', 'stl') == 'C:/jobs/rev1.2/bracket.stl'
    assert commands._swap_ext('C:/x/part.step', 'stl') == 'C:/x/part.stl'
    assert commands._swap_ext(r'C:\x\part.step', 'stl') == r'C:\x\part.stl'


def test_classify_error_codes():
    assert commands.classify_error(KeyError('bdy9')) == ('stale_token', False)
    assert commands.classify_error(ValueError('bad')) == ('bad_params', False)
    assert commands.classify_error(TypeError('bad')) == ('bad_params', False)
    code, retriable = commands.classify_error(RuntimeError('No active Fusion design'))
    assert code == 'no_design' and retriable is True
    assert commands.classify_error(
        RuntimeError('needs Fusion July 2026+'))[0] == 'unsupported'
    assert commands.classify_error(RuntimeError('boom'))[0] == 'fusion_error'


class _Line:
    """A sketch-line-like object with start/end geometry points."""
    def __init__(self, x0, y0, x1, y1):
        self.startSketchPoint = type('P', (), {'geometry': _P(x0, y0)})()
        self.endSketchPoint = type('P', (), {'geometry': _P(x1, y1)})()


class _P:
    def __init__(self, x, y, z=0.0):
        self.x, self.y, self.z = x, y, z


def test_closest_endpoint_pair_picks_the_shared_corner():
    # Two lines both STARTING at the shared corner (0,0): the old fixed-end
    # logic would have picked line1's far end; the closest-pair logic must pick
    # the (0,0) endpoints of both.
    a = _Line(0, 0, 10, 0)
    b = _Line(0, 0, 0, 10)
    pa, pb = commands._closest_endpoint_pair(a, b)
    assert (pa.x, pa.y) == (0, 0)
    assert (pb.x, pb.y) == (0, 0)


def test_as_component_resolves_occurrence_and_component():
    comp = type('Component', (), {'objectType': 'adsk::fusion::Component'})()
    occ = type('Occurrence', (), {'component': comp})()
    assert commands._as_component(occ) is comp
    assert commands._as_component(comp) is comp
    assert commands._as_component(None) is None
    assert commands._as_component(object()) is None


def test_dim_point_maps_line_to_endpoint():
    ln = _Line(1, 2, 3, 4)
    pt = commands._dim_point(ln)
    assert pt is ln.startSketchPoint
    # A bare sketch point (no startSketchPoint) passes through unchanged.
    sp = object()
    assert commands._dim_point(sp) is sp
