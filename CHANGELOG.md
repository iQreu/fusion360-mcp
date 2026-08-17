# Changelog

The section matching the pushed tag becomes the GitHub release body
(release.yml extracts it), which the in-Fusion update popup and the
`fusionmcp_update` notice show to the user — keep entries short, user-facing
and grouped under **Added / Fixed / Changed**.

## v1.11.1 — 2026-08-17

### Added
- Update popup in Fusion: when a new FusionMCP version is available, a native
  Yes/No dialog shows the version and its changes — one click installs it,
  no typed commands. A second popup confirms the result and reminds about the
  Fusion restart. (Opt out: `FUSION_MCP_UPDATE_POPUP=off`.)
- `show_message` tool: native Fusion info popup for user-facing announcements.
- `CHANGELOG.md` drives release notes, so update popups show real
  Added/Fixed lists instead of a bare compare link.

### Fixed
- Updater no longer refuses git updates because of untracked files
  (handoff/research notes) — only real local edits block a pull.

## v1.11.0 — 2026-08-11

### Added
- Scan tools (server-side): `scan_align` (deterministic ICP with optional
  uniform scale), `scan_fit_check` (collision/clearance gate for printed
  parts vs the scanned object), `scan_cavity_sections` (monotone drop-on
  cavity outlines from a scan), `scan_convert` (GLB/PLY/OFF → STL/OBJ).
- Photo tools (new `[photo]` extras): `photo_rectify` (ArUco/QR marker →
  perspective-free image with exact mm-per-pixel scale) and
  `photo_to_sketch` (silhouette → DXF polylines → sketch profiles).
- Mesh tools: `mesh_export` (mesh body → STL/OBJ file), `face_groups`
  (list + per-group export), `mesh_repair`, `mesh_smooth`, `mesh_shell`,
  `mesh_separate`, and full `mesh_remesh` parameters.
- Canvas photo tracing: `canvas_calibrate` (scripted two-point calibration),
  `canvas_list` / `canvas_update` / `canvas_delete`, `import_svg`, and the
  `trace_photo` prompt.

### Fixed
- `scan_fit_check` works on machines without a trimesh ray backend
  (inside/outside now derived from face normals).
- Docs no longer claim scipy is part of the `[re]` extras.

## v1.10.0 — 2026-08-04

### Added
- July 2026 GA wave: `cam_setup`/`cam_suppress`, `corner_closure`,
  `timeline_builder`, `design_diagnostics`, `sketch_status`,
  `create_appearance`, native fastener probe, `validate_only` dry-runs,
  `include_screenshot` on mutating tools, `electronics_bom`, and the
  `constrain_and_dimension` / `cam_to_gcode` prompts.
