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
import json
import os

import updater
from fusion_client import FusionClient, FusionError, FusionNotConnected
from mcp.server.fastmcp import FastMCP, Image

HOST = os.environ.get('FUSION_MCP_HOST', '127.0.0.1')
PORT = int(os.environ.get('FUSION_MCP_PORT', '9123'))

mcp = FastMCP('fusion360')
fusion = FusionClient(HOST, PORT)


# Tool annotations (readOnlyHint/destructiveHint/idempotentHint) let MCP clients
# reason about a tool's safety. They arrived in the SDK ~1.9; on older versions
# ToolAnnotations is absent, so _annot yields no kwargs and tools stay plain.
try:
    from mcp.types import ToolAnnotations

    def _annot(**kw):
        return {'annotations': ToolAnnotations(**kw)}
except Exception:  # pragma: no cover - depends on installed SDK
    def _annot(**kw):
        return {}


def _call(op, **params):
    """Forward to the add-in, turning transport errors into readable strings.
    The first result of a session also carries the pending-update notice (new
    version + release notes) gathered by the startup background check."""
    try:
        result = fusion.call(op, params)
    except (FusionError, FusionNotConnected) as exc:
        return {'error': str(exc)}
    if isinstance(result, dict):
        notice = updater.consume_notice()
        if notice:
            result['fusionmcp_update'] = notice
    return result


# --------------------------------------------------------------------------- #
# State / inspection
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def server_info() -> dict:
    """Report the add-in version, uptime and per-operation telemetry
    (call counts, average/max execution time in ms, error counts). Useful for
    checking the connection is live and for spotting slow operations."""
    info = _call('server_info')
    if isinstance(info, dict):
        info['server_version'] = updater.LOCAL_VERSION
        addin = info.get('version')
        if addin and addin != updater.LOCAL_VERSION:
            info['version_mismatch'] = (
                'Add-in %s != server %s. After an update the Fusion add-in '
                'must be restarted (Shift+S -> Stop, Run) to load the new code.'
                % (addin, updater.LOCAL_VERSION))
    return info


@mcp.tool(**_annot(readOnlyHint=True))
def check_for_updates() -> dict:
    """Check GitHub for a newer FusionMCP release. Returns current vs latest
    version, whether an update is available, release notes, and whether the
    startup auto-check already pre-downloaded the package ("downloaded"). It
    never installs anything. If an update is available, ASK THE USER before
    calling apply_update."""
    return updater.check()


@mcp.tool(**_annot(destructiveHint=True))
def apply_update(confirm: bool = False, method: str = 'auto') -> dict:
    """Install the latest FusionMCP version (uses the pre-downloaded package
    from the startup check when present, else downloads). Requires the user's
    consent: only call with confirm=True after the user has agreed to a specific
    update reported by check_for_updates or the startup notice. method: "auto"
    (git pull for a clean checkout, else release zip), "git", or "zip". After it
    succeeds the user must restart the Fusion add-in and Claude Desktop."""
    return updater.apply(confirm=confirm, method=method)


@mcp.tool(**_annot(readOnlyHint=True))
def get_state(include_mass_props: bool = False) -> dict:
    """Summarise the active Fusion design: document, units, design_type, bodies,
    sketches and parameters, each with a reusable token. Call this first to orient
    yourself. Set include_mass_props=True to also compute per-body volume (slower)."""
    return _call('get_state', include_mass_props=include_mass_props)


