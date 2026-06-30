"""FusionMCP — Model Context Protocol server for Autodesk Fusion 360.

A stdio MCP server (FastMCP) that forwards tool calls over a persistent local
socket to the FusionMCP add-in running inside Fusion 360.

Conventions
-----------
* All lengths are MILLIMETRES. Angles are DEGREES.
* Geometry is referenced by opaque string tokens (e.g. "edg7", "fac3",
  "prf1", "bdy2") returned by get_state / query_entities / feature tools.
  Tokens stay valid for the Fusion session.
* operation is one of: "new", "join", "cut", "intersect".
* Sketch planes: "XY" | "XZ" | "YZ", or a planar-face token.
* Axes: "X" | "Y" | "Z", or a token (sketch line / edge).
"""
import os

from mcp.server.fastmcp import FastMCP, Image

from fusion_client import FusionClient, FusionError, FusionNotConnected

HOST = os.environ.get('FUSION_MCP_HOST', '127.0.0.1')
PORT = int(os.environ.get('FUSION_MCP_PORT', '9123'))

mcp = FastMCP('fusion360')
fusion = FusionClient(HOST, PORT)


def _call(op, **params):
    """Forward to the add-in, turning transport errors into readable strings."""
    try:
        return fusion.call(op, params)
    except (FusionError, FusionNotConnected) as exc:
        return {'error': str(exc)}


# --------------------------------------------------------------------------- #
# State / inspection
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_state(include_mass_props: bool = False) -> dict:
    """Summarise the active Fusion design: document, units, design_type, bodies,
    sketches and parameters, each with a reusable token. Call this first to orient
    yourself. Set include_mass_props=True to also compute per-body volume (slower)."""
    return _call('get_state', include_mass_props=include_mass_props)


@mcp.tool()
def query_entities(kind: str, target: str = '', include_mass_props: bool = False) -> dict:
    """List sub-entities with tokens and geometry, for picking edges/faces/profiles.

    kind: "bodies" | "sketches" | "profiles" | "faces" | "edges".
    target: required for profiles (a sketch token) and for faces/edges (a body
    token). Edges report length/endpoints; faces report centroid/type.
    Set include_mass_props=True to also compute face/profile area (slower solve)."""
    return _call('query_entities', kind=kind, target=target or None,
                 include_mass_props=include_mass_props)


# --------------------------------------------------------------------------- #
# Sketching
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_sketch(plane: str = 'XY', name: str = '') -> dict:
    """Create a sketch on a plane ("XY"/"XZ"/"YZ" or a planar-face token).
    Returns a sketch token used by the sketch_* tools."""
    return _call('create_sketch', plane=plane, name=name or None)


@mcp.tool()
def sketch_rectangle(sketch: str, x1: float, y1: float, x2: float, y2: float) -> dict:
    """Add a two-corner rectangle (mm) to a sketch. Returns updated profiles."""
    return _call('sketch_rectangle', sketch=sketch, x1=x1, y1=y1, x2=x2, y2=y2)


@mcp.tool()
def sketch_circle(sketch: str, cx: float, cy: float, r: float) -> dict:
    """Add a circle (centre cx,cy and radius r, mm). Returns updated profiles."""
    return _call('sketch_circle', sketch=sketch, cx=cx, cy=cy, r=r)


@mcp.tool()
def sketch_line(sketch: str, x1: float, y1: float, x2: float, y2: float) -> dict:
    """Add a single line segment (mm). Returns the line token and any profiles."""
    return _call('sketch_line', sketch=sketch, x1=x1, y1=y1, x2=x2, y2=y2)


@mcp.tool()
def sketch_arc(sketch: str, cx: float, cy: float, start_x: float, start_y: float,
               sweep_deg: float) -> dict:
    """Add an arc by centre, start point and swept angle (degrees, CCW)."""
    return _call('sketch_arc', sketch=sketch, cx=cx, cy=cy,
                 start_x=start_x, start_y=start_y, sweep_deg=sweep_deg)


@mcp.tool()
def sketch_polygon(sketch: str, cx: float, cy: float, r: float, sides: int,
                   start_angle: float = 0.0) -> dict:
    """Add a regular polygon inscribed in radius r (mm), with `sides` vertices."""
    return _call('sketch_polygon', sketch=sketch, cx=cx, cy=cy, r=r,
                 sides=sides, start_angle=start_angle)


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
@mcp.tool()
def extrude(profile: str, distance: float, operation: str = 'new',
            symmetric: bool = False) -> dict:
    """Extrude a profile token by `distance` mm. operation: new|join|cut|intersect.
    If symmetric, distance is the total (centred) length. Returns body tokens."""
    return _call('extrude', profile=profile, distance=distance,
                 operation=operation, symmetric=symmetric)


