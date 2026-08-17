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
import struct
import tempfile
import time
import traceback

import adsk.core
import adsk.fusion
import logutil
from registry import Registry

VERSION = '1.14.0'
MM = 0.1  # 1 mm = 0.1 cm (Fusion internal length unit)

_registry = Registry()

# Identity of the active document, so dispatch() can drop entity tokens when
# the user (or open_document) switches to a DIFFERENT document — tokens are
# per-document, and a stale doc-A token must never resolve against doc B.
# Keyed on (creationId, dataFile.id): creationId survives the first save (so
# saving a new design is not mistaken for a switch) but differs for File > New
# (so a saved -> unsaved switch IS detected). The cloud id disambiguates open
# copies of one document, which share a creationId.
_active_doc_key = (None, None)

# Cross-call object store for run_code: store('jig', obj) in one snippet,
# fetch('jig') in a later one. Session-lived, like the token registry, but
# holds arbitrary Python objects (inputs, dicts, ...), not just entities.
_code_store = {}

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
    'electronics_info', 'electronics_components', 'electronics_nets',
    'electronics_layers', 'electronics_library', 'electronics_export',
    'mesh_compare', 'thread_types', 'api_introspect', 'selection_filter',
    'list_materials', 'list_appearances', 'data_folders', 'version_history',
    'share_link', 'annotate', 'annotations_clear',
    'design_diagnostics', 'sketch_status',
    # File writers that do not mutate the design (cache-wise read-only; the
    # server still omits readOnlyHint on them because they write user paths).
    'mesh_export', 'face_groups', 'canvas_list',
    # Popups: no design mutation.
    'show_message', 'notify_update',
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


def _validate_or_add(feats, fin, kind):
    """validate_only tail shared by sweep/loft/shell: Fusion has no dry-run API,
    so actually try the add, summarise what it made, then delete the feature
    again. No tokens are registered for the transient geometry. If the delete
    fails the feature is REAL — report it as a normal creation instead of
    leaking an unregistered timeline entry."""
    try:
        feat = feats.add(fin)
    except Exception as exc:  # noqa: BLE001 - the invalid input IS the answer
        return {'valid': False, 'kind': kind, 'error': str(exc)}
    summary = {}
    with contextlib.suppress(Exception):
        summary['bodies'] = feat.bodies.count
        summary['faces'] = sum(feat.bodies.item(i).faces.count
                               for i in range(feat.bodies.count))
    removed = False
    with contextlib.suppress(Exception):
        removed = bool(feat.deleteMe())
    if not removed:
        res = _feature_result(feat, kind)
        res.update({'valid': True, 'committed': True,
                    'note': 'validate_only could not remove the test feature, '
                            'so it was kept — these tokens are real.'})
        return res
    out = {'valid': True, 'committed': False, 'kind': kind}
    out.update(summary)
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
    # Surface ops that finished AFTER their client timed out (see bridge.py) so
    # a stuck/slow machine is diagnosable.
    with contextlib.suppress(Exception):
        import bridge
        late = bridge._state.get('late_completions', 0)
        if late:
            info['late_completions'] = late
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
    faces = p.get('faces', [])
    feats = _root(app).features.shellFeatures
    if faces:
        entities = _collection(faces)
    else:
        # No faces to remove -> hollow the whole body (closed shell). The input
        # collection still needs the target body, or Fusion has nothing to act on.
        # _registry.get (not get_opt) so a STALE body token raises the helpful
        # KeyError instead of a misleading "you passed no body".
        body = _registry.get(p['body']) if p.get('body') else None
        if body is None:
            raise ValueError('shell needs either faces=[tokens] to remove, or a '
                             'body token to hollow with no opening')
        entities = adsk.core.ObjectCollection.create()
        entities.add(body)
    sin = feats.createInput(entities, False)
    sin.insideThickness = _vi(p['thickness'] * MM)
    if p.get('validate_only'):
        return _validate_or_add(feats, sin, 'shell')
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
    # deleteMe() reports failure by returning False (e.g. an entity consumed by
    # later features, isDeletable == False) — it does not always raise.
    if obj.deleteMe() is False:
        raise RuntimeError('Fusion refused to delete %s (deleteMe returned '
                           'False — the entity may not be deletable in the '
                           'current context).' % p['token'])
    # Forget the token so a later call gets the registry's helpful "stale token"
    # KeyError instead of a cryptic Fusion "object is invalid" from deep inside a
    # feature add.
    _registry.remove(p['token'])
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
    if p.get('extended') is not None:
        # July 2026+: isExtended=True stretches the plane display to the
        # viewport; False keeps the compact resized square.
        with contextlib.suppress(Exception):
            plane.isExtended = bool(p['extended'])
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
        # setByEdge takes a linear BRepEdge/SketchLine and works in parametric
        # designs; setByLine wants a transient Line3D (direct-edit only).
        ain.setByEdge(_registry.get(p['edge']))
    elif method == 'cylinder':
        ain.setByCircularFace(_registry.get(p['face']))
    else:
        raise ValueError('method must be two_points|edge|cylinder, got %r' % method)
    axis = axes.add(ain)
    return {'axis': _registry.add('cax', axis), 'method': method}


def op_construction_point(app, p):
    """Create a construction point. method: at_point (vertex/sketch-point token),
    two_edges (2 edge tokens), edge_plane (edge token + plane),
    distance_on_path (edge/sketch-curve token + ratio 0..1 along it,
    Fusion July 2026+)."""
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
    elif method == 'distance_on_path':
        setter = getattr(cin, 'setByDistanceOnPath', None)
        if setter is None:
            raise RuntimeError('distance_on_path needs Fusion July 2026+')
        # The distance is a normalised ratio: 0 = path start, 1 = path end.
        setter(_registry.get(p['path']), _vi(float(p.get('ratio', 0.5))))
    else:
        raise ValueError('method must be at_point|two_edges|edge_plane|'
                         'distance_on_path, got %r' % method)
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


def _dim_point(entity):
    """A SketchPoint usable by addDistanceDimension. Passes SketchPoints
    through; for a SketchLine returns an endpoint (its start), since
    addDistanceDimension only accepts SketchPoints."""
    for attr in ('startSketchPoint',):
        pt = getattr(entity, attr, None)
        if pt is not None:
            return pt
    return entity


def op_sketch_dimension(app, p):
    """Add a driving dimension to a sketch. kind: distance (2 point OR line
    tokens — a line uses its start endpoint + at x,y mm text position),
    radius|diameter (circle/arc token), angle (2 lines).
    Optional parameter=name renames the dimension's parameter."""
    sk = _registry.get(p['sketch'])
    dims = sk.sketchDimensions
    kind = (p.get('kind') or 'distance').lower()
    ents = [_registry.get(t) for t in p.get('entities', [])]
    tx = _pt(p.get('text_x', 0), p.get('text_y', 0))
    if kind == 'distance':
        orient = adsk.fusion.DimensionOrientations.AlignedDimensionOrientation
        # addDistanceDimension needs SketchPoints; map line tokens to endpoints.
        a, b = _dim_point(ents[0]), _dim_point(ents[1])
        d = dims.addDistanceDimension(a, b, orient, tx)
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


def _curve_endpoints(curve):
    """{'start': Point3D, 'end': Point3D} for an open sketch curve."""
    out = {}
    for which, attr in (('start', 'startSketchPoint'), ('end', 'endSketchPoint')):
        with contextlib.suppress(Exception):
            out[which] = getattr(curve, attr).geometry
    return out


def _closest_endpoint_pair(curve_a, curve_b):
    """The (Point3D, Point3D) endpoints of two curves nearest each other — the
    shared/near corner, regardless of how each line was drawn."""
    ea, eb = _curve_endpoints(curve_a), _curve_endpoints(curve_b)
    if not ea or not eb:
        raise RuntimeError('a sketch curve has no endpoints (closed curve?)')

    def dist2(u, v):
        return (u.x - v.x) ** 2 + (u.y - v.y) ** 2 + (u.z - v.z) ** 2

    return min(((pa, pb) for pa in ea.values() for pb in eb.values()),
               key=lambda pair: dist2(*pair))


def op_sketch_fillet(app, p):
    """Add a 2D fillet of `radius` mm between two sketch lines that share (or
    nearly share) an endpoint (line tokens). The fillet is anchored at the two
    endpoints closest to each other, so it works no matter which way each line
    was drawn."""
    sk = _registry.get(p['sketch'])
    l0, l1 = _registry.get(p['line1']), _registry.get(p['line2'])
    pt0, pt1 = _closest_endpoint_pair(l0, l1)
    arc = sk.sketchCurves.sketchArcs.addFillet(l0, pt0, l1, pt1, p['radius'] * MM)
    return {'sketch': _registry.add('skt', sk), 'arc': _registry.add('arc', arc)}


# --------------------------------------------------------------------------- #
# Advanced features (loft / sweep / rib / draft / thread / split)
# --------------------------------------------------------------------------- #
def op_sketch_blend_curve(app, p):
    """Bridge two OPEN sketch curves with a smooth fitted spline (Fusion July
    2026+). Ends are picked automatically (closest endpoints) unless
    end1/end2 ("start"|"end") force a side. curvature=True gives a G2 blend
    (default G1/tangent)."""
    sk = _registry.get(p['sketch'])
    adder = getattr(sk.sketchCurves.sketchFittedSplines, 'addBlendCurve', None)
    if adder is None:
        raise RuntimeError('addBlendCurve needs Fusion July 2026+')
    c1, c2 = _registry.get(p['curve1']), _registry.get(p['curve2'])
    e1, e2 = _curve_endpoints(c1), _curve_endpoints(c2)
    if not e1 or not e2:
        raise RuntimeError('curve has no endpoints — closed curves cannot blend')
    pick1 = (p.get('end1') or '').lower()
    pick2 = (p.get('end2') or '').lower()
    if pick1 in e1 and pick2 in e2:
        p1, p2 = e1[pick1], e2[pick2]
    else:
        p1, p2 = _closest_endpoint_pair(c1, c2)
    spline = adder(c1, p1, c2, p2, bool(p.get('curvature', False)))
    out = _profiles_summary(sk)
    out['curve'] = _registry.add('spl', spline)
    return out


def op_auto_constrain(app, p):
    """Run Fusion's AutoConstrain on a sketch (2026+): adds the geometric
    constraints a human would (horizontal/vertical/coincident/...), stabilising
    imported DXF or hand-drawn geometry in one call."""
    sk = _registry.get(p['sketch'])
    maker = getattr(sk, 'createAutoConstrainInput', None)
    runner = getattr(sk, 'autoConstrain', None)
    if not (maker and runner):
        raise RuntimeError('The AutoConstrain API is not in this Fusion build '
                           '(needs 2026+).')
    result = runner(maker())
    out = {'sketch': p['sketch'], 'applied': result is not None}
    for attr, key in (('constraintCount', 'constraints_added'),
                      ('dimensionCount', 'dimensions_added')):
        with contextlib.suppress(Exception):
            out[key] = getattr(result, attr)
    with contextlib.suppress(Exception):
        out['moved_geometry'] = result.movedGeometry.count
    return out


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
    if p.get('validate_only'):
        return _validate_or_add(feats, lin, 'loft')
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
    if p.get('validate_only'):
        return _validate_or_add(feats, sin, 'sweep')
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
    # recommendThreadData returns (ok, designation, threadClass) — a 3-tuple; the
    # designation (e.g. "M10x1.5") already encodes the size.
    ok, designation, cls = query.recommendThreadData(
        face.geometry.radius * 2, is_internal, thread_type)
    if not ok:
        raise RuntimeError('No recommended thread data for this face diameter')
    info = feats.createThreadInfo(is_internal, thread_type, designation, cls)
    tin = feats.createInput(face, info)
    tin.isModeled = bool(p.get('modeled', True))
    return _feature_result(feats.add(tin), 'thread')


def op_thread_types(app, p):
    """List the thread standards available to the thread tool — built-in
    types plus (Fusion July 2026+) custom thread libraries hosted on the
    team hub."""
    query = _root(app).features.threadFeatures.threadDataQuery
    out = {}
    for attr, key in (('defaultMetricThreadType', 'default_metric'),
                      ('defaultInchThreadType', 'default_inch')):
        with contextlib.suppress(Exception):
            out[key] = getattr(query, attr)
    for attr, key in (('allThreadTypes', 'all'), ('publicThreadTypes', 'public')):
        with contextlib.suppress(Exception):
            out[key] = list(getattr(query, attr))
    hubs = []
    with contextlib.suppress(Exception):
        for hub_id in query.availableHubLibraryIds:
            entry = {'id': hub_id}
            with contextlib.suppress(Exception):
                entry['name'] = query.getHubLibraryDisplayName(hub_id)
            with contextlib.suppress(Exception):
                entry['thread_types'] = list(query.getHubLibraryThreadTypes(hub_id))
            hubs.append(entry)
    if hubs:
        out['hub_libraries'] = hubs
    if not out:
        raise RuntimeError('Thread data query returned nothing — is a design open?')
    return out


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


def _as_component(obj):
    """The Component for a component/occurrence token (or None)."""
    if obj is None:
        return None
    comp = getattr(obj, 'component', None)  # Occurrence -> its component
    if comp is not None:
        return comp
    if getattr(obj, 'objectType', '').endswith('Component'):
        return obj
    return None


def op_copy_body(app, p):
    """Copy body tokens into a target component/occurrence token (or root if
    omitted) using copy/paste. Returns tokens of the pasted bodies."""
    root = _root(app)
    bodies = _collection(p['bodies'])
    target_comp = root
    if p.get('target'):
        target_comp = _as_component(_registry.get(p['target']))
        if target_comp is None:
            raise ValueError('target must be a component or occurrence token, '
                             'got %r' % p['target'])
    # copyPasteBodies pastes into the component that owns the collection, so use
    # the TARGET component's collection — not always the root's.
    result = target_comp.features.copyPasteBodies.add(bodies)
    out = []
    try:
        for i in range(result.bodies.count):
            out.append(_registry.add('bdy', result.bodies.item(i)))
    except Exception:
        pass
    return {'bodies': out, 'target': target_comp.name}


_JOINT_MOTION = ('rigid', 'revolute', 'slider', 'cylindrical', 'pin_slot',
                 'planar', 'ball')


def _joint_geometry(token):
    """Build a JointGeometry from a planar-face token (centre keypoint), a
    curve/edge token, or a vertex/sketch-point/construction-point token."""
    obj = _registry.get(token)
    key = adsk.fusion.JointKeyPointTypes.CenterKeyPoint
    try:
        return adsk.fusion.JointGeometry.createByPlanarFace(obj, None, key)
    except Exception:
        pass
    try:
        return adsk.fusion.JointGeometry.createByCurve(obj, key)
    except Exception:
        pass
    try:
        return adsk.fusion.JointGeometry.createByPoint(obj)
    except Exception:
        raise ValueError('token %r is not usable joint geometry (need a planar '
                         'face, an edge/curve, or a vertex/sketch point)' % token)


def _apply_joint_motion(jin, motion, axis_name):
    """Set the motion type on a JointInput OR AsBuiltJointInput (both expose the
    same setAs*JointMotion family)."""
    if motion not in _JOINT_MOTION:
        raise ValueError('motion must be one of %s, got %r' % (list(_JOINT_MOTION), motion))
    axis_map = {
        'X': adsk.fusion.JointDirections.XAxisJointDirection,
        'Y': adsk.fusion.JointDirections.YAxisJointDirection,
        'Z': adsk.fusion.JointDirections.ZAxisJointDirection,
    }
    axis = axis_map.get((axis_name or 'Z').upper(), axis_map['Z'])
    if motion == 'revolute':
        jin.setAsRevoluteJointMotion(axis)
    elif motion == 'slider':
        jin.setAsSliderJointMotion(axis)
    elif motion == 'cylindrical':
        jin.setAsCylindricalJointMotion(axis)
    elif motion == 'planar':
        jin.setAsPlanarJointMotion(axis)
    elif motion == 'ball':
        jin.setAsBallJointMotion(axis_map['Z'], axis_map['X'])
    elif motion == 'pin_slot':
        jin.setAsPinSlotJointMotion(axis, axis_map['X'])
    else:
        jin.setAsRigidJointMotion()


def op_joint(app, p):
    """Create a joint between two geometry tokens (planar faces recommended).
    motion: rigid|revolute|slider|cylindrical|pin_slot|planar|ball. For
    revolute/cylindrical an axis ("X"/"Y"/"Z") sets the rotation axis."""
    root = _root(app)
    geo0 = _joint_geometry(p['geo0'])
    geo1 = _joint_geometry(p['geo1'])
    jin = root.joints.createInput(geo0, geo1)
    motion = (p.get('motion') or 'rigid').lower()
    _apply_joint_motion(jin, motion, p.get('axis'))
    joint = root.joints.add(jin)
    return {'joint': _registry.add('jnt', joint), 'motion': motion}


