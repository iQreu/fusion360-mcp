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
import contextlib
import json
import os
import threading
import time

import codecad
import dfm
import fasteners
import freecad
import mech
import photo
import photogrammetry
import scan
import slicer
import updater
import viewer
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


def _call(op, _consume_notice=True, **params):
    """Forward to the add-in, turning transport errors into a readable dict with
    a structured error `code`. A genuine model-facing tool result also carries
    the one-shot pending-update notice; pass _consume_notice=False for calls the
    model never reads directly (resource prefetches) so the notice isn't eaten
    before the model sees it."""
    try:
        result = fusion.call(op, params)
    except FusionNotConnected as exc:
        return {'error': str(exc), 'code': 'not_connected'}
    except FusionError as exc:
        out = {'error': str(exc)}
        code = getattr(exc, 'code', None)
        if code:
            out['code'] = code
        retriable = getattr(exc, 'retriable', None)
        if retriable is not None:
            out['retriable'] = retriable
        return out
    if _consume_notice and isinstance(result, dict):
        notice = updater.consume_notice()
        if notice:
            result['fusionmcp_update'] = notice
    return result


def _with_screenshot(result, include_screenshot):
    """Visual-verification loop: when a mutating tool is asked, attach an
    iso/fit viewport screenshot to its result (one extra add-in round trip,
    no extra tool call). Any screenshot failure returns the plain result
    unchanged — verification must never break the mutation it verifies.
    Note: the capture moves the camera to the iso preset."""
    if not include_screenshot or not isinstance(result, dict) \
            or result.get('error'):
        return result
    try:
        path = os.path.join(os.environ.get('TEMP', os.getcwd()),
                            'fusion_mcp_verify.png')
        shot = fusion.call('screenshot', {
            'path': path, 'width': 800, 'height': 600, 'direction': 'iso',
            'fit': True, 'return_base64': True})
        b64 = shot.get('image_base64') if isinstance(shot, dict) else None
        if b64:
            import base64
            return [result, Image(data=base64.b64decode(b64), format='png')]
    except Exception:  # noqa: BLE001 - the screenshot is best-effort
        pass
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


# Elicitation (MCP): when the SDK/client supports it, apply_update asks the human
# to accept the specific update in-band, instead of trusting a model-set flag.
# Everything degrades gracefully to the confirm=True gate when unavailable.
try:
    from mcp.server.fastmcp import Context as _Context
except Exception:  # pragma: no cover - depends on installed SDK
    _Context = None

try:
    from pydantic import BaseModel

    class _UpdateConsent(BaseModel):
        """Elicitation response schema: the user's yes/no to installing."""
        install: bool
except Exception:  # pragma: no cover
    _UpdateConsent = None


async def _elicit_update_consent(ctx):
    """Return True if the user explicitly accepted the update via elicitation,
    False otherwise (declined, cancelled, or elicitation unavailable)."""
    elicit = getattr(ctx, 'elicit', None)
    if elicit is None or _UpdateConsent is None:
        return False
    info = updater.check()
    if not info.get('update_available'):
        return False
    prompt = ('Install FusionMCP update %s -> %s now? It overwrites the local '
              'install; you must restart the add-in and Claude Desktop after.'
              % (info.get('current_version'), info.get('latest_version')))
    try:
        # Context.elicit requires a real response schema (a Pydantic model);
        # schema=None makes the SDK raise, so pass a one-field consent model.
        result = await elicit(message=prompt, schema=_UpdateConsent)
    except Exception:  # noqa: BLE001 - client without elicitation, or shape drift
        return False
    # Accept a variety of result shapes across SDK versions.
    action = getattr(result, 'action', None)
    data = getattr(result, 'data', None)
    if action is not None:
        return action == 'accept' and bool(getattr(data, 'install', True))
    if isinstance(result, tuple) and result:
        accepted = str(result[0]).lower().startswith('accept')
        payload = result[1] if len(result) > 1 else None
        return accepted and bool(getattr(payload, 'install', True))
    # Unknown result shape: FAIL CLOSED. This gates a destructive install —
    # a drifted SDK shape (e.g. a mapping, whose keys getattr can't see) must
    # never count as consent just because it is truthy.
    return bool(getattr(result, 'install', False))


if _Context is not None:
    @mcp.tool(**_annot(destructiveHint=True))
    async def apply_update(confirm: bool = False, method: str = 'auto',
                           ctx: _Context = None) -> dict:
        """Install the latest FusionMCP version (uses the pre-downloaded package
        from the startup check when present, else downloads). Consent is
        required: when the client supports elicitation, this asks the user to
        accept the specific update directly; otherwise pass confirm=True only
        after the user agreed to an update from check_for_updates. method:
        "auto" (git pull for a clean checkout, else release zip), "git", "zip".
        After success the user must restart the Fusion add-in and Claude Desktop."""
        if not confirm and ctx is not None:
            confirm = await _elicit_update_consent(ctx)
        return updater.apply(confirm=confirm, method=method)
else:  # pragma: no cover - old SDK without Context
    @mcp.tool(**_annot(destructiveHint=True))
    def apply_update(confirm: bool = False, method: str = 'auto') -> dict:
        """Install the latest FusionMCP version. Requires confirm=True (the
        user's consent) after an update reported by check_for_updates. method:
        "auto"|"git"|"zip". Restart the add-in and Claude Desktop afterwards."""
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


@mcp.tool(**_annot(readOnlyHint=True))
def design_diagnostics(limit: int = 100) -> dict:
    """One-call health report of the active design: timeline features in
    error/warning state (with Fusion's own message), sketches that are not
    fully constrained, open (non-solid) bodies, empty components and unsaved
    changes. Run it after a big batch build or before export/CAM/print to
    catch silent modelling problems. limit caps the issue list."""
    return _call('design_diagnostics', limit=limit)


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
            to_face: str = '', include_screenshot: bool = False):
    """Extrude a profile token by `distance` mm. operation: new|join|cut|intersect.
    If symmetric, distance is the total (centred) length. taper_angle (deg)
    drafts the sides (e.g. molds). to_face: extrude up to a face/body token
    instead of a distance. Returns body tokens. include_screenshot=True also
    attaches an iso screenshot of the result — see what you built without a
    second call."""
    return _with_screenshot(
        _call('extrude', profile=profile, distance=distance,
              operation=operation, symmetric=symmetric,
              taper_angle=taper_angle, to_face=to_face or None),
        include_screenshot)


@mcp.tool()
def revolve(profile: str, axis: str, angle: float = 360.0,
            operation: str = 'new', include_screenshot: bool = False):
    """Revolve a profile around an axis ("X"/"Y"/"Z" or a line/edge token) by
    `angle` degrees. operation: new|join|cut|intersect."""
    return _with_screenshot(
        _call('revolve', profile=profile, axis=axis, angle=angle,
              operation=operation), include_screenshot)


@mcp.tool()
def fillet(edges: list[str], radius: float, include_screenshot: bool = False):
    """Round one or more edge tokens with a constant radius (mm)."""
    return _with_screenshot(_call('fillet', edges=edges, radius=radius),
                            include_screenshot)


@mcp.tool()
def chamfer(edges: list[str], distance: float,
            include_screenshot: bool = False):
    """Bevel one or more edge tokens with an equal distance (mm)."""
    return _with_screenshot(_call('chamfer', edges=edges, distance=distance),
                            include_screenshot)


@mcp.tool()
def shell(thickness: float, faces: list[str] = [],
          validate_only: bool = False, include_screenshot: bool = False):
    """Hollow the body with a wall `thickness` mm, removing the given face tokens
    (open faces). Pass an empty list to shell without removing a face.
    validate_only=True reports whether the shell would succeed (and what it
    would produce) WITHOUT keeping it — cheap pre-flight for thin walls."""
    return _with_screenshot(
        _call('shell', thickness=thickness, faces=faces,
              validate_only=validate_only or None), include_screenshot)


@mcp.tool()
def combine(target: str, tools: list[str], operation: str = 'join',
            keep_tools: bool = False, include_screenshot: bool = False):
    """Boolean combine a target body token with tool body tokens.
    operation: join|cut|intersect."""
    return _with_screenshot(
        _call('combine', target=target, tools=tools, operation=operation,
              keep_tools=keep_tools), include_screenshot)


@mcp.tool()
def rectangular_pattern(entities: list[str], count1: int, spacing1: float,
                        direction1: str = 'X', count2: int = 0,
                        spacing2: float = 0.0, direction2: str = 'Y',
                        include_screenshot: bool = False):
    """Rectangular pattern of body/feature tokens. counts are instance counts,
    spacings are mm. Set count2>0 for a second direction; spacing2 defaults to
    spacing1 when left at 0."""
    return _with_screenshot(
        _call('rectangular_pattern', entities=entities, count1=count1,
              spacing1=spacing1, direction1=direction1, count2=count2,
              spacing2=(spacing2 or spacing1), direction2=direction2),
        include_screenshot)


@mcp.tool()
def circular_pattern(entities: list[str], axis: str, count: int,
                     angle: float = 360.0, symmetric: bool = False,
                     include_screenshot: bool = False):
    """Circular pattern of tokens about an axis ("X"/"Y"/"Z" or token),
    `count` instances over `angle` degrees."""
    return _with_screenshot(
        _call('circular_pattern', entities=entities, axis=axis, count=count,
              angle=angle, symmetric=symmetric), include_screenshot)


@mcp.tool()
def mirror(entities: list[str], plane: str, include_screenshot: bool = False):
    """Mirror tokens across a plane ("XY"/"XZ"/"YZ" or a planar-face token)."""
    return _with_screenshot(_call('mirror', entities=entities, plane=plane),
                            include_screenshot)


@mcp.tool()
def offset_face(faces: list[str], distance: float,
                include_screenshot: bool = False):
    """Press-pull: offset the given face tokens by `distance` mm (negative
    pushes inward). The quickest way to tweak a wall thickness or clearance
    without touching sketches."""
    return _with_screenshot(
        _call('offset_face', faces=faces, distance=distance),
        include_screenshot)


@mcp.tool()
def scale(entities: list[str], factor: float, point: str = '') -> dict:
    """Uniformly scale bodies/components (tokens) by `factor` about a point
    token (default: the design origin). Classic use: an STL/scan imported in
    the wrong unit (factor 25.4 or 0.0394)."""
    return _call('scale', entities=entities, factor=factor, point=point or None)


@mcp.tool()
def thicken(faces: list[str], thickness: float, symmetric: bool = False,
            operation: str = 'new', include_screenshot: bool = False):
    """Thicken surface faces (tokens) into a solid, `thickness` mm (symmetric
    centres it on the surface). operation: new|join|cut|intersect. Turns
    surface lofts/sweeps into printable solids."""
    return _with_screenshot(
        _call('thicken', faces=faces, thickness=thickness,
              symmetric=symmetric, operation=operation), include_screenshot)


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
         csink_diameter: float = 0.0, csink_angle: float = 90.0,
         include_screenshot: bool = False):
    """Create a hole at point (x,y) mm on a sketch. kind: simple|counterbore|
    countersink. Set through_all=True or give depth (mm). Counterbore needs
    cbore_diameter/cbore_depth; countersink needs csink_diameter/csink_angle."""
    return _with_screenshot(
        _call('hole', sketch=sketch, x=x, y=y, diameter=diameter, depth=depth,
              through_all=through_all, kind=kind,
              cbore_diameter=cbore_diameter, cbore_depth=cbore_depth,
              csink_diameter=csink_diameter, csink_angle=csink_angle),
        include_screenshot)


