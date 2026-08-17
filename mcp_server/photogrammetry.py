"""Photos -> textured mesh via an installed photogrammetry CLI, plus
real-world scale recovery from markers in the scene.

run() shells out to external programs only: RealityScan 2.x (Epic; free
under $1M revenue, CLI since 2.0, AMD GPUs since 2.2) is preferred, Meshroom
(`meshroom_batch`; full quality needs an NVIDIA/CUDA GPU) is the fallback.
Neither ships on PATH by default — standard install dirs are probed and
FUSION_MCP_PHOTOGRAMMETRY overrides with a full exe path.

Output is OBJ (both tools' native export); feed it to scan_convert ->
import_mesh, then the normal scan pipeline (scan_align, mesh_to_brep...)
takes over. Reconstruction runs MINUTES to HOURS depending on photo count
and GPU — the tool call blocks for up to `timeout` seconds.

Photogrammetry units are ARBITRARY. Two ways to fix that here:
* RealityScan: pass marker `distances` to run() — forwarded to the CLI as
  -detectMarkers/-defineDistance constraints (solved during alignment).
* Meshroom (or any pipeline that leaves a cameras.sfm): scale_from_markers()
  detects printed ArUco markers in the source photos, triangulates their
  corners with the calibrated camera poses and rescales the mesh to
  millimetres — needs the optional "photo" extras (OpenCV).
"""
import glob
import json
import os
import shutil
import subprocess

_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')


def detect():
    """Photogrammetry backends found on this machine, preference order."""
    found = []
    override = os.environ.get('FUSION_MCP_PHOTOGRAMMETRY')
    if override and os.path.isfile(override):
        name = 'meshroom' if 'meshroom' in os.path.basename(override).lower() \
            else 'realityscan'
        found.append({'backend': name, 'path': override})
    for exe in ('RealityScan.exe', 'RealityCapture.exe'):
        path = shutil.which(exe)
        if not path:
            hits = glob.glob(r'C:\Program Files\Epic Games\RealityScan*\%s'
                             % exe) + \
                glob.glob(r'C:\Program Files\Capturing Reality\RealityCapture*\%s'
                          % exe)
            path = hits[0] if hits else None
        if path and not any(e['path'] == path for e in found):
            found.append({'backend': 'realityscan', 'path': path})
            break
    path = shutil.which('meshroom_batch') or shutil.which('meshroom_batch.exe')
    if not path:
        hits = glob.glob(r'C:\Program Files\Meshroom*\meshroom_batch.exe')
        path = hits[0] if hits else None
    if path and not any(e['path'] == path for e in found):
        found.append({'backend': 'meshroom', 'path': path})
    return found


def _count_images(images_dir):
    return sum(1 for name in os.listdir(images_dir)
               if os.path.splitext(name)[1].lower() in _IMAGE_EXTS)


def build_command(backend, exe, images_dir, out_obj, simplify_faces,
                  detect_markers=False, distances=None):
    """The CLI invocation per backend (split out for unit testing).
    distances: [[marker_a, marker_b, mm], ...] known distances between
    detected markers — RealityScan only, forwarded as -defineDistance in
    METRES after -detectMarkers so alignment solves at real scale."""
    if backend == 'realityscan':
        # RealityScan/RealityCapture batch grammar: verbs execute in order.
        cmd = [exe, '-headless',
               '-addFolder', images_dir,
               '-align']
        if detect_markers or distances:
            # RealityCapture CLI reference verbs; RealityScan inherits the
            # CLI. UNVERIFIED against a live install — marker names must
            # match what the app assigns (check once in the GUI under
            # Alignment > Markers, e.g. "1x12:012" for coded targets).
            cmd.append('-detectMarkers')
            for item in (distances or []):
                cmd += ['-defineDistance', str(item[0]), str(item[1]),
                        '%g' % (float(item[2]) / 1000.0)]
            if distances:
                cmd.append('-update')
        cmd += ['-setReconstructionRegionAuto',
                '-calculateNormalModel',
                '-simplify', str(int(simplify_faces)),
                '-exportSelectedModel', out_obj,
                '-quit']
        return cmd
    if backend == 'meshroom':
        # meshroom_batch writes texturedMesh.obj into the output directory.
        return [exe, '--input', images_dir,
                '--output', os.path.dirname(out_obj)]
    raise RuntimeError('Unknown backend %r' % backend)