@mcp.tool(**_annot(readOnlyHint=True))
def query_entities(kind: str, target: str = '', include_mass_props: bool = False) -> dict:
    """List sub-entities with tokens and geometry, for picking edges/faces/profiles.

    kind: "bodies" | "sketches" | "profiles" | "faces" | "edges" | "occurrences"
    | "meshes".
    target: required for profiles (a sketch token) and for faces/edges (a body
    token). Edges report length/endpoints; faces report centroid/type;
    occurrences report component name and body count; meshes are imported
    scan/mesh bodies.
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


@mcp.tool()
def sketch_points(sketch: str, points: list[list[float]]) -> dict:
    """Add many sketch points (mm) in one call. points: [[x,y], ...]. Returns a
    point token per input point. Fewer round-trips than one call per point."""
    return _call('sketch_points', sketch=sketch, points=points)


@mcp.tool()
def sketch_polyline(sketch: str, points: list[list[float]],
                    closed: bool = False) -> dict:
    """Add a connected polyline through points (mm) in one call. points:
    [[x,y], ...]. closed=True joins the last point back to the first."""
    return _call('sketch_polyline', sketch=sketch, points=points, closed=closed)


@mcp.tool()
def sketch_spline(sketch: str, points: list[list[float]]) -> dict:
    """Add a fitted spline through points (mm) in one call. points: [[x,y], ...]."""
    return _call('sketch_spline', sketch=sketch, points=points)


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
@mcp.tool()
def extrude(profile: str, distance: float = 0.0, operation: str = 'new',
            symmetric: bool = False, taper_angle: float = 0.0,
            to_face: str = '') -> dict:
    """Extrude a profile token by `distance` mm. operation: new|join|cut|intersect.
    If symmetric, distance is the total (centred) length. taper_angle (deg)
    drafts the sides (e.g. molds). to_face: extrude up to a face/body token
    instead of a distance. Returns body tokens."""
    return _call('extrude', profile=profile, distance=distance,
                 operation=operation, symmetric=symmetric,
                 taper_angle=taper_angle, to_face=to_face or None)


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
def offset_face(faces: list[str], distance: float) -> dict:
    """Press-pull: offset the given face tokens by `distance` mm (negative
    pushes inward). The quickest way to tweak a wall thickness or clearance
    without touching sketches."""
    return _call('offset_face', faces=faces, distance=distance)


@mcp.tool()
def scale(entities: list[str], factor: float, point: str = '') -> dict:
    """Uniformly scale bodies/components (tokens) by `factor` about a point
    token (default: the design origin). Classic use: an STL/scan imported in
    the wrong unit (factor 25.4 or 0.0394)."""
    return _call('scale', entities=entities, factor=factor, point=point or None)


@mcp.tool()
def thicken(faces: list[str], thickness: float, symmetric: bool = False,
            operation: str = 'new') -> dict:
    """Thicken surface faces (tokens) into a solid, `thickness` mm (symmetric
    centres it on the surface). operation: new|join|cut|intersect. Turns
    surface lofts/sweeps into printable solids."""
    return _call('thicken', faces=faces, thickness=thickness,
                 symmetric=symmetric, operation=operation)


@mcp.tool()
def move_body(body: str, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> dict:
    """Translate a body token by dx,dy,dz millimetres."""
    return _call('move_body', body=body, dx=dx, dy=dy, dz=dz)


@mcp.tool(**_annot(destructiveHint=True))
def delete(token: str) -> dict:
    """Delete the entity referenced by a token (body, feature, sketch, ...)."""
    return _call('delete', token=token)


# --------------------------------------------------------------------------- #
# Holes, construction geometry, sketch constraints/dimensions/editing
# --------------------------------------------------------------------------- #
@mcp.tool()
def hole(sketch: str, x: float, y: float, diameter: float, depth: float = 0.0,
         through_all: bool = False, kind: str = 'simple',
         cbore_diameter: float = 0.0, cbore_depth: float = 0.0,
         csink_diameter: float = 0.0, csink_angle: float = 90.0) -> dict:
    """Create a hole at point (x,y) mm on a sketch. kind: simple|counterbore|
    countersink. Set through_all=True or give depth (mm). Counterbore needs
    cbore_diameter/cbore_depth; countersink needs csink_diameter/csink_angle."""
    return _call('hole', sketch=sketch, x=x, y=y, diameter=diameter, depth=depth,
                 through_all=through_all, kind=kind, cbore_diameter=cbore_diameter,
                 cbore_depth=cbore_depth, csink_diameter=csink_diameter,
                 csink_angle=csink_angle)


@mcp.tool()
def construction_plane(method: str = 'offset', base: str = 'XY', offset: float = 0.0,
                       axis: str = 'X', angle: float = 0.0, points: list[str] = [],
                       face: str = '') -> dict:
    """Create a construction plane. method: "offset" (base plane/face + offset mm),
    "angle" (base + axis + angle deg), "three_points" (3 point tokens),
    "tangent" (cylindrical face + angle). Returns a plane token usable anywhere a
    plane is accepted."""
    return _call('construction_plane', method=method, base=base, offset=offset,
                 axis=axis, angle=angle, points=points, face=face or None)


@mcp.tool()
def construction_axis(method: str = 'edge', edge: str = '', points: list[str] = [],
                      face: str = '') -> dict:
    """Create a construction axis. method: "edge" (linear edge token), "two_points"
    (2 point tokens), "cylinder" (cylindrical face token). Returns an axis token."""
    return _call('construction_axis', method=method, edge=edge or None,
                 points=points, face=face or None)


@mcp.tool()
def construction_point(method: str = 'at_point', point: str = '', edges: list[str] = [],
                       edge: str = '', plane: str = '') -> dict:
    """Create a construction point. method: "at_point" (vertex/sketch-point token),
    "two_edges" (2 edge tokens), "edge_plane" (edge token + plane)."""
    return _call('construction_point', method=method, point=point or None,
                 edges=edges, edge=edge or None, plane=plane or None)


@mcp.tool()
def sketch_constraint(sketch: str, kind: str, entities: list[str]) -> dict:
    """Add a geometric constraint. kind: horizontal|vertical (1 line),
    parallel|perpendicular|equal|collinear (2 lines), tangent|concentric
    (2 curves), coincident (point+curve/point), midpoint (point+line).
    entities are curve/point tokens (see sketch_line/sketch_circle returns)."""
    return _call('sketch_constraint', sketch=sketch, kind=kind, entities=entities)


@mcp.tool()
def sketch_dimension(sketch: str, kind: str, entities: list[str],
                     text_x: float = 0.0, text_y: float = 0.0,
                     parameter: str = '') -> dict:
    """Add a driving dimension. kind: distance (2 tokens), radius|diameter
    (circle/arc token), angle (2 lines). text_x/text_y (mm) place the dimension
    text. Pass parameter to name the created dimension parameter for later reuse."""
    return _call('sketch_dimension', sketch=sketch, kind=kind, entities=entities,
                 text_x=text_x, text_y=text_y, parameter=parameter or None)


@mcp.tool()
def project_to_sketch(sketch: str, entities: list[str]) -> dict:
    """Project edges/faces/vertices (tokens) onto a sketch. Returns tokens of the
    new projected sketch curves."""
    return _call('project_to_sketch', sketch=sketch, entities=entities)


@mcp.tool()
def sketch_offset(sketch: str, curves: list[str], distance: float,
                  dir_x: float = 0.0, dir_y: float = 0.0) -> dict:
    """Offset sketch curve tokens by `distance` mm. (dir_x,dir_y) mm picks the
    side to offset toward. Returns tokens of the new offset curves."""
    return _call('sketch_offset', sketch=sketch, curves=curves, distance=distance,
                 dir_x=dir_x, dir_y=dir_y)


@mcp.tool()
def sketch_fillet(sketch: str, line1: str, line2: str, radius: float) -> dict:
    """Add a 2D fillet of `radius` mm between two sketch lines sharing an endpoint."""
    return _call('sketch_fillet', sketch=sketch, line1=line1, line2=line2, radius=radius)


# --------------------------------------------------------------------------- #
# Advanced features
# --------------------------------------------------------------------------- #
@mcp.tool()
def loft(profiles: list[str], rails: list[str] = [], operation: str = 'new') -> dict:
    """Loft through 2+ profile tokens (ordered). Optional rails (curve/edge
    tokens) guide the shape. operation: new|join|cut|intersect."""
    return _call('loft', profiles=profiles, rails=rails, operation=operation)


@mcp.tool()
def sweep(profile: str, path: str, twist_angle: float = 0.0,
          operation: str = 'new') -> dict:
    """Sweep a profile token along a path (curve/edge token). twist_angle in deg.
    operation: new|join|cut|intersect."""
    return _call('sweep', profile=profile, path=path, twist_angle=twist_angle,
                 operation=operation)


@mcp.tool()
def rib(curves: list[str], thickness: float, symmetric: bool = True,
        depth: float = 0.0) -> dict:
    """Create a rib from open sketch curve tokens with `thickness` mm.
    symmetric centres thickness on the curves; depth (mm) sets extent if given."""
    return _call('rib', curves=curves, thickness=thickness, symmetric=symmetric,
                 depth=depth)


@mcp.tool()
def draft(faces: list[str], neutral_plane: str, angle: float,
          tangent_chain: bool = True) -> dict:
    """Apply a draft `angle` deg to face tokens, pulled from a neutral plane
    (plane name or planar-face token)."""
    return _call('draft', faces=faces, neutral_plane=neutral_plane, angle=angle,
                 tangent_chain=tangent_chain)


@mcp.tool()
def thread(face: str, internal: bool = False, modeled: bool = True) -> dict:
    """Add a thread to a cylindrical face token, sized from Fusion's recommended
    thread data. modeled=True cuts real geometry; False is cosmetic."""
    return _call('thread', face=face, internal=internal, modeled=modeled)


@mcp.tool()
def split_body(body: str, tool: str, extend_tool: bool = True) -> dict:
    """Split a body token with a tool: a body/face token, or a plane name
    ("XY"/"XZ"/"YZ") / construction-plane token."""
    return _call('split_body', body=body, tool=tool, extend_tool=extend_tool)


# --------------------------------------------------------------------------- #
# Assemblies: components, joints, rename, copy
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_component(name: str = '') -> dict:
    """Create a new empty component (as an occurrence under the root).
    Returns component + occurrence tokens."""
    return _call('create_component', name=name or None)


@mcp.tool()
def rename(token: str, new_name: str) -> dict:
    """Rename any named entity by token: body, sketch, component, feature or
    occurrence."""
    return _call('rename', token=token, new_name=new_name)


@mcp.tool()
def copy_body(bodies: list[str], target: str = '') -> dict:
    """Copy body tokens (into an optional target component/occurrence token).
    Returns tokens of the pasted bodies."""
    return _call('copy_body', bodies=bodies, target=target or None)


@mcp.tool()
def joint(geo0: str, geo1: str, motion: str = 'rigid', axis: str = 'Z') -> dict:
    """Create a joint between two geometry tokens (planar faces recommended).
    motion: rigid|revolute|slider|cylindrical|pin_slot|planar|ball. axis
    ("X"/"Y"/"Z") sets the rotation/slide axis for the relevant motions."""
    return _call('joint', geo0=geo0, geo1=geo1, motion=motion, axis=axis)


# --------------------------------------------------------------------------- #
# Materials, measurement, import, timeline
# --------------------------------------------------------------------------- #
@mcp.tool()
def set_material(body: str, material: str, library: str = '') -> dict:
    """Assign a physical material (e.g. "Steel", "Aluminum 6061") to a body
    token — changes its computed mass. Optional library name to disambiguate."""
    return _call('set_material', body=body, material=material, library=library or None)


@mcp.tool()
def set_appearance(body: str, appearance: str, library: str = '') -> dict:
    """Assign an appearance (colour/finish) to a body token. Optional library."""
    return _call('set_appearance', body=body, appearance=appearance,
                 library=library or None)


@mcp.tool(**_annot(readOnlyHint=True))
def measure(a: str, b: str, kind: str = 'distance') -> dict:
    """Measure between two geometry tokens. kind: "distance" (minimum distance,
    mm) or "angle" (degrees)."""
    return _call('measure', a=a, b=b, kind=kind)


@mcp.tool(**_annot(readOnlyHint=True))
def bounding_box(body: str = '') -> dict:
    """Axis-aligned bounding box in mm (min/max points and x/y/z size). Pass a
    body token, or omit for the whole model."""
    return _call('bounding_box', body=body or None)


@mcp.tool(**_annot(readOnlyHint=True))
def center_of_mass(body: str) -> dict:
    """Centre of mass of a body token, in mm (runs a mass-properties solve)."""
    return _call('center_of_mass', body=body)


@mcp.tool(**_annot(readOnlyHint=True))
def interference(bodies: list[str]) -> dict:
    """Detect interference (overlap) between two or more body tokens. Returns
    interfering pairs with overlap volume (mm^3)."""
    return _call('interference', bodies=bodies)


@mcp.tool()
def import_file(format: str, path: str, plane: str = 'XY') -> dict:
    """Import a CAD file. format: step|iges|sat|smt|f3d (into the root) or dxf
    (2D sketch onto `plane`: "XY"/"XZ"/"YZ" or a planar-face token)."""
    return _call('import_file', format=format, path=path, plane=plane)


@mcp.tool()
def timeline(action: str = 'list', position: int = 0) -> dict:
    """Inspect or roll back the parametric timeline. action: "list" (items with
    index/name/suppressed) or "rollback" (move the marker to `position`)."""
    return _call('timeline', action=action, position=position)


@mcp.tool()
def suppress_feature(feature: str, suppress: bool = True) -> dict:
    """Suppress (or unsuppress with suppress=False) a feature token in the
    timeline. Parametric designs only."""
    return _call('suppress_feature', feature=feature, suppress=suppress)


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
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
    format: step | iges | sat | smt | f3d | stl | 3mf (3mf is the better
    3D-print format: mesh + units in one file).
    On Fusion Personal some neutral formats (STEP/IGES/SAT/SMT) may be
    license-restricted; with allow_fallback the export falls back to STL then F3D
    and reports what happened instead of erroring."""
    return _call('export', format=format, path=path, allow_fallback=allow_fallback)


