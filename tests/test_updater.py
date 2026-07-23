"""Tests for the version comparison, staging and startup-notice logic in the
GitHub updater."""
import io
import os
import zipfile

import updater


def _make_zip(entries):
    """A real in-memory zip (download_to_staging/_apply_zip validate archives)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_parse_strips_v_prefix_and_suffix():
    assert updater.parse_version('v1.2.3') == (1, 2, 3)
    assert updater.parse_version('1.4.0-beta.1') == (1, 4, 0)
    assert updater.parse_version('release-2.0') == (2, 0)
    assert updater.parse_version('') == ()
    assert updater.parse_version('none') == ()


def test_is_newer_basic():
    assert updater.is_newer('1.2.0', '1.1.0')
    assert updater.is_newer('2.0.0', '1.9.9')
    assert not updater.is_newer('1.1.0', '1.1.0')
    assert not updater.is_newer('1.0.0', '1.1.0')


def test_is_newer_pads_unequal_lengths():
    assert not updater.is_newer('1.2', '1.2.0')   # equal
    assert updater.is_newer('1.2.1', '1.2')       # 1.2.1 > 1.2.0


def test_is_newer_ignores_unparseable_candidate():
    assert not updater.is_newer('', '1.0.0')
    assert not updater.is_newer('garbage', '1.0.0')


def test_apply_requires_confirmation():
    result = updater.apply(confirm=False)
    assert result['applied'] is False
    assert 'onfirm' in result['reason'] or 'consent' in result['reason'].lower()


def test_staged_zip_path_sanitises_version():
    path = updater.staged_zip_path('v1.2.3/../evil')
    assert os.path.basename(path) == 'fusionmcp-v1.2.3_.._evil.zip'
    assert os.path.dirname(path) == updater.staging_dir()


def test_download_to_staging_writes_and_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, 'gettempdir', lambda: str(tmp_path))
    blob = _make_zip({'repo-top/mcp_server/_version.py': "__version__ = '9.9.9'"})
    calls = []

    def fake_get(url, accept=''):
        calls.append(url)
        return blob

    monkeypatch.setattr(updater, '_http_get', fake_get)
    info = {'version': 'v9.9.9', 'zip_url': 'https://example.test/z.zip'}
    path = updater.download_to_staging(info)
    assert path and os.path.isfile(path)
    with open(path, 'rb') as fh:
        assert fh.read() == blob
    # A digest sidecar is written so tampering/corruption can be detected.
    assert os.path.isfile(path + '.sha256')
    # Second call reuses the staged file (digest matches) without re-downloading.
    assert updater.download_to_staging(info) == path
    assert len(calls) == 1


def test_download_to_staging_rejects_non_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(updater, '_http_get', lambda url, accept='': b'not a zip')
    assert updater.download_to_staging(
        {'version': '3.0', 'zip_url': 'https://example.test/z.zip'}) is None


def test_extract_over_blocks_zip_slip(tmp_path):
    root = tmp_path / 'install'
    root.mkdir()
    (root / 'mcp_server').mkdir()
    evil = _make_zip({'top/mcp_server/ok.py': 'x = 1',
                      'top/../../escape.py': 'pwned = 1'})
    import pytest
    with pytest.raises(RuntimeError, match='zip-slip'):
        updater._extract_over(evil, str(root))
    # The escaping payload must NOT have been written outside the root.
    assert not (tmp_path.parent / 'escape.py').exists()


def test_download_to_staging_without_url_or_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, 'gettempdir', lambda: str(tmp_path))
    assert updater.download_to_staging({'version': '2.0'}) is None

    def boom(url, accept=''):
        raise OSError('offline')

    monkeypatch.setattr(updater, '_http_get', boom)
    assert updater.download_to_staging(
        {'version': '2.0', 'zip_url': 'https://example.test/z.zip'}) is None


def _set_pending(monkeypatch, pending, announced=False):
    monkeypatch.setattr(updater, '_pending', pending)
    monkeypatch.setattr(updater, '_announced', announced)


def test_consume_notice_is_one_shot(monkeypatch):
    _set_pending(monkeypatch, {
        'update_available': True, 'latest_version': '9.0.0',
        'release_notes': 'notes', 'downloaded': 'C:/staged.zip',
    })
    notice = updater.consume_notice()
    assert notice['latest_version'] == '9.0.0'
    assert notice['release_notes'] == 'notes'
    assert 'already downloaded' in notice['message']
    assert 'apply_update(confirm=True)' in notice['message']
    assert updater.consume_notice() is None  # delivered once per session


def test_consume_notice_none_without_update(monkeypatch):
    _set_pending(monkeypatch, None)
    assert updater.consume_notice() is None
    _set_pending(monkeypatch, {'update_available': False})
    assert updater.consume_notice() is None
    _set_pending(monkeypatch, {'update_available': True, 'latest_version': '9.0'},
                 announced=True)
    assert updater.consume_notice() is None


def test_start_background_check_respects_off(monkeypatch):
    monkeypatch.setattr(updater, 'AUTO_MODE', 'off')
    assert updater.start_background_check() is None


def test_background_check_notify_mode_skips_download(monkeypatch):
    monkeypatch.setattr(updater, 'AUTO_MODE', 'notify')
    monkeypatch.setattr(updater, 'latest', lambda: {
        'version': 'v99.0.0', 'source': 'release', 'url': 'u',
        'notes': 'big release', 'zip_url': 'z'})
    monkeypatch.setattr(updater, 'download_to_staging',
                        lambda info: (_ for _ in ()).throw(AssertionError('no download')))
    _set_pending(monkeypatch, None)
    updater._background_check()
    notice = updater.consume_notice()
    assert notice['update_available'] is True
    assert notice['latest_version'] == 'v99.0.0'
    assert notice.get('downloaded') is None


def test_background_check_reports_errors_quietly(monkeypatch):
    monkeypatch.setattr(updater, 'latest', lambda: {'error': 'rate limited'})
    _set_pending(monkeypatch, None)
    updater._background_check()
    assert updater._pending['update_available'] is False
    assert 'rate limited' in updater._pending['error']
    assert updater.consume_notice() is None
