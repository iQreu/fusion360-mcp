# Changelog

The section matching the pushed tag becomes the GitHub release body
(release.yml extracts it), which the in-Fusion update popup and the
`fusionmcp_update` notice show to the user — keep entries short, user-facing
and grouped under **Added / Fixed / Changed**.

## v1.14.0 — 2026-08-17

### Added
- `photo_measure`: millimetre dimensions straight off a rectified photo —
  point-to-point distances (with optional edge snapping), automatic hole
  detection (centres, diameters, bolt-pattern spacing) and an annotated
  preview image so every measurement can be verified by eye.
- `photo_rectify` upgrades: several markers in one frame refine the fit and
  report how well they agree; no printed marker needed — the 4 corners of
  any known rectangle (A4 sheet, bank card) or two points a known distance
  apart work too; optional lens-distortion removal (`undistort="auto"` with
  2+ markers) and an EXIF ultra-wide-lens warning.
- `photogrammetry_scale`: recover the real millimetre scale of a Meshroom
  reconstruction from ArUco markers lying in the scene (corners
  triangulated from cameras.sfm) — closes the "arbitrary units" gap.
- `photogrammetry_run` can forward known marker distances to RealityScan
  (`distances=[[a, b, mm], ...]`) so the scale is solved during alignment
  (CLI verbs not yet verified against a live install).

## v1.13.0 — 2026-08-17

### Added
- `codecad_run`: build123d scripts to STEP/STL without Fusion in the loop
  (optional `codecad` extras) — parametric generators in pure Python, then
  import_file brings the solid in. Note: Windows Smart App Control blocks
  the OCP kernel DLL on locked-down machines.
- `photogrammetry_run`: photos folder to mesh via an installed RealityScan
  (preferred) or Meshroom — auto-detected, arbitrary scale flagged, result
  feeds the normal scan pipeline (scan_convert, import_mesh, scan_align).

## v1.12.0 — 2026-08-17

### Added
- `print_estimate`: slice an exported STL with your installed slicer
  (PrusaSlicer / OrcaSlicer / Bambu Studio, auto-detected) and get print
  time, filament grams and cost — with your own profile and prices.
- `fastener_lookup` + `hole_spec`: metric fastener tables (ISO/DIN M2-M12)
  — clearance/tapped/heat-set-insert holes, counterbores, suggested bolt
  lengths — ready for the hole tool.
- `dfm_check`: design-for-manufacturing report — FDM (bed/overhangs/walls),
  injection molding (draft angles, undercuts) and 3-axis CNC (unreachable
  surfaces, flip advice).
- `loft_from_sections`: one call from scan_sections/scan_cavity_sections
  output to a lofted body (offset planes + closed fitted splines + optional
  centreline rail).
- `sketch_doctor`: sketch health + auto-constrain repair in one call.
- `drawing_table` and create_drawing `auto_dimension`/`flat_pattern`
  automation options (July 2026 preview) — auto-generated drawings with
  dimensions and custom tables on the sheet.
- `silhouette` (experimental): body/mesh outline along any direction into a
  sketch — cutting templates via export_sketch_dxf.
- `fastener_update_size`: refresh Content-Library screws after geometry
  changes (preview).
- `FUSIONMCP_TOOLSETS` env var trims the tool list to named groups for
  clients without tool search.

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
