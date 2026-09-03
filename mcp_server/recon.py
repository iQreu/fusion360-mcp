"""Scan reconstruction — from a mesh to FEATURES, not splines (v1.16).

Server-side (no Fusion round trip), built on the same optional "re" stack as
scan.py (numpy + trimesh; scikit-image only for mesh_offset's marching
cubes). Everything is deterministic: no numpy.random, no RANSAC lotteries —
region growing, eigen-analysis and least squares.

Public functions (all paths are mesh files in MILLIMETRES):

* frame()        — datum alignment WITHOUT a CAD model: largest plane -> Z=0,
                   dominant direction -> X, writes the re-framed STL.
* segment()      — region growing into planar / cylindrical / spherical /
                   freeform patches with an adjacency graph.
* features()     — holes (Ø, depth, through/blind), cutouts, bosses, rounds
                   (fillet radii), hole patterns (pitch, rectangle, PCD) and
                   plate thicknesses — a measurement sheet off the scan.
* profile()      — one planar section turned into LINES + ARCS (corner
                   detection, greedy arc merging, angle/radius snapping) ready
                   to become a constrained Fusion sketch.
* thread_identify() — pitch/handedness/major-minor of a scanned thread by a
                   folded-phase periodogram, snapped to ISO 261 / UNC / UNF.
* mesh_offset()  — voxel dilation of a scan by +d mm (modes: offset |
                   monotone drop-on cavity), the cavity cutter you import and
                   combine-cut.
* fit_report()   — one PASS/WARN/FAIL sheet: fit_check + print_check + dfm
                   (+ wall thickness when a ray backend exists).
"""
import math
import os

import scan
from scan import _assemble_loops, _fit_circle2d, _load, _plane_basis, _rounded, _sample_surface

np = scan.np
trimesh = scan.trimesh

# --------------------------------------------------------------------------- #
# shared small helpers
# --------------------------------------------------------------------------- #
_MAX_SEG_FACES = 400000


def _unit(vec):
    vec = np.asarray(vec, dtype=float)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def _fit_plane(points, weights=None):
    """Weighted least-squares plane -> (centroid, unit normal, rms)."""
    pts = np.asarray(points, dtype=float)
    if weights is None:
        weights = np.ones(len(pts))
    weights = np.asarray(weights, dtype=float)
    centroid = (pts * weights[:, None]).sum(axis=0) / weights.sum()
    rel = pts - centroid
    cov = (rel * weights[:, None]).T @ rel
    normal = np.linalg.eigh(cov)[1][:, 0]
    rms = float(np.sqrt(np.mean((rel @ normal) ** 2)))
    return centroid, _unit(normal), rms


def _fit_sphere(points):
    """Linear least-squares sphere -> (centre, radius, rms) or None."""
    pts = np.asarray(points, dtype=float)
    a_mat = np.column_stack([2.0 * pts, np.ones(len(pts))])
    try:
        sol, *_ = np.linalg.lstsq(a_mat, (pts ** 2).sum(axis=1), rcond=None)
    except np.linalg.LinAlgError:
        return None
    centre, k = sol[:3], float(sol[3])
    r_sq = k + float(centre @ centre)
    if r_sq <= 0:
        return None
    radius = math.sqrt(r_sq)
    rms = float(np.sqrt(np.mean(
        (np.linalg.norm(pts - centre, axis=1) - radius) ** 2)))
    return centre, radius, rms


def _csr(adjacency, count, values=None):
    """Face adjacency pairs -> (offsets, targets[, values]) for O(1)
    neighbour lookup; `values` (one per pair, e.g. dihedral angles) is
    expanded alongside the targets."""
    if len(adjacency) == 0:
        empty = np.zeros(0, dtype=int)
        if values is None:
            return np.zeros(count + 1, dtype=int), empty
        return np.zeros(count + 1, dtype=int), empty, np.zeros(0)
    src = np.concatenate([adjacency[:, 0], adjacency[:, 1]])
    dst = np.concatenate([adjacency[:, 1], adjacency[:, 0]])
    order = np.argsort(src, kind='stable')
    src, dst = src[order], dst[order]
    offsets = np.searchsorted(src, np.arange(count + 1))
    if values is None:
        return offsets, dst
    vals = np.concatenate([values, values])[order]
    return offsets, dst, vals


# --------------------------------------------------------------------------- #
# segment — region growing into primitive patches
# --------------------------------------------------------------------------- #
def _grow_planar(mesh, offsets, targets, angle_deg, dist_tol, min_faces,
                 min_area=0.0, edge_angles=None, smooth_deg=30.0):
    """Phase 1: planar regions. A face joins a region when its normal is
    within angle_deg of the region's running mean normal AND its centroid
    lies within dist_tol of the region's running plane — the distance test
    stops a region leaking across a thin step onto a parallel plane.
    A SMALL region (< min_faces) survives only if it is big enough by area
    AND bounded by sharp creases: a coarse CAD tessellation makes every
    cylinder facet a 2-triangle "plane", but those are joined to their
    neighbours smoothly and belong to phase 2."""
    normals = np.asarray(mesh.face_normals, dtype=float)
    centres = np.asarray(mesh.triangles_center, dtype=float)
    areas = np.asarray(mesh.area_faces, dtype=float)
    n_faces = len(normals)
    labels = -np.ones(n_faces, dtype=int)
    cos_thr = math.cos(math.radians(angle_deg))

    # Seed order: flattest neighbourhoods first (min dihedral angle to
    # neighbours) so regions start in the interior of a plane, not on an edge.
    angles = np.asarray(mesh.face_adjacency_angles, dtype=float)
    adj = np.asarray(mesh.face_adjacency)
    local = np.zeros(n_faces)
    if len(adj):
        np.maximum.at(local, adj[:, 0], angles)
        np.maximum.at(local, adj[:, 1], angles)
    order = np.lexsort((-areas, local))

    next_label = 0
    for seed in order:
        if labels[seed] >= 0:
            continue
        region = [int(seed)]
        labels[seed] = next_label
        sum_n = normals[seed] * areas[seed]
        sum_c = centres[seed] * areas[seed]
        sum_a = areas[seed]
        head = 0
        while head < len(region):
            f = region[head]
            head += 1
            mean_n = sum_n / (np.linalg.norm(sum_n) or 1.0)
            mean_c = sum_c / sum_a
            for g in targets[offsets[f]:offsets[f + 1]]:
                if labels[g] >= 0:
                    continue
                if float(normals[g] @ mean_n) < cos_thr:
                    continue
                if abs(float((centres[g] - mean_c) @ mean_n)) > dist_tol:
                    continue
                labels[g] = next_label
                region.append(int(g))
                sum_n = sum_n + normals[g] * areas[g]
                sum_c = sum_c + centres[g] * areas[g]
                sum_a += areas[g]
        # Keep a region by face count OR by area: a CAD-export STL has whole
        # planes made of 2 triangles, a scan has thousands of tiny ones.
        keep = len(region) >= min_faces or sum_a >= min_area
        if keep and len(region) < min_faces and edge_angles is not None:
            smooth_thr = math.radians(smooth_deg)
            in_region = set(region)
            for f in region:
                for g, ang in zip(targets[offsets[f]:offsets[f + 1]],
                                  edge_angles[offsets[f]:offsets[f + 1]], strict=False):
                    if int(g) not in in_region and ang < smooth_thr:
                        keep = False      # smooth border: a facet of a curved surface
                        break
                if not keep:
                    break
        if not keep:
            labels[region] = -1
        else:
            next_label += 1
    return labels, next_label


def _grow_smooth(mesh, offsets, targets, labels, start_label, smooth_deg,
                 min_faces, min_area=0.0):
    """Phase 2: the faces no plane claimed, grouped by smoothness (dihedral
    angle between neighbours below smooth_deg) — cylinders, spheres, blends,
    freeform. Sharp creases separate regions; labelled planes are walls."""
    adj = np.asarray(mesh.face_adjacency)
    angles = np.asarray(mesh.face_adjacency_angles, dtype=float)
    thr = math.radians(smooth_deg)
    # Smooth-edge lookup keyed by the sorted face pair.
    smooth = {}
    for (a, b), ang in zip(adj.tolist(), angles.tolist(), strict=False):
        smooth[(a, b)] = ang < thr
        smooth[(b, a)] = ang < thr
    areas = np.asarray(mesh.area_faces, dtype=float)
    next_label = start_label
    for seed in np.where(labels < 0)[0]:
        if labels[seed] >= 0:
            continue
        region = [int(seed)]
        labels[seed] = next_label
        head = 0
        while head < len(region):
            f = region[head]
            head += 1
            for g in targets[offsets[f]:offsets[f + 1]]:
                if labels[g] >= 0 or not smooth.get((f, int(g)), False):
                    continue
                labels[g] = next_label
                region.append(int(g))
        if len(region) < min_faces and float(areas[region].sum()) < min_area:
            labels[region] = -2   # -2: examined, too small (never re-seeded)
        else:
            next_label += 1
    labels[labels == -2] = -1
    return labels, next_label


