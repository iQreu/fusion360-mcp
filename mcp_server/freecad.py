"""FreeCAD headless bridge: FEM analysis, CAD inspection and conversion.

Runs FreeCAD as a subprocess (`freecadcmd.exe`) — FreeCAD bundles its own
Python (3.11 conda) so nothing here imports into the server process. Every
call generates a script into a temp dir, points it at a params.json, runs
freecadcmd and reads a result.json back; freecadcmd's noisy stdout (gmsh,
CalculiX, recompute ticks) is only used for error reporting.

Verified live on FreeCAD 1.1.3 / Windows: Part, Sketcher, ObjectsFem,
femmesh.gmshtools, femtools.ccxtools (bundled gmsh.exe + ccx.exe), TechDraw,
Import (STEP/IGES), Mesh/MeshPart all import headlessly; a full scripted
FEM (gmsh mesh -> CalculiX -> vonMises/displacement readback) works.

Units: FreeCAD's Part/FEM world is mm; with E given in MPa the stresses come
back in MPa and displacements in mm — no conversion anywhere.

Override discovery with FUSION_MCP_FREECAD (full path to freecadcmd).
"""
import glob
import json
import os
import shutil
import subprocess
import tempfile

# Linear-elastic material presets. E [MPa], nu, density [kg/m^3],
# yield strength [MPa] (conservative typical values, not certified data).
MATERIALS = {
    'steel': {'name': 'Steel S235', 'E': 210000.0, 'nu': 0.30,
              'density': 7850.0, 'yield': 235.0},
    'stainless': {'name': 'Stainless 304', 'E': 193000.0, 'nu': 0.29,
                  'density': 8000.0, 'yield': 215.0},
    'aluminum': {'name': 'Aluminum 6061-T6', 'E': 68900.0, 'nu': 0.33,
                 'density': 2700.0, 'yield': 276.0},
    'brass': {'name': 'Brass CuZn37', 'E': 97000.0, 'nu': 0.31,
              'density': 8500.0, 'yield': 200.0},
    'titanium': {'name': 'Ti-6Al-4V', 'E': 113800.0, 'nu': 0.34,
                 'density': 4430.0, 'yield': 880.0},
    'pla': {'name': 'PLA (printed)', 'E': 3500.0, 'nu': 0.36,
            'density': 1240.0, 'yield': 50.0},
    'petg': {'name': 'PETG (printed)', 'E': 2100.0, 'nu': 0.40,
             'density': 1270.0, 'yield': 47.0},
    'abs': {'name': 'ABS (printed)', 'E': 2300.0, 'nu': 0.35,
            'density': 1040.0, 'yield': 40.0},
    'nylon': {'name': 'PA12 (printed)', 'E': 1700.0, 'nu': 0.39,
              'density': 1010.0, 'yield': 45.0},
    'pc': {'name': 'Polycarbonate', 'E': 2300.0, 'nu': 0.37,
           'density': 1200.0, 'yield': 62.0},
}

_PRINTED = ('pla', 'petg', 'abs', 'nylon', 'pc')

_SOLID_EXTS = ('.step', '.stp', '.iges', '.igs', '.brep', '.brp', '.fcstd')
_MESH_EXTS = ('.stl', '.obj', '.3mf', '.ply', '.off')


def detect():
    """Full path to freecadcmd, or None. FUSION_MCP_FREECAD (file path) wins,
    then PATH, then the standard Windows/Linux/macOS install locations
    (newest version first)."""
    override = os.environ.get('FUSION_MCP_FREECAD')
    if override and os.path.isfile(override):
        return override
    for exe in ('freecadcmd', 'FreeCADCmd', 'freecadcmd.exe'):
        path = shutil.which(exe)
        if path:
            return path
    patterns = (
        r'C:\Program Files\FreeCAD*\bin\freecadcmd.exe',
        '/usr/bin/freecadcmd', '/usr/local/bin/freecadcmd',
        '/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd',
    )
    hits = []
    for pattern in patterns:
        hits.extend(glob.glob(pattern))
    return sorted(hits)[-1] if hits else None


def _require():
    path = detect()
    if not path:
        raise RuntimeError(
            'FreeCAD not found (looked for freecadcmd on PATH and in '
            'Program Files). Install FreeCAD 1.x from freecad.org or set '
            'FUSION_MCP_FREECAD to the freecadcmd executable path.')
    return path


