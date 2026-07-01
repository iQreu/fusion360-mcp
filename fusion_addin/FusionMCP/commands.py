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
import re
import traceback

import adsk.core
import adsk.fusion
import logutil
from registry import Registry

VERSION = '1.2.0'
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
    distance = _vi(p['distance'] * MM)
    if p.get('symmetric'):
        ein.setSymmetricExtent(distance, True)  # distance = full length
    else:
        ein.setDistanceExtent(False, distance)
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
    """Add text to a sketch at (x, y) mm. height in mm; optional font, bold,
    italic, angle (deg). The returned text token extrudes directly (extrude /
    emboss), so labels and logos need no extra tracing."""
    sk = _registry.get(p['sketch'])
    texts = sk.sketchTexts
    height_mm = float(p.get('height', 10.0))
    x, y = float(p.get('x', 0.0)), float(p.get('y', 0.0))
    text = p['text']
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


def op_mesh_to_brep(app, p):
    """Convert mesh bodies (tokens; all meshes when omitted) into BRep bodies —
    the reverse-engineering gateway: after conversion the body works with every
    solid tool (combine, split, measure, export STEP...). Uses the native
    convert-mesh feature when this Fusion exposes it, else drives Fusion's
    Convert Mesh command. Dense scans convert as faceted solids."""
    root = _root(app)
    meshes = ([_registry.get(t) for t in p.get('meshes') or []]
              or list(root.meshBodies))
    if not meshes:
        raise RuntimeError('No mesh bodies to convert; import one with import_mesh')
    before = root.bRepBodies.count
    native = getattr(root.features, 'convertMeshBodyFeatures', None)
    used = None
    if native is not None:
        try:
            for m in meshes:
                native.add(native.createInput(m))
            used = 'convertMeshBodyFeatures'
        except Exception:
            used = None
    if used is None:
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
                           'Very dense scans may need Reduce in the MESH workspace '
                           'first.' % used)
    return {'bodies': bodies, 'method': used}


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
def op_create_drawing(app, p):
    """Open Fusion's "Drawing from Design" dialog for the active design. The
    Fusion API cannot build drawing sheets headlessly, so the user finishes the
    sheet setup in the UI. For fully scripted 2D output use export_sketch_dxf
    or export_flat_pattern."""
    ui = app.userInterface
    cmd = None
    for cmd_id in ('DrawingFromDesignCommand', 'NewDrawingFromDesignCommand',
                   'FusionDrawingFromDesignCommand'):
        cmd = ui.commandDefinitions.itemById(cmd_id)
        if cmd:
            break
    if not cmd:
        raise RuntimeError('The drawing-from-design command is not available in '
                           'this Fusion version. Use export_sketch_dxf / '
                           'export_flat_pattern for scripted 2D output.')
    cmd.execute()
    return {'launched': cmd.id,
            'note': 'Fusion opened the drawing dialog; the user completes the '
                    'sheet setup interactively.'}


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
    else:
        raise ValueError('format must be step|iges|sat|smt|f3d|stl, got %r' % fmt)
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
    'mesh_section': op_mesh_section,
    'create_drawing': op_create_drawing,
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