@mcp.tool(**_annot(readOnlyHint=True))
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


@mcp.tool(**_annot(readOnlyHint=True))
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


# --------------------------------------------------------------------------- #
# BOM, sketch text / engraving, sheet metal, meshes, drawings
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def bom(include_mass: bool = True, csv_path: str = '') -> dict:
    """Bill of materials for the active design: one row per component with
    quantity, body count, materials and per-unit mass (kg) plus the assembly's
    total mass. Set csv_path (absolute path) to also write the table as CSV."""
    return _call('bom', include_mass=include_mass, csv_path=csv_path or None)


@mcp.tool()
def sketch_text(sketch: str, text: str, x: float = 0.0, y: float = 0.0,
                height: float = 10.0, font: str = '', bold: bool = False,
                italic: bool = False, angle: float = 0.0,
                path: str = '') -> dict:
    """Add text to a sketch at (x, y) mm with cap height `height` mm — or along
    a curve when `path` (sketch line/arc/circle/spline token) is given (labels
    on arcs, ring engravings). Optional font name, bold/italic, rotation angle
    (deg). The returned text token can be extruded directly or passed to emboss
    for engraving."""
    return _call('sketch_text', sketch=sketch, text=text, x=x, y=y, height=height,
                 font=font or None, bold=bold, italic=italic, angle=angle,
                 path=path or None)