# Prelude shared by every generated script: read params.json, run the body,
# always write result.json (ok or error) so the server never has to parse
# freecadcmd's noisy stdout.
_PRELUDE = '''\
import json, os, sys, traceback
_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "params.json"), "r", encoding="utf-8") as fh:
    P = json.load(fh)
def _finish(payload):
    with open(os.path.join(_here, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str)
try:
    import FreeCAD
'''

_EPILOGUE = '''\
except Exception:
    _finish({"error": traceback.format_exc(limit=8)})
'''

# Body snippet reused by inspect/fem/convert: load any supported file into
# (doc, solids, meshes). FCStd opens as a document; STEP/IGES/BREP read as a
# shape; STL/OBJ/... read as a Mesh (optionally solidified by the caller).
_LOAD_SNIPPET = '''\
    import Part
    doc = FreeCAD.newDocument("fcmcp")
    path = P["path"]
    ext = os.path.splitext(path)[1].lower()
    solids, mesh = [], None
    if ext == ".fcstd":
        doc = FreeCAD.openDocument(path)
        for obj in doc.Objects:
            shape = getattr(obj, "Shape", None)
            if shape is not None and getattr(shape, "Solids", None):
                solids.extend(shape.Solids)
    elif ext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
        shape = Part.Shape()
        shape.read(path)
        solids = list(shape.Solids) or [shape]
    else:
        import Mesh
        mesh = Mesh.Mesh(path)
'''


