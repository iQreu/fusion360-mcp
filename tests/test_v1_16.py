"""Tests for v1.16.0 "skany + RE": recon module (segment / features /
profile / thread / offset / frame / fit_report), the add-in's pure
generators (snap-fit, fir-tree profiles) and dispatch wiring. Mesh tests
build their fixtures procedurally (no boolean engine needed) and skip
cleanly without the optional 're' stack."""
import math
import os
import re

import commands
import pytest
import recon

np = pytest.importorskip('numpy') if recon.np is not None else None
_HAS_RE = recon.trimesh is not None
_HAS_SKIMAGE = True
try:
    import skimage  # noqa: F401
except ImportError:
    _HAS_SKIMAGE = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# version pins — the ONLY place with the literal
# --------------------------------------------------------------------------- #
def test_versions_pinned_to_1_16_0():
    from _version import __version__
    assert __version__ == '1.16.0'
    assert commands.VERSION == '1.16.0'
    with open(os.path.join(ROOT, 'mcp_server', 'pyproject.toml'), encoding='utf-8') as fh:
        assert re.search(r'^version = "1\.16\.0"$', fh.read(), re.M)
    with open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8') as fh:
        assert '## v1.16.0' in fh.read()
    # The add-in manifest is what Fusion's Add-Ins dialog shows as the
    # version — it sat at "1.0.0" for fifteen releases.
    import json
    with open(os.path.join(ROOT, 'fusion_addin', 'FusionMCP', 'FusionMCP.manifest'),
              encoding='utf-8') as fh:
        assert json.load(fh)['version'] == commands.VERSION


# --------------------------------------------------------------------------- #
# dispatch wiring
# --------------------------------------------------------------------------- #
def test_new_ops_registered_and_probe_table_sane():
    for op in ('capabilities_probe', 'new_document', 'fillet_max_radius',
               'sketch_profile', 'add_boss', 'add_snap_fit', 'add_clip_fir_tree'):
        assert op in commands.DISPATCH, op
    assert 'capabilities_probe' in commands._READ_ONLY_OPS
    assert 'fillet_max_radius' not in commands._READ_ONLY_OPS
    kinds = {'features', 'features_attr', 'class', 'module', 'module_class',
             'app_attr', 'root_attr', 'design_attr', 'call'}
    labels = [row[0] for row in commands._CAPABILITY_PROBES]
    assert len(labels) == len(set(labels)), 'duplicate probe labels'
    for label, kind, _spec, tools in commands._CAPABILITY_PROBES:
        assert kind in kinds, label
        assert tools, label


def test_registry_replace_only_touches_known_tokens():
    from registry import Registry
    reg = Registry()
    tok = reg.add('edg', object())
    fresh = object()
    reg.replace(tok, fresh)
    assert reg.get(tok) is fresh
    reg.replace('edg999', fresh)
    with pytest.raises(KeyError):
        reg.get('edg999')


# --------------------------------------------------------------------------- #
# generators — pure geometry
# --------------------------------------------------------------------------- #
def test_snap_fit_profile_shape_and_hook():
    pts = commands.snap_fit_profile(12.0, 1.5, 1.0, lead_angle_deg=30.0)
    assert len(pts) == 7
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert min(xs) == 0.0 and min(ys) == 0.0
    assert max(ys) == pytest.approx(2.5)                 # thickness + undercut
    # Retention face is vertical at x = length.
    assert [12.0, 2.5] in pts and [12.0, 1.5] in pts
    tapered = commands.snap_fit_profile(12.0, 1.5, 1.0, taper=True)
    assert len(tapered) == 8
    assert [12.0, 0.75] in tapered                       # half thickness at the tip


def test_snap_fit_mechanics_flags_overstrain_with_suggestions():
    ok = commands.snap_fit_mechanics(15.0, 1.5, 1.2, 6.0, material='petg')
    assert ok['strain'] == pytest.approx(1.5 * 1.5 * 1.2 / 225.0, rel=1e-6)
    assert ok['ok'] is True
    bad = commands.snap_fit_mechanics(8.0, 2.0, 2.0, 6.0, material='pla')
    assert bad['ok'] is False
    assert bad['suggest_length_mm'] > 8.0
    assert 0 < bad['suggest_undercut_mm'] < 2.0
    tapered = commands.snap_fit_mechanics(8.0, 2.0, 2.0, 6.0, material='pla', taper=True)
    assert tapered['strain'] < bad['strain']
    assert ok['insertion_force_n'] > ok['deflection_force_n'] > 0


