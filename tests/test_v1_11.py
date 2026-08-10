"""Tests for the v1.11.0 scan+photo wave: ICP alignment, fit check, cavity
sections and format conversion (scan.py), photo rectification/vectorisation
(photo.py), and the pure-Python add-in helpers (mesh file writer, canvas
matrix math). Mesh/photo tests skip cleanly when the optional extras are
missing; CI installs both sets so nothing skips there."""
import math
import os
import struct

import commands
import photo
import pytest
import scan

requires_re = pytest.mark.skipif(
    scan.trimesh is None, reason="optional 're' extras not installed")
requires_photo = pytest.mark.skipif(
    photo.cv2 is None or photo.ezdxf is None,
    reason="optional 'photo' extras not installed")


# --------------------------------------------------------------------------- #
# scan.align
# --------------------------------------------------------------------------- #
@requires_re
def test_align_recovers_a_rigid_transform(tmp_path):
    import numpy as np
    import trimesh
    model = trimesh.creation.box(extents=(40, 20, 10))
    move = trimesh.transformations.rotation_matrix(
        math.radians(30), (0, 0, 1))
    move[:3, 3] = (15.0, -7.0, 3.0)
    scan_mesh = model.copy()
    scan_mesh.apply_transform(np.linalg.inv(move))
    scan_path, model_path = tmp_path / 's.stl', tmp_path / 'm.stl'
    scan_mesh.export(str(scan_path))
    model.export(str(model_path))

    out = tmp_path / 'aligned.stl'
    rep = scan.align(str(scan_path), str(model_path), out_path=str(out))
    assert rep['rms_after_mm'] < 0.5
    assert rep['rms_after_mm'] < rep['rms_before_mm']
    assert rep['scale'] == 1.0
    assert out.exists()
    # The written mesh must actually sit on the model.
    aligned = trimesh.load(str(out), force='mesh')
    assert np.abs(aligned.bounds - model.bounds).max() < 1.0


@requires_re
def test_align_solves_uniform_scale_when_asked(tmp_path):
    import trimesh
    model = trimesh.creation.box(extents=(40, 20, 10))
    scan_mesh = model.copy()
    scan_mesh.apply_scale(1.06)  # 6 % scanner calibration error
    scan_path, model_path = tmp_path / 's.stl', tmp_path / 'm.stl'
    scan_mesh.export(str(scan_path))
    model.export(str(model_path))
    rep = scan.align(str(scan_path), str(model_path), scale=True)
    assert abs(rep['scale'] - 1 / 1.06) < 0.01
    assert rep['rms_after_mm'] < 0.5


# --------------------------------------------------------------------------- #
# scan.fit_check
# --------------------------------------------------------------------------- #
@requires_re
def test_fit_check_clearance_when_object_is_outside_the_part(tmp_path):
    import trimesh
    trimesh.creation.box(extents=(30, 30, 30)).export(str(tmp_path / 's.stl'))
    trimesh.creation.box(extents=(10, 10, 10)).export(str(tmp_path / 'm.stl'))
    rep = scan.fit_check(str(tmp_path / 's.stl'), str(tmp_path / 'm.stl'),
                         clearance_mm=20.0)
    assert rep['signed'] is True
    assert rep['collisions'] == 0
    # Box corners at +-15 vs a +-5 box: nearest point is the model corner.
    assert abs(rep['clearance_mm']['min'] - math.sqrt(300)) < 0.5
    assert rep['below_target'] == 1.0  # 17.3 mm < 20 mm target


@requires_re
def test_fit_check_counts_collisions_inside_the_part(tmp_path):
    import trimesh
    trimesh.creation.box(extents=(10, 10, 10)).export(str(tmp_path / 's.stl'))
    trimesh.creation.box(extents=(30, 30, 30)).export(str(tmp_path / 'm.stl'))
    rep = scan.fit_check(str(tmp_path / 's.stl'), str(tmp_path / 'm.stl'))
    assert rep['signed'] is True
    assert rep['collisions'] == 8  # every box vertex is inside the model
    assert abs(rep['penetration_mm']['max'] - 10.0) < 0.5
    assert len(rep['worst_points_mm']) == 8


