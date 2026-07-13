"""Operation handlers for FusionMCP.

Every handler runs on Fusion's main thread (dispatched from bridge.py) and has
the signature `handler(app, params: dict) -> json-serialisable dict`.

Length convention: all length inputs/outputs across the wire are in MILLIMETRES.
Fusion's internal unit is centimetres, so we multiply by MM on the way in and
divide by MM on the way out. Angles are in DEGREES on the wire.
"""
import base64
import contextlib
import csv
import io
import math
import os
import re
import tempfile
import time
import traceback

import adsk.core
import adsk.fusion
import logutil
from registry import Registry

VERSION = '1.6.0'
MM = 0.1  # 1 mm = 0.1 cm (Fusion internal length unit)

_registry = Registry()

# get_state / query_entities cache. Invalidated by a mutation generation counter
# (bumped in dispatch after any non-read-only op) combined with a cheap structural
# signature so edits made directly in the Fusion UI also bust the cache.
_state_cache = {}
_mutation_gen = 0

_READ_ONLY_OPS = frozenset({
    'ping', 'server_info', 'get_state', 'query_entities', 'list_parameters',
    'measure', 'bounding_box', 'center_of_mass', 'interference', 'screenshot',
    'fit_view', 'timeline', 'bom', 'mesh_info',
    'get_selection', 'highlight', 'multi_screenshot', 'list_documents',
    'mass_properties', 'cam_setups', 'export_parameters',
})


