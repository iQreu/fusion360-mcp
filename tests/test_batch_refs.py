"""Tests for batch $alias.path reference resolution in commands.py."""
import commands
import pytest


def test_resolve_simple_key():
    results = {'s': {'sketch': 'skt1'}}
    assert commands._resolve_ref('$s.sketch', results) == 'skt1'


def test_resolve_nested_index_and_key():
    results = {'r': {'profiles': [{'token': 'prf1'}, {'token': 'prf2'}]}}
    assert commands._resolve_ref('$r.profiles[1].token', results) == 'prf2'


def test_resolve_unknown_alias_raises():
    with pytest.raises(KeyError):
        commands._resolve_ref('$missing.x', {})


def test_resolve_params_recurses_into_lists_and_dicts():
    results = {'b': {'bodies': [{'token': 'bdy7'}]}}
    params = {'target': '$b.bodies[0].token', 'tools': ['$b.bodies[0].token'],
              'plain': 5, 'nested': {'k': '$b.bodies[0].token'}}
    out = commands._resolve_params(params, results)
    assert out['target'] == 'bdy7'
    assert out['tools'] == ['bdy7']
    assert out['plain'] == 5
    assert out['nested']['k'] == 'bdy7'


def test_double_dollar_is_escaped_literal():
    assert commands._resolve_params('$$literal', {}) == '$literal'


def test_non_reference_string_passes_through():
    assert commands._resolve_params('XY', {}) == 'XY'