def op_as_built_joint(app, p):
    """Joint two occurrences WHERE THEY ALREADY SIT (no geometry snapping) —
    the right tool for imported/positioned assemblies. occ0/occ1 are occurrence
    tokens; motion as in `joint`; optional geometry token (a face/edge for the
    joint origin, required for revolute/cylindrical/etc., omit for rigid)."""
    root = _root(app)
    occ0 = _registry.get(p['occ0'])
    occ1 = _registry.get(p['occ1'])
    geometry = _joint_geometry(p['geometry']) if p.get('geometry') else None
    joints = getattr(root, 'asBuiltJoints', None)
    if joints is None:
        raise RuntimeError('As-built joints are not available in this Fusion build.')
    jin = joints.createInput(occ0, occ1, geometry)
    motion = (p.get('motion') or 'rigid').lower()
    _apply_joint_motion(jin, motion, p.get('axis'))
    joint = joints.add(jin)
    return {'joint': _registry.add('jnt', joint), 'motion': motion,
            'kind': 'as_built'}


def op_joint_origin(app, p):
    """Create a named joint origin at a geometry token (face/edge/vertex/sketch
    point) so later joints can snap to a stable, explicit reference point."""
    root = _root(app)
    origins = getattr(root, 'jointOrigins', None)
    if origins is None:
        raise RuntimeError('Joint origins are not available in this Fusion build.')
    geo = _joint_geometry(p['geometry'])
    oin = origins.createInput(geo)
    origin = origins.add(oin)
    if p.get('name'):
        with contextlib.suppress(Exception):
            origin.name = p['name']
    return {'joint_origin': _registry.add('jor', origin),
            'name': getattr(origin, 'name', None)}


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
    units = p.get('units', 'mm')
    if isinstance(value, str):
        vi = adsk.core.ValueInput.createByString(value)
    else:
        # createByReal is in Fusion internal units (cm for lengths), which would
        # silently make value=25 units='mm' a 250 mm parameter. Embed the unit so
        # the wire's mm/deg/etc. is honoured (matches set_parameter via expression).
        # repr() round-trips the float exactly (unlike %g, which drops to 6 sig figs).
        vi = adsk.core.ValueInput.createByString('%s %s' % (repr(float(value)), units))
    prm = design.userParameters.add(p['name'], vi, units, p.get('comment', ''))
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
    # Document appearances first (covers create_appearance results and any
    # appearance the user customised in-document), then favourites, then the
    # shipped libraries. An explicit `library` skips straight to the libraries.
    if not library:
        with contextlib.suppress(Exception):
            a = _design(app).appearances.itemByName(name)
            if a is not None:
                return a
        with contextlib.suppress(Exception):
            a = app.favoriteAppearances.itemByName(name)
            if a is not None:
                return a
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


def op_create_appearance(app, p):
    """Create a custom appearance IN THE DOCUMENT: copy a base appearance
    (default: a matte plastic from the shipped libraries) under a new name,
    then recolour it (r/g/b 0-255, optional alpha) and set surface roughness
    (0..1). base/library pick the appearance to copy. The result is
    immediately usable: set_appearance(body, name)."""
    design = _design(app)
    name = p.get('name')
    if not name:
        raise ValueError('name is required')
    # Validate everything BEFORE addByCopy — there is no rollback for a
    # half-configured appearance.
    rgb = [p.get(k) for k in ('r', 'g', 'b')]
    color = None
    if any(v is not None for v in rgb):
        vals = []
        for v in rgb + [p.get('alpha', 255)]:
            v = int(v or 0)
            if not 0 <= v <= 255:
                raise ValueError('r/g/b/alpha must be 0-255, got %r' % v)
            vals.append(v)
        color = adsk.core.Color.create(*vals)
    roughness = p.get('roughness')
    if roughness is not None:
        roughness = float(roughness)
        if not 0.0 <= roughness <= 1.0:
            raise ValueError('roughness must be 0..1, got %r' % p['roughness'])
    existing = None
    with contextlib.suppress(Exception):
        existing = design.appearances.itemByName(str(name))
    if existing is not None:
        raise ValueError('An appearance named %r already exists in this '
                         'document — pick another name, or assign it with '
                         'set_appearance.' % name)
    base = None
    if p.get('base'):
        base = _find_appearance(app, p['base'], p.get('library'))
    else:
        # A plastic takes an albedo recolour most predictably.
        for guess in ('Plastic - Matte (Black)', 'Paint - Enamel Glossy (Black)'):
            with contextlib.suppress(Exception):
                base = _find_appearance(app, guess)
            if base is not None:
                break
        if base is None:
            libs = app.materialLibraries
            for i in range(libs.count):
                with contextlib.suppress(Exception):
                    if libs.item(i).appearances.count:
                        base = libs.item(i).appearances.item(0)
                        break
    if base is None:
        raise RuntimeError('No base appearance found to copy — pass '
                           'base=<name from list_appearances>.')
    new = design.appearances.addByCopy(base, str(name))
    out = {'appearance': new.name, 'base': base.name, 'source': 'document'}
    if color is not None:
        colored = False
        candidates = []
        with contextlib.suppress(Exception):
            prop = new.appearanceProperties.itemById('opaque_albedo')
            if prop is not None:
                candidates.append(prop)
        with contextlib.suppress(Exception):
            props = new.appearanceProperties
            candidates.extend(props.item(i) for i in range(props.count))
        for prop in candidates:
            cp = None
            with contextlib.suppress(Exception):
                cp = adsk.core.ColorProperty.cast(prop)
            if cp is None:
                continue
            with contextlib.suppress(Exception):
                cp.value = color
                colored = True
            if not colored:
                # Some colour slots are list-valued (texture-connected).
                with contextlib.suppress(Exception):
                    cp.values = [color]
                    colored = True
            if colored:
                break
        out['colored'] = colored
        if not colored:
            out['note'] = ('The copied appearance exposes no writable colour '
                           'property — pick a different base '
                           '(list_appearances).')
    if roughness is not None:
        rough_set = False
        with contextlib.suppress(Exception):
            prop = new.appearanceProperties.itemById('surface_roughness')
            if prop is not None:
                prop.value = roughness
                rough_set = True
        out['roughness_set'] = rough_set
    return out


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
        hit = {'volume_mm3': vol}
        # Report WHICH pair overlaps, so callers with 3+ bodies can act.
        for prop, key in (('entityOne', 'body_a'), ('entityTwo', 'body_b')):
            with contextlib.suppress(Exception):
                ent = getattr(r, prop)
                hit[key] = _registry.add('bdy', ent)
                hit[key + '_name'] = ent.name
        hits.append(hit)
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


# The one Timeline Builder job of this session. The cloud service takes its
# time, so start/status/open are separate actions instead of one long block.
_tb_job = None

_JOB_STATUS = {0: 'not_started', 1: 'in_progress', 2: 'completed', 3: 'failed'}


def _tb_status(job):
    with contextlib.suppress(Exception):
        return _JOB_STATUS.get(int(job.status), 'unknown')
    return 'unknown'


def op_timeline_builder(app, p):
    """Rebuild an editable parametric timeline from a bare BRep body — an
    imported STEP becomes a design with real features (Timeline Builder cloud
    service, Fusion July 2026+ preview). action="start" (body token; waits up
    to `timeout` s, default 60), "status" (poll the running job), "open"
    (activate the produced document — re-orient with get_state after)."""
    global _tb_job
    action = (p.get('action') or 'start').lower()
    tbj = getattr(adsk.fusion, 'TimelineBuilderJob', None)
    if tbj is None or not hasattr(tbj, 'createTimeline'):
        raise RuntimeError('TimelineBuilderJob is not available in this Fusion '
                           'build (needs Fusion July 2026+).')
    if action == 'start':
        body = _registry.get(p['body'])
        job = tbj.createTimeline(body)
        if job is None:
            raise RuntimeError('Fusion refused to start the Timeline Builder '
                               'job — cloud service unreachable, or this body '
                               'is not a valid input.')
        _tb_job = job
        requested = float(p.get('timeout', 60))
        deadline = time.time() + min(requested,
                                     max(30.0, _main_thread_ceiling() - 15.0))
        while _tb_status(job) in ('not_started', 'in_progress') \
                and time.time() < deadline:
            adsk.doEvents()
            time.sleep(0.5)
        status = _tb_status(job)
    elif action == 'status':
        if _tb_job is None:
            raise RuntimeError('No Timeline Builder job was started this '
                               'session — call timeline_builder(action='
                               '"start", body=...) first.')
        job = _tb_job
        status = _tb_status(job)
    elif action == 'open':
        if _tb_job is None:
            raise RuntimeError('No Timeline Builder job was started this '
                               'session.')
        doc = None
        with contextlib.suppress(Exception):
            doc = _tb_job.resultDocument
        if doc is None:
            raise RuntimeError('The job has not produced a document (status: '
                               '%s).' % _tb_status(_tb_job))
        doc.activate()
        return {'activated': doc.name,
                'note': 'The rebuilt parametric design is now the active '
                        'document — call get_state to re-orient (old tokens '
                        'are invalid).'}
    else:
        raise ValueError('action must be start|status|open, got %r' % action)
    out = {'status': status}
    if status == 'completed':
        with contextlib.suppress(Exception):
            out['result_document'] = job.resultDocument.name
        out['note'] = 'Open the rebuilt design with timeline_builder(action="open").'
    elif status in ('not_started', 'in_progress'):
        out['note'] = ('Cloud job still running — check again with '
                       'timeline_builder(action="status").')
    elif status == 'failed':
        out['note'] = ('The Timeline Builder service could not rebuild this '
                       'body (it was cancelled or the geometry is not '
                       'supported).')
    return out


# --------------------------------------------------------------------------- #
# Diagnostics: design health, sketch pre-flight
# --------------------------------------------------------------------------- #
def op_design_diagnostics(app, p):
    """One-call health report for the active design: timeline features in
    error/warning state (with Fusion's own message), sketches that are not
    fully constrained, non-solid (open) bodies, empty components and unsaved
    changes. limit caps the issue list (default 100). Run it after a big
    batch or before export/print to catch silent modelling problems."""
    design = _design(app)
    limit = int(p.get('limit') or 100)
    issues = []
    timeline_errors = timeline_warnings = 0

    # healthState names are resolved dynamically, so a build that renumbers or
    # adds states cannot make the mapping lie.
    states = {}
    hs = getattr(adsk.fusion, 'FeatureHealthStates', None)
    if hs is not None:
        for nm in dir(hs):
            if nm.endswith('FeatureHealthState'):
                with contextlib.suppress(Exception):
                    states[int(getattr(hs, nm))] = \
                        nm[:-len('FeatureHealthState')].lower()
    with contextlib.suppress(Exception):
        tl = design.timeline
        for i in range(tl.count):
            it = tl.item(i)
            state = None
            with contextlib.suppress(Exception):
                state = states.get(int(it.healthState))
            if state not in ('error', 'warning'):
                continue
            if state == 'error':
                timeline_errors += 1
            else:
                timeline_warnings += 1
            entry = {'kind': 'timeline_' + state, 'index': i}
            with contextlib.suppress(Exception):
                entry['name'] = it.name
            with contextlib.suppress(Exception):
                msg = it.errorOrWarningMessage
                if msg:
                    entry['message'] = msg
            issues.append(entry)

    unconstrained = open_bodies = empty_components = 0
    with contextlib.suppress(Exception):
        root = design.rootComponent
        for comp in design.allComponents:
            with contextlib.suppress(Exception):
                for sk in comp.sketches:
                    if sk.isFullyConstrained:
                        continue
                    unconstrained += 1
                    issues.append({'kind': 'sketch_not_fully_constrained',
                                   'sketch': _registry.add('skt', sk),
                                   'name': sk.name, 'component': comp.name})
            with contextlib.suppress(Exception):
                for body in comp.bRepBodies:
                    if body.isSolid:
                        continue
                    open_bodies += 1
                    issues.append({'kind': 'open_body',
                                   'body': _registry.add('bdy', body),
                                   'name': body.name, 'component': comp.name,
                                   'note': 'Surface (non-solid) body — it '
                                           'will not export or print as a '
                                           'solid.'})
            with contextlib.suppress(Exception):
                if comp is not root and comp.bRepBodies.count == 0 \
                        and comp.sketches.count == 0 \
                        and comp.occurrences.count == 0 \
                        and comp.meshBodies.count == 0:
                    empty_components += 1
                    issues.append({'kind': 'empty_component',
                                   'name': comp.name})

    out = {'healthy': not issues, 'issue_count': len(issues),
           'issues': issues[:limit],
           'timeline_errors': timeline_errors,
           'timeline_warnings': timeline_warnings,
           'unconstrained_sketches': unconstrained,
           'open_bodies': open_bodies,
           'empty_components': empty_components}
    if len(issues) > limit:
        out['note'] = 'Issue list truncated to %d of %d.' % (limit, len(issues))
    with contextlib.suppress(Exception):
        out['unsaved_changes'] = bool(app.activeDocument.isModified)
    return out


def op_sketch_status(app, p):
    """Pre-flight a sketch before sweep/loft/shell — most failed attempts
    trace back to an open or missing profile. For one sketch token (or every
    root sketch when omitted) report: profile count, fully-constrained state,
    curve/construction counts, and OPEN ENDPOINTS (positions in sketch mm
    where exactly one curve ends) — an open chain never forms a profile, so
    these are exactly where a closing segment or coincident constraint is
    missing."""
    if p.get('sketch'):
        sketches = [_registry.get(p['sketch'])]
    else:
        sketches = list(_root(app).sketches)
    out = []
    for sk in sketches:
        entry = {'token': _registry.add('skt', sk)}
        with contextlib.suppress(Exception):
            entry['name'] = sk.name
        with contextlib.suppress(Exception):
            entry['fully_constrained'] = bool(sk.isFullyConstrained)
        with contextlib.suppress(Exception):
            entry['profiles'] = sk.profiles.count
        curves = construction = 0
        ends = {}
        with contextlib.suppress(Exception):
            for c in sk.sketchCurves:
                is_constr = False
                with contextlib.suppress(Exception):
                    is_constr = bool(c.isConstruction)
                if is_constr:
                    construction += 1
                    continue
                curves += 1
                # Endpoint census: coincident-by-position counts as joined,
                # matching how profiles close. Closed curves (circles,
                # ellipses) have no endpoints — the suppress skips them.
                for prop in ('startSketchPoint', 'endSketchPoint'):
                    with contextlib.suppress(Exception):
                        g = getattr(c, prop).geometry
                        key = (round(g.x, 4), round(g.y, 4))
                        ends[key] = ends.get(key, 0) + 1
        entry['curves'] = curves
        entry['construction_curves'] = construction
        open_pts = sorted(k for k, n in ends.items() if n == 1)
        entry['open_endpoint_count'] = len(open_pts)
        entry['open_endpoints_mm'] = [[round(x / MM, 4), round(y / MM, 4)]
                                      for x, y in open_pts[:20]]
        out.append(entry)
    return {'count': len(out), 'sketches': out}


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
    fmt = (p.get('format') or 'dxf').lower()
    if fmt == 'dxf':
        opts = em.createDXFFlatPatternExportOptions(p['path'], flat)
    elif fmt == 'step':
        creator = getattr(em, 'createSTEPExportOptionsForFlatPattern', None)
        if creator is None:
            raise RuntimeError('STEP flat-pattern export needs Fusion July 2026+; '
                               'use format="dxf" on this build.')
        opts = creator(p['path'], flat)
    else:
        raise ValueError('format must be dxf|step, got %r' % fmt)
    if not em.execute(opts):
        raise RuntimeError('Flat-pattern export of %r failed (execute returned '
                           'false — file locked or path unwritable).' % p['path'])
    return {'exported': p['path'], 'format': fmt}


def _sheet_metal_feats(app, name):
    feats = getattr(_root(app).features, name, None)
    if feats is None:
        raise RuntimeError('%s is not available in this Fusion build (the '
                           'sheet-metal fold/join API needs July 2026+).' % name)
    return feats


def op_fold(app, p):
    """Fold a sheet-metal body along a bend line (Fusion July 2026+ preview).
    `face` = stationary face token (stays fixed), `bend_line` = a sketch-line
    token drawn across that face. Optional angle (deg, default 90), bend
    radius (mm) and corner_relief."""
    feats = _sheet_metal_feats(app, 'foldFeatures')
    fin = feats.createInput(_registry.get(p['face']))
    lines = fin.bendLines
    adder = getattr(lines, 'add', None) or getattr(lines, 'addBendLine', None)
    if adder is None:
        raise RuntimeError('FoldFeatureInput.bendLines exposes no add method '
                           'in this build — the preview API changed.')
    bend = adder(_registry.get(p['bend_line']))
    if bend is not None and p.get('angle') is not None:
        angle = _vi(math.radians(float(p['angle'])))
        for attr in ('angle', 'bendAngle', 'foldAngle'):
            if hasattr(bend, attr):
                with contextlib.suppress(Exception):
                    setattr(bend, attr, angle)
                    break
    if bend is not None and p.get('radius') is not None:
        radius = _vi(float(p['radius']) * MM)
        for attr in ('bendRadius', 'radius'):
            if hasattr(bend, attr):
                with contextlib.suppress(Exception):
                    setattr(bend, attr, radius)
                    break
    if p.get('corner_relief') is not None:
        with contextlib.suppress(Exception):
            fin.isUseCornerRelief = bool(p['corner_relief'])
    return _feature_result(feats.add(fin), 'fold')


