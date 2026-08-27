"""Tests for v1.15.0: FreeCAD headless bridge (FEM, inspect, convert,
run_script). FreeCAD is an external install — everything protocol-level is
tested with a faked subprocess; live tests run only where freecadcmd exists
(the reference dev box has FreeCAD 1.1.3, CI runners do not)."""
import json
import os

import freecad
import pytest

_LIVE = freecad.detect() is not None


# --------------------------------------------------------------------------- #
# detection / installation report
# --------------------------------------------------------------------------- #
def test_detect_env_override_wins(tmp_path, monkeypatch):
    exe = tmp_path / 'freecadcmd.exe'
    exe.write_bytes(b'x')
    monkeypatch.setenv('FUSION_MCP_FREECAD', str(exe))
    assert freecad.detect() == str(exe)


def test_detect_env_override_ignored_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv('FUSION_MCP_FREECAD', str(tmp_path / 'nope.exe'))
    # Falls through to PATH/Program Files — must not return the bogus path.
    assert freecad.detect() != str(tmp_path / 'nope.exe')


def test_require_hint_when_not_installed(monkeypatch):
    monkeypatch.setattr(freecad, 'detect', lambda: None)
    with pytest.raises(RuntimeError) as err:
        freecad._require()
    assert 'freecad.org' in str(err.value)
    assert 'FUSION_MCP_FREECAD' in str(err.value)


def test_info_not_installed(monkeypatch):
    monkeypatch.setattr(freecad, 'detect', lambda: None)
    rep = freecad.info()
    assert rep['installed'] is False
    assert 'freecad.org' in rep['hint']


# --------------------------------------------------------------------------- #
# generated scripts must at least be valid Python (the indent-level contract
# between _PRELUDE and the pre-indented bodies is easy to break silently)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('body', [
    freecad._INSPECT_BODY,
    freecad._CONVERT_BODY,
    freecad._RUN_BODY,
    freecad._FEM_BODY,
    freecad._FEM_BODY.replace(
        'P["mesh_max_mm"]',
        '(P["mesh_max_mm"] or round(solid.BoundBox.DiagonalLength / 12.0,'
        ' 2))'),
])
def test_script_templates_compile(body):
    compile(freecad._PRELUDE + body + freecad._EPILOGUE, '<job>', 'exec')


# --------------------------------------------------------------------------- #
# _run protocol with a faked freecadcmd
# --------------------------------------------------------------------------- #
class _Proc:
    returncode = 0
    stdout = stderr = ''


def _fake_subprocess(result_payload=None, write_result=True):
    """subprocess.run stand-in: read params.json next to the script, write
    result.json (or don't, to exercise the no-result path)."""
    captured = {}

    def fake_run(cmd, **kw):
        script = cmd[1]
        here = os.path.dirname(script)
        with open(os.path.join(here, 'params.json'), encoding='utf-8') as fh:
            captured['params'] = json.load(fh)
        captured['script'] = open(script, encoding='utf-8').read()
        if write_result:
            with open(os.path.join(here, 'result.json'), 'w',
                      encoding='utf-8') as fh:
                json.dump(result_payload, fh)
        return _Proc()
    return fake_run, captured


def test_run_roundtrip_and_error_paths(monkeypatch):
    fake, captured = _fake_subprocess({'result': 42})
    monkeypatch.setattr(freecad.subprocess, 'run', fake)
    monkeypatch.setattr(freecad, 'detect', lambda: 'fc.exe')
    assert freecad.run_script('result = 42') == {'result': 42}
    assert captured['params']['code'] == 'result = 42'
    compile(captured['script'], '<job>', 'exec')

    fake, _ = _fake_subprocess({'error': 'Traceback ... boom'})
    monkeypatch.setattr(freecad.subprocess, 'run', fake)
    with pytest.raises(RuntimeError) as err:
        freecad.run_script('x')
    assert 'boom' in str(err.value)

    fake, _ = _fake_subprocess(write_result=False)
    monkeypatch.setattr(freecad.subprocess, 'run', fake)
    with pytest.raises(RuntimeError) as err:
        freecad.run_script('x')
    assert 'no result' in str(err.value)