@mcp.tool()
def construction_plane(method: str = 'offset', base: str = 'XY', offset: float = 0.0,
                       axis: str = 'X', angle: float = 0.0, points: list[str] = [],
                       face: str = '', extended: bool = True) -> dict:
    """Create a construction plane. method: "offset" (base plane/face + offset mm),
    "angle" (base + axis + angle deg), "three_points" (3 point tokens),
    "tangent" (cylindrical face + angle). Returns a plane token usable anywhere a
    plane is accepted. extended=False shows it as a compact square instead of
    stretching across the viewport (Fusion July 2026+, ignored on older)."""
    return _call('construction_plane', method=method, base=base, offset=offset,
                 axis=axis, angle=angle, points=points, face=face or None,
                 extended=None if extended else False)


@mcp.tool()
def construction_axis(method: str = 'edge', edge: str = '', points: list[str] = [],
                      face: str = '') -> dict:
    """Create a construction axis. method: "edge" (linear edge token), "two_points"
    (2 point tokens), "cylinder" (cylindrical face token). Returns an axis token."""
    return _call('construction_axis', method=method, edge=edge or None,
                 points=points, face=face or None)


@mcp.tool()
def construction_point(method: str = 'at_point', point: str = '', edges: list[str] = [],
                       edge: str = '', plane: str = '', path: str = '',
                       ratio: float = 0.5) -> dict:
    """Create a construction point. method: "at_point" (vertex/sketch-point token),
    "two_edges" (2 edge tokens), "edge_plane" (edge token + plane),
    "distance_on_path" (edge/sketch-curve token in `path` + `ratio` 0..1 along
    it — 0=start, 1=end; Fusion July 2026+)."""
    return _call('construction_point', method=method, point=point or None,
                 edges=edges, edge=edge or None, plane=plane or None,
                 path=path or None, ratio=ratio)


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


@mcp.tool()
def sketch_blend_curve(sketch: str, curve1: str, curve2: str,
                       end1: str = '', end2: str = '',
                       curvature: bool = False) -> dict:
    """Bridge two OPEN sketch curves with a smooth fitted spline (Fusion July
    2026+). Ends are picked automatically (closest endpoints) unless end1/end2
    ("start"|"end") force a side. curvature=True gives a curvature-continuous
    G2 blend (default is tangent/G1)."""
    return _call('sketch_blend_curve', sketch=sketch, curve1=curve1,
                 curve2=curve2, end1=end1 or None, end2=end2 or None,
                 curvature=curvature)


@mcp.tool()
def auto_constrain(sketch: str) -> dict:
    """Run Fusion's AutoConstrain on a sketch (2026+): adds the geometric
    constraints a human would (horizontal/vertical/coincident/...) in one
    call — stabilises imported DXF or quickly-drawn geometry before
    dimensioning."""
    return _call('auto_constrain', sketch=sketch)


@mcp.tool(**_annot(readOnlyHint=True))
def sketch_status(sketch: str = '') -> dict:
    """Pre-flight a sketch before sweep/loft/shell — most failed attempts
    trace back to an open or missing profile. Reports profile count,
    fully-constrained state, curve/construction counts and OPEN ENDPOINTS
    (mm positions where exactly one curve ends — exactly where a closing
    segment or coincident constraint is missing; open chains never form
    profiles). Pass a sketch token, or omit to check every root sketch."""
    return _call('sketch_status', sketch=sketch or None)


# --------------------------------------------------------------------------- #
# Advanced features
# --------------------------------------------------------------------------- #
@mcp.tool()
def loft(profiles: list[str], rails: list[str] = [], operation: str = 'new',
         validate_only: bool = False, include_screenshot: bool = False):
    """Loft through 2+ profile tokens (ordered). Optional rails (curve/edge
    tokens) guide the shape. operation: new|join|cut|intersect.
    validate_only=True reports whether the loft would succeed (and what it
    would produce) WITHOUT keeping it — pre-flight profile compatibility
    before committing (pair with sketch_status)."""
    return _with_screenshot(
        _call('loft', profiles=profiles, rails=rails, operation=operation,
              validate_only=validate_only or None), include_screenshot)


@mcp.tool()
def sweep(profile: str, path: str, twist_angle: float = 0.0,
          operation: str = 'new', validate_only: bool = False,
          include_screenshot: bool = False):
    """Sweep a profile token along a path (curve/edge token). twist_angle in deg.
    operation: new|join|cut|intersect. validate_only=True reports whether the
    sweep would succeed WITHOUT keeping it — pre-flight a doubtful
    profile/path pair (pair with sketch_status)."""
    return _with_screenshot(
        _call('sweep', profile=profile, path=path, twist_angle=twist_angle,
              operation=operation, validate_only=validate_only or None),
        include_screenshot)


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


@mcp.tool(**_annot(readOnlyHint=True))
def thread_types() -> dict:
    """List available thread standards: defaults, all/public built-in types,
    and (Fusion July 2026+) custom thread libraries hosted on the team hub
    with their thread types."""
    return _call('thread_types')


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


@mcp.tool()
def as_built_joint(occ0: str, occ1: str, motion: str = 'rigid',
                   geometry: str = '', axis: str = 'Z') -> dict:
    """Joint two occurrences WHERE THEY ALREADY SIT — no geometry snapping — the
    right tool for imported/positioned assemblies (plain `joint` would move the
    parts together). occ0/occ1 are occurrence tokens; motion as in `joint`;
    `geometry` is a face/edge token for the pivot (needed for revolute/slider/
    cylindrical/etc., omit for rigid)."""
    return _call('as_built_joint', occ0=occ0, occ1=occ1, motion=motion,
                 geometry=geometry or None, axis=axis)


@mcp.tool()
def joint_origin(geometry: str, name: str = '') -> dict:
    """Create a named joint origin at a geometry token (face/edge/vertex/sketch
    point) — a stable, explicit reference other joints can snap to."""
    return _call('joint_origin', geometry=geometry, name=name or None)


@mcp.tool()
def contact_set(tokens: list[str] = [], action: str = 'add',
                name: str = '') -> dict:
    """Make driven joints respect physical contact instead of parts passing
    through each other. action="add": create a contact set from 2+ occurrence/
    body tokens; action="all"/"none": toggle global all-contact. Pairs with
    drive_joint + interference to sweep a mechanism through its range."""
    return _call('contact_set', tokens=tokens, action=action, name=name or None)


# --------------------------------------------------------------------------- #
# Materials, measurement, import, timeline
# --------------------------------------------------------------------------- #
@mcp.tool()
def set_material(body: str, material: str, library: str = '') -> dict:
    """Assign a physical material (e.g. "Steel", "Aluminum 6061") to a body
    token — changes its computed mass. Optional library name to disambiguate.
    Use list_materials to find exact names."""
    return _call('set_material', body=body, material=material, library=library or None)


@mcp.tool()
def set_appearance(body: str, appearance: str, library: str = '') -> dict:
    """Assign an appearance (colour/finish) to a body token. Optional library.
    Use list_appearances to find exact names."""
    return _call('set_appearance', body=body, appearance=appearance,
                 library=library or None)


@mcp.tool(**_annot(readOnlyHint=True))
def list_materials(filter: str = '', limit: int = 200) -> dict:
    """Browse material names for set_material: document materials, favourites and
    the shipped libraries. filter = name substring (e.g. "alum"); limit caps."""
    return _call('list_materials', filter=filter or None, limit=limit)


@mcp.tool(**_annot(readOnlyHint=True))
def list_appearances(filter: str = '', limit: int = 200) -> dict:
    """Browse appearance names for set_appearance (document + shipped libraries).
    filter = name substring; limit caps the result."""
    return _call('list_appearances', filter=filter or None, limit=limit)


@mcp.tool()
def create_appearance(name: str, r: int | None = None, g: int | None = None,
                      b: int | None = None, alpha: int = 255,
                      roughness: float | None = None, base: str = '',
                      library: str = '') -> dict:
    """Create a custom appearance in the document (copy + recolour): r/g/b
    0-255 (optional alpha), roughness 0..1 (0 = polished, 1 = matte), base =
    appearance name to copy (default: a matte plastic; library narrows the
    lookup). Any exact colour without leaving the chat — assign it with
    set_appearance(body, name) afterwards."""
    return _call('create_appearance', name=name, r=r, g=g, b=b, alpha=alpha,
                 roughness=roughness, base=base or None,
                 library=library or None)


@mcp.tool()
def insert_fastener(size: str = 'M6', length: float = 20.0,
                    thread: bool = True, native: bool = True) -> dict:
    """Insert an ISO 4762 socket-head cap screw as its own component — the
    native content-library fastener when this Fusion build ships it (v2704+),
    else modelled parametrically (head + shank + hex socket + optional
    cosmetic thread). size: M3|M4|M5|M6|M8|M10|M12; length mm (shank under
    the head); native=False forces the parametric path. Real hardware for
    assemblies/BOM; joint it with as_built_joint."""
    return _call('insert_fastener', size=size, length=length, thread=thread,
                 native=native)


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
    index/name/suppressed) or "rollback" (move the marker to `position`).

    After action="rollback", tokens created by features beyond `position` are
    invalid (their geometry is rolled out) — re-run get_state/query_entities
    before reusing tokens, and move the marker back to the end before resuming
    edits."""
    return _call('timeline', action=action, position=position)


@mcp.tool()
def suppress_feature(feature: str, suppress: bool = True) -> dict:
    """Suppress (or unsuppress with suppress=False) a feature token in the
    timeline. Parametric designs only."""
    return _call('suppress_feature', feature=feature, suppress=suppress)


@mcp.tool()
def timeline_builder(action: str = 'start', body: str = '',
                     timeout: float = 60.0) -> dict:
    """Rebuild an editable parametric timeline from a bare BRep body — an
    imported STEP becomes a design with real features (Timeline Builder
    cloud service, Fusion July 2026+). action="start" (body = body token;
    waits up to `timeout` s for the cloud job), "status" (poll a running
    job), "open" (activate the produced document — then call get_state, old
    tokens are invalid). Needs network access; large bodies can take
    minutes, so start + status is the usual flow."""
    return _call('timeline_builder', action=action, body=body or None,
                 timeout=timeout)


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


# NOT readOnlyHint: path lets the model overwrite an arbitrary PNG on disk.
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


# NOT readOnlyHint: its whole purpose is writing a file at the model-chosen path.
@mcp.tool()
def capture_to_file(path: str, width: int = 1024, height: int = 768,
                    direction: str = 'current', fit: bool = False) -> dict:
    """Save the viewport to a PNG file WITHOUT returning the image bytes. Use
    when you just need the file (e.g. to attach later) and don't need to see it
    now — avoids a large base64 payload. direction/fit as in `screenshot`."""
    return fusion.call('screenshot', {'path': path, 'width': width,
                                      'height': height, 'direction': direction,
                                      'fit': fit, 'return_base64': False})


@mcp.tool(**_annot(readOnlyHint=True))
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
def batch(operations: list[dict], stop_on_error: bool = True,
          include_screenshot: bool = False):
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
    fillet, ...). Both "run_fusion_code" and "run_code" work as the op for the
    escape hatch. "$alias.path" supports .key and [i] including negative indices
    ([-1]); an unparseable path raises rather than silently mis-resolving.
    Returns a per-operation result list. include_screenshot=True also attaches
    an iso screenshot after the whole batch — verify a multi-step build with
    zero extra calls."""
    return _with_screenshot(
        _call('batch', operations=operations, stop_on_error=stop_on_error),
        include_screenshot)