def op_join_by_bend(app, p):
    """Join two sheet-metal bodies with a bend between two linear edges of
    DIFFERENT bodies (Fusion July 2026+ preview). Optional bend radius (mm)."""
    feats = _sheet_metal_feats(app, 'joinByBendFeatures')
    jin = feats.createInput(_registry.get(p['edge_a']), _registry.get(p['edge_b']))
    if p.get('radius') is not None:
        radius = _vi(float(p['radius']) * MM)
        for attr in ('bendRadius', 'radius'):
            if hasattr(jin, attr):
                with contextlib.suppress(Exception):
                    setattr(jin, attr, radius)
                    break
    return _feature_result(feats.add(jin), 'join_by_bend')


def op_corner_closure(app, p):
    """Close the corner where two sheet-metal flanges meet (Fusion July 2026+
    preview). edge_a = dominant flange edge token, edge_b = submissive edge
    (the edges that face each other across the corner; pick with
    query_entities kind="edges"). Optional gap (mm), overlap (0..1 switches
    from symmetric-gap to overlap alignment; flip puts the submissive flange
    on top), transition: smooth|straight|trim, width_aligned bool."""
    feats = _sheet_metal_feats(app, 'cornerClosureFeatures')
    cin = feats.createInput(_registry.get(p['edge_a']), _registry.get(p['edge_b']))
    if cin is None:
        raise RuntimeError('Fusion refused to create a corner-closure input '
                           'for these edges — are both on sheet-metal flanges?')
    ctype = None
    with contextlib.suppress(Exception):
        ctype = int(cin.closureType)
    if ctype == 0:  # UndefinedCornerClosureType
        raise ValueError('These two edges do not define a shared corner. Pass '
                         'the two flange edges that meet at the corner to '
                         'close.')
    if p.get('gap') is not None:
        cin.gap = _vi(float(p['gap']) * MM)
    if p.get('overlap') is not None:
        overlap = float(p['overlap'])
        if not 0.0 <= overlap <= 1.0:
            raise ValueError('overlap must be between 0 and 1, got %r'
                             % p['overlap'])
        cin.setToOverlapAlignmentType(_vi(overlap), bool(p.get('flip', False)))
    if p.get('transition'):
        names = {'smooth': 'SmoothCornerBendTransitionType',
                 'straight': 'StraightLineCornerBendTransitionType',
                 'trim': 'TrimToBendCornerBendTransitionType'}
        key = str(p['transition']).lower()
        if key not in names:
            raise ValueError('transition must be smooth|straight|trim, got %r'
                             % p['transition'])
        enum = getattr(adsk.fusion, 'BendTransitionTypes', None)
        value = getattr(enum, names[key], None) if enum else None
        if value is None:
            raise RuntimeError('BendTransitionTypes is not available in this '
                               'Fusion build — the preview API changed.')
        cin.bendTransition = value
    if p.get('width_aligned') is not None:
        with contextlib.suppress(Exception):
            cin.isWidthExtentAligned = bool(p['width_aligned'])
    out = _feature_result(feats.add(cin), 'corner_closure')
    out['closure_type'] = {1: 'two_bend', 2: 'three_bend'}.get(ctype, 'unknown')
    return out


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
    ok = False
    if parametric:
        base = root.features.baseFeatures.add()
        base.startEdit()
    try:
        if base:
            added = root.meshBodies.add(p['path'], units, base)
        else:
            added = root.meshBodies.add(p['path'], units)
        ok = True
    finally:
        if base:
            base.finishEdit()
            if not ok:
                # A failed import (bad path/format) would otherwise leave an
                # empty Base feature cluttering the timeline.
                with contextlib.suppress(Exception):
                    base.deleteMe()
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


def _enum_value(module, value_name):
    """Look up an enum VALUE by name anywhere in an adsk submodule (the exact
    holder class names vary across Fusion releases). None when absent."""
    for attr in dir(module):
        holder = getattr(module, attr, None)
        value = getattr(holder, value_name, None)
        if value is not None and not callable(value):
            return value
    return None


def _fusion_enum(value_name):
    value = _enum_value(adsk.fusion, value_name)
    if value is None:
        raise RuntimeError('Enum value %r not found — this Fusion version may not '
                           'support the requested option.' % value_name)
    return value


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
    Requires the mesh-feature API (Fusion 2024+). Optional settings (applied
    best-effort per build): density 0-1, shape_preservation 0-1,
    preserve_boundaries / preserve_sharp_edges bools, method
    adaptive|uniform."""
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
        if p.get('density') is not None:
            with contextlib.suppress(Exception):
                rin.density = float(p['density'])
        if p.get('shape_preservation') is not None:
            with contextlib.suppress(Exception):
                rin.shapePreservation = float(p['shape_preservation'])
        if p.get('preserve_boundaries') is not None:
            with contextlib.suppress(Exception):
                rin.isPreserveBoundariesEnabled = bool(p['preserve_boundaries'])
        if p.get('preserve_sharp_edges') is not None:
            with contextlib.suppress(Exception):
                rin.isPreserveSharpEdgesEnabled = bool(p['preserve_sharp_edges'])
        if (p.get('method') or '').lower() == 'uniform':
            value = _fusion_enum_any(['UniformMeshRemeshMethodType',
                                      'UniformRemeshType'])
            if value is not None:
                with contextlib.suppress(Exception):
                    rin.meshRemeshMethodType = value
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
    made_plane = None
    if p.get('offset'):
        planes = root.constructionPlanes
        cin = planes.createInput()
        cin.setByOffset(plane, _vi(p['offset'] * MM))
        plane = made_plane = planes.add(cin)
    sk = root.sketches.add(plane)
    try:
        sk.intersectWithSketchPlane([mesh])
    except Exception as exc:
        # Don't leave a stray empty sketch + offset plane behind on failure.
        with contextlib.suppress(Exception):
            sk.deleteMe()
        if made_plane is not None:
            with contextlib.suppress(Exception):
                made_plane.deleteMe()
        raise RuntimeError('This Fusion version cannot section a mesh into a '
                           'sketch (%s). Convert with mesh_to_brep first, then '
                           'section the solid.' % exc)
    return {'sketch': _registry.add('skt', sk),
            'curves': sk.sketchCurves.count,
            'profiles': sk.profiles.count}


def _fusion_enum_any(names):
    """First enum value found among candidate names (Preview APIs rename
    their enum members between builds). None when none exist."""
    for name in names:
        value = _enum_value(adsk.fusion, name)
        if value is not None:
            return value
    return None


def _triangle_data(mesh_body):
    """(vertices_mm, triangles) of a mesh body, probing the per-build property
    names (TriangleMesh exposes nodeIndices, PolygonMesh triangleNodeIndices;
    nodeCoordinatesAsDouble is the fast path when present)."""
    for attr in ('mesh', 'displayMesh'):
        dm = getattr(mesh_body, attr, None)
        if dm is None:
            continue
        idx = None
        for name in ('nodeIndices', 'triangleNodeIndices'):
            idx = getattr(dm, name, None)
            if idx:
                break
        if not idx:
            continue
        flat = getattr(dm, 'nodeCoordinatesAsDouble', None)
        if flat:
            verts = [(flat[i] / MM, flat[i + 1] / MM, flat[i + 2] / MM)
                     for i in range(0, len(flat), 3)]
        else:
            verts = [(pt.x / MM, pt.y / MM, pt.z / MM)
                     for pt in dm.nodeCoordinates]
        if verts:
            tris = [(idx[i], idx[i + 1], idx[i + 2])
                    for i in range(0, len(idx), 3)]
            return verts, tris
    raise RuntimeError('Mesh body exposes no triangle data in this build')


def _write_mesh_file(path, verts, tris):
    """Write vertices (mm) + triangles to binary STL or OBJ by extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.obj':
        with open(path, 'w', encoding='ascii') as fh:
            for v in verts:
                fh.write('v %.6f %.6f %.6f\n' % v)
            for t in tris:
                fh.write('f %d %d %d\n' % (t[0] + 1, t[1] + 1, t[2] + 1))
        return
    if ext != '.stl':
        raise ValueError('path must end in .stl or .obj, got %r' % path)
    with open(path, 'wb') as fh:
        fh.write(b'FusionMCP mesh export'.ljust(80, b' '))
        fh.write(struct.pack('<I', len(tris)))
        for a, b, c in tris:
            va, vb, vc = verts[a], verts[b], verts[c]
            ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
            wx, wy, wz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            fh.write(struct.pack('<12fH', nx / ln, ny / ln, nz / ln,
                                 *va, *vb, *vc, 0))


def op_mesh_export(app, p):
    """Write ONE mesh body (token) to an STL or OBJ file in mm (format from
    the extension) — the bridge to the server-side scan tools
    (scan_analyze/scan_align/scan_deviation/scan_cavity_sections work on
    files, not on live Fusion meshes). Pure-Python write: large scans take a
    few seconds on Fusion's UI thread."""
    m = _registry.get(p['mesh'])
    verts, tris = _triangle_data(m)
    _write_mesh_file(p['path'], verts, tris)
    return {'exported': p['path'], 'triangles': len(tris), 'nodes': len(verts)}


def op_face_groups(app, p):
    """List a mesh body's face groups (segmentation regions painted in the
    MESH workspace or auto-generated): tempId, area, centroid, bounding box,
    planarity. With `group` (a tempId) + `export_path` (.stl/.obj) also
    writes that group's triangles to a file for server-side surface fitting —
    that part needs PolygonMesh.triangleFaceGroupTempIds (Preview API,
    Fusion Sep 2024+). tempIds are only stable while the document stays open
    and the mesh unmodified."""
    m = _registry.get(p['mesh'])
    groups = getattr(m, 'faceGroups', None)
    if groups is None:
        raise RuntimeError('Face groups are not available in this Fusion '
                           'version.')
    out = []
    for i in range(groups.count):
        g = groups.item(i)
        entry = {'index': i}
        for attr in ('tempId', 'isPlanar'):
            with contextlib.suppress(Exception):
                entry[attr] = getattr(g, attr)
        with contextlib.suppress(Exception):
            entry['area_mm2'] = round(g.area / (MM * MM), 2)
        with contextlib.suppress(Exception):
            entry['centroid_mm'] = _xyz_mm(g.centroid)
        with contextlib.suppress(Exception):
            bb = g.boundingBox
            entry['min_mm'] = _xyz_mm(bb.minPoint)
            entry['max_mm'] = _xyz_mm(bb.maxPoint)
        out.append(entry)
    result = {'mesh': p['mesh'], 'count': len(out), 'groups': out}
    if not out:
        result['note'] = ('No face groups on this mesh — generate them in the '
                          'MESH workspace (Modify > Generate Face Groups) '
                          'first.')

    if p.get('export_path') and p.get('group') is not None:
        pm = getattr(m, 'mesh', None)
        ids = getattr(pm, 'triangleFaceGroupTempIds', None) if pm else None
        if not ids:
            raise RuntimeError(
                'PolygonMesh.triangleFaceGroupTempIds is not available in '
                'this Fusion build (Preview API, Sep 2024+) — export the '
                'whole mesh with mesh_export and segment it server-side '
                'instead.')
        verts, tris = _triangle_data(m)
        want = int(p['group'])
        picked = [t for k, t in enumerate(tris)
                  if k < len(ids) and int(ids[k]) == want]
        if not picked:
            raise RuntimeError('No triangles carry face-group tempId %d — '
                               'use a tempId from the groups list.' % want)
        _write_mesh_file(p['export_path'], verts, picked)
        result['exported'] = {'path': p['export_path'], 'group': want,
                              'triangles': len(picked)}
    return result


def op_mesh_repair(app, p):
    """Repair scan defects (holes, floaters, non-manifold junk) on mesh
    bodies (tokens; all meshes when omitted). mode: "stitch" (close gaps and
    remove debris, default) or "rebuild" (full re-wrap; `quality` fast |
    accurate, `density` 8-256, `offset` mm grows the rebuilt skin). Preview
    mesh-feature API — parameters are applied best-effort per build."""
    root = _root(app)
    feats = getattr(root.features, 'meshRepairFeatures', None)
    if feats is None:
        raise RuntimeError('meshRepairFeatures not available in this Fusion '
                           'version — use the MESH workspace Repair command.')
    mode = (p.get('mode') or 'stitch').lower()
    if mode not in ('stitch', 'rebuild'):
        raise ValueError('mode must be stitch|rebuild, got %r' % p.get('mode'))
    out = []
    for m in _mesh_targets(root, p):
        try:
            rin = feats.createInput()
        except TypeError:
            rin = feats.createInput(m)
        with contextlib.suppress(Exception):
            rin.mesh = m
        if mode == 'rebuild':
            value = _fusion_enum_any(['RebuildMeshRepairType'])
            if value is not None:
                with contextlib.suppress(Exception):
                    rin.meshRepairType = value
            if (p.get('quality') or '').lower() == 'accurate':
                value = _fusion_enum_any(['AccurateMeshRepairRebuildType',
                                          'AccurateRebuildType'])
                if value is not None:
                    with contextlib.suppress(Exception):
                        rin.meshRepairRebuildType = value
            if p.get('density'):
                with contextlib.suppress(Exception):
                    rin.density = int(p['density'])
            if p.get('offset'):
                with contextlib.suppress(Exception):
                    rin.offset = float(p['offset']) * MM
        feats.add(rin)
        out.append(_mesh_info_entry(m))
    return {'repaired': out, 'mode': mode}


def op_mesh_smooth(app, p):
    """Smooth mesh bodies (tokens; all when omitted) — soften scanner noise
    before converting. smoothness 0-1 (Fusion default when omitted). Preview
    mesh-feature API."""
    root = _root(app)
    feats = getattr(root.features, 'meshSmoothFeatures', None)
    if feats is None:
        raise RuntimeError('meshSmoothFeatures not available in this Fusion '
                           'version — use the MESH workspace Smooth command.')
    out = []
    for m in _mesh_targets(root, p):
        try:
            sin = feats.createInput()
        except TypeError:
            sin = feats.createInput(m)
        with contextlib.suppress(Exception):
            sin.mesh = m
        if p.get('smoothness') is not None:
            with contextlib.suppress(Exception):
                sin.smoothness = float(p['smoothness'])
        feats.add(sin)
        out.append(_mesh_info_entry(m))
    return {'smoothed': out}


def op_mesh_shell(app, p):
    """Shell (hollow/offset) mesh bodies by `thickness` mm — turn a scanned
    outer skin into a wall of even thickness. Preview mesh-feature API; the
    thickness property name is probed per build."""
    root = _root(app)
    feats = getattr(root.features, 'meshShellFeatures', None)
    if feats is None:
        raise RuntimeError('meshShellFeatures not available in this Fusion '
                           'version — use the MESH workspace Shell command.')
    thickness = float(p['thickness']) * MM
    out = []
    for m in _mesh_targets(root, p):
        try:
            sin = feats.createInput()
        except TypeError:
            sin = feats.createInput(m)
        with contextlib.suppress(Exception):
            sin.mesh = m
        applied = False
        for value in (thickness, _vi(thickness)):
            for attr in ('thickness', 'shellThickness', 'offset'):
                try:
                    setattr(sin, attr, value)
                    applied = True
                    break
                except Exception:  # noqa: PERF203 - probing property names
                    continue
            if applied:
                break
        if not applied:
            raise RuntimeError('Could not set the shell thickness on this '
                               "build's MeshShellFeatureInput — run "
                               "api_introspect('MeshShellFeatureInput') and "
                               'report the property name.')
        feats.add(sin)
        out.append(_mesh_info_entry(m))
    return {'shelled': out, 'thickness_mm': p['thickness']}


def op_mesh_separate(app, p):
    """Split mesh bodies (tokens; all when omitted) into their disconnected
    shells — a scan session that captured several parts becomes one mesh body
    per part. Preview mesh-feature API."""
    root = _root(app)
    feats = getattr(root.features, 'meshSeparateFeatures', None)
    if feats is None:
        raise RuntimeError('meshSeparateFeatures not available in this Fusion '
                           'version — use the MESH workspace Separate command.')
    before = root.meshBodies.count
    for m in _mesh_targets(root, p):
        try:
            sin = feats.createInput()
        except TypeError:
            sin = feats.createInput(m)
        with contextlib.suppress(Exception):
            sin.mesh = m
        feats.add(sin)
    return {'meshes_before': before,
            'meshes_after': root.meshBodies.count,
            'meshes': [_mesh_info_entry(m) for m in root.meshBodies]}


# --------------------------------------------------------------------------- #
# Canvases (photos as tracing references)
# --------------------------------------------------------------------------- #
def _matrix2d_cells(t):
    return [[t.getCell(r, c) for c in range(3)] for r in range(3)]


def _mat3_mul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)]
            for r in range(3)]


