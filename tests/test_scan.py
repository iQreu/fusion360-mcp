"""Tests for the server-side scan module. Loop assembly and the missing-deps
error are pure logic; the mesh-based tests run only when the optional 're'
dependencies are installed (they skip cleanly in a bare CI)."""
import math

import pytest
import scan


def test_missing_deps_error_is_actionable(monkeypatch):
    monkeypatch.setattr(scan, 'trimesh', None)
    monkeypatch.setattr(scan, '_IMPORT_ERROR', 'No module named numpy')
    with pytest.raises(RuntimeError) as err:
        scan._require()
    assert 'mcp_server[re]' in str(err.value)
    assert 'numpy' in str(err.value)


def test_assemble_loops_closes_a_shuffled_square():
    square = [
        ((0, 0, 0), (1, 0, 0)),
        ((1, 1, 0), (0, 1, 0)),
        ((0, 1, 0), (0, 0, 0)),
        ((1, 0, 0), (1, 1, 0)),
    ]
    loops = scan._assemble_loops(square)
    assert len(loops) == 1
    points, closed = loops[0]
    assert closed is True
    assert len(points) == 4


def test_assemble_loops_keeps_open_chains_open():
    chain = [((0, 0, 0), (1, 0, 0)), ((1, 0, 0), (2, 0, 0))]
    loops = scan._assemble_loops(chain)
    assert len(loops) == 1
    points, closed = loops[0]
    assert closed is False
    assert len(points) == 3


def test_assemble_loops_no_duplicate_vertex_at_a_junction():
    # A closed square A-B-C-D-A plus a spur D-E, with the spur listed BEFORE the
    # closing segment. The backward walk must not re-enter the chain at D and
    # duplicate it.
    A, B, C, D, E = (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 2, 0)
    segs = [(A, B), (B, C), (C, D), (D, E), (D, A)]
    loops = scan._assemble_loops(segs)
    for points, _closed in loops:
        keys = [(round(p[0], 3), round(p[1], 3), round(p[2], 3)) for p in points]
        assert len(keys) == len(set(keys)), 'a vertex was duplicated: %s' % keys


def test_assemble_loops_walks_backward_from_a_mid_chain_seed():
    # An open chain 0-1-2-3-4 whose FIRST-listed segment is in the middle (2-3):
    # the walk must extend both directions and return one contiguous 5-point
    # chain, not fragment it.
    chain = [
        ((2, 0, 0), (3, 0, 0)),   # seed sits mid-chain (listed first)
        ((0, 0, 0), (1, 0, 0)),
        ((1, 0, 0), (2, 0, 0)),
        ((3, 0, 0), (4, 0, 0)),
    ]
    loops = scan._assemble_loops(chain)
    assert len(loops) == 1
    points, closed = loops[0]
    assert closed is False
    assert len(points) == 5
    xs = [round(p[0]) for p in points]
    assert sorted(xs) == [0, 1, 2, 3, 4]


def test_assemble_loops_separates_disjoint_loops_and_drops_degenerate():
    two = [
        ((0, 0, 0), (1, 0, 0)), ((1, 0, 0), (0.5, 1, 0)), ((0.5, 1, 0), (0, 0, 0)),
        ((5, 5, 5), (6, 5, 5)), ((6, 5, 5), (5.5, 6, 5)), ((5.5, 6, 5), (5, 5, 5)),
        ((9, 9, 9), (9, 9, 9)),  # zero-length: ignored
    ]
    loops = scan._assemble_loops(two)
    assert len(loops) == 2
    assert all(closed for _, closed in loops)


def test_assemble_loops_backward_walk_closure_is_kept():
    # A closed pentagon H-Y-Z-T-X plus one chord T-Y (a degree-3 junction, as
    # emitted when the section plane grazes a shared mesh vertex). The forward
    # walk stops at the junction; the BACKWARD walk consumes the closing edge
    # and returns True — a result that used to be discarded, reporting the
    # closed contour as open and dropping its closing edge from the output.
    H, Y, Z, T, X = (0, 0, 0), (1, 0, 0), (2, 0.5, 0), (1.5, 1.5, 0), (0, 1, 0)
    segs = [(H, Y), (Y, Z), (Z, T), (T, Y), (T, X), (X, H)]
    loops = scan._assemble_loops(segs)
    closed_loops = [pts for pts, closed in loops if closed]
    assert len(closed_loops) == 1, 'the pentagon must be reported closed'
    assert len(closed_loops[0]) == 5


def test_print_check_catches_elevated_flat_overhang_on_tall_parts(tmp_path):
    tm = pytest.importorskip('trimesh')
    t = tm.transformations.translation_matrix
    # A 100x100x2 plate whose flat underside hangs 3 mm above the bed on one
    # 5x5x3 foot, with a 195 mm pillar on top (part 200 mm tall). A base band
    # scaled by part height (2% -> 4 mm) used to classify the whole underside
    # as "on plate" and report no issues — the first layers would print in air.
    plate = tm.creation.box(extents=(100.0, 100.0, 2.0), transform=t([0, 0, 4.0]))
    foot = tm.creation.box(extents=(5.0, 5.0, 3.0), transform=t([0, 0, 1.5]))
    pillar = tm.creation.box(extents=(5.0, 5.0, 195.0),
                             transform=t([0, 0, 5.0 + 97.5]))
    path = str(tmp_path / 'bridge.stl')
    tm.util.concatenate([plate, foot, pillar]).export(path)
    res = scan.print_check(path, bed=(300, 300, 300))
    assert res['overhang']['unsupported_area_fraction'] > 0.15
    assert any('support' in r.lower() for r in res['recommendations'])