def _run(freecadcmd, body, params, timeout, keep=()):
    """Write prelude+body+epilogue and params.json to a temp dir, run
    freecadcmd, return the parsed result.json. `keep` names params whose
    file outputs live outside the temp dir (nothing to do here — the body
    writes them straight to the user path)."""
    tmpdir = tempfile.mkdtemp(prefix='fcmcp-')
    try:
        script = os.path.join(tmpdir, 'job.py')
        # Body templates are pre-indented one level (inside the prelude try).
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(_PRELUDE + body + _EPILOGUE)
        with open(os.path.join(tmpdir, 'params.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump(params, fh)
        try:
            proc = subprocess.run([freecadcmd, script], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('FreeCAD timed out after %ss — raise timeout '
                               'or coarsen the FEM mesh (mesh_max_mm).'
                               % timeout)
        result_path = os.path.join(tmpdir, 'result.json')
        if not os.path.isfile(result_path):
            tail = ((proc.stderr or '') + '\n' + (proc.stdout or '')).strip()
            raise RuntimeError('FreeCAD produced no result (exit %s): %s'
                               % (proc.returncode, tail[-800:]))
        with open(result_path, 'r', encoding='utf-8') as fh:
            result = json.load(fh)
        if 'error' in result:
            raise RuntimeError('FreeCAD script failed:\n%s'
                               % result['error'][-1200:])
        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def info():
    """Installation report: freecadcmd path and version string."""
    path = detect()
    if not path:
        return {'installed': False,
                'hint': 'Install FreeCAD 1.x from freecad.org or set '
                        'FUSION_MCP_FREECAD.'}
    out = {'installed': True, 'path': path}
    try:
        proc = subprocess.run([path, '--version'], capture_output=True,
                              text=True, timeout=60)
        first = ((proc.stdout or '') + (proc.stderr or '')).strip()
        out['version'] = first.splitlines()[0] if first else 'unknown'
    except Exception as exc:  # noqa: BLE001
        out['version'] = 'unknown (%s)' % exc
    bin_dir = os.path.dirname(path)
    for tool_name, exe in (('gmsh', 'gmsh.exe'), ('calculix', 'ccx.exe')):
        candidate = os.path.join(bin_dir, exe)
        alt = shutil.which(exe.replace('.exe', ''))
        out[tool_name] = candidate if os.path.isfile(candidate) else alt
    out['fem_ready'] = bool(out.get('gmsh') and out.get('calculix'))
    out['materials'] = sorted(MATERIALS)
    return out


_INSPECT_BODY = _LOAD_SNIPPET + '''\
    out = {"file": path}
    if mesh is not None:
        out["kind"] = "mesh"
        out["triangles"] = mesh.CountFacets
        out["points"] = mesh.CountPoints
        bb = mesh.BoundBox
        out["bbox_mm"] = [round(v, 3) for v in
                          (bb.XLength, bb.YLength, bb.ZLength)]
        out["is_solid"] = bool(mesh.isSolid())
        out["has_self_intersections"] = bool(mesh.hasSelfIntersections())
        out["volume_mm3"] = round(mesh.Volume, 2) if mesh.isSolid() else None
        _finish(out)
        sys.exit(0)
    out["kind"] = "solid"
    out["solids"] = len(solids)
    report = []
    for si, solid in enumerate(solids[:P["max_solids"]]):
        bb = solid.BoundBox
        entry = {
            "solid": si + 1,
            "valid": bool(solid.isValid()),
            "closed": bool(getattr(solid, "isClosed", lambda: True)()),
            "volume_mm3": round(solid.Volume, 2),
            "area_mm2": round(solid.Area, 2),
            "bbox_mm": [round(v, 3) for v in
                        (bb.XLength, bb.YLength, bb.ZLength)],
            "bbox_min": [round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin)],
            "bbox_max": [round(v, 3) for v in (bb.XMax, bb.YMax, bb.ZMax)],
            "faces": [],
        }
        for fi, face in enumerate(solid.Faces[:P["max_faces"]]):
            surf = face.Surface
            stype = type(surf).__name__
            c = face.CenterOfMass
            frec = {"face": "Face%d" % (fi + 1), "type": stype,
                    "area_mm2": round(face.Area, 2),
                    "center": [round(c.x, 2), round(c.y, 2), round(c.z, 2)]}
            try:
                if stype == "Plane":
                    n = face.normalAt(0, 0)
                    frec["normal"] = [round(n.x, 3), round(n.y, 3),
                                     round(n.z, 3)]
                elif stype == "Cylinder":
                    frec["radius_mm"] = round(surf.Radius, 3)
                    a = surf.Axis
                    frec["axis"] = [round(a.x, 3), round(a.y, 3),
                                    round(a.z, 3)]
                elif stype == "Sphere":
                    frec["radius_mm"] = round(surf.Radius, 3)
                elif stype in ("Cone", "Toroid", "Torus"):
                    for attr, key in (("Radius", "radius_mm"),
                                      ("MajorRadius", "major_radius_mm"),
                                      ("MinorRadius", "minor_radius_mm"),
                                      ("SemiAngle", "half_angle_rad")):
                        if hasattr(surf, attr):
                            frec[key] = round(float(getattr(surf, attr)), 3)
            except Exception:
                pass
            entry["faces"].append(frec)
        if len(solid.Faces) > P["max_faces"]:
            entry["faces_truncated"] = len(solid.Faces)
        report.append(entry)
    out["report"] = report
    _finish(out)
'''


def inspect(path, max_faces=120, max_solids=8, timeout=300):
    """Geometry census of a CAD file (STEP/IGES/BREP/FCStd) or mesh
    (STL/OBJ/...): per-solid validity, volume, bbox, and per-face surface
    type/area/center (+plane normals, cylinder radius/axis) so faces can be
    picked for freecad_fem by name or by geometry."""
    if not os.path.isfile(path):
        raise RuntimeError('File not found: %r' % path)
    return _run(_require(), _INSPECT_BODY,
                {'path': os.path.abspath(path), 'max_faces': int(max_faces),
                 'max_solids': int(max_solids)}, timeout)


_FEM_BODY = _LOAD_SNIPPET + '''\
    import ObjectsFem
    from femmesh.gmshtools import GmshTools
    from femtools import ccxtools

    warnings = list(P.get("warnings") or [])
    if mesh is not None:
        if mesh.CountFacets > P["max_mesh_faces"]:
            raise RuntimeError(
                "Mesh has %d triangles — too many to solidify for FEM. "
                "Reduce it first (mesh_reduce / scan tools) to under %d."
                % (mesh.CountFacets, P["max_mesh_faces"]))
        shape = Part.Shape()
        shape.makeShapeFromMesh(mesh.Topology, 0.05)
        solid = Part.makeSolid(shape)
        try:
            solid = solid.removeSplitter()
        except Exception:
            pass
        warnings.append("Input was a mesh: FEM ran on a faceted solid "
                        "(one face per triangle) — prefer STEP exports.")
    else:
        if not solids:
            raise RuntimeError("No solid found in %s" % path)
        solid = solids[0]
        if len(solids) > 1:
            solid = max(solids, key=lambda s: s.Volume)
            warnings.append("File has %d solids — analyzed the largest "
                            "by volume." % len(solids))

    part = doc.addObject("Part::Feature", "Target")
    part.Shape = solid
    doc.recompute()

    bb = solid.BoundBox
    tol = max(bb.DiagonalLength * 1e-4, 0.01)
    KEYWORD_AXIS = {"xmin": (0, bb.XMin, -1), "xmax": (0, bb.XMax, 1),
                    "ymin": (1, bb.YMin, -1), "ymax": (1, bb.YMax, 1),
                    "zmin": (2, bb.ZMin, -1), "zmax": (2, bb.ZMax, 1)}

    def pick(specs, what):
        names = []
        for spec in specs:
            spec = str(spec).strip()
            low = spec.lower()
            if low in KEYWORD_AXIS:
                axis, bound, sign = KEYWORD_AXIS[low]
                for i, face in enumerate(part.Shape.Faces):
                    c = face.CenterOfMass
                    coord = (c.x, c.y, c.z)[axis]
                    if abs(coord - bound) > tol:
                        continue
                    try:
                        n = face.normalAt(0, 0)
                        ncomp = (n.x, n.y, n.z)[axis]
                    except Exception:
                        ncomp = sign
                    if ncomp * sign > 0.7:
                        names.append("Face%d" % (i + 1))
            elif low.startswith("face"):
                idx = int(low[4:])
                if not 1 <= idx <= len(part.Shape.Faces):
                    raise RuntimeError(
                        "%s: Face%d out of range (solid has %d faces)"
                        % (what, idx, len(part.Shape.Faces)))
                names.append("Face%d" % idx)
            else:
                raise RuntimeError(
                    "%s: unknown face spec %r — use FaceN (see "
                    "freecad_inspect) or xmin/xmax/ymin/ymax/zmin/zmax"
                    % (what, spec))
        names = sorted(set(names), key=lambda s: int(s[4:]))
        if not names:
            raise RuntimeError("%s: no faces matched %r" % (what, specs))
        return names

    analysis = ObjectsFem.makeAnalysis(doc, "Analysis")
    solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, "Solver")
    solver.GeometricalNonlinearity = "linear"
    solver.ThermoMechSteadyState = False
    solver.MatrixSolverType = "default"
    solver.IterationsControlParameterTimeUse = False
    analysis.addObject(solver)

    matdef = P["material"]
    mat = ObjectsFem.makeMaterialSolid(doc, "Material")
    m = dict(mat.Material)
    m["Name"] = matdef["name"]
    m["YoungsModulus"] = "%s MPa" % matdef["E"]
    m["PoissonRatio"] = "%s" % matdef["nu"]
    m["Density"] = "%s kg/m^3" % matdef["density"]
    mat.Material = m
    analysis.addObject(mat)

    fixed_names = pick(P["fixed"], "fixed")
    fixed = ObjectsFem.makeConstraintFixed(doc, "Fixed")
    fixed.References = [(part, n) for n in fixed_names]
    analysis.addObject(fixed)

    applied = []
    for li, load in enumerate(P["loads"]):
        load_names = pick(load["faces"], "load %d" % (li + 1))
        if load.get("pressure_mpa") is not None:
            con = ObjectsFem.makeConstraintPressure(doc, "Load%d" % (li + 1))
            con.References = [(part, n) for n in load_names]
            con.Pressure = "%s MPa" % load["pressure_mpa"]
            con.Reversed = bool(load.get("pull", False))
            applied.append({"faces": load_names,
                            "pressure_mpa": load["pressure_mpa"]})
        else:
            con = ObjectsFem.makeConstraintForce(doc, "Load%d" % (li + 1))
            con.References = [(part, n) for n in load_names]
            con.Force = "%s N" % load["force_n"]
            con.Reversed = not bool(load.get("pull", False))
            applied.append({"faces": load_names, "force_n": load["force_n"]})
        analysis.addObject(con)

    if P.get("gravity"):
        weight = ObjectsFem.makeConstraintSelfWeight(doc, "SelfWeight")
        analysis.addObject(weight)

    femmesh = ObjectsFem.makeMeshGmsh(doc, "Mesh")
    femmesh.Shape = part
    femmesh.CharacteristicLengthMax = "%s mm" % P["mesh_max_mm"]
    analysis.addObject(femmesh)
    doc.recompute()

    err = GmshTools(femmesh).create_mesh()
    if err:
        raise RuntimeError("gmsh meshing failed: %s" % err)

    fea = ccxtools.FemToolsCcx(analysis, solver)
    fea.purge_results()
    fea.run()

    res = None
    for obj in doc.Objects:
        if obj.isDerivedFrom("Fem::FemResultObject"):
            res = obj
            break
    if res is None:
        raise RuntimeError("CalculiX produced no result object")

    vm = sorted(res.vonMises)
    disp = sorted(res.DisplacementLengths)
    if not vm:
        raise RuntimeError("Result has no vonMises stresses")

    def pct(sorted_vals, q):
        return sorted_vals[min(len(sorted_vals) - 1,
                               int(q * (len(sorted_vals) - 1)))]

    max_vm = vm[-1]
    out = {
        "material": matdef["name"],
        "mass_g": round(solid.Volume * matdef["density"] * 1e-6, 2),
        "nodes": len(vm),
        "fixed_faces": fixed_names,
        "loads": applied,
        "von_mises_max_mpa": round(max_vm, 2),
        "von_mises_p95_mpa": round(pct(vm, 0.95), 2),
        "displacement_max_mm": round(disp[-1], 4),
        "displacement_p95_mm": round(pct(disp, 0.95), 4),
        "yield_mpa": matdef["yield"],
        "safety_factor": round(matdef["yield"] / max_vm, 2)
                         if max_vm > 1e-9 else None,
        "mesh_max_mm": P["mesh_max_mm"],
        "warnings": warnings,
    }
    _finish(out)
'''


def fem_analyze(path, fixed, loads, material='steel', mesh_max_mm=0.0,
                gravity=False, E=0.0, nu=0.0, density=0.0,
                yield_mpa=0.0, max_mesh_faces=50000, timeout=900):
    """Linear static FEM on a CAD file via FreeCAD + gmsh + CalculiX.

    fixed: list of face specs — 'FaceN' (indices from freecad_inspect) or the
    keywords xmin/xmax/ymin/ymax/zmin/zmax (all planar faces on that bbox
    side). loads: [{'faces': [...], 'force_n': 500}] (along face normal,
    pushing by default; 'pull': True flips) or {'faces': [...],
    'pressure_mpa': 2.5}. material: preset key or 'custom' with E/nu/density/
    yield_mpa overrides. mesh_max_mm 0 = auto (bbox diagonal / 12).
    Returns von Mises max/p95 [MPa], displacement [mm], mass and a safety
    factor vs the material's yield strength."""
    if not os.path.isfile(path):
        raise RuntimeError('File not found: %r' % path)
    if not fixed:
        raise RuntimeError("fixed faces are required — e.g. ['zmin'] or "
                           "['Face3'] (freecad_inspect lists faces)")
    if not loads:
        raise RuntimeError("at least one load is required — e.g. "
                           "[{'faces': ['zmax'], 'force_n': 200}]")
    warnings = []
    key = (material or 'steel').lower()
    if key == 'custom':
        if not (E and nu and density):
            raise RuntimeError("material='custom' needs E (MPa), nu and "
                               'density (kg/m^3)')
        matdef = {'name': 'Custom', 'E': float(E), 'nu': float(nu),
                  'density': float(density),
                  'yield': float(yield_mpa) or 1.0}
        if not yield_mpa:
            warnings.append('No yield_mpa given for the custom material — '
                            'safety_factor is meaningless.')
    else:
        if key not in MATERIALS:
            raise RuntimeError('Unknown material %r — one of %s or "custom"'
                               % (material, ', '.join(sorted(MATERIALS))))
        matdef = dict(MATERIALS[key])
        if E:
            matdef['E'] = float(E)
        if yield_mpa:
            matdef['yield'] = float(yield_mpa)
    if key in _PRINTED:
        warnings.append(
            'Printed-polymer preset: FDM parts are anisotropic — expect '
            '~50-80% of this strength across layer lines; the linear model '
            'also ignores creep.')
    loads_norm = []
    for load in loads:
        if not isinstance(load, dict) or 'faces' not in load:
            raise RuntimeError("each load needs {'faces': [...], 'force_n' "
                               "or 'pressure_mpa': value}")
        if load.get('force_n') is None and load.get('pressure_mpa') is None:
            raise RuntimeError('load %r has neither force_n nor pressure_mpa'
                               % load)
        loads_norm.append({'faces': list(load['faces']),
                           'force_n': load.get('force_n'),
                           'pressure_mpa': load.get('pressure_mpa'),
                           'pull': bool(load.get('pull', False))})
    params = {
        'path': os.path.abspath(path),
        'fixed': list(fixed),
        'loads': loads_norm,
        'material': matdef,
        'mesh_max_mm': float(mesh_max_mm) or 0.0,
        'gravity': bool(gravity),
        'max_mesh_faces': int(max_mesh_faces),
        'warnings': warnings,
    }
    if not params['mesh_max_mm']:
        # Resolved in-script would need the bbox; do a cheap default here:
        # the script gets 0 and derives bbox/12 itself.
        pass
    body = _FEM_BODY.replace(
        'P["mesh_max_mm"]',
        '(P["mesh_max_mm"] or round(solid.BoundBox.DiagonalLength / 12.0, 2))')
    result = _run(_require(), body, params, timeout)
    if key in _PRINTED and result.get('safety_factor'):
        result['safety_factor_printed'] = round(
            result['safety_factor'] * 0.6, 2)
    return result


_CONVERT_BODY = _LOAD_SNIPPET + '''\
    out_path = P["out_path"]
    oext = os.path.splitext(out_path)[1].lower()
    out = {"out": out_path}
    if oext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
        if mesh is not None:
            if mesh.CountFacets > P["max_mesh_faces"]:
                raise RuntimeError(
                    "Mesh has %d triangles — solidifying that many makes an "
                    "unusable faceted STEP. Reduce below %d first."
                    % (mesh.CountFacets, P["max_mesh_faces"]))
            shape = Part.Shape()
            shape.makeShapeFromMesh(mesh.Topology, P["mesh_tolerance"])
            shape = Part.makeSolid(shape)
            try:
                shape = shape.removeSplitter()
            except Exception:
                pass
            out["note"] = ("Faceted solid (one face per triangle) — fine as "
                           "a reference body, not a parametric model.")
            solids = [shape]
        compound = solids[0] if len(solids) == 1 else Part.makeCompound(solids)
        if oext in (".brep", ".brp"):
            compound.exportBrep(out_path)
        elif oext in (".iges", ".igs"):
            compound.exportIges(out_path)
        else:
            compound.exportStep(out_path)
        out["solids"] = len(solids)
        out["volume_mm3"] = round(sum(s.Volume for s in solids), 2)
    elif oext in (".stl", ".obj", ".ply", ".3mf", ".off"):
        import Mesh
        if mesh is None:
            import MeshPart
            shape = solids[0] if len(solids) == 1 else \\
                Part.makeCompound(solids)
            mesh = MeshPart.meshFromShape(
                Shape=shape, LinearDeflection=P["linear_deflection"],
                AngularDeflection=P["angular_deflection"], Relative=False)
        mesh.write(out_path)
        out["triangles"] = mesh.CountFacets
    else:
        raise RuntimeError("Unsupported output format %r" % oext)
    if not os.path.isfile(out_path):
        raise RuntimeError("Export produced no file at %s" % out_path)
    out["size"] = os.path.getsize(out_path)
    _finish(out)
'''


def convert(in_path, out_path, linear_deflection_mm=0.1,
            angular_deflection_deg=15.0, mesh_tolerance_mm=0.05,
            max_mesh_faces=200000, timeout=600):
    """CAD format conversion through FreeCAD/OpenCascade. Solids
    (STEP/IGES/BREP/FCStd) convert between each other and tessellate to
    STL/OBJ/PLY/3MF (deflection controls quality); meshes convert between
    mesh formats or solidify into a faceted STEP/BREP reference body."""
    if not os.path.isfile(in_path):
        raise RuntimeError('File not found: %r' % in_path)
    iext = os.path.splitext(in_path)[1].lower()
    if iext not in _SOLID_EXTS + _MESH_EXTS:
        raise RuntimeError('Unsupported input format %r' % iext)
    import math
    params = {
        'path': os.path.abspath(in_path),
        'out_path': os.path.abspath(out_path),
        'linear_deflection': float(linear_deflection_mm),
        'angular_deflection': math.radians(float(angular_deflection_deg)),
        'mesh_tolerance': float(mesh_tolerance_mm),
        'max_mesh_faces': int(max_mesh_faces),
    }
    return _run(_require(), _CONVERT_BODY, params, timeout)


_RUN_BODY = '''\
    import Part  # noqa: F401 - convenience for user scripts
    App = FreeCAD
    result = None
    _ns = {"FreeCAD": FreeCAD, "App": App, "Part": Part, "P": P,
           "result": None}
    exec(compile(P["code"], "<freecad_run>", "exec"), _ns)
    _finish({"result": _ns.get("result")})
'''


def run_script(code, timeout=600):
    """Arbitrary Python in headless FreeCAD (same trust model as
    run_fusion_code / codecad_run). The script gets FreeCAD/App/Part
    pre-imported, may import any FreeCAD module (ObjectsFem, TechDraw,
    Mesh, ...) and should assign a JSON-serializable `result`."""
    return _run(_require(), _RUN_BODY, {'code': code}, timeout)