def op_canvas_calibrate(app, p):
    """Two-point canvas calibration, fully scripted (Fusion's right-click
    Calibrate has no API): pass two feature points p1/p2 as [x, y] in the
    canvas plane's sketch coordinates (mm — read them off a screenshot or
    sketch points over the photo) and the true `distance` mm between them.
    The canvas is scaled uniformly about p1; optional rotate_to_deg also
    rotates so p1->p2 points at that angle, and move_p1_to=[x, y] then
    translates p1 onto a target point."""
    c = _registry.get(p['canvas'])
    p1 = [float(v) * MM for v in p['p1']]
    p2 = [float(v) * MM for v in p['p2']]
    distance = float(p['distance']) * MM
    if distance <= 0:
        raise ValueError('distance must be positive')
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    current = math.hypot(dx, dy)
    if current < 1e-9:
        raise ValueError('p1 and p2 coincide — pick two distinct features')
    factor = distance / current
    angle = 0.0
    if p.get('rotate_to_deg') is not None:
        angle = math.radians(float(p['rotate_to_deg'])) - math.atan2(dy, dx)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    # M = T(p1) . R . S . T(-p1): scale+rotate about p1 in plane space.
    a, b = factor * cos_a, -factor * sin_a
    c2, d = factor * sin_a, factor * cos_a
    m = [[a, b, p1[0] - (a * p1[0] + b * p1[1])],
         [c2, d, p1[1] - (c2 * p1[0] + d * p1[1])],
         [0.0, 0.0, 1.0]]
    if p.get('move_p1_to') is not None:
        target = [float(v) * MM for v in p['move_p1_to']]
        m[0][2] += target[0] - p1[0]
        m[1][2] += target[1] - p1[1]
    t = c.transform
    new = _mat3_mul(m, _matrix2d_cells(t))
    for r in range(3):
        for col in range(3):
            t.setCell(r, col, new[r][col])
    c.transform = t
    return {'canvas': p['canvas'],
            'scale_applied': round(factor, 6),
            'rotation_applied_deg': round(math.degrees(angle), 4),
            'note': 'The p1-p2 features now span %.3f mm in the canvas plane.'
                    % (distance / MM)}


def op_canvas_list(app, p):
    """List the canvases in the root component with tokens for
    canvas_calibrate/canvas_update/canvas_delete."""
    root = _root(app)
    canvases = getattr(root, 'canvases', None)
    if canvases is None:
        raise RuntimeError('Canvases are not available in this Fusion version.')
    out = []
    for i in range(canvases.count):
        c = canvases.item(i)
        entry = {'token': _registry.add('cnv', c)}
        for attr in ('name', 'opacity', 'isDisplayedThrough', 'isSelectable'):
            with contextlib.suppress(Exception):
                entry[attr] = getattr(c, attr)
        with contextlib.suppress(Exception):
            entry['image'] = c.imageFilename
        out.append(entry)
    return {'count': len(out), 'canvases': out}


def op_canvas_update(app, p):
    """Adjust a canvas (token): opacity 0-100, name, displayed_through
    (visible through the model), selectable, flip_h/flip_v mirror the image
    in place."""
    c = _registry.get(p['canvas'])
    changed = []
    if p.get('opacity') is not None:
        c.opacity = int(p['opacity'])
        changed.append('opacity')
    if p.get('name'):
        c.name = p['name']
        changed.append('name')
    if p.get('displayed_through') is not None:
        c.isDisplayedThrough = bool(p['displayed_through'])
        changed.append('displayed_through')
    if p.get('selectable') is not None:
        c.isSelectable = bool(p['selectable'])
        changed.append('selectable')
    if p.get('flip_h'):
        c.flipHorizontal()
        changed.append('flip_h')
    if p.get('flip_v'):
        c.flipVertical()
        changed.append('flip_v')
    if not changed:
        raise ValueError('Nothing to change — pass opacity, name, '
                         'displayed_through, selectable, flip_h or flip_v.')
    return {'canvas': p['canvas'], 'changed': changed}


def op_canvas_delete(app, p):
    """Remove a canvas (token) from the design."""
    token = p['canvas']
    c = _registry.get(token)
    if not c.deleteMe():
        raise RuntimeError('Fusion refused to delete the canvas — is it '
                           'referenced by a sketch?')
    _registry.remove(token)
    return {'deleted': token}


def op_import_svg(app, p):
    """Import an SVG file's curves into a sketch (ImportManager, Fusion Oct
    2022+): into an existing sketch (token) or a new sketch on `plane`.
    Options: flip_h/flip_v mirror the import, scale applies a uniform factor.
    Typical photo flow: photo_rectify -> photo_to_sketch makes a DXF instead
    (import_file format=dxf); use this for real vector art (logos, gaskets
    from a vector datasheet)."""
    im = app.importManager
    make_opts = getattr(im, 'createSVGImportOptions', None)
    if make_opts is None:
        raise RuntimeError('SVG import is not available in this Fusion '
                           'version — convert the SVG to DXF and use '
                           'import_file.')
    root = _root(app)
    if p.get('sketch'):
        sk = _registry.get(p['sketch'])
    else:
        sk = root.sketches.add(_resolve_plane(app, p.get('plane', 'XY')))
    try:
        opts = make_opts(p['path'])
    except TypeError:
        opts = make_opts()
        opts.filename = p['path']
    if p.get('flip_h'):
        with contextlib.suppress(Exception):
            opts.isHorizontalFlip = True
    if p.get('flip_v'):
        with contextlib.suppress(Exception):
            opts.isVerticalFlip = True
    with contextlib.suppress(Exception):
        opts.isViewFit = False
    if p.get('scale'):
        with contextlib.suppress(Exception):
            t = opts.transform
            m = adsk.core.Matrix3D.create()
            for i in range(3):
                m.setCell(i, i, float(p['scale']))
            t.transformBy(m)
            opts.transform = t
    before = sk.sketchCurves.count
    im.importToTarget(opts, sk)
    return {'sketch': _registry.add('skt', sk),
            'curves_added': sk.sketchCurves.count - before,
            'curves': sk.sketchCurves.count,
            'profiles': sk.profiles.count}


# --------------------------------------------------------------------------- #
# User-facing popups (update notifications and important messages)
# --------------------------------------------------------------------------- #
def op_show_message(app, p):
    """Show a native Fusion information popup to the USER (not the model) —
    used by the server to announce finished updates and other events the user
    must see without reading the chat. Modal: blocks this op (and the single
    op socket) until dismissed, so keep it rare and short."""
    app.userInterface.messageBox(str(p['text']),
                                 str(p.get('title') or 'FusionMCP'))
    return {'shown': True}


def op_notify_update(app, p):
    """Ask the user IN FUSION whether to install a FusionMCP update: a native
    Yes/No popup showing the new version and its release notes. Returns
    {'install': bool}; the server applies the update only after a Yes.
    Driven automatically by the server's startup update check — the popup IS
    the user's consent, no typed command needed."""
    ui = app.userInterface
    notes = (p.get('notes') or '').strip() or '(no release notes)'
    text = ('A new version of FusionMCP is available.\n\n'
            'Installed:  %s\n'
            'Available:  %s\n\n'
            'Changes:\n%s\n\n'
            'Install now? Fusion and the MCP client must be restarted '
            'afterwards.' % (p.get('current') or VERSION, p['version'], notes))
    try:
        res = ui.messageBox(text, 'FusionMCP update',
                            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                            adsk.core.MessageBoxIconTypes.QuestionIconType)
        agreed = res == adsk.core.DialogResults.DialogYes
    except Exception:  # noqa: BLE001 - enum shapes vary; fall back to info-only
        ui.messageBox(text + '\n\n(Install via apply_update in the MCP chat.)',
                      'FusionMCP update')
        agreed = False
    return {'install': agreed, 'version': p['version']}


# --------------------------------------------------------------------------- #
# v1.12: workshop wave — loft from sections, silhouette, sketch doctor,
# drawing tables, fastener refresh
# --------------------------------------------------------------------------- #
def op_loft_from_sections(app, p):
    """Build a loft body from scan_sections / scan_cavity_sections output in
    ONE call: per section {level_mm, points_mm: [[u, v], ...]} an offset
    construction plane and one CLOSED fitted spline are created on `plane`
    (XY|XZ|YZ or a plane token), then every profile is lofted. rail=True adds
    a centreline rail through each section's first point — the cure for
    scalloped lofts with smooth profiles. operation: new|join|cut|intersect.
    Sections must share point ordering and start anchor (scan_cavity_sections
    output already does; never feed raw unordered polylines)."""
    root = _root(app)
    base = _resolve_plane(app, p.get('plane', 'XY'))
    sections = p.get('sections') or []
    if len(sections) < 2:
        raise ValueError('Need at least 2 sections with points_mm')
    planes = root.constructionPlanes
    made = []
    anchors_world = []
    for sec in sections:
        pts = sec.get('points_mm') or []
        if len(pts) < 3:
            raise ValueError('Each section needs >= 3 points_mm')
        level = float(sec.get('level_mm', 0.0))
        plane = base
        if abs(level) > 1e-9:
            cin = planes.createInput()
            cin.setByOffset(base, _vi(level * MM))
            plane = planes.add(cin)
        sk = root.sketches.add(plane)
        coll = adsk.core.ObjectCollection.create()
        for u, v in pts:
            coll.add(adsk.core.Point3D.create(u * MM, v * MM, 0.0))
        spline = sk.sketchCurves.sketchFittedSplines.add(coll)
        with contextlib.suppress(Exception):
            spline.isClosed = True
        if sk.profiles.count == 0:
            raise RuntimeError(
                'Section at %.2f mm produced no closed profile — points may '
                'self-intersect after the spline fit; decimate or clean them.'
                % level)
        made.append(sk)
        with contextlib.suppress(Exception):
            anchors_world.append(sk.sketchToModelSpace(
                adsk.core.Point3D.create(pts[0][0] * MM, pts[0][1] * MM, 0.0)))
    lofts = root.features.loftFeatures
    lin = lofts.createInput(_operation(p.get('operation')))
    for sk in made:
        lin.loftSections.add(sk.profiles.item(0))
    used_rail = False
    if p.get('rail') and len(anchors_world) == len(made):
        with contextlib.suppress(Exception):
            rail_sk = root.sketches.add(base)
            coll = adsk.core.ObjectCollection.create()
            for w in anchors_world:
                coll.add(rail_sk.modelToSketchSpace(w))
            rail = rail_sk.sketchCurves.sketchFittedSplines.add(coll)
            lin.centerLineOrRails.addRail(rail)
            used_rail = True
    feat = lofts.add(lin)
    bodies = [{'token': _registry.add('bdy', b), 'name': b.name}
              for b in feat.bodies]
    return {'bodies': bodies, 'sections': len(made), 'rail': used_rail,
            'sketches': [_registry.add('skt', sk) for sk in made]}


def _stroke_curves_into_sketch(sk, bodies, tolerance_cm):
    """Sample every edge of the given (temporary) BRep bodies into fitted
    splines on sketch `sk`. Returns the number of curves created."""
    count = 0
    for body in bodies:
        for edge in getattr(body, 'edges', []) or []:
            with contextlib.suppress(Exception):
                ev = edge.evaluator
                ok, start, end = ev.getParameterExtents()
                if not ok:
                    continue
                ok, pts = ev.getStrokes(start, end, tolerance_cm)
                if not ok or len(pts) < 2:
                    continue
                coll = adsk.core.ObjectCollection.create()
                for pt in pts:
                    coll.add(sk.modelToSketchSpace(pt))
                sk.sketchCurves.sketchFittedSplines.add(coll)
                count += 1
    return count


def op_silhouette(app, p):
    """EXPERIMENTAL (April 2026+ preview): project the outline of a body or
    mesh along a view direction into a new sketch — cutting templates and
    gaskets from any angle; export with export_sketch_dxf. Pass body= (BRep
    token; TemporaryBRepManager.createSilhouetteCurves) or mesh= (mesh token;
    MeshBody.silhouette). direction: "x"|"y"|"z" or [x, y, z]; the curves
    land on `plane` (default XY)."""
    root = _root(app)
    d = p.get('direction', 'z')
    if isinstance(d, (list, tuple)):
        vec = adsk.core.Vector3D.create(*[float(v) for v in d])
    else:
        axes = {'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0),
                'z': (0.0, 0.0, 1.0)}
        key = str(d).lower()
        if key not in axes:
            raise ValueError('direction must be x|y|z or [x,y,z], got %r' % d)
        vec = adsk.core.Vector3D.create(*axes[key])

    result = None
    if p.get('mesh'):
        m = _registry.get(p['mesh'])
        fn = getattr(m, 'silhouette', None)
        if fn is None:
            raise RuntimeError('MeshBody.silhouette is not available in this '
                               'Fusion build (needs April 2026+).')
        try:
            result = fn(vec)
        except TypeError as exc:
            raise RuntimeError('MeshBody.silhouette signature mismatch on '
                               'this build (%s) — run api_introspect('
                               '"MeshBody") and report it.' % exc)
    else:
        body = _registry.get(p['body'])
        tbm = adsk.fusion.TemporaryBRepManager.get()
        fn = getattr(tbm, 'createSilhouetteCurves', None)
        if fn is None:
            raise RuntimeError('TemporaryBRepManager.createSilhouetteCurves '
                               'is not available in this Fusion build.')
        try:
            result = fn(body, vec, True)
        except TypeError:
            result = fn(body, vec)

    # Normalise the result to a list of things with .edges; the preview APIs
    # return an ObjectCollection of temporary wire bodies.
    items = []
    if result is not None:
        if hasattr(result, 'count') and hasattr(result, 'item'):
            items = [result.item(i) for i in range(result.count)]
        elif isinstance(result, (list, tuple)):
            items = list(result)
        else:
            items = [result]
    items = [it for it in items if hasattr(it, 'edges')]
    if not items:
        raise RuntimeError(
            'Silhouette returned no usable curve bodies (got %r) — the '
            'preview API shape changed; run api_introspect and report it.'
            % (type(result).__name__ if result is not None else None))

    sk = root.sketches.add(_resolve_plane(app, p.get('plane', 'XY')))
    count = _stroke_curves_into_sketch(sk, items, 0.02)  # 0.2 mm tolerance
    if not count:
        with contextlib.suppress(Exception):
            sk.deleteMe()
        raise RuntimeError('Silhouette produced no sketch curves.')
    return {'sketch': _registry.add('skt', sk), 'curves': count,
            'profiles': sk.profiles.count}


def op_sketch_doctor(app, p):
    """One-stop sketch health check and repair: per sketch (token, or every
    root sketch) — fully-constrained state, Fusion's own health state and
    error/warning message, profile count and open endpoints; with fix=True
    also runs Fusion's auto-constrain on under-constrained sketches and
    reports how many constraints were added. Follow up remaining gaps with
    sketch_dimension."""
    status = op_sketch_status(app, p)
    fix = bool(p.get('fix'))
    health_names = {}
    with contextlib.suppress(Exception):
        holder = adsk.fusion.FeatureHealthStates
        for attr in dir(holder):
            value = getattr(holder, attr, None)
            if isinstance(value, int):
                health_names[value] = attr
    for entry in status['sketches']:
        sk = _registry.get(entry['token'])
        with contextlib.suppress(Exception):
            hs = sk.healthState
            entry['health'] = health_names.get(hs, hs)
        with contextlib.suppress(Exception):
            msg = sk.errorOrWarningMessage
            if msg:
                entry['message'] = msg
        if fix and not entry.get('fully_constrained', True):
            gc = sk.geometricConstraints
            before = None
            with contextlib.suppress(Exception):
                before = gc.count
            applied = False
            with contextlib.suppress(Exception):
                cin = gc.createAutoConstrainInput()
                gc.autoConstrain(cin)
                applied = True
            if not applied:
                with contextlib.suppress(Exception):
                    gc.autoConstrain()
                    applied = True
            entry['auto_constrained'] = applied
            if applied and before is not None:
                with contextlib.suppress(Exception):
                    entry['constraints_added'] = gc.count - before
            with contextlib.suppress(Exception):
                entry['fully_constrained_after'] = bool(sk.isFullyConstrained)
    status['fix'] = fix
    return status


def op_drawing_table(app, p):
    """Add a custom table to the ACTIVE drawing's sheet (Fusion July 2026+
    preview): data = rows of cell strings (first row = header), optional
    title and position_mm [x, y]. Open/create the drawing first
    (create_drawing); cut lists, parameter tables and mini-BOMs land right
    on the sheet."""
    doc = app.activeDocument
    product = doc.products.itemByProductType('DrawingProductType') if doc else None
    if not product:
        raise RuntimeError('The active document is not a drawing — run '
                           'create_drawing (or open the drawing tab) first.')
    try:
        import adsk.drawing
        drawing = adsk.drawing.Drawing.cast(product) or product
    except ImportError:
        drawing = product
    sheet = getattr(drawing, 'activeSheet', None)
    if sheet is None:
        with contextlib.suppress(Exception):
            sheet = drawing.sheets.item(0)
    if sheet is None:
        raise RuntimeError('The drawing has no sheets.')
    tables = getattr(sheet, 'customTables', None)
    if tables is None:
        raise RuntimeError('Sheet.customTables is not available in this '
                           'Fusion build (preview, July 2026+).')
    data = p.get('data') or []
    if not data or not all(isinstance(row, list) and row for row in data):
        raise ValueError('data must be a non-empty list of non-empty rows')
    rows, cols = len(data), max(len(row) for row in data)
    try:
        tin = tables.createInput()
    except TypeError:
        tin = tables.createInput(rows, cols)
    for attr, value in (('rowCount', rows), ('numberOfRows', rows),
                        ('columnCount', cols), ('numberOfColumns', cols)):
        with contextlib.suppress(Exception):
            setattr(tin, attr, value)
    if p.get('title'):
        with contextlib.suppress(Exception):
            tin.title = p['title']
    if p.get('position_mm'):
        with contextlib.suppress(Exception):
            x, y = p['position_mm']
            tin.position = adsk.core.Point2D.create(float(x) * MM,
                                                    float(y) * MM)
    table = tables.add(tin)
    filled = 0
    for r, row in enumerate(data):
        for c, cell in enumerate(row):
            for setter in ('setCellData', 'setCellText', 'setCellValue'):
                fn = getattr(table, setter, None)
                if fn is None:
                    continue
                with contextlib.suppress(Exception):
                    fn(r, c, str(cell))
                    filled += 1
                    break
    return {'rows': rows, 'columns': cols, 'cells_set': filled,
            'sheet': getattr(sheet, 'name', None)}


