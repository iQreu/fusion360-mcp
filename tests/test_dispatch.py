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
    # v1.10.0: July 2026 GA wave + diagnostics
    'timeline_builder', 'corner_closure', 'cam_setup', 'cam_suppress',
    'design_diagnostics', 'sketch_status', 'create_appearance',
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
    def __init__(self, file_id, creation_id=None):
        self.dataFile = None if file_id is None else type('DF', (), {'id': file_id})()
        if creation_id is not None:
            self.creationId = creation_id


class _FakeApp:
    def __init__(self, file_id, creation_id=None):
        self.activeDocument = _FakeDoc(file_id, creation_id)


def _reset_doc_state():
    commands._active_doc_key = (None, None)
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


def test_saving_keeps_tokens_when_creation_id_present():
    # Same doc across its first save: creationId constant, cloud id None -> id.
    _reset_doc_state()
    commands._drop_tokens_on_doc_switch(_FakeApp(None, creation_id='cid-A'))
    tok = commands._registry.add('bdy', object())
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A', creation_id='cid-A'))
    assert commands._registry.get_opt(tok) is not None


def test_switching_to_a_different_saved_document_drops_tokens():
    _reset_doc_state()
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A'))
    tok = commands._registry.add('bdy', object())
    commands._drop_tokens_on_doc_switch(_FakeApp('file-B'))  # genuine switch
    assert commands._registry.get_opt(tok) is None
    _reset_doc_state()


def test_switching_saved_to_unsaved_drops_tokens():
    # Regression: saved doc A -> File > New (unsaved doc B, no dataFile). The
    # old id-only key early-returned and doc-A tokens kept resolving — a
    # delete('bdy1') in doc B destroyed a body in background doc A.
    _reset_doc_state()
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A', creation_id='cid-A'))
    tok = commands._registry.add('bdy', object())
    commands._drop_tokens_on_doc_switch(_FakeApp(None, creation_id='cid-B'))
    assert commands._registry.get_opt(tok) is None
    _reset_doc_state()


def test_doc_switch_clears_isolate_stash_and_code_store():
    _reset_doc_state()
    commands._drop_tokens_on_doc_switch(_FakeApp('file-A', creation_id='cid-A'))
    commands._isolate_stash = [(object(), True)]
    commands._code_store['jig'] = object()
    commands._drop_tokens_on_doc_switch(_FakeApp('file-B', creation_id='cid-B'))
    assert commands._isolate_stash is None      # no isolate deadlock in doc B
    assert commands._code_store == {}           # no live doc-A objects via fetch()
    _reset_doc_state()


def test_op_that_creates_a_document_keeps_its_fresh_tokens():
    # An op can itself create/activate a NEW document (documents.add inside
    # run_code, open_document, the headless-drawing fallback) and mint tokens
    # for it. The old-document state must die IMMEDIATELY (not on the next
    # dispatch, which would misattribute the fresh tokens to the old document
    # and wipe them).
    _reset_doc_state()
    app = _FakeApp('file-A', creation_id='cid-A')
    commands._drop_tokens_on_doc_switch(app)
    old_tok = commands._registry.add('bdy', object())
    commands._code_store['old'] = object()
    fresh = {}

    def fake_op(a, p):
        a.activeDocument = _FakeDoc(None, creation_id='cid-B')  # File > New
        fresh['tok'] = commands._registry.add('bdy', object())
        commands._code_store['jig'] = object()
        return {}

    commands.DISPATCH['__test_newdoc'] = fake_op
    try:
        commands.dispatch(app, '__test_newdoc', {})
    finally:
        del commands.DISPATCH['__test_newdoc']
    # Old-document state died immediately; state minted DURING the op survives.
    assert commands._registry.get_opt(old_tok) is None
    assert commands._registry.get_opt(fresh['tok']) is not None
    assert 'old' not in commands._code_store
    assert 'jig' in commands._code_store
    # The NEXT dispatch must not wipe the fresh token either.
    commands._drop_tokens_on_doc_switch(app)
    assert commands._registry.get_opt(fresh['tok']) is not None
    _reset_doc_state()
    commands._code_store.clear()


def test_failing_op_that_switched_documents_still_invalidates():
    # A sub-op inside batch can switch documents and THEN raise; op_batch
    # catches the exception, so the invalidation must run on the exception
    # path too (finally) — otherwise the outer dispatch advances the doc key
    # while tokens minted earlier in the batch stay resolvable against the
    # old document forever.
    _reset_doc_state()
    app = _FakeApp('file-A', creation_id='cid-A')
    commands._drop_tokens_on_doc_switch(app)
    minted = {}

    def mint_op(a, p):
        minted['tok'] = commands._registry.add('bdy', object())
        return {}

    def switch_and_raise(a, p):
        a.activeDocument = _FakeDoc(None, creation_id='cid-B')
        raise RuntimeError('boom after the switch')

    commands.DISPATCH['__test_mint'] = mint_op
    commands.DISPATCH['__test_boom'] = switch_and_raise
    try:
        commands.dispatch(app, '__test_mint', {})   # doc-A token
        with pytest.raises(RuntimeError):
            commands.dispatch(app, '__test_boom', {})
    finally:
        del commands.DISPATCH['__test_mint']
        del commands.DISPATCH['__test_boom']
    # The doc-A token must be dead immediately and stay dead after the next
    # dispatch's pre-op check (key is already doc B on both sides).
    assert commands._registry.get_opt(minted['tok']) is None
    commands._drop_tokens_on_doc_switch(app)
    assert commands._registry.get_opt(minted['tok']) is None
    _reset_doc_state()


def test_timeline_rollback_cache_bust_is_case_insensitive():
    # op_timeline lowercases its action, so dispatch's mutating check must too —
    # 'Rollback' used to roll back but leave the stale get_state cache alive.
    calls = []
    original = commands.DISPATCH['timeline']
    commands.DISPATCH['timeline'] = lambda app, p: calls.append(p) or {}
    try:
        commands._state_cache['probe'] = 'stale'
        gen = commands._mutation_gen
        commands.dispatch(None, 'timeline', {'action': 'Rollback', 'position': 0})
        assert commands._mutation_gen == gen + 1
        assert 'probe' not in commands._state_cache
    finally:
        commands.DISPATCH['timeline'] = original


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
