"""Tests for the v1.12.0 workshop wave: slicer G-code parsing and detection,
fastener tables, DFM heuristics, and the server toolset filter."""
import os

import dfm
import fasteners
import pytest
import scan
import slicer

requires_re = pytest.mark.skipif(
    scan.trimesh is None, reason="optional 're' extras not installed")


# --------------------------------------------------------------------------- #
# slicer
# --------------------------------------------------------------------------- #
PRUSA_FOOTER = """
; filament used [mm] = 1873.45
; filament used [cm3] = 4.51
; filament used [g] = 5.59
; total filament cost = 0.14
; estimated printing time (normal mode) = 1h 33m 12s
"""

ORCA_FOOTER = """
; total filament used [g] : 12.30
; total filament cost : 0.31
; total estimated time: 2h 5m 7s
"""


def test_parse_stats_prusa_footer():
    stats = slicer.parse_stats(PRUSA_FOOTER)
    assert stats['time_s'] == 1 * 3600 + 33 * 60 + 12
    assert stats['filament_g'] == 5.59
    assert stats['filament_cm3'] == 4.51
    assert abs(stats['filament_m'] - 1.87345) < 1e-6
    assert stats['cost'] == 0.14


def test_parse_stats_orca_footer():
    stats = slicer.parse_stats(ORCA_FOOTER)
    assert stats['time_s'] == 2 * 3600 + 5 * 60 + 7
    assert stats['filament_g'] == 12.30


def test_parse_duration_days_and_partials():
    assert slicer.parse_duration('2d 11h 45m 51s') == 2 * 86400 + 11 * 3600 + 45 * 60 + 51
    assert slicer.parse_duration('58m') == 58 * 60
    assert slicer.parse_duration('') is None


def test_estimate_missing_model_and_missing_slicer(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError) as err:
        slicer.estimate(str(tmp_path / 'nope.stl'))
    assert 'not found' in str(err.value)
    model = tmp_path / 'part.stl'
    model.write_bytes(b'solid x\nendsolid x\n')
    monkeypatch.setattr(slicer, 'detect', lambda: [])
    with pytest.raises(RuntimeError) as err:
        slicer.estimate(str(model))
    assert 'FUSION_MCP_SLICER' in str(err.value)


def test_estimate_parses_fake_prusa_run(tmp_path, monkeypatch):
    model = tmp_path / 'part.stl'
    model.write_bytes(b'solid x\nendsolid x\n')
    fake_exe = str(tmp_path / 'prusa-slicer-console.exe')

    def fake_run(cmd, **kw):
        out_path = cmd[cmd.index('-o') + 1]
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.write('G1 X0 Y0\n' + PRUSA_FOOTER)

        class R:
            returncode = 0
            stdout = stderr = ''
        return R()

    monkeypatch.setattr(slicer, 'detect',
                        lambda: [{'slicer': 'prusa', 'path': fake_exe}])
    monkeypatch.setattr(slicer.subprocess, 'run', fake_run)
    monkeypatch.delenv('FUSION_MCP_SLICER_PROFILE', raising=False)
    keep = tmp_path / 'kept.gcode'
    rep = slicer.estimate(str(model), gcode_out=str(keep),
                          price_per_kg=100.0, printer_watts=120,
                          energy_price_kwh=1.0)
    assert rep['print_time'] == '1h 33m'
    assert rep['filament_g'] == 5.59
    # cost from the G-code footer (0.14) + energy 0.12 kW * 1.553 h * 1.0
    assert rep['cost'] == round(0.14 + 0.12 * (5592 / 3600), 2)
    assert rep['energy_kwh'] == round(0.12 * (5592 / 3600), 2)
    assert keep.exists()
    assert 'defaults' in rep['note']


# --------------------------------------------------------------------------- #
# fasteners
# --------------------------------------------------------------------------- #
def test_lookup_m3_core_numbers():
    row = fasteners.lookup('M3')
    assert row['pitch_mm'] == 0.5
    assert row['tap_drill_mm'] == 2.5
    assert row['clearance_mm'] == {'close': 3.2, 'normal': 3.4, 'loose': 3.6}
    assert row['socket_head']['dk'] == 5.5
    assert row['heat_set']['hole'] == 4.0


def test_lookup_normalises_size_spelling():
    assert fasteners.lookup('m4')['size'] == 'M4'
    assert fasteners.lookup('2,5')['size'] == 'M2.5'
    with pytest.raises(RuntimeError):
        fasteners.lookup('M7')


