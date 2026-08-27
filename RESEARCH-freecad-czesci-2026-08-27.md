# Research 2026-08-27 — FreeCAD, ekosystem MCP, części zamienne (v1.15)

Workflow 5 agentów (freecad-landscape, freecad-headless, mcp-ecosystem,
spare-parts-re, fusion-api-news), 56 ustaleń. Wnioski wdrożone w v1.15.0;
poniżej esencja + backlog na kolejne fale.

## Zweryfikowane lokalnie (FreeCAD 1.1.3, Windows)

- Pełny łańcuch MES działa headless: `ObjectsFem` → `GmshTools.create_mesh()`
  → `FemToolsCcx.run()` → `Fem::FemResultObject.vonMises/DisplacementLengths`
  (MPa/mm). Bundlowane `gmsh.exe` 4.15 + `ccx.exe` (CalculiX 2.22) — zero
  instalacji. **Tylko `run()`** — `run()+load_results()` dubluje wyniki.
- `freecadcmd.exe` zwraca **exit code 0 nawet przy nieobsłużonym wyjątku**
  skryptu → sukces bramkować plikiem result.json/sentinelem, nie kodem wyjścia
  (v1.15: `_run` w `mcp_server/freecad.py`).
- Python w FreeCAD to **3.11 (conda)** — importu do serwera (3.12) nie ma;
  jedyna słuszna architektura to subprocess + STEP/JSON przez temp dir.
- Headless importują się: Part, Sketcher, PartDesign, Fem, TechDraw, CAM,
  Import, Mesh/MeshPart, Draft, Spreadsheet, BOPTools, Materials. `*Gui` — nie.
- TechDraw headless: strona + widoki + **eksport DXF** (`writeDXFPage`) TAK;
  SVG/PDF całej strony — tylko GUI. Szablon: `Default_Template_A4_Landscape.svg`
  (stary `A4_LandscapeTD.svg` zniknął w 1.1).
- CAM 2.5D headless działa na stable 1.1.3 (Job/Profile/PostProcessorFactory →
  G-code) — wbrew docs blwfish (ich ograniczenie, nie API).
- Sketcher: w 1.x `sk.AttachmentSupport` (nie `sk.Support`).

## Ekosystem MCP (po 2026-08-17)

- python-sdk: **1.29.1 = ostatni 1.x** (security-only); 2.x (2.0.0/2.1.x) to
  teraz domyślny `pip install mcp` — rename FastMCP→MCPServer, httpx2,
  obowiązkowe opentelemetry-api + mcp-types. **Migracja 2.x = zadanie na
  v1.16**, nie bump. Tasks nadal niezaimplementowane w żadnym SDK.
- Roadmapa MCP (2026-08-22): Tasks → do spec core, server-initiated events,
  Streamable-HTTP-over-stdio, progressive tool discovery (nasze toolsets =
  zgodny kierunek).
- Claude Code sierpień: naprawione deferral narzędzi MCP + prompt caching z
  ToolSearch — 184 narzędzia FusionMCP nie bolą. Ikony SEP-973 i MCP Apps
  nadal nierenderowane w klientach Claude — nie inwestować. Registry: publish
  otwarty, ale preview (możliwe resety) — czekać na GA.

## Części zamienne

- **isofits** (PyPI, MIT, pure Python) — ISO 286 zweryfikowane co do µm
  z tabelami (H7@25=+21/0, k6@40=+18/+2). Wdrożone w `fit_suggest`.
- **Brak otwartej bazy** o-ringów/pasów/segerów — tabele DIN 471/472
  przepisane z kart Westfield Fasteners (v1.15 `mech.py`); UWAGA: karty
  producentów mylą kolumny (d3 pierścienia vs d2 rowka) — zawsze sprawdzać
  regułą d2 = d1 ∓ 2t.
- BOLTS żyje jako boltsparts/boltsparts (GPL-3) — łożyska/profile; wymiary
  to fakty, przepisywać, nie kopiować plików.
- CAD z chmury punktów: **CAD-Recode v1.5** (pointcloud→CadQuery, Qwen2-1.5B)
  i **cadrille** (ICLR'26, +zdjęcia/tekst) — oba **CC-BY-NC** → tylko jako
  opt-in extra z ostrzeżeniem; CPU-realne (1.5–2B). Point2CAD (PyMesh=Linux),
  HoLa-BRep i PartField (CUDA) — odpadają na tej maszynie.
- **thread_identify** (fit walca → unwrap residuów → FFT skoku → snap do ISO
  261/262): nie istnieje open-source, zbudowalne na trimesh/pyransac3d —
  kandydat P2, prototypować na realnym skanie.
- **geomfitty** (MIT, numpy/scipy): fit torusa/okręgu 3D (brak w pyransac3d)
  → rowki o-ringów i zaokrąglenia ze skanów — kandydat P2 (brak na PyPI,
  vendor lub git-pin).
- Motoryzacja: TecDoc zamknięty (scrapery = ryzyko prawne); jedyne otwarte
  API to NHTSA vPIC (VIN→pojazd, bez numerów części). pymeshlab 2025.7 ma
  koła cp312 win (GPL-3, tylko opcjonalny import).

## Fusion

- **Brak update'u po lipcu 2026** (v2704.1.53 = bugfix z 6 sierpnia; następny
  spodziewany wrzesień/październik). Flange nadal bez API, elektronika nadal
  read-only.
- Od **2026-09-07** Autodesk blokuje buildy <2703.1.11 → można zakładać API
  maj/lipiec 2026 (odnotowane w README).

## Backlog v1.15.x / v1.16

P2: `freecad_techdraw_dxf` (dedykowane narzędzie zamiast furtki),
`thread_identify`, geomfitty (torus/rowki), sketch-DOF diagnostyka w Fusion
(wzorzec theosib). P3/v1.16: migracja mcp 2.x, `[re-ml]` CAD-Recode/cadrille
(NC!), FreeCAD CAM fallback, GUI addon XML-RPC (wzorzec neka-nat) + pętla
screenshotów, registry publish po GA, fixture'y regresyjne (wzorzec blwfish).
