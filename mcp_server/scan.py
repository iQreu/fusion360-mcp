"""Scan analysis for reverse engineering.

Everything here runs in the MCP server process — no round-trip to Fusion and
no load on Fusion's single UI thread. The heavy dependencies are optional
(the "re" extras: numpy, scipy, trimesh, pyransac3d); every public function
raises a RuntimeError with install instructions when they are missing, so the
server works fine without them.

Units: mesh files are assumed to be in MILLIMETRES (the wire convention).
`analyze` flags suspicious sizes so the model can ask the user about units.

Implementation notes: sampling and nearest-neighbour queries are hand-rolled
on numpy instead of using numpy.random/scipy — deterministic output, and the
fewer native submodules we touch the fewer machines (e.g. with Windows Smart
App Control blocking unsigned DLLs) the tools break on.
"""
import math
import random

try:
    import numpy as np
    import trimesh
except ImportError as exc:  # pragma: no cover - exercised via _require test
    np = None
    trimesh = None
    _IMPORT_ERROR = str(exc)
else:
    _IMPORT_ERROR = None

_INSTALL_HINT = (
    "Scan tools need the optional 're' dependencies. Install them with: "
    "pip install -e \"mcp_server[re]\"  (or: pip install numpy trimesh "
    "pyransac3d) and restart the MCP server."
)

# Sampling caps keep every call bounded on big scans.
_SAMPLE_PRIMITIVES = 6000
_SAMPLE_SYMMETRY = 1500
_SAMPLE_THICKNESS = 300


def _require():
    if trimesh is None:
        raise RuntimeError('%s Import error: %s' % (_INSTALL_HINT, _IMPORT_ERROR))


def _load(path):
    _require()
    mesh = trimesh.load(path, force='mesh')
    if mesh.is_empty or len(mesh.faces) == 0:
        raise RuntimeError('No triangles found in %r' % path)
    return mesh


def _rounded(value, digits=4):
    if isinstance(value, (list, tuple)):
        return [_rounded(v, digits) for v in value]
    if hasattr(value, 'tolist'):
        return _rounded(value.tolist(), digits)
    if isinstance(value, float):
        return round(value, digits)
    return value


def _sample_surface(mesh, count, seed=0):
    """Deterministic area-weighted surface sampling (avoids numpy.random —
    reproducible reports, fewer native modules). Returns (points, face_idx)."""
    rng = random.Random(seed)
    areas = np.asarray(mesh.area_faces, dtype=float)
    cum = np.cumsum(areas)
    if cum[-1] <= 0:
        raise RuntimeError('Mesh has zero surface area')
    picks = np.array([rng.random() for _ in range(count)]) * cum[-1]
    fidx = np.clip(np.searchsorted(cum, picks), 0, len(areas) - 1)
    tri = mesh.triangles[fidx]
    r1 = np.sqrt(np.array([rng.random() for _ in range(count)]))
    r2 = np.array([rng.random() for _ in range(count)])
    a, b, c = 1.0 - r1, r1 * (1.0 - r2), r1 * r2
    pts = tri[:, 0] * a[:, None] + tri[:, 1] * b[:, None] + tri[:, 2] * c[:, None]
    return pts, fidx