def test_fir_tree_profile_geometry():
    prof = commands.fir_tree_profile(6.0, panel_thickness=1.5, fins=3, interference=0.4)
    assert prof[0][0] == 0.0 and prof[-1][0] == 0.0          # starts/ends on the axis
    assert prof[0][1] == 1.5                                  # head top
    assert max(r for r, _z in prof) == pytest.approx(6.0)     # head radius = (d+6)/2
    fin_r = max(r for r, z in prof if z < 0)
    assert fin_r == pytest.approx(3.4)                        # hole/2 + interference
    assert sum(1 for r, _z in prof if abs(r - 3.4) < 1e-9) == 3
    with pytest.raises(ValueError):
        commands.fir_tree_profile(6.0, 1.5, stem_diameter=7.0)


# --------------------------------------------------------------------------- #
# recon — pure helpers
# --------------------------------------------------------------------------- #
def test_thread_candidates_prefer_iso_coarse():
    hits = recon._thread_candidates(7.9, 1.25, internal=False)
    assert hits and hits[0]['designation'] == 'M8'
    fine = recon._thread_candidates(9.9, 1.0, internal=False)
    assert fine[0]['designation'] == 'M10x1'
    internal = recon._thread_candidates(6.65, 1.25, internal=True)   # nut minor ~ D - 1.08p
    assert internal[0]['designation'] == 'M8'
    unc = recon._thread_candidates(6.3, 25.4 / 20, internal=False)
    assert any(h['designation'].startswith('1/4-20') for h in unc)


def test_hole_patterns_rectangle_pcd_linear_pair():
    def hole(hid, x, y, d=6.0, plane=0):
        return {'id': hid, 'plane': plane, 'center_mm': [x, y, 0.0], 'diameter_mm': d}
    rect = recon._hole_patterns([hole('H1', -20, -12), hole('H2', -20, 12),
                                 hole('H3', 20, 12), hole('H4', 20, -12)])
    assert rect[0]['layout'] == 'rectangle'
    assert rect[0]['sides_mm'] == [24.0, 40.0]
    assert rect[0]['pcd_mm'] == pytest.approx(46.65, abs=0.01)
    pcd = recon._hole_patterns([hole('H%d' % k, 20 * math.cos(a), 20 * math.sin(a))
                                for k, a in enumerate(np.linspace(0, 2 * math.pi, 6,
                                                                  endpoint=False))])
    assert pcd[0]['layout'] == 'circular' and pcd[0]['pcd_mm'] == pytest.approx(40.0)
    lin = recon._hole_patterns([hole('H1', 0, 0), hole('H2', 10, 0), hole('H3', 20, 0)])
    assert lin[0]['layout'] == 'linear' and lin[0]['pitch_mm'] == [10.0, 10.0]
    pair = recon._hole_patterns([hole('H1', 0, 0), hole('H2', 15, 0)])
    assert pair[0]['layout'] == 'pair' and pair[0]['spacings'][0]['spacing_mm'] == 15.0
    # Different diameters never pattern together.
    mixed = recon._hole_patterns([hole('H1', 0, 0, 3.0), hole('H2', 15, 0, 8.0)])
    assert mixed == []


def test_fold_score_finds_pitch_and_hand_on_synthetic_helix():
    # Points spread over the whole cylinder surface (low-discrepancy, not a
    # single helix curve — that would make phase constant).
    k = np.arange(6000)
    z = (k * 0.6180339887) % 1.0 * 25.0
    theta = (k * 0.3819660113) % 1.0 * 2 * math.pi
    pitch, hand = 1.25, 1
    phase = ((z - hand * pitch * theta / (2 * math.pi)) / pitch) % 1.0
    r = 4.0 - 0.7 * np.abs(phase - 0.5)
    right = recon._fold_score(z, theta, r - r.mean(), pitch, 1)
    left = recon._fold_score(z, theta, r - r.mean(), pitch, -1)
    off = recon._fold_score(z, theta, r - r.mean(), 1.0, 1)
    assert right > 0.9 and left < 0.3 and off < 0.3


