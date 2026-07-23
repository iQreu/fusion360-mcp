"""Tests for the dispatch table wiring (no Fusion runtime needed)."""
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
