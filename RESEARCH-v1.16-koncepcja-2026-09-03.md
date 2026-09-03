# Koncepcja v1.16 — „od skanu do części, która wytrzyma” (2026-09-03)

Stan wyjściowy: v1.15.0 (c0a170a, nie otagowane) = FreeCAD FEM liniowo-statyczny +
narzędzia części zamiennych; 183 narzędzi / 166 opów; 235 testów.

Fakty zewnętrzne sprawdzone dziś:
- Fusion: **brak update'u po lipcu 2026** (2704.1.53). API Constraints jest
  przebudowywane, nic nowego do adopcji → v1.16 buduje na tym, co mamy, plus
  FreeCAD/Python.
- FreeCAD FEM + CalculiX headless: `AnalysisType` `frequency` i `buckling`
  działają (ccxtools; wiele wartości własnych = osobne obiekty wyników;
  parametr accuracy wyboczenia od 1.1). CalculiX liczy też nieliniowo i termicznie.
- mcp 1.29.1 = ostatni 1.x; `pip install mcp` daje dziś 2.x → migracja w tej
  fali (shim).

Zasada fali: **każda lekcja z projektów live (kratka, antena, zegar, drezyna)
staje się narzędziem**, zamiast skryptu-drivera pisanego od nowa za każdym razem.

---

## 1. Wytrzymałość — „czy to wytrzyma i gdzie pęknie”

### P1
1. **`freecad_fem(analysis='static'|'frequency'|'buckling')`**
   - frequency: N pierwszych częstości własnych [Hz] + opis postaci (kierunek
     dominujący, udział masy) → uchwyty w aucie (drgania 20–200 Hz), ramki,
     wsporniki.
   - buckling: mnożnik obciążenia krytycznego dla cienkich słupków/żeber druku.
   - Wynik zawsze z sekcją `interpretation` po ludzku („1. postać 87 Hz — poniżej
     zakresu drgań silnika 25–60 Hz”, „SF wyboczenia 1.4 — za mało dla FDM”).
2. **Hotspot readback do Fusiona**: FEM zwraca top-3 skupiska naprężeń
   (współrzędne mm, najbliższa ściana, wartość) → nowa opcja
   `annotate_in_fusion=true` stawia znaczniki custom graphics (op `annotate`
   już jest) + screenshot. „Gdzie pęknie” widać w viewporcie, nie w tabeli.
3. **Kierunek naprężeń głównych → orientacja druku**: z wyniku FreeCAD czytamy
   `PS1Vector` w hotspotach; porównujemy z kandydatami orientacji z
   `print_check` (ściany kontaktu ze stołem). Raport: „warstwy prostopadłe do
   rozciągania w hotspocie → spodziewana wytrzymałość ×0,5–0,7; obróć o 90°
   wokół X” + zderatowany SF (dziś stały ×0,6 — będzie kierunkowy).
4. **Wiarygodność FEM**: `mesh_convergence=true` liczy 2× (siatka auto i ×0,7),
   raportuje zmianę max vM w %; ostrzeżenie o **osobliwości**, gdy max/p95 > 3
   i hotspot leży na ostrej krawędzi wklęsłej („daj tam R1, nie zagęszczaj siatki”).
5. **`hand_calc`** (mech.py, czysty Python) — kontrola FEM i szybkie odpowiedzi:
   belka (wspornikowa/dwupodporowa: ugięcie, naprężenie), **zatrzask**
   (cantilever snap-fit: odkształcenie, siła montażu, dopuszczalne wychylenie
   dla PLA/PETG/PA), pasowanie wtłaczane (Lamé: naprężenia, siła wtłaczania),
   śruba (napięcie wstępne, ścinanie, długość skręcenia gwintu drukowanego),
   naczynie cienkościenne, zawias żywy. Każdy wynik z użytym wzorem i założeniami.
6. **`material_advisor`**: środowisko (komora silnika / kabina w słońcu /
   zewnątrz / kontakt z paliwem-olejem / mokro) → ranking
   PLA/PETG/ABS/ASA/PA/PC/TPU/PP z HDT/Tg, UV, chemią i **deratingiem granicy
   plastyczności w temperaturze** (PLA w kabinie latem 60–70 °C = 0). Tabela
   danych, nie wiedza modelu.
7. **`param_sweep`**: zmienia parametr użytkownika Fusiona (np. `grubosc`) po
   liście wartości → eksport STEP → `freecad_fem` (lub tylko masa/interferencja)
   → tabela SF vs masa vs wartość. „Jaka grubość żebra” z liczbą, do 5 wartości,
   każda iteracja z przywróceniem parametru w `finally`.

### P2
- Kontakt (tie) między bryłami złożenia; obciążenie termiczne (kabina 80 °C
  + siła); materiał ortotropowy FDM w CalculiX (`*ELASTIC, TYPE=ENGINEERING
  CONSTANTS`) — dopiero gdy p.3 okaże się za grube.
- Poza zakresem: zmęczenie (bez sensu dla FDM bez danych), Fusion Simulation
  (brak API — potwierdzone).