def _design_signature(app):
    """A cheap fingerprint of design shape/parameters to detect external edits."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return None
    root = design.rootComponent
    try:
        tl = design.timeline.count
    except Exception:
        tl = 0
    params = tuple((prm.name, prm.expression) for prm in design.allParameters)
    return (root.bRepBodies.count, root.sketches.count, root.occurrences.count,
            tl, hash(params))

_OPS = {
    'new': adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    'join': adsk.fusion.FeatureOperations.JoinFeatureOperation,
    'cut': adsk.fusion.FeatureOperations.CutFeatureOperation,
    'intersect': adsk.fusion.FeatureOperations.IntersectFeatureOperation,
}


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #
def _design(app):
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        raise RuntimeError('No active Fusion design. Switch to the DESIGN workspace '
                           'and open or create a document.')
    return design


def _root(app):
    return _design(app).rootComponent


def _vi(real):
    return adsk.core.ValueInput.createByReal(real)


def _pt(x_mm, y_mm, z_mm=0.0):
    return adsk.core.Point3D.create(x_mm * MM, y_mm * MM, z_mm * MM)


def _xyz_mm(point):
    return [round(point.x / MM, 4), round(point.y / MM, 4), round(point.z / MM, 4)]


def _operation(name):
    key = (name or 'new').lower()
    if key not in _OPS:
        raise ValueError('operation must be one of %s, got %r' % (list(_OPS), name))
    return _OPS[key]


def _resolve_plane(app, ref):
    root = _root(app)
    named = {
        'XY': root.xYConstructionPlane,
        'XZ': root.xZConstructionPlane,
        'YZ': root.yZConstructionPlane,
    }
    if isinstance(ref, str) and ref.upper() in named:
        return named[ref.upper()]
    # Otherwise a token to a planar face or construction plane.
    return _registry.get(ref)


def _resolve_axis(app, ref):
    root = _root(app)
    named = {
        'X': root.xConstructionAxis,
        'Y': root.yConstructionAxis,
        'Z': root.zConstructionAxis,
    }
    if isinstance(ref, str) and ref.upper() in named:
        return named[ref.upper()]
    return _registry.get(ref)


def _surface_type(face):
    try:
        st = face.geometry.surfaceType
        names = {
            adsk.core.SurfaceTypes.PlaneSurfaceType: 'plane',
            adsk.core.SurfaceTypes.CylinderSurfaceType: 'cylinder',
            adsk.core.SurfaceTypes.ConeSurfaceType: 'cone',
            adsk.core.SurfaceTypes.SphereSurfaceType: 'sphere',
            adsk.core.SurfaceTypes.TorusSurfaceType: 'torus',
            adsk.core.SurfaceTypes.NurbsSurfaceType: 'nurbs',
        }
        return names.get(st, str(st))
    except Exception:
        return 'unknown'


def _collection(tokens):
    coll = adsk.core.ObjectCollection.create()
    for tok in tokens:
        coll.add(_registry.get(tok))
    return coll


def _feature_result(feat, kind):
    out = {'feature': _registry.add('ftr', feat), 'kind': kind, 'bodies': []}
    try:
        for body in feat.bodies:
            out['bodies'].append({
                'token': _registry.add('bdy', body),
                'name': body.name,
                'faces': body.faces.count,
                'edges': body.edges.count,
            })
    except Exception:
        pass
    return out


def _profiles_summary(sketch, include_area=False):
    # areaProperties() runs a solve per profile; skip unless explicitly asked
    # (cheaper sketching, esp. on Personal-tier hardware).
    profiles = []
    for i in range(sketch.profiles.count):
        prof = sketch.profiles.item(i)
        item = {'token': _registry.add('prf', prof), 'index': i}
        if include_area:
            try:
                item['area_mm2'] = round(prof.areaProperties().area / (MM * MM), 4)
            except Exception:
                item['area_mm2'] = None
        profiles.append(item)
    return {'sketch': _registry.add('skt', sketch), 'profiles': profiles}


# --------------------------------------------------------------------------- #
# State / inspection
# --------------------------------------------------------------------------- #
def op_ping(app, p):
    return {'pong': True, 'version': VERSION}


def op_server_info(app, p):
    """Report version, uptime and per-operation telemetry (calls/avg/max ms)."""
    info = {'version': VERSION, 'op_count': len(DISPATCH)}
    info.update(logutil.stats_snapshot())
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        info['has_active_design'] = design is not None
    except Exception:
        info['has_active_design'] = False
    return info


def op_get_state(app, p):
    design = _design(app)
    root = design.rootComponent
    # physicalProperties.volume triggers a mass-properties solve per body, which
    # is the slowest part of get_state on big models — opt-in only.
    include_mass = bool(p.get('include_mass_props', False))

    cache_key = ('gs', _mutation_gen, _design_signature(app), include_mass)
    if cache_key in _state_cache:
        return _state_cache[cache_key]

    bodies = []
    for body in root.bRepBodies:
        entry = {
            'token': _registry.add('bdy', body),
            'name': body.name,
            'is_solid': body.isSolid,
            'faces': body.faces.count,
            'edges': body.edges.count,
            'visible': body.isVisible,
        }
        if include_mass and body.isSolid:
            try:
                entry['volume_mm3'] = round(body.physicalProperties.volume / (MM ** 3), 3)
            except Exception:
                entry['volume_mm3'] = None
        bodies.append(entry)

    sketches = []
    for sk in root.sketches:
        sketches.append({
            'token': _registry.add('skt', sk),
            'name': sk.name,
            'profiles': sk.profiles.count,
        })

    params = []
    for prm in design.allParameters:
        params.append({'name': prm.name, 'value_internal': prm.value,
                       'expression': prm.expression, 'unit': prm.unit})

    direct = design.designType == adsk.fusion.DesignTypes.DirectDesignType
    state = {
        'document': app.activeDocument.name if app.activeDocument else None,
        'length_units': design.unitsManager.defaultLengthUnits,
        'wire_length_unit': 'mm',
        'design_type': 'direct' if direct else 'parametric',
        'component_count': design.allComponents.count,
        'bodies': bodies,
        'sketches': sketches,
        'parameters': params,
    }
    _state_cache[cache_key] = state
    return state


def op_query_entities(app, p):
    kind = p.get('kind', 'bodies')
    target = p.get('target')
    # Face area / profile area are solves; skip unless requested.
    include_mass = bool(p.get('include_mass_props', False))
    root = _root(app)

    cache_key = ('qe', _mutation_gen, _design_signature(app), kind, target, include_mass)
    if cache_key in _state_cache:
        return _state_cache[cache_key]

    out = []

    if kind == 'bodies':
        for body in root.bRepBodies:
            out.append({'token': _registry.add('bdy', body), 'name': body.name,
                        'faces': body.faces.count, 'edges': body.edges.count})
    elif kind == 'sketches':
        for sk in root.sketches:
            out.append({'token': _registry.add('skt', sk), 'name': sk.name,
                        'profiles': sk.profiles.count})
    elif kind == 'profiles':
        sk = _registry.get(target)
        for i in range(sk.profiles.count):
            prof = sk.profiles.item(i)
            item = {'token': _registry.add('prf', prof), 'index': i}
            if include_mass:
                try:
                    item['area_mm2'] = round(prof.areaProperties().area / (MM * MM), 4)
                except Exception:
                    item['area_mm2'] = None
            out.append(item)
    elif kind == 'faces':
        body = _registry.get(target)
        for face in body.faces:
            item = {
                'token': _registry.add('fac', face),
                'type': _surface_type(face),
                'centroid_mm': _xyz_mm(face.centroid),
            }
            if include_mass:
                item['area_mm2'] = round(face.area / (MM * MM), 3)
            out.append(item)
    elif kind == 'edges':
        body = _registry.get(target)
        for edge in body.edges:
            item = {'token': _registry.add('edg', edge),
                    'length_mm': round(edge.length / MM, 3)}
            try:
                if edge.startVertex and edge.endVertex:
                    item['start_mm'] = _xyz_mm(edge.startVertex.geometry)
                    item['end_mm'] = _xyz_mm(edge.endVertex.geometry)
            except Exception:
                pass
            out.append(item)
    elif kind == 'occurrences':
        for occ in root.occurrences:
            out.append({'token': _registry.add('occ', occ), 'name': occ.name,
                        'component': _registry.add('cmp', occ.component),
                        'bodies': occ.bRepBodies.count})
    elif kind == 'meshes':
        for m in root.meshBodies:
            out.append({'token': _registry.add('msh', m), 'name': m.name})
    else:
        raise ValueError('kind must be bodies|sketches|profiles|faces|edges|'
                         'occurrences|meshes, got %r' % kind)

    result = {'kind': kind, 'count': len(out), 'entities': out}
    _state_cache[cache_key] = result
    return result


# --------------------------------------------------------------------------- #
# Sketching
# --------------------------------------------------------------------------- #
def op_create_sketch(app, p):
    plane = _resolve_plane(app, p.get('plane', 'XY'))
    sk = _root(app).sketches.add(plane)
    if p.get('name'):
        sk.name = p['name']
    return {'sketch': _registry.add('skt', sk), 'name': sk.name}


def op_sketch_rectangle(app, p):
    sk = _registry.get(p['sketch'])
    rect = sk.sketchCurves.sketchLines.addTwoPointRectangle(
        _pt(p['x1'], p['y1']), _pt(p['x2'], p['y2']))
    out = _profiles_summary(sk)
    # Tokenise the four edges so they can be constrained / dimensioned.
    out['lines'] = [_registry.add('lin', rect.item(i)) for i in range(rect.count)]
    return out


def op_sketch_circle(app, p):
    sk = _registry.get(p['sketch'])
    circle = sk.sketchCurves.sketchCircles.addByCenterRadius(
        _pt(p['cx'], p['cy']), p['r'] * MM)
    out = _profiles_summary(sk)
    out['circle'] = _registry.add('cir', circle)
    out['center'] = _registry.add('spt', circle.centerSketchPoint)
    return out


def op_sketch_line(app, p):
    sk = _registry.get(p['sketch'])
    line = sk.sketchCurves.sketchLines.addByTwoPoints(
        _pt(p['x1'], p['y1']), _pt(p['x2'], p['y2']))
    return {'sketch': _registry.add('skt', sk),
            'line': _registry.add('lin', line),
            'profiles': _profiles_summary(sk)['profiles']}


def op_sketch_arc(app, p):
    sk = _registry.get(p['sketch'])
    arc = sk.sketchCurves.sketchArcs.addByCenterStartSweep(
        _pt(p['cx'], p['cy']),
        _pt(p['start_x'], p['start_y']),
        math.radians(p['sweep_deg']))
    return {'sketch': _registry.add('skt', sk),
            'arc': _registry.add('arc', arc),
            'profiles': _profiles_summary(sk)['profiles']}


def op_sketch_polygon(app, p):
    sk = _registry.get(p['sketch'])
    cx, cy, r, n = p['cx'], p['cy'], p['r'], int(p['sides'])
    if n < 3:
        raise ValueError('polygon needs at least 3 sides')
    start = math.radians(p.get('start_angle', 0))
    pts = [_pt(cx + r * math.cos(start + 2 * math.pi * i / n),
               cy + r * math.sin(start + 2 * math.pi * i / n)) for i in range(n)]
    lines = sk.sketchCurves.sketchLines
    for i in range(n):
        lines.addByTwoPoints(pts[i], pts[(i + 1) % n])
    return _profiles_summary(sk)


def _points_param(p):
    pts = p.get('points') or []
    if len(pts) < 2:
        raise ValueError('need at least 2 points')
    return pts


def op_sketch_points(app, p):
    """Add many sketch points (mm) in one call. points: [[x,y], ...]. Returns a
    point token per input point."""
    sk = _registry.get(p['sketch'])
    tokens = [_registry.add('spt', _sketch_point(sk, xy[0], xy[1]))
              for xy in (p.get('points') or [])]
    return {'sketch': _registry.add('skt', sk), 'points': tokens}


def op_sketch_polyline(app, p):
    """Add a connected polyline through points (mm) in one call. points:
    [[x,y], ...]. closed=True joins the last point back to the first. Returns the
    line tokens and updated profiles."""
    sk = _registry.get(p['sketch'])
    pts = _points_param(p)
    lines = sk.sketchCurves.sketchLines
    toks = []
    n = len(pts)
    last = n if p.get('closed') else n - 1
    for i in range(last):
        a, b = pts[i], pts[(i + 1) % n]
        seg = lines.addByTwoPoints(_pt(a[0], a[1]), _pt(b[0], b[1]))
        toks.append(_registry.add('lin', seg))
    out = _profiles_summary(sk)
    out['lines'] = toks
    return out


def op_sketch_spline(app, p):
    """Add a fitted spline through points (mm) in one call. points: [[x,y], ...]."""
    sk = _registry.get(p['sketch'])
    pts = _points_param(p)
    coll = adsk.core.ObjectCollection.create()
    for xy in pts:
        coll.add(_pt(xy[0], xy[1]))
    spline = sk.sketchCurves.sketchFittedSplines.add(coll)
    out = _profiles_summary(sk)
    out['spline'] = _registry.add('spl', spline)
    return out


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
def op_extrude(app, p):
    prof = _registry.get(p['profile'])
    feats = _root(app).features.extrudeFeatures
    ein = feats.createInput(prof, _operation(p.get('operation', 'new')))
    if p.get('to_face'):
        # Extrude up to a face/body instead of a fixed distance.
        extent = adsk.fusion.ToEntityExtentDefinition.create(
            _registry.get(p['to_face']), False)
        ein.setOneSideExtent(
            extent, adsk.fusion.ExtentDirections.PositiveExtentDirection)
    else:
        distance = _vi(p['distance'] * MM)
        if p.get('symmetric'):
            ein.setSymmetricExtent(distance, True)  # distance = full length
        else:
            ein.setDistanceExtent(False, distance)
    if p.get('taper_angle'):
        ein.taperAngle = _vi(math.radians(p['taper_angle']))
    return _feature_result(feats.add(ein), 'extrude')


def op_revolve(app, p):
    prof = _registry.get(p['profile'])
    axis = _resolve_axis(app, p['axis'])
    feats = _root(app).features.revolveFeatures
    rin = feats.createInput(prof, axis, _operation(p.get('operation', 'new')))
    rin.setAngleExtent(False, _vi(math.radians(p.get('angle', 360))))
    return _feature_result(feats.add(rin), 'revolve')


def op_fillet(app, p):
    feats = _root(app).features.filletFeatures
    fin = feats.createInput()
    fin.addConstantRadiusEdgeSet(_collection(p['edges']), _vi(p['radius'] * MM), True)
    return _feature_result(feats.add(fin), 'fillet')


def op_chamfer(app, p):
    feats = _root(app).features.chamferFeatures
    edges = _collection(p['edges'])
    dist = _vi(p['distance'] * MM)
    try:
        cin = feats.createInput2()
        cin.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, dist, True)
    except Exception:
        cin = feats.createInput(edges, True)
        cin.setToEqualDistance(dist)
    return _feature_result(feats.add(cin), 'chamfer')


def op_shell(app, p):
    feats = _root(app).features.shellFeatures
    sin = feats.createInput(_collection(p.get('faces', [])), False)
    sin.insideThickness = _vi(p['thickness'] * MM)
    return _feature_result(feats.add(sin), 'shell')


def op_combine(app, p):
    target = _registry.get(p['target'])
    feats = _root(app).features.combineFeatures
    cin = feats.createInput(target, _collection(p['tools']))
    cin.operation = _operation(p.get('operation', 'join'))
    cin.isKeepToolBodies = bool(p.get('keep_tools', False))
    return _feature_result(feats.add(cin), 'combine')


def op_rectangular_pattern(app, p):
    feats = _root(app).features.rectangularPatternFeatures
    spacing_type = adsk.fusion.PatternDistanceType.SpacingPatternDistanceType
    pin = feats.createInput(
        _collection(p['entities']),
        _resolve_axis(app, p.get('direction1', 'X')),
        _vi(int(p['count1'])),
        _vi(p['spacing1'] * MM),
        spacing_type)
    if p.get('count2'):
        pin.setDirectionTwo(
            _resolve_axis(app, p.get('direction2', 'Y')),
            _vi(int(p['count2'])),
            _vi(p.get('spacing2', p['spacing1']) * MM))
    return _feature_result(feats.add(pin), 'rectangular_pattern')


def op_circular_pattern(app, p):
    feats = _root(app).features.circularPatternFeatures
    pin = feats.createInput(_collection(p['entities']), _resolve_axis(app, p['axis']))
    pin.quantity = _vi(int(p['count']))
    pin.totalAngle = _vi(math.radians(p.get('angle', 360)))
    pin.isSymmetric = bool(p.get('symmetric', False))
    return _feature_result(feats.add(pin), 'circular_pattern')


def op_mirror(app, p):
    feats = _root(app).features.mirrorFeatures
    min_ = feats.createInput(_collection(p['entities']), _resolve_plane(app, p['plane']))
    return _feature_result(feats.add(min_), 'mirror')


def op_move_body(app, p):
    body = _registry.get(p['body'])
    ents = adsk.core.ObjectCollection.create()
    ents.add(body)
    transform = adsk.core.Matrix3D.create()
    transform.translation = adsk.core.Vector3D.create(
        p.get('dx', 0) * MM, p.get('dy', 0) * MM, p.get('dz', 0) * MM)
    feats = _root(app).features.moveFeatures
    return _feature_result(feats.add(feats.createInput(ents, transform)), 'move')


def op_delete(app, p):
    obj = _registry.get(p['token'])
    obj.deleteMe()
    return {'deleted': p['token']}


# --------------------------------------------------------------------------- #
# Holes
# --------------------------------------------------------------------------- #
def _sketch_point(sk, x_mm, y_mm):
    """Add a sketch point (mm) and return the live SketchPoint."""
    return sk.sketchPoints.add(_pt(x_mm, y_mm))


def _hole_extent(hin, p):
    if p.get('through_all'):
        hin.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
    else:
        hin.setDistanceExtent(_vi(p['depth'] * MM))


def op_hole(app, p):
    """Create a hole (simple|counterbore|countersink) positioned at a point on a
    sketch. Params: sketch, x, y (mm), diameter (mm); depth (mm) or
    through_all=True; kind and its extra dims (cbore_diameter/cbore_depth or
    csink_diameter/csink_angle)."""
    root = _root(app)
    sk = _registry.get(p['sketch'])
    pt = _sketch_point(sk, p['x'], p['y'])
    holes = root.features.holeFeatures
    kind = (p.get('kind') or 'simple').lower()
    dia = _vi(p['diameter'] * MM)
    if kind == 'simple':
        hin = holes.createSimpleInput(dia)
    elif kind == 'counterbore':
        hin = holes.createCounterboreInput(
            dia, _vi(p['cbore_diameter'] * MM), _vi(p['cbore_depth'] * MM))
    elif kind == 'countersink':
        hin = holes.createCountersinkInput(
            dia, _vi(p['csink_diameter'] * MM), _vi(math.radians(p.get('csink_angle', 90))))
    else:
        raise ValueError('kind must be simple|counterbore|countersink, got %r' % kind)
    hin.setPositionBySketchPoint(pt)
    _hole_extent(hin, p)
    return _feature_result(holes.add(hin), 'hole')


# --------------------------------------------------------------------------- #
# Construction geometry
# --------------------------------------------------------------------------- #
def op_construction_plane(app, p):
    """Create a construction plane. method: offset (base plane/face + offset mm),
    angle (base + edge/axis + angle deg), three_points (3 point tokens),
    tangent (cylindrical face + optional angle)."""
    root = _root(app)
    planes = root.constructionPlanes
    cin = planes.createInput()
    method = (p.get('method') or 'offset').lower()
    if method == 'offset':
        cin.setByOffset(_resolve_plane(app, p.get('base', 'XY')), _vi(p['offset'] * MM))
    elif method == 'angle':
        cin.setByAngle(_resolve_axis(app, p['axis']),
                       _vi(math.radians(p['angle'])),
                       _resolve_plane(app, p.get('base', 'XY')))
    elif method == 'three_points':
        pts = [_registry.get(t) for t in p['points']]
        cin.setByThreePoints(pts[0], pts[1], pts[2])
    elif method == 'tangent':
        cin.setByTangent(_registry.get(p['face']),
                         _vi(math.radians(p.get('angle', 0))),
                         _resolve_plane(app, p.get('base', 'XY')))
    else:
        raise ValueError('method must be offset|angle|three_points|tangent, got %r' % method)
    plane = planes.add(cin)
    return {'plane': _registry.add('pln', plane), 'method': method}


def op_construction_axis(app, p):
    """Create a construction axis. method: two_points (2 point tokens), edge
    (linear edge token), cylinder (cylindrical/conical face token)."""
    root = _root(app)
    axes = root.constructionAxes
    ain = axes.createInput()
    method = (p.get('method') or 'edge').lower()
    if method == 'two_points':
        pts = [_registry.get(t) for t in p['points']]
        ain.setByTwoPoints(pts[0], pts[1])
    elif method == 'edge':
        ain.setByLine(_registry.get(p['edge']))
    elif method == 'cylinder':
        ain.setByCircularFace(_registry.get(p['face']))
    else:
        raise ValueError('method must be two_points|edge|cylinder, got %r' % method)
    axis = axes.add(ain)
    return {'axis': _registry.add('cax', axis), 'method': method}


def op_construction_point(app, p):
    """Create a construction point. method: at_point (vertex/sketch-point token),
    two_edges (2 edge tokens), edge_plane (edge token + plane)."""
    root = _root(app)
    pts = root.constructionPoints
    cin = pts.createInput()
    method = (p.get('method') or 'at_point').lower()
    if method == 'at_point':
        cin.setByPoint(_registry.get(p['point']))
    elif method == 'two_edges':
        edges = [_registry.get(t) for t in p['edges']]
        cin.setByTwoEdges(edges[0], edges[1])
    elif method == 'edge_plane':
        cin.setByEdgeAndPlane(_registry.get(p['edge']), _resolve_plane(app, p['plane']))
    else:
        raise ValueError('method must be at_point|two_edges|edge_plane, got %r' % method)
    point = pts.add(cin)
    return {'point': _registry.add('cpt', point), 'method': method}


# --------------------------------------------------------------------------- #
# Sketch constraints, dimensions and editing
# --------------------------------------------------------------------------- #
def op_sketch_constraint(app, p):
    """Add a geometric constraint to sketch geometry (curve/point tokens).

    kind: horizontal|vertical (one line), parallel|perpendicular|equal|collinear
    (two lines), tangent|concentric (two curves), coincident (point + curve/point),
    midpoint (point + line)."""
    sk = _registry.get(p['sketch'])
    gc = sk.geometricConstraints
    kind = (p.get('kind') or '').lower()
    ents = [_registry.get(t) for t in p.get('entities', [])]
    if kind == 'horizontal':
        c = gc.addHorizontal(ents[0])
    elif kind == 'vertical':
        c = gc.addVertical(ents[0])
    elif kind == 'parallel':
        c = gc.addParallel(ents[0], ents[1])
    elif kind == 'perpendicular':
        c = gc.addPerpendicular(ents[0], ents[1])
    elif kind == 'equal':
        c = gc.addEqual(ents[0], ents[1])
    elif kind == 'collinear':
        c = gc.addCollinear(ents[0], ents[1])
    elif kind == 'tangent':
        c = gc.addTangent(ents[0], ents[1])
    elif kind == 'concentric':
        c = gc.addConcentric(ents[0], ents[1])
    elif kind == 'coincident':
        c = gc.addCoincident(ents[0], ents[1])
    elif kind == 'midpoint':
        c = gc.addMidPoint(ents[0], ents[1])
    else:
        raise ValueError('unsupported constraint kind %r' % kind)
    return {'constraint': _registry.add('con', c), 'kind': kind}


def op_sketch_dimension(app, p):
    """Add a driving dimension to a sketch. kind: distance (2 point/line tokens +
    at x,y mm text position), radius|diameter (circle/arc token), angle (2 lines).
    Optional parameter=name renames the dimension's parameter."""
    sk = _registry.get(p['sketch'])
    dims = sk.sketchDimensions
    kind = (p.get('kind') or 'distance').lower()
    ents = [_registry.get(t) for t in p.get('entities', [])]
    tx = _pt(p.get('text_x', 0), p.get('text_y', 0))
    if kind == 'distance':
        orient = adsk.fusion.DimensionOrientations.AlignedDimensionOrientation
        d = dims.addDistanceDimension(ents[0], ents[1], orient, tx)
    elif kind == 'radius':
        d = dims.addRadialDimension(ents[0], tx)
    elif kind == 'diameter':
        d = dims.addDiameterDimension(ents[0], tx)
    elif kind == 'angle':
        d = dims.addAngularDimension(ents[0], ents[1], tx)
    else:
        raise ValueError('kind must be distance|radius|diameter|angle, got %r' % kind)
    if p.get('parameter') and d.parameter:
        d.parameter.name = p['parameter']
    return {'dimension': _registry.add('dim', d), 'kind': kind,
            'parameter': d.parameter.name if d.parameter else None}