def test_hole_spec_variants():
    clearance = fasteners.hole_spec('M5', head='counterbore',
                                    material_thickness=10)
    assert clearance['diameter_mm'] == 5.5
    assert clearance['counterbore'] == {'diameter_mm': 10.0, 'depth_mm': 5.0}
    # grip 10 + nut 4.0 + washer 1.0 + 2 protrusion
    assert clearance['suggested_bolt_length_mm'] == 17.0

    tapped = fasteners.hole_spec('M6', kind='tapped')
    assert tapped['diameter_mm'] == 5.0
    assert tapped['thread'] == 'M6x1.0'

    insert = fasteners.hole_spec('M3', kind='heat_set')
    assert insert['diameter_mm'] == 4.0
    assert insert['depth_mm'] == 6.7

    with pytest.raises(RuntimeError):
        fasteners.hole_spec('M12', kind='heat_set')


# --------------------------------------------------------------------------- #
# dfm
# --------------------------------------------------------------------------- #
@requires_re
def test_dfm_injection_flags_straight_walls(tmp_path):
    import trimesh
    trimesh.creation.box(extents=(30, 20, 15)).export(str(tmp_path / 'b.stl'))
    rep = dfm.check(str(tmp_path / 'b.stl'), process='injection', axis='z')
    # All four side walls of a box are exactly parallel to the pull.
    assert rep['zero_draft']['area_fraction'] > 0.4
    assert rep['undercuts']['area_fraction'] == 0.0
    assert any('draft' in r for r in rep['recommendations'])


@requires_re
def test_dfm_cnc_flags_underside_of_a_mushroom(tmp_path):
    import trimesh
    # A wide plate on a narrow column: the plate's underside faces down and
    # shadows the base around the column.
    plate = trimesh.creation.box(extents=(40, 40, 5))
    plate.apply_translation((0, 0, 17.5))
    column = trimesh.creation.box(extents=(10, 10, 15))
    column.apply_translation((0, 0, 7.5))
    mushroom = trimesh.util.concatenate([plate, column])
    mushroom.export(str(tmp_path / 'm.stl'))
    rep = dfm.check(str(tmp_path / 'm.stl'), process='cnc3axis', axis='z')
    assert rep['down_facing']['area_fraction'] > 0.1
    assert any('flip' in r.lower() for r in rep['recommendations'])


@requires_re
def test_dfm_fdm_delegates_to_print_check(tmp_path):
    import trimesh
    trimesh.creation.box(extents=(30, 20, 15)).export(str(tmp_path / 'b.stl'))
    rep = dfm.check(str(tmp_path / 'b.stl'), process='fdm')
    assert rep['process'] == 'fdm'
    assert rep['fits_bed'] is True


def test_dfm_rejects_unknown_process(tmp_path):
    with pytest.raises(RuntimeError):
        dfm.check(str(tmp_path / 'x.stl'), process='casting')


# --------------------------------------------------------------------------- #
# toolsets
# --------------------------------------------------------------------------- #
def test_toolset_classification_and_filtering(monkeypatch):
    import server
    assert server._toolset_of('scan_align') == 'scan'
    assert server._toolset_of('mesh_repair') == 'scan'
    assert server._toolset_of('photo_rectify') == 'photo'
    assert server._toolset_of('cam_post') == 'cam'
    assert server._toolset_of('print_estimate') == 'print'
    assert server._toolset_of('extrude') == 'core'
    assert server._toolset_of('sketch_doctor') == 'diag'

    fake_registry = {'extrude': 1, 'scan_align': 2, 'photo_rectify': 3,
                     'cam_post': 4}
    monkeypatch.setattr(server.mcp._tool_manager, '_tools', fake_registry)
    monkeypatch.setenv('FUSIONMCP_TOOLSETS', 'scan')
    result = server._apply_toolsets()
    assert result == {'enabled': ['core', 'scan'], 'dropped': 2}
    assert set(fake_registry) == {'extrude', 'scan_align'}


def test_toolsets_unset_keeps_everything(monkeypatch):
    import server
    monkeypatch.delenv('FUSIONMCP_TOOLSETS', raising=False)
    assert server._apply_toolsets() is None


# --------------------------------------------------------------------------- #
# add-in additions are probing-only; verify they at least import + register
# --------------------------------------------------------------------------- #
def test_new_ops_registered():
    import commands
    for op in ('loft_from_sections', 'silhouette', 'sketch_doctor',
               'drawing_table', 'fastener_update_size'):
        assert op in commands.DISPATCH


def test_versions_in_sync():
    import commands
    from _version import __version__
    assert commands.VERSION == __version__ == '1.12.0'
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'mcp_server', 'pyproject.toml')
    with open(pyproject, encoding='utf-8') as fh:
        assert 'version = "%s"' % __version__ in fh.read()
