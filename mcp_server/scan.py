"""Scan analysis for reverse engineering.

Everything here runs in the MCP server process — no round-trip to Fusion and
no load on Fusion's single UI thread. The heavy dependencies are optional
(the "re" extras: numpy, trimesh, pyransac3d); every public function
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
import os
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
# print_check — 3D-print readiness analysis (no Fusion, no numpy.random)
# --------------------------------------------------------------------------- #
_PRINT_SAMPLE = 4000


def print_check(path, bed=(256.0, 256.0, 256.0), overhang_deg=45.0,
                min_wall=1.0, nozzle=0.4):
    """Score an STL for FDM 3D printing (all mm): bed fit across orientations,
    unsupported-overhang area for a Z build, thin walls vs nozzle/min_wall, and
    a flat footprint estimate. Pure trimesh/numpy — no Fusion round-trip."""
    mesh = _load(path)
    ext = np.asarray(mesh.extents, dtype=float)  # x,y,z size
    size = [float(x) for x in ext]
    bed = [float(b) for b in bed]

    # Bed fit: the part fits if some axis-permutation of its bounding box fits
    # within the bed footprint (x,y) and height (z).
    import itertools
    fits_orientations = []
    for perm in set(itertools.permutations(range(3))):
        d = [ext[perm[0]], ext[perm[1]], ext[perm[2]]]
        if d[0] <= bed[0] and d[1] <= bed[1] and d[2] <= bed[2]:
            fits_orientations.append(['xyz'[perm[0]], 'xyz'[perm[1]], 'xyz'[perm[2]]])
    fits = bool(fits_orientations)

    # Overhang: face area whose downward tilt from vertical exceeds the printable
    # angle (measured for a +Z build). A face needs support when the angle
    # between its normal and -Z is small (it faces down) beyond the threshold.
    normals = np.asarray(mesh.face_normals, dtype=float)
    areas = np.asarray(mesh.area_faces, dtype=float)
    total_area = float(areas.sum()) or 1.0
    down = normals[:, 2]  # cos(angle to +Z); downward faces have negative z
    # Steepness from the build plate: a face is "overhang" if it points downward
    # more than `overhang_deg` below horizontal, excluding the near-flat bottom.
    overhang_mask = down < -math.sin(math.radians(overhang_deg))
    # Exclude only the base resting on the plate: fully downward faces AND near
    # the lowest Z. An elevated flat overhang (bridge/ceiling) also has a
    # downward normal but sits high, so it must STILL count as unsupported.
    z_centroid = np.asarray(mesh.triangles_center, dtype=float)[:, 2]
    z_min = float(z_centroid.min())
    # A FIXED thin band (~a few first layers), deliberately NOT scaled by part
    # height: scaling it excluded ever-larger elevated undersides on tall parts
    # — a bridge 3 mm above the bed was classed "on plate" once the part grew
    # past 150 mm, silencing the exact overhang this check exists to catch.
    base_band = 0.5
    on_plate = (down < -0.999) & (z_centroid <= z_min + base_band)
    overhang_mask = overhang_mask & ~on_plate
    overhang_area = float(areas[overhang_mask].sum())

    walls = _wall_thickness(mesh, float(max(ext)))
    thin = None
    if walls and 'p5' in walls:
        thin = {
            'median_mm': walls['median'],
            'p5_mm': walls['p5'],
            'below_min_wall': walls['p5'] < min_wall,
            'below_two_nozzle': walls['p5'] < 2 * nozzle,
        }

    recommendations = []
    if not fits:
        recommendations.append('Part exceeds the bed in every orientation — '
                               'scale down or split it.')
    if overhang_area / total_area > 0.15:
        recommendations.append('Significant unsupported overhang area (%.0f%%) — '
                               'reorient or enable supports.'
                               % (100 * overhang_area / total_area))
    if thin and thin['below_min_wall']:
        recommendations.append('Thin walls below %.2f mm detected — thicken or '
                               'they may not print.' % min_wall)
    if not mesh.is_watertight:
        recommendations.append('Mesh is not watertight — repair before slicing.')
    if not recommendations:
        recommendations.append('No blocking issues found for FDM printing.')

    return {
        'file': path,
        'size_mm': _rounded(size, 2),
        'bed_mm': bed,
        'fits_bed': fits,
        'fit_orientations': fits_orientations,
        'watertight': bool(mesh.is_watertight),
        'volume_mm3': _rounded(float(mesh.volume), 2) if mesh.is_watertight else None,
        'overhang': {
            'threshold_deg': overhang_deg,
            'unsupported_area_fraction': _rounded(overhang_area / total_area, 3),
        },
        'walls': thin or walls,
        'min_wall_mm': min_wall,
        'nozzle_mm': nozzle,
        'recommendations': recommendations,
    }


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

    def walk(points):
        """Extend `points` forward from its tail as far as the chain goes.
        Returns True if it closed back onto points[0]. Stops without appending
        if it would re-enter a vertex already in the chain (a branch junction on
        a non-watertight contour), so a shared vertex is never duplicated."""
        seen = {key(tuple(pt)) for pt in points}
        while True:
            tail = points[-1]
            candidates = [c for c in adjacency.get(key(tuple(tail)), [])
                          if not used[c[0]]]
            if not candidates:
                return False
            idx, end = candidates[0]
            nxt = segs[idx][1 - end]
            nkey = key(nxt)
            if nkey == key(tuple(points[0])):
                used[idx] = True
                return True
            if nkey in seen:
                # Re-entering the existing chain (degree>=3 junction): stop here
                # rather than appending a duplicate vertex.
                return False
            used[idx] = True
            seen.add(nkey)
            points.append(list(nxt))

    for start in range(len(segs)):
        if used[start]:
            continue
        used[start] = True
        a, b = segs[start]
        points = [list(a), list(b)]
        closed = walk(points)
        if not closed:
            # The seed may sit MID-chain in an open (non-watertight) contour, so
            # also walk backward from the head and prepend — otherwise one
            # physical contour is fragmented into arbitrary pieces. The backward
            # walk can be the one that closes the loop (e.g. when the forward
            # walk stopped at a degree-3 junction), so keep its result.
            head = list(reversed(points))
            closed = walk(head)      # extends from the original start point
            points = list(reversed(head))
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
                # Ceiling division keeps the decimated count <= max_points
                # (floor division could return nearly 2x the promised bound).
                stride = max(1, -(-len(flat) // max_points))
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
# Upper bound on deviation() sampling: the chunked NN allocates a
# (chunk, 4*samples) float64 matrix, so an unbounded `samples` (e.g. 100000)
# would try to allocate hundreds of MB and thrash/OOM the target machines.
_MAX_DEVIATION_SAMPLES = 20000


def deviation(scan_path, model_path, samples=4000, tolerance=0.2):
    """Compare a scan with a rebuilt model (both mesh files, both in mm):
    point-sampled two-way surface distances with percentile stats and the
    fraction within tolerance. Approximate (sampling-based) by design."""
    scan_mesh = _load(scan_path)
    model_mesh = _load(model_path)
    requested = int(samples)
    samples = max(100, min(requested, _MAX_DEVIATION_SAMPLES))
    samples_clamped = samples != requested

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
        'samples': samples,
        'samples_clamped': samples_clamped,
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


# --------------------------------------------------------------------------- #
# align — rigid (optionally scaled) registration of a scan onto a model
# --------------------------------------------------------------------------- #
_MAX_ALIGN_SAMPLES = 8000


def _kabsch(src, dst, allow_scale=False):
    """Best-fit rotation (+ optional uniform scale) and translation mapping
    src points onto dst points (paired Nx3 arrays). Returns (R, s, t)."""
    sc, dc = src.mean(axis=0), dst.mean(axis=0)
    s0, d0 = src - sc, dst - dc
    u, sv, vt = np.linalg.svd(s0.T @ d0)
    sign = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    corr = np.array([1.0, 1.0, sign])
    rot = vt.T @ np.diag(corr) @ u.T
    scale = 1.0
    if allow_scale:
        denom = float((s0 ** 2).sum())
        if denom > 0:
            scale = float((sv * corr).sum() / denom)
    t = dc - scale * (rot @ sc)
    return rot, scale, t


def align(scan_path, model_path, out_path=None, samples=3000, scale=False,
          iterations=40, tolerance=1e-4):
    """Rigidly align a scan file onto a model file (both mm) with deterministic
    ICP: centroid + PCA-axis initial guesses, then iterate nearest-point /
    Kabsch. scale=True also solves a uniform scale (scanner calibration error).
    Writes the aligned scan to out_path (STL/OBJ by extension) when given.
    Returns the 4x4 transform (mm) and before/after RMS distances — run this
    before scan_deviation / scan_fit_check when the scan and the model are not
    in the same coordinate frame."""
    scan_mesh = _load(scan_path)
    model_mesh = _load(model_path)
    requested = int(samples)
    samples = max(200, min(requested, _MAX_ALIGN_SAMPLES))
    src_pts, _ = _sample_surface(scan_mesh, samples)
    dst_pts, _ = _sample_surface(model_mesh, samples * 2, seed=1)

    def rms_of(pts, probe=600):
        stride = max(1, -(-len(pts) // probe))
        d, _ = _nearest(pts[::stride], dst_pts)
        return float(np.sqrt((d ** 2).mean()))

    # Initial candidates: plain centroid shift, plus PCA-axis alignment. The
    # eigenvector signs are ambiguous, so all four proper-rotation sign
    # combinations compete; the cheapest (probe RMS) one seeds the ICP.
    sc, dc = src_pts.mean(axis=0), dst_pts.mean(axis=0)
    candidates = [(np.eye(3), dc - sc)]
    try:
        _, vs = np.linalg.eigh(np.cov((src_pts - sc).T))
        _, vd = np.linalg.eigh(np.cov((dst_pts - dc).T))
        parity = np.sign(np.linalg.det(vs) * np.linalg.det(vd)) or 1.0
        for fx in (1.0, -1.0):
            for fy in (1.0, -1.0):
                f = np.diag([fx, fy, parity * fx * fy])  # det(R) = +1
                rot = vd @ f @ vs.T
                candidates.append((rot, dc - rot @ sc))
    except Exception:  # noqa: BLE001 - degenerate covariance: centroid only
        pass
    rms_before = rms_of(src_pts)
    rot, t = min(candidates, key=lambda c: rms_of(src_pts @ c[0].T + c[1]))
    s = 1.0
    cur = src_pts @ rot.T + t

    rms = prev_rms = None
    used = 0
    while used < max(1, int(iterations)):
        used += 1
        _, idx = _nearest(cur, dst_pts)
        r_step, s_step, t_step = _kabsch(cur, dst_pts[idx], allow_scale=scale)
        cur = s_step * (cur @ r_step.T) + t_step
        rot = r_step @ rot
        s = s_step * s
        t = s_step * (r_step @ t) + t_step
        rms = float(np.sqrt(((cur - dst_pts[idx]) ** 2).sum(axis=1).mean()))
        if prev_rms is not None and abs(prev_rms - rms) < tolerance:
            break
        prev_rms = rms

    matrix = np.eye(4)
    matrix[:3, :3] = s * rot
    matrix[:3, 3] = t
    report = {
        'scan': scan_path,
        'model': model_path,
        'matrix_mm': _rounded([list(row) for row in matrix], 6),
        'scale': _rounded(float(s), 6),
        'rms_before_mm': _rounded(rms_before),
        'rms_after_mm': _rounded(rms),
        'iterations': used,
        'samples': samples,
        'samples_clamped': samples != requested,
        'note': ('Nearest-sample RMS (approximate). Apply matrix_mm to scan '
                 'coordinates to land in model coordinates. If rms_after is '
                 'still large the shapes may genuinely differ, or the PCA '
                 'seed picked a wrong symmetry — retry with scale=True or '
                 'pre-rotate the scan.'),
    }
    if out_path:
        aligned = scan_mesh.copy()
        aligned.vertices = np.asarray(aligned.vertices) @ (s * rot).T + t
        aligned.export(out_path)
        report['aligned_path'] = out_path
    return report


# --------------------------------------------------------------------------- #
# fit_check — does the scanned object fit the designed part?
# --------------------------------------------------------------------------- #
_MAX_FIT_POINTS = 20000


def fit_check(scan_path, model_path, clearance_mm=0.0, max_points=_MAX_FIT_POINTS):
    """Check a rebuilt/designed part (model file) against the scanned object
    (scan file), both mm and ALREADY in one frame (scan_align first if not):
    per-scan-vertex exact distance to the model surface, signed by
    inside/outside when the model is watertight. Reports collisions (scan
    vertices inside the model — the part would not seat), penetration depths
    and clearance percentiles; `clearance_mm` adds a fraction-below-target.
    This is the printed-part fit verification used before committing a print."""
    scan_mesh = _load(scan_path)
    model_mesh = _load(model_path)
    pts = np.asarray(scan_mesh.vertices, dtype=float)
    requested = len(pts)
    stride = max(1, -(-requested // int(max_points)))
    pts = pts[::stride]

    # Candidate faces from a dense surface sampling, then the exact
    # point-to-triangle distance (same scheme as deviation()).
    dst_pts, dst_fidx = _sample_surface(
        model_mesh, min(max(4 * len(pts), 2000), 24000), seed=1)
    _, idx = _nearest(pts, dst_pts)
    tris = model_mesh.triangles[dst_fidx[idx]]
    closest = trimesh.triangles.closest_point(tris, pts)
    dist = np.linalg.norm(pts - closest, axis=1)

    inside = None
    if model_mesh.is_watertight:
        # Inside/outside from the candidate face's outward normal (positive
        # dot = outside). No ray backend needed — mesh.contains() requires
        # rtree/embree, which target machines may not have. Near-surface
        # points at concave edges can misclassify by a hair; real seating
        # collisions have depth and classify robustly.
        normals = np.asarray(model_mesh.face_normals)[dst_fidx[idx]]
        inside = np.einsum('ij,ij->i', pts - closest, normals) < 0.0

    report = {
        'scan': scan_path,
        'model': model_path,
        'points': int(len(pts)),
        'points_stride': int(stride),
    }
    if inside is None:
        report['signed'] = False
        report['note'] = ('Model is not watertight — distances are unsigned, '
                          'collision count unavailable. Repair/re-export the '
                          'model for a full fit check.')
        clearance = dist
    else:
        report['signed'] = True
        collisions = int(inside.sum())
        report['collisions'] = collisions
        if collisions:
            depth = dist[inside]
            worst = np.argsort(depth)[::-1][:10]
            report['penetration_mm'] = {
                'max': _rounded(float(depth.max())),
                'p95': _rounded(float(np.percentile(depth, 95))),
            }
            report['worst_points_mm'] = _rounded(
                [list(p) for p in pts[inside][worst]], 2)
        clearance = dist[~inside] if collisions else dist
    if len(clearance):
        report['clearance_mm'] = {
            'min': _rounded(float(clearance.min())),
            'p5': _rounded(float(np.percentile(clearance, 5))),
            'p50': _rounded(float(np.percentile(clearance, 50))),
        }
        if clearance_mm:
            report['clearance_target_mm'] = float(clearance_mm)
            report['below_target'] = _rounded(
                float((clearance < clearance_mm).mean()))
    return report


# --------------------------------------------------------------------------- #
# cavity_sections — drop-on cavity outlines from a scan
# --------------------------------------------------------------------------- #
_MAX_CAVITY_SECTIONS = 40


def _hull2d(points):
    """Convex hull (Andrew monotone chain), CCW order. Pure Python."""
    pts = sorted({(round(float(x), 6), round(float(y), 6)) for x, y in points})
    if len(pts) <= 2:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def _offset_convex(poly, margin):
    """Offset a CCW convex polygon outward by `margin` (exact miter join)."""
    n = len(poly)
    if n < 3 or margin <= 0:
        return [list(p) for p in poly]

    def edge_normal(a, b):
        ex, ey = b[0] - a[0], b[1] - a[1]
        length = math.hypot(ex, ey) or 1.0
        return ey / length, -ex / length  # outward for CCW

    out = []
    for i in range(n):
        prv, cur, nxt = poly[i - 1], poly[i], poly[(i + 1) % n]
        n1, n2 = edge_normal(prv, cur), edge_normal(cur, nxt)
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        denom = 1.0 + (n1[0] * n2[0] + n1[1] * n2[1])
        if denom < 1e-9:  # degenerate spike — push along the mean normal
            length = math.hypot(bx, by) or 1.0
            out.append([cur[0] + margin * bx / length,
                        cur[1] + margin * by / length])
        else:
            out.append([cur[0] + margin * bx / denom,
                        cur[1] + margin * by / denom])
    return out


def _shoelace(poly):
    return 0.5 * abs(sum(
        poly[i][0] * poly[(i + 1) % len(poly)][1]
        - poly[(i + 1) % len(poly)][0] * poly[i][1]
        for i in range(len(poly))))


def _anchor_start(poly):
    """Rotate a polygon's start vertex to the max-x (tie: max-y) vertex so
    every section shares the loft anchor (prevents twisted lofts)."""
    if not poly:
        return poly
    start = max(range(len(poly)), key=lambda i: (poly[i][0], poly[i][1]))
    return poly[start:] + poly[:start]


def cavity_sections(scan_path, axis='z', spacing=2.0, margin=2.0,
                    cumulative='above', lookback=None, start=None, end=None,
                    max_sections=_MAX_CAVITY_SECTIONS):
    """Design a straight-insertion cavity around a scanned object: per-level
    CONVEX-HULL outlines that are cumulative along the insertion axis, offset
    outward by `margin` mm and guaranteed monotone (each section contains the
    previous one), so lofting them yields a cavity the object drops into.

    cumulative="above" (default) fits a cover lowered along -axis onto the
    object (each level swallows every scan point above it); "below" fits a
    pocket the object is inserted into from +axis. `lookback` (default
    = spacing) additionally swallows points one section behind the level —
    without it, lofts pinch between sections on steep zones. Convex hulls
    only: concave openings (grilles etc.) need the occupancy-raster approach.

    Rebuild flow per section: construction_plane(offset), one fitted
    sketch_spline through points_mm (NOT a polyline — polyline lofts bead),
    then loft; sections already share a start anchor and CCW orientation.
    Verify the result with scan_fit_check."""
    mesh = _load(scan_path)
    axis = (axis or 'z').lower()
    if axis not in _AXES:
        raise RuntimeError('axis must be x|y|z, got %r' % axis)
    ax, (u, v), sketch_plane, coord_names = _AXES[axis]
    cumulative = (cumulative or 'above').lower()
    if cumulative not in ('above', 'below'):
        raise RuntimeError("cumulative must be 'above' or 'below', got %r"
                           % cumulative)

    pts = np.asarray(mesh.vertices, dtype=float)
    # Scan clouds are dense, but converted/CAD meshes may have almost no
    # vertices on large faces (a cone's side has none at mid-height), which
    # would leave whole levels empty — densify with deterministic surface
    # samples so every level sees geometry.
    sampled, _ = _sample_surface(mesh, _SAMPLE_PRIMITIVES)
    pts = np.vstack([pts, sampled])
    coord = pts[:, ax]
    lo = float(coord.min()) if start is None else float(start)
    hi = float(coord.max()) if end is None else float(end)
    if hi <= lo:
        raise RuntimeError('Empty level range %.3f..%.3f' % (lo, hi))

    spacing = max(0.2, float(spacing))
    spacing_clamped = False
    count = int(math.floor((hi - lo) / spacing)) + 1
    if count > int(max_sections):
        spacing = (hi - lo) / (int(max_sections) - 1)
        count = int(max_sections)
        spacing_clamped = True
    levels = [lo + i * spacing for i in range(count)]
    if levels[-1] < hi - 1e-9:
        levels.append(hi)
    lookback = spacing if lookback is None else max(0.0, float(lookback))

    # Build from the contained (small) end so monotonicity is enforced by
    # unioning each hull with the previous one's vertices.
    build_order = list(reversed(levels)) if cumulative == 'above' else levels
    prev_hull = None
    by_level = {}
    for level in build_order:
        if cumulative == 'above':
            sel = pts[coord >= level - lookback]
        else:
            sel = pts[coord <= level + lookback]
        flat = [(p[u], p[v]) for p in sel]
        if prev_hull:
            flat.extend((p[0], p[1]) for p in prev_hull)
        hull = _hull2d(flat)
        if len(hull) < 3:
            by_level[level] = None
            continue
        prev_hull = hull
        by_level[level] = hull

    sections = []
    for level in levels:
        hull = by_level.get(level)
        if not hull:
            sections.append({'level_mm': _rounded(level),
                             'error': 'fewer than 3 scan points at this level'})
            continue
        poly = _anchor_start(_offset_convex(hull, float(margin)))
        sections.append({
            'level_mm': _rounded(level),
            'points_mm': _rounded(poly),
            'area_mm2': _rounded(_shoelace(poly), 2),
        })

    return {
        'file': scan_path,
        'axis': axis,
        'sketch_plane': sketch_plane,
        'coords': list(coord_names),
        'cumulative': cumulative,
        'spacing_mm': _rounded(spacing),
        'spacing_clamped': spacing_clamped,
        'margin_mm': float(margin),
        'lookback_mm': _rounded(lookback),
        'note': ('For each section: construction_plane(method="offset", '
                 'base="%s", offset=level_mm), one closed fitted spline '
                 'through points_mm (sections already share start anchor + '
                 'CCW), then loft through all profiles. Monotone sections '
                 'guarantee straight insertion along %s. Verify with '
                 'scan_fit_check.' % (sketch_plane, axis)),
        'sections': sections,
    }


# --------------------------------------------------------------------------- #
# convert — phone-scan formats (GLB/PLY/OFF/OBJ) -> Fusion-importable mesh
# --------------------------------------------------------------------------- #
def convert(path, out_path=None, fmt='stl'):
    """Convert a mesh file Fusion cannot import (GLB/GLTF/PLY/OFF — typical
    phone-scanner exports) into STL or OBJ for import_mesh. Scene files are
    flattened into one mesh. Returns the output path plus size stats and a
    units warning when the extents look non-millimetre."""
    mesh = _load(path)
    fmt = (fmt or 'stl').lower()
    if fmt not in ('stl', 'obj'):
        raise RuntimeError("fmt must be 'stl' or 'obj', got %r" % fmt)
    if not out_path:
        out_path = os.path.splitext(path)[0] + '.' + fmt
    mesh.export(out_path)
    bounds = mesh.bounds
    size = bounds[1] - bounds[0]
    scale = float(size.max())
    report = {
        'input': path,
        'output': out_path,
        'format': fmt,
        'triangles': int(len(mesh.faces)),
        'vertices': int(len(mesh.vertices)),
        'watertight': bool(mesh.is_watertight),
        'size_mm': _rounded(size),
        'note': "Import with import_mesh(path, units='mm').",
    }
    if scale < 5 or scale > 5000:
        report['units_warning'] = (
            'Largest extent is %.3f mm — the file may not be in millimetres '
            '(GLB is metres by convention). Import with the matching units, '
            'e.g. import_mesh(units="m").' % scale)
    return report
