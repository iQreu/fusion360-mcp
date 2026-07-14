"""Tests for the electronics_* ops' pure logic (no live Fusion needed).

A fake ``adsk.electron`` module is installed per-test: cast() dispatches on a
``_kind`` attribute and Units.u2mm applies the documented 1/320000 mm scale.
"""
import sys
import types

import commands
import pytest


class Coll:
    """count/item(i) Electronics collection fake."""

    def __init__(self, items=()):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


def NS(**kw):
    return types.SimpleNamespace(**kw)


def _make_electron():
    electron = types.ModuleType('adsk.electron')

    def _caster(kind):
        class Caster:
            @staticmethod
            def cast(obj):
                return obj if getattr(obj, '_kind', None) == kind else None
        Caster.__name__ = kind.capitalize()
        return Caster

    electron.Schematic = _caster('schematic')
    electron.Board = _caster('board')
    electron.Library = _caster('library')
    electron.EcadDesign = _caster('design')

    class Units:
        @staticmethod
        def u2mm(v):
            return v / 320000.0

    electron.Units = Units
    return electron


@pytest.fixture
def electron(monkeypatch):
    mod = _make_electron()
    monkeypatch.setitem(sys.modules, 'adsk.electron', mod)
    monkeypatch.setattr(sys.modules['adsk'], 'electron', mod, raising=False)
    return mod


def _element(name='R1', value='10k'):
    return NS(name=name, value=value, x=320000, y=640000, angle=90.0,
              mirror=0, locked=False, populate=True,
              package=NS(name='R0603'), package3d=NS(name='R0603_3D'),
              attributes=Coll([NS(name='MPN', value='RC0603FR-0710KL')]))


def _board(**over):
    brd = NS(_kind='board', name='pcb', headline='demo board',
             elements=Coll([_element()]),
             signals=Coll([NS(name='GND', netClass=NS(name='default'),
                              wires=Coll([1, 2]), vias=Coll([1]),
                              polyPours=Coll([]),
                              contactRefs=Coll([NS(element=NS(name='R1'),
                                                   contact=NS(name='1'))]))]),
             layers=Coll([NS(number=1, name='Top', used=True, visible=True,
                             color='4'),
                          NS(number=2, name='Route2', used=False, visible=False,
                             color='1')]),
             errors=Coll([]), linkedSchematic=None, parentDesign=None)
    vars(brd).update(over)
    return brd


def _schematic(**over):
    sch = NS(_kind='schematic', name='sch', headline='demo schematic',
             sheets=Coll([NS(number=1, name='Sheet1', instances=Coll([1]),
                             wires=Coll([1, 2]), nets=Coll([1]),
                             busses=Coll([]), texts=Coll([]))]),
             parts=Coll([NS(name='R1', value='10k',
                            deviceset=NS(name='RESISTOR'),
                            device=NS(package=NS(name='R0603')),
                            package3d=NS(name='R0603_3D'),
                            instances=Coll([1]),
                            attributes=Coll([]))]),
             nets=Coll([NS(name='GND', netClass=NS(name='default'),
                           pinRefs=Coll([NS(part=NS(name='R1'),
                                            pin=NS(name='1'))]),
                           segments=Coll([]))]),
             errors=Coll([]), linkedBoard=None, parentDesign=None)
    vars(sch).update(over)
    return sch


def _app(product):
    return NS(activeProduct=product)


def test_electron_missing_raises_readable_error():
    with pytest.raises(RuntimeError, match='adsk.electron'):
        commands.op_electronics_info(_app(NS()), {})


def test_info_requires_an_electronics_product(electron):
    with pytest.raises(RuntimeError, match='not an Electronics document'):
        commands.op_electronics_info(_app(NS(_kind=None)), {})


def test_info_reports_board_and_linked_schematic(electron):
    brd = _board(linkedSchematic=_schematic())
    out = commands.op_electronics_info(_app(brd), {})
    assert out['active'] == 'board'
    assert out['board']['elements'] == 1
    assert out['board']['drc_errors'] == 0
    assert out['schematic']['per_sheet'][0]['wires'] == 2


def test_components_board_side_converts_units(electron):
    out = commands.op_electronics_components(_app(_board()), {})
    assert out['side'] == 'board'
    comp = out['components'][0]
    assert comp['x_mm'] == 1.0 and comp['y_mm'] == 2.0
    assert comp['angle_deg'] == 90.0
    assert comp['package'] == 'R0603'
    assert comp['attributes']['MPN'] == 'RC0603FR-0710KL'


def test_components_schematic_side_lists_parts(electron):
    out = commands.op_electronics_components(_app(_schematic()), {})
    assert out['side'] == 'schematic'
    part = out['components'][0]
    assert part['device_set'] == 'RESISTOR'
    assert part['package'] == 'R0603'
    assert part['gates_placed'] == 1


def test_components_filter_and_limit(electron):
    brd = _board(elements=Coll([_element('R1'), _element('R2'),
                                _element('C1')]))
    out = commands.op_electronics_components(_app(brd), {'filter': 'r'})
    assert [c['name'] for c in out['components']] == ['R1', 'R2']
    out = commands.op_electronics_components(_app(brd), {'limit': 1})
    assert out['count'] == 1


def test_nets_schematic_side_reports_pins(electron):
    out = commands.op_electronics_nets(_app(_schematic()), {})
    assert out['side'] == 'schematic'
    net = out['nets'][0]
    assert net['name'] == 'GND'
    assert net['pins'] == [{'part': 'R1', 'pin': '1'}]


def test_nets_board_side_reports_routing(electron):
    out = commands.op_electronics_nets(_app(_board()), {})
    assert out['side'] == 'board'
    sig = out['nets'][0]
    assert sig['traces'] == 2 and sig['vias'] == 1 and sig['pours'] == 0
    assert sig['contacts'] == [{'element': 'R1', 'pad': '1'}]


def test_layers_used_only_filters(electron):
    out = commands.op_electronics_layers(_app(_board()), {})
    assert out['count'] == 2
    out = commands.op_electronics_layers(_app(_board()), {'used_only': True})
    assert [ly['name'] for ly in out['layers']] == ['Top']


def test_export_dispatches_on_extension(electron):
    calls = {}

    class EM:
        def createEagleBrdExportOptions(self, path):
            calls['path'] = path
            return NS(outputPath=path)

        def execute(self, options):
            calls['executed'] = options.outputPath
            return True

    brd = _board(exportManager=EM())
    out = commands.op_electronics_export(_app(brd), {'path': 'C:/t/x.brd'})
    assert out == {'exported': 'C:/t/x.brd', 'kind': 'board'}
    assert calls['executed'] == 'C:/t/x.brd'


def test_export_rejects_unknown_extension_and_unreachable_product(electron):
    with pytest.raises(ValueError, match='.brd, .sch or .lbr'):
        commands.op_electronics_export(_app(_board()), {'path': 'out.step'})
    with pytest.raises(RuntimeError, match='No schematic'):
        commands.op_electronics_export(_app(_board()), {'path': 'out.sch'})


def test_export_failure_raises(electron):
    class EM:
        def createEagleBrdExportOptions(self, path):
            return NS(outputPath=path)

        def execute(self, options):
            return False

    with pytest.raises(RuntimeError, match='failed'):
        commands.op_electronics_export(_app(_board(exportManager=EM())),
                                       {'path': 'C:/t/x.brd'})