@mcp.tool()
def revolve(profile: str, axis: str, angle: float = 360.0,
            operation: str = 'new') -> dict:
    """Revolve a profile around an axis ("X"/"Y"/"Z" or a line/edge token) by
    `angle` degrees. operation: new|join|cut|intersect."""
    return _call('revolve', profile=profile, axis=axis, angle=angle,
                 operation=operation)


@mcp.tool()
def fillet(edges: list[str], radius: float) -> dict:
    """Round one or more edge tokens with a constant radius (mm)."""
    return _call('fillet', edges=edges, radius=radius)


@mcp.tool()
def chamfer(edges: list[str], distance: float) -> dict:
    """Bevel one or more edge tokens with an equal distance (mm)."""
    return _call('chamfer', edges=edges, distance=distance)


@mcp.tool()
def shell(thickness: float, faces: list[str] = []) -> dict:
    """Hollow the body with a wall `thickness` mm, removing the given face tokens
    (open faces). Pass an empty list to shell without removing a face."""
    return _call('shell', thickness=thickness, faces=faces)


@mcp.tool()
def combine(target: str, tools: list[str], operation: str = 'join',
            keep_tools: bool = False) -> dict:
    """Boolean combine a target body token with tool body tokens.
    operation: join|cut|intersect."""
    return _call('combine', target=target, tools=tools, operation=operation,
                 keep_tools=keep_tools)


@mcp.tool()
def rectangular_pattern(entities: list[str], count1: int, spacing1: float,
                        direction1: str = 'X', count2: int = 0,
                        spacing2: float = 0.0, direction2: str = 'Y') -> dict:
    """Rectangular pattern of body/feature tokens. counts are instance counts,
    spacings are mm. Set count2>0 for a second direction."""
    return _call('rectangular_pattern', entities=entities, count1=count1,
                 spacing1=spacing1, direction1=direction1, count2=count2,
                 spacing2=spacing2, direction2=direction2)


@mcp.tool()
def circular_pattern(entities: list[str], axis: str, count: int,
                     angle: float = 360.0, symmetric: bool = False) -> dict:
    """Circular pattern of tokens about an axis ("X"/"Y"/"Z" or token),
    `count` instances over `angle` degrees."""
    return _call('circular_pattern', entities=entities, axis=axis, count=count,
                 angle=angle, symmetric=symmetric)


@mcp.tool()
def mirror(entities: list[str], plane: str) -> dict:
    """Mirror tokens across a plane ("XY"/"XZ"/"YZ" or a planar-face token)."""
    return _call('mirror', entities=entities, plane=plane)


