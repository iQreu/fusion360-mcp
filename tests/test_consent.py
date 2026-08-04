"""The elicitation consent gate for apply_update must FAIL CLOSED: only an
explicit, recognised acceptance counts — any unknown/drifted result shape is a
decline, because the gate protects a destructive install."""
import asyncio
import types

import pytest

pytest.importorskip('mcp', reason='MCP SDK not installed')


def _consent(result_shape, monkeypatch):
    import server
    monkeypatch.setattr(server.updater, 'check', lambda: {
        'update_available': True, 'current_version': '1.0.0',
        'latest_version': '2.0.0'})

    async def elicit(message=None, schema=None):
        return result_shape

    ctx = types.SimpleNamespace(elicit=elicit)
    return asyncio.run(server._elicit_update_consent(ctx))


def test_unknown_shapes_fail_closed(monkeypatch):
    # A mapping's keys are invisible to getattr — a dict-shaped DECLINE used to
    # be truthy and count as consent. So did any bare truthy object/string.
    assert _consent({'action': 'decline'}, monkeypatch) is False
    assert _consent({'action': 'accept'}, monkeypatch) is False  # unknown shape
    assert _consent(object(), monkeypatch) is False
    assert _consent('decline', monkeypatch) is False


def test_accept_action_with_install_is_consent(monkeypatch):
    res = types.SimpleNamespace(action='accept',
                                data=types.SimpleNamespace(install=True))
    assert _consent(res, monkeypatch) is True


def test_decline_action_is_not_consent(monkeypatch):
    res = types.SimpleNamespace(action='decline', data=None)
    assert _consent(res, monkeypatch) is False


def test_explicit_install_attribute_is_respected(monkeypatch):
    assert _consent(types.SimpleNamespace(install=True), monkeypatch) is True
    assert _consent(types.SimpleNamespace(install=False), monkeypatch) is False