def _nearest(query, ref, exclude_self=False, chunk=256):
    """Nearest ref point per query point -> (distances, indices). Chunked
    numpy brute force — no KD-tree dependency, fine for the sample sizes used
    here. exclude_self=True skips the identical index (query IS ref)."""
    query = np.asarray(query, dtype=float)
    ref = np.asarray(ref, dtype=float)
    ref_sq = (ref ** 2).sum(axis=1)
    dists = np.empty(len(query))
    idx = np.empty(len(query), dtype=int)
    for i in range(0, len(query), chunk):
        q = query[i:i + chunk]
        d2 = ((q ** 2).sum(axis=1)[:, None] + ref_sq[None, :]
              - 2.0 * (q @ ref.T))
        if exclude_self:
            rows = np.arange(i, min(i + chunk, len(query)))
            d2[np.arange(len(rows)), rows] = np.inf
        best = d2.argmin(axis=1)
        idx[i:i + chunk] = best
        dists[i:i + chunk] = np.sqrt(np.clip(
            d2[np.arange(len(best)), best], 0.0, None))
    return dists, idx


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #
def analyze(path, max_primitives=8):
    """Inspect a scan/mesh file and return a rebuild plan: overall size,
    symmetry planes, RANSAC-fitted planes/cylinders/spheres (with hole/boss
    classification), and a wall-thickness estimate."""
    mesh = _load(path)
    bounds = mesh.bounds
    size = (bounds[1] - bounds[0])
    scale = float(size.max())

    report = {
        'file': path,
        'triangles': int(len(mesh.faces)),
        'vertices': int(len(mesh.vertices)),
        'watertight': bool(mesh.is_watertight),
        'min_mm': _rounded(bounds[0]),
        'max_mm': _rounded(bounds[1]),
        'size_mm': _rounded(size),
        'area_mm2': _rounded(float(mesh.area), 2),
    }
    if mesh.is_watertight:
        report['volume_mm3'] = _rounded(float(mesh.volume), 2)
        report['center_of_mass_mm'] = _rounded(mesh.center_mass)
    if scale < 5 or scale > 5000:
        report['units_warning'] = (
            'Largest extent is %.3f mm — the file may not be in millimetres. '
            'Re-import with the right units or scale it.' % scale)

    report['symmetry_planes'] = _symmetry_planes(mesh, scale)
    report['planes'] = _planes_from_facets(mesh, max_primitives)
    cylinders, spheres = _ransac_primitives(mesh, scale, max_primitives)
    report['cylinders'] = cylinders
    report['spheres'] = spheres
    report['wall_thickness_mm'] = _wall_thickness(mesh, scale)
    report['note'] = (
        'Coordinates are mesh coordinates in mm. Typical flow: rebuild the '
        'main shape from planes/cylinders (or scan_sections contours), then '
        'verify with scan_deviation against an exported STL of the solid.')
    return report


def _symmetry_planes(mesh, scale):
    """Reflection symmetry across the three axis-aligned planes through the
    bounding-box centre (covers the vast majority of mechanical parts)."""
    pts, _ = _sample_surface(mesh, _SAMPLE_SYMMETRY)
    centre = mesh.bounds.mean(axis=0)
    # Sampling density baseline: mirrored points on a truly symmetric part
    # land about one sample-spacing away from the nearest sample.
    spacing = float(np.percentile(_nearest(pts, pts, exclude_self=True)[0], 95))
    out = []
    for axis, name in enumerate(('YZ', 'XZ', 'XY')):
        mirrored = pts.copy()
        mirrored[:, axis] = 2.0 * centre[axis] - mirrored[:, axis]
        p95 = float(np.percentile(_nearest(mirrored, pts)[0], 95))
        if p95 < max(1.5 * spacing, 0.005 * scale):
            out.append({'plane': name, 'axis': 'xyz'[axis],
                        'at_mm': _rounded(float(centre[axis])),
                        'p95_error_mm': _rounded(p95)})
    return out