---

## 2. Skany — z siatki do cech, nie do splajnów

### P1
1. **`scan_frame`** (datum alignment bez modelu CAD): z prymitywów
   `scan_analyze` wybieramy A/B/C (płaszczyzna bazowa, oś walca, płaszczyzna
   boczna) → transformacja sztywna; skan ląduje w osiach z płaszczyzną
   montażową na Z=0. Zapis wyrównanego STL + macierz; opcja import do Fusiona
   na origin. Dziś `scan_align` wyrównuje tylko do istniejącego modelu.
2. **`scan_segment`**: region growing po normalnych/krzywiźnie → łaty
   (płaska / walcowa / swobodna) z grafem sąsiedztwa i wymiarami; eksport jako
   face groups do Fusiona (`generateFaceGroups` API nie daje kontroli — nasze da).
3. **`scan_features`** — spis cech w mm jak `photo_measure`, ale na siatce:
   otwory (wewnętrzne pętle brzegowe łat płaskich → okrąg Kasa: środek/Ø/
   głębokość i czy przelotowy), rozstawy i wzory otworów, kołki/kołnierze
   (walce wypukłe), rowki, promienie zaokrągleń krawędzi (fit walca/torusa
   wzdłuż krawędzi łat). Z uwagą o niedomiarze błyszczących czarnych
   powierzchni (lekcja 2–3 mm).
4. **`scan_profile` → szkic z linii i łuków**: przecięcie płaszczyzną →
   polilinia → segmentacja na linie/łuki (detekcja narożników, tolerancja,
   snap do kątów 0/45/90 i „ładnych” promieni) → **szkic Fusiona z prawdziwą
   geometrią, wiązaniami i wymiarami** (sketch ops + `auto_constrain`), gotowy
   do extrude. To rdzeń RE pryzmatycznego; dziś mamy tylko punkty i splajny.
5. **`thread_identify`** (backlog P2 → teraz): fit walca (mamy
   `_refine_cylinder`) → residua radialne rozwinięte po kącie/wysokości → FFT
   skoku → snap do ISO 261/262 + UNC/UNF; zwraca „M8×1.25 zewnętrzny, długość 14”.
6. **`mesh_offset`** (generator wnęki): dylatacja wokselowa skanu o +d mm
   (trimesh voxel → marching cubes → wygładzenie) z trybami `offset` (wierny)
   i `monotone` (gwarancja wsuwania po osi — dzisiejsze `cavity_sections`
   jako przypadek szczególny). Wynik STL → import → combine-cut.
   Cztery projekty pisały to ręcznie.
7. **`fit_report`**: jedno wywołanie zamiast skryptu weryfikacji: `fit_check`
   (kolizje/prześwit p5/p50) + grubość ścian między wnęką a skorupą +
   szczelina rantu + `print_check` + `dfm` → PASS/FAIL z progami. Ten skrypt
   powstawał 5 razy — ma być narzędziem.

### P2
- **Heatmapa odchyłki w viewporcie**: `scan_deviation` → CustomGraphicsMesh
  z `CustomGraphicsVertexColorEffect` (do weryfikacji live) → screenshot.
- **`scan_symmetry`**: płaszczyzna symetrii lustrzanej (PCA + ICP kopii
  lustrzanej), asymetria w mm; skan połówki → mirror.
- **Chmury punktów** (PLY/XYZ/E57 bez trójkątów): open3d jako extra
  `[re-cloud]` (MIT, ciężki) — Poisson/BPA → STL. Wiele skanerów daje tylko punkty.
- **Sklejanie 2 skanów** (góra/dół): align skan↔skan + unia manifold3d.
- geomfitty (torus) jako vendor dla rowków o-ringów.

---

## 3. Reverse engineering — warstwa przepływu

1. **Prompt `reverse_engineer_scan` v2** = frame → segment → features → profile
   → build → fit_report, z decyzją „pryzmatyczny (szkice) vs swobodny
   (loft/sekcje)” na podstawie udziału łat płaskich/walcowych z `scan_segment`.
2. **Tokeny brył z odciskiem geometrycznym**: registry zapisuje (objętość,
   centroid, bbox) i po `combine`/`move`/`deleteMe` re-rozwiązuje token po
   odcisku, gdy uchwyt zdechł. Lekcje: nazwy po finishEdit ginęły, combine
   zmieniał nazwy, Move psuł uczestników. Infrastruktura, ale największy
   zysk niezawodności w budowie wieloetapowej.
3. **`silhouette_match`** (foto-RE P3 → P2): op `silhouette` (mamy) rzutuje
   kontur CAD na zrektyfikowane zdjęcie → odchyłka Hausdorffa w mm. „Czy
   dobrze zrozumiałem zdjęcie” jako liczba.
4. Reszta foto-RE P2 (photo_depth ONNX, vtracer, arkusz ArUco) — bez zmiany
   priorytetu, wchodzi jeśli zostanie budżet.