def op_fastener_update_size(app, p):
    """Refresh inserted Content-Library fasteners after their host geometry
    changed (Fusion July 2026+ preview): finds every occurrence backed by a
    FastenerOccurrenceDefinition and calls updateSize() so screw
    diameter/length re-match the plates they clamp."""
    root = _root(app)
    out = []
    occs = root.allOccurrences
    for i in range(occs.count):
        occ = occs.item(i)
        definition = getattr(occ, 'definition', None)
        if definition is None or 'fastener' not in \
                str(getattr(definition, 'objectType', '')).lower():
            continue
        entry = {'occurrence': occ.name}
        with contextlib.suppress(Exception):
            entry['size_up_to_date'] = bool(definition.isSizeUpToDate)
        if not entry.get('size_up_to_date', False):
            try:
                definition.updateSize()
                entry['updated'] = True
            except Exception as exc:  # noqa: BLE001 - per-fastener report
                entry['error'] = str(exc)
        out.append(entry)
    if not out:
        return {'fasteners': [], 'count': 0,
                'note': 'No Content-Library fasteners found (they must be '
                        'inserted via the Fusion UI; needs July 2026+).'}
    return {'fasteners': out, 'count': len(out)}


# --------------------------------------------------------------------------- #
# Drawings (2D documentation)
# --------------------------------------------------------------------------- #
def op_mesh_compare(app, p):
    """Signed-distance deviation between two mesh bodies via the native
    PolygonMesh.compareWith (Fusion July 2026+) — no file round-trip: compare
    a scan against a converted/re-imported rebuild in place. Distances are
    per-node of mesh_a against the surface of mesh_b; stats in mm. On older
    builds (or for file-vs-file) use the server-side scan_deviation."""
    a = _registry.get(p['mesh_a'])
    b = _registry.get(p['mesh_b'])

    def poly(mesh_body):
        for attr in ('mesh', 'displayMesh'):
            pm = getattr(mesh_body, attr, None)
            if pm is not None and hasattr(pm, 'compareWith'):
                return pm
        return None

    pa, pb = poly(a), poly(b)
    if pa is None or pb is None:
        raise RuntimeError('PolygonMesh.compareWith is not available in this '
                           'Fusion build (needs July 2026+). Export both meshes '
                           'as STL and use scan_deviation instead.')
    dists = pa.compareWith(pb)
    if not dists:
        raise RuntimeError('compareWith returned no data — do the meshes overlap?')
    signed_mm = [d / MM for d in dists]
    absd = sorted(abs(d) for d in signed_mm)
    n = len(absd)
    tol = float(p.get('tolerance', 0.2))

    def pct(q):
        return absd[min(n - 1, int(q * (n - 1)))]

    return {
        'nodes': n,
        'mean_mm': round(sum(absd) / n, 4),
        'rms_mm': round(math.sqrt(sum(d * d for d in absd) / n), 4),
        'p50_mm': round(pct(0.50), 4),
        'p90_mm': round(pct(0.90), 4),
        'p99_mm': round(pct(0.99), 4),
        'max_mm': round(absd[-1], 4),
        'signed_min_mm': round(min(signed_mm), 4),
        'signed_max_mm': round(max(signed_mm), 4),
        'within_tolerance': round(sum(1 for d in absd if d <= tol) / n, 4),
        'tolerance_mm': tol,
    }


def _drawing_enum(drawing_mod, names):
    """First enum value found among candidate names in adsk.drawing, or None."""
    for name in names:
        if not name:
            continue
        value = _enum_value(drawing_mod, name)
        if value is not None:
            return value
    return None


def _create_drawing_via_manager(app, drawing_mod, p):
    """The official creation surface (Fusion July 2026+, preview):
    DrawingManager.createDrawingInput() -> template / sheet size / orientation /
    standard / units knobs -> createDrawing(). None means 'not on this build'."""
    mgr_cls = getattr(drawing_mod, 'DrawingManager', None)
    if mgr_cls is None or not hasattr(mgr_cls, 'get'):
        return None
    mgr = mgr_cls.get()
    if mgr is None or not hasattr(mgr, 'createDrawingInput'):
        return None
    din = mgr.createDrawingInput()
    if p.get('template'):
        base = _drawing_enum(drawing_mod, ('FromTemplateBaseDocumentType',))
        if base is not None:
            with contextlib.suppress(Exception):
                din.baseDocumentType = base
        din.templateFile = p['template']
    sheet = (p.get('sheet_size') or '').upper().replace(' ', '')
    if sheet:
        value = _drawing_enum(drawing_mod, (
            '%sISOSheetSize' % sheet, '%sASMESheetSize' % sheet,
            '%sSheetSize' % sheet))
        if value is not None:
            din.sheetSize = value
    orientation = (p.get('orientation') or '').capitalize()
    if orientation:
        value = _drawing_enum(drawing_mod, ('%sSheetOrientationType' % orientation,))
        if value is not None:
            din.orientationType = value
    standard = (p.get('standard') or '').upper()
    if standard:
        value = _drawing_enum(drawing_mod, ('%sDrawingStandardType' % standard,))
        if value is not None:
            din.standard = value
    units = (p.get('drawing_units') or '').lower()
    if units:
        value = _drawing_enum(drawing_mod, (
            {'mm': 'MillimeterDrawingUnitType', 'in': 'InchDrawingUnitType'}.get(units),))
        if value is not None:
            din.units = value
    # Automation preferences (July 2026 preview): the generator can lay out
    # views AND dimensions by itself. Property names are probed by keyword —
    # the preview surface renames members between builds.
    if p.get('auto_dimension') is not None or p.get('flat_pattern') is not None:
        with contextlib.suppress(Exception):
            ap = din.automationPreferences
            holders = [ap]
            for name in ('globalPreferences', 'mainAssemblyPreferences',
                         'flatPatternPreferences'):
                holder = getattr(ap, name, None)
                if holder is not None:
                    holders.append(holder)
            for holder in holders:
                for attr in dir(holder):
                    low = attr.lower()
                    if p.get('auto_dimension') is not None and \
                            'dimension' in low and low.startswith('is'):
                        with contextlib.suppress(Exception):
                            setattr(holder, attr, bool(p['auto_dimension']))
                    if p.get('flat_pattern') is not None and \
                            'flatpattern' in low and low.startswith('is'):
                        with contextlib.suppress(Exception):
                            setattr(holder, attr, bool(p['flat_pattern']))
    created = mgr.createDrawing(din)
    out = {'headless': True, 'api': 'DrawingManager'}
    with contextlib.suppress(Exception):
        out['created'] = getattr(created, 'name', None) or app.activeDocument.name
    with contextlib.suppress(Exception):
        drawing = drawing_mod.Drawing.cast(created)
        if drawing:
            out['sheets'] = drawing.sheets.count
    return out


def _try_headless_drawing(app, p):
    """Best-effort headless drawing creation. Prefers the official
    DrawingManager API (Fusion July 2026+); earlier 2026 builds fall back to
    adding a bare drawing document and probing for a template setter. None
    means 'fall back to the UI dialog'."""
    try:
        import adsk.drawing
    except ImportError:
        return None
    with contextlib.suppress(Exception):
        result = _create_drawing_via_manager(app, adsk.drawing, p)
        if result is not None:
            return result
    template = p.get('template')
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
    """Create a drawing for the active design. Fusion July 2026+ does this
    fully headlessly via DrawingManager (optional template, sheet_size
    "A0"-"A4"/"A"-"E", orientation "landscape"|"portrait", standard "iso"|"asme",
    drawing_units "mm"|"in"); older builds fall back to a bare drawing document
    or the "Drawing from Design" dialog. Export with drawing_export; for 2D
    output without a sheet use export_sketch_dxf / export_flat_pattern."""
    if p.get('headless', True):
        result = _try_headless_drawing(app, p)
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


def op_selection_filter(app, p):
    """Inspect or set the active workspace's selection filters (Fusion July
    2026+) — narrow what the user's clicks can pick before asking them to
    select (e.g. faces only). action: "list" | "set" (filters=[names],
    enabled=True/False) | "all" (enabled=True/False)."""
    settings = getattr(app.userInterface.activeWorkspace,
                       'selectionFilterSettings', None)
    if settings is None:
        raise RuntimeError('selectionFilterSettings needs Fusion July 2026+')
    action = (p.get('action') or 'list').lower()
    if action == 'set':
        for name in p.get('filters') or []:
            settings.setFilterEnabled(name, bool(p.get('enabled', True)))
    elif action == 'all':
        settings.areAllFiltersEnabled = bool(p.get('enabled', True))
    elif action != 'list':
        raise ValueError('action must be list|set|all, got %r' % action)
    out = {'action': action}
    with contextlib.suppress(Exception):
        out['select_through'] = settings.isSelectThroughEnabled
    filters = []
    with contextlib.suppress(Exception):
        for name in settings.availableFilters:
            entry = {'name': name}
            with contextlib.suppress(Exception):
                entry['enabled'] = settings.isFilterEnabled(name)
            filters.append(entry)
    out['filters'] = filters
    return out


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
    occurrences, construction geometry) falling back to isVisible (sketches).
    Only assigns to a name the CLASS actually defines — otherwise the assignment
    would silently create an instance attribute and falsely report success
    (BRepFace/BRepEdge/Profile have no visibility toggle)."""
    cls = type(obj)
    last_exc = None
    for attr in ('isLightBulbOn', 'isVisible'):
        if not hasattr(cls, attr):
            continue  # class doesn't define it -> don't create a phantom attr
        try:
            setattr(obj, attr, bool(visible))
            return attr
        except Exception as exc:  # noqa: BLE001 - getter-only? try the next name
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError('%s has no settable visibility (only bodies/occurrences/'
                       'sketches/meshes/construction geometry can be shown/hidden)'
                       % cls.__name__)


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
    # If a face/edge/etc. token was passed, keep its OWNING body visible — else
    # isolating a face would hide its own body and produce a blank screenshot.
    body = getattr(target, 'body', None)
    if body is not None:
        keep.append(body)
        target = body  # the thing we actually keep shown must be a body
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
    # Save the stash BEFORE the final show, so an error here still leaves
    # unisolate able to restore the pre-isolate state.
    _isolate_stash = stash
    with contextlib.suppress(Exception):
        _set_visible(target, True)
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
    raw = p.get('directions') or ['iso', 'front', 'top', 'right']
    width = int(p.get('width', 800))
    height = int(p.get('height', 600))
    base = p.get('base_path') or ''
    # Validate AND normalise all presets up front so a typo (or a non-string
    # smuggled in via batch) doesn't discard already-captured shots halfway
    # through — the capture loop below concatenates d into a file name.
    directions, bad = [], []
    for d in raw:
        s = d if isinstance(d, str) else ('' if d is None else None)
        s = s.lower() if s is not None else None
        if s in ('', 'current'):
            directions.append('current')
        elif s in _CAMERA_DIRS:
            directions.append(s)
        else:
            bad.append(d)
    if bad:
        raise ValueError('unknown camera preset(s) %s; valid: current|%s'
                         % (bad, '|'.join(_CAMERA_DIRS)))
    # Remember the user's camera so we can put it back afterwards.
    saved_cam = None
    with contextlib.suppress(Exception):
        saved_cam = app.activeViewport.camera
    shots = []
    try:
        for d in directions:
            vp = _apply_camera_direction(app, d, p.get('fit', True))
            path = ((base + '_' + d + '.png') if base
                    else os.path.join(tempfile.gettempdir(), 'fusion_mcp_%s.png' % d))
            vp.saveAsImageFile(path, width, height)
            with open(path, 'rb') as fh:
                b64 = base64.b64encode(fh.read()).decode('ascii')
            shots.append({'direction': d, 'path': path, 'image_base64': b64})
    finally:
        if saved_cam is not None:
            with contextlib.suppress(Exception):
                app.activeViewport.camera = saved_cam
                app.activeViewport.refresh()
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

    def _can_undo():
        # The Undo control is disabled exactly when the undo stack is empty.
        # Unlike the timeline marker, this also tracks undoable actions that
        # create no timeline item (set_appearance/set_material/rename, every
        # edit in a direct-modeling design) — a marker-based check either
        # reported phantom steps or, worse, executed an undo and then refused
        # to count it.
        with contextlib.suppress(Exception):
            cd = app.userInterface.commandDefinitions.itemById('UndoCommand')
            return bool(cd.controlDefinition.isEnabled)
        return None

    done = 0
    for _ in range(steps):
        if _can_undo() is False:
            break  # nothing left to undo — checked BEFORE executing
        try:
            app.executeTextCommand('Commands.Start UndoCommand')
        except Exception:
            break
        done += 1
    return {'undone': done, 'requested': steps, 'tokens_may_be_stale': True,
            'note': 'Re-run get_state/query_entities before reusing old tokens.'}


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _swap_ext(path, ext):
    # os.path.splitext handles both / and \ separators and leading-dot names,
    # so a forward-slash path with a dotted directory isn't mangled.
    return os.path.splitext(path)[0] + '.' + ext


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
    # execute() returns False for several failure modes (unwritable/locked file,
    # license-restricted format) without raising; treat that as a real failure so
    # op_export never reports success with no file and can trigger its fallback.
    if not em.execute(opts):
        raise RuntimeError('Export of %r failed (execute returned false — file '
                           'locked, path unwritable, or format restricted).' % path)
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
    doc = app.activeDocument
    if not doc:
        raise RuntimeError('No document is open. Open a document with a '
                           'MANUFACTURE setup first.')
    prod = doc.products.itemByProductType('CAMProductType')
    cam = adsk.cam.CAM.cast(prod) if prod else None
    if not cam:
        raise RuntimeError('No MANUFACTURE data in this document — create a '
                           'setup with cam_setup first (or open the '
                           'MANUFACTURE workspace once).')
    return cam


def _cam_product_materialized(app):
    """Like _cam_product, but when the document has never entered MANUFACTURE
    (so no CAM product exists yet) it activates the MANUFACTURE workspace once
    to materialise it. Returns (cam, previous_workspace_or_None); the caller
    restores the workspace in a finally."""
    import adsk.cam
    doc = app.activeDocument
    if not doc:
        raise RuntimeError('No document is open.')
    prod = doc.products.itemByProductType('CAMProductType')
    cam = adsk.cam.CAM.cast(prod) if prod else None
    if cam:
        return cam, None
    ui = app.userInterface
    ws = ui.workspaces.itemById('CAMEnvironment')
    if ws is None:
        raise RuntimeError('The MANUFACTURE workspace is not available in '
                           'this Fusion install.')
    prev = ui.activeWorkspace
    ws.activate()
    adsk.doEvents()
    prod = doc.products.itemByProductType('CAMProductType')
    cam = adsk.cam.CAM.cast(prod) if prod else None
    if not cam:
        with contextlib.suppress(Exception):
            if prev is not None:
                prev.activate()
        raise RuntimeError('Could not materialise the MANUFACTURE product '
                           'for this document.')
    return cam, prev


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


def _main_thread_ceiling():
    """The bridge's per-op main-thread timeout (seconds). Read lazily to avoid a
    circular import; falls back to the bridge default if unavailable."""
    try:
        import bridge
        return float(getattr(bridge, 'MAIN_THREAD_TIMEOUT', 300))
    except Exception:
        return 300.0


def op_cam_generate(app, p):
    """(Re)generate toolpaths — for one setup by name, or all setups when
    omitted. Blocks until generation finishes or `timeout` s (default 240). The
    wait is capped a few seconds below the bridge main-thread ceiling so a long
    job returns a graceful {completed: false} instead of a transport timeout
    (which would leave the op running re-entrantly)."""
    cam = _cam_product(app)
    if p.get('setup'):
        future = cam.generateToolpath(_cam_setup_by_name(cam, p['setup']))
    else:
        future = cam.generateAllToolpaths(False)
    requested = float(p.get('timeout', 240))
    ceiling = max(30.0, _main_thread_ceiling() - 15.0)
    effective = min(requested, ceiling)
    deadline = time.time() + effective
    while not future.isGenerationCompleted and time.time() < deadline:
        adsk.doEvents()
        time.sleep(0.2)
    return {'completed': bool(future.isGenerationCompleted),
            'setup': p.get('setup') or 'all',
            'waited_s': round(effective, 1),
            'timeout_clamped': effective < requested}


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


_CAM_OP_TYPES = {'milling': 'MillingOperation', 'turning': 'TurningOperation',
                 'jet': 'JetOperation', 'additive': 'AdditiveOperation'}
_CAM_STOCK_MODES = {
    'relative_box': 'RelativeBoxStock', 'fixed_box': 'FixedBoxStock',
    'relative_cylinder': 'RelativeCylinderStock',
    'fixed_cylinder': 'FixedCylinderStock',
    'relative_tube': 'RelativeTubeStock', 'fixed_tube': 'FixedTubeStock',
    'solid': 'SolidStock', 'previous_setup': 'PreviousSetupStock',
}


def op_cam_setup(app, p):
    """Create a MANUFACTURE setup (Fusion 2023+; GA since v2704 for the full
    flow). bodies: body/occurrence tokens to machine (default: every root
    body); operation_type: milling|turning|jet|additive; stock_mode:
    relative_box (default)|fixed_box|relative_cylinder|fixed_cylinder|
    relative_tube|fixed_tube|solid|previous_setup; optional name. Operations/
    toolpaths are then added in the UI or via run_fusion_code; generate with
    cam_generate, post with cam_post. Milling needs no machine; additive
    setups do (not created here)."""
    import adsk.cam
    cam, prev_ws = _cam_product_materialized(app)
    try:
        kind = (p.get('operation_type') or 'milling').lower()
        if kind not in _CAM_OP_TYPES:
            raise ValueError('operation_type must be one of %s, got %r'
                             % (sorted(_CAM_OP_TYPES), p.get('operation_type')))
        sin = cam.setups.createInput(
            getattr(adsk.cam.OperationTypes, _CAM_OP_TYPES[kind]))
        models = [_registry.get(t) for t in (p.get('bodies') or [])]
        if not models:
            with contextlib.suppress(Exception):
                bodies = cam.designRootOccurrence.bRepBodies
                models = [bodies.item(i) for i in range(bodies.count)]
        if not models:
            raise RuntimeError('No bodies to machine — pass bodies=[tokens].')
        try:
            sin.models = models
        except Exception:
            # Design-side proxies can be rejected: remap to the CAM product's
            # own view of the design bodies by name.
            cam_bodies = {}
            with contextlib.suppress(Exception):
                bodies = cam.designRootOccurrence.bRepBodies
                cam_bodies = {bodies.item(i).name: bodies.item(i)
                              for i in range(bodies.count)}
            sin.models = [cam_bodies.get(getattr(m, 'name', None), m)
                          for m in models]
        stock = (p.get('stock_mode') or 'relative_box').lower()
        if stock not in _CAM_STOCK_MODES:
            raise ValueError('stock_mode must be one of %s, got %r'
                             % (sorted(_CAM_STOCK_MODES), p.get('stock_mode')))
        with contextlib.suppress(Exception):
            sin.stockMode = getattr(adsk.cam.SetupStockModes,
                                    _CAM_STOCK_MODES[stock])
        if p.get('name'):
            with contextlib.suppress(Exception):
                sin.name = str(p['name'])
        setup = cam.setups.add(sin)
        if setup is None:
            raise RuntimeError('Fusion refused to create the setup.')
        if p.get('name'):
            with contextlib.suppress(Exception):
                setup.name = str(p['name'])
        return {'setup': setup.name, 'operation_type': kind,
                'stock_mode': stock, 'models': len(models)}
    finally:
        if prev_ws is not None:
            with contextlib.suppress(Exception):
                prev_ws.activate()


def op_cam_suppress(app, p):
    """Suppress (or with suppress=False, restore) a CAM setup or one of its
    operations by name — skip an operation without deleting it. Names come
    from cam_setups."""
    cam = _cam_product(app)
    name = p['name']
    suppress = bool(p.get('suppress', True))
    target = None
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        if s.name == name:
            target = s
            break
        found = None
        with contextlib.suppress(Exception):
            for j in range(s.allOperations.count):
                o = s.allOperations.item(j)
                if o.name == name:
                    found = o
                    break
        if found is not None:
            target = found
            break
    if target is None:
        raise RuntimeError('No CAM setup or operation named %r (see '
                           'cam_setups)' % name)
    if getattr(target, 'isSuppressible', True) is False:
        raise RuntimeError('%r cannot be suppressed in this build.' % name)
    target.isSuppressed = suppress
    return {'name': name, 'suppressed': bool(target.isSuppressed)}


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
    API objects (not token dicts), so a whole part can be built in a few lines.
    The design-dependent helpers resolve _root(app) lazily, so importing the
    scope never fails outside the DESIGN workspace."""

    def h_sketch(plane='XY', name=None):
        sk = _root(app).sketches.add(_resolve_plane(app, plane))
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
        feats = _root(app).features.extrudeFeatures
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