def _classify_patch(mesh, face_ids, planar):
    """Fit a primitive to one patch -> dict (kind + parameters, mm)."""
    normals = np.asarray(mesh.face_normals)[face_ids]
    areas = np.asarray(mesh.area_faces)[face_ids]
    centres = np.asarray(mesh.triangles_center)[face_ids]
    verts = np.asarray(mesh.vertices)[np.unique(np.asarray(mesh.faces)[face_ids])]
    area = float(areas.sum())
    bbox = [verts.min(axis=0), verts.max(axis=0)]
    out = {'faces': int(len(face_ids)), 'area_mm2': _rounded(area, 2),
           'bbox_min_mm': _rounded(bbox[0], 2), 'bbox_max_mm': _rounded(bbox[1], 2)}
    if planar:
        centroid, normal, rms = _fit_plane(centres, areas)
        mean_n = _unit((normals * areas[:, None]).sum(axis=0))
        if float(normal @ mean_n) < 0:
            normal = -normal
        out.update({'kind': 'plane', 'normal': _rounded(normal),
                    'point_mm': _rounded(centroid, 3), 'rms_mm': _rounded(rms, 3)})
        return out

    weighted = normals * np.sqrt(areas)[:, None]
    evals, evecs = np.linalg.eigh(weighted.T @ weighted)
    total = float(evals.sum()) or 1.0
    flatness = float(evals[0]) / total          # ~0 -> normals in a plane
    if flatness < 0.03 and len(face_ids) >= 6:
        axis = _unit(evecs[:, 0])
        u_vec, v_vec = _plane_basis(axis)
        rel = verts - verts.mean(axis=0)
        fit = _fit_circle2d(np.column_stack([rel @ u_vec, rel @ v_vec]))
        if fit is not None:
            cx, cy, radius = fit
            centre = verts.mean(axis=0) + cx * u_vec + cy * v_vec
            radial = rel - np.outer(rel @ axis, axis) - (cx * u_vec + cy * v_vec)
            rms = float(np.sqrt(np.mean(
                (np.linalg.norm(radial, axis=1) - radius) ** 2)))
            if rms < max(0.15, 0.05 * radius):
                along = (verts - centre) @ axis
                lo, hi = float(along.min()), float(along.max())
                c_rel = centres - centre
                c_rad = c_rel - np.outer(c_rel @ axis, axis)
                c_rad_n = c_rad / (np.linalg.norm(c_rad, axis=1)[:, None] + 1e-12)
                convex = float(np.einsum('ij,ij->i', normals, c_rad_n)
                               @ areas) / (areas.sum() or 1.0)
                theta = np.arctan2(c_rad @ v_vec, c_rad @ u_vec)
                # Angular coverage from a 36-bin histogram of centroid angles.
                bins = np.unique(np.floor((theta + math.pi) / (2 * math.pi) * 36)
                                 .astype(int) % 36)
                out.update({
                    'kind': 'cylinder',
                    'axis': _rounded(axis),
                    'point_mm': _rounded(centre + axis * (lo + hi) / 2.0, 3),
                    'radius_mm': _rounded(radius, 3),
                    'diameter_mm': _rounded(2 * radius, 3),
                    'length_mm': _rounded(hi - lo, 3),
                    'end_a_mm': _rounded(centre + axis * lo, 3),
                    'end_b_mm': _rounded(centre + axis * hi, 3),
                    'convex': bool(convex > 0),
                    'coverage': _rounded(len(bins) / 36.0, 2),
                    'rms_mm': _rounded(rms, 3),
                })
                return out
    sphere = _fit_sphere(verts) if len(verts) >= 12 else None
    if sphere is not None:
        centre, radius, rms = sphere
        if rms < max(0.15, 0.03 * radius) and flatness > 0.1:
            outward = (centres - centre)
            outward = outward / (np.linalg.norm(outward, axis=1)[:, None] + 1e-12)
            convex = float(np.einsum('ij,ij->i', normals, outward) @ areas) \
                / (areas.sum() or 1.0)
            out.update({'kind': 'sphere', 'center_mm': _rounded(centre, 3),
                        'radius_mm': _rounded(radius, 3), 'convex': bool(convex > 0),
                        'rms_mm': _rounded(rms, 3)})
            return out
    out.update({'kind': 'freeform', 'normal_spread': _rounded(flatness, 3),
                'mean_normal': _rounded(_unit((normals * areas[:, None]).sum(axis=0)))})
    return out


def _segment_mesh(mesh, angle_deg=10.0, dist_tol=0.5, smooth_deg=30.0,
                  min_faces=20):
    """Labels per face (-1 = unassigned) plus classified patches and their
    adjacency. Internal: also returns face-id lists per patch."""
    n_faces = len(mesh.faces)
    if n_faces > _MAX_SEG_FACES:
        raise RuntimeError('Mesh has %d triangles — segment handles up to %d. '
                           'Decimate first (mesh_reduce in Fusion, or '
                           'scan_convert after a reduce).' % (n_faces, _MAX_SEG_FACES))
    adj = np.asarray(mesh.face_adjacency)
    offsets, targets, edge_angles = _csr(
        adj, n_faces, np.asarray(mesh.face_adjacency_angles, dtype=float))
    min_area = 0.002 * float(mesh.area)
    labels, n_planar = _grow_planar(mesh, offsets, targets, angle_deg,
                                    dist_tol, min_faces, min_area, edge_angles,
                                    smooth_deg)
    labels, n_total = _grow_smooth(mesh, offsets, targets, labels, n_planar,
                                   smooth_deg, min_faces, min_area)
    patches = []
    face_lists = []
    for lab in range(n_total):
        ids = np.where(labels == lab)[0]
        entry = _classify_patch(mesh, ids, planar=lab < n_planar)
        entry['id'] = lab
        patches.append(entry)
        face_lists.append(ids)
    # Patch adjacency (which patches share an edge).
    neighbours = {lab: set() for lab in range(n_total)}
    if len(adj):
        la, lb = labels[adj[:, 0]], labels[adj[:, 1]]
        mask = (la != lb) & (la >= 0) & (lb >= 0)
        for a, b in zip(la[mask].tolist(), lb[mask].tolist(), strict=False):
            neighbours[a].add(b)
            neighbours[b].add(a)
    for entry in patches:
        entry['adjacent'] = sorted(neighbours[entry['id']])
    return labels, patches, face_lists


def segment(path, angle_deg=10.0, dist_tol=0.5, smooth_deg=30.0,
            min_faces=20, max_patches=40, out_path=None):
    """Segment a scan into primitive patches (plane / cylinder / sphere /
    freeform) by deterministic region growing. Optional out_path writes a
    PLY with one colour per patch for eyeballing the split."""
    mesh = _load(path)
    labels, patches, _face_lists = _segment_mesh(
        mesh, angle_deg=angle_deg, dist_tol=dist_tol, smooth_deg=smooth_deg,
        min_faces=min_faces)
    total_area = float(mesh.area) or 1.0
    unassigned = float(np.asarray(mesh.area_faces)[labels < 0].sum())
    patches_sorted = sorted(patches, key=lambda e: -e['area_mm2'])
    kinds = {}
    for entry in patches:
        kinds[entry['kind']] = kinds.get(entry['kind'], 0) + 1
    report = {
        'file': path,
        'triangles': int(len(mesh.faces)),
        'patch_count': len(patches),
        'kinds': kinds,
        'unassigned_area_fraction': _rounded(unassigned / total_area, 3),
        'patches': patches_sorted[:max_patches],
        'note': ('Patch ids are stable for this file+parameters and are the '
                 'ids scan_features refers to. plane.normal points OUT of the '
                 'material; cylinder.convex=true is a boss/shaft, false a '
                 'hole/bore.'),
    }
    if len(patches) > max_patches:
        report['truncated_to'] = max_patches
    if out_path:
        colours = np.zeros((len(mesh.faces), 4), dtype=np.uint8)
        colours[:, 3] = 255
        colours[:, :3] = 90
        palette = np.array([[230, 25, 75], [60, 180, 75], [255, 225, 25],
                            [0, 130, 200], [245, 130, 48], [145, 30, 180],
                            [70, 240, 240], [240, 50, 230], [210, 245, 60],
                            [250, 190, 212], [0, 128, 128], [220, 190, 255],
                            [170, 110, 40], [255, 250, 200], [128, 0, 0],
                            [170, 255, 195]], dtype=np.uint8)
        assigned = labels >= 0
        colours[assigned, :3] = palette[labels[assigned] % len(palette)]
        painted = mesh.copy()
        painted.visual.face_colors = colours
        painted.export(out_path)
        report['out_path'] = out_path
    return report


