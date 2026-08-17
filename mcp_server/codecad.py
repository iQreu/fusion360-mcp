"""Parametric code-CAD without Fusion in the loop (build123d).

Runs a Python script against build123d (the "codecad" extras) and exports
STEP/STL — millisecond-fast parametric prototypes, bracket generators and
STEP round-trips with no Fusion add-in involved; bring the result into the
design with import_file (STEP) or import_mesh (STL).

Trust model: the script is executed with full Python access, exactly like
the run_fusion_code tool — only feed it code you (or the model on your
instruction) wrote.

Convention: the script must leave its result in a variable named `result`
(a build123d Part/Solid/Compound, or a BuildPart whose .part is taken).
`from build123d import *` is pre-applied, so scripts read like build123d
documentation examples. build123d units are millimetres.
"""
import os

try:
    import build123d
except ImportError as exc:  # pragma: no cover - exercised via hint test
    build123d = None
    _IMPORT_ERROR = str(exc)
else:
    _IMPORT_ERROR = None

_INSTALL_HINT = (
    "Code-CAD needs the optional 'codecad' dependency. Install it with: "
    "pip install -e \"mcp_server[codecad]\"  (or: pip install build123d) "
    "and restart the MCP server. NOTE: on machines with Windows Smart App "
    "Control enabled the OCP kernel DLL is blocked ('DLL load failed') — "
    "there is no workaround short of disabling SAC; use the native Fusion "
    "tools instead."
)


def _require():
    if build123d is None:
        raise RuntimeError('%s Import error: %s' % (_INSTALL_HINT, _IMPORT_ERROR))


def run(script, out_path, fmt=None):
    """Execute a build123d script and export `result` to out_path (.step or
    .stl decides the format unless fmt overrides). Returns export info plus
    volume/bounding box when available."""
    _require()
    fmt = (fmt or os.path.splitext(out_path)[1].lstrip('.')).lower()
    if fmt not in ('step', 'stl'):
        raise RuntimeError("out_path must end in .step or .stl, got %r"
                           % out_path)

    namespace = {'__name__': '__fusionmcp_codecad__'}
    exec('from build123d import *', namespace)  # noqa: S102 - documented trust model
    try:
        exec(script, namespace)  # noqa: S102
    except Exception as exc:
        raise RuntimeError('Script failed: %s: %s'
                           % (type(exc).__name__, exc))
    result = namespace.get('result')
    if result is None:
        raise RuntimeError("The script must assign its final shape to a "
                           "variable named `result` (a Part/Solid, or a "
                           "BuildPart).")
    part = getattr(result, 'part', result)  # BuildPart -> .part

    exporter = getattr(build123d, 'export_step' if fmt == 'step'
                       else 'export_stl', None)
    if exporter is None:
        raise RuntimeError('build123d.%s is missing — upgrade build123d.'
                           % ('export_step' if fmt == 'step' else 'export_stl'))
    ok = exporter(part, out_path)
    if ok is False or not os.path.isfile(out_path):
        raise RuntimeError('Export to %r failed.' % out_path)

    report = {'output': out_path, 'format': fmt,
              'note': ("Bring it into Fusion with import_file(format='step') "
                       'for a real solid, or import_mesh for the STL.'
                       if fmt == 'step' else
                       'Bring it into Fusion with import_mesh.')}
    try:
        report['volume_mm3'] = round(float(part.volume), 2)
    except Exception:  # noqa: BLE001 - shells/wires have no volume
        pass
    try:
        bb = part.bounding_box()
        report['size_mm'] = [round(float(bb.size.X), 3),
                             round(float(bb.size.Y), 3),
                             round(float(bb.size.Z), 3)]
    except Exception:  # noqa: BLE001
        pass
    return report