def op_project_to_sketch(app, p):
    """Project edges/faces/vertices (tokens) onto a sketch, returning new
    projected sketch curve tokens."""
    sk = _registry.get(p['sketch'])
    projected = []
    for tok in p.get('entities', []):
        ents = sk.project(_registry.get(tok))
        for i in range(ents.count):
            projected.append(_registry.add('prj', ents.item(i)))
    return {'sketch': _registry.add('skt', sk), 'projected': projected}


def op_sketch_offset(app, p):
    """Offset sketch curves (tokens) by `distance` mm, returning new curve tokens.
    direction_point (x,y mm) picks which side to offset toward."""
    sk = _registry.get(p['sketch'])
    curves = _collection(p['curves'])
    dir_pt = _pt(p.get('dir_x', 0), p.get('dir_y', 0))
    created = sk.offset(curves, dir_pt, p['distance'] * MM)
    return {'sketch': _registry.add('skt', sk),
            'curves': [_registry.add('off', created.item(i)) for i in range(created.count)]}


def op_sketch_fillet(app, p):
    """Add a 2D fillet of `radius` mm between two sketch lines that share an
    endpoint (line tokens)."""
    sk = _registry.get(p['sketch'])
    l0, l1 = _registry.get(p['line1']), _registry.get(p['line2'])
    # Anchor the fillet near each line's endpoint closest to the shared corner.
    arc = sk.sketchCurves.sketchArcs.addFillet(
        l0, l0.endSketchPoint.geometry, l1, l1.startSketchPoint.geometry, p['radius'] * MM)
    return {'sketch': _registry.add('skt', sk), 'arc': _registry.add('arc', arc)}


# --------------------------------------------------------------------------- #
# Advanced features (loft / sweep / rib / draft / thread / split)
# --------------------------------------------------------------------------- #
def op_loft(app, p):
    """Loft through 2+ profile tokens. Optional `rails` (curve/edge tokens) guide
    the shape. operation: new|join|cut|intersect."""
    feats = _root(app).features.loftFeatures
    lin = feats.createInput(_operation(p.get('operation', 'new')))
    profiles = p.get('profiles', [])
    if len(profiles) < 2:
        raise ValueError('loft needs at least 2 profile tokens')
    for tok in profiles:
        lin.loftSections.add(_registry.get(tok))
    for tok in p.get('rails', []):
        lin.centerLineOrRails.addRail(_registry.get(tok))
    return _feature_result(feats.add(lin), 'loft')


def op_sweep(app, p):
    """Sweep a profile token along a path (curve/edge token). Optional
    twist_angle (deg). operation: new|join|cut|intersect."""
    root = _root(app)
    feats = root.features.sweepFeatures
    path = root.features.createPath(_registry.get(p['path']))
    sin = feats.createInput(_registry.get(p['profile']), path,
                            _operation(p.get('operation', 'new')))
    if p.get('twist_angle'):
        sin.twistAngle = _vi(math.radians(p['twist_angle']))
    return _feature_result(feats.add(sin), 'sweep')


def op_rib(app, p):
    """Create a rib from open sketch profile curve tokens with `thickness` mm.
    symmetric centres the thickness on the sketch curves."""
    feats = _root(app).features.ribFeatures
    curves = _collection(p['curves'])
    rin = feats.createInput(curves, _vi(p['thickness'] * MM),
                            bool(p.get('symmetric', True)))
    if p.get('depth'):
        rin.setTwoSidesToExtent(_vi(p['depth'] * MM))
    return _feature_result(feats.add(rin), 'rib')


def op_draft(app, p):
    """Apply a draft `angle` deg to face tokens, pulled relative to a neutral
    plane (plane name or planar-face token)."""
    feats = _root(app).features.draftFeatures
    faces = _collection(p['faces'])
    plane = _resolve_plane(app, p['neutral_plane'])
    din = feats.createInput(faces, plane, bool(p.get('tangent_chain', True)))
    din.isTangentChain = bool(p.get('tangent_chain', True))
    din.setSingleAngle(True, _vi(math.radians(p['angle'])))
    return _feature_result(feats.add(din), 'draft')


def op_thread(app, p):
    """Add a thread to a cylindrical face token. modeled=True cuts real geometry
    (slower); False is a cosmetic thread. Uses Fusion's recommended thread data
    for the face diameter."""
    feats = _root(app).features.threadFeatures
    face = _registry.get(p['face'])
    query = feats.threadDataQuery
    thread_type = query.defaultMetricThreadType
    is_internal = bool(p.get('internal', False))
    # recommendThreadData returns (ok, designation, size, class) for the face.
    ok, designation, size, cls = query.recommendThreadData(
        face.geometry.radius * 2, is_internal, thread_type)
    if not ok:
        raise RuntimeError('No recommended thread data for this face diameter')
    info = feats.createThreadInfo(is_internal, thread_type, size, designation, cls)
    tin = feats.createInput(face, info)
    tin.isModeled = bool(p.get('modeled', True))
    return _feature_result(feats.add(tin), 'thread')


def op_split_body(app, p):
    """Split a body token with a splitting tool: a body/face token, or a plane
    name ("XY"/"XZ"/"YZ") / construction-plane token."""
    feats = _root(app).features.splitBodyFeatures
    tool = _registry.get_opt(p['tool']) or _resolve_plane(app, p['tool'])
    sin = feats.createInput(_registry.get(p['body']), tool,
                            bool(p.get('extend_tool', True)))
    return _feature_result(feats.add(sin), 'split_body')


def op_offset_face(app, p):
    """Press-pull: offset face tokens by `distance` mm (negative pushes in).
    Quick thickness/clearance tweaks without editing sketches."""
    feats = _root(app).features.offsetFacesFeatures
    return _feature_result(
        feats.add(feats.createInput(_collection(p['faces']),
                                    _vi(p['distance'] * MM))),
        'offset_face')


def op_scale(app, p):
    """Uniformly scale bodies/components (tokens) by `factor` about a point
    (token; default: the origin). E.g. fix an STL imported in the wrong unit."""
    feats = _root(app).features.scaleFeatures
    point = (_registry.get(p['point']) if p.get('point')
             else _root(app).originConstructionPoint)
    sin = feats.createInput(_collection(p['entities']), point,
                            _vi(float(p['factor'])))
    return _feature_result(feats.add(sin), 'scale')


def op_thicken(app, p):
    """Thicken surface faces (tokens) into a solid, `thickness` mm (symmetric
    centres it). The solid counterpart for surface lofts/sweeps/patches."""
    feats = _root(app).features.thickenFeatures
    faces = _collection(p['faces'])
    tin = feats.createInput(faces, _vi(p['thickness'] * MM),
                            bool(p.get('symmetric', False)),
                            _operation(p.get('operation', 'new')),
                            bool(p.get('chain', True)))
    return _feature_result(feats.add(tin), 'thicken')


# --------------------------------------------------------------------------- #
# Assemblies: components, occurrences, joints, rename, copy
# --------------------------------------------------------------------------- #
def op_create_component(app, p):
    """Create a new empty component as an occurrence under the root. Optional
    name. Returns component + occurrence tokens."""
    root = _root(app)
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    if p.get('name'):
        comp.name = p['name']
    return {'component': _registry.add('cmp', comp),
            'occurrence': _registry.add('occ', occ), 'name': comp.name}


def op_rename(app, p):
    """Rename any named entity by token (body, sketch, component, feature,
    occurrence, parameter). Sets `.name` to `new_name`."""
    obj = _registry.get(p['token'])
    obj.name = p['new_name']
    return {'token': p['token'], 'name': obj.name}


def op_copy_body(app, p):
    """Copy body tokens into a target component/occurrence token (or root if
    omitted) using copy/paste. Returns tokens of the pasted bodies."""
    root = _root(app)
    bodies = _collection(p['bodies'])
    target = _registry.get_opt(p.get('target')) if p.get('target') else root
    result = root.features.copyPasteBodies.add(bodies)
    out = []
    try:
        for i in range(result.bodies.count):
            out.append(_registry.add('bdy', result.bodies.item(i)))
    except Exception:
        pass
    _ = target
    return {'bodies': out}


_JOINT_MOTION = ('rigid', 'revolute', 'slider', 'cylindrical', 'pin_slot',
                 'planar', 'ball')


def _joint_geometry(token):
    """Build a JointGeometry from a planar-face token (centre keypoint) or fall
    back to a curve/edge token."""
    obj = _registry.get(token)
    key = adsk.fusion.JointKeyPointTypes.CenterKeyPoint
    try:
        return adsk.fusion.JointGeometry.createByPlanarFace(obj, None, key)
    except Exception:
        return adsk.fusion.JointGeometry.createByCurve(obj, key)


def op_joint(app, p):
    """Create a joint between two geometry tokens (planar faces recommended).
    motion: rigid|revolute|slider|cylindrical|pin_slot|planar|ball. For
    revolute/cylindrical an axis ("X"/"Y"/"Z") sets the rotation axis."""
    root = _root(app)
    geo0 = _joint_geometry(p['geo0'])
    geo1 = _joint_geometry(p['geo1'])
    jin = root.joints.createInput(geo0, geo1)
    motion = (p.get('motion') or 'rigid').lower()
    if motion not in _JOINT_MOTION:
        raise ValueError('motion must be one of %s, got %r' % (list(_JOINT_MOTION), motion))
    axis_map = {
        'X': adsk.fusion.JointDirections.XAxisJointDirection,
        'Y': adsk.fusion.JointDirections.YAxisJointDirection,
        'Z': adsk.fusion.JointDirections.ZAxisJointDirection,
    }
    axis = axis_map.get((p.get('axis') or 'Z').upper(), axis_map['Z'])
    if motion == 'revolute':
        jin.setAsRevoluteJointMotion(axis)
    elif motion == 'slider':
        jin.setAsSliderJointMotion(axis)
    elif motion == 'cylindrical':
        jin.setAsCylindricalJointMotion(axis)
    elif motion == 'planar':
        jin.setAsPlanarJointMotion(axis)
    elif motion == 'ball':
        jin.setAsBallJointMotion(
            adsk.fusion.JointDirections.ZAxisJointDirection,
            adsk.fusion.JointDirections.XAxisJointDirection)
    elif motion == 'pin_slot':
        jin.setAsPinSlotJointMotion(axis, axis_map['X'])
    else:
        jin.setAsRigidJointMotion()
    joint = root.joints.add(jin)
    return {'joint': _registry.add('jnt', joint), 'motion': motion}


# --------------------------------------------------------------------------- #
# Assembly motion: driving joints, limits, occurrence transform, grounding
# --------------------------------------------------------------------------- #
def op_drive_joint(app, p):
    """Set a joint's motion value: rotation (deg) for revolute/cylindrical,
    slide (mm) for slider/cylindrical. kind: auto|rotation|slide. Combine with
    interference + multi_screenshot to check a mechanism through its range."""
    joint = _registry.get(p['joint'])
    motion = joint.jointMotion
    kind = (p.get('kind') or 'auto').lower()
    value = float(p['value'])
    out = {'joint': p['joint']}
    if kind in ('auto', 'rotation') and hasattr(motion, 'rotationValue'):
        motion.rotationValue = math.radians(value)
        out['rotation_deg'] = round(math.degrees(motion.rotationValue), 4)
    elif kind in ('auto', 'slide') and hasattr(motion, 'slideValue'):
        motion.slideValue = value * MM
        out['slide_mm'] = round(motion.slideValue / MM, 4)
    else:
        raise ValueError('Joint has no %s motion (motion type: %s)'
                         % (kind, type(motion).__name__))
    return out


def op_set_joint_limits(app, p):
    """Set limits on a joint's motion. kind: rotation (deg) or slide (mm);
    min/max/rest are optional — omitted ones stay untouched."""
    joint = _registry.get(p['joint'])
    motion = joint.jointMotion
    kind = (p.get('kind') or 'rotation').lower()
    if kind == 'rotation':
        limits, conv = getattr(motion, 'rotationLimits', None), math.radians
    elif kind == 'slide':
        limits, conv = getattr(motion, 'slideLimits', None), lambda v: v * MM
    else:
        raise ValueError('kind must be rotation|slide, got %r' % kind)
    if limits is None:
        raise ValueError('Joint has no %s limits (motion type: %s)'
                         % (kind, type(motion).__name__))
    if p.get('min') is not None:
        limits.isMinimumValueEnabled = True
        limits.minimumValue = conv(float(p['min']))
    if p.get('max') is not None:
        limits.isMaximumValueEnabled = True
        limits.maximumValue = conv(float(p['max']))
    if p.get('rest') is not None:
        limits.isRestValueEnabled = True
        limits.restValue = conv(float(p['rest']))
    return {'joint': p['joint'], 'kind': kind}