@mcp.tool()
def run_fusion_code(code: str) -> dict:
    """Execute an arbitrary Fusion 360 Python API snippet on the main thread —
    the power tool for anything the curated tools don't cover, in ONE round trip.

    In scope: adsk, app, ui, design, root, math, MM (mm->cm factor), registry,
    reg(kind, obj) -> token, tok(token) -> LIVE object (use tokens from
    get_state/query_entities directly in code), store(name, obj) / fetch(name)
    (keep any object across snippets for the session), and mm/degree helpers
    returning LIVE objects:
        pt(x_mm, y_mm, z_mm=0), mm(v), vmm(v)->ValueInput, deg(d)->radians,
        new_sketch(plane), rect(sk, x1,y1,x2,y2), circle(sk, cx,cy,r),
        extrude_profile(profile, dist_mm, operation='new', symmetric=False)
    Assign to `result` (JSON-serialisable) to return a value; stdout is captured.
    Unsure of a property name? Check first with api_introspect.

    Example (a 40x20x10 mm block in a few lines):
        sk = new_sketch("XY"); rect(sk, 0, 0, 40, 20)
        f = extrude_profile(sk.profiles.item(0), 10)
        result = f.bodies.item(0).name

    Example (continue on an existing body across calls):
        body = tok("bdy1"); store("base", body)
        # ...later snippet: body = fetch("base")
    """
    return _call('run_code', code=code)


@mcp.tool(**_annot(readOnlyHint=True))
def api_introspect(target: str = 'adsk.fusion', query: str = '',
                   limit: int = 80) -> dict:
    """Explore the Fusion API surface before writing run_fusion_code: list an
    object's members (properties/methods/enum values) with one-line docs.
    target: an entity token ("bdy1"), a stored object ("$name" from
    store(...)), or a dotted path ("adsk.fusion.ExtrudeFeatures",
    "adsk.drawing", "adsk.cam"). `query` filters member names by substring —
    e.g. target="bdy1", query="area". Reads the class, never evaluates live
    properties."""
    return _call('api_introspect', target=target, query=query or None,
                 limit=limit)


# --------------------------------------------------------------------------- #
# BOM, sketch text / engraving, sheet metal, meshes, drawings
# --------------------------------------------------------------------------- #
# NOT readOnlyHint: csv_path writes/overwrites a file at the model-chosen path.
@mcp.tool()
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
def emboss(profile: str, depth: float, engrave: bool = True,
           include_screenshot: bool = False):
    """Engrave (engrave=True, cuts into the solid below the sketch plane) or
    emboss (engrave=False, raises material above it) a sketch-text or profile
    token, `depth` mm deep. Typical flow: sketch on a face -> sketch_text ->
    emboss."""
    return _with_screenshot(
        _call('emboss', profile=profile, depth=depth, engrave=engrave),
        include_screenshot)


@mcp.tool()
def flat_pattern(face: str = '', body: str = '') -> dict:
    """Create (or reuse) the flat pattern of a sheet-metal body. Pass the
    stationary planar face token, or just the body token (its largest planar
    face is used). The body must be sheet metal (uniform thickness)."""
    return _call('flat_pattern', face=face or None, body=body or None)


@mcp.tool()
def export_flat_pattern(path: str, face: str = '', body: str = '',
                        format: str = 'dxf') -> dict:
    """Export the flat pattern for fabrication: format "dxf" (2D outline for
    laser/waterjet) or "step" (3D flat solid, Fusion July 2026+). Creates the
    flat pattern first when a face/body token is given and none exists yet."""
    return _call('export_flat_pattern', path=path, face=face or None,
                 body=body or None, format=format)


@mcp.tool()
def fold(face: str, bend_line: str, angle: float = 90.0, radius: float = 0.0,
         corner_relief: bool = False, include_screenshot: bool = False):
    """Fold a sheet-metal body along a bend line (Fusion July 2026+). `face` is
    the stationary face token, `bend_line` a sketch-line token drawn across it
    (create_sketch on the face + sketch_line). angle in degrees, optional bend
    radius in mm (0 = sheet-metal rule default). corner_relief=False forces
    relief off; True forces it on."""
    # Forward corner_relief untouched: `or None` would collapse an explicit
    # False to None and make "relief off" unreachable.
    return _with_screenshot(
        _call('fold', face=face, bend_line=bend_line, angle=angle,
              radius=radius or None, corner_relief=corner_relief),
        include_screenshot)


@mcp.tool()
def join_by_bend(edge_a: str, edge_b: str, radius: float = 0.0) -> dict:
    """Join two sheet-metal bodies with a bend between two linear edges of
    DIFFERENT bodies (Fusion July 2026+). Pick the edges with query_entities
    kind="edges". Optional bend radius in mm (0 = rule default)."""
    return _call('join_by_bend', edge_a=edge_a, edge_b=edge_b,
                 radius=radius or None)


@mcp.tool()
def corner_closure(edge_a: str, edge_b: str, gap: float | None = None,
                   overlap: float | None = None, flip: bool = False,
                   transition: str = '',
                   width_aligned: bool | None = None) -> dict:
    """Close the corner where two sheet-metal flanges meet (Fusion July
    2026+). edge_a/edge_b: the two flange edges that face each other across
    the corner (pick with query_entities kind="edges"). gap in mm; overlap
    0..1 switches from symmetric-gap to overlap alignment (flip puts the
    other flange on top); transition: smooth|straight|trim. Reports whether
    Fusion treated it as a two- or three-bend corner."""
    return _call('corner_closure', edge_a=edge_a, edge_b=edge_b, gap=gap,
                 overlap=overlap, flip=flip, transition=transition or None,
                 width_aligned=width_aligned)


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
def mesh_to_brep(meshes: list[str] = [], method: str = 'faceted') -> dict:
    """Convert mesh bodies (tokens; all meshes when omitted) into solid BRep
    bodies so every solid tool works on them (combine, split_body, measure,
    export STEP...). method: "faceted" (triangles as-is), "prismatic"
    (recognises planes/cylinders — much cleaner solids from machine-part
    scans; try it first), "organic" (T-Spline fit for freeform shapes).
    Dense scans should go through mesh_reduce first."""
    return _call('mesh_to_brep', meshes=meshes, method=method)


@mcp.tool()
def mesh_reduce(meshes: list[str] = [], target_faces: int = 0,
                proportion: float = 0.0, max_deviation: float = 0.0,
                method: str = 'adaptive') -> dict:
    """Reduce a scan's triangle count (all meshes when tokens omitted) before
    converting or sectioning. Pick ONE target: target_faces (absolute count),
    proportion (percent of original, 0-100) or max_deviation (mm; the default,
    0.05 mm, preserves shape). method: adaptive (keeps detail) | uniform."""
    return _call('mesh_reduce', meshes=meshes, target_faces=target_faces or None,
                 proportion=proportion or None,
                 max_deviation=max_deviation or None, method=method)


@mcp.tool()
def mesh_remesh(meshes: list[str] = [], density: float = -1.0,
                shape_preservation: float = -1.0,
                preserve_boundaries: int = -1,
                preserve_sharp_edges: int = -1,
                method: str = 'adaptive') -> dict:
    """Regenerate mesh triangulation (fixes slivers/degenerate triangles that
    break mesh_to_brep). Optional settings (leave at -1 for Fusion defaults,
    applied best-effort on Preview builds): density 0-1, shape_preservation
    0-1, preserve_boundaries / preserve_sharp_edges (0/1), method
    adaptive|uniform."""
    return _call('mesh_remesh', meshes=meshes,
                 density=None if density < 0 else density,
                 shape_preservation=None if shape_preservation < 0
                 else shape_preservation,
                 preserve_boundaries=None if preserve_boundaries < 0
                 else bool(preserve_boundaries),
                 preserve_sharp_edges=None if preserve_sharp_edges < 0
                 else bool(preserve_sharp_edges),
                 method=method)


@mcp.tool()
def mesh_plane_cut(mesh: str, plane: str = 'XY', offset: float = 0.0,
                   mode: str = 'trim') -> dict:
    """Cut a mesh with a plane at `offset` mm — chop scanner-table junk off a
    scan, or keep half of a symmetric part and mirror it later. mode: "trim"
    (discard one side) or "split" (keep both as separate meshes)."""
    return _call('mesh_plane_cut', mesh=mesh, plane=plane, offset=offset,
                 mode=mode)


@mcp.tool()
def canvas_add(image: str, plane: str = 'XY', width_mm: float = 0.0,
               opacity: int = 100) -> dict:
    """Attach an image file (photo of a part) as a canvas on a plane —
    reverse-engineer from a photo by tracing it with sketches. width_mm scales
    the image to a known width; the user can fine-tune with right-click >
    Calibrate in Fusion."""
    return _call('canvas_add', image=image, plane=plane,
                 width_mm=width_mm or None, opacity=opacity)


@mcp.tool()
def mesh_section(mesh: str, plane: str = 'XY', offset: float = 0.0) -> dict:
    """Slice a mesh with a plane ("XY"/"XZ"/"YZ" or a plane token) at `offset`
    mm, producing a section sketch of the scan's cross-section — trace it with
    sketch_polyline/sketch_spline and dimensions to rebuild the part
    parametrically."""
    return _call('mesh_section', mesh=mesh, plane=plane, offset=offset)


@mcp.tool(**_annot(readOnlyHint=True))
def mesh_compare(mesh_a: str, mesh_b: str, tolerance: float = 0.2) -> dict:
    """Native signed-distance deviation between two mesh bodies IN the design
    (Fusion July 2026+) — no file round-trip. Per-node distance stats in mm
    (mean/rms/p50/p90/p99/max, signed min/max) plus the fraction within
    `tolerance` mm. The fast verification loop for reverse engineering; for
    file-vs-file comparison (or older Fusion) use scan_deviation."""
    return _call('mesh_compare', mesh_a=mesh_a, mesh_b=mesh_b,
                 tolerance=tolerance)


# NOT readOnlyHint: writes a file at the model-chosen path.
@mcp.tool()
def mesh_export(mesh: str, path: str) -> dict:
    """Write ONE mesh body (token) to an STL or OBJ file in mm (format from
    the extension) — the bridge from a mesh living in Fusion to the
    server-side scan tools (scan_analyze, scan_align, scan_deviation,
    scan_cavity_sections, print_check), which work on files."""
    return _call('mesh_export', mesh=mesh, path=path)


