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
`query_entities(kind, target, include_mass_props=False)`
**Szkice**: `create_sketch`, `sketch_rectangle`, `sketch_circle`, `sketch_line`,
`sketch_arc`, `sketch_polygon`
**Cechy**: `extrude`, `revolve`, `fillet`, `chamfer`, `shell`, `combine`,
`rectangular_pattern`, `circular_pattern`, `mirror`, `move_body`, `delete`
**Parametry**: `list_parameters`, `set_parameter`, `add_parameter`
**I/O**: `export(format, path, allow_fallback=True)` (step/iges/sat/smt/f3d/stl),
`screenshot(direction, fit)`, `capture_to_file(direction, fit)`, `fit_view`, `save`
— presety kamery: `current|front|back|left|right|top|bottom|iso|iso-top-right|iso-top-left|iso-bottom-right|iso-bottom-left`
**Wydajność**: `batch(operations)` — wiele operacji w jednym round-tripie,
`set_design_mode("direct"|"parametric")`
**Furtka**: `run_fusion_code(code)` — dowolny kod Fusion Python API w jednym wywołaniu

`operation` ∈ `new|join|cut|intersect`. Płaszczyzny: `XY|XZ|YZ` lub token ściany.
Osie: `X|Y|Z` lub token linii/krawędzi.

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

Otworów nie ma jako osobnego narzędzia — najprościej: `sketch_circle` na ścianie
+ `extrude(..., "cut")`, albo `run_fusion_code` dla `HoleFeatures`.

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

## Rozbudowa

Dodanie operacji = jedna funkcja `op_*` w
[commands.py](fusion_addin/FusionMCP/commands.py) + wpis w `DISPATCH`, oraz
odpowiadające narzędzie w [server.py](mcp_server/server.py). Add-in pracuje na
natywnych obiektach API, więc nie ma generowania kodu ze stringów.

## Rozwiązywanie problemów

- **„Cannot reach the FusionMCP add-in"** — Fusion nie działa albo add-in nie
  jest uruchomiony (Shift+S ▸ Run). Sprawdź log Fusion: `app.log(...)` pisze
  „FusionMCP: bridge listening…".
- **„No active Fusion design"** — przełącz się na workspace **DESIGN** i otwórz dokument.
- **Port zajęty** — zrestartuj Fusion (zostało stare nasłuchiwanie po awarii).
- Zmiana kodu add-ina wymaga Stop+Run add-ina (lub restartu Fusion).
```