def _code_store_put(name, obj):
    _code_store[str(name)] = obj
    return str(name)


def op_run_code(app, p):
    """Execute an arbitrary Fusion API snippet on the main thread.

    Available names: adsk, app, ui, design, root, math, MM, registry,
    reg(kind, obj) -> token, tok(token) -> live object,
    store(name, obj) / fetch(name) -> keep objects across snippets,
    plus mm/degree helpers: pt, mm, vmm, deg,
    new_sketch(plane), rect(sk,x1,y1,x2,y2), circle(sk,cx,cy,r),
    extrude_profile(profile, dist_mm, operation, symmetric).
    Assign to `result` to return a value.

    design/root are bound lazily: a snippet that never touches them works even
    when a drawing/CAM document is active (where there is no Design).
    """
    code = p['code']

    def _lazy(fn):
        try:
            return fn(app)
        except Exception:
            return None

    g = {
        'adsk': adsk,
        'app': app,
        'ui': app.userInterface,
        'design': _lazy(_design),
        'root': _lazy(_root),
        'math': math,
        'MM': MM,
        'registry': _registry,
        'reg': lambda kind, obj: _registry.add(kind, obj),
        'tok': _registry.get,
        'store': _code_store_put,
        'fetch': _code_store.get,
    }
    g.update(_run_code_helpers(app))
    buf = io.StringIO()
    # ONE namespace: with split globals/locals, names defined at top level are
    # invisible inside def/lambda/comprehension bodies (they resolve against
    # globals only) — ordinary multi-line snippets would raise NameError.
    with contextlib.redirect_stdout(buf):
        exec(code, g)  # noqa: S102 - intentional escape hatch
    return {'stdout': buf.getvalue(), 'result': _jsonable(g.get('result'))}


def op_reset_registry(app, p):
    _registry.reset()
    return {'reset': True}


def _introspect_target(p):
    """Resolve the introspection target: a registry token ('bdy1'), a stored
    run_code object ('$name'), or a dotted adsk path ('adsk.fusion.Component')."""
    target = p.get('target') or 'adsk.fusion'
    if target.startswith('$'):
        obj = _code_store.get(target[1:])
        if obj is None:
            raise KeyError('Nothing stored under %r — use store(name, obj) in '
                           'run_fusion_code first' % target)
        return target, obj
    obj = _registry.get_opt(target)
    if obj is not None:
        return target, obj
    parts = target.split('.')
    if parts[0] != 'adsk':
        raise ValueError('target must be an entity token, a $stored name, or a '
                         'dotted adsk.* path, got %r' % target)
    import importlib
    obj = adsk
    for i, part in enumerate(parts[1:], start=2):
        nxt = getattr(obj, part, None)
        if nxt is None and i == 2:
            # Lazily import optional namespaces (adsk.drawing, adsk.electron...)
            with contextlib.suppress(Exception):
                importlib.import_module('.'.join(parts[:i]))
                nxt = getattr(obj, part, None)
        if nxt is None:
            raise AttributeError('%s has no attribute %r'
                                 % ('.'.join(parts[:i - 1]), part))
        obj = nxt
    return target, obj


def op_api_introspect(app, p):
    """Explore the Fusion API surface without leaving the chat: list an
    object's members with one-line docs. target: a registry token ('bdy1'),
    a run_code-stored object ('$jig') or a dotted path
    ('adsk.fusion.ExtrudeFeatures'). `query` filters member names (substring).
    The companion of run_fusion_code — check exact property/method names
    before writing a snippet. Members are read off the CLASS, so no live
    properties are evaluated."""
    import inspect as pyinspect
    target, obj = _introspect_target(p)
    query = (p.get('query') or '').lower()
    limit = max(1, int(p.get('limit', 80)))
    direct = pyinspect.ismodule(obj) or pyinspect.isclass(obj)
    holder = obj if direct else type(obj)
    names = [n for n in dir(holder) if not n.startswith('_')]
    if query:
        names = [n for n in names if query in n.lower()]
    names.sort()
    members = []
    for name in names[:limit]:
        try:
            attr = getattr(holder, name)
        except Exception:  # noqa: BLE001 - descriptor refused; still report it
            members.append({'name': name, 'kind': 'property'})
            continue
        if isinstance(attr, property):
            kind, doc = 'property', attr.__doc__
        elif callable(attr):
            kind, doc = 'method', getattr(attr, '__doc__', None)
        else:
            kind, doc = 'attribute', None
        entry = {'name': name, 'kind': kind}
        if kind == 'attribute' and isinstance(attr, (bool, int, float, str)):
            entry['value'] = attr
        if doc:
            entry['doc'] = doc.strip().splitlines()[0][:160]
        members.append(entry)
    out = {'target': target, 'count': len(names), 'members': members}
    if len(names) > len(members):
        out['truncated_to'] = len(members)
    with contextlib.suppress(Exception):
        out['type'] = (getattr(obj, '__name__', None) if direct
                       else obj.objectType)
    doc = pyinspect.getdoc(holder)
    if doc:
        out['doc'] = doc.splitlines()[0][:200]
    return out


# --------------------------------------------------------------------------- #
# Configurations
# --------------------------------------------------------------------------- #
def op_configurations(app, p):
    """Work with a configured design's configuration table. action: "list"
    (rows = configurations + column titles + active row), "activate"
    (name = row name), "cell" (row=, column= indexes — read one cell).
    Returns {configured: false} when the design has no configurations."""
    design = _design(app)
    try:
        table = design.configurationTopTable
    except Exception:  # noqa: BLE001 - older builds / unconfigured designs
        table = None
    if not table:
        return {'configured': False,
                'note': 'The active design has no configuration table.'}
    action = (p.get('action') or 'list').lower()
    rows = table.rows

    def row_entries():
        out = []
        for i in range(rows.count):
            row = rows.item(i)
            entry = {'index': i, 'name': row.name}
            with contextlib.suppress(Exception):
                entry['id'] = row.id
            out.append(entry)
        return out

    if action == 'activate':
        target = p.get('name')
        for i in range(rows.count):
            row = rows.item(i)
            if row.name == target:
                row.activate()
                return {'activated': target}
        raise ValueError('No configuration named %r (have: %s)' % (
            target, ', '.join(e['name'] for e in row_entries()) or 'none'))
    if action == 'cell':
        r, c = int(p['row']), int(p['column'])
        ncols = table.columns.count
        if not (0 <= r < rows.count) or not (0 <= c < ncols):
            raise ValueError('cell (row=%d, column=%d) out of range; table is '
                             '%d rows x %d columns' % (r, c, rows.count, ncols))
        cell = table.getCell(r, c)
        if cell is None:
            raise ValueError('No cell at (row=%d, column=%d).' % (r, c))
        out = {'row': r, 'column': c}
        for attr in ('value', 'expression', 'title', 'name'):
            with contextlib.suppress(Exception):
                out[attr] = _jsonable(getattr(cell, attr))
        return out
    if action != 'list':
        raise ValueError('action must be list|activate|cell, got %r' % action)
    out = {'configured': True, 'rows': row_entries()}
    with contextlib.suppress(Exception):
        out['active'] = table.activeRow.name
    columns = []
    with contextlib.suppress(Exception):
        for i in range(table.columns.count):
            col = table.columns.item(i)
            entry = {'index': i}
            for attr in ('title', 'name', 'id'):
                with contextlib.suppress(Exception):
                    entry[attr] = getattr(col, attr)
            columns.append(entry)
    out['columns'] = columns
    return out


# --------------------------------------------------------------------------- #
# Electronics (read-only) — schematics, PCBs and libraries via adsk.electron.
# The Electronics API (Fusion May 2026+) is a read-only preview: inspect and
# export, no editing. Its coordinates are ints in internal editor units
# (1/320000 mm) and are converted to millimetres on the wire like everything
# else in this file.
# --------------------------------------------------------------------------- #
def _electron():
    try:
        import adsk.electron
    except ImportError:
        raise RuntimeError(
            'This Fusion has no Electronics API (adsk.electron) — it ships '
            'with the May 2026 update. Update Fusion to use electronics_* ops.')
    return adsk.electron


def _eitems(coll):
    """Iterate a count/item(i) Electronics collection (None-safe)."""
    if coll is None:
        return
    for i in range(int(coll.count)):
        item = coll.item(i)
        if item is not None:
            yield item


def _u2mm(value):
    return round(_electron().Units.u2mm(int(value)), 4)


def _ecount(obj, prop):
    with contextlib.suppress(Exception):
        coll = getattr(obj, prop, None)
        if coll is not None:
            return int(coll.count)
    return None


def _ecad_attrs(obj):
    """EcadAttributes on a part/element -> {name: value} (best effort)."""
    out = {}
    with contextlib.suppress(Exception):
        for a in _eitems(obj.attributes):
            with contextlib.suppress(Exception):
                if a.name:
                    out[str(a.name)] = '' if a.value is None else str(a.value)
    return out


def _ecad_context(app):
    """Resolve (schematic, board, library, active_kind) from the active product.

    Any Electronics product works as an entry point: an EcadDesign reaches
    both sides, a schematic reaches its linked board and vice versa.
    """
    electron = _electron()
    product = app.activeProduct
    sch = electron.Schematic.cast(product)
    brd = electron.Board.cast(product)
    lib = electron.Library.cast(product)
    des = electron.EcadDesign.cast(product)
    active = ('schematic' if sch else 'board' if brd else 'library' if lib
              else 'design' if des else None)
    if active is None:
        raise RuntimeError(
            'The active product is not an Electronics document. Open a '
            'schematic, 2D PCB, electronics design or library tab first.')
    if des:
        with contextlib.suppress(Exception):
            sch = sch or des.schematic
        with contextlib.suppress(Exception):
            brd = brd or des.board
    if sch and not brd:
        with contextlib.suppress(Exception):
            brd = sch.linkedBoard
    if brd and not sch:
        with contextlib.suppress(Exception):
            sch = brd.linkedSchematic
    return sch, brd, lib, active


def op_electronics_info(app, p):
    """Overview of the open electronics design: which product is active
    (schematic/board/library/design), summary counts for each reachable side,
    per-sheet breakdown and ERC/DRC error counts. The electronics get_state."""
    sch, brd, lib, active = _ecad_context(app)
    out = {'active': active}
    if sch:
        info = {'name': getattr(sch, 'name', None)}
        with contextlib.suppress(Exception):
            info['headline'] = sch.headline
        for prop, key in (('sheets', 'sheets'), ('parts', 'parts'),
                          ('nets', 'nets'), ('modules', 'modules'),
                          ('errors', 'erc_errors')):
            n = _ecount(sch, prop)
            if n is not None:
                info[key] = n
        sheets = []
        with contextlib.suppress(Exception):
            for sh in _eitems(sch.sheets):
                entry = {}
                with contextlib.suppress(Exception):
                    entry['number'] = int(sh.number)
                with contextlib.suppress(Exception):
                    entry['name'] = sh.name
                for prop in ('instances', 'wires', 'nets', 'busses', 'texts'):
                    n = _ecount(sh, prop)
                    if n is not None:
                        entry[prop] = n
                sheets.append(entry)
        if sheets:
            info['per_sheet'] = sheets
        out['schematic'] = info
    if brd:
        info = {'name': getattr(brd, 'name', None)}
        with contextlib.suppress(Exception):
            info['headline'] = brd.headline
        for prop, key in (('elements', 'elements'), ('signals', 'signals'),
                          ('layers', 'layers'), ('holes', 'holes'),
                          ('errors', 'drc_errors')):
            n = _ecount(brd, prop)
            if n is not None:
                info[key] = n
        out['board'] = info
    if lib:
        info = {'name': getattr(lib, 'name', None)}
        with contextlib.suppress(Exception):
            info['id'] = lib.id
        with contextlib.suppress(Exception):
            info['editable'] = bool(lib.editable)
        for prop, key in (('deviceSets', 'device_sets'), ('devices', 'devices'),
                          ('symbols', 'symbols'), ('packages', 'packages'),
                          ('packages3d', 'packages_3d')):
            n = _ecount(lib, prop)
            if n is not None:
                info[key] = n
        out['library'] = info
    return out