def op_move_occurrence(app, p):
    """Move/rotate a whole occurrence (component instance): dx/dy/dz in mm,
    rx/ry/rz in deg about world axes through the occurrence origin. This is the
    assembly-level counterpart of move_body."""
    occ = _registry.get(p['occurrence'])
    t = occ.transform2 if hasattr(occ, 'transform2') else occ.transform
    origin = t.translation
    delta = adsk.core.Matrix3D.create()
    for vec, ang in (((1, 0, 0), p.get('rx', 0)), ((0, 1, 0), p.get('ry', 0)),
                     ((0, 0, 1), p.get('rz', 0))):
        if ang:
            rot = adsk.core.Matrix3D.create()
            rot.setToRotation(math.radians(ang),
                              adsk.core.Vector3D.create(*vec),
                              adsk.core.Point3D.create(origin.x, origin.y, origin.z))
            delta.transformBy(rot)
    if p.get('dx') or p.get('dy') or p.get('dz'):
        tr = adsk.core.Matrix3D.create()
        tr.translation = adsk.core.Vector3D.create(
            p.get('dx', 0) * MM, p.get('dy', 0) * MM, p.get('dz', 0) * MM)
        delta.transformBy(tr)
    t.transformBy(delta)
    if hasattr(occ, 'transform2'):
        occ.transform2 = t
    else:
        occ.transform = t
    new_origin = (occ.transform2 if hasattr(occ, 'transform2') else occ.transform).translation
    return {'occurrence': p['occurrence'],
            'origin_mm': [round(new_origin.x / MM, 4), round(new_origin.y / MM, 4),
                          round(new_origin.z / MM, 4)]}


def op_ground_occurrence(app, p):
    """Ground (anchor, default) or unground an occurrence so joints move the
    other parts relative to it."""
    occ = _registry.get(p['occurrence'])
    occ.isGrounded = bool(p.get('grounded', True))
    return {'occurrence': p['occurrence'], 'grounded': occ.isGrounded}


# --------------------------------------------------------------------------- #
# Cloud data panel: projects, documents
# --------------------------------------------------------------------------- #
def op_list_documents(app, p):
    """List cloud projects and the documents in their root folders (data-panel
    view). Optional project name filter. Cloud calls can be slow on first use."""
    data = app.data
    target = p.get('project')
    projects = []
    for i in range(data.dataProjects.count):
        proj = data.dataProjects.item(i)
        if target and proj.name != target:
            continue
        docs = []
        try:
            files = proj.rootFolder.dataFiles
            for j in range(files.count):
                df = files.item(j)
                entry = {'name': df.name}
                with contextlib.suppress(Exception):
                    entry['type'] = df.fileExtension
                docs.append(entry)
        except Exception as exc:
            docs = [{'error': str(exc)}]
        projects.append({'project': proj.name, 'documents': docs})
    if target and not projects:
        raise RuntimeError('No cloud project named %r' % target)
    return {'count': len(projects), 'projects': projects}


def op_open_document(app, p):
    """Open a cloud document by name (optionally scoped to a project). The
    opened document becomes active; call get_state afterwards."""
    data = app.data
    name = p['name']
    target = p.get('project')
    for i in range(data.dataProjects.count):
        proj = data.dataProjects.item(i)
        if target and proj.name != target:
            continue
        try:
            files = proj.rootFolder.dataFiles
        except Exception:
            continue
        for j in range(files.count):
            df = files.item(j)
            if df.name == name:
                doc = app.documents.open(df)
                return {'opened': doc.name, 'project': proj.name}
    raise RuntimeError('Document %r not found%s. Use list_documents to see '
                       'what is available.'
                       % (name, ' in project %r' % target if target else ''))


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
def op_list_parameters(app, p):
    design = _design(app)
    return {'parameters': [{'name': x.name, 'value_internal': x.value,
                            'expression': x.expression, 'unit': x.unit,
                            'comment': x.comment} for x in design.allParameters]}


def op_set_parameter(app, p):
    design = _design(app)
    prm = design.allParameters.itemByName(p['name'])
    if not prm:
        raise RuntimeError('No parameter named %r' % p['name'])
    if 'expression' in p:
        prm.expression = str(p['expression'])
    elif 'value' in p:
        prm.value = float(p['value'])  # internal units
    else:
        raise ValueError('set_parameter needs "expression" (preferred) or "value"')
    return {'name': prm.name, 'value_internal': prm.value, 'expression': prm.expression}


def op_add_parameter(app, p):
    design = _design(app)
    value = p['value']
    vi = (adsk.core.ValueInput.createByString(str(value))
          if isinstance(value, str) else _vi(float(value)))
    prm = design.userParameters.add(p['name'], vi, p.get('units', 'mm'),
                                    p.get('comment', ''))
    return {'name': prm.name, 'value_internal': prm.value, 'expression': prm.expression}


def op_export_parameters(app, p):
    """Write all parameters to a CSV file (name, kind, expression, unit,
    comment) — edit in a spreadsheet, re-apply with import_parameters."""
    design = _design(app)
    user_names = {prm.name for prm in design.userParameters}
    path = p['csv_path']
    count = 0
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(['name', 'kind', 'expression', 'unit', 'comment'])
        for prm in design.allParameters:
            writer.writerow([prm.name,
                             'user' if prm.name in user_names else 'model',
                             prm.expression, prm.unit, prm.comment or ''])
            count += 1
    return {'csv': path, 'parameters': count}