def run(images_dir, out_obj, backend='auto', simplify_faces=1000000,
        timeout=7200, detect_markers=False, distances=None):
    """Reconstruct a mesh from a folder of photos. images_dir: 20+ sharp,
    overlapping photos of the object (all sides, diffuse light, matte
    surface — shiny/black parts reconstruct poorly). out_obj: where the OBJ
    lands. backend: auto|realityscan|meshroom. distances: known mm distances
    between printed markers in the scene ([[name_a, name_b, mm], ...]) —
    RealityScan solves the scale during alignment; Meshroom ignores them
    (use scale_from_markers afterwards)."""
    if not os.path.isdir(images_dir):
        raise RuntimeError('images_dir not found: %r' % images_dir)
    for item in (distances or []):
        if len(item) != 3 or float(item[2]) <= 0:
            raise RuntimeError('distances items must be [marker_a, '
                               'marker_b, mm>0], got %r' % (item,))
    count = _count_images(images_dir)
    if count < 10:
        raise RuntimeError(
            'Only %d photos in %r — photogrammetry needs 20+ overlapping '
            'shots covering every side.' % (count, images_dir))
    backends = detect()
    if backend != 'auto':
        backends = [b for b in backends if b['backend'] == backend]
    if not backends:
        raise RuntimeError(
            'No photogrammetry backend found (looked for RealityScan/'
            'RealityCapture and meshroom_batch on PATH and in Program '
            'Files). Install RealityScan (free under $1M revenue) or set '
            'FUSION_MCP_PHOTOGRAMMETRY to the executable.')
    chosen = backends[0]
    os.makedirs(os.path.dirname(os.path.abspath(out_obj)), exist_ok=True)
    cmd = build_command(chosen['backend'], chosen['path'], images_dir,
                        out_obj, simplify_faces,
                        detect_markers=detect_markers, distances=distances)
    run_result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    produced = out_obj if os.path.isfile(out_obj) else None
    if produced is None and chosen['backend'] == 'meshroom':
        hits = glob.glob(os.path.join(os.path.dirname(out_obj), '**',
                                      'texturedMesh.obj'), recursive=True)
        produced = hits[0] if hits else None
    if run_result.returncode != 0 or produced is None:
        tail = ((run_result.stderr or '') + '\n'
                + (run_result.stdout or '')).strip()
        raise RuntimeError(
            '%s failed (exit %s) or produced no OBJ. CLI licensing note: '
            'the free RealityScan seat may require a logged-in Epic session '
            'for headless use. Output tail: %s'
            % (chosen['backend'], run_result.returncode, tail[-600:]))
    out = {
        'backend': chosen['backend'],
        'photos': count,
        'output': produced,
        'note': ('Units are ARBITRARY (no scale reference in photos) — '
                 'import with import_mesh, then scan_align against a known '
                 'model, or photogrammetry_scale with ArUco markers in the '
                 'scene. scan_convert can turn the OBJ into STL first if '
                 'needed.'),
    }
    if distances and chosen['backend'] == 'realityscan':
        out['note'] = ('Scale was constrained by %d marker distance(s) via '
                       'the CLI (verbs unverified live) — sanity-check the '
                       'result with scan_analyze before trusting it.'
                       % len(distances))
    elif distances:
        out['note'] += (' NOTE: the meshroom backend ignores CLI marker '
                        'distances — run photogrammetry_scale instead.')
    return out