def _planes_from_facets(mesh, max_primitives, min_fraction=0.02):
    """Large planar regions. Prefers trimesh's coplanar facet groups; falls
    back to bucketing faces by (normal, plane offset) when the facet graph
    machinery is unavailable — same report, no connectivity required."""
    total = float(mesh.area)
    rows = None
    try:
        areas = np.asarray(mesh.facets_area)
        if len(areas):
            rows = (areas, np.asarray(mesh.facets_normal),
                    np.asarray(mesh.facets_origin))
    except Exception:  # noqa: BLE001 - fall through to the bucket method
        rows = None
    if rows is None:
        normals = mesh.face_normals
        centres = mesh.triangles_center
        offsets = np.einsum('ij,ij->i', centres, normals)
        keys = np.column_stack([np.round(normals, 2), np.round(offsets, 1)])
        uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
        areas = np.bincount(inverse, weights=mesh.area_faces,
                            minlength=len(uniq))
        reps = np.zeros(len(uniq), dtype=int)
        reps[inverse] = np.arange(len(inverse))
        rows = (areas, normals[reps], centres[reps])

    areas, normals, origins = rows
    out = []
    for idx in np.argsort(areas)[::-1][:max_primitives]:
        fraction = float(areas[idx]) / total
        if fraction < min_fraction:
            break
        out.append({
            'normal': _rounded(normals[idx]),
            'point_mm': _rounded(origins[idx]),
            'area_mm2': _rounded(float(areas[idx]), 2),
            'area_fraction': _rounded(fraction),
        })
    return out


def _ransac_primitives(mesh, scale, max_primitives):
    """Iteratively fit cylinders (and one sphere) with pyRANSAC-3D, removing
    inliers between rounds. Cylinders are classified hole vs boss using the
    surface normals of their inlier points."""
    try:
        import pyransac3d
    except ImportError:
        msg = {'error': 'pyransac3d missing — cylinder/sphere fit skipped. %s'
                        % _INSTALL_HINT}
        return [msg], [msg]

    pts, face_idx = _sample_surface(mesh, _SAMPLE_PRIMITIVES)
    normals = mesh.face_normals[face_idx]
    thresh = max(0.003 * scale, 0.02)
    # pyransac3d draws with the global stdlib RNG; seed it so reports are
    # reproducible run-to-run.
    random.seed(0)
    cylinders = []
    remaining = np.arange(len(pts))
    for _ in range(max_primitives):
        if len(remaining) < 200:
            break
        sub = pts[remaining]
        try:
            with np.errstate(all='ignore'):  # degenerate draws are expected
                centre, axis, radius, inliers = pyransac3d.Cylinder().fit(
                    sub, thresh=thresh, maxIteration=800)
        except Exception:  # noqa: BLE001 - RANSAC can fail on degenerate leftovers
            break
        if radius <= 0 or radius > scale or len(inliers) / len(pts) < 0.03:
            break
        chosen = remaining[np.asarray(inliers, dtype=int)]
        axis = np.asarray(axis, dtype=float)
        norm = np.linalg.norm(axis)
        if norm == 0:
            break
        axis = axis / norm
        radial = pts[chosen] - np.asarray(centre, dtype=float)
        radial -= np.outer(radial @ axis, axis)
        lengths = np.linalg.norm(radial, axis=1)
        ok = lengths > 1e-9
        side = float(np.mean(np.einsum(
            'ij,ij->i', normals[chosen][ok], radial[ok] / lengths[ok, None])))
        span = pts[chosen] @ axis
        cylinders.append({
            'radius_mm': _rounded(float(radius)),
            'diameter_mm': _rounded(2.0 * float(radius)),
            'point_mm': _rounded(list(centre)),
            'axis': _rounded(list(axis)),
            'length_mm': _rounded(float(span.max() - span.min())),
            'kind': 'hole' if side < 0 else 'boss',
            'coverage': _rounded(len(chosen) / len(pts)),
        })
        keep = np.ones(len(remaining), dtype=bool)
        keep[np.asarray(inliers, dtype=int)] = False
        remaining = remaining[keep]

    spheres = []
    if len(remaining) >= 200:
        try:
            with np.errstate(all='ignore'):  # degenerate draws are expected
                centre, radius, inliers = pyransac3d.Sphere().fit(
                    pts[remaining], thresh=thresh, maxIteration=800)
            if 0 < radius < scale and len(inliers) / len(pts) >= 0.05:
                spheres.append({
                    'radius_mm': _rounded(float(radius)),
                    'center_mm': _rounded(list(centre)),
                    'coverage': _rounded(len(inliers) / len(pts)),
                })
        except Exception:  # noqa: BLE001
            pass
    return cylinders, spheres


