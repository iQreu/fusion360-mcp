"""Operation handlers for FusionMCP.

Every handler runs on Fusion's main thread (dispatched from bridge.py) and has
the signature `handler(app, params: dict) -> json-serialisable dict`.

Length convention: all length inputs/outputs across the wire are in MILLIMETRES.
Fusion's internal unit is centimetres, so we multiply by MM on the way in and
divide by MM on the way out. Angles are in DEGREES on the wire.
"""
import base64
import io
import contextlib
import math
import re
import traceback

import adsk.core
import adsk.fusion

from registry import Registry

MM = 0.1  # 1 mm = 0.1 cm (Fusion internal length unit)

_registry = Registry()

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
    return {'pong': True, 'version': '1.0.0'}


def op_get_state(app, p):
    design = _design(app)
    root = design.rootComponent
    # physicalProperties.volume triggers a mass-properties solve per body, which
    # is the slowest part of get_state on big models — opt-in only.
    include_mass = bool(p.get('include_mass_props', False))

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
    return {
        'document': app.activeDocument.name if app.activeDocument else None,
        'length_units': design.unitsManager.defaultLengthUnits,
        'wire_length_unit': 'mm',
        'design_type': 'direct' if direct else 'parametric',
        'component_count': design.allComponents.count,
        'bodies': bodies,
        'sketches': sketches,
        'parameters': params,
    }


def op_query_entities(app, p):
    kind = p.get('kind', 'bodies')
    target = p.get('target')
    # Face area / profile area are solves; skip unless requested.
    include_mass = bool(p.get('include_mass_props', False))
    root = _root(app)
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
    else:
        raise ValueError('kind must be bodies|sketches|profiles|faces|edges, got %r' % kind)

    return {'kind': kind, 'count': len(out), 'entities': out}


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
    sk.sketchCurves.sketchLines.addTwoPointRectangle(
        _pt(p['x1'], p['y1']), _pt(p['x2'], p['y2']))
    return _profiles_summary(sk)


def op_sketch_circle(app, p):
    sk = _registry.get(p['sketch'])
    sk.sketchCurves.sketchCircles.addByCenterRadius(
        _pt(p['cx'], p['cy']), p['r'] * MM)
    return _profiles_summary(sk)


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
    'get_state': op_get_state,
    'query_entities': op_query_entities,
    'create_sketch': op_create_sketch,
    'sketch_rectangle': op_sketch_rectangle,
    'sketch_circle': op_sketch_circle,
    'sketch_line': op_sketch_line,
    'sketch_arc': op_sketch_arc,
    'sketch_polygon': op_sketch_polygon,
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
    handler = DISPATCH.get(op)
    if handler is None:
        raise RuntimeError('Unknown op: %r (available: %s)'
                           % (op, ', '.join(sorted(DISPATCH))))
    return handler(app, params or {})
