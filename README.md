# FusionMCP — najwydajniejszy MCP dla Fusion 360

Serwer **Model Context Protocol** dający Claude (lub innemu klientowi MCP) pełną
kontrolę nad Autodesk Fusion 360 przez jego natywne Python API.

## Dlaczego taka architektura

API Fusion 360 jest dostępne **wyłącznie z wnętrza procesu Fusion** i prawie
każde wywołanie musi iść przez **główny wątek UI**. Dlatego:

```
Claude Desktop ──stdio──▶ Serwer MCP (proces, uv)
                              │  jedno stałe połączenie TCP (keep-alive)
                              ▼
                          Add-In w Fusion 360
                              │  most: custom event → kolejka na głównym wątku
                              ▼
                          Fusion API (natywnie)
```

Decyzje pod kątem **wydajności**:

| Wybór | Zysk |
|------|------|
| Jedno stałe połączenie TCP z ramkowaniem długości | brak narzutu HTTP/handshake na każde wywołanie |
| Most przez `CustomEvent` + `threading.Event` | poprawna i szybka serializacja na główny wątek, bez pollingu |
| **Rejestr tokenów encji** (`edg7`, `fac3`, `prf1`…) | model adresuje krawędzie/ściany/profile między wywołaniami bez ciągłego re-odpytywania |
| Operacje wykonane **natywnie w add-inie** (bez codegenu) | brak kruchego sklejania stringów API |
| `run_fusion_code` jako furtka | dowolnie złożona operacja w **jednym** round-tripie |
| `screenshot` zwraca obraz do modelu | Claude „widzi" model i koryguje kurs |

## Instalacja

```powershell
powershell -ExecutionPolicy Bypass -File c:\MCP\scripts\install.ps1
```

Instalator: zainstaluje `uv` (jeśli brak), pobierze zależności serwera, skopiuje
add-in do folderu AddIns Fusion i dopisze wpis `fusion360` do
`claude_desktop_config.json`.

Następnie:
1. Uruchom Fusion 360 → **Tools ▸ Add-Ins ▸ Scripts and Add-Ins** (Shift+S),
   zakładka **Add-Ins**, zaznacz **FusionMCP**, włącz **Run on Startup**, kliknij **Run**.
2. Zrestartuj Claude Desktop.
3. Otwórz dowolny **Design** w Fusion i poproś Claude o użycie narzędzi `fusion360`.

## Narzędzia (MCP tools)

Jednostki na łączu: **długości w mm, kąty w stopniach**. Geometria adresowana
**tokenami** zwracanymi przez `get_state` / `query_entities` / narzędzia cech.