def test_fit_circle_recovers_center_and_radius():
    pytest.importorskip('numpy')
    pts = [[10 + 5 * math.cos(t), -3 + 5 * math.sin(t)]
           for t in [i * math.pi / 16 for i in range(32)]]
    cx, cy, r, err = scan._fit_circle(pts)
    assert abs(cx - 10) < 1e-6
    assert abs(cy + 3) < 1e-6
    assert abs(r - 5) < 1e-6
    assert err < 1e-6


def test_sections_finds_circles_on_a_cylinder(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'cyl.stl')
    tm.creation.cylinder(radius=10.0, height=40.0, sections=128).export(path)
    res = scan.sections(path, axis='z', count=3)
    assert res['sketch_plane'] == 'XY'
    circles = [c for s in res['sections'] for c in s['contours']
               if c['kind'] == 'circle']
    assert circles, 'expected fitted circles on a cylinder'
    assert abs(circles[0]['radius_mm'] - 10.0) < 0.2


def test_analyze_box_reports_size_planes_and_symmetry(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'box.stl')
    tm.creation.box(extents=(40.0, 20.0, 10.0)).export(path)
    report = scan.analyze(path, max_primitives=8)
    assert report['watertight'] is True
    assert sorted(round(s) for s in report['size_mm']) == [10, 20, 40]
    assert len(report['planes']) == 6
    assert len(report['symmetry_planes']) == 3


def test_analyze_cylinder_finds_boss_cylinder(tmp_path):
    tm = pytest.importorskip('trimesh')
    pytest.importorskip('pyransac3d')
    path = str(tmp_path / 'cyl.stl')
    tm.creation.cylinder(radius=8.0, height=30.0, sections=96).export(path)
    report = scan.analyze(path)
    cyls = [c for c in report['cylinders'] if 'radius_mm' in c]
    assert cyls, 'expected a fitted cylinder'
    assert abs(cyls[0]['radius_mm'] - 8.0) < 0.5
    assert cyls[0]['kind'] == 'boss'


def test_refine_cylinder_rescues_a_bad_ransac_model():
    # pyransac3d's 3-point cylinder lottery shifts between releases (0.7.0
    # returned r=6.33 for an 8 mm cylinder); the deterministic refinement
    # must recover the true model from a poor-but-overlapping candidate.
    np = pytest.importorskip('numpy')
    angles = np.linspace(0.0, 2.0 * math.pi, 240, endpoint=False)
    heights = np.linspace(-15.0, 15.0, 25)
    ang, hgt = np.meshgrid(angles, heights)
    pts = np.column_stack([8.0 * np.cos(ang).ravel(),
                           8.0 * np.sin(ang).ravel(), hgt.ravel()])
    normals = np.column_stack([np.cos(ang).ravel(), np.sin(ang).ravel(),
                               np.zeros(ang.size)])
    # Deliberately bad start: tilted axis, offset centre, wrong radius.
    bad_axis = np.array([0.25, 0.1, 1.0])
    bad_axis /= np.linalg.norm(bad_axis)
    centre, axis, radius, ids = scan._refine_cylinder(
        pts, normals, np.array([1.5, -1.0, 2.0]), bad_axis, 6.3, thresh=0.6)
    assert ids is not None and len(ids) > 0.9 * len(pts)
    assert abs(radius - 8.0) < 0.05
    assert abs(abs(float(axis @ np.array([0.0, 0.0, 1.0]))) - 1.0) < 1e-3
    assert math.hypot(float(centre[0]), float(centre[1])) < 0.05


def test_deviation_of_identical_meshes_is_tiny(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'part.stl')
    tm.creation.box(extents=(30.0, 30.0, 6.0)).export(path)
    report = scan.deviation(path, path, samples=800, tolerance=0.5)
    assert report['scan_to_model']['p90_mm'] < 0.6
    assert report['scan_to_model']['within_tolerance'] > 0.9


def test_deviation_clamps_excessive_samples(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'part.stl')
    tm.creation.box(extents=(20.0, 20.0, 20.0)).export(path)
    report = scan.deviation(path, path, samples=10_000_000, tolerance=0.5)
    assert report['samples'] <= scan._MAX_DEVIATION_SAMPLES
    assert report['samples_clamped'] is True


def test_sections_respects_max_points_bound(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'blob.stl')
    # An icosphere slice is a many-point non-circular contour -> exercises the
    # polyline decimation stride (must stay within max_points).
    tm.creation.icosphere(subdivisions=3, radius=20.0).export(path)
    res = scan.sections(path, axis='z', count=2, max_points=40)
    for s in res['sections']:
        for c in s['contours']:
            if c['kind'] == 'polyline':
                assert len(c['points_mm']) <= 40


def test_print_check_flags_oversize_and_reports_fit(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'part.stl')
    tm.creation.box(extents=(50.0, 40.0, 10.0)).export(path)
    # Fits a normal bed.
    ok = scan.print_check(path, bed=(256, 256, 256))
    assert ok['fits_bed'] is True
    assert ok['fit_orientations']
    assert sorted(round(s) for s in ok['size_mm']) == [10, 40, 50]
    # Too small a bed in every orientation -> flagged.
    bad = scan.print_check(path, bed=(20, 20, 20))
    assert bad['fits_bed'] is False
    assert any('bed' in r.lower() for r in bad['recommendations'])