# NOT readOnlyHint: the export branch writes a file at the model-chosen path.
@mcp.tool()
def face_groups(mesh: str, group: int = -1, export_path: str = '') -> dict:
    """List a mesh body's face groups (segmentation regions): tempId, area,
    centroid, bounding box, planarity. Pass group=<tempId> plus export_path
    (.stl/.obj) to also write that group's triangles to a file for
    server-side surface fitting (needs a Preview API — Fusion Sep 2024+; the
    error says so when absent). tempIds are stable only while the document
    stays open and the mesh unmodified."""
    return _call('face_groups', mesh=mesh,
                 group=None if group < 0 else group,
                 export_path=export_path or None)


@mcp.tool()
def mesh_repair(meshes: list[str] = [], mode: str = 'stitch',
                quality: str = 'fast', density: int = 0,
                offset: float = 0.0) -> dict:
    """Repair scan defects (holes, floaters, non-manifold junk) on mesh
    bodies (all meshes when tokens omitted). mode: "stitch" (close gaps,
    remove debris — default) or "rebuild" (full re-wrap: quality
    fast|accurate, density 8-256, offset mm grows the skin). Preview
    mesh-feature API — falls back to a clear error on builds without it."""
    return _call('mesh_repair', meshes=meshes, mode=mode, quality=quality,
                 density=density or None, offset=offset or None)


@mcp.tool()
def mesh_smooth(meshes: list[str] = [], smoothness: float = -1.0) -> dict:
    """Smooth mesh bodies (all when tokens omitted) to soften scanner noise
    before converting. smoothness 0-1; leave at -1 for Fusion's default.
    Preview mesh-feature API."""
    return _call('mesh_smooth', meshes=meshes,
                 smoothness=None if smoothness < 0 else smoothness)


@mcp.tool()
def mesh_shell(thickness: float, meshes: list[str] = []) -> dict:
    """Hollow mesh bodies into an even wall of `thickness` mm (a scanned
    outer skin becomes a printable shell). Preview mesh-feature API."""
    return _call('mesh_shell', thickness=thickness, meshes=meshes)


@mcp.tool()
def mesh_separate(meshes: list[str] = []) -> dict:
    """Split mesh bodies (all when tokens omitted) into their disconnected
    shells — one mesh body per physical part after a multi-part scan.
    Preview mesh-feature API."""
    return _call('mesh_separate', meshes=meshes)


@mcp.tool()
def canvas_calibrate(canvas: str, p1: list[float], p2: list[float],
                     distance: float, rotate_to_deg: float = -9999.0,
                     move_p1_to: list[float] = []) -> dict:
    """Two-point canvas calibration, fully scripted (the UI's right-click
    Calibrate has no API): p1/p2 are [x, y] mm in the canvas plane's sketch
    coordinates (read them off a screenshot or sketch points placed over two
    known features) and `distance` is the true mm between those features.
    Scales the canvas uniformly about p1; rotate_to_deg (leave at -9999 to
    skip) also rotates so p1->p2 points at that angle; move_p1_to=[x, y]
    then puts p1 on a target point."""
    return _call('canvas_calibrate', canvas=canvas, p1=p1, p2=p2,
                 distance=distance,
                 rotate_to_deg=None if rotate_to_deg <= -9998 else rotate_to_deg,
                 move_p1_to=move_p1_to or None)


@mcp.tool(**_annot(readOnlyHint=True))
def canvas_list() -> dict:
    """List the canvases in the design with tokens for canvas_calibrate /
    canvas_update / canvas_delete (name, opacity, image file)."""
    return _call('canvas_list')


@mcp.tool()
def canvas_update(canvas: str, opacity: int = -1, name: str = '',
                  displayed_through: int = -1, selectable: int = -1,
                  flip_h: bool = False, flip_v: bool = False) -> dict:
    """Adjust a canvas (token): opacity 0-100, rename, displayed_through /
    selectable (0/1; -1 leaves unchanged), flip_h/flip_v mirror the image in
    place."""
    return _call('canvas_update', canvas=canvas,
                 opacity=None if opacity < 0 else opacity,
                 name=name or None,
                 displayed_through=None if displayed_through < 0
                 else bool(displayed_through),
                 selectable=None if selectable < 0 else bool(selectable),
                 flip_h=flip_h, flip_v=flip_v)


@mcp.tool()
def canvas_delete(canvas: str) -> dict:
    """Remove a canvas (token) from the design."""
    return _call('canvas_delete', canvas=canvas)


@mcp.tool()
def show_message(text: str, title: str = 'FusionMCP') -> dict:
    """Show a native popup dialog inside Fusion so the USER sees a message
    without reading the chat — announce a finished long job or something
    that needs their attention at the machine. Modal: blocks further tool
    calls until dismissed, so use sparingly and keep the text short."""
    return _call('show_message', text=text, title=title)


@mcp.tool()
def import_svg(path: str, sketch: str = '', plane: str = 'XY',
               scale: float = 0.0, flip_h: bool = False,
               flip_v: bool = False) -> dict:
    """Import an SVG file's curves into a sketch — an existing one (token)
    or a new sketch on `plane`. scale applies a uniform factor; flip_h/flip_v
    mirror. For photos use photo_rectify + photo_to_sketch (DXF) instead;
    this is for genuine vector art (logos, gasket outlines)."""
    return _call('import_svg', path=path, sketch=sketch or None, plane=plane,
                 scale=scale or None, flip_h=flip_h, flip_v=flip_v)


@mcp.tool()
def create_drawing(template: str = '', headless: bool = True,
                   sheet_size: str = '', orientation: str = '',
                   standard: str = '', drawing_units: str = '',
                   auto_dimension: int = -1, flat_pattern: int = -1) -> dict:
    """Create a drawing for the active design. Fusion July 2026+ does this
    fully headlessly (DrawingManager API): optional `template` file,
    sheet_size ("A0".."A4" ISO or "A".."E" ASME), orientation
    ("landscape"|"portrait"), standard ("iso"|"asme"), drawing_units
    ("mm"|"in"). auto_dimension=1 asks the generator to place dimensions
    automatically and flat_pattern=1 to add sheet-metal flat-pattern sheets
    (July 2026 preview automation; -1 leaves Fusion defaults). Older
    versions fall back to a bare drawing document or the "Drawing from
    Design" dialog. Then export with drawing_export; add tables with
    drawing_table. For 2D output without a sheet use export_sketch_dxf /
    export_flat_pattern."""
    return _call('create_drawing', template=template or None, headless=headless,
                 sheet_size=sheet_size or None, orientation=orientation or None,
                 standard=standard or None, drawing_units=drawing_units or None,
                 auto_dimension=None if auto_dimension < 0 else bool(auto_dimension),
                 flat_pattern=None if flat_pattern < 0 else bool(flat_pattern))


@mcp.tool()
def drawing_table(data: list[list[str]], title: str = '',
                  position_mm: list[float] = []) -> dict:
    """Add a custom table to the active drawing's sheet (Fusion July 2026+
    preview): `data` is rows of cell strings, first row = header — cut
    lists, parameter tables, mini-BOMs right on the sheet. Open/create the
    drawing first (create_drawing)."""
    return _call('drawing_table', data=data, title=title or None,
                 position_mm=position_mm or None)


@mcp.tool()
def loft_from_sections(sections: list[dict], plane: str = 'XY',
                       operation: str = 'new', rail: bool = False) -> dict:
    """Loft a body straight from scan_sections / scan_cavity_sections output:
    per section {level_mm, points_mm: [[u,v],...]} this creates the offset
    plane + closed fitted spline and lofts all profiles in one call (the
    manual multi-batch rebuild, automated). rail=True threads a centreline
    rail through each section's first point — use it when profiles are
    smooth and the loft shows scalloping. operation: new|join|cut|intersect.
    Verify cavity fits afterwards with scan_fit_check."""
    return _call('loft_from_sections', sections=sections, plane=plane,
                 operation=operation, rail=rail)


@mcp.tool()
def silhouette(body: str = '', mesh: str = '', direction: str = 'z',
               plane: str = 'XY') -> dict:
    """EXPERIMENTAL (Fusion April 2026+): project a body's or mesh's outline
    along a view direction into a new sketch — cutting templates and gasket
    outlines from any angle; export with export_sketch_dxf. Pass body= (BRep
    token) or mesh= (mesh token); direction "x"|"y"|"z"."""
    return _call('silhouette', body=body or None, mesh=mesh or None,
                 direction=direction, plane=plane)


@mcp.tool()
def sketch_doctor(sketch: str = '', fix: bool = False) -> dict:
    """Sketch health check and repair in one call: per sketch (token, or all)
    — fully-constrained state, Fusion's health state and error message,
    profiles and open endpoints; fix=True also runs auto-constrain on
    under-constrained sketches and reports the constraint delta. Finish the
    remaining degrees of freedom with sketch_dimension."""
    return _call('sketch_doctor', sketch=sketch or None, fix=fix)


@mcp.tool()
def fastener_update_size() -> dict:
    """Refresh inserted Content-Library fasteners after host geometry changed
    (Fusion July 2026+ preview): re-runs sizing on every fastener occurrence
    so screw diameter/length match the plates again."""
    return _call('fastener_update_size')


@mcp.tool()
def drawing_export(path: str, format: str = 'pdf') -> dict:
    """Export the active drawing document to "pdf" or "dxf" at `path` — the
    final documentation step after create_drawing (and any manual sheet
    tweaks the user made)."""
    return _call('drawing_export', path=path, format=format)


# --------------------------------------------------------------------------- #
# Scan analysis — runs IN THE SERVER PROCESS (no Fusion needed, no load on
# Fusion's UI thread). Requires the optional "re" extras (numpy, trimesh,
# pyransac3d); returns a clear install hint when they are missing.
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def scan_analyze(path: str, max_primitives: int = 8) -> dict:
    """Analyse a scan/mesh file (STL/OBJ/3MF, mm) WITHOUT Fusion: size, volume,
    symmetry planes, RANSAC-fitted planes/cylinders/spheres with hole-vs-boss
    classification, and wall thickness. The output is a rebuild plan: turn the
    primitives into sketches/extrudes/holes with the parametric tools, then
    check the result with scan_deviation."""
    try:
        return scan.analyze(path, max_primitives=max_primitives)
    except Exception as exc:  # noqa: BLE001 - surface as a readable tool error
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def scan_sections(path: str, axis: str = 'z', count: int = 8,
                  heights: list[float] = [], max_points: int = 80) -> dict:
    """Slice a scan file into cross-sections perpendicular to a world axis
    (x|y|z) — at `count` even heights or explicit `heights` (mm). Returns per
    slice: fitted circles (center/radius) and decimated polylines in
    sketch-plane coordinates, ready to rebuild with construction_plane +
    sketch_circle/sketch_polyline in one batch."""
    try:
        return scan.sections(path, axis=axis, count=count,
                             heights=heights or None, max_points=max_points)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def scan_deviation(scan_path: str, model_path: str, samples: int = 4000,
                   tolerance: float = 0.2) -> dict:
    """Compare the original scan with the rebuilt model: export the solid with
    export("stl", ...) first, then call this. Returns two-way surface-distance
    stats (mean/rms/p50/p90/p99/max) and the fraction within `tolerance` mm —
    the verification loop of reverse engineering: rebuild, measure, refine."""
    try:
        return scan.deviation(scan_path, model_path, samples=samples,
                              tolerance=tolerance)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def print_check(path: str, bed_x: float = 256.0, bed_y: float = 256.0,
                bed_z: float = 256.0, overhang_deg: float = 45.0,
                min_wall: float = 1.0, nozzle: float = 0.4) -> dict:
    """Score an STL/OBJ/3MF file for FDM 3D printing WITHOUT Fusion: bed fit
    across orientations (bed_x/y/z mm), unsupported-overhang area for a Z build
    (faces steeper than overhang_deg below horizontal), thin walls vs
    min_wall/nozzle, watertightness, and actionable recommendations. Backs the
    prepare_for_3d_print prompt. Needs the 're' extras (numpy/trimesh)."""
    try:
        return scan.print_check(path, bed=(bed_x, bed_y, bed_z),
                                overhang_deg=overhang_deg, min_wall=min_wall,
                                nozzle=nozzle)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: writes the aligned mesh at the model-chosen path.