def op_electronics_components(app, p):
    """List components: board elements (position mm / rotation deg, footprint,
    populated flag) or schematic parts (value, device set, attributes such as
    MPN). side: auto|board|schematic; optional name substring filter, limit."""
    sch, brd, lib, active = _ecad_context(app)
    side = (p.get('side') or 'auto').lower()
    if side == 'auto':
        side = 'board' if (brd and active != 'schematic') else 'schematic'
    flt = (p.get('filter') or '').lower()
    limit = int(p.get('limit') or 0)
    items = []
    if side == 'board':
        if not brd:
            raise RuntimeError('No board is reachable from the active document.')
        source = brd.elements
    elif side == 'schematic':
        if not sch:
            raise RuntimeError('No schematic is reachable from the active '
                               'document.')
        source = sch.parts
    else:
        raise ValueError('side must be auto|board|schematic, got %r' % side)
    for it in _eitems(source):
        name = str(getattr(it, 'name', '') or '')
        if flt and flt not in name.lower():
            continue
        entry = {'name': name}
        with contextlib.suppress(Exception):
            entry['value'] = '' if it.value is None else str(it.value)
        if side == 'board':
            with contextlib.suppress(Exception):
                entry['x_mm'] = _u2mm(it.x)
                entry['y_mm'] = _u2mm(it.y)
            with contextlib.suppress(Exception):
                entry['angle_deg'] = round(float(it.angle), 3)
            with contextlib.suppress(Exception):
                entry['mirrored'] = bool(it.mirror)
            with contextlib.suppress(Exception):
                entry['locked'] = bool(it.locked)
            with contextlib.suppress(Exception):
                entry['populated'] = bool(it.populate)
            with contextlib.suppress(Exception):
                if it.package is not None:
                    entry['package'] = it.package.name
        else:
            with contextlib.suppress(Exception):
                if it.deviceset is not None:
                    entry['device_set'] = it.deviceset.name
            with contextlib.suppress(Exception):
                if it.device is not None and it.device.package is not None:
                    entry['package'] = it.device.package.name
            n = _ecount(it, 'instances')
            if n is not None:
                entry['gates_placed'] = n
        with contextlib.suppress(Exception):
            if it.package3d is not None:
                entry['package3d'] = it.package3d.name
        attrs = _ecad_attrs(it)
        if attrs:
            entry['attributes'] = attrs
        items.append(entry)
        if limit and len(items) >= limit:
            break
    return {'side': side, 'count': len(items), 'components': items}


def op_electronics_nets(app, p):
    """Connectivity: schematic nets with their pin connections (part + pin),
    or board copper signals with trace/via/pour counts and pad contacts.
    side: auto|schematic|board; optional name substring filter, limit."""
    sch, brd, lib, active = _ecad_context(app)
    side = (p.get('side') or 'auto').lower()
    if side == 'auto':
        side = 'board' if (brd and active == 'board') else \
               'schematic' if sch else 'board'
    flt = (p.get('filter') or '').lower()
    limit = int(p.get('limit') or 0)
    nets = []
    if side == 'schematic':
        if not sch:
            raise RuntimeError('No schematic is reachable from the active '
                               'document.')
        for net in _eitems(sch.nets):
            name = str(getattr(net, 'name', '') or '')
            if flt and flt not in name.lower():
                continue
            entry = {'name': name}
            with contextlib.suppress(Exception):
                if net.netClass is not None:
                    entry['class'] = net.netClass.name
            pins = []
            with contextlib.suppress(Exception):
                for pr in _eitems(net.pinRefs):
                    ref = {}
                    with contextlib.suppress(Exception):
                        ref['part'] = pr.part.name
                    with contextlib.suppress(Exception):
                        ref['pin'] = pr.pin.name
                    if ref:
                        pins.append(ref)
            entry['pins'] = pins
            nets.append(entry)
            if limit and len(nets) >= limit:
                break
    elif side == 'board':
        if not brd:
            raise RuntimeError('No board is reachable from the active document.')
        for sig in _eitems(brd.signals):
            name = str(getattr(sig, 'name', '') or '')
            if flt and flt not in name.lower():
                continue
            entry = {'name': name}
            with contextlib.suppress(Exception):
                if sig.netClass is not None:
                    entry['class'] = sig.netClass.name
            for prop, key in (('wires', 'traces'), ('vias', 'vias'),
                              ('polyPours', 'pours')):
                n = _ecount(sig, prop)
                if n is not None:
                    entry[key] = n
            contacts = []
            with contextlib.suppress(Exception):
                for cr in _eitems(sig.contactRefs):
                    ref = {}
                    with contextlib.suppress(Exception):
                        ref['element'] = cr.element.name
                    with contextlib.suppress(Exception):
                        ref['pad'] = cr.contact.name
                    if ref:
                        contacts.append(ref)
            entry['contacts'] = contacts
            nets.append(entry)
            if limit and len(nets) >= limit:
                break
    else:
        raise ValueError('side must be auto|schematic|board, got %r' % side)
    return {'side': side, 'count': len(nets), 'nets': nets}


def op_electronics_layers(app, p):
    """Layer table of the board (fallback: schematic/library): number, name,
    used, visible, color. used_only=True hides unused layers."""
    sch, brd, lib, active = _ecad_context(app)
    src = brd or sch or lib
    if src is None:
        raise RuntimeError('No layer table is reachable from the active '
                           'document.')
    used_only = bool(p.get('used_only', False))
    layers = []
    for ly in _eitems(src.layers):
        entry = {}
        with contextlib.suppress(Exception):
            entry['number'] = int(ly.number)
        with contextlib.suppress(Exception):
            entry['name'] = ly.name
        with contextlib.suppress(Exception):
            entry['used'] = bool(ly.used)
        with contextlib.suppress(Exception):
            entry['visible'] = bool(ly.visible)
        with contextlib.suppress(Exception):
            entry['color'] = str(ly.color)
        if used_only and not entry.get('used'):
            continue
        layers.append(entry)
    source = 'board' if src is brd else 'schematic' if src is sch else 'library'
    return {'source': source, 'count': len(layers), 'layers': layers}


def op_electronics_library(app, p):
    """Inspect component libraries. With a library document active: its device
    sets, each with devices and their packages. With a schematic/board/design
    active: the libraries embedded in that document, with content counts.
    Optional name substring filter and limit (applies to device sets)."""
    sch, brd, lib, active = _ecad_context(app)
    flt = (p.get('filter') or '').lower()
    limit = int(p.get('limit') or 0)
    if lib:
        out = {'library': getattr(lib, 'name', None)}
        with contextlib.suppress(Exception):
            out['editable'] = bool(lib.editable)
        for prop, key in (('symbols', 'symbols'), ('packages', 'packages'),
                          ('packages3d', 'packages_3d')):
            n = _ecount(lib, prop)
            if n is not None:
                out[key] = n
        sets = []
        for ds in _eitems(lib.deviceSets):
            name = str(getattr(ds, 'name', '') or '')
            if flt and flt not in name.lower():
                continue
            entry = {'name': name}
            with contextlib.suppress(Exception):
                entry['description'] = ds.description
            devices = []
            with contextlib.suppress(Exception):
                for d in _eitems(ds.devices):
                    dev = {'name': str(getattr(d, 'name', '') or '')}
                    with contextlib.suppress(Exception):
                        if d.package is not None:
                            dev['package'] = d.package.name
                    devices.append(dev)
            entry['devices'] = devices
            sets.append(entry)
            if limit and len(sets) >= limit:
                break
        out['count'] = len(sets)
        out['device_sets'] = sets
        return out
    src = brd if active == 'board' else sch if active == 'schematic' else (brd or sch)
    if src is None:
        raise RuntimeError('No schematic or board is reachable from the active '
                           'document, so no embedded libraries can be listed.')
    libs = []
    with contextlib.suppress(Exception):
        for lib in _eitems(src.libraries):
            name = str(getattr(lib, 'name', '') or '')
            if flt and flt not in name.lower():
                continue
            entry = {'name': name}
            for prop, key in (('deviceSets', 'device_sets'),
                              ('devices', 'devices'), ('symbols', 'symbols'),
                              ('packages', 'packages'),
                              ('packages3d', 'packages_3d')):
                n = _ecount(lib, prop)
                if n is not None:
                    entry[key] = n
            libs.append(entry)
            if limit and len(libs) >= limit:
                break
    return {'count': len(libs), 'libraries': libs}


def op_electronics_export(app, p):
    """Export electronics to EAGLE 9.6.2 files — .brd (board), .sch (schematic)
    or .lbr (library), chosen by the path extension. The matching product must
    be reachable from the active document."""
    sch, brd, lib, active = _ecad_context(app)
    path = p['path']
    ext = os.path.splitext(path)[1].lower()
    targets = {'.brd': (brd, 'createEagleBrdExportOptions', 'board'),
               '.sch': (sch, 'createEagleSchExportOptions', 'schematic'),
               '.lbr': (lib, 'createEagleLbrExportOptions', 'library')}
    if ext not in targets:
        raise ValueError('path must end in .brd, .sch or .lbr, got %r' % ext)
    product, factory_name, kind = targets[ext]
    if product is None:
        raise RuntimeError('No %s is reachable from the active document.' % kind)
    em = getattr(product, 'exportManager', None)
    if em is None:
        raise RuntimeError('This Fusion version exposes no electronics export '
                           'API.')
    factory = getattr(em, factory_name, None)
    options = factory(path) if factory else None
    if options is None:
        raise RuntimeError('Fusion refused to create %s export options — is '
                           'the right document open?' % ext)
    if not em.execute(options):
        raise RuntimeError('Electronics export to %r failed.' % path)
    return {'exported': path, 'kind': kind}


# --------------------------------------------------------------------------- #
# Materials & appearances (read-only browse)
# --------------------------------------------------------------------------- #
def _collect_named(collection, flt, out, seen, source):
    for i in range(collection.count):
        item = collection.item(i)
        name = getattr(item, 'name', None)
        if not name or (flt and flt not in name.lower()):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({'name': name, 'source': source})


def op_list_materials(app, p):
    """Browse available materials by name so set_material has an exact target:
    materials already in the document, your favourites, and the shipped
    material libraries. filter = name substring; limit caps the result."""
    design = _design(app)
    flt = (p.get('filter') or '').lower()
    limit = int(p.get('limit') or 200)
    out, seen = [], set()
    with contextlib.suppress(Exception):
        _collect_named(design.materials, flt, out, seen, 'document')
    with contextlib.suppress(Exception):
        # Favourites live on Application, not Design.
        _collect_named(app.favoriteMaterials, flt, out, seen, 'favorite')
    with contextlib.suppress(Exception):
        libs = app.materialLibraries
        for i in range(libs.count):
            lib = libs.item(i)
            with contextlib.suppress(Exception):
                _collect_named(lib.materials, flt, out, seen, lib.name)
            if len(out) >= limit:
                break
    shown = out[:limit]
    return {'count': len(shown), 'total_matched': len(out), 'materials': shown}


def op_list_appearances(app, p):
    """Browse available appearances by name for set_appearance. Covers the
    document's appearances and the shipped appearance libraries. filter = name
    substring; limit caps the result."""
    design = _design(app)
    flt = (p.get('filter') or '').lower()
    limit = int(p.get('limit') or 200)
    out, seen = [], set()
    with contextlib.suppress(Exception):
        _collect_named(design.appearances, flt, out, seen, 'document')
    with contextlib.suppress(Exception):
        _collect_named(app.favoriteAppearances, flt, out, seen, 'favorite')
    with contextlib.suppress(Exception):
        libs = app.materialLibraries
        for i in range(libs.count):
            lib = libs.item(i)
            with contextlib.suppress(Exception):
                _collect_named(lib.appearances, flt, out, seen, lib.name)
            if len(out) >= limit:
                break
    shown = out[:limit]
    return {'count': len(shown), 'total_matched': len(out), 'appearances': shown}


# --------------------------------------------------------------------------- #
# Parametric fasteners — ISO 4762 socket-head cap screws, built from a table
# --------------------------------------------------------------------------- #
# size -> (nominal d, head dia dk, head height k, socket across-flats s, pitch)
_ISO4762 = {
    'M3': (3.0, 5.5, 3.0, 2.5, 0.5),
    'M4': (4.0, 7.0, 4.0, 3.0, 0.7),
    'M5': (5.0, 8.5, 5.0, 4.0, 0.8),
    'M6': (6.0, 10.0, 6.0, 5.0, 1.0),
    'M8': (8.0, 13.0, 8.0, 6.0, 1.25),
    'M10': (10.0, 16.0, 10.0, 8.0, 1.5),
    'M12': (12.0, 18.0, 12.0, 10.0, 1.75),
}


def _native_fastener(app, size, length_mm):
    """Fusion v2704+ exposes content-library fasteners
    (FastenerOccurrenceDefinition). Our research notes name the class but not
    its factory signature, so probe a few plausible spellings defensively and
    return None to fall back to the parametric ISO 4762 model. Live-Fusion
    verification: api_introspect
    target="adsk.fusion.FastenerOccurrenceDefinition"."""
    cls = getattr(adsk.fusion, 'FastenerOccurrenceDefinition', None)
    if cls is None:
        return None
    definition = None
    for args in (('ISO 4762', size, length_mm * MM),
                 (size, length_mm * MM), (size,)):
        with contextlib.suppress(Exception):
            definition = cls.create(*args)
        if definition is not None:
            break
    if definition is None:
        return None
    occs = _root(app).occurrences
    occ = None
    for meth in ('addByOccurrenceDefinition',
                 'addByFastenerOccurrenceDefinition', 'addByDefinition'):
        fn = getattr(occs, meth, None)
        if fn is None:
            continue
        with contextlib.suppress(Exception):
            occ = fn(definition)
        if occ is not None:
            break
    if occ is None:
        return None
    out = {'occurrence': _registry.add('occ', occ), 'native': True,
           'size': size, 'length_mm': length_mm}
    with contextlib.suppress(Exception):
        comp = occ.component
        out['component'] = _registry.add('cmp', comp)
        out['name'] = comp.name
        out['bodies'] = [_registry.add('bdy', b) for b in comp.bRepBodies]
    return out


def op_insert_fastener(app, p):
    """Insert an ISO 4762 socket-head cap screw as its own component — via the
    native content-library fastener API when this Fusion build has it
    (v2704+), else modelled parametrically (head, shank, hex socket, best-
    effort cosmetic thread). size: M3|M4|M5|M6|M8|M10|M12; length mm (shank
    under the head); thread=False skips the thread (parametric path only);
    native=False forces the parametric path. Returns component/body tokens."""
    size = str(p.get('size', 'M6')).upper()
    if size not in _ISO4762:
        raise ValueError('size must be one of %s, got %r'
                         % (sorted(_ISO4762), p.get('size')))
    length = float(p.get('length', 20.0))
    if length <= 0:
        raise ValueError('length must be > 0 mm')
    if p.get('native', True):
        native = _native_fastener(app, size, length)
        if native is not None:
            return native
    d, dk, k, s, _pitch = _ISO4762[size]
    root = _root(app)
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = '%sx%g SHCS' % (size, length)
    feats = comp.features
    sketches = comp.sketches
    xy = comp.xYConstructionPlane

    def circle_extrude(diameter, z0_mm, height_mm, operation='new'):
        sk = sketches.add(xy)
        sk.sketchCurves.sketchCircles.addByCenterRadius(_pt(0, 0), diameter / 2.0 * MM)
        prof = sk.profiles.item(0)
        ein = feats.extrudeFeatures.createInput(prof, _operation(operation))
        start = adsk.fusion.FromEntityStartDefinition.create(xy, _vi(z0_mm * MM)) \
            if z0_mm else None
        ein.setDistanceExtent(False, _vi(height_mm * MM))
        if start is not None:
            with contextlib.suppress(Exception):
                ein.startExtent = start
        return feats.extrudeFeatures.add(ein)

    # Head: from z=0 up +k. Shank: from z=0 down -length.
    head = circle_extrude(dk, 0.0, k, 'new')
    circle_extrude(d, 0.0, -length, 'join')

    # Hex socket cut into the head top (across-flats s, depth ~0.6k). The hex is
    # drawn on the top plane and cut downward into the head.
    with contextlib.suppress(Exception):
        r_socket = s / math.sqrt(3.0)  # across-flats -> circumradius
        top = comp.constructionPlanes.createInput()
        top.setByOffset(xy, _vi(k * MM))
        top_plane = comp.constructionPlanes.add(top)
        sk2 = sketches.add(top_plane)
        lines = sk2.sketchCurves.sketchLines
        verts = [_pt(r_socket * math.cos(math.radians(60 * i + 30)),
                     r_socket * math.sin(math.radians(60 * i + 30))) for i in range(6)]
        for i in range(6):
            lines.addByTwoPoints(verts[i], verts[(i + 1) % 6])
        prof = sk2.profiles.item(0)
        ein = feats.extrudeFeatures.createInput(
            prof, adsk.fusion.FeatureOperations.CutFeatureOperation)
        ein.setDistanceExtent(False, _vi(-0.6 * k * MM))
        feats.extrudeFeatures.add(ein)

    out = {'component': _registry.add('cmp', comp),
           'occurrence': _registry.add('occ', occ), 'name': comp.name,
           'size': size, 'length_mm': length, 'native': False}
    if getattr(adsk.fusion, 'FastenerOccurrenceDefinition', None) is not None:
        out['note'] = ('Native fastener API detected but its factory shape '
                       'did not match — modelled parametrically instead. '
                       'Probe with api_introspect target='
                       '"adsk.fusion.FastenerOccurrenceDefinition".')
    # Cosmetic thread on the shank side face (best effort).
    if p.get('thread', True):
        with contextlib.suppress(Exception):
            body = head.bodies.item(0)
            shank_faces = [f for f in body.faces
                           if _surface_type(f) == 'cylinder'
                           and abs(f.geometry.radius - d / 2.0 * MM) < 1e-4 * 10]
            if shank_faces:
                tfeats = feats.threadFeatures
                q = tfeats.threadDataQuery
                tt = q.defaultMetricThreadType
                ok, desig, cls = q.recommendThreadData(d * MM, False, tt)
                if ok:
                    info = tfeats.createThreadInfo(False, tt, desig, cls)
                    tin = tfeats.createInput(shank_faces[0], info)
                    tin.isModeled = False
                    tfeats.add(tin)
                    out['threaded'] = True
    bodies = []
    with contextlib.suppress(Exception):
        for b in comp.bRepBodies:
            bodies.append(_registry.add('bdy', b))
    out['bodies'] = bodies
    return out