**Stan i inspekcja**: `get_state(include_mass_props=False)`,
`query_entities(kind, target, include_mass_props=False)` (kind:
`bodies|sketches|profiles|faces|edges|occurrences`), `server_info` (wersja,
uptime, telemetria czasów per-operacja)
**Szkice**: `create_sketch`, `sketch_rectangle`, `sketch_circle`, `sketch_line`,
`sketch_arc`, `sketch_polygon`, `sketch_points`, `sketch_polyline`, `sketch_spline`
**Więzy i wymiary**: `sketch_constraint` (horizontal/vertical/parallel/
perpendicular/equal/collinear/tangent/concentric/coincident/midpoint),
`sketch_dimension` (distance/radius/diameter/angle), `sketch_offset`,
`sketch_fillet`, `project_to_sketch`
**Geometria konstrukcyjna**: `construction_plane` (offset/angle/three_points/
tangent), `construction_axis` (edge/two_points/cylinder), `construction_point`
**Cechy**: `extrude`, `revolve`, `fillet`, `chamfer`, `shell`, `combine`,
`rectangular_pattern`, `circular_pattern`, `mirror`, `move_body`, `delete`, `hole`
(simple/counterbore/countersink), `loft`, `sweep`, `rib`, `draft`, `thread`,
`split_body`
**Złożenia**: `create_component`, `rename`, `copy_body`, `joint`
(rigid/revolute/slider/cylindrical/pin_slot/planar/ball)
**Materiały i pomiary**: `set_material`, `set_appearance`, `measure`
(distance/angle), `bounding_box`, `center_of_mass`, `interference`
**BOM**: `bom(include_mass, csv_path)` — lista części z ilościami, materiałami,
masą jednostkową i całkowitą; opcjonalny zapis CSV
**Tekst i grawer**: `sketch_text` (tekst w szkicu: czcionka/wysokość/pochylenie),
`emboss(profile, depth, engrave)` — grawer (cut) lub wypukły napis (join);
token tekstu działa też w zwykłym `extrude`
**Blachy**: `flat_pattern(face|body)` — rozwinięcie blachy,
`export_flat_pattern(path, ...)` — DXF rozwinięcia pod laser/waterjet,
`export_sketch_dxf(sketch, path)` — dowolny szkic jako DXF
**Siatki / reverse engineering**: `import_mesh(path, units)` (stl/obj/3mf),
`mesh_info`, `mesh_to_brep(meshes)` — konwersja skanu na bryłę,
`mesh_section(mesh, plane, offset)` — szkic przekroju siatki do odrysowania;
`query_entities(kind="meshes")` listuje siatki
**Rysunki 2D**: `create_drawing` — otwiera kreator „Drawing from Design"
(API Fusion nie tworzy arkuszy w pełni automatycznie; skryptowalne 2D to
`export_sketch_dxf` / `export_flat_pattern`)
**Parametry**: `list_parameters`, `set_parameter`, `add_parameter`
**Timeline**: `timeline` (list/rollback), `suppress_feature`
**Aktualizacje**: automatyczne sprawdzenie + pobranie przy starcie (patrz
[Aktualizacje z GitHuba](#aktualizacje-z-githuba)); `check_for_updates` (odczyt:
wersje + release notes), `apply_update(confirm=True, method="auto")` (instaluje
**po zgodzie użytkownika**: `git pull` dla czystego checkoutu, inaczej zip)
**I/O**: `export(format, path, allow_fallback=True)` (step/iges/sat/smt/f3d/stl),
`import_file(format, path)` (step/iges/sat/smt/f3d/dxf),
`screenshot(direction, fit)`, `capture_to_file(direction, fit)`, `fit_view`, `save`
— presety kamery: `current|front|back|left|right|top|bottom|iso|iso-top-right|iso-top-left|iso-bottom-right|iso-bottom-left`
**Wydajność**: `batch(operations)` — wiele operacji w jednym round-tripie,
`set_design_mode("direct"|"parametric")`
**Furtka**: `run_fusion_code(code)` — dowolny kod Fusion Python API w jednym wywołaniu

**Resources** (odczyt bez wywołania narzędzia): `fusion://design/state`,
`fusion://design/parameters`, `fusion://design/tree`.
**Prompts** (gotowe szablony): `parametric_bracket`, `prepare_for_3d_print`,
`assemble_components`.
Narzędzia inspekcyjne są oznaczone adnotacją `readOnlyHint`, a `delete` —
`destructiveHint` (klient MCP wie, które operacje są bezpieczne).

`operation` ∈ `new|join|cut|intersect`. Płaszczyzny: `XY|XZ|YZ`, token ściany
lub token płaszczyzny konstrukcyjnej. Osie: `X|Y|Z`, token linii/krawędzi lub
osi konstrukcyjnej.

### Typowy przepływ

```
get_state()                                   # orientacja
s = create_sketch("XY")                       # -> {"sketch":"skt1"}
sketch_rectangle("skt1", 0,0, 40,20)          # -> profile "prf1"
extrude("prf1", 10, "new")                    # -> body "bdy1"
query_entities("edges", "bdy1")               # -> tokeny krawędzi
fillet(["edg1","edg2","edg3","edg4"], 3)      # zaokrąglenie
export("step", "C:\\out\\part.step")
```

Otwory: `hole(sketch, x, y, diameter, depth|through_all, kind)` z pełnym
`HoleFeatures` (simple/counterbore/countersink). Dla operacji spoza gotowych
narzędzi zawsze zostaje `run_fusion_code`.

## Wydajność i wersja Personal

API Fusion jest **jednowątkowe** (tylko główny wątek UI) — nie ma zrównoleglenia,
więc wydajność = mniej round-tripów, niższa latencja i mniej zbędnych obliczeń.
Co robi ten serwer:

| Dźwignia | Mechanizm |
|---------|-----------|
| Mniej round-tripów | `batch(operations)` — dziesiątki operacji w **jednym** dispatchu na głównym wątku; zależności przez `$alias.path`. Również `run_fusion_code` (cała część w jednym snippetcie, z helperami `pt/rect/circle/extrude_profile`). |
| Niższa latencja | stałe połączenie TCP + **TCP_NODELAY** (bez przestojów Nagle/delayed-ACK) |
| Mniej zbędnych obliczeń | `physicalProperties.volume` i `area` liczone **tylko na żądanie** (`include_mass_props=True`); domyślnie szybka ścieżka |
| Mniej recompute'ów | `set_design_mode("direct")` — bez timeline/historii, szybsze i lżejsze jednorazowe budowanie na słabszym sprzęcie |
| Lżejszy payload | `screenshot` domyślnie 1024×768; `capture_to_file` zapisuje PNG bez zwracania base64 |
| Cache stanu | `get_state`/`query_entities` są cache'owane i inwalidowane po każdej mutacji (licznik generacji) oraz przy zmianach struktury/parametrów w UI (sygnatura designu) — powtórne odpytania są natychmiastowe |
| Mniej round-tripów w szkicu | `sketch_points`/`sketch_polyline`/`sketch_spline` — dziesiątki punktów/segmentów w jednym wywołaniu |
| Telemetria | `server_info` zwraca liczbę wywołań i czasy (avg/max ms) per operacja — łatwe wykrycie wolnych operacji |

**Personal — eksport.** Wersja Personal bywa ograniczona w formatach neutralnych
(STEP/IGES/SAT/SMT). `export(..., allow_fallback=True)` przy zablokowanym formacie
spróbuje **STL → F3D** i zwróci jasny komunikat zamiast surowego błędu. STL i F3D
zwykle działają zawsze.

**Szybkie budowanie z `batch`** (płytka 40×20×10 mm):

```json
[
  {"op": "create_sketch", "params": {"plane": "XY"}, "as": "s"},
  {"op": "sketch_rectangle", "params": {"sketch": "$s.sketch", "x1": 0, "y1": 0, "x2": 40, "y2": 20}, "as": "r"},
  {"op": "extrude", "params": {"profile": "$r.profiles[0].token", "distance": 10}}
]
```

## Konfiguracja

- Port socketu: `9123` (stały w add-inie; w serwerze nadpisywalny zmiennymi
  `FUSION_MCP_HOST` / `FUSION_MCP_PORT`).
- Timeout pojedynczej operacji: 300 s (długie przebudowy).
- Aktualizacje: `FUSION_MCP_REPO` (domyślnie `iQreu/fusion360-mcp`),
  `FUSION_MCP_BRANCH` (domyślnie `main`),
  `FUSION_MCP_AUTO_UPDATE` = `download` (domyślnie: sprawdź i pobierz przy
  starcie) | `notify` (tylko sprawdź) | `off` (bez sieci przy starcie).

## Aktualizacje z GitHuba

Nowa wersja **pobiera się automatycznie**, a instaluje **za zgodą użytkownika**:

1. Przy starcie serwera (czyli przy starcie Claude Desktop) wątek w tle
   porównuje `_version.__version__` z najnowszym release'em GitHuba (gdy brak
   release'ów — z wersją w `mcp_server/pyproject.toml` na gałęzi domyślnej)
   i **od razu pobiera** paczkę do katalogu tymczasowego
   (`%TEMP%\FusionMCP\updates`); dla checkoutu git robi `git fetch`.
   Start serwera nie jest przez to opóźniony.
2. Przy pierwszym użyciu dowolnego narzędzia Claude dostaje jednorazową notkę
   `fusionmcp_update` z numerem wersji i **release notes** — pokaże Ci ją
   i **zapyta o zgodę** na instalację. Ręcznie: `check_for_updates()`.
3. Po Twojej zgodzie Claude wywoła `apply_update(confirm=True)`:
   - czysty checkout git → `git pull --ff-only` (odmawia przy niezacommitowanych
     zmianach — wtedy użyj `method="zip"`),
   - inaczej → instaluje z wcześniej pobranego zipa (bez ponownego pobierania;
     w razie braku pobiera) i nadpisuje pliki (pomija `.git`, `.venv`,
     `__pycache__`), a następnie kopiuje add-in do folderu AddIns Fusion.
4. Zrestartuj add-in FusionMCP (Shift+S ▸ Stop, Run) i Claude Desktop.

`apply_update` jest oznaczone `destructiveHint` — bez `confirm=True` tylko
zwraca prośbę o potwierdzenie i niczego nie instaluje. Automatykę wyłączysz
zmienną `FUSION_MCP_AUTO_UPDATE=off` (lub `notify`, by tylko sprawdzać).

## Rozbudowa

Dodanie operacji = jedna funkcja `op_*` w
[commands.py](fusion_addin/FusionMCP/commands.py) + wpis w `DISPATCH`, oraz
odpowiadające narzędzie w [server.py](mcp_server/server.py). Add-in pracuje na
natywnych obiektach API, więc nie ma generowania kodu ze stringów. Nowa operacja
jest automatycznie dostępna też w `batch`.

## Testy i jakość

Logika niezależna od Fusion (rejestr tokenów, referencje `$alias.path` w batch,
ramkowanie socketu, konwersje jednostek, telemetria, inwalidacja cache) ma
testy jednostkowe (`adsk` jest mockowany):

```powershell
python -m pip install -e "mcp_server[dev]"   # albo: pip install pytest ruff
python -m ruff check .
python -m pytest
```

CI (GitHub Actions, [.github/workflows/ci.yml](.github/workflows/ci.yml)) uruchamia
ruff + pytest na Pythonie 3.10 i 3.12 przy każdym push/PR.

**Publikacja wersji**: podbij wersję (`mcp_server/_version.py`,
`mcp_server/pyproject.toml`, `VERSION` w `commands.py`), zrób tag `vX.Y.Z` i
wypchnij go — workflow [release.yml](.github/workflows/release.yml) opublikuje
release z automatycznymi release notes, które updater pokaże użytkownikom.

## Rozwiązywanie problemów

- **„Cannot reach the FusionMCP add-in"** — Fusion nie działa albo add-in nie
  jest uruchomiony (Shift+S ▸ Run). Sprawdź log add-ina:
  `%TEMP%\FusionMCP\fusionmcp.log` (rotujący, z czasami operacji) lub log Fusion
  („FusionMCP: bridge listening…").
- **„No active Fusion design"** — przełącz się na workspace **DESIGN** i otwórz dokument.
- **Port zajęty** — zrestartuj Fusion (zostało stare nasłuchiwanie po awarii).
- Zmiana kodu add-ina wymaga Stop+Run add-ina (lub restartu Fusion).
```
