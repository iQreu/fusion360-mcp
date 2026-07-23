"""Tests for the entity token registry."""
import pytest
from registry import Registry


class _Obj:
    """Stand-in for a Fusion API object with an optional persistent token."""
    def __init__(self, entity_token=None):
        if entity_token is not None:
            self.entityToken = entity_token


def test_tokens_are_kind_prefixed_and_incrementing():
    reg = Registry()
    a = reg.add('edg', _Obj())
    b = reg.add('edg', _Obj())
    c = reg.add('fac', _Obj())
    assert a == 'edg1'
    assert b == 'edg2'
    assert c == 'fac1'


def test_dedup_by_entity_token_returns_same_token():
    reg = Registry()
    first = reg.add('bdy', _Obj('ET-42'))
    second = reg.add('bdy', _Obj('ET-42'))  # same persistent entityToken
    assert first == second
    assert reg.get(first) is not None


def test_dedup_refreshes_stale_reference():
    reg = Registry()
    old = _Obj('ET-1')
    new = _Obj('ET-1')
    tok = reg.add('bdy', old)
    reg.add('bdy', new)
    assert reg.get(tok) is new  # latest live object wins


def test_get_unknown_token_raises():
    reg = Registry()
    with pytest.raises(KeyError):
        reg.get('nope9')


def test_get_none_raises():
    reg = Registry()
    with pytest.raises(KeyError):
        reg.get(None)


def test_get_opt_returns_none_for_unknown():
    reg = Registry()
    assert reg.get_opt('ghost1') is None


def test_reset_invalidates_old_tokens_without_reusing_strings():
    reg = Registry()
    old = reg.add('edg', _Obj('ET-9'))
    reg.reset()
    # Old token is dead after reset...
    with pytest.raises(KeyError):
        reg.get(old)
    # ...and its string is NOT reissued, so a stale client token can never
    # silently resolve to a different entity.
    fresh = reg.add('edg', _Obj())
    assert fresh != old
    assert fresh == 'edg2'


def test_remove_forgets_token():
    reg = Registry()
    tok = reg.add('bdy', _Obj('ET-7'))
    reg.remove(tok)
    with pytest.raises(KeyError):
        reg.get(tok)
    # entityToken mapping is gone too: re-adding the same entity mints a new
    # token rather than resurrecting the removed one.
    again = reg.add('bdy', _Obj('ET-7'))
    assert again != tok


def test_remove_unknown_token_is_noop():
    reg = Registry()
    reg.remove('ghost1')  # must not raise