@mcp.tool()
def emboss(profile: str, depth: float, engrave: bool = True) -> dict:
    """Engrave (engrave=True, cuts into the solid below the sketch plane) or
    emboss (engrave=False, raises material above it) a sketch-text or profile
    token, `depth` mm deep. Typical flow: sketch on a face -> sketch_text ->
    emboss."""
    return _call('emboss', profile=profile, depth=depth, engrave=engrave)


@mcp.tool()
def flat_pattern(face: str = '', body: str = '') -> dict:
    """Create (or reuse) the flat pattern of a sheet-metal body. Pass the
    stationary planar face token, or just the body token (its largest planar
    face is used). The body must be sheet metal (uniform thickness)."""
    return _call('flat_pattern', face=face or None, body=body or None)


@mcp.tool()
def export_flat_pattern(path: str, face: str = '', body: str = '') -> dict:
    """Export the flat pattern as a DXF outline ready for laser/waterjet
    cutting. Creates the flat pattern first when a face/body token is given and
    none exists yet."""
    return _call('export_flat_pattern', path=path, face=face or None,
                 body=body or None)


@mcp.tool()
def export_sketch_dxf(sketch: str, path: str) -> dict:
    """Save a sketch (token) as a 2D DXF file — fully scripted 2D output for
    laser cutting or documentation, no drawing sheet needed."""
    return _call('export_sketch_dxf', sketch=sketch, path=path)