# --------------------------------------------------------------------------- #
# fem_analyze validation and material handling
# --------------------------------------------------------------------------- #
def _touch(tmp_path, name='part.step'):
    p = tmp_path / name
    p.write_bytes(b'ISO-10303-21;')
    return str(p)


def test_fem_validates_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(freecad, 'detect', lambda: 'fc.exe')
    step = _touch(tmp_path)
    with pytest.raises(RuntimeError, match='not found'):
        freecad.fem_analyze(str(tmp_path / 'no.step'), ['zmin'],
                            [{'faces': ['zmax'], 'force_n': 1}])
    with pytest.raises(RuntimeError, match='fixed faces'):
        freecad.fem_analyze(step, [], [{'faces': ['zmax'], 'force_n': 1}])
    with pytest.raises(RuntimeError, match='load'):
        freecad.fem_analyze(step, ['zmin'], [])
    with pytest.raises(RuntimeError, match='neither force_n'):
        freecad.fem_analyze(step, ['zmin'], [{'faces': ['zmax']}])
    with pytest.raises(RuntimeError, match='Unknown material'):
        freecad.fem_analyze(step, ['zmin'],
                            [{'faces': ['zmax'], 'force_n': 1}],
                            material='unobtainium')
    with pytest.raises(RuntimeError, match='custom'):
        freecad.fem_analyze(step, ['zmin'],
                            [{'faces': ['zmax'], 'force_n': 1}],
                            material='custom')


def test_fem_printed_material_warns_and_derates(tmp_path, monkeypatch):
    step = _touch(tmp_path)
    fake, captured = _fake_subprocess(
        {'safety_factor': 10.0, 'von_mises_max_mpa': 5.0, 'warnings': []})
    monkeypatch.setattr(freecad.subprocess, 'run', fake)
    monkeypatch.setattr(freecad, 'detect', lambda: 'fc.exe')
    rep = freecad.fem_analyze(step, ['zmin'],
                              [{'faces': ['zmax'], 'force_n': 100}],
                              material='petg')
    assert rep['safety_factor_printed'] == 6.0
    warnings = captured['params']['warnings']
    assert any('anisotropic' in w for w in warnings)
    assert captured['params']['material']['E'] == 2100.0
    # Steel gets no derating key.
    rep = freecad.fem_analyze(step, ['zmin'],
                              [{'faces': ['zmax'], 'force_n': 100}])
    assert 'safety_factor_printed' not in rep


def test_fem_custom_material(tmp_path, monkeypatch):
    step = _touch(tmp_path)
    fake, captured = _fake_subprocess({'ok': True})
    monkeypatch.setattr(freecad.subprocess, 'run', fake)
    monkeypatch.setattr(freecad, 'detect', lambda: 'fc.exe')
    freecad.fem_analyze(step, ['Face1'], [{'faces': ['Face2'],
                                           'pressure_mpa': 2.0}],
                        material='custom', E=1000, nu=0.4, density=1100)
    mat = captured['params']['material']
    assert mat == {'name': 'Custom', 'E': 1000.0, 'nu': 0.4,
                   'density': 1100.0, 'yield': 1.0}
    assert any('yield_mpa' in w for w in captured['params']['warnings'])
    load = captured['params']['loads'][0]
    assert load['pressure_mpa'] == 2.0 and load['force_n'] is None


# --------------------------------------------------------------------------- #
# convert validation
# --------------------------------------------------------------------------- #
def test_convert_validates_formats(tmp_path, monkeypatch):
    monkeypatch.setattr(freecad, 'detect', lambda: 'fc.exe')
    with pytest.raises(RuntimeError, match='not found'):
        freecad.convert(str(tmp_path / 'no.step'), str(tmp_path / 'o.stl'))
    bad = tmp_path / 'part.xyz'
    bad.write_bytes(b'x')
    with pytest.raises(RuntimeError, match='input format'):
        freecad.convert(str(bad), str(tmp_path / 'o.stl'))