def _wall_thickness(mesh, scale):
    """Median wall thickness from inward ray casts off sampled surface points.
    Approximate, but enough to pick shell thickness when rebuilding."""
    try:
        pts, face_idx = _sample_surface(mesh, _SAMPLE_THICKNESS)
        normals = mesh.face_normals[face_idx]
        origins = pts - normals * (1e-3 * scale)
        locs, ray_idx, _ = mesh.ray.intersects_location(
            origins, -normals, multiple_hits=False)
        dist = []
        for loc, ray in zip(locs, ray_idx, strict=False):
            d = float(np.linalg.norm(loc - pts[ray]))
            if 0 < d < 0.5 * scale:
                dist.append(d)
        if not dist:
            return None
        return {'median': _rounded(float(np.median(dist)), 3),
                'p5': _rounded(float(np.percentile(dist, 5)), 3),
                'samples': len(dist)}
    except Exception as exc:  # noqa: BLE001 - ray backend availability varies
        return {'error': 'thickness estimate failed: %s' % exc}


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
_AXES = {
    'x': (0, (1, 2), 'YZ', ('y', 'z')),
    'y': (1, (0, 2), 'XZ', ('x', 'z')),
    'z': (2, (0, 1), 'XY', ('x', 'y')),
}


def _assemble_loops(segments, tol=1e-4):
    """Chain raw plane-intersection segments into ordered point loops.

    segments: iterable of ((x, y, z), (x, y, z)). Pure Python on purpose —
    deterministic, dependency-free and unit-testable.
    Returns [(points, closed)], points as [x, y, z] lists.
    """
    def key(pt):
        return (round(pt[0] / tol), round(pt[1] / tol), round(pt[2] / tol))

    adjacency = {}
    segs = []
    for seg in segments:
        a, b = tuple(seg[0]), tuple(seg[1])
        if key(a) == key(b):
            continue
        idx = len(segs)
        segs.append((a, b))
        adjacency.setdefault(key(a), []).append((idx, 0))
        adjacency.setdefault(key(b), []).append((idx, 1))

    used = [False] * len(segs)
    loops = []
    for start in range(len(segs)):
        if used[start]:
            continue
        used[start] = True
        a, b = segs[start]
        points = [list(a), list(b)]
        closed = False
        while True:
            tail = points[-1]
            candidates = [c for c in adjacency.get(key(tuple(tail)), [])
                          if not used[c[0]]]
            if not candidates:
                break
            idx, end = candidates[0]
            used[idx] = True
            nxt = segs[idx][1 - end]
            if key(nxt) == key(tuple(points[0])):
                closed = True
                break
            points.append(list(nxt))
        loops.append((points, closed))
    return loops


def _fit_circle(points_2d):
    """Least-squares (Kasa) circle fit. Returns (cx, cy, r, rms_error)."""
    pts = np.asarray(points_2d, dtype=float)
    a = np.column_stack([2.0 * pts[:, 0], 2.0 * pts[:, 1], np.ones(len(pts))])
    b = (pts ** 2).sum(axis=1)
    (cx, cy, c), *_ = np.linalg.lstsq(a, b, rcond=None)
    r = math.sqrt(max(c + cx * cx + cy * cy, 0.0))
    err = float(np.sqrt(np.mean(
        (np.linalg.norm(pts - (cx, cy), axis=1) - r) ** 2)))
    return float(cx), float(cy), float(r), err


