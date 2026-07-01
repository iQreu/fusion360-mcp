"""Tests for the parameter CSV export/import round-trip (fake design)."""
import commands


class Prm:
    def __init__(self, name, expression, unit='mm', comment=''):
        self.name = name
        self.expression = expression
        self.unit = unit
        self.comment = comment


class Params(list):
    def itemByName(self, name):
        for prm in self:
            if prm.name == name:
                return prm
        return None


class FakeDesign:
    def __init__(self, user, model=()):
        self.userParameters = Params(user)
        self.allParameters = Params(list(user) + list(model))


def _with_design(monkeypatch, design):
    monkeypatch.setattr(commands, '_design', lambda app: design)


def test_export_parameters_writes_all_with_kind(tmp_path, monkeypatch):
    design = FakeDesign(user=[Prm('width', '40 mm', comment='plate width')],
                        model=[Prm('d1', 'width / 2')])
    _with_design(monkeypatch, design)
    path = str(tmp_path / 'params.csv')
    res = commands.op_export_parameters(None, {'csv_path': path})
    assert res == {'csv': path, 'parameters': 2}
    text = open(path, encoding='utf-8').read()
    assert 'width,user,40 mm,mm,plate width' in text
    assert 'd1,model,width / 2' in text


def test_import_parameters_updates_existing_expressions(tmp_path, monkeypatch):
    prm = Prm('width', '40 mm')
    design = FakeDesign(user=[prm])
    _with_design(monkeypatch, design)
    path = tmp_path / 'params.csv'
    path.write_text('name,expression\nwidth,55 mm\n\n,\n', encoding='utf-8')
    res = commands.op_import_parameters(None, {'csv_path': str(path)})
    assert prm.expression == '55 mm'
    assert res['results'] == [{'name': 'width', 'action': 'updated'}]
    assert res['count'] == 1  # blank/incomplete rows are skipped