def op_import_parameters(app, p):
    """Apply parameters from a CSV (columns: name, expression; optional unit,
    comment). Existing parameters get the new expression; unknown names are
    created as user parameters. Reports per-row results."""
    design = _design(app)
    results = []
    with open(p['csv_path'], newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            name = (row.get('name') or '').strip()
            expr = (row.get('expression') or '').strip()
            if not name or not expr:
                continue
            try:
                prm = design.allParameters.itemByName(name)
                if prm:
                    prm.expression = expr
                    results.append({'name': name, 'action': 'updated'})
                else:
                    design.userParameters.add(
                        name, adsk.core.ValueInput.createByString(expr),
                        (row.get('unit') or 'mm').strip(),
                        (row.get('comment') or '').strip())
                    results.append({'name': name, 'action': 'created'})
            except Exception as exc:
                results.append({'name': name, 'action': 'failed',
                                'error': str(exc)})
    return {'count': len(results), 'results': results}


# --------------------------------------------------------------------------- #
# Materials, appearance, measurement, import, timeline
# --------------------------------------------------------------------------- #
def _find_material(app, name, library=None):
    libs = app.materialLibraries
    for i in range(libs.count):
        lib = libs.item(i)
        if library and lib.name != library:
            continue
        try:
            m = lib.materials.itemByName(name)
        except Exception:
            m = None
        if m:
            return m
    raise RuntimeError('Material %r not found in material libraries' % name)


def _find_appearance(app, name, library=None):
    libs = app.materialLibraries
    for i in range(libs.count):
        lib = libs.item(i)
        if library and lib.name != library:
            continue
        try:
            a = lib.appearances.itemByName(name)
        except Exception:
            a = None
        if a:
            return a
    raise RuntimeError('Appearance %r not found in appearance libraries' % name)


def op_set_material(app, p):
    """Assign a physical material (from a material library) to a body token —
    this changes the computed mass. Optional library name to disambiguate."""
    body = _registry.get(p['body'])
    body.material = _find_material(app, p['material'], p.get('library'))
    return {'body': p['body'], 'material': body.material.name}


def op_set_appearance(app, p):
    """Assign an appearance (colour/finish) to a body token. Optional library."""
    body = _registry.get(p['body'])
    body.appearance = _find_appearance(app, p['appearance'], p.get('library'))
    return {'body': p['body'], 'appearance': body.appearance.name}


def op_measure(app, p):
    """Measure between two geometry tokens. kind: distance (minimum distance, mm)
    or angle (degrees)."""
    mm = app.measureManager
    e0, e1 = _registry.get(p['a']), _registry.get(p['b'])
    kind = (p.get('kind') or 'distance').lower()
    if kind == 'distance':
        res = mm.measureMinimumDistance(e0, e1)
        return {'kind': 'distance', 'value_mm': round(res.value / MM, 4)}
    elif kind == 'angle':
        res = mm.measureAngle(e0, e1)
        return {'kind': 'angle', 'value_deg': round(math.degrees(res.value), 4)}
    raise ValueError('kind must be distance|angle, got %r' % kind)


def op_bounding_box(app, p):
    """Axis-aligned bounding box of a body token (or the whole root if omitted),
    in mm: min/max points and x/y/z size."""
    if p.get('body'):
        bb = _registry.get(p['body']).boundingBox
    else:
        bb = _root(app).boundingBox
    lo, hi = _xyz_mm(bb.minPoint), _xyz_mm(bb.maxPoint)
    return {'min_mm': lo, 'max_mm': hi,
            'size_mm': [round(hi[i] - lo[i], 4) for i in range(3)]}


def op_center_of_mass(app, p):
    """Centre of mass of a body token, in mm (requires a mass-properties solve)."""
    com = _registry.get(p['body']).physicalProperties.centerOfMass
    return {'body': p['body'], 'center_mm': _xyz_mm(com)}


def op_mass_properties(app, p):
    """Full mass report for a body token: mass (kg), volume (mm^3), surface
    area (mm^2), centre of mass (mm) and moments of inertia about the world
    axes through the centre of mass (kg*mm^2)."""
    body = _registry.get(p['body'])
    props = body.physicalProperties
    out = {
        'body': p['body'],
        'mass_kg': round(props.mass, 6),
        'volume_mm3': round(props.volume / (MM ** 3), 3),
        'area_mm2': round(props.area / (MM * MM), 3),
        'center_of_mass_mm': _xyz_mm(props.centerOfMass),
    }
    with contextlib.suppress(Exception):
        ok, xx, yy, zz, xy, yz, xz = props.getXYZMomentsOfInertia()
        if ok:
            # kg*cm^2 -> kg*mm^2
            out['moments_kg_mm2'] = {
                'xx': round(xx * 100, 3), 'yy': round(yy * 100, 3),
                'zz': round(zz * 100, 3), 'xy': round(xy * 100, 3),
                'yz': round(yz * 100, 3), 'xz': round(xz * 100, 3),
            }
    return out


def op_interference(app, p):
    """Detect interference (overlap) between two or more body tokens. Returns the
    interfering pairs with overlap volume (mm^3)."""
    design = _design(app)
    coll = _collection(p['bodies'])
    iin = design.createInterferenceInput(coll)
    results = design.analyzeInterference(iin)
    hits = []
    for i in range(results.count):
        r = results.item(i)
        try:
            vol = round(r.interferenceBody.volume / (MM ** 3), 3)
        except Exception:
            vol = None
        hits.append({'volume_mm3': vol})
    return {'count': results.count, 'interferences': hits}


_IMPORT_OPTS = {
    'step': 'createSTEPImportOptions',
    'iges': 'createIGESImportOptions',
    'sat': 'createSATImportOptions',
    'smt': 'createSMTImportOptions',
    'f3d': 'createFusionArchiveImportOptions',
}


def op_import_file(app, p):
    """Import a CAD file into the active design. format: step|iges|sat|smt|f3d
    (imported into the root), or dxf (2D sketch onto a plane name/token via
    `plane`)."""
    im = app.importManager
    fmt = p['format'].lower()
    path = p['path']
    root = _root(app)
    if fmt == 'dxf':
        plane = _resolve_plane(app, p.get('plane', 'XY'))
        opts = im.createDXF2DImportOptions(path, plane)
        im.importToTarget(opts, root)
        return {'imported': path, 'format': 'dxf'}
    if fmt not in _IMPORT_OPTS:
        raise ValueError('format must be step|iges|sat|smt|f3d|dxf, got %r' % fmt)
    opts = getattr(im, _IMPORT_OPTS[fmt])(path)
    im.importToTarget(opts, root)
    return {'imported': path, 'format': fmt}


def op_timeline(app, p):
    """Inspect or roll back the parametric timeline. action: "list" (default,
    returns items) or "rollback" (set marker to index `position`)."""
    design = _design(app)
    tl = design.timeline
    action = (p.get('action') or 'list').lower()
    if action == 'rollback':
        tl.markerPosition = int(p['position'])
        return {'marker_position': tl.markerPosition, 'count': tl.count}
    if action == 'list':
        items = []
        for i in range(tl.count):
            it = tl.item(i)
            try:
                name = it.name
            except Exception:
                name = None
            items.append({'index': i, 'name': name,
                          'suppressed': bool(getattr(it, 'isSuppressed', False))})
        return {'count': tl.count, 'marker_position': tl.markerPosition,
                'items': items}
    raise ValueError('action must be list|rollback, got %r' % action)


def op_suppress_feature(app, p):
    """Suppress (or with suppress=False, unsuppress) a feature token in the
    timeline. Parametric designs only."""
    feat = _registry.get(p['feature'])
    entity = feat.timelineObject if hasattr(feat, 'timelineObject') else feat
    entity.isSuppressed = bool(p.get('suppress', True))
    return {'feature': p['feature'], 'suppressed': entity.isSuppressed}


# --------------------------------------------------------------------------- #
# BOM / assembly reports
# --------------------------------------------------------------------------- #
def _component_materials(comp):
    mats = set()
    for body in comp.bRepBodies:
        try:
            if body.material:
                mats.add(body.material.name)
        except Exception:
            pass
    return sorted(mats)


def _component_mass_kg(comp):
    mass = 0.0
    for body in comp.bRepBodies:
        try:
            if body.isSolid:
                mass += body.physicalProperties.mass  # kg
        except Exception:
            pass
    return round(mass, 6)


def op_bom(app, p):
    """Bill of materials: one row per distinct component with quantity, body
    count, materials and (include_mass=True, default) per-unit mass in kg.
    csv_path additionally writes the table as a CSV file."""
    design = _design(app)
    root = design.rootComponent
    include_mass = bool(p.get('include_mass', True))
    rows = {}

    def visit(occurrences):
        for occ in occurrences:
            comp = occ.component
            row = rows.get(comp.name)
            if row is None:
                row = {'component': comp.name,
                       'part_number': getattr(comp, 'partNumber', '') or None,
                       'description': getattr(comp, 'description', '') or None,
                       'quantity': 0,
                       'bodies': comp.bRepBodies.count,
                       'materials': _component_materials(comp)}
                if include_mass:
                    row['unit_mass_kg'] = _component_mass_kg(comp)
                rows[comp.name] = row
            row['quantity'] += 1
            visit(occ.childOccurrences)

    visit(root.occurrences)
    if not rows and root.bRepBodies.count:
        # Single-part design: report the root component as the only line item.
        row = {'component': root.name, 'part_number': None, 'description': None,
               'quantity': 1, 'bodies': root.bRepBodies.count,
               'materials': _component_materials(root)}
        if include_mass:
            row['unit_mass_kg'] = _component_mass_kg(root)
        rows[root.name] = row

    items = sorted(rows.values(), key=lambda r: r['component'].lower())
    out = {'count': len(items), 'items': items}
    if include_mass:
        out['total_mass_kg'] = round(
            sum(r.get('unit_mass_kg', 0.0) * r['quantity'] for r in items), 6)
    if p.get('csv_path'):
        cols = ['component', 'part_number', 'description', 'quantity', 'bodies',
                'materials'] + (['unit_mass_kg'] if include_mass else [])
        with open(p['csv_path'], 'w', newline='', encoding='utf-8') as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            for r in items:
                writer.writerow([
                    '; '.join(r[c]) if c == 'materials' else r.get(c, '')
                    for c in cols])
        out['csv'] = p['csv_path']
    return out


# --------------------------------------------------------------------------- #
# Sketch text, emboss / engrave
# --------------------------------------------------------------------------- #
def op_sketch_text(app, p):
    """Add text to a sketch at (x, y) mm — or along a curve when `path` (sketch
    curve token) is given. height in mm; optional font, bold, italic, angle
    (deg). The returned text token extrudes directly (extrude / emboss), so
    labels and logos need no extra tracing."""
    sk = _registry.get(p['sketch'])
    texts = sk.sketchTexts
    height_mm = float(p.get('height', 10.0))
    x, y = float(p.get('x', 0.0)), float(p.get('y', 0.0))
    text = p['text']
    if p.get('path'):
        tin = texts.createInput2(text, height_mm * MM)
        tin.setAsAlongPath(
            _registry.get(p['path']), bool(p.get('above_path', True)),
            adsk.core.HorizontalAlignments.LeftHorizontalAlignment, 0)
    else:
        try:
            tin = texts.createInput2(text, height_mm * MM)
            # Layout box: generous width estimate so the text never wraps.
            box_w = float(p.get('box_width', max(4.0, 0.8 * height_mm * len(text))))
            box_h = float(p.get('box_height', 1.6 * height_mm))
            tin.setAsMultiLine(
                _pt(x, y), _pt(x + box_w, y + box_h),
                adsk.core.HorizontalAlignments.LeftHorizontalAlignment,
                adsk.core.VerticalAlignments.BottomVerticalAlignment, 0)
        except Exception:
            # Older Fusion: positional single-line input.
            tin = texts.createInput(text, height_mm * MM, _pt(x, y))
    if p.get('font'):
        with contextlib.suppress(Exception):
            tin.fontName = p['font']
    with contextlib.suppress(Exception):
        style = 0
        if p.get('bold'):
            style |= adsk.fusion.TextStyles.TextStyleBold
        if p.get('italic'):
            style |= adsk.fusion.TextStyles.TextStyleItalic
        if style:
            tin.textStyle = style
    if p.get('angle'):
        with contextlib.suppress(Exception):
            tin.angle = math.radians(p['angle'])
    st = texts.add(tin)
    return {'sketch': _registry.add('skt', sk), 'text': _registry.add('txt', st)}


def op_emboss(app, p):
    """Engrave (cut, default) or emboss (raise, engrave=False) a sketch text or
    profile token into/out of the solid it sits on, `depth` mm deep. Engraving
    cuts below the sketch plane; embossing joins material above it."""
    prof = _registry.get(p['profile'])
    feats = _root(app).features.extrudeFeatures
    engrave = bool(p.get('engrave', True))
    ein = feats.createInput(prof, _operation('cut' if engrave else 'join'))
    depth = abs(float(p['depth'])) * MM
    ein.setDistanceExtent(False, _vi(-depth if engrave else depth))
    return _feature_result(feats.add(ein), 'engrave' if engrave else 'emboss')


# --------------------------------------------------------------------------- #
# Sheet metal: flat pattern + DXF; sketch DXF export
# --------------------------------------------------------------------------- #
def _flat_pattern_face(p):
    """The stationary face to unfold from: an explicit face token, or the
    largest planar face of a body token."""
    if p.get('face'):
        return _registry.get(p['face'])
    if p.get('body'):
        body = _registry.get(p['body'])
        planar = [f for f in body.faces if _surface_type(f) == 'plane']
        if not planar:
            raise RuntimeError('Body has no planar face to unfold from')
        return max(planar, key=lambda f: f.area)
    return None


def op_flat_pattern(app, p):
    """Create (or reuse) the flat pattern of a sheet-metal component. Pass a
    stationary planar `face` token, or a `body` token (its largest planar face
    is used). The body must be a sheet-metal body of uniform thickness."""
    face = _flat_pattern_face(p)
    if face is None:
        raise ValueError('flat_pattern needs a face or body token')
    comp = face.body.parentComponent
    fp = comp.flatPattern
    if not fp:
        fp = comp.createFlatPattern(face)
    return {'flat_pattern': _registry.add('flp', fp), 'component': comp.name}


def op_export_flat_pattern(app, p):
    """Export the document's flat pattern as DXF (laser/waterjet-ready outline).
    When a face/body token is given and no flat pattern exists yet, it is
    created first."""
    if p.get('face') or p.get('body'):
        face = _flat_pattern_face(p)
        comp = face.body.parentComponent
        if not comp.flatPattern:
            comp.createFlatPattern(face)
    product = app.activeDocument.products.itemByProductType('FlatPatternProductType')
    if not product:
        raise RuntimeError('No flat pattern in this document. Pass the face or '
                           'body token of a sheet-metal body to create one.')
    flat = product.flatPattern
    em = product.exportManager
    opts = em.createDXFFlatPatternExportOptions(p['path'], flat)
    em.execute(opts)
    return {'exported': p['path'], 'format': 'dxf'}


def op_export_sketch_dxf(app, p):
    """Save a sketch (token) as a 2D DXF file — quick route to laser cutting or
    2D documentation without a drawing sheet."""
    sk = _registry.get(p['sketch'])
    sk.saveAsDXF(p['path'])
    return {'exported': p['path'], 'format': 'dxf', 'sketch': p['sketch']}


# --------------------------------------------------------------------------- #
# Meshes / reverse engineering
# --------------------------------------------------------------------------- #
_MESH_UNITS = {'mm': 'MillimeterMeshUnit', 'cm': 'CentimeterMeshUnit',
               'm': 'MeterMeshUnit', 'in': 'InchMeshUnit', 'ft': 'FootMeshUnit'}


def op_import_mesh(app, p):
    """Insert an STL/OBJ/3MF scan/mesh file. units: mm|cm|m|in|ft (mesh files
    carry no units — pick the one the scan was exported in). In a parametric
    design the mesh is wrapped in a base feature, as Fusion requires."""
    design = _design(app)
    root = design.rootComponent
    unit_key = (p.get('units') or 'mm').lower()
    if unit_key not in _MESH_UNITS:
        raise ValueError('units must be one of %s, got %r'
                         % (sorted(_MESH_UNITS), p.get('units')))
    units = getattr(adsk.fusion.MeshUnits, _MESH_UNITS[unit_key])
    parametric = design.designType == adsk.fusion.DesignTypes.ParametricDesignType
    base = None
    if parametric:
        base = root.features.baseFeatures.add()
        base.startEdit()
    try:
        if base:
            added = root.meshBodies.add(p['path'], units, base)
        else:
            added = root.meshBodies.add(p['path'], units)
    finally:
        if base:
            base.finishEdit()
    meshes = [{'token': _registry.add('msh', added.item(i)),
               'name': added.item(i).name} for i in range(added.count)]
    return {'meshes': meshes, 'count': len(meshes)}


def op_mesh_info(app, p):
    """Triangle/node counts (and best-effort bounding box) of mesh bodies. Pass
    a mesh token, or omit to report every mesh in the root component."""
    root = _root(app)
    meshes = [_registry.get(p['mesh'])] if p.get('mesh') else list(root.meshBodies)
    out = []
    for m in meshes:
        entry = {'token': _registry.add('msh', m), 'name': m.name}
        try:
            dm = m.displayMesh or m.mesh
            entry['triangles'] = dm.triangleCount
            entry['nodes'] = dm.nodeCount
        except Exception:
            pass
        try:
            bb = m.boundingBox
            lo, hi = _xyz_mm(bb.minPoint), _xyz_mm(bb.maxPoint)
            entry['min_mm'], entry['max_mm'] = lo, hi
            entry['size_mm'] = [round(hi[i] - lo[i], 4) for i in range(3)]
        except Exception:
            pass
        out.append(entry)
    return {'count': len(out), 'meshes': out}


def _fusion_enum(value_name):
    """Look up an adsk.fusion enum VALUE by its name without knowing the enum
    class (exact holder names vary across Fusion releases)."""
    for attr in dir(adsk.fusion):
        holder = getattr(adsk.fusion, attr, None)
        value = getattr(holder, value_name, None)
        if value is not None and not callable(value):
            return value
    raise RuntimeError('Enum value %r not found — this Fusion version may not '
                       'support the requested option.' % value_name)


def _mesh_targets(root, p, key='meshes'):
    meshes = ([_registry.get(t) for t in p.get(key) or []]
              or list(root.meshBodies))
    if not meshes:
        raise RuntimeError('No mesh bodies; import one with import_mesh')
    return meshes


_CONVERT_METHODS = {
    'faceted': 'FacetedMeshConvertMethodType',
    'prismatic': 'PrismaticMeshConvertMethodType',
    'organic': 'OrganicMeshConvertMethodType',
}


def op_mesh_to_brep(app, p):
    """Convert mesh bodies (tokens; all meshes when omitted) into BRep bodies —
    the reverse-engineering gateway: after conversion every solid tool works
    (combine, split, measure, export STEP...). method: faceted (default,
    triangles as-is), prismatic (recognises planes/cylinders — much cleaner
    solids from machine-part scans), organic (T-Spline fit). Uses the native
    mesh-convert feature when available, else drives Fusion's Convert Mesh
    command (faceted only)."""
    root = _root(app)
    meshes = _mesh_targets(root, p)
    method = (p.get('method') or 'faceted').lower()
    if method not in _CONVERT_METHODS:
        raise ValueError('method must be faceted|prismatic|organic, got %r' % method)
    before = root.bRepBodies.count
    used = None
    feats = getattr(root.features, 'meshConvertFeatures', None)
    if feats is not None:
        try:
            for m in meshes:
                try:
                    cin = feats.createInput()
                except TypeError:
                    cin = feats.createInput(m)
                coll = adsk.core.ObjectCollection.create()
                coll.add(m)
                with contextlib.suppress(Exception):
                    cin.inputBodies = coll
                if method != 'faceted':
                    cin.meshConvertMethodType = _fusion_enum(_CONVERT_METHODS[method])
                feats.add(cin)
            used = 'meshConvertFeatures(%s)' % method
        except Exception:  # noqa: BLE001 - fall back to the UI command below
            used = None
    if used is None:
        if method != 'faceted':
            raise RuntimeError('This Fusion version has no mesh-convert API; the '
                               'command fallback only supports method="faceted".')
        # Documented workaround: select the meshes and run the UI command.
        sels = app.userInterface.activeSelections
        sels.clear()
        for m in meshes:
            sels.add(m)
        app.executeTextCommand('Commands.Start ParaMeshConvertCommand')
        app.executeTextCommand('NuCommands.CommitCmd')
        sels.clear()
        used = 'ParaMeshConvertCommand'
    bodies = [{'token': _registry.add('bdy', root.bRepBodies.item(i)),
               'name': root.bRepBodies.item(i).name}
              for i in range(before, root.bRepBodies.count)]
    if not bodies:
        raise RuntimeError('Mesh conversion produced no BRep bodies (method: %s). '
                           'Dense scans usually need mesh_reduce first.' % used)
    return {'bodies': bodies, 'method': used}


def _mesh_info_entry(m):
    entry = {'token': _registry.add('msh', m), 'name': m.name}
    with contextlib.suppress(Exception):
        dm = m.displayMesh or m.mesh
        entry['triangles'] = dm.triangleCount
    return entry


def op_mesh_reduce(app, p):
    """Reduce a scan's triangle count before converting/sectioning. Target:
    target_faces (absolute), proportion (0-100 % of the original), or
    max_deviation (mm, default 0.05). method: adaptive (default, keeps detail)
    or uniform. Requires the mesh-feature API (Fusion 2024+)."""
    root = _root(app)
    feats = getattr(root.features, 'meshReduceFeatures', None)
    if feats is None:
        raise RuntimeError('meshReduceFeatures not available in this Fusion '
                           'version — use the MESH workspace Reduce command.')
    out = []
    for m in _mesh_targets(root, p):
        try:
            rin = feats.createInput()
        except TypeError:
            rin = feats.createInput(m)
        with contextlib.suppress(Exception):
            rin.mesh = m
        if p.get('target_faces'):
            rin.meshReduceTargetType = _fusion_enum('FaceCountMeshReduceTargetType')
            rin.facecount = int(p['target_faces'])
        elif p.get('proportion'):
            rin.meshReduceTargetType = _fusion_enum('ProportionMeshReduceTargetType')
            rin.proportion = float(p['proportion'])
        else:
            rin.meshReduceTargetType = _fusion_enum(
                'MaximumDeviationMeshReduceTargetType')
            rin.maximumDeviation = float(p.get('max_deviation', 0.05)) * MM
        if (p.get('method') or 'adaptive').lower() == 'uniform':
            rin.meshReduceMethodType = _fusion_enum('UniformReduceType')
        feats.add(rin)
        out.append(_mesh_info_entry(m))
    return {'reduced': out}


def op_mesh_remesh(app, p):
    """Regenerate a mesh's triangulation (fixes long slivers before convert).
    Requires the mesh-feature API (Fusion 2024+); defaults are used for the
    remesh settings."""
    root = _root(app)
    feats = getattr(root.features, 'meshRemeshFeatures', None)
    if feats is None:
        raise RuntimeError('meshRemeshFeatures not available in this Fusion '
                           'version — use the MESH workspace Remesh command.')
    out = []
    for m in _mesh_targets(root, p):
        try:
            rin = feats.createInput()
        except TypeError:
            rin = feats.createInput(m)
        with contextlib.suppress(Exception):
            rin.mesh = m
        feats.add(rin)
        out.append(_mesh_info_entry(m))
    return {'remeshed': out}


def op_mesh_plane_cut(app, p):
    """Cut a mesh (token) with a plane ("XY"/"XZ"/"YZ", plane/face token) at
    optional `offset` mm — chop off scanner-table junk or keep half of a
    symmetric scan. mode: trim (drop one side, default), split (two bodies).
    Requires the mesh-feature API (Fusion 2024+)."""
    root = _root(app)
    feats = getattr(root.features, 'meshPlaneCutFeatures', None)
    if feats is None:
        raise RuntimeError('meshPlaneCutFeatures not available in this Fusion '
                           'version — use the MESH workspace Plane Cut command.')
    mesh = _registry.get(p['mesh'])
    plane = _resolve_plane(app, p.get('plane', 'XY'))
    if p.get('offset'):
        planes = root.constructionPlanes
        cin = planes.createInput()
        cin.setByOffset(plane, _vi(p['offset'] * MM))
        plane = planes.add(cin)
    try:
        pin = feats.createInput()
    except TypeError:
        pin = feats.createInput(mesh, plane)
    with contextlib.suppress(Exception):
        pin.mesh = mesh
    for attr in ('plane', 'cutPlane', 'cuttingPlane'):
        with contextlib.suppress(Exception):
            setattr(pin, attr, plane)
            break
    mode = (p.get('mode') or 'trim').lower()
    if mode == 'split':
        for name in ('SplitBodyMeshPlaneCutType', 'SplitMeshPlaneCutType'):
            with contextlib.suppress(Exception):
                pin.meshPlaneCutType = _fusion_enum(name)
                break
    feats.add(pin)
    return {'cut': p['mesh'], 'mode': mode,
            'meshes': [_mesh_info_entry(m) for m in root.meshBodies]}


def op_canvas_add(app, p):
    """Attach an image (photo of the part) as a canvas on a plane, optionally
    scaled — trace it with sketches for reverse engineering without a 3D scan.
    width_mm sets the printed width of the image on the plane; fine-tune with
    Fusion's right-click Calibrate."""
    root = _root(app)
    canvases = getattr(root, 'canvases', None)
    if canvases is None:
        raise RuntimeError('Canvases are not available in this Fusion version.')
    plane = _resolve_plane(app, p.get('plane', 'XY'))
    cin = canvases.createInput(p['image'], plane)
    with contextlib.suppress(Exception):
        cin.opacity = int(p.get('opacity', 100))
    if p.get('width_mm'):
        with contextlib.suppress(Exception):
            t = cin.transform
            # transform is unitless: scale image pixels to the requested width.
            current = abs(t.getCell(0, 0)) or 1.0
            factor = (p['width_mm'] * MM) / current
            scale = adsk.core.Matrix3D.create()
            scale.setCell(0, 0, factor)
            scale.setCell(1, 1, factor)
            t.transformBy(scale)
            cin.transform = t
    canvas = canvases.add(cin)
    return {'canvas': _registry.add('cnv', canvas),
            'note': 'Use right-click > Calibrate in Fusion for exact two-point '
                    'scaling if width_mm was approximate.'}


def op_mesh_section(app, p):
    """Slice a mesh (token) with a plane ("XY"/"XZ"/"YZ", plane/face token) at
    optional `offset` mm, producing a section sketch — trace it with
    sketch_polyline/sketch_spline dimensions to rebuild the part parametrically."""
    mesh = _registry.get(p['mesh'])
    root = _root(app)
    plane = _resolve_plane(app, p.get('plane', 'XY'))
    if p.get('offset'):
        planes = root.constructionPlanes
        cin = planes.createInput()
        cin.setByOffset(plane, _vi(p['offset'] * MM))
        plane = planes.add(cin)
    sk = root.sketches.add(plane)
    try:
        sk.intersectWithSketchPlane([mesh])
    except Exception as exc:
        raise RuntimeError('This Fusion version cannot section a mesh into a '
                           'sketch (%s). Convert with mesh_to_brep first, then '
                           'section the solid.' % exc)
    return {'sketch': _registry.add('skt', sk),
            'curves': sk.sketchCurves.count,
            'profiles': sk.profiles.count}


# --------------------------------------------------------------------------- #
# Drawings (2D documentation)
# --------------------------------------------------------------------------- #
def _try_headless_drawing(app, template):
    """Best-effort headless drawing creation via the 2026 Drawings API. The
    exact creation surface is young and thinly documented, so this probes a
    few shapes and cleans up after itself; None means 'fall back to the UI'."""
    try:
        import adsk.drawing
    except ImportError:
        return None
    doc = None
    try:
        doc_type = getattr(adsk.core.DocumentTypes, 'DrawingDocumentType', None)
        if doc_type is None:
            return None
        doc = app.documents.add(doc_type)
        product = doc.products.itemByProductType('DrawingProductType')
        drawing = adsk.drawing.Drawing.cast(product) if product else None
        if drawing is None:
            raise RuntimeError('no DrawingProductType on the new document')
        if template:
            applied = False
            for attr in ('applyTemplate', 'loadTemplate', 'setTemplate'):
                fn = getattr(drawing, attr, None)
                if fn:
                    with contextlib.suppress(Exception):
                        fn(template)
                        applied = True
                        break
            if not applied:
                raise RuntimeError('this Fusion build exposes no template API')
        out = {'created': doc.name, 'headless': True}
        with contextlib.suppress(Exception):
            out['sheets'] = drawing.sheets.count
        return out
    except Exception:  # noqa: BLE001 - close the stray document, use the dialog
        if doc is not None:
            with contextlib.suppress(Exception):
                doc.close(False)
        return None


def op_create_drawing(app, p):
    """Create a drawing for the active design. Tries the headless Drawings API
    first (Fusion 2026+; optional `template` path/name); when that is not
    available it opens the "Drawing from Design" dialog for the user to finish.
    For fully scripted 2D output use export_sketch_dxf / export_flat_pattern;
    export a finished drawing with drawing_export."""
    if p.get('headless', True):
        result = _try_headless_drawing(app, p.get('template'))
        if result is not None:
            return result
    ui = app.userInterface
    cmd = None
    for cmd_id in ('NewFusionDrawingDocumentCommand', 'DrawingFromDesignCommand',
                   'NewDrawingFromDesignCommand', 'FusionDrawingFromDesignCommand'):
        cmd = ui.commandDefinitions.itemById(cmd_id)
        if cmd:
            break
    if not cmd:
        raise RuntimeError('Headless drawing creation is unavailable and no '
                           'drawing-from-design command was found. Use '
                           'export_sketch_dxf / export_flat_pattern instead.')
    cmd.execute()
    return {'launched': cmd.id, 'headless': False,
            'note': 'Fusion opened the drawing dialog; the user completes the '
                    'sheet setup interactively. Afterwards drawing_export can '
                    'save it as PDF/DXF.'}


def op_drawing_export(app, p):
    """Export the ACTIVE drawing document to PDF or DXF. Open/create the
    drawing first (create_drawing); works on whatever sheets it contains."""
    doc = app.activeDocument
    product = doc.products.itemByProductType('DrawingProductType') if doc else None
    if not product:
        raise RuntimeError('The active document is not a drawing. Switch to the '
                           'drawing tab (or run create_drawing) first.')
    try:
        import adsk.drawing
        drawing = adsk.drawing.Drawing.cast(product) or product
    except ImportError:
        drawing = product
    em = getattr(drawing, 'exportManager', None)
    if em is None:
        raise RuntimeError('This Fusion version exposes no drawing export API.')
    fmt = (p.get('format') or 'pdf').lower()
    creators = {'pdf': 'createPDFExportOptions', 'dxf': 'createDXFExportOptions'}
    if fmt not in creators:
        raise ValueError('format must be pdf|dxf, got %r' % fmt)
    creator = getattr(em, creators[fmt], None)
    if creator is None:
        raise RuntimeError('Drawing %s export is not available in this Fusion '
                           'version.' % fmt.upper())
    em.execute(creator(p['path']))
    return {'exported': p['path'], 'format': fmt, 'document': doc.name}


# --------------------------------------------------------------------------- #
# Interaction: user selection, highlighting, visibility, isolate, undo
# --------------------------------------------------------------------------- #
# Token prefix per API object type for selection/highlight round-tripping.
_SELECTION_KINDS = {
    'BRepFace': ('fac', 'face'),
    'BRepEdge': ('edg', 'edge'),
    'BRepVertex': ('vtx', 'vertex'),
    'BRepBody': ('bdy', 'body'),
    'MeshBody': ('msh', 'mesh'),
    'Occurrence': ('occ', 'occurrence'),
    'Sketch': ('skt', 'sketch'),
    'Profile': ('prf', 'profile'),
    'SketchLine': ('lin', 'sketch_line'),
    'SketchCircle': ('cir', 'sketch_circle'),
    'SketchArc': ('arc', 'sketch_arc'),
    'SketchPoint': ('spt', 'sketch_point'),
    'ConstructionPlane': ('pln', 'construction_plane'),
    'ConstructionAxis': ('cax', 'construction_axis'),
    'ConstructionPoint': ('cpt', 'construction_point'),
    'JointOrigin': ('jor', 'joint_origin'),
}


def op_get_selection(app, p):
    """What the user currently has selected in the Fusion UI, as tokens — so
    they can click a face/edge/body and say "here". Faces report centroid and
    surface type, edges report length, so the geometry is identifiable."""
    out = []
    for sel in app.userInterface.activeSelections:
        ent = sel.entity
        type_name = ent.objectType.split('::')[-1]
        prefix, kind = _SELECTION_KINDS.get(type_name, ('ent', type_name))
        item = {'token': _registry.add(prefix, ent), 'kind': kind}
        with contextlib.suppress(Exception):
            item['name'] = ent.name
        if kind == 'face':
            with contextlib.suppress(Exception):
                item['type'] = _surface_type(ent)
                item['centroid_mm'] = _xyz_mm(ent.centroid)
        elif kind == 'edge':
            with contextlib.suppress(Exception):
                item['length_mm'] = round(ent.length / MM, 3)
        elif kind == 'sketch_point':
            with contextlib.suppress(Exception):
                item['point_mm'] = _xyz_mm(ent.worldGeometry)
        out.append(item)
    return {'count': len(out), 'selection': out}


def op_highlight(app, p):
    """Select the given tokens in the Fusion UI so the user SEES which entities
    are meant ("I would fillet these edges — OK?"). Replaces the current
    selection; empty tokens list just clears it."""
    sels = app.userInterface.activeSelections
    sels.clear()
    added = []
    for tok in p.get('tokens') or []:
        try:
            sels.add(_registry.get(tok))
            added.append(tok)
        except Exception:
            pass  # entity may be hidden or not selectable; highlight the rest
    return {'highlighted': added, 'count': len(added)}


def _set_visible(obj, visible):
    """Toggle visibility via isLightBulbOn (the settable toggle on bodies,
    occurrences, construction geometry) falling back to isVisible (sketches)."""
    try:
        obj.isLightBulbOn = bool(visible)
        return 'isLightBulbOn'
    except Exception:
        obj.isVisible = bool(visible)
        return 'isVisible'


def _get_visible(obj):
    for attr in ('isLightBulbOn', 'isVisible'):
        with contextlib.suppress(Exception):
            return bool(getattr(obj, attr))
    return True


def op_set_visibility(app, p):
    """Show/hide entities by token (bodies, occurrences, sketches, meshes,
    construction geometry). Hidden bodies don't render on screenshots."""
    visible = bool(p.get('visible', True))
    done, failed = [], []
    for tok in p.get('tokens') or []:
        try:
            _set_visible(_registry.get(tok), visible)
            done.append(tok)
        except Exception as exc:
            failed.append({'token': tok, 'error': str(exc)})
    out = {'visible': visible, 'changed': done}
    if failed:
        out['failed'] = failed
    return out


def _same_entity(a, b):
    """Identity across Fusion API proxy objects (each collection access returns
    a fresh proxy, so `is` never matches)."""
    with contextlib.suppress(Exception):
        return a.entityToken == b.entityToken
    with contextlib.suppress(Exception):
        return a == b
    return a is b


_isolate_stash = None  # [(proxy, previous_visibility)] while isolated


def op_isolate(app, p):
    """Show ONLY the given body/occurrence/mesh token: hides every other root
    body, occurrence and mesh, remembering their state for unisolate. Great
    before screenshots of a single part inside an assembly."""
    global _isolate_stash
    if _isolate_stash is not None:
        raise RuntimeError('Already isolated; call unisolate first.')
    target = _registry.get(p['token'])
    keep = [target]
    # An occurrence chain that owns the target must stay visible too.
    with contextlib.suppress(Exception):
        ctx = target.assemblyContext
        while ctx:
            keep.append(ctx)
            ctx = ctx.assemblyContext
    root = _root(app)
    stash, hidden = [], 0
    for coll in (root.bRepBodies, root.occurrences, root.meshBodies):
        for obj in coll:
            if any(_same_entity(obj, k) for k in keep):
                continue
            prev = _get_visible(obj)
            stash.append((obj, prev))
            if prev:
                with contextlib.suppress(Exception):
                    _set_visible(obj, False)
                    hidden += 1
    _set_visible(target, True)
    _isolate_stash = stash
    return {'isolated': p['token'], 'hidden': hidden}


def op_unisolate(app, p):
    """Restore the visibility state saved by isolate."""
    global _isolate_stash
    if _isolate_stash is None:
        return {'restored': 0, 'note': 'Nothing is isolated.'}
    restored = 0
    for obj, prev in _isolate_stash:
        with contextlib.suppress(Exception):
            _set_visible(obj, prev)
            restored += 1
    _isolate_stash = None
    return {'restored': restored}


def op_multi_screenshot(app, p):
    """Capture several camera presets in ONE round-trip (e.g. iso/front/top/
    right) so the model is visible from all sides at once. Returns one base64
    PNG per direction."""
    directions = p.get('directions') or ['iso', 'front', 'top', 'right']
    width = int(p.get('width', 800))
    height = int(p.get('height', 600))
    base = p.get('base_path') or ''
    shots = []
    for d in directions:
        vp = _apply_camera_direction(app, d, p.get('fit', True))
        path = ((base + '_' + d + '.png') if base
                else os.path.join(tempfile.gettempdir(), 'fusion_mcp_%s.png' % d))
        vp.saveAsImageFile(path, width, height)
        with open(path, 'rb') as fh:
            b64 = base64.b64encode(fh.read()).decode('ascii')
        shots.append({'direction': d, 'path': path, 'image_base64': b64})
    return {'count': len(shots), 'shots': shots}


def op_section_view(app, p):
    """Turn on a section-analysis view: slices the display (not the geometry)
    with a plane at `offset` mm — see inside pockets, shells and housings on
    screenshots. Requires Fusion with the section-analysis API (2023+)."""
    design = _design(app)
    analyses = getattr(design, 'analyses', None)
    sections = getattr(analyses, 'sectionAnalyses', None) if analyses else None
    if sections is None:
        raise RuntimeError('Section analysis is not available in this Fusion '
                           'version; use split_body on a copy instead.')
    plane = _resolve_plane(app, p.get('plane', 'XY'))
    sin = sections.createInput(plane, p.get('offset', 0.0) * MM)
    section = sections.add(sin)
    return {'section': _registry.add('sec', section),
            'plane': str(p.get('plane', 'XY')), 'offset_mm': p.get('offset', 0.0)}


def op_section_off(app, p):
    """Remove all section-analysis views (restore the full display)."""
    design = _design(app)
    analyses = getattr(design, 'analyses', None)
    sections = getattr(analyses, 'sectionAnalyses', None) if analyses else None
    removed = 0
    if sections is not None:
        for i in range(sections.count - 1, -1, -1):
            with contextlib.suppress(Exception):
                sections.item(i).deleteMe()
                removed += 1
    return {'removed': removed}


def op_undo(app, p):
    """Undo the last `steps` operations via Fusion's undo stack. Entity tokens
    issued before the undo may now point at deleted objects — re-query with
    get_state/query_entities before reusing them."""
    steps = max(1, int(p.get('steps', 1)))
    done = 0
    for _ in range(steps):
        try:
            app.executeTextCommand('Commands.Start UndoCommand')
            done += 1
        except Exception:
            break
    return {'undone': done, 'tokens_may_be_stale': True,
            'note': 'Re-run get_state/query_entities before reusing old tokens.'}


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _swap_ext(path, ext):
    base = path.rsplit('.', 1)[0] if '.' in path.rsplit('\\', 1)[-1] else path
    return base + '.' + ext


def _do_export(app, fmt, path):
    design = _design(app)
    em = design.exportManager
    if fmt == 'step':
        opts = em.createSTEPExportOptions(path, design.rootComponent)
    elif fmt == 'iges':
        opts = em.createIGESExportOptions(path, design.rootComponent)
    elif fmt == 'sat':
        opts = em.createSATExportOptions(path, design.rootComponent)
    elif fmt == 'smt':
        opts = em.createSMTExportOptions(path, design.rootComponent)
    elif fmt == 'f3d':
        opts = em.createFusionArchiveExportOptions(path)
    elif fmt == 'stl':
        opts = em.createSTLExportOptions(design.rootComponent, path)
        opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    elif fmt == '3mf':
        opts = em.createC3MFExportOptions(design.rootComponent, path)
        with contextlib.suppress(Exception):
            opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    else:
        raise ValueError('format must be step|iges|sat|smt|f3d|stl|3mf, got %r' % fmt)
    em.execute(opts)
    return path


def op_export(app, p):
    fmt = p['format'].lower()
    path = p['path']
    # Personal-tier licenses restrict some neutral CAD formats (STEP/IGES/SAT/SMT).
    # When such an export is blocked, fall back to STL then F3D so the user still
    # gets geometry, with a clear note instead of a raw API error.
    allow_fallback = bool(p.get('allow_fallback', True))
    try:
        out = _do_export(app, fmt, path)
        return {'exported': out, 'format': fmt, 'fallback': False}
    except Exception as exc:
        if not allow_fallback or fmt in ('stl', 'f3d'):
            raise
        for alt in ('stl', 'f3d'):
            try:
                out = _do_export(app, alt, _swap_ext(path, alt))
                return {'exported': out, 'format': alt, 'fallback': True,
                        'requested_format': fmt,
                        'note': '%s export failed (%s); exported %s instead. '
                                'On Fusion Personal some neutral formats are '
                                'license-restricted.' % (fmt.upper(), exc, alt.upper())}
            except Exception:
                continue
        raise


# Camera presets: name -> (view direction from target toward eye, up vector).
_CAMERA_DIRS = {
    'front':            ((0, -1, 0), (0, 0, 1)),
    'back':             ((0, 1, 0), (0, 0, 1)),
    'left':             ((-1, 0, 0), (0, 0, 1)),
    'right':            ((1, 0, 0), (0, 0, 1)),
    'top':              ((0, 0, 1), (0, 1, 0)),
    'bottom':           ((0, 0, -1), (0, 1, 0)),
    'iso':              ((1, -1, 1), (0, 0, 1)),
    'iso-top-right':    ((1, -1, 1), (0, 0, 1)),
    'iso-top-left':     ((-1, -1, 1), (0, 0, 1)),
    'iso-bottom-right': ((1, 1, -1), (0, 0, 1)),
    'iso-bottom-left':  ((-1, 1, -1), (0, 0, 1)),
}


def _apply_camera_direction(app, direction, do_fit):
    vp = app.activeViewport
    if not vp:
        raise RuntimeError('No active viewport')
    direction = (direction or 'current').lower()
    if direction not in ('current', '') and direction not in _CAMERA_DIRS:
        raise ValueError('direction must be "current"|%s, got %r'
                         % ('|'.join(_CAMERA_DIRS), direction))
    if direction in _CAMERA_DIRS:
        eye_dir, up = _CAMERA_DIRS[direction]
        cam = vp.camera
        vec = adsk.core.Vector3D.create(*eye_dir)
        vec.normalize()
        dist = cam.eye.distanceTo(cam.target)
        cam.eye = adsk.core.Point3D.create(
            cam.target.x + vec.x * dist,
            cam.target.y + vec.y * dist,
            cam.target.z + vec.z * dist)
        cam.upVector = adsk.core.Vector3D.create(*up)
        cam.isFitView = bool(do_fit)  # frame the whole model when fitting
        vp.camera = cam
        vp.refresh()
    elif do_fit:
        vp.fit()
    return vp


def op_screenshot(app, p):
    path = p['path']
    width = int(p.get('width', 1280))
    height = int(p.get('height', 720))
    vp = _apply_camera_direction(app, p.get('direction', 'current'),
                                 p.get('fit', False))
    saved = vp.saveAsImageFile(path, width, height)
    image_b64 = None
    if p.get('return_base64', True):
        try:
            with open(path, 'rb') as fh:
                image_b64 = base64.b64encode(fh.read()).decode('ascii')
        except Exception:
            image_b64 = None
    return {'path': path, 'saved': saved, 'width': width, 'height': height,
            'direction': (p.get('direction') or 'current'), 'image_base64': image_b64}


def op_fit_view(app, p):
    app.activeViewport.fit()
    return {'fitted': True}


def op_save(app, p):
    doc = app.activeDocument
    if not doc:
        raise RuntimeError('No active document')
    doc.save(p.get('message', ''))
    return {'saved': True, 'name': doc.name}


def op_set_design_mode(app, p):
    """Switch parametric (timeline/history) vs direct (no history) modeling.

    Direct mode skips per-feature timeline recompute and uses less memory, so
    one-shot builds are faster on weaker (Personal) hardware — at the cost of
    edit history. Switching to direct on a design with history flattens it.
    """
    design = _design(app)
    mode = (p.get('mode') or '').lower()
    types = adsk.fusion.DesignTypes
    if mode == 'direct':
        design.designType = types.DirectDesignType
    elif mode == 'parametric':
        design.designType = types.ParametricDesignType
    else:
        raise ValueError('mode must be "parametric" or "direct", got %r' % p.get('mode'))
    is_direct = design.designType == types.DirectDesignType
    return {'design_type': 'direct' if is_direct else 'parametric'}


# --------------------------------------------------------------------------- #
# CAM (MANUFACTURE): list setups, regenerate toolpaths, post-process G-code.
# The API cannot create setups — the user makes them once in the UI; from then
# on regeneration and posting are scriptable.
# --------------------------------------------------------------------------- #
def _cam_product(app):
    import adsk.cam
    prod = app.activeDocument.products.itemByProductType('CAMProductType')
    cam = adsk.cam.CAM.cast(prod) if prod else None
    if not cam:
        raise RuntimeError('No MANUFACTURE data in this document. Create a '
                           'Setup in the MANUFACTURE workspace first — the '
                           'Fusion API cannot create setups.')
    return cam


def _cam_setup_by_name(cam, name):
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        if s.name == name:
            return s
    raise RuntimeError('No CAM setup named %r (see cam_setups)' % name)


def op_cam_setups(app, p):
    """List MANUFACTURE setups with their operations and toolpath state."""
    cam = _cam_product(app)
    out = []
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        entry = {'index': i, 'name': s.name}
        ops = []
        with contextlib.suppress(Exception):
            for j in range(s.allOperations.count):
                o = s.allOperations.item(j)
                op_entry = {'name': o.name}
                with contextlib.suppress(Exception):
                    op_entry['strategy'] = o.strategy
                with contextlib.suppress(Exception):
                    op_entry['has_toolpath'] = bool(o.hasToolpath)
                ops.append(op_entry)
        entry['operations'] = ops
        out.append(entry)
    return {'count': len(out), 'setups': out}


def op_cam_generate(app, p):
    """(Re)generate toolpaths — for one setup by name, or all setups when
    omitted. Blocks until generation finishes or `timeout` s (default 240)."""
    cam = _cam_product(app)
    if p.get('setup'):
        future = cam.generateToolpath(_cam_setup_by_name(cam, p['setup']))
    else:
        future = cam.generateAllToolpaths(False)
    deadline = time.time() + float(p.get('timeout', 240))
    while not future.isGenerationCompleted and time.time() < deadline:
        adsk.doEvents()
        time.sleep(0.2)
    return {'completed': bool(future.isGenerationCompleted),
            'setup': p.get('setup') or 'all'}


def op_cam_post(app, p):
    """Post-process a setup's toolpaths to NC/G-code. path: output file (its
    directory and stem become the program folder/name); post_config: a .cps
    post-processor path or a filename from Fusion's generic post folder
    (default fanuc.cps); units: mm|in|document."""
    import adsk.cam
    cam = _cam_product(app)
    setup = _cam_setup_by_name(cam, p['setup'])
    folder, filename = os.path.split(p['path'])
    program = filename.rsplit('.', 1)[0] or 'program'
    post = p.get('post_config') or 'fanuc.cps'
    if not os.path.isabs(post):
        post = os.path.join(cam.genericPostFolder, post)
    units_map = {
        'mm': adsk.cam.PostOutputUnitOptions.MillimetersOutput,
        'in': adsk.cam.PostOutputUnitOptions.InchesOutput,
        'document': adsk.cam.PostOutputUnitOptions.DocumentUnitsOutput,
    }
    units = units_map.get((p.get('units') or 'mm').lower(), units_map['mm'])
    pin = adsk.cam.PostProcessInput.create(program, post, folder or '.', units)
    with contextlib.suppress(Exception):
        pin.isOpenInEditor = False
    if not cam.postProcess(setup, pin):
        raise RuntimeError('Post-processing failed — are the setup toolpaths '
                           'generated and valid? Run cam_generate first.')
    return {'posted': p['setup'], 'folder': folder or '.', 'program': program,
            'post': os.path.basename(post)}


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #
def _jsonable(value):
    import json
    try:
        json.dumps(value)
        return value
    except Exception:
        return repr(value)


def _run_code_helpers(app):
    """Concise mm/degree helpers injected into run_code scope. They return LIVE
    API objects (not token dicts), so a whole part can be built in a few lines."""
    root = _root(app)

    def h_sketch(plane='XY', name=None):
        sk = root.sketches.add(_resolve_plane(app, plane))
        if name:
            sk.name = name
        return sk

    def h_rect(sk, x1, y1, x2, y2):
        sk.sketchCurves.sketchLines.addTwoPointRectangle(_pt(x1, y1), _pt(x2, y2))
        return sk

    def h_circle(sk, cx, cy, r):
        sk.sketchCurves.sketchCircles.addByCenterRadius(_pt(cx, cy), r * MM)
        return sk

    def h_extrude(profile, dist_mm, operation='new', symmetric=False):
        feats = root.features.extrudeFeatures
        ein = feats.createInput(profile, _operation(operation))
        d = _vi(dist_mm * MM)
        if symmetric:
            ein.setSymmetricExtent(d, True)
        else:
            ein.setDistanceExtent(False, d)
        return feats.add(ein)

    return {
        'pt': _pt,                                  # pt(x_mm, y_mm, z_mm=0)
        'mm': lambda v: v * MM,                     # mm -> internal cm
        'vmm': lambda v: _vi(v * MM),               # ValueInput from mm
        'deg': math.radians,                        # degrees -> radians
        'new_sketch': h_sketch,
        'rect': h_rect,
        'circle': h_circle,
        'extrude_profile': h_extrude,
    }


def op_run_code(app, p):
    """Execute an arbitrary Fusion API snippet on the main thread.

    Available names: adsk, app, ui, design, root, math, MM, registry,
    reg(kind, obj) -> token, plus mm/degree helpers: pt, mm, vmm, deg,
    new_sketch(plane), rect(sk,x1,y1,x2,y2), circle(sk,cx,cy,r),
    extrude_profile(profile, dist_mm, operation, symmetric).
    Assign to `result` to return a value.
    """
    code = p['code']
    g = {
        'adsk': adsk,
        'app': app,
        'ui': app.userInterface,
        'design': _design(app),
        'root': _root(app),
        'math': math,
        'MM': MM,
        'registry': _registry,
        'reg': lambda kind, obj: _registry.add(kind, obj),
    }
    g.update(_run_code_helpers(app))
    local = {}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, g, local)  # noqa: S102 - intentional escape hatch
    result = local.get('result', g.get('result'))
    return {'stdout': buf.getvalue(), 'result': _jsonable(result)}