def test_segment_polyline_rounded_bar_outline():
    # 50 x 20 bar with a semicircular +x end — analytic points, no mesh.
    pts = [(-25.0, -10.0 + 20.0 * k / 40) for k in range(40)]            # left edge up
    pts += [(-25.0 + 50.0 * k / 100, 10.0) for k in range(101)]           # top edge
    ang = np.linspace(math.pi / 2, -math.pi / 2, 60)[1:-1]
    pts += [(25.0 + 10 * math.cos(a), 10 * math.sin(a)) for a in ang]     # arc down
    pts += [(25.0 - 50.0 * k / 100, -10.0) for k in range(101)]           # bottom edge back
    xy = np.array(pts)
    prims = recon._segment_polyline(xy, True, 0.15)
    kinds = [p['kind'] for p in prims]
    assert kinds.count('arc') == 1 and kinds.count('line') == 3, kinds
    segs = recon._build_segments(xy, prims, True, 0.15, 2.0, 0.5)
    arc = [s for s in segs if s['kind'] == 'arc'][0]
    assert arc['radius_mm'] == pytest.approx(10.0, abs=0.05)
    assert arc['center'][0] == pytest.approx(25.0, abs=0.1)
    assert recon._residual_to_segments(xy, segs) < 0.15
    lines = [s for s in segs if s['kind'] == 'line']
    assert all(abs(s['length_mm'] - 50.0) < 0.3 or abs(s['length_mm'] - 20.0) < 0.3
               for s in lines)


def test_cumulative_and_fill_interior_numpy_paths():
    grid = np.zeros((3, 3, 6), dtype=bool)
    grid[1, 1, 2] = True
    down = recon._cumulative(grid, 2, from_positive=True)    # cover from +z: fill below
    assert down[1, 1, :3].all() and not down[1, 1, 3:].any()
    up = recon._cumulative(grid, 2, from_positive=False)
    assert up[1, 1, 2:].all() and not up[1, 1, :2].any()
    shell = np.zeros((7, 7, 7), dtype=bool)
    shell[1:6, 1:6, 1:6] = True
    shell[2:5, 2:5, 2:5] = False                              # hollow cube
    solid = recon._fill_interior(shell)
    assert solid[3, 3, 3] and solid[1:6, 1:6, 1:6].all() and not solid[0].any()


# --------------------------------------------------------------------------- #
# recon — meshes (procedural fixtures)
# --------------------------------------------------------------------------- #
def _extrude_convex(points_2d, height):
    """Solid from a convex CCW polygon: fan caps + side quads (no shapely)."""
    trimesh = recon.trimesh
    pts = np.asarray(points_2d, dtype=float)
    n = len(pts)
    bottom = np.column_stack([pts, np.zeros(n)])
    top = np.column_stack([pts, np.full(n, float(height))])
    verts = np.vstack([bottom, top])
    faces = []
    for k in range(1, n - 1):
        faces.append([0, k + 1, k])                    # bottom (facing -z)
        faces.append([n, n + k, n + k + 1])            # top (facing +z)
    for k in range(n):
        a, b = k, (k + 1) % n
        faces.append([a, b, n + b])
        faces.append([a, n + b, n + a])
    mesh = trimesh.Trimesh(verts, np.array(faces), process=True)
    trimesh.repair.fix_normals(mesh)
    return mesh


def _threaded_rod(path, d=8.0, p=1.25, length=20.0, hand=1, nz=240, nt=64):
    trimesh = recon.trimesh
    depth = 0.6134 * p
    r_major = d / 2 - 0.05
    zs = np.linspace(0, length, nz)
    ts = np.linspace(0, 2 * math.pi, nt, endpoint=False)
    zz, tt = np.meshgrid(zs, ts, indexing='ij')
    phase = ((zz - hand * p * tt / (2 * math.pi)) / p) % 1.0
    rr = r_major - depth * (1 - (1 - 2 * np.abs(phase - 0.5)))
    verts = np.column_stack([(rr * np.cos(tt)).ravel(), (rr * np.sin(tt)).ravel(), zz.ravel()])
    faces = []
    for i in range(nz - 1):
        for j in range(nt):
            a, b = i * nt + j, i * nt + (j + 1) % nt
            c, dd = (i + 1) * nt + j, (i + 1) * nt + (j + 1) % nt
            faces += [[a, b, dd], [a, dd, c]]
    trimesh.Trimesh(verts, np.array(faces), process=False).export(path)