@mcp.tool()
def scan_align(scan_path: str, model_path: str, out_path: str = '',
               samples: int = 3000, scale: bool = False) -> dict:
    """Rigidly align a scan file onto a model file (both mm) with
    deterministic ICP (centroid + PCA seeds). Run this BEFORE scan_deviation
    or scan_fit_check whenever the scan and the model are not already in one
    coordinate frame. Returns the 4x4 transform and before/after RMS;
    out_path also writes the aligned scan (STL/OBJ by extension).
    scale=True additionally solves a uniform scale (scanner calibration)."""
    try:
        return scan.align(scan_path, model_path, out_path=out_path or None,
                          samples=samples, scale=scale)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def scan_fit_check(scan_path: str, model_path: str, clearance_mm: float = 0.0,
                   max_points: int = 20000) -> dict:
    """Verify a designed part (model file, e.g. export("stl")) against the
    scanned object it must fit (scan file), both mm and already aligned
    (scan_align first if not): per-scan-vertex distance to the model, signed
    inside/outside when the model is watertight. Reports collisions (scan
    points inside the part — it would not seat), penetration depth, and
    clearance percentiles; clearance_mm adds a fraction-below-target. The
    printed-part fit gate before committing a print."""
    try:
        return scan.fit_check(scan_path, model_path, clearance_mm=clearance_mm,
                              max_points=max_points)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def scan_cavity_sections(scan_path: str, axis: str = 'z', spacing: float = 2.0,
                         margin: float = 2.0, cumulative: str = 'above',
                         lookback: float = -1.0,
                         level_range: list[float] = []) -> dict:
    """Design a straight-insertion cavity around a scanned object: per-level
    convex-hull outlines, cumulative along the insertion axis, offset by
    `margin` mm and guaranteed monotone — loft them and the object drops
    straight in. cumulative="above" fits a cover lowered onto the object;
    "below" fits a pocket entered from +axis. level_range=[start, end] limits
    the levels (mm); lookback (-1 = one spacing) swallows points one section
    behind to stop lofts pinching between sections. Sections already share a
    start anchor + CCW order: per section make construction_plane(offset) +
    ONE closed fitted sketch_spline through points_mm (never a polyline —
    polyline lofts bead), then loft and combine-cut. Verify with
    scan_fit_check. Convex hulls only — concave openings need the raster
    approach by hand."""
    try:
        start = level_range[0] if len(level_range) > 0 else None
        end = level_range[1] if len(level_range) > 1 else None
        return scan.cavity_sections(
            scan_path, axis=axis, spacing=spacing, margin=margin,
            cumulative=cumulative,
            lookback=None if lookback < 0 else lookback,
            start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: writes the converted mesh at the model-chosen path.
@mcp.tool()
def scan_convert(path: str, out_path: str = '', fmt: str = 'stl') -> dict:
    """Convert a mesh file Fusion cannot import (GLB/GLTF/PLY/OFF — typical
    phone-scan exports) to STL or OBJ, flattening scenes, then import the
    result with import_mesh. Warns when the extents suggest non-mm units
    (GLB is metres by convention: import_mesh(units="m"))."""
    try:
        return scan.convert(path, out_path=out_path or None, fmt=fmt)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# --------------------------------------------------------------------------- #
# Photo -> CAD — runs IN THE SERVER PROCESS. Requires the optional "photo"
# extras (opencv-contrib-python-headless, ezdxf); clear install hint when
# missing.
# --------------------------------------------------------------------------- #
# NOT readOnlyHint: writes the rectified image at the model-chosen path.
@mcp.tool()
def photo_rectify(image: str, out_path: str = '', marker: str = '4x4_50',
                  marker_size_mm: float = 50.0, mm_per_px: float = 0.2,
                  margin_mm: float = 10.0, ref_points: list = [],
                  ref_width_mm: float = 0.0, ref_height_mm: float = 0.0,
                  scale_points: list = [], known_mm: float = 0.0,
                  undistort: str = 'off', refine: bool = True) -> dict:
    """Remove perspective from a photo of a flat part and fix the scale:
    after this every pixel is exactly mm_per_px millimetres in the part's
    plane. Reference, best first: (1) printed square marker(s) IN the plane
    (marker: ArUco dictionary, default "4x4_50", or "qr"; marker_size_mm =
    printed side; SEVERAL same-size markers refine the fit and report their
    agreement, and undistort="auto" then also removes lens distortion);
    (2) no marker — ref_points=[[x,y]x4] pixel corners TL,TR,BR,BL of a
    known rectangle (A4 210x297, bank card 85.6x53.98) + ref_width_mm/
    ref_height_mm; (3) last resort — scale_points=[[x,y],[x,y]] two points
    a known_mm apart (ruler): scale only, NO perspective fix. Writes
    <name>_rect.png (or out_path) and returns mm_per_px for photo_measure,
    photo_to_sketch or canvas_add. Accuracy is real (~0.2-1 mm) only in the
    reference plane."""
    try:
        return photo.rectify(image, out_path=out_path or None, marker=marker,
                             marker_size_mm=marker_size_mm,
                             mm_per_px=mm_per_px, margin_mm=margin_mm,
                             ref_points=ref_points or None,
                             ref_width_mm=ref_width_mm,
                             ref_height_mm=ref_height_mm,
                             scale_points=scale_points or None,
                             known_mm=known_mm, undistort=undistort,
                             refine=refine)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: writes the annotated preview image.
@mcp.tool()
def photo_measure(image: str, mm_per_px: float, segments: list = [],
                  holes: bool = False, snap_px: int = 0,
                  min_diameter_mm: float = 2.0, max_diameter_mm: float = 60.0,
                  sensitivity: int = 30, annotate_path: str = '',
                  include_image: bool = True):
    """Measure a RECTIFIED photo in millimetres — the step that turns
    'I can see the part' into real dimensions. segments:
    [[[x1,y1],[x2,y2]], ...] pixel point pairs -> distances in mm (snap_px
    snaps endpoints to the nearest image edge); holes=true detects circular
    holes (centres, diameters, centre-to-centre bolt spacing; tune with
    min/max_diameter_mm and sensitivity — lower finds more). Run
    photo_rectify first and use ITS output image and mm_per_px. Returns the
    numbers plus an annotated preview image (include_image=false for the
    JSON only) — verify every drawn line/circle sits where intended before
    trusting the numbers."""
    try:
        result = photo.measure(image, mm_per_px, segments=segments or None,
                               holes=holes, snap_px=snap_px,
                               min_diameter_mm=min_diameter_mm,
                               max_diameter_mm=max_diameter_mm,
                               sensitivity=sensitivity,
                               annotate_path=annotate_path or None)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}
    if include_image and isinstance(result, dict) and result.get('annotated'):
        try:
            with open(result['annotated'], 'rb') as fh:
                return [result, Image(data=fh.read(), format='png')]
        except Exception:  # noqa: BLE001 - the preview is best-effort
            pass
    return result


# NOT readOnlyHint: writes the DXF at the model-chosen path.
@mcp.tool()
def photo_to_sketch(image: str, mm_per_px: float, dxf_path: str = '',
                    threshold: float = -1.0, invert: bool = False,
                    epsilon_mm: float = 0.3, min_area_mm2: float = 4.0,
                    holes: bool = True, blur_px: int = 3) -> dict:
    """Vectorise a part silhouette (rectified photo or flat scan) into a DXF
    of closed polylines (mm, Y up), then bring it into Fusion with
    import_file(format="dxf", plane=...) — photo to sketch profiles in two
    calls. Dark part on light background by default (invert=True for the
    opposite); threshold -1 = automatic; epsilon_mm controls simplification;
    min_area_mm2 drops specks; holes=False keeps only outer outlines."""
    try:
        return photo.to_sketch_dxf(image, mm_per_px, dxf_path=dxf_path or None,
                                   threshold=threshold, invert=invert,
                                   epsilon_mm=epsilon_mm,
                                   min_area_mm2=min_area_mm2, holes=holes,
                                   blur_px=blur_px)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# --------------------------------------------------------------------------- #
# Workshop tools — run IN THE SERVER PROCESS: slicing estimates (external
# slicer CLI), fastener data (vendored standards tables), DFM heuristics.
# --------------------------------------------------------------------------- #
# NOT readOnlyHint: runs an external slicer and can write the G-code file.
@mcp.tool()
def print_estimate(model_path: str, profile: str = '', slicer_name: str = 'auto',
                   gcode_out: str = '', material: str = 'pla',
                   price_per_kg: float = 0.0, printer_watts: float = 0.0,
                   energy_price_kwh: float = 0.0) -> dict:
    """Slice an exported STL/3MF with the user's installed slicer
    (PrusaSlicer / OrcaSlicer / Bambu Studio, auto-detected) and report print
    time, filament grams and cost — model to "how long and how much" in one
    call after export("stl", ...). profile: a config exported from the
    slicer GUI (PrusaSlicer .ini, or "machine.json;process.json" for
    Orca/Bambu); without one PrusaSlicer slices with generic defaults.
    gcode_out keeps the G-code. Cost adds price_per_kg (your filament price)
    and optionally printer_watts x energy_price_kwh."""
    try:
        return slicer.estimate(model_path, profile=profile or None,
                               slicer=slicer_name, gcode_out=gcode_out or None,
                               material=material,
                               price_per_kg=price_per_kg or None,
                               printer_watts=printer_watts or None,
                               energy_price_kwh=energy_price_kwh or None)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def fastener_lookup(size: str) -> dict:
    """Metric fastener data (ISO/DIN via the BOLTS tables, mm): coarse pitch,
    tap drill, clearance holes (close/normal/loose), socket/hex head, nut,
    washer and counterbore envelope, heat-set insert hole — everything
    needed to model around a screw. Sizes M2-M12."""
    try:
        return fasteners.lookup(size)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def hole_spec(size: str, kind: str = 'clearance', fit: str = 'normal',
              head: str = 'none', material_thickness: float = 0.0) -> dict:
    """The exact hole to model for a metric screw, ready for the `hole` tool:
    kind "clearance" (fit close|normal|loose), "tapped" (tap drill + thread
    designation) or "heat_set" (FDM brass-insert pocket). head "counterbore"
    (socket head sits flush) or "countersink" adds the head recess;
    material_thickness suggests a bolt length."""
    try:
        return fasteners.hole_spec(
            size, kind=kind, fit=fit, head=head,
            material_thickness=material_thickness or None)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: executes a user-supplied script and writes the export.
