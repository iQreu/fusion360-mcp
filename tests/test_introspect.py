"""api_introspect — target resolution and member listing (fake adsk)."""
import commands
import pytest


def test_dotted_path_lists_enum_values():
    out = commands.op_api_introspect(None, {'target': 'adsk.fusion.FeatureOperations'})
    names = {m['name'] for m in out['members']}
    assert 'CutFeatureOperation' in names
    assert out['type'] == 'FeatureOperations'
    cut = next(m for m in out['members'] if m['name'] == 'CutFeatureOperation')
    assert cut['kind'] == 'attribute'
    assert cut['value'] == 'cut'


def test_query_filters_members():
    out = commands.op_api_introspect(None, {'target': 'adsk.fusion.FeatureOperations',
                                            'query': 'cut'})
    assert out['members']
    assert all('cut' in m['name'].lower() for m in out['members'])


def test_limit_truncates():
    out = commands.op_api_introspect(None, {'target': 'adsk.fusion.FeatureOperations',
                                            'limit': 1})
    assert len(out['members']) == 1
    assert out['count'] >= 4
    assert out['truncated_to'] == 1


def test_stored_object_via_dollar():
    commands._code_store['probe'] = {'a': 1}
    out = commands.op_api_introspect(None, {'target': '$probe', 'query': 'item'})
    assert {'items'} <= {m['name'] for m in out['members']}


def test_missing_store_name_raises():
    with pytest.raises(KeyError):
        commands.op_api_introspect(None, {'target': '$nope'})


def test_non_adsk_path_rejected():
    with pytest.raises(ValueError):
        commands.op_api_introspect(None, {'target': 'os.path'})


def test_registry_token_uses_instance_class():
    class Widget:
        def spin(self):
            """Rotate the widget."""
    token = commands._registry.add('wgt', Widget())
    out = commands.op_api_introspect(None, {'target': token})
    spin = next(m for m in out['members'] if m['name'] == 'spin')
    assert spin['kind'] == 'method'
    assert spin['doc'] == 'Rotate the widget.'