@pytest.mark.skipif(not _HAS_RE, reason="optional 're' extras not installed")
def test_features_on_annulus_finds_through_hole_and_thickness(tmp_path):
    trimesh = recon.trimesh
    ring = trimesh.creation.annulus(r_min=3.0, r_max=12.0, height=5.0, sections=64)
    path = str(tmp_path / 'ring.stl')
    ring.export(path)
    rep = recon.features(path)
    assert rep['patches']['planes'] == 2 and rep['patches']['cylinders'] == 2
    holes = rep['holes']
    assert len(holes) == 2                      # the bore seen from both faces
    for h in holes:
        assert h['diameter_mm'] == pytest.approx(6.0, abs=0.05)
        assert h['through'] is True
        assert h['depth_mm'] == pytest.approx(5.0, abs=0.05)
    assert rep['thicknesses'][0]['thickness_mm'] == pytest.approx(5.0, abs=1e-3)
    # The outer wall is a full convex cylinder -> reported as a boss, not a hole.
    assert any(b['diameter_mm'] == pytest.approx(24.0, abs=0.05) for b in rep['bosses'])
    seg = recon.segment(path, out_path=str(tmp_path / 'ring_seg.ply'))
    assert seg['patch_count'] == 4 and os.path.exists(seg['out_path'])
    assert seg['unassigned_area_fraction'] == 0.0


@pytest.mark.skipif(not _HAS_RE, reason="optional 're' extras not installed")
def test_profile_rounded_bar_gives_lines_and_arc(tmp_path):
    outline = [(-25.0, -10.0), (25.0, -10.0)]
    outline += [(25.0 + 10 * math.cos(a), 10 * math.sin(a))
                for a in np.linspace(-math.pi / 2, math.pi / 2, 40)[1:-1]]
    outline += [(25.0, 10.0), (-25.0, 10.0)]
    path = str(tmp_path / 'bar.stl')
    _extrude_convex(outline, 6.0).export(path)
    rep = recon.profile(path, axis='z', offset=3.0)
    loop = rep['loops'][0]
    assert loop['closed'] and loop['arcs'] == 1 and loop['lines'] == 3
    assert loop['max_residual_mm'] < 0.15
    arc = [s for s in loop['segments'] if s['kind'] == 'arc'][0]
    assert arc['radius_mm'] == pytest.approx(10.0, abs=0.05)
    assert 'start_3d' in arc and arc['start_3d'][2] == pytest.approx(3.0)
    assert rep['sketch_plane'] == 'XY' and rep['offset_mm'] == 3.0
    # Tangent junction: the top line runs exactly horizontal after the fix.
    top = [s for s in loop['segments'] if s['kind'] == 'line'
           and abs(s['start'][1] - 10.0) < 0.2 and abs(s['end'][1] - 10.0) < 0.2]
    assert top and abs(abs(top[0]['angle_deg']) - 180.0) < 0.2 or top[0]['angle_deg'] == 0.0


@pytest.mark.skipif(not _HAS_RE, reason="optional 're' extras not installed")
def test_thread_identify_m8_right_and_left(tmp_path):
    right = str(tmp_path / 'rod.stl')
    left = str(tmp_path / 'rod_l.stl')
    _threaded_rod(right, hand=1)
    _threaded_rod(left, hand=-1)
    rep = recon.thread_identify(right)
    assert rep['thread_detected'] and rep['designation'] == 'M8'
    assert rep['pitch_mm'] == pytest.approx(1.25, abs=0.01)
    assert rep['hand'] == 'right' and rep['kind'] == 'external'
    assert rep['major_diameter_mm'] == pytest.approx(7.9, abs=0.15)
    assert recon.thread_identify(left)['hand'] == 'left'
    # A plain cylinder must NOT be reported as threaded.
    plain = str(tmp_path / 'plain.stl')
    recon.trimesh.creation.cylinder(radius=4.0, height=20.0, sections=64).export(plain)
    assert recon.thread_identify(plain)['thread_detected'] is False