# --------------------------------------------------------------------------- #
# Real-scale recovery from ArUco markers + Meshroom camera poses.
# --------------------------------------------------------------------------- #
def _find_sfm(mesh_path):
    base = os.path.dirname(os.path.abspath(mesh_path))
    for root in (base, os.path.dirname(base)):
        hits = glob.glob(os.path.join(root, '**', 'cameras.sfm'),
                         recursive=True)
        if hits:
            return hits[0]
    raise RuntimeError(
        'cameras.sfm not found near %r — pass sfm_path (Meshroom writes it '
        'in the StructureFromMotion cache). RealityScan reconstructions: '
        'pass marker distances to photogrammetry_run instead.' % mesh_path)


def _parse_sfm(sfm_path):
    """AliceVision cameras.sfm -> posed views with intrinsics. Handles both
    focal conventions (pxFocalLength in px; focalLength in mm +
    sensorWidth) and the newer principal point stored as an offset from the
    image centre. All numbers arrive as JSON strings."""
    with open(sfm_path, encoding='utf-8') as fh:
        data = json.load(fh)
    intrinsics = {}
    for item in data.get('intrinsics', []):
        width = float(item.get('width', 0))
        height = float(item.get('height', 0))
        if 'pxFocalLength' in item:
            value = item['pxFocalLength']
            if isinstance(value, (list, tuple)):
                fx, fy = float(value[0]), float(value[1])
            else:
                fx = fy = float(value)
        else:
            focal_mm = float(item.get('focalLength', 0))
            sensor = float(item.get('sensorWidth', 36.0)) or 36.0
            fx = fy = focal_mm / sensor * width
        pp = item.get('principalPoint', [width / 2.0, height / 2.0])
        px, py = float(pp[0]), float(pp[1])
        if abs(px) < 0.25 * width and abs(py) < 0.25 * height:
            px, py = width / 2.0 + px, height / 2.0 + py
        dist = [float(d) for d in (item.get('distortionParams') or [])]
        intrinsics[str(item.get('intrinsicId'))] = {
            'fx': fx, 'fy': fy, 'pp': (px, py), 'dist': dist,
            'wh': (width, height)}
    poses = {}
    for item in data.get('poses', []):
        transform = item['pose']['transform']
        poses[str(item['poseId'])] = (
            [float(v) for v in transform['rotation']],
            [float(v) for v in transform['center']])
    views = []
    for item in data.get('views', []):
        pose_id = str(item.get('poseId', item.get('viewId')))
        intr_id = str(item.get('intrinsicId'))
        if pose_id in poses and intr_id in intrinsics:
            views.append({'viewId': str(item.get('viewId')),
                          'path': item.get('path', ''),
                          'pose': poses[pose_id],
                          'intr': intrinsics[intr_id]})
    return views


def _undistort_observed(pts, intr):
    """Invert AliceVision's radial distortion (distorted = undistorted *
    (1 + k1 r^2 + k2 r^4 + k3 r^6) in normalised coords) by fixed-point
    iteration. Identity when no distortion params are present."""
    import numpy as np
    dist = intr.get('dist') or []
    if not any(dist):
        return np.asarray(pts, dtype='float64')
    k1, k2, k3 = (list(dist) + [0.0, 0.0, 0.0])[:3]
    focal = np.array([intr['fx'], intr['fy']])
    pp = np.array(intr['pp'])
    d_norm = (np.asarray(pts, dtype='float64') - pp) / focal
    u_norm = d_norm.copy()
    for _ in range(6):
        r2 = (u_norm ** 2).sum(axis=-1, keepdims=True)
        u_norm = d_norm / (1.0 + r2 * (k1 + r2 * (k2 + r2 * k3)))
    return u_norm * focal + pp


