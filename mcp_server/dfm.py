"""Design-for-manufacturing checks on a mesh file (mm), in the server process.

No commercial DFM library is open source, so these are transparent
heuristics on trimesh (the "re" extras), tuned for the shop use-cases:

- injection: draft angle vs the mold pull direction, undercut detection by
  ray occlusion along +/-pull, uniform-wall estimate.
- cnc3axis: down-facing surfaces (unmachinable from above), up-facing
  surfaces occluded from above (unreachable pockets), and whether a second
  setup (flip) resolves them.
- fdm: delegates to scan.print_check (bed fit, overhangs, thin walls).

All face classification is exact vector math over every face; ray casting is
capped and chunked. Results are deterministic.
"""
import math

import scan

_AXES = {'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0), 'z': (0.0, 0.0, 1.0)}
_RAY_CAP = 2000      # max faces ray-tested per direction
_RAY_CHUNK = 800     # pure-python ray backend allocates per query


def _axis_vector(np, axis):
    if isinstance(axis, (list, tuple)):
        vec = np.asarray(axis, dtype=float)
    else:
        key = str(axis or 'z').lower()
        if key not in _AXES:
            raise RuntimeError('axis must be x|y|z or [x,y,z], got %r' % axis)
        vec = np.asarray(_AXES[key])
    norm = float(np.linalg.norm(vec))
    if norm == 0:
        raise RuntimeError('axis vector must be non-zero')
    return vec / norm


def _occluded(mesh, np, origins, direction, cap=_RAY_CAP):
    """True per origin when a ray along `direction` hits the mesh (the spot
    is shadowed by other geometry). Capped: beyond `cap` rays the extra
    entries return False with a `capped` flag handled by the caller."""
    n = min(len(origins), cap)
    hit = np.zeros(len(origins), dtype=bool)
    directions = np.tile(direction, (n, 1))
    for i in range(0, n, _RAY_CHUNK):
        hit[i:i + _RAY_CHUNK] = mesh.ray.intersects_any(
            origins[i:i + _RAY_CHUNK], directions[i:i + _RAY_CHUNK])
    return hit, len(origins) > cap


def _worst(np, centroids, areas, mask, limit=8):
    idx = np.argsort(np.where(mask, areas, -1.0))[::-1][:limit]
    return [{'at_mm': [round(float(v), 1) for v in centroids[i]],
             'area_mm2': round(float(areas[i]), 1)}
            for i in idx if mask[i]]


def check(path, process='fdm', axis='z', min_draft_deg=1.0, min_wall=1.0,
          clearance=0.05):
    """DFM report for `process` = "fdm" | "injection" | "cnc3axis".

    axis: mold pull direction (injection) or tool axis (cnc3axis).
    min_draft_deg: required draft on walls parallel to the pull direction.
    min_wall: wall-thickness floor in mm. Returns per-check area fractions,
    worst offender locations, and actionable recommendations."""
    process = (process or 'fdm').lower()
    if process == 'fdm':
        report = scan.print_check(path, min_wall=min_wall)
        report['process'] = 'fdm'
        return report
    if process not in ('injection', 'cnc3axis'):
        raise RuntimeError("process must be fdm|injection|cnc3axis, got %r"
                           % process)

    mesh = scan._load(path)
    import numpy as np
    pull = _axis_vector(np, axis)
    normals = np.asarray(mesh.face_normals, dtype=float)
    areas = np.asarray(mesh.area_faces, dtype=float)
    centroids = np.asarray(mesh.triangles_center, dtype=float)
    total = float(areas.sum()) or 1.0
    along = normals @ pull                     # cos(angle to pull direction)
    scale = float(np.asarray(mesh.extents).max())
    eps = max(clearance, 1e-3 * scale)

    report = {'file': path, 'process': process,
              'axis': [round(float(v), 4) for v in pull]}
    recommendations = []

    if process == 'injection':
        # Draft: a wall parallel to pull has |along| = 0 (zero draft). The
        # draft angle is asin(|along|); flag walls under min_draft_deg,
        # excluding genuine top/bottom faces (|along| > 0.5).
        draft_deg = np.degrees(np.arcsin(np.clip(np.abs(along), 0.0, 1.0)))
        no_draft = (draft_deg < float(min_draft_deg)) & (np.abs(along) < 0.5)
        frac = float(areas[no_draft].sum() / total)
        report['zero_draft'] = {
            'min_draft_deg': float(min_draft_deg),
            'area_fraction': round(frac, 3),
            'worst': _worst(np, centroids, areas, no_draft),
        }
        if frac > 0.02:
            recommendations.append(
                'Walls with less than %.1f deg draft cover %.0f%% of the '
                'surface — add draft toward the parting line.'
                % (min_draft_deg, 100 * frac))

        # Undercuts: a cavity-side face (along > 0) must see open sky along
        # +pull; a core-side face along -pull. Shadowed faces cannot release.
        # trimesh's ray backend needs rtree — degrade to a clear note when it
        # is missing instead of failing the whole report.
        undercut = np.zeros(len(areas), dtype=bool)
        capped = False
        ray_error = None
        for sign in (1.0, -1.0):
            side = along * sign > 0.1
            candidates = np.nonzero(side)[0]
            order = candidates[np.argsort(areas[candidates])[::-1]]
            origins = centroids[order] + normals[order] * eps
            try:
                hit, was_capped = _occluded(mesh, np, origins, pull * sign)
            except Exception as exc:  # noqa: BLE001 - missing rtree etc.
                ray_error = str(exc)
                break
            undercut[order[:len(hit)]] |= hit
            capped |= was_capped
        if ray_error is not None:
            report['undercuts'] = {
                'error': 'undercut ray test unavailable (%s) — pip install '
                         'rtree to enable it' % ray_error}
        else:
            frac = float(areas[undercut].sum() / total)
            report['undercuts'] = {
                'area_fraction': round(frac, 3),
                'worst': _worst(np, centroids, areas, undercut),
                'ray_capped': capped,
            }
            if frac > 0.005:
                recommendations.append(
                    'Shadowed (undercut) surfaces detected (%.1f%% of area) '
                    '— they need side actions/lifters or a redesign to '
                    'release along the pull direction.' % (100 * frac))

        walls = scan._wall_thickness(mesh, scale)
        report['walls'] = walls
        if walls and walls.get('p5') is not None and walls['p5'] < min_wall:
            recommendations.append(
                'Thinnest walls (p5 %.2f mm) are below %.2f mm — molding '
                'risks short shots; thicken or add ribs.'
                % (walls['p5'], min_wall))
        if walls and walls.get('median') is not None \
                and walls.get('p5') is not None \
                and walls['median'] > 2.5 * max(walls['p5'], 0.01):
            recommendations.append(
                'Wall thickness varies strongly (median vs p5) — uneven '
                'walls cause sink marks and warpage; aim for uniform '
                'thickness with ribs instead of bulk.')

    else:  # cnc3axis
        down = along < -math.sin(math.radians(5))
        frac_down = float(areas[down].sum() / total)
        # Up-facing faces occluded from above: unreachable with a 3-axis
        # tool coming down the axis.
        up = along > 0.1
        candidates = np.nonzero(up)[0]
        order = candidates[np.argsort(areas[candidates])[::-1]]
        origins = centroids[order] + normals[order] * eps
        report['down_facing'] = {
            'area_fraction': round(frac_down, 3),
            'worst': _worst(np, centroids, areas, down),
        }
        blocked = np.zeros(len(areas), dtype=bool)
        frac_blocked = 0.0
        try:
            hit, capped = _occluded(mesh, np, origins, pull)
            blocked[order[:len(hit)]] = hit
            frac_blocked = float(areas[blocked].sum() / total)
            report['occluded_from_above'] = {
                'area_fraction': round(frac_blocked, 3),
                'worst': _worst(np, centroids, areas, blocked),
                'ray_capped': capped,
            }
        except Exception as exc:  # noqa: BLE001 - missing rtree etc.
            report['occluded_from_above'] = {
                'error': 'occlusion ray test unavailable (%s) — pip install '
                         'rtree to enable it' % exc}
        if frac_down > 0.01:
            recommendations.append(
                'Down-facing surfaces (%.0f%% of area) cannot be machined '
                'from this setup — plan a flip (second setup) or accept '
                'as-cast faces.' % (100 * frac_down))
        if frac_blocked > 0.005:
            recommendations.append(
                'Up-facing surfaces shadowed by overhanging geometry '
                'detected — a straight 3-axis tool cannot reach them; '
                'consider splitting the part or 5-axis machining.')
        walls = scan._wall_thickness(mesh, scale)
        report['walls'] = walls

    if not recommendations:
        recommendations.append('No blocking issues found for %s.' % process)
    report['min_wall_mm'] = float(min_wall)
    report['recommendations'] = recommendations
    return report