@mcp.tool()
def codecad_run(script: str, out_path: str) -> dict:
    """Run a build123d Python script WITHOUT Fusion in the loop and export
    STEP/STL (extension of out_path decides) — millisecond parametric
    prototypes and bracket generators; then import_file(format="step")
    brings a real solid into the design. The script gets `from build123d
    import *` and must assign its final shape to `result`. Same trust model
    as run_fusion_code: the script has full Python access. Needs the
    optional 'codecad' extras (pip install build123d)."""
    try:
        return codecad.run(script, out_path)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: launches a long external reconstruction and writes files.
@mcp.tool()
def photogrammetry_run(images_dir: str, out_obj: str, backend: str = 'auto',
                       simplify_faces: int = 1000000, timeout: int = 7200,
                       detect_markers: bool = False,
                       distances: list = []) -> dict:
    """Reconstruct a 3D mesh from a folder of photos using an installed
    photogrammetry app (RealityScan preferred, Meshroom fallback — detected
    automatically). Needs 20+ sharp overlapping photos of all sides; runs
    minutes to hours. Real-world scale: with RealityScan pass
    distances=[[marker_a, marker_b, mm], ...] between coded targets in the
    scene (+detect_markers=true; solved during alignment, CLI verbs
    unverified live) — otherwise the mesh has ARBITRARY scale: fix it with
    photogrammetry_scale (ArUco markers) or scan_align to a known model.
    Shiny or black parts reconstruct poorly — matte spray helps."""
    try:
        return photogrammetry.run(images_dir, out_obj, backend=backend,
                                  simplify_faces=simplify_faces,
                                  timeout=timeout,
                                  detect_markers=detect_markers,
                                  distances=distances or None)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: writes the scaled mesh copy.
@mcp.tool()
def photogrammetry_scale(mesh: str, images_dir: str, marker_length_mm: float,
                         sfm_path: str = '', marker: str = '4x4_50',
                         out_path: str = '', min_views: int = 2) -> dict:
    """Recover the REAL millimetre scale of a photogrammetry mesh from
    printed ArUco markers that were lying in the scene: detects them in the
    source photos, triangulates their corners with the Meshroom camera poses
    (cameras.sfm — auto-found next to the mesh, or pass sfm_path) and writes
    a rescaled copy (default <mesh>_mm.obj). All markers must share the same
    printed side length (marker_length_mm). Closes the 'arbitrary units'
    gap of photogrammetry_run for the Meshroom backend; check spread_pct in
    the result — >3% means a noisy reconstruction."""
    try:
        return photogrammetry.scale_from_markers(
            mesh, images_dir, marker_length_mm,
            sfm_path=sfm_path or None, marker=marker,
            out_path=out_path or None, min_views=min_views)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def dfm_check(path: str, process: str = 'fdm', axis: str = 'z',
              min_draft_deg: float = 1.0, min_wall: float = 1.0) -> dict:
    """Design-for-manufacturing check of an exported mesh (export("stl")
    first). process: "fdm" (bed fit, overhangs, thin walls), "injection"
    (draft angles vs the pull `axis`, undercut detection by ray occlusion,
    uniform-wall), "cnc3axis" (down-facing surfaces and pockets a straight
    tool cannot reach, flip-setup advice). Transparent trimesh heuristics —
    area fractions, worst offender locations and recommendations."""
    try:
        return dfm.check(path, process=process, axis=axis,
                         min_draft_deg=min_draft_deg, min_wall=min_wall)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# --------------------------------------------------------------------------- #
# FreeCAD bridge — headless freecadcmd subprocess: FEM strength checks,
# neutral-kernel geometry inspection, format conversion. FreeCAD bundles its
# own Python + gmsh + CalculiX, so none of this touches the server process.
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def freecad_info() -> dict:
    """Is FreeCAD installed and FEM-ready? Reports the freecadcmd path,
    version, bundled gmsh/CalculiX solvers and the available material
    presets for freecad_fem. Run this before the other freecad_* tools."""
    try:
        return freecad.info()
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def freecad_inspect(path: str, max_faces: int = 120) -> dict:
    """Second-opinion geometry census of a CAD file through the OpenCascade
    kernel — works on STEP/IGES/BREP/FCStd (per-solid validity, volume,
    bbox and every face's surface type, area, center, plane normal or
    cylinder radius+axis) and meshes (watertightness, self-intersections).
    Use it to verify an export("step") and to pick faces ('Face7') for
    freecad_fem constraints."""
    try:
        return freecad.inspect(path, max_faces=max_faces)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: runs external solvers (long) in a temp sandbox.
@mcp.tool()
def freecad_fem(path: str, fixed: list, loads: list,
                material: str = 'steel', mesh_max_mm: float = 0.0,
                gravity: bool = False, E: float = 0.0, nu: float = 0.0,
                density: float = 0.0, yield_mpa: float = 0.0,
                timeout: int = 900) -> dict:
    """Will this part hold? Linear static FEM (FreeCAD + gmsh + CalculiX)
    on an exported STEP: von Mises max/p95 [MPa], displacement [mm], mass
    and a safety factor vs the material's yield strength. fixed: face specs
    — 'FaceN' from freecad_inspect or bbox keywords xmin/xmax/ymin/ymax/
    zmin/zmax. loads: [{"faces": ["zmax"], "force_n": 200}] (along the face
    normal, pushing; "pull": true flips) or {"faces": [...],
    "pressure_mpa": 2.5}. material: steel|stainless|aluminum|brass|titanium|
    pla|petg|abs|nylon|pc or "custom" (+E [MPa], nu, density [kg/m^3],
    yield_mpa). Printed materials also get safety_factor_printed (~60%,
    layer anisotropy). mesh_max_mm 0 = auto; halve it once to check mesh
    convergence — stress at sharp corners is singular and grows with
    refinement (fillet the corner, judge by p95)."""
    try:
        return freecad.fem_analyze(
            path, fixed, loads, material=material, mesh_max_mm=mesh_max_mm,
            gravity=gravity, E=E, nu=nu, density=density,
            yield_mpa=yield_mpa, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: writes the converted file.
@mcp.tool()
def freecad_convert(in_path: str, out_path: str,
                    linear_deflection_mm: float = 0.1,
                    angular_deflection_deg: float = 15.0) -> dict:
    """Convert CAD files through the OpenCascade kernel: STEP/IGES/BREP/
    FCStd between each other, solid -> STL/OBJ/PLY/3MF (deflection controls
    tessellation quality), mesh -> mesh, or mesh -> faceted STEP/BREP
    reference body. Covers formats Fusion cannot open (BREP, FCStd) and
    gives a slicer-independent tessellation with explicit quality knobs."""
    try:
        return freecad.convert(
            in_path, out_path, linear_deflection_mm=linear_deflection_mm,
            angular_deflection_deg=angular_deflection_deg)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# NOT readOnlyHint: executes a user-supplied script with full access.
@mcp.tool()
def freecad_run(script: str, timeout: int = 600) -> dict:
    """Arbitrary Python in headless FreeCAD (freecadcmd) — the escape hatch
    to everything the dedicated freecad_* tools do not cover (TechDraw
    SVG/DXF drawings, Draft, OCC modeling, FEM variants). FreeCAD/App/Part
    are pre-imported; any FreeCAD module can be imported; assign a
    JSON-serializable `result`. Same trust model as run_fusion_code."""
    try:
        return freecad.run_script(script, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# --------------------------------------------------------------------------- #
# Spare-part mechanical data — vendored standards tables and textbook
# formulas that turn MEASURED dimensions (scan/photo) into INTENTIONAL ones.
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def fit_suggest(measured_mm: float, feature: str = 'shaft',
                application: str = 'sliding', fit: str = '') -> dict:
    """Turn a measured diameter into a proper ISO 286 toleranced spec:
    nearest standard nominal, hole+shaft limits in mm and the resulting
    clearance/interference range. feature: which member was measured
    ("shaft"|"hole"). application: loose_running|running|sliding|
    close_sliding|location|transition|press|heavy_press — or pass an
    explicit fit like "H7/g6". The bridge from scan_analyze/photo_measure
    numbers to dimensions you can put on a drawing."""
    try:
        return mech.fit_suggest(measured_mm, feature=feature,
                                application=application, fit=fit or None)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def bearing_lookup(designation: str = '', bore_mm: float = 0.0,
                   od_mm: float = 0.0, tolerance_mm: float = 0.5) -> dict:
    """Deep-groove ball bearing envelopes (60x/62x/63x/68x/69x miniature,
    6800-6806, 6900-6906, 6000-6010, 6200-6210, 6300-6310): look up "608"
    /"6204ZZ", or identify a bearing from MEASURED seat dimensions (bore
    and/or OD ± tolerance) — "the scanned pocket is Ø21.9x7, what was in
    it?". Includes shaft/housing seat fit advice and FDM compensation."""
    try:
        return mech.bearing_lookup(designation=designation or None,
                                   bore=bore_mm or None, od=od_mm or None,
                                   tolerance=tolerance_mm)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def circlip_lookup(diameter_mm: float, kind: str = 'shaft') -> dict:
    """DIN 471 (external/shaft) and DIN 472 (internal/bore) retaining-ring
    data for Ø3-100: ring thickness, groove diameter, groove width (H13)
    and depth — model the groove straight from the numbers. Non-standard
    diameters return the nearest standard sizes."""
    try:
        return mech.circlip_lookup(diameter_mm, kind=kind)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def oring_gland(cs_mm: float, id_mm: float = 0.0,
                seal: str = 'static_radial') -> dict:
    """O-ring groove design from the cord thickness: depth/width for
    static_radial | dynamic_radial | face seals with standard squeeze and
    ~75-80% fill, nearest standard cross-section (metric + AS568), and —
    with id_mm — bore/groove-root diameters and the stretch check. Measure
    the old ring's cord with photo_measure/scan and model the groove from
    this."""
    try:
        return mech.oring_gland(cs_mm, id_mm=id_mm, seal=seal)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


@mcp.tool(**_annot(readOnlyHint=True))
def belt_calc(profile: str, teeth_small: int, teeth_large: int = 0,
              belt_teeth: int = 0, center_distance_mm: float = 0.0) -> dict:
    """Synchronous-belt drive geometry (GT2/GT3/GT5/HTD3/HTD5/HTD8/T2.5/T5/
    T10/MXL/XL): pulley pitch diameters from tooth counts and belt length
    <-> center distance (give belt_teeth for the exact center, or
    center_distance_mm for the nearest whole-tooth belt + adjustment).
    Replacement-pulley printing: pitch Ø, ratio and printed-part notes."""
    try:
        return mech.belt_calc(profile, teeth_small, teeth_large=teeth_large,
                              belt_teeth=belt_teeth,
                              center_distance_mm=center_distance_mm)
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc)}