@mcp.tool()
def import_mesh(path: str, units: str = 'mm') -> dict:
    """Insert an STL/OBJ/3MF scan or mesh file and return mesh tokens. units:
    mm|cm|m|in|ft — mesh files carry no units, so pick what the scan was
    exported in. Reverse-engineering entry point (see mesh_to_brep,
    mesh_section)."""
    return _call('import_mesh', path=path, units=units)


@mcp.tool(**_annot(readOnlyHint=True))
def mesh_info(mesh: str = '') -> dict:
    """Triangle/node counts and bounding box of mesh bodies. Pass a mesh token,
    or omit to report every mesh in the design."""
    return _call('mesh_info', mesh=mesh or None)


@mcp.tool()
def mesh_to_brep(meshes: list[str] = []) -> dict:
    """Convert mesh bodies (tokens; all meshes when omitted) into solid BRep
    bodies so every solid tool works on them (combine, split_body, measure,
    export STEP...). Dense scans convert as faceted solids — consider reducing
    the mesh in Fusion first."""
    return _call('mesh_to_brep', meshes=meshes)


@mcp.tool()
def mesh_section(mesh: str, plane: str = 'XY', offset: float = 0.0) -> dict:
    """Slice a mesh with a plane ("XY"/"XZ"/"YZ" or a plane token) at `offset`
    mm, producing a section sketch of the scan's cross-section — trace it with
    sketch_polyline/sketch_spline and dimensions to rebuild the part
    parametrically."""
    return _call('mesh_section', mesh=mesh, plane=plane, offset=offset)