5. **`[re-ml]` CAD-Recode/cadrille** (pointcloud → CadQuery): P3, licencja
   CC-BY-NC → tylko opt-in z ostrzeżeniem; CadQuery wymaga OCP (SAC blokuje
   na tej maszynie) — realne tylko jako generator kodu do podglądu.

---

## 4. Projektowanie modeli — mniej rozbitych buildów

### P1
1. **`fillet_max_radius` + fallback**: bisekcja promienia przez `validate_only`
   (mamy mechanizm) → największy wykonalny R; opcja `fallback='chamfer'`.
   BLEND_TOO_BIG ugryzł 3 razy.
2. **Generatory detali druku (DFM-poprawne z automatu)**: `add_boss` (słupek
   pod heat-set/śrubę z `hole_spec`, zaokrąglenie u podstawy, żebra),
   `add_snap_fit` (zatrzask wspornikowy policzony przez `hand_calc`),
   `add_clip_fir_tree` („choinka” w otwór Ø z tolerancją — motoryzacja),
   `add_living_hinge`, `add_rib_pattern`. Parametryczne, w timeline, z nazwami.
3. **`new_document(kind='part'|'assembly')`**:
   `documents.add(FusionDesignDocumentType)` daje wiele komponentów; dokument
   domyślny Fusion 2026 to CZĘŚĆ (lekcja drezyny).
4. **`design_mode`**: odczyt/przełączenie parametric↔direct z ostrzeżeniami
   (direct: brak copyPaste, auto-scalanie stykających się brył, brak timeline);
   `get_state` naprawione dla direct (dziś rzuca na `allParameters`/`timeline`).
   Podpowiedź automatyczna, gdy timeline > 300 feature'ów (timeout 300 s).
5. **Zadania długie**: `async=true` na ciężkich opach (import dużej siatki,
   wipe timeline, mesh_to_brep; timeline_builder już tak działa) → `job_id` +
   `job_status`. Własny polling, bo MCP Tasks nadal nie ma w SDK ani klientach.
6. **`interference` rozszerzone** o `ignore_coincident=true` i podsumowanie par
   (z drezyny: 40 brył, 0 przenikań — idealny test montażu).
7. **`capabilities_probe`**: jedno wywołanie sonduje wszystkie Preview API,
   których używamy (mesh*Features, triangleFaceGroupTempIds, silhouette,
   canvases, DrawingManager, CornerClosure…) → macierz „jest/brak” na tym
   buildzie. Zastępuje ręczny checklist api_introspect po każdym update.

### P2
- `loft_from_sections`: automatyczna kotwica + orientacja CCW + wykrycie
  „polilinia zamiast splajnu” (obie lekcje kosztowały przebudowy).
- `sketch_fit_geometry(points)` jako samodzielne narzędzie (wspólne z 2.4).
- FreeCAD TechDraw DXF jako dedykowane narzędzie; FreeCAD CAM fallback.

---

## 5. Infrastruktura fali
- **mcp 2.x**: shim `try: from mcp.server import MCPServer as FastMCP` + pin
  `>=1.29.1,<3`; elicit → wzorzec Resolve pod `hasattr`; test w scratch-venv
  z 2.x (opentelemetry, pydantic-core DLL pod SAC). Lock na socket już jest.
- Nowe extras: `[re-cloud]` open3d (P2); FEM bez nowych zależności (FreeCAD
  zewnętrzny).
- Toolsety: `strength` (fem/hand_calc/material/sweep), `scan` rozszerzony.
- Testy: fixtury syntetyczne (walec z gwintem generowany trimeshem dla
  thread_identify; płytka z 4 otworami dla scan_features; L-profil dla
  scan_profile) — bez zależności od ray backendu (lekcja rtree).

## 6. Kolejność wykonania (propozycja)
1. capabilities_probe + design_mode/get_state fix + new_document (odblokowuje
   resztę, ~1 dzień).
2. Skany P1: scan_frame → scan_segment → scan_features → scan_profile →
   thread_identify → mesh_offset → fit_report (rdzeń fali).
3. Wytrzymałość P1: freecad_fem analysis types → hotspot/PS1/orientacja →
   convergence → hand_calc → material_advisor → param_sweep.
4. Projektowanie P1: fillet_max_radius, generatory, interference, async jobs.
5. mcp 2.x shim + prompt RE v2 + CHANGELOG + testy → v1.16.0.

Szacunek: ~25 nowych narzędzi, ~60 testów. Bramka live: FEM frequency na
realnym STEP-ie z Fusiona, scan_profile na skanie kratki/anteny,
thread_identify na śrubie M8.

## 7. Otwarte decyzje (dla usera)
- Jedna duża fala czy podział: v1.16 „skany+RE”, v1.17 „wytrzymałość”?
  Rekomendacja: podzielić — skany są pilniejsze (priorytet z 2026-08-17),
  a FEM frequency/buckling wymaga prototypu live przed commitem.
- open3d ([re-cloud]) — ~60 zależności; dodać dopiero, gdy pojawi się skan
  w postaci chmury punktów.
- CAD-Recode (CC-BY-NC) — czy w ogóle w repo publicznym.