# --------------------------------------------------------------------------- #
# live tests — only where FreeCAD is actually installed
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _LIVE, reason='FreeCAD not installed')
def test_live_run_script_and_inspect(tmp_path):
    step = str(tmp_path / 'box.step')
    rep = freecad.run_script(
        'import Part\n'
        'box = Part.makeBox(30, 20, 10)\n'
        'box.exportStep(%r)\n'
        'result = round(box.Volume, 1)\n' % step)
    assert rep == {'result': 6000.0}
    ins = freecad.inspect(step)
    solid = ins['report'][0]
    assert ins['kind'] == 'solid' and solid['valid']
    assert abs(solid['volume_mm3'] - 6000.0) < 0.1
    assert len(solid['faces']) == 6
    assert all(f['type'] == 'Plane' for f in solid['faces'])


@pytest.mark.skipif(not _LIVE, reason='FreeCAD not installed')
def test_live_fem_axial_stress_matches_hand_calc(tmp_path):
    # 100x10x10 bar, 500 N axially on the far end: sigma = F/A = 5 MPa.
    step = str(tmp_path / 'bar.step')
    freecad.run_script(
        'import Part\n'
        'Part.makeBox(100, 10, 10).exportStep(%r)\n'
        'result = 1\n' % step)
    rep = freecad.fem_analyze(step, fixed=['xmin'],
                              loads=[{'faces': ['xmax'], 'force_n': 500}],
                              material='steel', mesh_max_mm=8.0)
    assert 4.0 < rep['von_mises_max_mpa'] < 6.5
    assert rep['safety_factor'] > 30
    assert rep['mass_g'] == pytest.approx(78.5, rel=0.01)
    assert rep['fixed_faces']


# --------------------------------------------------------------------------- #
# mech: fits, bearings, circlips, o-rings, belts
# --------------------------------------------------------------------------- #
mech = pytest.importorskip('mech')


@pytest.mark.skipif(mech.isofits is None, reason='isofits not installed')
def test_fit_suggest_matches_published_iso286():
    rep = mech.fit_suggest(25.02, feature='shaft', application='sliding')
    assert rep['nominal_mm'] == 25 and rep['fit'] == 'H7/g6'
    assert rep['hole']['deviation_um'] == [21.0, 0.0]
    assert rep['shaft']['deviation_um'] == [-7.0, -20.0]
    assert rep['result'] == {'kind': 'clearance', 'range_um': [7.0, 41.0]}
    assert rep['hole']['limits_mm'] == [25.0, 25.021]

    press = mech.fit_suggest(40, feature='hole', application='press')
    assert press['result']['kind'] == 'interference'
    explicit = mech.fit_suggest(25, fit='H7/h6')
    assert explicit['fit'] == 'H7/h6'
    assert explicit['result']['range_um'][0] == 0.0


@pytest.mark.skipif(mech.isofits is None, reason='isofits not installed')
def test_fit_suggest_flags_off_nominal():
    rep = mech.fit_suggest(19.6, feature='shaft')
    assert rep['nominal_mm'] == 20 or rep['nominal_mm'] == 19
    assert 'note' in rep


def test_fit_suggest_validation():
    with pytest.raises(RuntimeError):
        mech.fit_suggest(500)
    with pytest.raises(RuntimeError):
        mech.fit_suggest(20, feature='bolt')
    with pytest.raises(RuntimeError):
        mech.fit_suggest(20, application='wobbly')