def op_reset_registry(app, p):
    _registry.reset()
    return {'reset': True}


# --------------------------------------------------------------------------- #
# Batch — many operations in ONE main-thread dispatch / round-trip
# --------------------------------------------------------------------------- #
_PATH_PART = re.compile(r'\.([A-Za-z_]\w*)|\[(\d+)\]')


def _resolve_ref(ref, results):
    """Resolve a "$alias.key[0].key2" reference against earlier batch results."""
    body = ref[1:]
    m = re.match(r'[A-Za-z_]\w*', body)
    if not m:
        raise ValueError('Bad batch reference: %r' % ref)
    alias = m.group(0)
    if alias not in results:
        raise KeyError('Batch reference to unknown alias %r in %r' % (alias, ref))
    value = results[alias]
    for part in _PATH_PART.finditer(body[m.end():]):
        key, idx = part.group(1), part.group(2)
        value = value[key] if key is not None else value[int(idx)]
    return value


def _resolve_params(obj, results):
    if isinstance(obj, dict):
        return {k: _resolve_params(v, results) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_params(v, results) for v in obj]
    if isinstance(obj, str):
        if obj.startswith('$$'):
            return obj[1:]          # escaped literal "$..."
        if obj.startswith('$'):
            return _resolve_ref(obj, results)
    return obj