def sections(path, axis='z', count=8, heights=None, max_points=80):
    """Slice a scan with planes perpendicular to a world axis and return the
    contours per slice — circles where a circle fits, decimated polylines
    otherwise — as sketch-ready 2D coordinates."""
    mesh = _load(path)
    axis = (axis or 'z').lower()
    if axis not in _AXES:
        raise RuntimeError("axis must be x|y|z, got %r" % axis)
    ax, (u, v), sketch_plane, coord_names = _AXES[axis]

    lo, hi = float(mesh.bounds[0][ax]), float(mesh.bounds[1][ax])
    if heights:
        levels = [float(h) for h in heights]
    else:
        inset = 0.02 * (hi - lo)
        levels = list(np.linspace(lo + inset, hi - inset, int(count)))

    normal = np.zeros(3)
    normal[ax] = 1.0
    out = []
    for h in levels:
        origin = np.zeros(3)
        origin[ax] = h
        segs = trimesh.intersections.mesh_plane(mesh, normal, origin)
        contours = []
        for points, closed in _assemble_loops(segs):
            flat = [[pt[u], pt[v]] for pt in points]
            entry = {'closed': closed, 'points': len(flat)}
            cx, cy, r, err = (None, None, None, None)
            if closed and len(flat) >= 8:
                cx, cy, r, err = _fit_circle(flat)
            if r and err < max(0.02, 0.01 * r):
                entry.update({'kind': 'circle', 'center_mm': _rounded([cx, cy]),
                              'radius_mm': _rounded(r),
                              'fit_error_mm': _rounded(err)})
            else:
                stride = max(1, len(flat) // max_points)
                entry.update({'kind': 'polyline',
                              'points_mm': _rounded(flat[::stride])})
            contours.append(entry)
        out.append({'height_mm': _rounded(float(h)), 'contours': contours})

    return {
        'file': path,
        'axis': axis,
        'sketch_plane': sketch_plane,
        'coords': list(coord_names),
        'note': ('For each slice: construction_plane(method="offset", '
                 'base="%s", offset=height_mm), then draw the contours on it. '
                 '2D coordinates map to world (%s, %s).'
                 % (sketch_plane, coord_names[0], coord_names[1])),
        'sections': out,
    }


# --------------------------------------------------------------------------- #
# deviation
# --------------------------------------------------------------------------- #
def deviation(scan_path, model_path, samples=4000, tolerance=0.2):
    """Compare a scan with a rebuilt model (both mesh files, both in mm):
    point-sampled two-way surface distances with percentile stats and the
    fraction within tolerance. Approximate (sampling-based) by design."""
    scan_mesh = _load(scan_path)
    model_mesh = _load(model_path)

    def one_way(src, dst):
        # Nearest sampled point picks the candidate face; the exact
        # point-to-triangle distance removes in-plane sampling noise.
        src_pts, _ = _sample_surface(src, int(samples))
        dst_pts, dst_fidx = _sample_surface(dst, int(samples) * 4, seed=1)
        _, idx = _nearest(src_pts, dst_pts)
        tris = dst.triangles[dst_fidx[idx]]
        closest = trimesh.triangles.closest_point(tris, src_pts)
        dist = np.linalg.norm(src_pts - closest, axis=1)
        return {
            'mean_mm': _rounded(float(dist.mean()), 4),
            'rms_mm': _rounded(float(np.sqrt((dist ** 2).mean())), 4),
            'p50_mm': _rounded(float(np.percentile(dist, 50)), 4),
            'p90_mm': _rounded(float(np.percentile(dist, 90)), 4),
            'p99_mm': _rounded(float(np.percentile(dist, 99)), 4),
            'max_mm': _rounded(float(dist.max()), 4),
            'within_tolerance': _rounded(float((dist <= tolerance).mean())),
        }

    report = {
        'scan': scan_path,
        'model': model_path,
        'tolerance_mm': tolerance,
        'scan_to_model': one_way(scan_mesh, model_mesh),
        'model_to_scan': one_way(model_mesh, scan_mesh),
        'note': ('Sampling-based approximation. scan_to_model shows scan '
                 'regions the model misses; model_to_scan shows model regions '
                 'absent from the scan.'),
    }
    if scan_mesh.is_watertight and model_mesh.is_watertight:
        report['volume_scan_mm3'] = _rounded(float(scan_mesh.volume), 2)
        report['volume_model_mm3'] = _rounded(float(model_mesh.volume), 2)
    return report