@mcp.tool()
def create_drawing() -> dict:
    """Open Fusion's "Drawing from Design" dialog for the active design (the
    Fusion API cannot build drawing sheets headlessly — the user completes the
    sheet setup in the UI). For fully scripted 2D output use export_sketch_dxf
    or export_flat_pattern."""
    return _call('create_drawing')


# --------------------------------------------------------------------------- #
# Mass report, parameter CSV round-trip, CAM
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def mass_properties(body: str) -> dict:
    """Full mass report for a body token: mass (kg), volume (mm^3), surface
    area (mm^2), centre of mass (mm) and moments of inertia (kg*mm^2). Set the
    material first (set_material) for correct density."""
    return _call('mass_properties', body=body)


@mcp.tool(**_annot(readOnlyHint=True))
def export_parameters(csv_path: str) -> dict:
    """Write all model/user parameters to a CSV file (name, kind, expression,
    unit, comment) for spreadsheet editing; re-apply with import_parameters."""
    return _call('export_parameters', csv_path=csv_path)


@mcp.tool()
def import_parameters(csv_path: str) -> dict:
    """Apply parameters from a CSV (columns: name, expression; optional unit,
    comment). Existing parameters are updated, unknown names become new user
    parameters; returns per-row results."""
    return _call('import_parameters', csv_path=csv_path)


@mcp.tool(**_annot(readOnlyHint=True))
def cam_setups() -> dict:
    """List MANUFACTURE (CAM) setups with their operations and toolpath state.
    Note: the Fusion API cannot CREATE setups — the user makes them once in the
    MANUFACTURE workspace; generation and posting are then scriptable."""
    return _call('cam_setups')


@mcp.tool()
def cam_generate(setup: str = '', timeout: float = 240.0) -> dict:
    """(Re)generate CAM toolpaths — one setup by name, or all when omitted.
    Blocks until done or `timeout` seconds; check cam_setups afterwards."""
    return _call('cam_generate', setup=setup or None, timeout=timeout)


@mcp.tool()
def cam_post(setup: str, path: str, post_config: str = '',
             units: str = 'mm') -> dict:
    """Post-process a setup's toolpaths to NC/G-code at `path`. post_config: a
    .cps file path or a name from Fusion's generic post library (default
    fanuc.cps); units: mm|in|document. Run cam_generate first."""
    return _call('cam_post', setup=setup, path=path,
                 post_config=post_config or None, units=units)