@pytest.mark.skipif(not _HAS_RE, reason="optional 're' extras not installed")
def test_frame_puts_tilted_plate_on_datums(tmp_path):
    trimesh = recon.trimesh
    plate = trimesh.creation.box(extents=[60.0, 40.0, 5.0])
    plate.apply_transform(trimesh.transformations.rotation_matrix(
        math.radians(25), [1.0, 0.4, 0.2]))
    plate.apply_translation([100.0, -30.0, 55.0])
    path = str(tmp_path / 'tilted.stl')
    plate.export(path)
    rep = recon.frame(path, out_path=str(tmp_path / 'framed.stl'))
    assert rep['bbox_min_mm'] == pytest.approx([0.0, 0.0, 0.0], abs=0.01)
    assert rep['bbox_max_mm'] == pytest.approx([60.0, 40.0, 5.0], abs=0.01)
    # The transform maps original vertices onto the framed mesh.
    t = np.array(rep['transform_4x4'])
    framed = trimesh.load(rep['output'])
    moved = (np.c_[plate.vertices, np.ones(len(plate.vertices))] @ t.T)[:, :3]
    assert np.allclose(np.sort(moved, axis=0), np.sort(framed.vertices, axis=0), atol=1e-3)


@pytest.mark.skipif(not (_HAS_RE and _HAS_SKIMAGE), reason='needs re extras + scikit-image')
def test_mesh_offset_grows_box_and_monotone_caps(tmp_path):
    trimesh = recon.trimesh
    path = str(tmp_path / 'box.stl')
    trimesh.creation.box(extents=[30.0, 20.0, 10.0]).export(path)
    rep = recon.mesh_offset(path, 2.0, out_path=str(tmp_path / 'off.stl'))
    assert rep['watertight'] is True
    assert rep['size_mm'] == pytest.approx([34.0, 24.0, 14.0], abs=1.5 * rep['voxel_pitch_mm'])
    mono = recon.mesh_offset(path, 2.0, out_path=str(tmp_path / 'mono.stl'),
                             mode='monotone', axis='z', approach='+', extend_mm=5.0)
    assert mono['watertight'] is True
    # Cover from +z: the cavity opens at -z, so the cutter extends `extend_mm`
    # past the part downward (and not upward).
    assert mono['size_mm'][2] > rep['size_mm'][2] + 4.0
    lo, hi = recon.trimesh.load(mono['output']).bounds[:, 2]
    assert lo < -5.0 - 4.0 and hi < 5.0 + 2.0 + 2 * mono['voxel_pitch_mm']
    assert mono['size_mm'][0] == pytest.approx(rep['size_mm'][0], abs=1.5 * rep['voxel_pitch_mm'])


@pytest.mark.skipif(not _HAS_RE, reason="optional 're' extras not installed")
def test_fit_report_verdicts_and_thresholds(tmp_path):
    trimesh = recon.trimesh
    model = str(tmp_path / 'model.stl')
    trimesh.creation.box(extents=[30.0, 20.0, 10.0]).export(model)
    rep = recon.fit_report(model)
    names = [c['check'] for c in rep['checks']]
    assert 'printability' in names and 'walls' in names and 'seating' not in names
    assert rep['overall'] in ('PASS', 'WARN')
    assert rep['thresholds']['min_wall_mm'] == 1.2
    # A scan that sits INSIDE the model must fail seating.
    inner = str(tmp_path / 'inner.stl')
    trimesh.creation.box(extents=[10.0, 10.0, 4.0]).export(inner)
    rep2 = recon.fit_report(model, scan_path=inner)
    seat = [c for c in rep2['checks'] if c['check'] == 'seating'][0]
    assert seat['status'] == 'FAIL' and seat['collisions'] > 0
    assert rep2['overall'] == 'FAIL'
    big = str(tmp_path / 'big.stl')
    trimesh.creation.box(extents=[400.0, 20.0, 10.0]).export(big)
    assert [c for c in recon.fit_report(big)['checks']
            if c['check'] == 'printability'][0]['status'] == 'FAIL'


# --------------------------------------------------------------------------- #
# server: the new tools register (needs the mcp SDK — skipped otherwise)
# --------------------------------------------------------------------------- #
def test_server_registers_v1_16_tools():
    pytest.importorskip('mcp')
    import asyncio

    import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    for expected in ('scan_segment', 'scan_features', 'scan_profile',
                     'scan_thread_identify', 'scan_frame', 'scan_mesh_offset',
                     'scan_fit_report', 'capabilities_probe', 'new_document',
                     'fillet_max_radius', 'sketch_profile', 'add_boss',
                     'add_snap_fit', 'add_clip_fir_tree'):
        assert expected in names, expected
    assert server._toolset_of('scan_thread_identify') == 'scan'
    assert server._toolset_of('add_snap_fit') == 'detail'
    assert server._toolset_of('capabilities_probe') == 'diag'
    assert server.MCP_SDK_MAJOR in (1, 2)