def _camera_matrix(view, convention):
    """3x4 projection for a posed view. AliceVision documents the stored
    rotation as camera-to-world ('c2w': x_cam = R^T (X - C)) but exporters
    have flip-flopped — the caller tries both and keeps the one that
    reprojects better."""
    import numpy as np
    r9, c3 = view['pose']
    rot = np.array(r9, dtype='float64').reshape(3, 3)
    if convention == 'c2w':
        rot = rot.T
    centre = np.array(c3, dtype='float64')
    intr = view['intr']
    k_mat = np.array([[intr['fx'], 0.0, intr['pp'][0]],
                      [0.0, intr['fy'], intr['pp'][1]],
                      [0.0, 0.0, 1.0]])
    return k_mat @ np.hstack([rot, (-rot @ centre).reshape(3, 1)])


def _triangulate(observations):
    """DLT triangulation from [(P 3x4, (x, y)), ...] -> 3D point or None."""
    import numpy as np
    rows = []
    for p_mat, (x, y) in observations:
        rows.append(x * p_mat[2] - p_mat[0])
        rows.append(y * p_mat[2] - p_mat[1])
    try:
        _, _, vt = np.linalg.svd(np.asarray(rows))
    except np.linalg.LinAlgError:
        return None
    hom = vt[-1]
    if abs(hom[3]) < 1e-12:
        return None
    return hom[:3] / hom[3]


def _scale_from_detections(views, detections, marker_length_mm, min_views=2):
    """Core scale solve (pure numpy, unit-testable). detections:
    {viewId: [(marker_id, corners 4x2 px), ...]}. Triangulates every marker
    corner under both pose conventions, keeps the one that reprojects
    better, and compares reconstructed marker sides with the printed
    length."""
    import numpy as np
    by_id = {v['viewId']: v for v in views}
    observations = {}
    for view_id, found in detections.items():
        view = by_id.get(str(view_id))
        if view is None:
            continue
        for marker_id, quad in found:
            quad = _undistort_observed(quad, view['intr'])
            for corner in range(4):
                observations.setdefault((marker_id, corner), []).append(
                    (str(view_id), quad[corner]))
    need = max(2, int(min_views))
    best = None
    for convention in ('c2w', 'w2c'):
        p_mats = {v['viewId']: _camera_matrix(v, convention) for v in views}
        points, errors = {}, []
        for key, seen in observations.items():
            if len(seen) < need:
                continue
            rows = [(p_mats[vid], xy) for vid, xy in seen]
            point = _triangulate(rows)
            if point is None:
                continue
            points[key] = point
            for p_mat, xy in rows:
                proj = p_mat @ np.append(point, 1.0)
                if abs(proj[2]) > 1e-12:
                    errors.append(float(np.hypot(
                        *(proj[:2] / proj[2] - np.asarray(xy)))))
        if errors and points:
            median_err = float(np.median(errors))
            if best is None or median_err < best[0]:
                best = (median_err, convention, points)
    if best is None:
        raise RuntimeError(
            'No marker corner was seen by %d+ registered cameras — print '
            'bigger markers, keep them sharp in more photos, or check the '
            'marker dictionary.' % need)
    reproj_px, convention, points = best
    sides, markers_used = [], {}
    for marker_id in {m for m, _ in points}:
        quad = [points.get((marker_id, corner)) for corner in range(4)]
        lengths = []
        for a in range(4):
            b = (a + 1) % 4
            if quad[a] is not None and quad[b] is not None:
                lengths.append(float(np.linalg.norm(quad[b] - quad[a])))
        if lengths:
            markers_used[marker_id] = lengths
            sides.extend(lengths)
    if not sides:
        raise RuntimeError(
            'Markers triangulated, but never two ADJACENT corners of the '
            'same marker — need sharper or bigger markers in more photos.')
    median_side = float(np.median(sides))
    return {
        'scale_mm_per_unit': float(marker_length_mm) / median_side,
        'spread_pct': round(
            100.0 * (max(sides) - min(sides)) / median_side, 2),
        'convention': convention,
        'reproj_px': round(reproj_px, 2),
        'markers_used': [
            {'id': mid, 'sides_units': [round(s, 5) for s in lens]}
            for mid, lens in sorted(markers_used.items(),
                                    key=lambda kv: str(kv[0]))],
    }