# --------------------------------------------------------------------------- #
# Assembly motion and cloud documents
# --------------------------------------------------------------------------- #
@mcp.tool()
def drive_joint(joint: str, value: float, kind: str = 'auto') -> dict:
    """Set a joint's motion value: rotation in degrees (revolute/cylindrical)
    or slide in mm (slider/cylindrical). kind: "auto" (pick what the joint
    supports), "rotation", "slide". Drive the joint through its range and use
    interference + multi_screenshot to verify a mechanism."""
    return _call('drive_joint', joint=joint, value=value, kind=kind)


@mcp.tool()
def set_joint_limits(joint: str, kind: str = 'rotation', min: float | None = None,
                     max: float | None = None, rest: float | None = None) -> dict:
    """Limit a joint's motion range. kind: "rotation" (deg) or "slide" (mm).
    Only the limits you pass are changed; rest is the neutral position."""
    return _call('set_joint_limits', joint=joint, kind=kind, min=min, max=max,
                 rest=rest)


@mcp.tool()
def move_occurrence(occurrence: str, dx: float = 0.0, dy: float = 0.0,
                    dz: float = 0.0, rx: float = 0.0, ry: float = 0.0,
                    rz: float = 0.0) -> dict:
    """Move (mm) and/or rotate (deg, about world axes through its origin) a
    whole occurrence — position components before adding joints. The
    assembly-level counterpart of move_body."""
    return _call('move_occurrence', occurrence=occurrence, dx=dx, dy=dy, dz=dz,
                 rx=rx, ry=ry, rz=rz)


@mcp.tool()
def ground_occurrence(occurrence: str, grounded: bool = True) -> dict:
    """Ground (anchor) an occurrence so joints move other parts relative to
    it; grounded=False releases it."""
    return _call('ground_occurrence', occurrence=occurrence, grounded=grounded)


@mcp.tool(**_annot(readOnlyHint=True))
def list_documents(project: str = '') -> dict:
    """List cloud projects and the documents in their root folders (the data
    panel). Optional project-name filter. First cloud access can be slow."""
    return _call('list_documents', project=project or None)


@mcp.tool()
def open_document(name: str, project: str = '') -> dict:
    """Open a cloud document by name (optionally within a given project); it
    becomes the active document — call get_state afterwards to re-orient."""
    return _call('open_document', name=name, project=project or None)


# --------------------------------------------------------------------------- #
# Interaction: selection, highlighting, visibility, multi-view, section, undo
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def get_selection() -> dict:
    """What the user currently has selected in the Fusion UI, as reusable
    tokens. Lets the user point with the mouse: ask them to click the
    face/edge/body they mean, then call this instead of guessing geometry.
    Faces report centroid/type, edges report length."""
    return _call('get_selection')


@mcp.tool()
def highlight(tokens: list[str] = []) -> dict:
    """Select the given tokens in the Fusion UI so the USER can see which
    entities you mean — confirm before destructive edits ("I'll fillet these 4
    edges — the highlighted ones — OK?"). Replaces the current selection; an
    empty list clears it."""
    return _call('highlight', tokens=tokens)


@mcp.tool()
def set_visibility(tokens: list[str], visible: bool = True) -> dict:
    """Show or hide entities by token (bodies, occurrences, sketches, meshes,
    construction geometry). Hidden bodies don't block screenshots."""
    return _call('set_visibility', tokens=tokens, visible=visible)


@mcp.tool()
def isolate(token: str) -> dict:
    """Show ONLY this body/occurrence/mesh, hiding everything else (state is
    remembered). Ideal before screenshots of one part inside an assembly.
    Restore with unisolate."""
    return _call('isolate', token=token)


@mcp.tool()
def unisolate() -> dict:
    """Restore the visibility state saved by isolate."""
    return _call('unisolate')