# --------------------------------------------------------------------------- #
# features — holes, cutouts, bosses, rounds, patterns, thicknesses
# --------------------------------------------------------------------------- #
def _patch_boundary_loops(mesh, labels, lab):
    """Ordered boundary loops (3D points) of the faces labelled `lab`: edges
    shared with another label plus open mesh-boundary edges."""
    adj = np.asarray(mesh.face_adjacency)
    adj_edges = np.asarray(mesh.face_adjacency_edges)
    verts = np.asarray(mesh.vertices)
    segs = []
    if len(adj):
        in_a = labels[adj[:, 0]] == lab
        in_b = labels[adj[:, 1]] == lab
        for e in adj_edges[in_a != in_b]:
            segs.append((tuple(verts[e[0]]), tuple(verts[e[1]])))
    # Open boundary edges (non-watertight scans) that belong to this patch.
    try:
        edges = np.asarray(mesh.edges_sorted)
        groups = trimesh.grouping.group_rows(edges, require_count=1)
        for idx in np.asarray(groups).ravel():
            if labels[idx // 3] == lab:
                e = edges[idx]
                segs.append((tuple(verts[e[0]]), tuple(verts[e[1]])))
    except Exception:  # noqa: BLE001 - optional refinement
        pass
    return _assemble_loops(segs, tol=1e-3)


def _line_point_distance(point, origin, direction):
    rel = np.asarray(point) - origin
    return float(np.linalg.norm(rel - (rel @ direction) * direction))


def _hole_patterns(holes):
    """Spacings and recognised layouts among holes on one plane with similar
    diameters."""
    out = []
    by_plane = {}
    for h in holes:
        by_plane.setdefault(h['plane'], []).append(h)
    for plane, group in by_plane.items():
        # Cluster by diameter (0.4 mm).
        group = sorted(group, key=lambda h: h['diameter_mm'])
        clusters, cur = [], [group[0]]
        for h in group[1:]:
            if h['diameter_mm'] - cur[-1]['diameter_mm'] <= 0.4:
                cur.append(h)
            else:
                clusters.append(cur)
                cur = [h]
        clusters.append(cur)
        for cluster in clusters:
            if len(cluster) < 2 or len(cluster) > 12:
                continue
            centres = np.array([h['center_mm'] for h in cluster], dtype=float)
            ids = [h['id'] for h in cluster]
            pairs = []
            for i in range(len(cluster)):
                for j in range(i + 1, len(cluster)):
                    pairs.append({'holes': [ids[i], ids[j]],
                                  'spacing_mm': _rounded(float(np.linalg.norm(
                                      centres[i] - centres[j])), 2)})
            pairs.sort(key=lambda d: d['spacing_mm'])
            entry = {'plane': plane, 'holes': ids,
                     'diameter_mm': _rounded(float(np.mean(
                         [h['diameter_mm'] for h in cluster])), 2),
                     'spacings': pairs[:30]}
            n = len(cluster)
            rect = None
            if n == 4:
                d_sorted = sorted(p['spacing_mm'] for p in pairs)
                a, b = d_sorted[0], d_sorted[2]
                diag = math.hypot(a, b)
                if (abs(d_sorted[1] - a) < 0.3 and abs(d_sorted[3] - b) < 0.3
                        and abs(d_sorted[4] - diag) < 0.4 and abs(d_sorted[5] - diag) < 0.4):
                    rect = (a, b)
            # Concyclic centres (PCD). Every rectangle is concyclic, so a
            # "circular" layout additionally needs (near-)equal angular gaps
            # or 5+ holes.
            circ = None
            if n >= 3:
                _c0, normal, _rms = _fit_plane(centres)
                u_vec, v_vec = _plane_basis(normal)
                rel = centres - centres.mean(axis=0)
                xy = np.column_stack([rel @ u_vec, rel @ v_vec])
                fit = _fit_circle2d(xy)
                if fit is not None:
                    cx, cy, r = fit
                    d = np.linalg.norm(xy - (cx, cy), axis=1)
                    if r > 1.0 and float(np.abs(d - r).max()) < max(0.3, 0.02 * r):
                        ang = np.sort(np.arctan2(xy[:, 1] - cy, xy[:, 0] - cx))
                        gaps = np.diff(np.concatenate([ang, [ang[0] + 2 * math.pi]]))
                        circ = {'pcd_mm': _rounded(2 * r, 2),
                                'pattern_center_mm': _rounded(
                                    centres.mean(axis=0) + cx * u_vec + cy * v_vec, 2),
                                'angular_spacing_deg': _rounded(
                                    [math.degrees(g) for g in gaps], 1),
                                'equal': bool(gaps.max() / max(gaps.min(), 1e-9) < 1.2)}
            if rect:
                entry['layout'] = 'rectangle'
                entry['sides_mm'] = [_rounded(rect[0], 2), _rounded(rect[1], 2)]
                if circ:
                    entry['pcd_mm'] = circ['pcd_mm']
                    entry['pattern_center_mm'] = circ['pattern_center_mm']
            elif circ and (n >= 5 or circ['equal']):
                entry['layout'] = 'circular'
                entry.update({k: v for k, v in circ.items() if k != 'equal'})
            elif n >= 3:
                rel = centres - centres.mean(axis=0)
                direction = np.linalg.eigh(rel.T @ rel)[1][:, -1]
                off = np.linalg.norm(rel - np.outer(rel @ direction, direction), axis=1)
                if float(off.max()) < 0.3:
                    t = np.sort(rel @ direction)
                    entry['layout'] = 'linear'
                    entry['pitch_mm'] = _rounded([float(x) for x in np.diff(t)], 2)
            if 'layout' not in entry:
                entry['layout'] = 'pair' if n == 2 else 'irregular'
            out.append(entry)
    return out


def features(path, angle_deg=10.0, dist_tol=0.5, min_faces=20,
             max_features=60):
    """Measurement sheet from a scan: holes (centre, Ø, depth, through/blind),
    non-circular cutouts, bosses, rounds/fillets, hole patterns and plate
    thicknesses. Built on segment(); ids reference its patches."""
    mesh = _load(path)
    labels, patches, face_lists = _segment_mesh(
        mesh, angle_deg=angle_deg, dist_tol=dist_tol, min_faces=min_faces)
    by_id = {p['id']: p for p in patches}
    planes = [p for p in patches if p['kind'] == 'plane']
    cylinders = [p for p in patches if p['kind'] == 'cylinder']
    holes, cutouts = [], []

    # --- holes and cutouts: inner boundary loops of planar patches -------- #
    for plane in planes:
        loops = _patch_boundary_loops(mesh, labels, plane['id'])
        # >= 3 points: a CAD-export plate's outer boundary can be a 4-point
        # rectangle — dropping it would promote a hole to "outer".
        closed = [pts for pts, is_closed in loops if is_closed and len(pts) >= 3]
        if len(closed) < 2:
            continue
        normal = np.asarray(plane['normal'], dtype=float)
        u_vec, v_vec = _plane_basis(normal)
        origin = np.asarray(plane['point_mm'], dtype=float)

        def flat(pts, origin=origin, u_vec=u_vec, v_vec=v_vec):
            rel = np.asarray(pts, dtype=float) - origin
            return np.column_stack([rel @ u_vec, rel @ v_vec])
        areas = [scan._shoelace(flat(pts).tolist()) for pts in closed]
        outer = int(np.argmax(areas))
        for i, pts in enumerate(closed):
            if i == outer:
                continue
            xy = flat(pts)
            fit = _fit_circle2d(xy) if len(pts) >= 6 else None
            if fit is not None:
                cx, cy, r = fit
                resid = np.abs(np.linalg.norm(xy - (cx, cy), axis=1) - r)
                if r >= 0.4 and float(resid.max()) < max(0.15, 0.06 * r):
                    centre = origin + cx * u_vec + cy * v_vec
                    holes.append({'id': 'H%d' % (len(holes) + 1),
                                  'kind': 'hole', 'plane': plane['id'],
                                  'center_mm': _rounded(centre, 3),
                                  'diameter_mm': _rounded(2 * r, 3),
                                  'circularity_mm': _rounded(float(resid.max()), 3),
                                  'axis': _rounded(normal),
                                  '_centre': centre, '_r': r})
                    continue
            lo, hi = xy.min(axis=0), xy.max(axis=0)
            cutouts.append({'id': 'C%d' % (len(cutouts) + 1), 'kind': 'cutout',
                            'plane': plane['id'],
                            'center_mm': _rounded(origin + ((lo + hi) / 2)[0] * u_vec
                                                  + ((lo + hi) / 2)[1] * v_vec, 3),
                            'size_mm': _rounded((hi - lo).tolist(), 2),
                            'area_mm2': _rounded(float(areas[i]), 2),
                            'points': int(len(pts))})

    # --- depth / through via the matching cylinder wall ------------------- #
    # A circular inner loop whose matching cylinder is CONVEX is the base
    # ring of a boss standing on the plane, not a hole — drop it.
    boss_rings = []
    for hole in holes:
        best = None
        for cyl in cylinders:
            axis = np.asarray(cyl['axis'], dtype=float)
            if abs(float(axis @ np.asarray(hole['axis']))) < 0.9:
                continue
            if abs(cyl['radius_mm'] - hole['_r']) > max(0.25, 0.12 * hole['_r']):
                continue
            off = _line_point_distance(hole['_centre'],
                                       np.asarray(cyl['point_mm'], dtype=float), axis)
            if off > 0.3 * hole['_r'] + 0.3:
                continue
            if best is None or off < best[0]:
                best = (off, cyl)
        if best is None:
            hole['depth_mm'] = None
            hole['through'] = None
            hole['note'] = 'no cylindrical wall found for this opening'
            continue
        cyl = best[1]
        if cyl['convex']:
            boss_rings.append(hole)
            continue
        hole['wall_patch'] = cyl['id']
        hole['depth_mm'] = cyl['length_mm']
        hole['_wall'] = cyl['id']
    for ring in boss_rings:
        holes.remove(ring)
    for k, hole in enumerate(holes):
        hole['id'] = 'H%d' % (k + 1)
    # Through: the same wall patch is an opening on two different planes.
    walls = {}
    for hole in holes:
        if hole.get('_wall') is not None:
            walls.setdefault(hole['_wall'], []).append(hole)
    for wall_holes in walls.values():
        planes_hit = {h['plane'] for h in wall_holes}
        through = len(planes_hit) >= 2
        for h in wall_holes:
            h['through'] = through
            if not through:
                cyl = by_id[h['_wall']]
                # Blind: a small plane (the bottom) adjacent to the wall,
                # facing along the axis.
                cyl_axis = np.asarray(cyl['axis'], dtype=float)
                bottom = [by_id[a] for a in cyl['adjacent']
                          if by_id[a]['kind'] == 'plane' and a != h['plane']
                          and abs(float(np.asarray(by_id[a]['normal']) @ cyl_axis)) > 0.9]
                h['bottom'] = 'flat' if bottom else 'unknown'
    for hole in holes:
        for key in ('_centre', '_r', '_wall'):
            hole.pop(key, None)

    # --- bosses and rounds ------------------------------------------------- #
    bosses, rounds = [], []
    for cyl in cylinders:
        if cyl['coverage'] >= 0.6 and cyl['convex']:
            bosses.append({'id': 'B%d' % (len(bosses) + 1), 'kind': 'boss',
                           'patch': cyl['id'], 'diameter_mm': cyl['diameter_mm'],
                           'height_mm': cyl['length_mm'], 'axis': cyl['axis'],
                           'end_a_mm': cyl['end_a_mm'], 'end_b_mm': cyl['end_b_mm']})
        elif cyl['coverage'] < 0.6:
            adj_planes = [a for a in cyl['adjacent'] if by_id[a]['kind'] == 'plane']
            rounds.append({'id': 'R%d' % (len(rounds) + 1),
                           'kind': 'fillet' if cyl['convex'] else 'inside_fillet',
                           'patch': cyl['id'], 'radius_mm': cyl['radius_mm'],
                           'length_mm': cyl['length_mm'],
                           'sweep_deg': _rounded(360.0 * cyl['coverage'], 0),
                           'between_planes': adj_planes[:2]})

    # --- plate thicknesses: antiparallel plane pairs ----------------------- #
    thicknesses = []
    for i, pa in enumerate(planes):
        na = np.asarray(pa['normal'], dtype=float)
        for pb in planes[i + 1:]:
            nb = np.asarray(pb['normal'], dtype=float)
            if float(na @ nb) > -0.98:
                continue
            dist = abs(float((np.asarray(pb['point_mm']) - np.asarray(pa['point_mm'])) @ na))
            # Footprints must overlap: the smaller centroid projects inside
            # the larger patch's bbox (in-plane).
            small, large = (pa, pb) if pa['area_mm2'] < pb['area_mm2'] else (pb, pa)
            lo = np.asarray(large['bbox_min_mm']) - 1.0
            hi = np.asarray(large['bbox_max_mm']) + 1.0
            c = np.asarray(small['point_mm'])
            inside = all(lo[k] <= c[k] <= hi[k] or abs(na[k]) > 0.9 for k in range(3))
            if inside and dist > 0.2:
                thicknesses.append({'kind': 'thickness', 'planes': [pa['id'], pb['id']],
                                    'thickness_mm': _rounded(dist, 3),
                                    'area_mm2': _rounded(min(pa['area_mm2'], pb['area_mm2']), 1)})
    thicknesses.sort(key=lambda t: -t['area_mm2'])

    size = mesh.bounds[1] - mesh.bounds[0]
    report = {
        'file': path,
        'size_mm': _rounded(size, 2),
        'patches': {'planes': len(planes), 'cylinders': len(cylinders),
                    'total': len(patches)},
        'holes': holes[:max_features],
        'cutouts': cutouts[:max_features],
        'bosses': bosses[:max_features],
        'rounds': rounds[:max_features],
        'patterns': _hole_patterns(holes),
        'thicknesses': thicknesses[:6],
        'note': ('Scan coordinates, mm. Shiny/black surfaces scan 0.2-0.5 mm '
                 'undersize on holes and 2-3 mm undersize on dark glossy '
                 'pipes — snap diameters with fit_suggest/hole_spec, not raw. '
                 'plane ids = scan_segment patch ids.'),
    }
    return report


# --------------------------------------------------------------------------- #
# profile — a section as LINES + ARCS
# --------------------------------------------------------------------------- #
def _douglas_peucker(pts, tol):
    """Indices kept by Douglas-Peucker on an OPEN polyline (numpy Nx2)."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = pts[b] - pts[a]
        length = float(np.linalg.norm(seg))
        rel = pts[a + 1:b] - pts[a]
        if length < 1e-12:
            dist = np.linalg.norm(rel, axis=1)
        else:
            dist = np.abs(rel[:, 0] * seg[1] - rel[:, 1] * seg[0]) / length
        i = int(np.argmax(dist))
        if dist[i] > tol:
            idx = a + 1 + i
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [int(i) for i in np.where(keep)[0]]


def _arc_fit(pts):
    """Circle through pts (Nx2) -> (cx, cy, r, max_residual, sweep_deg, ccw)
    or None. sweep follows the point order from first to last."""
    fit = _fit_circle2d(pts)
    if fit is None:
        return None
    cx, cy, r = fit
    resid = float(np.abs(np.linalg.norm(pts - (cx, cy), axis=1) - r).max())
    ang = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
    steps = np.diff(ang)
    steps = (steps + math.pi) % (2 * math.pi) - math.pi
    sweep = float(steps.sum())
    if len(steps) and not (np.all(steps >= -1e-6) or np.all(steps <= 1e-6)):
        # Direction reversals: not a monotone arc (e.g. two lines).
        return None
    return cx, cy, r, resid, math.degrees(abs(sweep)), sweep > 0


def _segment_polyline(pts, closed, tol):
    """Split a polyline into line and arc primitives. Returns a list of dicts
    with 'kind', index span (i, j into pts) and fitted parameters."""
    n = len(pts)
    if closed:
        # Rotate so index 0 is a sharp corner (max deviation from the chord to
        # its far point) — DP needs a genuine break point on closed loops.
        far = int(np.argmax(np.linalg.norm(pts - pts[0], axis=1)))
        first = _douglas_peucker(pts[:far + 1], tol)
        second = _douglas_peucker(np.vstack([pts[far:], pts[:1]]), tol)
        verts = first + [(far + k) % n for k in second[1:-1]]
        # Index 0 and `far` were forced in by the split; drop them (or any
        # vertex) when collinear with their neighbours within tol — a
        # mid-edge vertex from a triangulated side face is not a corner.
        changed = True
        while changed and len(verts) > 3:
            changed = False
            for k in range(len(verts)):
                a = pts[verts[k - 1]]
                b = pts[verts[k]]
                c = pts[verts[(k + 1) % len(verts)]]
                seg = c - a
                length = float(np.linalg.norm(seg))
                if length < 1e-12:
                    continue
                dist = abs(float((b - a)[0] * seg[1] - (b - a)[1] * seg[0])) / length
                if dist <= tol:
                    del verts[k]
                    changed = True
                    break
        # Start the chord cycle at the sharpest corner so a smooth run (an
        # arc) is never split by the arbitrary loop start.
        if len(verts) >= 3:
            turns = []
            for k in range(len(verts)):
                a = pts[verts[k - 1]]
                b = pts[verts[k]]
                c = pts[verts[(k + 1) % len(verts)]]
                d1, d2 = b - a, c - b
                n1, n2 = np.linalg.norm(d1), np.linalg.norm(d2)
                cosang = float(d1 @ d2) / (n1 * n2) if n1 > 0 and n2 > 0 else 1.0
                turns.append(math.acos(max(-1.0, min(1.0, cosang))))
            start = int(np.argmax(turns))
            verts = verts[start:] + verts[:start]
        chords = [(verts[k], verts[(k + 1) % len(verts)]) for k in range(len(verts))]
    else:
        verts = _douglas_peucker(pts, tol)
        chords = [(verts[k], verts[k + 1]) for k in range(len(verts) - 1)]
    if not chords:
        return []

    def span(i, j):
        if j > i:
            return pts[i:j + 1]
        return np.vstack([pts[i:], pts[:j + 1]])

    prims = []
    k = 0
    m = len(chords)
    while k < m:
        i = chords[k][0]
        best = None
        # Greedy: the longest run of chords k..k+r that one arc explains.
        for r in range(1, m):
            if k + r >= m and not closed:
                break
            j = chords[(k + r) % m][1]
            run = span(i, j)
            arc = _arc_fit(run)
            if arc is None or arc[3] > tol or arc[4] > 350 or arc[2] < 2 * tol:
                break
            # A long straight chord inside the run has no interior points to
            # violate the residual test, but its sagitta on the fitted circle
            # would: a 25 mm chord on R66 sags 1.2 mm — not an arc.
            longest = float(np.linalg.norm(np.diff(run, axis=0), axis=1).max())
            if longest * longest / (8.0 * arc[2]) > tol:
                break
            if len(run) >= 5 and arc[4] >= 8.0:
                best = (r, j, arc)
        if best is not None:
            r, j, arc = best
            prims.append({'kind': 'arc', 'i': i, 'j': j, 'fit': arc})
            k += r + 1
        else:
            j = chords[k][1]
            prims.append({'kind': 'line', 'i': i, 'j': j})
            k += 1
    return prims


def _snap_angle(direction, snap_deg):
    ang = math.degrees(math.atan2(direction[1], direction[0]))
    target = round(ang / 45.0) * 45.0
    if abs(ang - target) <= snap_deg:
        rad = math.radians(target)
        return np.array([math.cos(rad), math.sin(rad)]), target
    return None, None


def _intersect_lines(p1, d1, p2, d2):
    denom = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denom) < 1e-9:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / denom
    return p1 + t * d1


def _build_segments(pts, prims, closed, tol, angle_snap_deg, radius_snap_mm):
    """Turn primitive spans into concrete 2D segments with shared junctions,
    applying angle/radius snapping where it stays within tol."""
    n_prims = len(prims)
    junction = {}
    for p in prims:
        junction[p['i']] = pts[p['i']].copy()
        junction[p['j']] = pts[p['j']].copy()

    # Arc parameters (radius snapping) first — they own their junctions.
    arcs = {}
    for idx, p in enumerate(prims):
        if p['kind'] != 'arc':
            continue
        cx, cy, r, resid, sweep, ccw = p['fit']
        snapped = False
        if radius_snap_mm and radius_snap_mm > 0:
            r_snap = round(r / radius_snap_mm) * radius_snap_mm
            if r_snap > 0 and abs(r_snap - r) <= tol:
                r, snapped = r_snap, True
        centre = np.array([cx, cy])
        for key in (p['i'], p['j']):
            rel = junction[key] - centre
            norm = float(np.linalg.norm(rel)) or 1.0
            junction[key] = centre + rel / norm * r
        arcs[idx] = (centre, r, ccw, snapped, resid)

    def neighbour(idx, step):
        if closed:
            return prims[(idx + step) % n_prims]
        k = idx + step
        return prims[k] if 0 <= k < n_prims else None

    # Tangent junctions: Douglas-Peucker breaks LATE where a line runs
    # tangentially into an arc (the first chord stays within tol), which
    # skews the line by ~tol. Where the line is tangent to the arc's circle,
    # move the junction to the foot of the centre on the line.
    for _pass in range(3):   # each pass re-derives the line direction
        for idx, p in enumerate(prims):
            if p['kind'] != 'arc':
                continue
            centre, r, _ccw, _snapped, _resid = arcs[idx]
            for key, step in ((p['i'], -1), (p['j'], 1)):
                nb = neighbour(idx, step)
                if nb is None or nb['kind'] != 'line' or nb is p:
                    continue
                other = junction[nb['i']] if step == -1 else junction[nb['j']]
                direction = junction[key] - other
                length = float(np.linalg.norm(direction))
                if length < 1e-9:
                    continue
                direction = direction / length
                foot = other + float((centre - other) @ direction) * direction
                if abs(float(np.linalg.norm(centre - foot)) - r) <= 2 * tol and \
                        float(np.linalg.norm(foot - junction[key])) <= max(4 * tol, 0.2 * r):
                    junction[key] = foot

    # Angle snapping for lines whose BOTH neighbours are lines.
    snapped_dirs = {}
    if angle_snap_deg and angle_snap_deg > 0:
        for idx, p in enumerate(prims):
            if p['kind'] != 'line':
                continue
            prev_p, next_p = neighbour(idx, -1), neighbour(idx, 1)
            if (prev_p is not None and prev_p['kind'] == 'arc') or \
               (next_p is not None and next_p['kind'] == 'arc'):
                continue
            direction = junction[p['j']] - junction[p['i']]
            if np.linalg.norm(direction) < 4 * tol:
                continue
            snapped, target = _snap_angle(direction / np.linalg.norm(direction),
                                          angle_snap_deg)
            if snapped is not None:
                mid = (junction[p['i']] + junction[p['j']]) / 2.0
                snapped_dirs[idx] = (mid, snapped, target)
        # Re-intersect consecutive snapped lines at their shared junction.
        for idx, p in enumerate(prims):
            nxt_idx = (idx + 1) % n_prims if closed else idx + 1
            if nxt_idx >= n_prims or (not closed and nxt_idx == idx):
                continue
            if idx in snapped_dirs and nxt_idx in snapped_dirs and idx != nxt_idx:
                m1, d1, _ = snapped_dirs[idx]
                m2, d2, _ = snapped_dirs[nxt_idx]
                hit = _intersect_lines(m1, d1, m2, d2)
                key = p['j']
                if hit is not None and np.linalg.norm(hit - junction[key]) <= 3 * tol:
                    junction[key] = hit

    segments = []
    for idx, p in enumerate(prims):
        a, b = junction[p['i']], junction[p['j']]
        if p['kind'] == 'line':
            direction = b - a
            length = float(np.linalg.norm(direction))
            ang = math.degrees(math.atan2(direction[1], direction[0])) if length else 0.0
            seg = {'kind': 'line', 'start': _rounded(a.tolist(), 3),
                   'end': _rounded(b.tolist(), 3), 'length_mm': _rounded(length, 3),
                   'angle_deg': _rounded(ang, 2)}
            if idx in snapped_dirs:
                seg['snapped_angle_deg'] = snapped_dirs[idx][2]
            segments.append(seg)
        else:
            centre, r, ccw, snapped, resid = arcs[idx]
            a0 = math.atan2(a[1] - centre[1], a[0] - centre[0])
            a1 = math.atan2(b[1] - centre[1], b[0] - centre[0])
            sweep = (a1 - a0) % (2 * math.pi) if ccw else -((a0 - a1) % (2 * math.pi))
            mid_ang = a0 + sweep / 2.0
            mid = centre + r * np.array([math.cos(mid_ang), math.sin(mid_ang)])
            seg = {'kind': 'arc', 'start': _rounded(a.tolist(), 3),
                   'end': _rounded(b.tolist(), 3), 'mid': _rounded(mid.tolist(), 3),
                   'center': _rounded(centre.tolist(), 3), 'radius_mm': _rounded(r, 3),
                   'sweep_deg': _rounded(math.degrees(abs(sweep)), 2), 'ccw': bool(ccw),
                   'fit_residual_mm': _rounded(resid, 3)}
            if snapped:
                seg['snapped_radius'] = True
            segments.append(seg)
    return segments


def _residual_to_segments(pts, segments):
    """Max distance of the original points to the fitted primitives."""
    best = np.full(len(pts), np.inf)
    for seg in segments:
        if seg['kind'] == 'line':
            a, b = np.asarray(seg['start']), np.asarray(seg['end'])
            d = b - a
            ln = float(d @ d)
            if ln < 1e-12:
                dist = np.linalg.norm(pts - a, axis=1)
            else:
                t = np.clip(((pts - a) @ d) / ln, 0.0, 1.0)
                dist = np.linalg.norm(pts - (a + t[:, None] * d), axis=1)
        else:
            c = np.asarray(seg['center'])
            dist = np.abs(np.linalg.norm(pts - c, axis=1) - seg['radius_mm'])
        best = np.minimum(best, dist)
    return float(best.max()) if len(best) else 0.0


def profile(path, axis='z', offset=None, plane_normal=None, plane_point=None,
            tol=0.15, angle_snap_deg=2.0, radius_snap_mm=0.5, max_loops=4,
            min_points=6):
    """Cut a scan with one plane and return the contour(s) as LINE + ARC
    segments (mm) — sketch-ready geometry, not a spline. Plane: world axis
    + offset (default: mid-extent) or an explicit plane_normal/plane_point.
    Each loop: segments with shared junctions, closed flag, fit residual,
    plus the 3D frame (origin, u, v, normal) so the add-in can rebuild the
    curves in any sketch via modelToSketchSpace."""
    mesh = _load(path)
    if plane_normal is not None:
        normal = _unit(plane_normal)
        origin = np.asarray(plane_point if plane_point is not None
                            else mesh.bounds.mean(axis=0), dtype=float)
        u_vec, v_vec = _plane_basis(normal)
        sketch_plane = None
        axis_name = None
    else:
        axis_name = (axis or 'z').lower()
        if axis_name not in scan._AXES:
            raise RuntimeError("axis must be x|y|z, got %r" % axis)
        ax, (u, v), sketch_plane, _names = scan._AXES[axis_name]
        normal = np.zeros(3)
        normal[ax] = 1.0
        u_vec, v_vec = np.zeros(3), np.zeros(3)
        u_vec[u], v_vec[v] = 1.0, 1.0
        origin = np.zeros(3)
        origin[ax] = float(offset) if offset is not None else float(mesh.bounds.mean(axis=0)[ax])
    segs3d = trimesh.intersections.mesh_plane(mesh, normal, origin)
    loops = _assemble_loops(segs3d)
    if not loops:
        raise RuntimeError('The plane does not cut the mesh.')

    def to2d(points):
        rel = np.asarray(points, dtype=float) - origin
        return np.column_stack([rel @ u_vec, rel @ v_vec])

    def to3d(xy):
        return origin + xy[0] * u_vec + xy[1] * v_vec

    ranked = sorted(loops, key=lambda lp: (-int(lp[1]), -len(lp[0])))
    out_loops = []
    for points, closed in ranked[:max_loops]:
        xy = to2d(points)
        # Drop near-duplicate consecutive points (mesh_plane emits them).
        keep = [0]
        for k in range(1, len(xy)):
            if np.linalg.norm(xy[k] - xy[keep[-1]]) > 1e-6:
                keep.append(k)
        xy = xy[keep]
        if closed and len(xy) > 1 and np.linalg.norm(xy[0] - xy[-1]) < 1e-6:
            xy = xy[:-1]
        if len(xy) < min_points:
            continue
        prims = _segment_polyline(xy, closed, tol)
        if not prims:
            continue
        segments = _build_segments(xy, prims, closed, tol, angle_snap_deg,
                                   radius_snap_mm)
        for seg in segments:
            for key in ('start', 'end', 'mid'):
                if key in seg:
                    seg[key + '_3d'] = _rounded(to3d(np.asarray(seg[key])), 3)
        entry = {
            'closed': bool(closed),
            'points': int(len(xy)),
            'segments': segments,
            'lines': sum(1 for s in segments if s['kind'] == 'line'),
            'arcs': sum(1 for s in segments if s['kind'] == 'arc'),
            'max_residual_mm': _rounded(_residual_to_segments(xy, segments), 3),
            'bbox_min': _rounded(xy.min(axis=0).tolist(), 2),
            'bbox_max': _rounded(xy.max(axis=0).tolist(), 2),
        }
        if closed:
            entry['area_mm2'] = _rounded(scan._shoelace(xy.tolist()), 2)
        out_loops.append(entry)
    report = {
        'file': path,
        'frame': {'origin_mm': _rounded(origin, 3), 'u': _rounded(u_vec),
                  'v': _rounded(v_vec), 'normal': _rounded(normal)},
        'tolerance_mm': tol,
        'loops': out_loops,
        'loop_count_total': len(loops),
    }
    if sketch_plane:
        report['sketch_plane'] = sketch_plane
        report['offset_mm'] = _rounded(float(origin[scan._AXES[axis_name][0]]), 3)
    report['note'] = ('2D coords are (u, v) in the frame; *_3d are world mm. '
                     'Hand the loops to sketch_profile (segments with start_3d/'
                     'end_3d/mid_3d) to get real lines/arcs with coincident '
                     'constraints, or scan_profile(to_fusion=true) does it.')
    return report


# --------------------------------------------------------------------------- #
# thread_identify
# --------------------------------------------------------------------------- #
# ISO 261 coarse pitch by nominal diameter, plus common fine pitches.
_ISO_COARSE = {
    1.0: 0.25, 1.2: 0.25, 1.4: 0.3, 1.6: 0.35, 2.0: 0.4, 2.5: 0.45, 3.0: 0.5,
    3.5: 0.6, 4.0: 0.7, 5.0: 0.8, 6.0: 1.0, 7.0: 1.0, 8.0: 1.25, 10.0: 1.5,
    12.0: 1.75, 14.0: 2.0, 16.0: 2.0, 18.0: 2.5, 20.0: 2.5, 22.0: 2.5,
    24.0: 3.0, 27.0: 3.0, 30.0: 3.5, 33.0: 3.5, 36.0: 4.0, 39.0: 4.0,
    42.0: 4.5, 45.0: 4.5, 48.0: 5.0, 52.0: 5.0, 56.0: 5.5, 60.0: 5.5, 64.0: 6.0,
}
_ISO_FINE = {
    8.0: (1.0,), 10.0: (1.25, 1.0), 12.0: (1.5, 1.25), 14.0: (1.5,),
    16.0: (1.5,), 18.0: (1.5, 2.0), 20.0: (1.5, 2.0), 22.0: (1.5, 2.0),
    24.0: (2.0, 1.5), 27.0: (2.0,), 30.0: (2.0, 1.5), 33.0: (2.0,),
    36.0: (3.0, 2.0), 39.0: (3.0,), 42.0: (3.0, 2.0), 48.0: (3.0, 2.0),
}
# (name, major diameter mm, pitch mm) for a few UNC/UNF sizes.
_UNIFIED = [
    ('#4-40 UNC', 2.845, 25.4 / 40), ('#6-32 UNC', 3.505, 25.4 / 32),
    ('#8-32 UNC', 4.166, 25.4 / 32), ('#10-24 UNC', 4.826, 25.4 / 24),
    ('1/4-20 UNC', 6.35, 25.4 / 20), ('1/4-28 UNF', 6.35, 25.4 / 28),
    ('5/16-18 UNC', 7.938, 25.4 / 18), ('5/16-24 UNF', 7.938, 25.4 / 24),
    ('3/8-16 UNC', 9.525, 25.4 / 16), ('3/8-24 UNF', 9.525, 25.4 / 24),
    ('7/16-14 UNC', 11.112, 25.4 / 14), ('1/2-13 UNC', 12.7, 25.4 / 13),
    ('1/2-20 UNF', 12.7, 25.4 / 20), ('5/8-11 UNC', 15.875, 25.4 / 11),
    ('3/4-10 UNC', 19.05, 25.4 / 10),
]


def _thread_candidates(major_mm, pitch_mm, internal):
    """Standard designations consistent with a measured major diameter and
    pitch, best first. For an internal thread the measured 'major' is the
    nut's minor diameter, so nominal D ~ measured + 1.0825 p."""
    nominal_guess = major_mm + (1.0825 * pitch_mm if internal else 0.0)
    out = []
    for d_nom, coarse in _ISO_COARSE.items():
        d_err = d_nom - nominal_guess       # external threads run slightly under
        if not (-0.2 <= d_err <= 0.45):
            continue
        for p, series in [(coarse, 'coarse')] + [(f, 'fine') for f in _ISO_FINE.get(d_nom, ())]:
            p_err = abs(p - pitch_mm)
            if p_err <= max(0.08, 0.06 * p):
                name = 'M%g' % d_nom if series == 'coarse' else 'M%gx%g' % (d_nom, p)
                out.append({'designation': name, 'standard': 'ISO 261 %s' % series,
                            'nominal_mm': d_nom, 'pitch_mm': p,
                            'score': _rounded(abs(d_err) + 3 * p_err, 3)})
    for name, d_nom, p in _UNIFIED:
        d_err = d_nom - nominal_guess
        if -0.2 <= d_err <= 0.45 and abs(p - pitch_mm) <= max(0.06, 0.05 * p):
            out.append({'designation': name, 'standard': 'Unified',
                        'nominal_mm': _rounded(d_nom, 3), 'pitch_mm': _rounded(p, 4),
                        'score': _rounded(abs(d_err) + 3 * abs(p - pitch_mm), 3)})
    out.sort(key=lambda e: e['score'])
    return out


def _cylinder_scatter(pts, axis):
    """Radial scatter (std/r) of pts about the best in-plane circle for a
    given axis -> (score, (centre, radius)) or (inf, None)."""
    u_vec, v_vec = _plane_basis(axis)
    mean = pts.mean(axis=0)
    rel = pts - mean
    xy = np.column_stack([rel @ u_vec, rel @ v_vec])
    fit = _fit_circle2d(xy)
    if fit is None:
        return np.inf, None
    cx, cy, radius = fit
    dist = np.linalg.norm(xy - (cx, cy), axis=1)
    return float(np.std(dist) / max(radius, 1e-9)), (mean + cx * u_vec + cy * v_vec, radius)


def _thread_axis(pts, normals):
    """Best cylinder axis for a (threaded) shaft: candidates from the normal
    covariance and from the points' PCA, then a coordinate search on the
    radial scatter. Returns (axis, (centre, radius), scatter)."""
    candidates = [_unit(np.linalg.eigh(normals.T @ normals)[1][:, 0])]
    rel = pts - pts.mean(axis=0)
    candidates.append(_unit(np.linalg.eigh(rel.T @ rel)[1][:, -1]))
    best = None
    for cand in candidates:
        score, info = _cylinder_scatter(pts, cand)
        if info is not None and (best is None or score < best[2]):
            best = (cand, info, score)
    if best is None:
        raise RuntimeError('Cylinder fit failed.')
    axis, info, score = best
    for step_deg in (3.0, 1.0, 0.3, 0.1, 0.03):
        tilt = math.tan(math.radians(step_deg))
        improved = True
        while improved:
            improved = False
            u_vec, v_vec = _plane_basis(axis)
            for d in (u_vec, -u_vec, v_vec, -v_vec):
                cand = _unit(axis + tilt * d)
                cand_score, cand_info = _cylinder_scatter(pts, cand)
                if cand_info is not None and cand_score < score - 1e-12:
                    axis, info, score, improved = cand, cand_info, cand_score, True
    return axis, info, score


def _fold_score(z, theta, r, pitch, hand, bins=16):
    """R^2 of radius explained by the helical phase for one (pitch, hand):
    hand=+1 right-hand (z rises with CCW angle about +axis)."""
    phase = ((z - hand * pitch * theta / (2 * math.pi)) / pitch) % 1.0
    idx = np.minimum((phase * bins).astype(int), bins - 1)
    counts = np.bincount(idx, minlength=bins)
    sums = np.bincount(idx, weights=r, minlength=bins)
    means = np.where(counts > 0, sums / np.maximum(counts, 1), r.mean())
    between = float(((means - r.mean()) ** 2 * counts).sum())
    total = float(((r - r.mean()) ** 2).sum()) or 1e-12
    return between / total


def thread_identify(path, max_points=20000, pitch_min=0.3, pitch_max=6.0):
    """Identify a scanned thread: cylinder axis from the surface normals,
    major/minor diameters from the radial distribution, pitch + handedness
    by a folded-phase periodogram (smallest pitch explaining the radius
    variation — harmonics rejected), snapped to ISO 261 / UNC / UNF."""
    mesh = _load(path)
    scale = float((mesh.bounds[1] - mesh.bounds[0]).max())
    if len(mesh.vertices) >= 3000:
        pts = np.asarray(mesh.vertices, dtype=float)
        normals = np.asarray(mesh.vertex_normals, dtype=float)
    else:
        pts, fidx = _sample_surface(mesh, 12000)
        normals = np.asarray(mesh.face_normals)[fidx]
    stride = max(1, -(-len(pts) // int(max_points)))
    pts, normals = pts[::stride], normals[::stride]
    good = np.isfinite(normals).all(axis=1) & (np.linalg.norm(normals, axis=1) > 0.5)
    pts, normals = pts[good], normals[good]
    normals = normals / np.linalg.norm(normals, axis=1)[:, None]

    # Axis: two candidates — smallest eigenvector of the normal covariance
    # (works for ISO 30-degree flanks) and the points' long PCA axis (works
    # for any flank angle when the thread is longer than it is wide) — scored
    # by radial scatter after an in-plane circle fit, then polished by a
    # coordinate search minimising that scatter. No RANSAC, no normals
    # thresholds that drop steep flanks.
    axis, (centre, r0), _scatter = _thread_axis(pts, normals)
    caps = np.abs(normals @ axis) > 0.9      # end faces / chamfer tops
    sub, sub_n = pts[~caps], normals[~caps]
    if len(sub) < 100:
        raise RuntimeError('Too few thread-flank points — is this a threaded '
                           'cylinder in mm?')
    u_vec, v_vec = _plane_basis(axis)
    rel = sub - centre
    z = rel @ axis
    radial = rel - np.outer(z, axis)
    r = np.linalg.norm(radial, axis=1)
    theta = np.arctan2(radial @ v_vec, radial @ u_vec)
    # Trim the ends (chamfers/run-out) to the middle 90 % of the length.
    lo, hi = np.percentile(z, [5, 95])
    keep = (z >= lo) & (z <= hi)
    z, r, theta = z[keep], r[keep], theta[keep]
    if len(z) < 100:
        raise RuntimeError('Thread too short for a pitch estimate.')
    length = float(hi - lo) / 0.9
    major = float(np.percentile(r, 99)) * 2.0
    minor = float(np.percentile(r, 1)) * 2.0
    depth = (major - minor) / 2.0
    # External vs internal: outward normals point away from the axis.
    rad_n = radial[keep] / (r[:, None] + 1e-12)
    outward = float(np.mean(np.einsum('ij,ij->i', sub_n[keep], rad_n)))
    internal = outward < 0

    # Periodogram over standard pitches + a fine grid, both hands.
    grid = np.arange(pitch_min, pitch_max + 1e-9, 0.01)
    standard = sorted({p for p in _ISO_COARSE.values()}
                      | {p for fs in _ISO_FINE.values() for p in fs}
                      | {p for _n, _d, p in _UNIFIED})
    candidates = np.unique(np.round(np.concatenate([grid, standard]), 4))
    candidates = candidates[(candidates >= pitch_min) & (candidates <= pitch_max)
                            & (candidates < 0.6 * length)]
    if not len(candidates):
        raise RuntimeError('Thread length %.1f mm too short for pitches >= %.2f.'
                           % (length, pitch_min))
    r_var = r - r.mean()
    scores = np.zeros((len(candidates), 2))
    for i, p in enumerate(candidates):
        for h_idx, hand in enumerate((1, -1)):
            scores[i, h_idx] = _fold_score(z, theta, r_var, float(p), hand)
    best_score = float(scores.max())
    if best_score < 0.25:
        return {'file': path, 'thread_detected': False, 'best_r2': _rounded(best_score, 3),
                'axis': _rounded(axis), 'diameter_mm': _rounded(major, 3),
                'note': 'No helical periodicity found — a plain cylinder, or the '
                        'scan is too coarse for the pitch (need >= 6 points per pitch).'}
    # Smallest pitch within 90 % of the best score: harmonics (2p, 3p) score
    # as well as p; p/2 folds ridge onto valley and collapses.
    hand_idx = int(np.argmax(scores.max(axis=0)))
    col = scores[:, hand_idx]
    ok = np.where(col >= 0.9 * best_score)[0]
    # Only accept a smaller pitch if it forms its own local peak.
    pick = int(ok[0])
    for k in ok:
        if col[k] >= 0.95 * best_score:
            pick = int(k)
            break
    pitch = float(candidates[pick])
    # Refine around the pick.
    fine = np.arange(pitch - 0.02, pitch + 0.0201, 0.001)
    fine = fine[fine > 0]
    fine_scores = [_fold_score(z, theta, r_var, float(p), 1 if hand_idx == 0 else -1)
                   for p in fine]
    pitch = float(fine[int(np.argmax(fine_scores))])
    r2 = float(max(fine_scores))
    hand = 'right' if hand_idx == 0 else 'left'
    matches = _thread_candidates(major, pitch, internal)
    report = {
        'file': path,
        'thread_detected': True,
        'kind': 'internal' if internal else 'external',
        'pitch_mm': _rounded(pitch, 3),
        'hand': hand,
        'major_diameter_mm': _rounded(major, 3),
        'minor_diameter_mm': _rounded(minor, 3),
        'thread_depth_mm': _rounded(depth, 3),
        'iso_depth_expected_mm': _rounded(0.6134 * pitch, 3),
        'length_mm': _rounded(length, 2),
        'axis': _rounded(axis),
        'axis_point_mm': _rounded(centre, 3),
        'confidence_r2': _rounded(r2, 3),
        'points_used': int(len(z)),
        'matches': matches[:4],
        'designation': matches[0]['designation'] if matches else None,
    }
    if not matches:
        report['note'] = ('Pitch found but no standard size fits the diameter — '
                          'worn/undersize scan, a trapezoidal or pipe thread, or '
                          'a non-standard part. Compare major_diameter_mm with '
                          'the ISO table by hand.')
    if depth < 0.4 * 0.6134 * pitch:
        report['warning'] = ('Measured thread depth is well under the ISO value '
                             '— scanner resolution is smoothing the crests; the '
                             'pitch is trustworthy, the diameters less so.')
    if scale > 0 and length < 3 * pitch:
        report['warning'] = 'Fewer than 3 turns scanned — pitch confidence is low.'
    return report


# --------------------------------------------------------------------------- #
# mesh_offset — voxel dilation (cavity cutter)
# --------------------------------------------------------------------------- #
_MAX_VOXELS = 12_000_000


def _ball_offsets(radius_vox):
    r = int(math.ceil(radius_vox))
    rng = np.arange(-r, r + 1)
    dx, dy, dz = np.meshgrid(rng, rng, rng, indexing='ij')
    mask = dx ** 2 + dy ** 2 + dz ** 2 <= radius_vox ** 2 + 1e-9
    return np.column_stack([dx[mask], dy[mask], dz[mask]])


def _dilate(grid, radius_vox):
    """Binary dilation with a ball. scipy.ndimage when importable (fast),
    else a shift-OR over the ball offsets (pure numpy, small radii only)."""
    try:
        from scipy import ndimage
        r = int(math.ceil(radius_vox))
        rng = np.arange(-r, r + 1)
        dx, dy, dz = np.meshgrid(rng, rng, rng, indexing='ij')
        ball = dx ** 2 + dy ** 2 + dz ** 2 <= radius_vox ** 2 + 1e-9
        return ndimage.binary_dilation(grid, structure=ball)
    except Exception:  # noqa: BLE001 - fall back to numpy
        out = np.zeros_like(grid)
        shape = np.array(grid.shape)
        for off in _ball_offsets(radius_vox):
            src = [slice(max(0, -o), min(s, s - o)) for o, s in zip(off, shape, strict=False)]
            dst = [slice(max(0, o), min(s, s + o)) for o, s in zip(off, shape, strict=False)]
            out[tuple(dst)] |= grid[tuple(src)]
        return out


def _fill_interior(shell):
    """Solid occupancy from a closed voxel shell: everything not reachable
    from the padded border through empty voxels is interior."""
    try:
        from scipy import ndimage
        labels, _n = ndimage.label(~shell)
        border = np.zeros_like(shell)
        border[0, :, :] = border[-1, :, :] = True
        border[:, 0, :] = border[:, -1, :] = True
        border[:, :, 0] = border[:, :, -1] = True
        outside_labels = np.unique(labels[border & ~shell])
        outside = np.isin(labels, outside_labels[outside_labels > 0])
        return ~outside
    except Exception:  # noqa: BLE001 - numpy flood fill
        outside = np.zeros_like(shell)
        outside[0, :, :] = outside[-1, :, :] = True
        outside[:, 0, :] = outside[:, -1, :] = True
        outside[:, :, 0] = outside[:, :, -1] = True
        outside &= ~shell
        while True:
            grown = outside.copy()
            grown[1:, :, :] |= outside[:-1, :, :]
            grown[:-1, :, :] |= outside[1:, :, :]
            grown[:, 1:, :] |= outside[:, :-1, :]
            grown[:, :-1, :] |= outside[:, 1:, :]
            grown[:, :, 1:] |= outside[:, :, :-1]
            grown[:, :, :-1] |= outside[:, :, 1:]
            grown &= ~shell
            if np.array_equal(grown, outside):
                break
            outside = grown
        return ~outside


def _cumulative(grid, axis_index, from_positive):
    """Monotone occupancy along one axis: a cover approaching from +axis
    must clear everything ABOVE each level -> OR-accumulate from the top."""
    if from_positive:
        flipped = np.flip(grid, axis=axis_index)
        acc = np.logical_or.accumulate(flipped, axis=axis_index)
        return np.flip(acc, axis=axis_index)
    return np.logical_or.accumulate(grid, axis=axis_index)


def mesh_offset(path, distance, out_path=None, mode='offset', axis='z',
                approach='+', pitch=None, smooth_iterations=2, extend_mm=None):
    """Grow a scanned object by `distance` mm into a solid cutter mesh (STL):
    mode='offset' follows the shape everywhere; mode='monotone' also makes
    the result monotone along `axis` (approach '+' = the cover comes down
    onto the object from +axis; the cavity then runs down to the opening at
    -axis and the cutter is extended `extend_mm`, default 3x distance, past
    the part there) so a cover drops straight on without undercuts.
    Voxel-based: pitch = distance / (n + 0.5) (default n from distance/4,
    auto-coarsened to stay under ~12 M voxels) so the surface lands at
    +distance +- pitch/2 — the report states the pitch."""
    mesh = _load(path)
    distance = float(distance)
    if distance <= 0:
        raise RuntimeError('distance must be > 0 mm')
    try:
        from skimage import measure
    except ImportError as exc:
        raise RuntimeError('mesh_offset needs scikit-image for marching cubes: '
                           'pip install scikit-image  (import error: %s)' % exc) from exc
    mode = (mode or 'offset').lower()
    if mode not in ('offset', 'monotone'):
        raise RuntimeError("mode must be 'offset' or 'monotone'")
    size = mesh.bounds[1] - mesh.bounds[0]
    # Dilation is an integer number of voxels; the shell voxel adds ~0.5
    # and marching cubes another 0.5, so pick pitch = distance / (n + 0.5)
    # and dilate n voxels -> the surface lands at +distance +- pitch/2.
    pitch0 = float(pitch) if pitch else max(0.3, distance / 4.0)
    n_vox = max(1, int(round(distance / pitch0 - 0.5)))
    axis_index = None
    if mode == 'monotone':
        axis_name = (axis or 'z').lower()
        if axis_name not in scan._AXES:
            raise RuntimeError("axis must be x|y|z")
        axis_index = scan._AXES[axis_name][0]
    extend = float(extend_mm) if extend_mm is not None else 3.0 * distance
    while True:
        pitch = distance / (n_vox + 0.5)
        pad_lo = np.full(3, distance + 2 * pitch)
        pad_hi = pad_lo.copy()
        if axis_index is not None:
            # A cover approaching from +axis has its OPENING at -axis (the
            # cavity is the union of all slices above each level, so it runs
            # down to the rim): extend the cutter past the part on the
            # opening side so the combine-cut opens the cavity there.
            if approach != '-':
                pad_lo[axis_index] += extend
            else:
                pad_hi[axis_index] += extend
        dims = np.ceil((size + pad_lo + pad_hi) / pitch).astype(int) + 2
        if int(np.prod(dims)) <= _MAX_VOXELS or n_vox == 1:
            break
        n_vox -= 1
    origin = mesh.bounds[0] - pad_lo
    # Occupancy: dense deterministic surface sampling (~6 samples per voxel
    # face area, plus every vertex) -> surface voxels, closed by a 1-voxel
    # dilation (sampling pinholes + non-watertight scans), then interior
    # fill. trimesh's voxelized() does the same job ~30x slower.
    n_samples = int(min(3_000_000, max(20000, 6.0 * float(mesh.area) / (pitch * pitch))))
    pts, _fidx = _sample_surface(mesh, n_samples, seed=3)
    pts = np.vstack([pts, np.asarray(mesh.vertices, dtype=float)])
    shell = np.zeros(tuple(dims), dtype=bool)
    idx = np.floor((pts - origin) / pitch).astype(int)
    idx = idx[(idx >= 0).all(axis=1) & (idx < dims).all(axis=1)]
    shell[idx[:, 0], idx[:, 1], idx[:, 2]] = True
    solid = _fill_interior(_dilate(shell, 1.0))
    if axis_index is not None:
        solid = _cumulative(solid, axis_index, approach != '-')
    # The closing dilation already added one voxel; grow the remainder.
    grown = _dilate(solid, float(n_vox - 1)) if n_vox > 1 else solid
    # Empty outermost layers so marching cubes closes the surface (monotone
    # mode fills up to the grid top on purpose — this caps it).
    for ax in range(3):
        sl = [slice(None)] * 3
        sl[ax] = 0
        grown[tuple(sl)] = False
        sl[ax] = -1
        grown[tuple(sl)] = False
    verts, faces, _normals, _vals = measure.marching_cubes(
        grown.astype(np.float32), level=0.5)
    verts = verts * pitch + origin
    out = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    if smooth_iterations and smooth_iterations > 0:
        try:
            trimesh.smoothing.filter_laplacian(out, iterations=int(smooth_iterations))
        except Exception:  # noqa: BLE001 - smoothing is cosmetic (needs scipy)
            pass
    try:
        trimesh.repair.fix_normals(out)
    except Exception:  # noqa: BLE001
        pass
    if not out_path:
        stem = os.path.splitext(path)[0]
        out_path = '%s_offset%g%s.stl' % (stem, distance,
                                          '_mono' if mode == 'monotone' else '')
    out.export(out_path)
    report = {
        'input': path,
        'output': out_path,
        'mode': mode,
        'distance_mm': distance,
        'voxel_pitch_mm': _rounded(pitch, 3),
        'grid': [int(d) for d in dims],
        'triangles': int(len(out.faces)),
        'watertight': bool(out.is_watertight),
        'size_mm': _rounded(out.bounds[1] - out.bounds[0], 2),
        'input_watertight': bool(mesh.is_watertight),
        'note': ('Import with import_mesh, mesh_to_brep(method="faceted") and '
                 'combine(operation="cut") into the part; or keep it as the '
                 'cavity reference. Stair-step error ~ pitch/2 — verify with '
                 'scan_fit_check(clearance_mm=...).'),
    }
    if mode == 'monotone':
        report['axis'] = axis
        report['approach'] = approach
        report['extend_mm'] = _rounded(extend, 3)
    return report


# --------------------------------------------------------------------------- #
# fit_report — the verification sheet
# --------------------------------------------------------------------------- #
def fit_report(model_path, scan_path=None, clearance_mm=0.3, min_wall_mm=1.2,
               max_penetration_mm=0.0, bed=(256.0, 256.0, 256.0),
               overhang_deg=45.0, process='fdm'):
    """PASS / WARN / FAIL sheet for a designed part: seating against the
    scan (scan_fit_check), printability (print_check), manufacturability
    (dfm_check) and, when a ray backend exists, wall thickness. Thresholds
    are explicit inputs so the verdict is reproducible."""
    checks = []
    verdicts = []

    def verdict(name, status, detail):
        checks.append({'check': name, 'status': status, **detail})
        verdicts.append(status)

    if scan_path:
        fit = scan.fit_check(scan_path, model_path, clearance_mm=clearance_mm)
        if fit.get('signed'):
            pen = fit.get('penetration_mm', {}).get('max', 0.0) if fit.get('collisions') else 0.0
            status = 'PASS' if pen <= max_penetration_mm else (
                'WARN' if pen <= max_penetration_mm + 0.3 else 'FAIL')
            verdict('seating', status, {
                'collisions': fit.get('collisions', 0),
                'penetration_max_mm': pen,
                'clearance_mm': fit.get('clearance_mm'),
                'below_target_fraction': fit.get('below_target'),
                'worst_points_mm': fit.get('worst_points_mm', [])[:5]})
        else:
            verdict('seating', 'WARN', {'reason': fit.get('note'),
                                        'clearance_mm': fit.get('clearance_mm')})
    pc = scan.print_check(model_path, bed=bed, overhang_deg=overhang_deg,
                          min_wall=min_wall_mm)
    recs = [r for r in (pc.get('recommendations') or [])
            if not str(r).startswith('No blocking')]
    fits_bed = pc.get('fits_bed', True)
    ov = (pc.get('overhang') or {}).get('unsupported_area_fraction')
    status = 'PASS'
    if not fits_bed:
        status = 'FAIL'
    elif recs or (ov or 0.0) > 0.15:
        status = 'WARN'
    verdict('printability', status, {
        'fits_bed': fits_bed, 'overhang_area_fraction': ov,
        'recommendations': recs[:6],
        'fit_orientations': pc.get('fit_orientations')})
    walls = pc.get('walls') or {}
    p5 = walls.get('p5_mm', walls.get('p5')) if isinstance(walls, dict) else None
    if p5 is not None:
        verdict('walls', 'PASS' if p5 >= min_wall_mm else
                ('WARN' if p5 >= 0.75 * min_wall_mm else 'FAIL'),
                {'p5_mm': p5, 'median_mm': walls.get('median_mm', walls.get('median')),
                 'min_required_mm': min_wall_mm})
    else:
        verdict('walls', 'WARN', {'reason': 'wall thickness unavailable '
                                            '(needs the trimesh ray backend: pip install rtree)'})
    if process and process.lower() != 'fdm':
        # fdm == print_check above; injection / cnc3axis add their own checks.
        try:
            import dfm
            dfm_rep = dfm.check(model_path, process=process, min_wall=min_wall_mm)
            dfm_recs = dfm_rep.get('recommendations') or []
            verdict('dfm_%s' % process, 'PASS' if not dfm_recs else 'WARN',
                    {'recommendations': dfm_recs[:6],
                     'zero_draft': dfm_rep.get('zero_draft'),
                     'undercuts': dfm_rep.get('undercuts'),
                     'occluded_from_above': dfm_rep.get('occluded_from_above')})
        except Exception as exc:  # noqa: BLE001 - dfm optional
            verdict('dfm_%s' % process, 'WARN', {'reason': str(exc)})

    overall = 'PASS'
    if 'FAIL' in verdicts:
        overall = 'FAIL'
    elif 'WARN' in verdicts:
        overall = 'WARN'
    return {
        'model': model_path,
        'scan': scan_path,
        'overall': overall,
        'checks': checks,
        'thresholds': {'clearance_mm': clearance_mm, 'min_wall_mm': min_wall_mm,
                       'max_penetration_mm': max_penetration_mm,
                       'overhang_deg': overhang_deg},
        'note': 'FAIL = do not print; WARN = read the detail; PASS = within thresholds.',
    }


# --------------------------------------------------------------------------- #
# frame — datum alignment without a CAD model
# --------------------------------------------------------------------------- #
def frame(path, out_path=None, base_plane=0, x_from='auto', origin='bbox_min',
          max_primitives=8):
    """Re-frame a scan on its own datums: the chosen large plane becomes
    Z=0 with the part on +Z, X follows a perpendicular cylinder axis / the
    long direction (x_from: auto|pca|cylinder), origin at the projected bbox
    minimum, centroid, or a Z-parallel cylinder axis. Writes the transformed
    mesh and returns the 4x4 transform (row-major, mm) for import_mesh."""
    mesh = _load(path)
    planes = scan._planes_from_facets(mesh, max_primitives)
    if not planes:
        raise RuntimeError('No planar region large enough to serve as a datum.')
    base_plane = int(base_plane)
    if not 0 <= base_plane < len(planes):
        raise RuntimeError('base_plane must be 0..%d (largest first)' % (len(planes) - 1))
    plane = planes[base_plane]
    normal = _unit(plane['normal'])
    point = np.asarray(plane['point_mm'], dtype=float)
    # Part must sit on +Z: flip if the material centroid is behind the plane.
    centroid = mesh.centroid if not mesh.is_watertight else mesh.center_mass
    if float((centroid - point) @ normal) < 0:
        normal = -normal
    z_axis = normal

    cylinders = []
    try:
        scale = float((mesh.bounds[1] - mesh.bounds[0]).max())
        cylinders, _spheres = scan._ransac_primitives(mesh, scale, max_primitives)
    except Exception:  # noqa: BLE001 - optional (pyransac3d may be missing)
        cylinders = []
    x_axis = None
    chosen_x = None
    perp_cyls = [c for c in cylinders
                 if abs(float(_unit(c['axis']) @ z_axis)) < 0.3]
    para_cyls = [c for c in cylinders
                 if abs(float(_unit(c['axis']) @ z_axis)) > 0.95]
    if x_from in ('auto', 'cylinder') and perp_cyls:
        a = _unit(perp_cyls[0]['axis'])
        x_axis = _unit(a - (a @ z_axis) * z_axis)
        chosen_x = 'cylinder_axis'
    if x_axis is None:
        verts = np.asarray(mesh.vertices)
        rel = verts - verts.mean(axis=0)
        rel = rel - np.outer(rel @ z_axis, z_axis)
        direction = np.linalg.eigh(rel.T @ rel)[1][:, -1]
        x_axis = _unit(direction - (direction @ z_axis) * z_axis)
        chosen_x = 'pca_long_direction'
    y_axis = _unit(np.cross(z_axis, x_axis))
    x_axis = _unit(np.cross(y_axis, z_axis))
    rot = np.vstack([x_axis, y_axis, z_axis])   # world -> new frame rows

    new_pts = (np.asarray(mesh.vertices) - point) @ rot.T
    if origin == 'cylinder' and para_cyls:
        c = (np.asarray(para_cyls[0]['center_mm' if 'center_mm' in para_cyls[0]
                                     else 'point_mm'], dtype=float) - point) @ rot.T
        shift = np.array([c[0], c[1], 0.0])
        origin_used = 'cylinder_axis'
    elif origin == 'centroid':
        c = new_pts.mean(axis=0)
        shift = np.array([c[0], c[1], 0.0])
        origin_used = 'centroid'
    else:
        lo = new_pts.min(axis=0)
        shift = np.array([lo[0], lo[1], 0.0])
        origin_used = 'bbox_min'
    new_pts = new_pts - shift
    transform = np.eye(4)
    transform[:3, :3] = rot
    transform[:3, 3] = -rot @ point - shift
    framed = trimesh.Trimesh(vertices=new_pts, faces=np.asarray(mesh.faces), process=False)
    if not out_path:
        out_path = os.path.splitext(path)[0] + '_framed.stl'
    framed.export(out_path)
    return {
        'input': path,
        'output': out_path,
        'base_plane': {'index': base_plane, 'area_mm2': plane['area_mm2'],
                       'normal_world': _rounded(normal)},
        'x_from': chosen_x,
        'origin': origin_used,
        'transform_4x4': _rounded(transform.tolist(), 6),
        'bbox_min_mm': _rounded(framed.bounds[0], 3),
        'bbox_max_mm': _rounded(framed.bounds[1], 3),
        'planes_available': len(planes),
        'cylinders_seen': len(cylinders),
        'note': ('Datum plane is now Z=0 with the part on +Z; import the output '
                 'with import_mesh and it lands on the XY origin. transform_4x4 '
                 'maps original scan mm -> framed mm (use it to carry '
                 'scan_features coordinates over).'),
    }