def test_bearing_lookup_by_name_and_seat():
    r608 = mech.bearing_lookup('608-2RS')
    assert r608['bearings'][0] == {'designation': '608', 'bore_mm': 8,
                                   'od_mm': 22, 'width_mm': 7}
    # Scanned pocket Ø21.9 with an 8 mm shaft -> 608 exactly; OD alone is
    # ambiguous (608/627/6900 all have OD 22) and returns all candidates.
    seat = mech.bearing_lookup(bore=8, od=21.9)
    assert [b['designation'] for b in seat['bearings']] == ['608']
    ambiguous = mech.bearing_lookup(od=21.9)
    assert {b['designation'] for b in ambiguous['bearings']} == \
        {'608', '627', '6900'}
    both = mech.bearing_lookup(bore=17, od=40)
    assert [b['designation'] for b in both['bearings']] == ['6203']
    assert 'printed' in seat['seat_advice']
    with pytest.raises(RuntimeError, match='Unknown designation'):
        mech.bearing_lookup('99999')
    with pytest.raises(RuntimeError, match='No vendored bearing'):
        mech.bearing_lookup(bore=97.3)


def test_circlip_tables():
    ext = mech.circlip_lookup(20, kind='shaft')
    assert ext['groove_diameter_mm'] == 19.0
    assert ext['groove_width_mm'] == 1.3
    assert ext['ring_thickness_mm'] == 1.2
    internal = mech.circlip_lookup(20, kind='bore')
    assert internal['groove_diameter_mm'] == 21.0
    assert internal['ring_thickness_mm'] == 1.0
    # Groove must always be wider than the ring is thick.
    for table in (mech.DIN_471, mech.DIN_472):
        for d1, (s, d2, m, t) in table.items():
            assert m > s, (table is mech.DIN_471, d1)
            # External grooves cut in, internal grooves cut out.
            if table is mech.DIN_471:
                assert abs((d1 - 2 * t) - d2) < 1e-9, d1
            else:
                assert abs((d1 + 2 * t) - d2) < 1e-9, d1
    nonstd = mech.circlip_lookup(23, kind='shaft')
    assert 'error' in nonstd
    assert {n['d1_mm'] for n in nonstd['nearest']} >= {22, 24}


def test_oring_gland_rules():
    rep = mech.oring_gland(3.0, id_mm=24.0, seal='static_radial')
    # 20% mean squeeze -> depth 2.4; fill 75% -> width ~3.93.
    assert rep['groove_depth_mm'] == 2.4
    assert 3.8 < rep['groove_width_mm'] < 4.1
    assert rep['nearest_standard_cs_mm'] == 3.0
    groove = rep['radial_groove']
    assert groove['shaft_groove_root_mm'] == pytest.approx(24.48)
    assert groove['bore_mm'] == pytest.approx(24.48 + 4.8)
    face = mech.oring_gland(2.62, id_mm=30, seal='face')
    assert 'face_groove' in face
    squashed = mech.oring_gland(2.2)
    assert 'warning' in squashed
    with pytest.raises(RuntimeError):
        mech.oring_gland(3.0, seal='vacuum')
    with pytest.raises(RuntimeError):
        mech.oring_gland(12.0)


def test_belt_calc_roundtrip():
    import math
    rep = mech.belt_calc('GT2', 20, 40, belt_teeth=200)
    d1 = 20 * 2 / math.pi
    assert rep['pulley_small']['pitch_diameter_mm'] == pytest.approx(
        round(d1, 3))
    assert rep['belt']['length_mm'] == 400.0
    c = rep['center_distance_mm']
    # Round-trip: that center distance must give back the same belt.
    back = mech.belt_calc('GT2', 20, 40, center_distance_mm=c)
    assert back['belt']['teeth'] == 200
    assert back['adjustment_mm'] == pytest.approx(0.0, abs=0.01)
    with pytest.raises(RuntimeError):
        mech.belt_calc('GT9', 20)
    with pytest.raises(RuntimeError):
        mech.belt_calc('GT2', 20, 40, belt_teeth=40)  # too short


def test_versions_in_sync_v115():
    import commands
    from _version import __version__
    assert __version__ == '1.15.0'
    assert commands.VERSION == __version__
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'mcp_server', 'pyproject.toml')
    with open(pyproject, encoding='utf-8') as fh:
        assert 'version = "%s"' % __version__ in fh.read()