def op_batch(app, p):
    """Run a list of operations in a single main-thread dispatch.

    Each item: {"op": str, "params": {...}, "as": optional alias}. Params may
    reference earlier results via "$alias.path" (e.g. "$s.sketch",
    "$r.profiles[0].token"), resolved just before each op runs. This collapses
    N round-trips and N main-thread hand-offs into one.
    """
    operations = p.get('operations') or []
    stop_on_error = bool(p.get('stop_on_error', True))
    results = {}
    out = []
    for i, item in enumerate(operations):
        op = item.get('op')
        try:
            params = _resolve_params(item.get('params') or {}, results)
            res = dispatch(app, op, params)
            out.append({'index': i, 'op': op, 'ok': True, 'result': res})
            if item.get('as'):
                results[item['as']] = res
        except Exception as exc:  # noqa: BLE001 - report and optionally continue
            out.append({'index': i, 'op': op, 'ok': False,
                        'error': '{}: {}'.format(type(exc).__name__, exc),
                        'traceback': traceback.format_exc()})
            if stop_on_error:
                return {'operations': out, 'completed': i, 'stopped': True}
    return {'operations': out, 'completed': len(out), 'stopped': False}


# --------------------------------------------------------------------------- #
# Dispatch table
# --------------------------------------------------------------------------- #
DISPATCH = {
    'ping': op_ping,
    'server_info': op_server_info,
    'get_state': op_get_state,
    'query_entities': op_query_entities,
    'create_sketch': op_create_sketch,
    'sketch_rectangle': op_sketch_rectangle,
    'sketch_circle': op_sketch_circle,
    'sketch_line': op_sketch_line,
    'sketch_arc': op_sketch_arc,
    'sketch_polygon': op_sketch_polygon,
    'sketch_points': op_sketch_points,
    'sketch_polyline': op_sketch_polyline,
    'sketch_spline': op_sketch_spline,
    'extrude': op_extrude,
    'revolve': op_revolve,
    'fillet': op_fillet,
    'chamfer': op_chamfer,
    'shell': op_shell,
    'combine': op_combine,
    'rectangular_pattern': op_rectangular_pattern,
    'circular_pattern': op_circular_pattern,
    'mirror': op_mirror,
    'move_body': op_move_body,
    'delete': op_delete,
    'hole': op_hole,
    'construction_plane': op_construction_plane,
    'construction_axis': op_construction_axis,
    'construction_point': op_construction_point,
    'sketch_constraint': op_sketch_constraint,
    'sketch_dimension': op_sketch_dimension,
    'project_to_sketch': op_project_to_sketch,
    'sketch_offset': op_sketch_offset,
    'sketch_fillet': op_sketch_fillet,
    'loft': op_loft,
    'sweep': op_sweep,
    'rib': op_rib,
    'draft': op_draft,
    'thread': op_thread,
    'split_body': op_split_body,
    'create_component': op_create_component,
    'rename': op_rename,
    'copy_body': op_copy_body,
    'joint': op_joint,
    'set_material': op_set_material,
    'set_appearance': op_set_appearance,
    'measure': op_measure,
    'bounding_box': op_bounding_box,
    'center_of_mass': op_center_of_mass,
    'interference': op_interference,
    'import_file': op_import_file,
    'bom': op_bom,
    'sketch_text': op_sketch_text,
    'emboss': op_emboss,
    'flat_pattern': op_flat_pattern,
    'export_flat_pattern': op_export_flat_pattern,
    'export_sketch_dxf': op_export_sketch_dxf,
    'import_mesh': op_import_mesh,
    'mesh_info': op_mesh_info,
    'mesh_to_brep': op_mesh_to_brep,
    'mesh_reduce': op_mesh_reduce,
    'mesh_remesh': op_mesh_remesh,
    'mesh_plane_cut': op_mesh_plane_cut,
    'canvas_add': op_canvas_add,
    'mesh_section': op_mesh_section,
    'create_drawing': op_create_drawing,
    'drawing_export': op_drawing_export,
    'get_selection': op_get_selection,
    'highlight': op_highlight,
    'set_visibility': op_set_visibility,
    'isolate': op_isolate,
    'unisolate': op_unisolate,
    'multi_screenshot': op_multi_screenshot,
    'section_view': op_section_view,
    'section_off': op_section_off,
    'undo': op_undo,
    'drive_joint': op_drive_joint,
    'set_joint_limits': op_set_joint_limits,
    'move_occurrence': op_move_occurrence,
    'ground_occurrence': op_ground_occurrence,
    'list_documents': op_list_documents,
    'open_document': op_open_document,
    'offset_face': op_offset_face,
    'scale': op_scale,
    'thicken': op_thicken,
    'mass_properties': op_mass_properties,
    'export_parameters': op_export_parameters,
    'import_parameters': op_import_parameters,
    'cam_setups': op_cam_setups,
    'cam_generate': op_cam_generate,
    'cam_post': op_cam_post,
    'timeline': op_timeline,
    'suppress_feature': op_suppress_feature,
    'list_parameters': op_list_parameters,
    'set_parameter': op_set_parameter,
    'add_parameter': op_add_parameter,
    'export': op_export,
    'screenshot': op_screenshot,
    'fit_view': op_fit_view,
    'save': op_save,
    'set_design_mode': op_set_design_mode,
    'batch': op_batch,
    'run_code': op_run_code,
    'reset_registry': op_reset_registry,
}


def dispatch(app, op, params):
    global _mutation_gen
    handler = DISPATCH.get(op)
    if handler is None:
        raise RuntimeError('Unknown op: %r (available: %s)'
                           % (op, ', '.join(sorted(DISPATCH))))
    result = handler(app, params or {})
    # Any mutating op invalidates the cached read-only views.
    if op not in _READ_ONLY_OPS:
        _mutation_gen += 1
        _state_cache.clear()
    return result