# --------------------------------------------------------------------------- #
# Cloud data: folders, versions
# --------------------------------------------------------------------------- #
def _unix_to_iso(ts):
    with contextlib.suppress(Exception):
        import datetime
        return datetime.datetime.utcfromtimestamp(int(ts)).isoformat() + 'Z'
    return None


def op_data_folders(app, p):
    """Browse the cloud data structure: projects -> folders (recursive) with
    file names. project = optional project-name filter; max_depth caps recursion
    (default 3). A read-only map for finding documents to open_document."""
    data = app.data
    flt = p.get('project')
    max_depth = int(p.get('max_depth', 3))

    def walk_folder(folder, depth):
        node = {'name': folder.name, 'files': [], 'folders': []}
        with contextlib.suppress(Exception):
            for i in range(folder.dataFiles.count):
                node['files'].append(folder.dataFiles.item(i).name)
        if depth < max_depth:
            with contextlib.suppress(Exception):
                subs = folder.dataFolders
                for i in range(subs.count):
                    node['folders'].append(walk_folder(subs.item(i), depth + 1))
        return node

    projects = []
    for i in range(data.dataProjects.count):
        proj = data.dataProjects.item(i)
        if flt and proj.name != flt:
            continue
        with contextlib.suppress(Exception):
            projects.append({'project': proj.name,
                             'root': walk_folder(proj.rootFolder, 0)})
    if flt and not projects:
        raise RuntimeError('No cloud project named %r' % flt)
    return {'count': len(projects), 'projects': projects}


def _active_datafile(app):
    doc = app.activeDocument
    if not doc:
        raise RuntimeError('No active document.')
    # Document.dataFile RAISES (not returns None) for a never-saved document,
    # so suppress — the point of this guard is the actionable message below.
    df = None
    with contextlib.suppress(Exception):
        df = doc.dataFile
    if df is None:
        raise RuntimeError('The active document is not saved to the cloud, so it '
                           'has no version history or share link. Save it first.')
    return doc, df


def op_version_history(app, p):
    """Version history of the active (saved) document: version number, date and
    id per version, newest first — narrate 'what changed since v12'."""
    doc, df = _active_datafile(app)
    out = []
    with contextlib.suppress(Exception):
        versions = df.versions
        for i in range(versions.count):
            v = versions.item(i)
            entry = {}
            for attr, key in (('versionNumber', 'version'), ('id', 'id')):
                with contextlib.suppress(Exception):
                    entry[key] = getattr(v, attr)
            with contextlib.suppress(Exception):
                entry['created'] = _unix_to_iso(v.dateCreated)
            with contextlib.suppress(Exception):
                entry['description'] = v.description or None
            out.append(entry)
    out.sort(key=lambda e: e.get('version', 0), reverse=True)
    return {'document': doc.name, 'latest_version': getattr(df, 'latestVersionNumber', None),
            'count': len(out), 'versions': out}


def op_share_link(app, p):
    """Get (or create, when create=True) a shareable link for the active saved
    document. Returns the URL; sharing publishes the document to anyone with the
    link, so create=True should follow explicit user consent."""
    doc, df = _active_datafile(app)
    link = getattr(df, 'sharedLink', None)
    if link is None:
        raise RuntimeError('This Fusion build exposes no shared-link API.')
    out = {'document': doc.name}
    with contextlib.suppress(Exception):
        out['is_shared'] = bool(link.isShared)
    if p.get('create') and not out.get('is_shared', False):
        for attr in ('isShared',):
            with contextlib.suppress(Exception):
                setattr(link, attr, True)
                out['is_shared'] = True
    for attr, key in (('linkURL', 'url'), ('isPasswordRequired', 'password_required')):
        with contextlib.suppress(Exception):
            out[key] = getattr(link, attr)
    if not out.get('url'):
        out.pop('url', None)  # linkURL is '' while unshared — drop the noise
        if out.get('is_shared'):
            out['note'] = 'Shared, but this Fusion build did not report the link URL.'
        else:
            out['note'] = 'Not shared yet. Call share_link(create=true) to publish a link.'
    return out


# --------------------------------------------------------------------------- #
# Viewport annotations (custom graphics overlay)
# --------------------------------------------------------------------------- #
_annotation_group = None


def op_annotate(app, p):
    """Overlay labels and leader lines on the viewport (custom graphics) so a
    screenshot explains itself. texts: [{text, x, y, z, size}] (mm; size mm cap
    height, default 5). lines: [{from:[x,y,z], to:[x,y,z]}] (mm). Overlays are
    non-geometry; clear them with annotations_clear. Each call adds to the
    current overlay."""
    global _annotation_group
    root = _root(app)
    groups = getattr(root, 'customGraphicsGroups', None)
    if groups is None:
        raise RuntimeError('Custom graphics are not available in this Fusion build.')
    if _annotation_group is None:
        _annotation_group = groups.add()
    grp = _annotation_group
    n_text, n_line = 0, 0
    for t in p.get('texts') or []:
        with contextlib.suppress(Exception):
            mat = adsk.core.Matrix3D.create()
            mat.translation = adsk.core.Vector3D.create(
                float(t.get('x', 0)) * MM, float(t.get('y', 0)) * MM,
                float(t.get('z', 0)) * MM)
            size_cm = float(t.get('size', 5.0)) * MM
            grp.addText(str(t.get('text', '')), 'Arial', size_cm, mat)
            n_text += 1
    for ln in p.get('lines') or []:
        with contextlib.suppress(Exception):
            a, b = ln['from'], ln['to']
            coords = adsk.fusion.CustomGraphicsCoordinates.create([
                float(a[0]) * MM, float(a[1]) * MM, float(a[2]) * MM,
                float(b[0]) * MM, float(b[1]) * MM, float(b[2]) * MM])
            grp.addLines(coords, [0, 1], False)
            n_line += 1
    with contextlib.suppress(Exception):
        app.activeViewport.refresh()
    return {'texts_added': n_text, 'lines_added': n_line}


def op_annotations_clear(app, p):
    """Remove the viewport annotation overlay created by annotate."""
    global _annotation_group
    removed = False
    if _annotation_group is not None:
        with contextlib.suppress(Exception):
            _annotation_group.deleteMe()
            removed = True
        _annotation_group = None
    with contextlib.suppress(Exception):
        app.activeViewport.refresh()
    return {'cleared': removed}


# --------------------------------------------------------------------------- #
# Contact sets (mechanism motion respecting physical contact)
# --------------------------------------------------------------------------- #
def op_contact_set(app, p):
    """Create a contact set so driven joints respect physical contact instead of
    parts passing through each other. tokens: occurrence/body tokens (2+). Or
    action="all"/"none" to toggle global all-contact. Pairs with drive_joint +
    interference for mechanism checks."""
    design = _design(app)
    sets = getattr(design, 'contactSets', None)
    if sets is None:
        raise RuntimeError('Contact sets are not available in this Fusion build.')
    action = (p.get('action') or 'add').lower()
    if action in ('all', 'none'):
        enabled = action == 'all'
        for attr in ('isContactSetsEnabled', 'contactSetsEnabled', 'allContactEnabled'):
            if hasattr(type(design), attr) or hasattr(design, attr):
                with contextlib.suppress(Exception):
                    setattr(design, attr, enabled)
                    return {'all_contact': enabled}
        raise RuntimeError('This Fusion build exposes no global all-contact toggle; '
                           'create explicit contact sets instead.')
    tokens = p.get('tokens') or []
    if len(tokens) < 2:
        raise ValueError('contact_set needs 2+ occurrence/body tokens')
    # ContactSets.add takes a plain array of Occurrence/BRepBody objects, not
    # an ObjectCollection — the SWIG vector typemap rejects the proxy.
    cs = sets.add([_registry.get(t) for t in tokens])
    if p.get('name'):
        with contextlib.suppress(Exception):
            cs.name = p['name']
    return {'contact_set': _registry.add('cts', cs),
            'name': getattr(cs, 'name', None), 'members': len(tokens)}


# --------------------------------------------------------------------------- #
# Batch — many operations in ONE main-thread dispatch / round-trip
# --------------------------------------------------------------------------- #
_PATH_PART = re.compile(r'\.([A-Za-z_]\w*)|\[(-?\d+)\]')


def _resolve_ref(ref, results):
    """Resolve a "$alias.key[0].key2" reference against earlier batch results.
    Supports negative list indices ([-1]). Raises on any unparseable segment
    instead of silently skipping it, so typos surface at the reference."""
    body = ref[1:]
    m = re.match(r'[A-Za-z_]\w*', body)
    if not m:
        raise ValueError('Bad batch reference: %r' % ref)
    alias = m.group(0)
    if alias not in results:
        raise KeyError('Batch reference to unknown alias %r in %r' % (alias, ref))
    value = results[alias]
    rest = body[m.end():]
    cursor = 0
    for part in _PATH_PART.finditer(rest):
        # finditer skips non-matching text; require the path be consumed
        # contiguously so a bad segment (e.g. "(0)") raises here, not later.
        if part.start() != cursor:
            raise ValueError('Unparseable segment in batch reference %r near %r'
                             % (ref, rest[cursor:]))
        cursor = part.end()
        key, idx = part.group(1), part.group(2)
        value = value[key] if key is not None else value[int(idx)]
    if cursor != len(rest):
        raise ValueError('Unparseable trailing segment in batch reference %r: %r'
                         % (ref, rest[cursor:]))
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
    'electronics_info': op_electronics_info,
    'electronics_components': op_electronics_components,
    'electronics_nets': op_electronics_nets,
    'electronics_layers': op_electronics_layers,
    'electronics_library': op_electronics_library,
    'electronics_export': op_electronics_export,
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
    'run_fusion_code': op_run_code,  # alias: matches the standalone tool name in batch
    'reset_registry': op_reset_registry,
    'mesh_compare': op_mesh_compare,
    'fold': op_fold,
    'join_by_bend': op_join_by_bend,
    'sketch_blend_curve': op_sketch_blend_curve,
    'auto_constrain': op_auto_constrain,
    'thread_types': op_thread_types,
    'selection_filter': op_selection_filter,
    'configurations': op_configurations,
    'api_introspect': op_api_introspect,
    'as_built_joint': op_as_built_joint,
    'joint_origin': op_joint_origin,
    'list_materials': op_list_materials,
    'list_appearances': op_list_appearances,
    'insert_fastener': op_insert_fastener,
    'data_folders': op_data_folders,
    'version_history': op_version_history,
    'share_link': op_share_link,
    'annotate': op_annotate,
    'annotations_clear': op_annotations_clear,
    'contact_set': op_contact_set,
    # v1.10.0: July 2026 GA wave + diagnostics
    'timeline_builder': op_timeline_builder,
    'corner_closure': op_corner_closure,
    'cam_setup': op_cam_setup,
    'cam_suppress': op_cam_suppress,
    'design_diagnostics': op_design_diagnostics,
    'sketch_status': op_sketch_status,
    'create_appearance': op_create_appearance,
    # v1.11.0: scan + photo wave
    'mesh_export': op_mesh_export,
    'face_groups': op_face_groups,
    'mesh_repair': op_mesh_repair,
    'mesh_smooth': op_mesh_smooth,
    'mesh_shell': op_mesh_shell,
    'mesh_separate': op_mesh_separate,
    'canvas_calibrate': op_canvas_calibrate,
    'canvas_list': op_canvas_list,
    'canvas_update': op_canvas_update,
    'canvas_delete': op_canvas_delete,
    'import_svg': op_import_svg,
    # v1.11.1: user-facing update popups
    'show_message': op_show_message,
    'notify_update': op_notify_update,
    # v1.12.0: workshop wave
    'loft_from_sections': op_loft_from_sections,
    'silhouette': op_silhouette,
    'sketch_doctor': op_sketch_doctor,
    'drawing_table': op_drawing_table,
    'fastener_update_size': op_fastener_update_size,
}


def classify_error(exc):
    """Map an exception to a stable {code, retriable} for the wire, so the model
    can branch (retry vs re-query vs give up) without string-matching messages."""
    msg = str(exc).lower()
    if isinstance(exc, KeyError):
        # registry.get raises KeyError for unknown/stale tokens.
        return 'stale_token', False
    if isinstance(exc, (ValueError, TypeError)):
        return 'bad_params', False
    if 'no active fusion design' in msg or 'no document' in msg or \
            'switch to the design' in msg:
        return 'no_design', True
    if ('not available in this fusion' in msg or 'needs fusion' in msg
            or 'preview api' in msg or 'this fusion version' in msg
            or 'this fusion build' in msg):
        # 'preview api' (not bare 'preview') so an error echoing a user string
        # like the path 'C:/renders/preview.step' isn't misfiled as unsupported.
        return 'unsupported', False
    return 'fusion_error', False


def _doc_key(app):
    """Session identity of the active document: (creationId, cloud id).
    creationId (when the build exposes it) is constant for the life of a
    document — it survives the first save (None -> cloud id is the SAME doc,
    must not reset) and differs for File > New (so a saved -> unsaved switch IS
    a switch). The cloud id disambiguates open copies, which share a
    creationId. Deliberately NOT keyed on the document name: a name
    appears/changes on first save. (None, None) = no usable identity."""
    try:
        doc = app.activeDocument
    except Exception:
        return (None, None)
    if doc is None:
        return (None, None)
    cid = None
    with contextlib.suppress(Exception):
        cid = doc.creationId or None
    cloud_id = None
    with contextlib.suppress(Exception):
        df = doc.dataFile
        if df is not None:
            cloud_id = df.id
    return (cid, cloud_id)


def _doc_switched(prev_key, new_key):
    """Whether new_key identifies a DIFFERENT document than prev_key.
    creationId is authoritative when both sides have it (the cloud id only
    splits same-creationId copies once both are saved); on old builds without
    creationId, only a transition between two distinct non-None cloud ids
    provably is a switch (None -> id is a first save, same document)."""
    prev_cid, prev_cloud = prev_key
    cid, cloud_id = new_key
    if cid is not None and prev_cid is not None:
        return cid != prev_cid or (
            cloud_id is not None and prev_cloud is not None
            and cloud_id != prev_cloud)
    return (cloud_id is not None and prev_cloud is not None
            and cloud_id != prev_cloud)


def _drop_tokens_on_doc_switch(app):
    """If the active document changed since the last dispatch, invalidate all
    per-document state so stale tokens raise the helpful KeyError instead of
    silently resolving against (and mutating) the previous, still-open
    document in the background."""
    global _active_doc_key, _annotation_group, _isolate_stash
    key = _doc_key(app)
    if key == (None, None):
        return  # no identity at all: can't tell, so leave state alone
    if _doc_switched(_active_doc_key, key):
        _registry.reset()
        _state_cache.clear()
        _annotation_group = None  # the overlay belonged to the previous document
        # The isolate stash holds live proxies of the PREVIOUS document —
        # restoring them here would mutate a background document, so drop it
        # (that document keeps its current visibility state).
        _isolate_stash = None
        _code_store.clear()  # run_code store/fetch must not leak live doc-A objects
    _active_doc_key = key


def _drop_old_doc_state_after_op(app, pre_tokens, pre_code_keys):
    """If the HANDLER itself changed the active document (documents.add inside
    run_code, open_document, the headless-drawing fallback), drop the previous
    document's state immediately — but KEEP tokens and code-store entries
    minted DURING this dispatch, which belong to the newly active document.
    Without this, the next dispatch would see the key change and wipe the very
    tokens the op just returned."""
    global _active_doc_key, _annotation_group, _isolate_stash
    key = _doc_key(app)
    if key == (None, None):
        return
    if _doc_switched(_active_doc_key, key):
        for tok in pre_tokens:
            _registry.remove(tok)
        for k in [k for k in _code_store if k in pre_code_keys]:
            del _code_store[k]
        _state_cache.clear()
        _annotation_group = None
        _isolate_stash = None
    _active_doc_key = key


def dispatch(app, op, params):
    global _mutation_gen
    handler = DISPATCH.get(op)
    if handler is None:
        raise RuntimeError('Unknown op: %r (available: %s)'
                           % (op, ', '.join(sorted(DISPATCH))))
    if app is not None:
        _drop_tokens_on_doc_switch(app)
        pre_tokens = _registry.tokens()
        pre_code_keys = set(_code_store)
    try:
        result = handler(app, params or {})
    finally:
        # In a finally so a handler that RAISES after switching documents
        # (e.g. a failing run_code that did documents.add) still invalidates
        # the old document's state. op_batch catches sub-op exceptions, so
        # without this the batch's outer dispatch would advance the doc key
        # while stale mid-batch tokens stayed alive — resolvable against the
        # background document forever.
        if app is not None:
            _drop_old_doc_state_after_op(app, pre_tokens, pre_code_keys)
    # Any mutating op invalidates the cached read-only views. timeline rollback
    # mutates geometry but is otherwise read-only-shaped, so force it here.
    mutating = op not in _READ_ONLY_OPS or (
        op == 'timeline'
        and str((params or {}).get('action') or '').lower() == 'rollback')
    if mutating:
        _mutation_gen += 1
        _state_cache.clear()
    return result
