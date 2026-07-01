"""Tests for unit conversion and operation resolution helpers."""
import commands
import pytest


def test_mm_to_internal_point():
    p = commands._pt(40, 20, 10)  # mm
    assert p.x == pytest.approx(4.0)   # cm
    assert p.y == pytest.approx(2.0)
    assert p.z == pytest.approx(1.0)


def test_xyz_round_trips_back_to_mm():
    p = commands._pt(12.3456, 0, -5)
    assert commands._xyz_mm(p) == [12.3456, 0.0, -5.0]


def test_operation_lookup_is_case_insensitive():
    assert commands._operation('CUT') == commands._OPS['cut']
    assert commands._operation(None) == commands._OPS['new']


def test_operation_invalid_raises():
    with pytest.raises(ValueError):
        commands._operation('weld')