# --------------------------------------------------------------------------- #
# Mass report, parameter CSV round-trip, CAM
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def mass_properties(body: str) -> dict:
    """Full mass report for a body token: mass (kg), volume (mm^3), surface
    area (mm^2), centre of mass (mm) and moments of inertia (kg*mm^2). Set the
    material first (set_material) for correct density."""
    return _call('mass_properties', body=body)


# NOT readOnlyHint: writes/overwrites a CSV at the model-chosen path.
@mcp.tool()
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


@mcp.tool()
def configurations(action: str = 'list', name: str = '', row: int = 0,
                   column: int = 0) -> dict:
    """Work with a configured design's configuration table. action: "list"
    (configurations + columns + which is active), "activate" (name = the
    configuration to switch the design to), "cell" (row/column indexes — read
    one table cell). Returns {configured: false} when the design has no
    configurations."""
    return _call('configurations', action=action, name=name or None,
                 row=row, column=column)


@mcp.tool(**_annot(readOnlyHint=True))
def cam_setups() -> dict:
    """List MANUFACTURE (CAM) setups with their operations and toolpath
    state. Create new setups with cam_setup; generation and posting are
    scriptable via cam_generate/cam_post."""
    return _call('cam_setups')


@mcp.tool()
def cam_setup(bodies: list[str] = [], operation_type: str = 'milling',
              stock_mode: str = 'relative_box', name: str = '') -> dict:
    """Create a MANUFACTURE (CAM) setup — scripted end-to-end since Fusion
    v2704. bodies: body/occurrence tokens to machine (default: every root
    body); operation_type: milling|turning|jet|additive; stock_mode:
    relative_box|fixed_box|relative_cylinder|fixed_cylinder|relative_tube|
    fixed_tube|solid|previous_setup. Activates the MANUFACTURE workspace
    once when the document has never entered it. Add operations in the UI or
    via run_fusion_code, then cam_generate + cam_post."""
    return _call('cam_setup', bodies=bodies, operation_type=operation_type,
                 stock_mode=stock_mode, name=name or None)


@mcp.tool()
def cam_suppress(name: str, suppress: bool = True) -> dict:
    """Suppress (or restore with suppress=False) a CAM setup or a single
    operation by name (names from cam_setups) — skip work without deleting
    it. Suppressed operations are excluded from cam_generate/cam_post."""
    return _call('cam_suppress', name=name, suppress=suppress)


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
# Electronics — schematics, PCBs, libraries (read-only preview API, Fusion
# May 2026+). Inspect and export only: Fusion's Electronics API cannot create
# or edit schematic/board content yet.
# --------------------------------------------------------------------------- #
@mcp.tool(**_annot(readOnlyHint=True))
def electronics_info() -> dict:
    """Overview of the open electronics design: which product is active
    (schematic / 2D PCB / library / design), name, sheet/part/net or
    element/signal/layer counts, per-sheet breakdown, ERC/DRC error counts.
    Call this first when working with electronics documents. Requires an
    electronics tab to be the active document (Fusion May 2026+)."""
    return _call('electronics_info')


@mcp.tool(**_annot(readOnlyHint=True))
def electronics_components(side: str = 'auto', filter: str = '',
                           limit: int = 0) -> dict:
    """List electronics components. side="board": placed elements with
    position (mm), rotation (deg), mirrored/locked/populated flags and
    footprint. side="schematic": parts with value, device set, package and
    attributes (MPN, manufacturer, ...) — the raw material for a BOM.
    side="auto" picks the active side. filter: case-insensitive name
    substring; limit: cap the list (0 = all)."""
    return _call('electronics_components', side=side, filter=filter or None,
                 limit=limit)


@mcp.tool(**_annot(readOnlyHint=True))
def electronics_nets(side: str = 'auto', filter: str = '',
                     limit: int = 0) -> dict:
    """Connectivity of the electronics design. side="schematic": nets with
    their pin connections (part + pin) — the netlist. side="board": copper
    signals with trace/via/pour counts and pad contacts (element + pad).
    side="auto" picks the active side. filter: name substring; limit: cap."""
    return _call('electronics_nets', side=side, filter=filter or None,
                 limit=limit)


@mcp.tool(**_annot(readOnlyHint=True))
def electronics_layers(used_only: bool = False) -> dict:
    """Layer table of the PCB (fallback: schematic/library): number, name,
    used, visible, color. used_only=True hides unused layers. Layers 1-16 are
    copper (1 = Top, 16 = Bottom in the EAGLE convention)."""
    return _call('electronics_layers', used_only=used_only)


@mcp.tool(**_annot(readOnlyHint=True))
def electronics_library(filter: str = '', limit: int = 0) -> dict:
    """Inspect electronics component libraries. With a library document
    active: its device sets, each with devices and packages. With a
    schematic/PCB active: the libraries embedded in that document with
    content counts. filter: name substring; limit: cap (device sets)."""
    return _call('electronics_library', filter=filter or None, limit=limit)


def _ecad_bom_rows(components, group=True):
    """Aggregate schematic parts into BOM rows — pure Python over the
    electronics_components read, no extra Fusion API surface. Grouping key:
    value + package (group=False keeps one row per part)."""
    def _attr(attrs, names):
        for k, v in attrs.items():
            if str(k).upper() in names and v:
                return str(v)
        return ''

    rows = {}
    for c in components:
        attrs = c.get('attributes') or {}
        key = ((c.get('value') or '', c.get('package') or '') if group
               else (c.get('name') or '',))
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                'value': c.get('value') or '',
                'package': c.get('package') or '',
                'device_set': c.get('device_set') or '',
                'mpn': '', 'manufacturer': '',
                'quantity': 0, 'designators': []}
        # Backfill from whichever part in the group carries the attribute.
        if not row['mpn']:
            row['mpn'] = _attr(attrs, ('MPN', 'PART_NUMBER', 'PARTNO',
                                       'MANUFACTURER_PART_NUMBER'))
        if not row['manufacturer']:
            row['manufacturer'] = _attr(attrs, ('MANUFACTURER', 'MFR', 'MFG'))
        row['quantity'] += 1
        row['designators'].append(c.get('name') or '')
    out = sorted(rows.values(),
                 key=lambda r: (-r['quantity'], r['value'], r['package']))
    for r in out:
        r['designators'] = sorted(r['designators'])
    return out


# NOT readOnlyHint: csv_path writes/overwrites a file at the model-chosen path.
@mcp.tool()
def electronics_bom(group: bool = True, csv_path: str = '') -> dict:
    """Bill of materials of the open electronics design, aggregated from the
    schematic parts: one row per distinct value+package with quantity, sorted
    designators (R1, R2, ...) and MPN/manufacturer attributes when the parts
    carry them. group=False keeps one row per part. csv_path (absolute) also
    writes the table as CSV for ordering/assembly."""
    parts = _call('electronics_components', side='schematic', limit=0)
    if not isinstance(parts, dict) or parts.get('error'):
        return parts
    rows = _ecad_bom_rows(parts.get('components') or [], group=group)
    out = {'rows': len(rows), 'bom': rows}
    if csv_path:
        import csv
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(['quantity', 'value', 'package', 'device_set',
                            'mpn', 'manufacturer', 'designators'])
                for r in rows:
                    w.writerow([r['quantity'], r['value'], r['package'],
                                r['device_set'], r['mpn'], r['manufacturer'],
                                ' '.join(r['designators'])])
            out['csv'] = csv_path
        except OSError as exc:
            out['csv_error'] = str(exc)
    return out


# NOT readOnlyHint: this writes/overwrites a file at the model-chosen path.
@mcp.tool()
def electronics_export(path: str) -> dict:
    """Export the electronics design to an EAGLE 9.6.2 file. The extension
    picks the product: .brd (board), .sch (schematic), .lbr (library); it
    must be reachable from the active document. Path is the full output file
    path."""
    return _call('electronics_export', path=path)


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


@mcp.tool(**_annot(readOnlyHint=True))
def data_folders(project: str = '', max_depth: int = 3) -> dict:
    """Browse the cloud data tree: projects -> nested folders with file names.
    Deeper than list_documents (which only sees root folders) — use it to find a
    document buried in subfolders before open_document. project = name filter;
    max_depth caps recursion."""
    return _call('data_folders', project=project or None, max_depth=max_depth)


@mcp.tool(**_annot(readOnlyHint=True))
def version_history() -> dict:
    """Version history of the ACTIVE saved document: version number, date and id
    per version, newest first. Narrate "what changed since v12" or pair with
    mesh_compare across exports."""
    return _call('version_history')


@mcp.tool()
def share_link(create: bool = False) -> dict:
    """Get a shareable link for the ACTIVE saved document. create=True publishes
    the document to anyone with the link (get explicit user consent first);
    create=False (default) only reports whether a link already exists."""
    return _call('share_link', create=create)


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


@mcp.tool(**_annot(readOnlyHint=True))
def selection_filter(action: str = 'list', filters: list[str] = [],
                     enabled: bool = True) -> dict:
    """Inspect or set the active workspace's selection filters (Fusion July
    2026+) — narrow what the user's clicks can pick before asking them to
    select something (e.g. faces only, then get_selection). action: "list"
    (available filters + state), "set" (filters=[names], enabled), "all"
    (enabled for every filter). Restore with action="all", enabled=True."""
    return _call('selection_filter', action=action, filters=filters,
                 enabled=enabled)


@mcp.tool(**_annot(readOnlyHint=True))
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


@mcp.tool(**_annot(readOnlyHint=True))
def annotate(texts: list[dict] = [], lines: list[dict] = []) -> dict:
    """Overlay labels and leader lines on the viewport (custom graphics) so the
    next screenshot explains itself — callouts, dimensions-as-text, arrows.
    texts: [{"text","x","y","z","size"}] (mm; size = cap height, default 5).
    lines: [{"from":[x,y,z], "to":[x,y,z]}] (mm). Overlays aren't geometry and
    don't touch the model; each call adds to the current overlay. Clear with
    annotations_clear."""
    return _call('annotate', texts=texts, lines=lines)


@mcp.tool()
def annotations_clear() -> dict:
    """Remove the viewport annotation overlay created by annotate."""
    return _call('annotations_clear')


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
# MCP Apps: interactive viewer panel rendered in the chat (spec 2026-01-26).
# Clients without the ui extension just see a normal tool result.
# --------------------------------------------------------------------------- #
@mcp.resource(viewer.VIEWER_URI, mime_type=viewer.VIEWER_MIME)
def viewer_app() -> str:
    """HTML for the interactive Fusion viewer panel (MCP Apps)."""
    return viewer.VIEWER_HTML


@mcp.tool(meta={'ui': {'resourceUri': viewer.VIEWER_URI}},
          **_annot(readOnlyHint=True))
def open_viewer() -> dict:
    """Open an interactive viewport panel in the chat (MCP Apps): camera
    preset buttons (iso/front/top/...), fit, and a live BOM table — the user
    can look around the model without asking for screenshots one by one.
    On clients without MCP Apps support this returns a plain message."""
    return {'ok': True,
            'note': 'Viewer panel requested. If no panel appeared, this MCP '
                    'client does not support MCP Apps — use screenshot / '
                    'multi_screenshot instead.'}


