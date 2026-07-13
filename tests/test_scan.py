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


def test_assemble_loops_separates_disjoint_loops_and_drops_degenerate():
    two = [
        ((0, 0, 0), (1, 0, 0)), ((1, 0, 0), (0.5, 1, 0)), ((0.5, 1, 0), (0, 0, 0)),
        ((5, 5, 5), (6, 5, 5)), ((6, 5, 5), (5.5, 6, 5)), ((5.5, 6, 5), (5, 5, 5)),
        ((9, 9, 9), (9, 9, 9)),  # zero-length: ignored
    ]
    loops = scan._assemble_loops(two)
    assert len(loops) == 2
    assert all(closed for _, closed in loops)


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


def test_deviation_of_identical_meshes_is_tiny(tmp_path):
    tm = pytest.importorskip('trimesh')
    path = str(tmp_path / 'part.stl')
    tm.creation.box(extents=(30.0, 30.0, 6.0)).export(path)
    report = scan.deviation(path, path, samples=800, tolerance=0.5)
    assert report['scan_to_model']['p90_mm'] < 0.6
    assert report['scan_to_model']['within_tolerance'] > 0.9
