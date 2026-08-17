"""Tests for v1.13.0 part 1: code-CAD (build123d) and photogrammetry CLI
integration. build123d is exercised only when importable (Smart App Control
blocks its OCP kernel DLL on some machines — including the reference dev
box, so the hint path is the well-tested one)."""
import os

import codecad
import photogrammetry
import pytest


# --------------------------------------------------------------------------- #
# codecad
# --------------------------------------------------------------------------- #
def test_codecad_missing_dep_hint_mentions_sac(monkeypatch):
    monkeypatch.setattr(codecad, 'build123d', None)
    monkeypatch.setattr(codecad, '_IMPORT_ERROR', 'DLL load failed')
    with pytest.raises(RuntimeError) as err:
        codecad.run('result = 1', 'x.step')
    assert 'mcp_server[codecad]' in str(err.value)
    assert 'Smart App Control' in str(err.value)


@pytest.mark.skipif(codecad.build123d is None,
                    reason="optional 'codecad' extras not installed")
def test_codecad_box_roundtrip(tmp_path):
    out = tmp_path / 'box.step'
    rep = codecad.run('result = Box(20, 10, 5)', str(out))
    assert out.exists()
    assert abs(rep['volume_mm3'] - 1000.0) < 1.0


def test_codecad_requires_result_variable(monkeypatch):
    if codecad.build123d is None:
        pytest.skip("codecad extras not installed")
    with pytest.raises(RuntimeError) as err:
        codecad.run('x = 5', 'out.step')
    assert 'result' in str(err.value)


def test_codecad_rejects_unknown_format(monkeypatch):
    monkeypatch.setattr(codecad, 'build123d', object())  # bypass _require
    with pytest.raises(RuntimeError) as err:
        codecad.run('result = 1', 'out.iges')
    assert '.step or .stl' in str(err.value)


# --------------------------------------------------------------------------- #
# photogrammetry
# --------------------------------------------------------------------------- #
def test_build_command_shapes():
    rs = photogrammetry.build_command('realityscan', 'RS.exe', 'imgs',
                                      'out/model.obj', 500000)
    assert rs[0] == 'RS.exe' and '-headless' in rs and '-align' in rs
    assert rs[rs.index('-simplify') + 1] == '500000'
    ms = photogrammetry.build_command('meshroom', 'mb.exe', 'imgs',
                                      'out/model.obj', 500000)
    assert ms[:3] == ['mb.exe', '--input', 'imgs']
    with pytest.raises(RuntimeError):
        photogrammetry.build_command('colmap', 'x', 'imgs', 'o.obj', 1)


def test_run_validates_inputs(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError) as err:
        photogrammetry.run(str(tmp_path / 'missing'), str(tmp_path / 'o.obj'))
    assert 'not found' in str(err.value)

    imgdir = tmp_path / 'imgs'
    imgdir.mkdir()
    for i in range(3):
        (imgdir / ('p%d.jpg' % i)).write_bytes(b'x')
    with pytest.raises(RuntimeError) as err:
        photogrammetry.run(str(imgdir), str(tmp_path / 'o.obj'))
    assert '20+' in str(err.value)

    for i in range(3, 25):
        (imgdir / ('p%d.jpg' % i)).write_bytes(b'x')
    monkeypatch.setattr(photogrammetry, 'detect', lambda: [])
    with pytest.raises(RuntimeError) as err:
        photogrammetry.run(str(imgdir), str(tmp_path / 'o.obj'))
    assert 'FUSION_MCP_PHOTOGRAMMETRY' in str(err.value)


def test_run_finds_meshroom_textured_mesh(tmp_path, monkeypatch):
    imgdir = tmp_path / 'imgs'
    imgdir.mkdir()
    for i in range(25):
        (imgdir / ('p%d.jpg' % i)).write_bytes(b'x')
    out = tmp_path / 'recon' / 'model.obj'

    def fake_run(cmd, **kw):
        target = tmp_path / 'recon' / 'texturing' / 'texturedMesh.obj'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'o mesh\n')

        class R:
            returncode = 0
            stdout = stderr = ''
        return R()

    monkeypatch.setattr(photogrammetry, 'detect',
                        lambda: [{'backend': 'meshroom', 'path': 'mb.exe'}])
    monkeypatch.setattr(photogrammetry.subprocess, 'run', fake_run)
    rep = photogrammetry.run(str(imgdir), str(out))
    assert rep['backend'] == 'meshroom'
    assert rep['photos'] == 25
    assert rep['output'].endswith('texturedMesh.obj')
    assert 'ARBITRARY' in rep['note']


def test_detect_never_raises():
    assert isinstance(photogrammetry.detect(), list)


def test_versions_in_sync_v113():
    import commands
    from _version import __version__
    assert commands.VERSION == __version__ == '1.13.0'
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'mcp_server', 'pyproject.toml')
    with open(pyproject, encoding='utf-8') as fh:
        assert 'version = "%s"' % __version__ in fh.read()