# --------------------------------------------------------------------------- #
# Resources — read-only views a client can fetch without invoking a tool
# --------------------------------------------------------------------------- #
# Resources bypass the update-notice consumption (_consume_notice=False): a
# client that prefetches these at session start must not swallow the one-shot
# notice before any model-facing tool result can carry it.
@mcp.resource('fusion://design/state')
def resource_state() -> str:
    """Current design summary (bodies, sketches, parameters) as JSON."""
    return json.dumps(_call('get_state', _consume_notice=False), ensure_ascii=False)


@mcp.resource('fusion://design/parameters')
def resource_parameters() -> str:
    """All model/user parameters as JSON."""
    return json.dumps(_call('list_parameters', _consume_notice=False), ensure_ascii=False)


@mcp.resource('fusion://design/tree')
def resource_tree() -> str:
    """Component/occurrence hierarchy as JSON."""
    return json.dumps(_call('query_entities', kind='occurrences', _consume_notice=False),
                      ensure_ascii=False)


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
def reverse_engineer_scan(scan_path: str = '', tolerance_mm: float = 0.2) -> str:
    """Guide the model through a full scan-to-parametric-CAD workflow."""
    return (
        'Reverse-engineer the scan%s into a clean parametric Fusion design '
        '(target deviation <= %s mm).\n'
        '1) scan_analyze(path) — size, symmetry, planes, cylinders (holes vs '
        'bosses), wall thickness. If units look wrong, ask the user.\n'
        '2) Choose a strategy: (a) prismatic machine part -> rebuild from the '
        'fitted primitives with sketches, extrude, hole, fillet; (b) complex '
        'silhouette -> scan_sections(axis=...) and rebuild contours per slice '
        '(construction_plane + sketch_circle/sketch_polyline + loft/extrude, '
        'in one batch); (c) organic shape -> import_mesh + mesh_reduce + '
        'mesh_to_brep(method="organic" or "prismatic").\n'
        '3) Exploit symmetry: model half, then mirror.\n'
        '4) Add parameters (add_parameter) for key dimensions so the rebuild '
        'is editable.\n'
        '5) Verify: export("stl", temp_path) then scan_deviation(scan, temp) '
        '— scan_align first if the rebuild is not in the scan frame; iterate '
        'on the worst regions until within tolerance.\n'
        '6) Show the result: multi_screenshot + the deviation summary.'
        % ((' at %r' % scan_path) if scan_path else '', tolerance_mm)
    )


@mcp.prompt()
def trace_photo(image_path: str = '', marker_size_mm: float = 50.0) -> str:
    """Guide the model from a workshop photo to sketch geometry."""
    return (
        'Turn the photo%s into Fusion sketch geometry.\n'
        '1) Ask whether a square marker (ArUco/QR, %g mm side) lies in the '
        "part's plane; more markers = better. Without one, use the corners "
        'of a known rectangle (ref_points: A4, bank card) or two points a '
        'known distance apart (scale_points) instead.\n'
        '2) photo_rectify(image, marker_size_mm=%g) — perspective off, exact '
        'mm_per_px scale; check scale_spread_pct when several markers are '
        "in frame, and undistort='auto' if they disagree.\n"
        '3) photo_measure(rectified, mm_per_px, segments/holes=true) — pull '
        'the driving dimensions (hole diameters, bolt spacing, outline '
        'sizes) off the photo and VERIFY them on the annotated preview.\n'
        '4) Either trace curves: photo_to_sketch(rectified, mm_per_px) then '
        'import_file(format="dxf", plane=...) — profiles ready to extrude; '
        'or keep the photo visible: canvas_add(rectified, width_mm=size from '
        'the rectify report) and refine with canvas_calibrate on two known '
        'features.\n'
        '5) Clean the imported sketch (sketch_status, auto_constrain, '
        'sketch_dimension with the measured values as parameters) — '
        'vectorised curves are unclean by nature.\n'
        '6) Verify a key dimension against the real part and adjust.'
        % ((' at %r' % image_path) if image_path else '',
           marker_size_mm, marker_size_mm)
    )


@mcp.prompt()
def constrain_and_dimension() -> str:
    """Guide the model to fully constrain and dimension a sketch."""
    return (
        'Fully constrain the active sketch. 1) sketch_status — find open '
        'endpoints and the profile count; close open chains first '
        '(sketch_line, or sketch_constraint kind="coincident"). 2) '
        'auto_constrain to add the geometric constraints a human would. 3) '
        'Add driving dimensions with sketch_dimension, naming the key ones '
        'via parameter= so they become editable parameters. 4) Re-run '
        'sketch_status until fully_constrained is true; design_diagnostics '
        'confirms nothing else is under-constrained.'
    )


@mcp.prompt()
def cam_to_gcode(post: str = 'fanuc.cps') -> str:
    """Guide the model from a solid to posted G-code."""
    return (
        'Machine the active design and produce G-code. 1) get_state + '
        'bounding_box to size the part and pick orientations. 2) '
        'cam_setup(bodies=[...], operation_type="milling", '
        'stock_mode="relative_box") — one setup per orientation. 3) Add '
        'operations (adaptive clearing, contour, drill) in the MANUFACTURE '
        'UI or via run_fusion_code on the cam product. 4) cam_generate() and '
        'check cam_setups for toolpath errors; park problem operations with '
        'cam_suppress while iterating. 5) cam_post(setup, path, '
        'post_config="%s") and report the NC file path.' % post
    )


@mcp.prompt()
def spare_part(source: str = '', material: str = 'petg') -> str:
    """Guide the model through the full replacement-part workflow:
    measure -> standardize -> model -> verify strength -> print."""
    return (
        'Recreate the part%s as a printable replacement.\n'
        '1) MEASURE, never guess: scan_analyze / photo_rectify + '
        'photo_measure turn the source into millimetres. Distrust shiny/'
        'black-surface scan diameters by 2-3 mm.\n'
        '2) STANDARDIZE every measured dimension: fit_suggest (measured '
        'diameter -> ISO 286 nominal + fit), bearing_lookup (seat dims -> '
        'catalog bearing), circlip_lookup (groove specs), oring_gland '
        '(cord -> groove design), hole_spec/fastener_lookup (screw holes), '
        'belt_calc (pulleys). A spare part built from catalog numbers '
        'beats one built from noisy measurements.\n'
        '3) MODEL parametrically in Fusion; validate_only=true on risky '
        'sweeps/lofts; design_diagnostics before moving on.\n'
        '4) VERIFY: export("step", ...) then freecad_inspect (independent '
        'kernel check) and freecad_fem with the real fixing faces and '
        'loads, material="%s" — check safety_factor_printed, and fillet '
        'any corner the FEM flags before trusting max stress.\n'
        '5) PRINT: dfm_check + print_check on the exported STL, then '
        'print_estimate for time/cost. Report all measured->standardized '
        'substitutions so the user can veto them.'
        % (' from %s' % source if source else '', material)
    )


# --------------------------------------------------------------------------- #
# Toolsets — trim the tool list for clients without tool search (Claude
# Desktop). FUSIONMCP_TOOLSETS="scan,photo" keeps 'core' plus the named
# groups; unset = every tool. Server-side convention (GitHub-MCP style) —
# the protocol has no toolset mechanism.
# --------------------------------------------------------------------------- #
_TOOLSET_RULES = (
    ('scan', ('scan_', 'mesh_', 'import_mesh', 'face_groups')),
    ('photo', ('photo_', 'canvas_', 'import_svg', 'photogrammetry_')),
    ('cam', ('cam_',)),
    ('drawing', ('create_drawing', 'drawing_', 'export_sketch_dxf',
                 'export_flat_pattern')),
    ('electronics', ('electronics_',)),
    ('print', ('print_estimate', 'print_check', 'dfm_check',
               'fastener_lookup', 'hole_spec', 'insert_fastener',
               'fastener_update_size')),
    ('sheetmetal', ('fold', 'join_by_bend', 'corner_closure', 'flat_pattern')),
    ('freecad', ('freecad_',)),
    ('mech', ('fit_suggest', 'bearing_lookup', 'circlip_lookup',
              'oring_gland', 'belt_calc')),
    ('data', ('data_folders', 'version_history', 'share_link',
              'list_documents', 'open_document')),
    ('diag', ('design_diagnostics', 'sketch_status', 'sketch_doctor',
              'interference', 'mass_properties', 'api_introspect')),
)


def _toolset_of(name):
    for group, prefixes in _TOOLSET_RULES:
        for prefix in prefixes:
            if name == prefix or name.startswith(prefix):
                return group
    return 'core'


def _apply_toolsets():
    """Drop tools outside FUSIONMCP_TOOLSETS (+ implicit 'core') from the
    FastMCP registry. Best-effort over a private SDK surface: any failure
    leaves the full tool list, never breaks startup."""
    raw = os.environ.get('FUSIONMCP_TOOLSETS', '').strip()
    if not raw:
        return None
    wanted = {part.strip().lower() for part in raw.split(',') if part.strip()}
    wanted.add('core')
    try:
        registry = mcp._tool_manager._tools
        dropped = [name for name in list(registry)
                   if _toolset_of(name) not in wanted]
        for name in dropped:
            del registry[name]
        return {'enabled': sorted(wanted), 'dropped': len(dropped)}
    except Exception:  # noqa: BLE001 - private SDK internals may move
        return None


def _update_popup_worker(check_thread, attempts=30, retry_delay=60):
    """Surface a pending update to the USER as a native Fusion popup — no
    typed command needed. Waits for the startup update check, asks in Fusion
    via the notify_update op (Yes/No with release notes), applies the update
    on Yes and reports the outcome with a second popup. Quietly retries while
    Fusion is not running yet; gives up silently after ~30 min (the model
    notice from consume_notice still covers that case). Safe alongside tool
    calls: FusionClient.call serialises the socket under a lock. Opt out with
    FUSION_MCP_UPDATE_POPUP=off."""
    if os.environ.get('FUSION_MCP_UPDATE_POPUP', '').lower() == 'off':
        return
    if check_thread is not None:
        check_thread.join(timeout=120)
    info = updater.pending_info()
    if not info or not info.get('update_available'):
        return
    payload = {
        'version': info.get('latest_version'),
        'current': updater.LOCAL_VERSION,
        'notes': updater.plain_notes(info.get('release_notes')),
    }
    answer = None
    for _ in range(attempts):
        try:
            answer = fusion.call('notify_update', payload)
            break
        except FusionNotConnected:
            time.sleep(retry_delay)
        except FusionError:
            # Older add-in without the notify_update op — the model-facing
            # notice still announces the update in chat.
            return
    if not (isinstance(answer, dict) and answer.get('install')):
        return
    result = updater.apply(confirm=True)
    if result.get('applied'):
        text = ('FusionMCP was updated to %s.\n\nTo finish: fully restart '
                'Fusion (not just add-in Stop/Run) and restart the MCP '
                'client.' % result.get('new_version'))
    else:
        text = ('FusionMCP update was not installed: %s'
                % result.get('reason', 'unknown error'))
    with contextlib.suppress(Exception):
        fusion.call('show_message', {'title': 'FusionMCP update', 'text': text})


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
    _apply_toolsets()
    check_thread = updater.start_background_check()
    threading.Thread(target=_update_popup_worker, args=(check_thread,),
                     name='fusionmcp-update-popup', daemon=True).start()
    mcp.run()
