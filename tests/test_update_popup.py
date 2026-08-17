"""The in-Fusion update popup flow (v1.11.1): server-side worker consent
logic, release-note flattening, and the untracked-files dirty-check fix."""
import types

import pytest
import updater


# --------------------------------------------------------------------------- #
# updater.plain_notes
# --------------------------------------------------------------------------- #
def test_plain_notes_strips_markdown_and_truncates():
    notes = ('## v9.9.9 — 2026-01-01\n\n### Added\n'
             '* `scan_align` tool with **ICP**\n\n\n'
             '+ see [docs](https://example.com/docs)\n')
    text = updater.plain_notes(notes)
    assert 'v9.9.9' in text and '##' not in text
    assert '- scan_align tool with ICP' in text
    assert '- see docs' in text
    assert 'https://' not in text
    assert '\n\n\n' not in text

    long = '\n'.join('line %d' % i for i in range(400))
    short = updater.plain_notes(long, limit=200)
    assert len(short) <= 202
    assert short.endswith('…')


def test_plain_notes_handles_none():
    assert updater.plain_notes(None) == ''


# --------------------------------------------------------------------------- #
# updater._apply_git ignores untracked files
# --------------------------------------------------------------------------- #
def test_apply_git_dirty_check_ignores_untracked(monkeypatch, tmp_path):
    calls = []

    def fake_git(root, *args):
        calls.append(args)
        return types.SimpleNamespace(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(updater, '_git', fake_git)
    monkeypatch.setattr(updater, '_sync_addin', lambda root: True)
    result = updater._apply_git(str(tmp_path))
    assert result['applied'] is True
    assert calls[0] == ('status', '--porcelain', '--untracked-files=no')


# --------------------------------------------------------------------------- #
# server._update_popup_worker
# --------------------------------------------------------------------------- #
@pytest.fixture
def popup_env(monkeypatch):
    """Import server lazily and stub out everything the worker touches."""
    import server

    state = {'notify_calls': [], 'apply_calls': [], 'messages': []}

    def fake_call(op, params=None):
        if op == 'notify_update':
            state['notify_calls'].append(params)
            return state['notify_answer']
        if op == 'show_message':
            state['messages'].append(params)
            return {'shown': True}
        raise AssertionError('unexpected op %r' % op)

    def fake_apply(confirm=False, method='auto'):
        state['apply_calls'].append({'confirm': confirm})
        return state.get('apply_result',
                         {'applied': True, 'new_version': '9.9.9'})

    monkeypatch.setattr(server.fusion, 'call', fake_call)
    monkeypatch.setattr(server.updater, 'apply', fake_apply)
    monkeypatch.setattr(server.updater, 'pending_info', lambda: state['info'])
    monkeypatch.setattr(server.time, 'sleep', lambda s: None)
    monkeypatch.delenv('FUSION_MCP_UPDATE_POPUP', raising=False)
    state['server'] = server
    return state


def test_popup_consent_applies_and_confirms(popup_env):
    popup_env['info'] = {'update_available': True, 'latest_version': '9.9.9',
                         'release_notes': '* stuff'}
    popup_env['notify_answer'] = {'install': True, 'version': '9.9.9'}
    popup_env['server']._update_popup_worker(None)
    assert popup_env['apply_calls'] == [{'confirm': True}]
    assert popup_env['notify_calls'][0]['version'] == '9.9.9'
    assert 'restart' in popup_env['messages'][0]['text'].lower()


def test_popup_decline_never_applies(popup_env):
    popup_env['info'] = {'update_available': True, 'latest_version': '9.9.9'}
    popup_env['notify_answer'] = {'install': False}
    popup_env['server']._update_popup_worker(None)
    assert popup_env['apply_calls'] == []
    assert popup_env['messages'] == []


def test_popup_failure_reports_reason(popup_env):
    popup_env['info'] = {'update_available': True, 'latest_version': '9.9.9'}
    popup_env['notify_answer'] = {'install': True}
    popup_env['apply_result'] = {'applied': False, 'reason': 'disk full'}
    popup_env['server']._update_popup_worker(None)
    assert 'disk full' in popup_env['messages'][0]['text']


def test_popup_no_update_is_silent(popup_env):
    popup_env['info'] = {'update_available': False}
    popup_env['notify_answer'] = {'install': True}
    popup_env['server']._update_popup_worker(None)
    assert popup_env['notify_calls'] == []
    assert popup_env['apply_calls'] == []


def test_popup_gives_up_when_fusion_never_connects(popup_env, monkeypatch):
    from fusion_client import FusionNotConnected

    def never(op, params=None):
        raise FusionNotConnected('down')

    popup_env['info'] = {'update_available': True, 'latest_version': '9.9.9'}
    monkeypatch.setattr(popup_env['server'].fusion, 'call', never)
    popup_env['server']._update_popup_worker(None, attempts=3, retry_delay=0)
    assert popup_env['apply_calls'] == []


def test_popup_old_addin_without_op_is_silent(popup_env, monkeypatch):
    from fusion_client import FusionError

    def unknown_op(op, params=None):
        raise FusionError('Unknown op: notify_update')

    popup_env['info'] = {'update_available': True, 'latest_version': '9.9.9'}
    monkeypatch.setattr(popup_env['server'].fusion, 'call', unknown_op)
    popup_env['server']._update_popup_worker(None)
    assert popup_env['apply_calls'] == []


def test_popup_env_optout(popup_env, monkeypatch):
    monkeypatch.setenv('FUSION_MCP_UPDATE_POPUP', 'off')
    popup_env['info'] = {'update_available': True, 'latest_version': '9.9.9'}
    popup_env['notify_answer'] = {'install': True}
    popup_env['server']._update_popup_worker(None)
    assert popup_env['notify_calls'] == []