# NOTE: deliberately no return annotation — `-> list[Image]` makes FastMCP try
# to build a structured-output schema and pydantic cannot schematize Image,
# which kills the whole server at import time. Unannotated, the Image list is
# converted to image content blocks at runtime.
@mcp.tool(**_annot(readOnlyHint=True))
def multi_screenshot(directions: list[str] = [], width: int = 800,
                     height: int = 600, fit: bool = True):
    """Capture SEVERAL camera presets in one round-trip and return all images —
    see the model from every side at once instead of one screenshot at a time.
    directions defaults to ["iso", "front", "top", "right"]; presets as in
    `screenshot`. Keep the resolution modest — this returns len(directions)
    images."""
    result = fusion.call('multi_screenshot', {
        'directions': directions or None, 'width': width, 'height': height,
        'fit': fit})
    shots = result.get('shots') if isinstance(result, dict) else None
    if not shots:
        raise RuntimeError('multi_screenshot failed: {}'.format(result))
    import base64
    return [Image(data=base64.b64decode(s['image_base64']), format='png')
            for s in shots if s.get('image_base64')]


@mcp.tool()
def section_view(plane: str = 'XY', offset: float = 0.0) -> dict:
    """Slice the DISPLAY (not the geometry) with a section plane at `offset` mm
    — screenshots then show the inside of pockets, shells and housings. plane:
    "XY"/"XZ"/"YZ", a construction-plane or planar-face token. Turn off with
    section_off. Requires Fusion 2023+."""
    return _call('section_view', plane=plane, offset=offset)


@mcp.tool()
def section_off() -> dict:
    """Remove all section-analysis views and restore the full display."""
    return _call('section_off')


@mcp.tool(**_annot(destructiveHint=True))
def undo(steps: int = 1) -> dict:
    """Undo the last `steps` operations in Fusion — the safety net after a
    feature came out wrong. Tokens issued before the undo may point at deleted
    entities: re-run get_state/query_entities before reusing them."""
    return _call('undo', steps=steps)


# --------------------------------------------------------------------------- #
# Resources — read-only views a client can fetch without invoking a tool
# --------------------------------------------------------------------------- #
@mcp.resource('fusion://design/state')
def resource_state() -> str:
    """Current design summary (bodies, sketches, parameters) as JSON."""
    return json.dumps(_call('get_state'), ensure_ascii=False)


@mcp.resource('fusion://design/parameters')
def resource_parameters() -> str:
    """All model/user parameters as JSON."""
    return json.dumps(_call('list_parameters'), ensure_ascii=False)


@mcp.resource('fusion://design/tree')
def resource_tree() -> str:
    """Component/occurrence hierarchy as JSON."""
    return json.dumps(_call('query_entities', kind='occurrences'), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Prompts — reusable, parameterised task templates
# --------------------------------------------------------------------------- #
@mcp.prompt()
def parametric_bracket(width_mm: float = 60, height_mm: float = 40,
                       thickness_mm: float = 5) -> str:
    """Guide the model to build a parametric mounting bracket."""
    return (
        'Build a parametric L-bracket in Fusion 360 using the FusionMCP tools.\n'
        f'Target size: {width_mm}x{height_mm} mm, wall thickness {thickness_mm} mm.\n'
        'Steps: (1) call get_state to orient; (2) add user parameters for width, '
        'height and thickness with add_parameter; (3) sketch the L-profile and '
        'extrude it; (4) add mounting holes with the hole tool; (5) fillet the '
        'inner corner; (6) screenshot direction="iso" fit=True to verify. Prefer '
        'batch for multi-step construction.'
    )


@mcp.prompt()
def prepare_for_3d_print(clearance_mm: float = 0.2) -> str:
    """Guide the model to sanity-check a part for 3D printing."""
    return (
        'Review the active Fusion design for 3D printing. Use get_state and '
        'bounding_box to report overall size. Check wall thickness and add '
        f'{clearance_mm} mm clearance to mating features by editing parameters. '
        'Finally export STL with the export tool and confirm the file path.'
    )


@mcp.prompt()
def assemble_components() -> str:
    """Guide the model to build a multi-component assembly with joints."""
    return (
        'Create a multi-component assembly. For each part call create_component, '
        'build its geometry inside, then use query_entities kind="occurrences" to '
        'list them and the joint tool (planar-face tokens) to constrain motion. '
        'Verify with a screenshot direction="iso" fit=True.'
    )


if __name__ == '__main__':
    # Non-blocking: checks GitHub and pre-downloads a newer version so the
    # first tool result can announce it (with release notes). Installation
    # still happens only via apply_update(confirm=True).
    updater.start_background_check()
    mcp.run()
