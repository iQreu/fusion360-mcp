# Changelog

The section matching the pushed tag becomes the GitHub release body
(release.yml extracts it), which the in-Fusion update popup and the
`fusionmcp_update` notice show to the user — keep entries short, user-facing
and grouped under **Added / Fixed / Changed**.

## v1.16.0 — 2026-09-03

### Added
- Scan reverse engineering as FEATURES, not splines (new `recon` module,
  server-side, deterministic — no RANSAC):
  - `scan_segment`: region-growing split of a scan into plane / cylinder /
    sphere / freeform patches with an adjacency graph (optional coloured PLY).
  - `scan_features`: the measurement sheet — holes (centre, Ø, depth,
    through/blind), non-circular cutouts, bosses, fillet rounds, hole
    patterns (pair / linear / rectangle / PCD) and plate thicknesses.
  - `scan_profile`: one planar section turned into LINES + ARCS with
    corner detection, arc merging, tangent junctions and angle/radius
    snapping; `to_fusion=true` builds the constrained sketch directly.
  - `scan_thread_identify`: pitch, handedness, major/minor of a scanned
    thread (folded-phase periodogram) snapped to ISO 261 / UNC / UNF.
  - `scan_frame`: datum alignment without a CAD model — largest plane to
    Z=0, dominant direction to X, transform returned for import_mesh.
  - `scan_mesh_offset`: voxel offset of a scan by +d mm, plain or monotone
    (drop-on cavity along an axis) — the cavity cutter, ready to import.
  - `scan_fit_report`: one PASS/WARN/FAIL sheet (seating vs scan,
    printability, walls, DFM) with explicit thresholds.
- Fusion side: `capabilities_probe` (which Preview/optional APIs exist on
  this build — replaces the manual checklist after each Fusion update),
  `new_document(kind, direct)`, `fillet_max_radius` (bisection to the
  largest working radius, optional chamfer fallback), `sketch_profile`
  (lines + arcs sharing endpoints, optional constraints/dimensions),
  printed-detail generators `add_boss` (screw/heat-set boss with blind hole
  and base fillet), `add_snap_fit` (cantilever hook with strain/force
  check per material) and `add_clip_fir_tree` (push-in clip for a round
  hole, one revolve).
- `interference(ignore_coincident=true)` — the assembly check;
  `set_design_mode(mode="get")` reports mode, timeline size and the
  direct-mode rules, and flags a silently ignored switch.
- Prompt `reverse_engineer_scan` rewritten around the new tools.

### Fixed
- `get_state` no longer fails on direct-modelling designs (parameters
  unavailable there).

### Changed
- Runs on both mcp 1.x and 2.x SDKs (FastMCP/MCPServer shim); `[re]`
  extras gain scikit-image (marching cubes for scan_mesh_offset).

## v1.15.0 — 2026-08-27

### Added
- FreeCAD integration (new `freecad_*` tools, FreeCAD 1.x found
  automatically or via `FUSION_MCP_FREECAD`):
  - `freecad_fem`: "will this part hold?" — linear static FEM on an
    exported STEP through FreeCAD's bundled gmsh + CalculiX: von Mises
    stress, displacement, mass and a safety factor vs yield, with material
    presets (steel/aluminum/PLA/PETG/... or custom) and a derated
    safety factor for printed parts.
  - `freecad_inspect`: independent OpenCascade-kernel second opinion on any
    STEP/IGES/BREP/mesh — validity, volume, and a per-face census (type,
    area, normals, cylinder radius/axis) that also names faces for FEM
    constraints.
  - `freecad_convert`: STEP/IGES/BREP/FCStd conversions, tessellation to
    STL/OBJ/PLY/3MF with quality knobs, mesh -> faceted reference solid.
  - `freecad_run`: headless FreeCAD Python escape hatch (TechDraw DXF
    drawings, Draft, OCC modeling); `freecad_info` reports the install.
- Spare-part data tools — measured dimensions become intentional ones:
  - `fit_suggest`: measured diameter -> nearest standard nominal + ISO 286
    fit (H7/g6 & friends) with exact limits and clearance/interference
    range.
  - `bearing_lookup`: deep-groove bearing envelopes (608, 6000-6310,
    thin/miniature series) by designation or by measured seat dimensions,
    with seat-fit and FDM advice.
  - `circlip_lookup`: DIN 471/472 ring + groove dimensions for Ø3-100.
  - `oring_gland`: O-ring groove design (static/dynamic/face) from the
    measured cord, with standard cross-section snapping.
  - `belt_calc`: GT2/HTD/T-profile pulley and belt geometry, belt length
    <-> center distance.
- `spare_part` prompt: the full measure -> standardize -> model -> FEM ->
  print workflow.
- New toolsets `freecad` and `mech` for `FUSIONMCP_TOOLSETS`.

### Changed
- mcp pin raised to >=1.29.1 (final 1.x maintenance release); new
  dependency `isofits` (MIT) for ISO 286 data.

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

### Fixed
- `scan_analyze` cylinder fits no longer depend on pyransac3d's sampling
  luck (0.7.0 regressed an 8 mm fit to 6.3 mm): every candidate is refined
  deterministically — axis from the surface normals, centre/radius from a
  least-squares circle — and a free normals-based candidate plus RANSAC
  restarts compete for the most inliers.

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