def _scale_mesh(mesh_path, out_path, factor):
    """Uniformly scale a mesh file. OBJ->OBJ is a pure text transform that
    keeps textures/materials intact; other formats go through trimesh."""
    if mesh_path.lower().endswith('.obj') \
            and out_path.lower().endswith('.obj'):
        with open(mesh_path, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        scaled = []
        for line in lines:
            if line.startswith('v '):
                parts = line.split()
                xyz = ['%.6f' % (float(p) * factor) for p in parts[1:4]]
                scaled.append(' '.join(['v'] + xyz + parts[4:]) + '\n')
            else:
                scaled.append(line)
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.writelines(scaled)
        return
    try:
        import trimesh
    except ImportError as exc:
        raise RuntimeError(
            'Scaling %s files needs the optional [re] extras (trimesh) — '
            'pip install trimesh, or scale the OBJ output instead.'
            % os.path.splitext(mesh_path)[1]) from exc
    mesh = trimesh.load(mesh_path, force='mesh')
    mesh.apply_scale(factor)
    mesh.export(out_path)


def scale_from_markers(mesh_path, images_dir, marker_length_mm,
                       sfm_path=None, marker='4x4_50', out_path=None,
                       min_views=2):
    """Give a photogrammetry mesh its REAL millimetre scale from printed
    ArUco markers that were lying in the scene: detect them in the source
    photos, triangulate their corners with the Meshroom camera poses
    (cameras.sfm), compare with the printed side length and write a scaled
    copy of the mesh. Needs the "photo" extras (OpenCV). All markers must
    share the same printed size."""
    import photo
    photo._require_cv2()
    if float(marker_length_mm) <= 0:
        raise RuntimeError('marker_length_mm must be positive')
    if not os.path.isfile(mesh_path):
        raise RuntimeError('mesh not found: %r' % mesh_path)
    if not os.path.isdir(images_dir):
        raise RuntimeError('images_dir not found: %r' % images_dir)
    sfm_path = sfm_path or _find_sfm(mesh_path)
    views = _parse_sfm(sfm_path)
    if not views:
        raise RuntimeError('%r holds no posed views' % sfm_path)
    by_name = {}
    for view in views:
        base = os.path.basename(view['path']).lower()
        if base:
            by_name[base] = view
    detections, matched = {}, 0
    for name in sorted(os.listdir(images_dir)):
        view = by_name.get(name.lower())
        if view is None:
            continue
        gray = photo.cv2.imread(os.path.join(images_dir, name),
                                photo.cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        matched += 1
        found = photo._detect_aruco_all(gray, marker)
        if found:
            detections[view['viewId']] = found
    if not detections:
        raise RuntimeError(
            'No %s markers detected in the %d registered photos of %r — '
            'were the printed markers actually in the scene?'
            % (marker, matched, images_dir))
    stats = _scale_from_detections(views, detections, marker_length_mm,
                                   min_views=min_views)
    factor = stats.pop('scale_mm_per_unit')
    if not out_path:
        stem, ext = os.path.splitext(mesh_path)
        out_path = stem + '_mm' + ext
    _scale_mesh(mesh_path, out_path, factor)
    note = ('Mesh rescaled to millimetres — import_mesh(units="mm") and '
            'carry on with the scan pipeline.')
    if stats['spread_pct'] > 3.0:
        note += (' WARNING: marker sides disagree by %.1f%% — noisy '
                 'reconstruction or blurry markers; treat dimensions as '
                 'approximate.' % stats['spread_pct'])
    return {
        'mesh': mesh_path,
        'output': out_path,
        'sfm': sfm_path,
        'marker': marker,
        'marker_length_mm': float(marker_length_mm),
        'photos_with_markers': len(detections),
        'scale_mm_per_unit': round(factor, 6),
        **stats,
        'note': note,
    }