# --------------------------------------------------------------------------- #
# scan.cavity_sections
# --------------------------------------------------------------------------- #
def _inside_convex(poly, pt, tol=1e-6):
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (bx - ax) * (pt[1] - ay) - (by - ay) * (pt[0] - ax) < -tol:
            return False
    return True


@requires_re
def test_cavity_sections_monotone_anchored_and_margined(tmp_path):
    import trimesh
    # Cone: base radius 20 at z=0, apex at z=40 — wide bottom, narrow top.
    trimesh.creation.cone(radius=20, height=40).export(str(tmp_path / 's.stl'))
    rep = scan.cavity_sections(str(tmp_path / 's.stl'), axis='z', spacing=5.0,
                               margin=2.0, cumulative='above')
    secs = [s for s in rep['sections'] if 'points_mm' in s]
    assert len(secs) >= 5
    areas = [s['area_mm2'] for s in secs]
    # Lower sections must contain (be at least as large as) upper ones.
    assert areas == sorted(areas, reverse=True)
    for upper, lower in zip(secs[1:], secs[:-1], strict=False):
        for pt in upper['points_mm']:
            assert _inside_convex(lower['points_mm'], pt, tol=1e-4)
    # Bottom outline ~ base circle + margin.
    bottom = secs[0]['points_mm']
    radii = [math.hypot(x, y) for x, y in bottom]
    assert max(radii) <= 22.6
    assert max(radii) > 21.0
    # Shared loft anchor: every section starts at its max-x vertex.
    for s in secs:
        xs = [p[0] for p in s['points_mm']]
        assert s['points_mm'][0][0] == max(xs)


@requires_re
def test_cavity_sections_clamps_section_count(tmp_path):
    import trimesh
    trimesh.creation.cone(radius=20, height=40).export(str(tmp_path / 's.stl'))
    rep = scan.cavity_sections(str(tmp_path / 's.stl'), spacing=0.2)
    assert rep['spacing_clamped'] is True
    assert len(rep['sections']) <= scan._MAX_CAVITY_SECTIONS + 1


# --------------------------------------------------------------------------- #
# scan.convert
# --------------------------------------------------------------------------- #
@requires_re
def test_convert_ply_to_stl(tmp_path):
    import trimesh
    src = tmp_path / 'phone_scan.ply'
    trimesh.creation.box(extents=(50, 30, 20)).export(str(src))
    rep = scan.convert(str(src))
    assert rep['output'].endswith('.stl')
    assert os.path.exists(rep['output'])
    assert rep['triangles'] == 12
    assert 'units_warning' not in rep
    reloaded = trimesh.load(rep['output'], force='mesh')
    assert abs(float(reloaded.extents.max()) - 50) < 1e-6


@requires_re
def test_convert_warns_on_metre_scale_files(tmp_path):
    import trimesh
    src = tmp_path / 'metres.ply'
    trimesh.creation.box(extents=(0.05, 0.03, 0.02)).export(str(src))
    rep = scan.convert(str(src), out_path=str(tmp_path / 'out.stl'))
    assert 'units_warning' in rep


# --------------------------------------------------------------------------- #
# photo.py
# --------------------------------------------------------------------------- #
def test_missing_photo_deps_error_is_actionable(monkeypatch):
    monkeypatch.setattr(photo, 'cv2', None)
    monkeypatch.setattr(photo, '_CV2_ERROR', 'No module named cv2')
    with pytest.raises(RuntimeError) as err:
        photo._require_cv2()
    assert 'mcp_server[photo]' in str(err.value)
    assert 'opencv-contrib' in str(err.value)


@requires_photo
def test_to_sketch_dxf_traces_a_rectangle(tmp_path):
    import numpy as np
    img = np.full((200, 300), 255, dtype=np.uint8)
    img[50:150, 100:250] = 0  # 150 x 100 px black rectangle
    path = tmp_path / 'part.png'
    photo.cv2.imwrite(str(path), img)

    rep = photo.to_sketch_dxf(str(path), 0.5, epsilon_mm=0.6)
    assert rep['contours'] == 1
    width = rep['max_mm'][0] - rep['min_mm'][0]
    height = rep['max_mm'][1] - rep['min_mm'][1]
    assert abs(width - 75.0) < 2.0   # 150 px * 0.5 mm/px
    assert abs(height - 50.0) < 2.0
    doc = photo.ezdxf.readfile(rep['output'])
    polys = list(doc.modelspace().query('LWPOLYLINE'))
    assert len(polys) == 1
    assert polys[0].closed