@mcp.tool()
def move_body(body: str, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> dict:
    """Translate a body token by dx,dy,dz millimetres."""
    return _call('move_body', body=body, dx=dx, dy=dy, dz=dz)


@mcp.tool()
def delete(token: str) -> dict:
    """Delete the entity referenced by a token (body, feature, sketch, ...)."""
    return _call('delete', token=token)


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_parameters() -> dict:
    """List all model and user parameters with names, expressions and units."""
    return _call('list_parameters')


@mcp.tool()
def set_parameter(name: str, expression: str) -> dict:
    """Set a parameter by expression, e.g. expression="50 mm" or "width * 2"."""
    return _call('set_parameter', name=name, expression=expression)


@mcp.tool()
def add_parameter(name: str, value: str, units: str = 'mm', comment: str = '') -> dict:
    """Create a user parameter. value may be a number or an expression string
    like "25 mm"; units e.g. "mm", "deg", "" (unitless)."""
    return _call('add_parameter', name=name, value=value, units=units, comment=comment)


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
@mcp.tool()
def export(format: str, path: str, allow_fallback: bool = True) -> dict:
    """Export the design to an absolute file path.
    format: step | iges | sat | smt | f3d | stl.
    On Fusion Personal some neutral formats (STEP/IGES/SAT/SMT) may be
    license-restricted; with allow_fallback the export falls back to STL then F3D
    and reports what happened instead of erroring."""
    return _call('export', format=format, path=path, allow_fallback=allow_fallback)


@mcp.tool()
def screenshot(path: str = '', width: int = 1024, height: int = 768,
               direction: str = 'current', fit: bool = False) -> Image:
    """Capture the active viewport and return it as an image so you can SEE the
    current model. If path is empty a temp file is used. Keep the resolution
    modest on slower hardware.

    direction: camera preset — current (default, leaves the camera as-is),
      front, back, left, right, top, bottom, iso, iso-top-right, iso-top-left,
      iso-bottom-right, iso-bottom-left.
    fit: when True, zoom to frame the whole model (recommended with iso/top)."""
    if not path:
        path = os.path.join(os.environ.get('TEMP', os.getcwd()), 'fusion_mcp_view.png')
    result = fusion.call('screenshot', {'path': path, 'width': width,
                                        'height': height, 'direction': direction,
                                        'fit': fit, 'return_base64': True})
    b64 = result.get('image_base64')
    if not b64:
        raise RuntimeError('Screenshot failed: {}'.format(result))
    import base64
    return Image(data=base64.b64decode(b64), format='png')


@mcp.tool()
def capture_to_file(path: str, width: int = 1024, height: int = 768,
                    direction: str = 'current', fit: bool = False) -> dict:
    """Save the viewport to a PNG file WITHOUT returning the image bytes. Use
    when you just need the file (e.g. to attach later) and don't need to see it
    now — avoids a large base64 payload. direction/fit as in `screenshot`."""
    return fusion.call('screenshot', {'path': path, 'width': width,
                                      'height': height, 'direction': direction,
                                      'fit': fit, 'return_base64': False})


@mcp.tool()
def fit_view() -> dict:
    """Zoom the viewport to fit the whole model."""
    return _call('fit_view')


@mcp.tool()
def set_design_mode(mode: str) -> dict:
    """Switch modeling mode. mode="direct" drops timeline/history for faster
    one-shot builds and lower memory (good on Personal-tier hardware);
    mode="parametric" keeps editable history (default). Switching an existing
    parametric design to direct flattens its history."""
    return _call('set_design_mode', mode=mode)


@mcp.tool()
def save(message: str = '') -> dict:
    """Save the active document. New (never-saved) documents must be saved once
    manually in Fusion first."""
    return _call('save', message=message)


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #
@mcp.tool()
def batch(operations: list[dict], stop_on_error: bool = True) -> dict:
    """Run many operations in ONE round trip and ONE main-thread dispatch — the
    fastest way to build a multi-step part (use this instead of many separate
    tool calls).

    Each item: {"op": <tool name>, "params": {...}, "as": <optional alias>}.
    Reference earlier results with "$alias.path", e.g. a profile token produced
    mid-batch:

        [
          {"op": "create_sketch", "params": {"plane": "XY"}, "as": "s"},
          {"op": "sketch_rectangle",
           "params": {"sketch": "$s.sketch", "x1": 0, "y1": 0, "x2": 40, "y2": 20},
           "as": "r"},
          {"op": "extrude",
           "params": {"profile": "$r.profiles[0].token", "distance": 10}}
        ]

    op names match the other tools (create_sketch, sketch_rectangle, extrude,
    fillet, ...). Returns a per-operation result list."""
    return _call('batch', operations=operations, stop_on_error=stop_on_error)


@mcp.tool()
def run_fusion_code(code: str) -> dict:
    """Execute an arbitrary Fusion 360 Python API snippet on the main thread —
    the power tool for anything the curated tools don't cover, in ONE round trip.

    In scope: adsk, app, ui, design, root, math, MM (mm->cm factor), registry,
    reg(kind, obj) -> token, and mm/degree helpers returning LIVE objects:
        pt(x_mm, y_mm, z_mm=0), mm(v), vmm(v)->ValueInput, deg(d)->radians,
        new_sketch(plane), rect(sk, x1,y1,x2,y2), circle(sk, cx,cy,r),
        extrude_profile(profile, dist_mm, operation='new', symmetric=False)
    Assign to `result` (JSON-serialisable) to return a value; stdout is captured.

    Example (a 40x20x10 mm block in a few lines):
        sk = new_sketch("XY"); rect(sk, 0, 0, 40, 20)
        f = extrude_profile(sk.profiles.item(0), 10)
        result = f.bodies.item(0).name
    """
    return _call('run_code', code=code)


if __name__ == '__main__':
    mcp.run()
