"""Tests for the dispatch table wiring (no Fusion runtime needed)."""
import os
import re

import commands
import pytest

# Ops that must be registered — a typo in DISPATCH would drop a whole tool.
EXPECTED = {
    'ping', 'server_info', 'get_state', 'query_entities',
    'create_sketch', 'sketch_rectangle', 'sketch_circle', 'sketch_line',
    'sketch_arc', 'sketch_polygon', 'sketch_points', 'sketch_polyline',
    'sketch_spline', 'extrude', 'revolve', 'fillet', 'chamfer',
    'shell', 'combine', 'rectangular_pattern', 'circular_pattern', 'mirror',
    'move_body', 'delete', 'hole', 'construction_plane', 'construction_axis',
    'construction_point', 'sketch_constraint', 'sketch_dimension',
    'project_to_sketch', 'sketch_offset', 'sketch_fillet',
    'loft', 'sweep', 'rib', 'draft', 'thread', 'split_body',
    'create_component', 'rename', 'copy_body', 'joint',
    'set_material', 'set_appearance', 'measure', 'bounding_box',
    'center_of_mass', 'interference', 'import_file', 'timeline',
    'suppress_feature', 'list_parameters',
    'set_parameter', 'add_parameter', 'export', 'screenshot', 'fit_view',
    'save', 'set_design_mode', 'batch', 'run_code', 'reset_registry',
    'electronics_info', 'electronics_components', 'electronics_nets',
    'electronics_layers', 'electronics_library', 'electronics_export',
    # v1.8.0: July 2026 API wave
    'mesh_compare', 'fold', 'join_by_bend', 'sketch_blend_curve',
    'auto_constrain', 'thread_types', 'selection_filter', 'configurations',
    'api_introspect',
    # v1.9.0: new capabilities
    'as_built_joint', 'joint_origin', 'list_materials', 'list_appearances',
    'insert_fastener', 'data_folders', 'version_history', 'share_link',
    'annotate', 'annotations_clear', 'contact_set',
}


def test_all_expected_ops_registered():
    missing = EXPECTED - set(commands.DISPATCH)
    assert not missing, 'missing handlers: %s' % missing


def test_every_handler_is_callable():
    for name, fn in commands.DISPATCH.items():
        assert callable(fn), name


def test_dispatch_unknown_op_raises():
    with pytest.raises(RuntimeError):
        commands.dispatch(None, 'no_such_op', {})


_SERVER_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'mcp_server', 'server.py')


class _FakeDoc:
    def __init__(self, file_id):
        self.dataFile = None if file_id is None else type('DF', (), {'id': file_id})()


class _FakeApp:
    def __init__(self, file_id):
        self.activeDocument = _FakeDoc(file_id)


def _reset_doc_state():
    commands._active_doc_id = None
    commands._registry.reset()


def test_saving_a_new_document_does_not_drop_tokens():
    # Regression: keying doc identity on the name/dataFile tuple reset the
    # registry on first save (Untitled -> saved). None -> id is the SAME doc.
    _reset_doc_state()
    commands._drop_tokens_on_doc_switch(_FakeApp(None))     # unsaved
    tok = commands._registry.add('bdy', object())
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A'))  # first save
    assert commands._registry.get_opt(tok) is not None       # token survives
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A'))  # same doc again
    assert commands._registry.get_opt(tok) is not None


def test_switching_to_a_different_saved_document_drops_tokens():
    _reset_doc_state()
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A'))
    tok = commands._registry.add('bdy', object())
    commands._drop_tokens_on_doc_switch(_FakeApp('file-B'))  # genuine switch
    assert commands._registry.get_opt(tok) is None
    _reset_doc_state()


def test_every_server_op_string_exists_in_dispatch():
    """Guard against a typo'd op on the server side (e.g. _call('sketch_blendcurve'))
    or a renamed DISPATCH key: every op the server forwards must be dispatchable.
    Plain-text scan, so it needs neither the mcp SDK nor adsk."""
    with open(_SERVER_PY, encoding='utf-8') as fh:
        src = fh.read()
    ops = set(re.findall(r"""_call\(\s*['"]([a-z_]+)['"]""", src))
    ops |= set(re.findall(r"""fusion\.call\(\s*['"]([a-z_]+)['"]""", src))
    unknown = ops - set(commands.DISPATCH)
    assert not unknown, 'server.py sends ops missing from DISPATCH: %s' % unknown