@requires_photo
def test_to_sketch_dxf_rejects_blank_images(tmp_path):
    import numpy as np
    path = tmp_path / 'blank.png'
    photo.cv2.imwrite(str(path), np.full((100, 100), 255, dtype=np.uint8))
    with pytest.raises(RuntimeError) as err:
        photo.to_sketch_dxf(str(path), 0.5)
    assert 'invert' in str(err.value)


@requires_photo
def test_rectify_finds_aruco_and_scales_the_canvas(tmp_path):
    import numpy as np
    aruco = photo.cv2.aruco
    dictionary = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
    gen = getattr(aruco, 'generateImageMarker', None) or aruco.drawMarker
    marker = gen(dictionary, 7, 200)
    canvas = np.full((600, 800), 255, dtype=np.uint8)
    canvas[200:400, 300:500] = marker
    path = tmp_path / 'photo.png'
    photo.cv2.imwrite(str(path), canvas)

    rep = photo.rectify(str(path), marker='4x4_50', marker_size_mm=40.0,
                        mm_per_px=0.4, margin_mm=10.0)
    assert rep['marker_ids'] == [7]
    assert os.path.exists(rep['output'])
    # 200 px marker -> 40 mm / 0.4 mm/px = 100 px: everything halves, plus
    # a 25 px margin on each side.
    assert abs(rep['size_px'][0] - (400 + 50)) <= 6
    assert abs(rep['size_px'][1] - (300 + 50)) <= 6


@requires_photo
def test_rectify_without_marker_raises(tmp_path):
    import numpy as np
    path = tmp_path / 'plain.png'
    photo.cv2.imwrite(str(path), np.full((100, 100), 255, dtype=np.uint8))
    with pytest.raises(RuntimeError) as err:
        photo.rectify(str(path))
    assert 'marker' in str(err.value)


# --------------------------------------------------------------------------- #
# Add-in helpers (fake adsk from conftest)
# --------------------------------------------------------------------------- #
def test_write_mesh_file_binary_stl_roundtrip(tmp_path):
    verts = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)]
    tris = [(0, 1, 2)]
    path = str(tmp_path / 'one.stl')
    commands._write_mesh_file(path, verts, tris)
    with open(path, 'rb') as fh:
        blob = fh.read()
    assert len(blob) == 80 + 4 + 50
    assert struct.unpack('<I', blob[80:84])[0] == 1
    values = struct.unpack('<12fH', blob[84:])
    assert values[:3] == (0.0, 0.0, 1.0)          # +Z normal
    assert values[3:6] == (0.0, 0.0, 0.0)         # first vertex
    assert values[6:9] == (10.0, 0.0, 0.0)


def test_write_mesh_file_obj_and_bad_extension(tmp_path):
    verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    path = str(tmp_path / 'one.obj')
    commands._write_mesh_file(path, verts, [(0, 1, 2)])
    with open(path, encoding='ascii') as fh:
        lines = fh.read().splitlines()
    assert lines[0].startswith('v ')
    assert lines[-1] == 'f 1 2 3'  # OBJ indices are 1-based
    with pytest.raises(ValueError):
        commands._write_mesh_file(str(tmp_path / 'one.step'), verts, [(0, 1, 2)])


def test_triangle_data_converts_cm_to_mm():
    class _Poly:
        nodeIndices = [0, 1, 2]
        nodeCoordinatesAsDouble = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 2.0, 0.0]

    class _Body:
        mesh = _Poly()
        displayMesh = None

    verts, tris = commands._triangle_data(_Body())
    assert tris == [(0, 1, 2)]
    assert verts[1] == (10.0, 0.0, 0.0)  # 1 cm -> 10 mm
    assert verts[2] == (0.0, 20.0, 0.0)


def test_mat3_mul_identity_and_translation():
    ident = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    shift = [[1, 0, 5], [0, 1, -3], [0, 0, 1]]
    assert commands._mat3_mul(ident, shift) == shift
    twice = commands._mat3_mul(shift, shift)
    assert twice[0][2] == 10 and twice[1][2] == -6
