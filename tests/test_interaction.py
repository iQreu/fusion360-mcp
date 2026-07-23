"""Tests for the interaction ops' pure logic: visibility toggling, proxy-safe
entity identity, undo and highlight (no live Fusion needed — plain fakes)."""
import commands


class LightbulbObj:
    """Body/occurrence-like: class-level isLightBulbOn AND isVisible properties
    (mirrors real adsk proxy classes, which define these on the class)."""

    def __init__(self, on=True):
        self._on = on
        self._vis = on

    @property
    def isLightBulbOn(self):
        return self._on

    @isLightBulbOn.setter
    def isLightBulbOn(self, v):
        self._on = v

    @property
    def isVisible(self):
        return self._vis

    @isVisible.setter
    def isVisible(self, v):
        self._vis = v


class VisibleOnlyObj:
    """Sketch-like: only isVisible is defined on the class (no isLightBulbOn)."""

    def __init__(self, on=True):
        self._vis = on

    @property
    def isVisible(self):
        return self._vis

    @isVisible.setter
    def isVisible(self, v):
        self._vis = v


class NoVisibilityObj:
    """Face/edge-like: the class defines NO visibility toggle at all."""


def test_set_visible_prefers_lightbulb_then_isvisible():
    a = LightbulbObj(on=True)
    assert commands._set_visible(a, False) == 'isLightBulbOn'
    assert a.isLightBulbOn is False

    b = VisibleOnlyObj(on=True)
    assert commands._set_visible(b, False) == 'isVisible'
    assert b.isVisible is False


def test_set_visible_raises_on_objects_without_a_toggle():
    import pytest
    with pytest.raises(RuntimeError):
        commands._set_visible(NoVisibilityObj(), False)


def test_get_visible_reads_either_attribute():
    assert commands._get_visible(LightbulbObj(on=False)) is False
    assert commands._get_visible(VisibleOnlyObj(on=True)) is True
    assert commands._get_visible(object()) is True  # unknown -> assume visible


class Tokened:
    def __init__(self, token):
        self.entityToken = token


def test_same_entity_uses_entity_token_across_proxies():
    assert commands._same_entity(Tokened('e1'), Tokened('e1'))
    assert not commands._same_entity(Tokened('e1'), Tokened('e2'))


def test_same_entity_falls_back_to_equality():
    obj = object()
    assert commands._same_entity(obj, obj)
    assert not commands._same_entity(object(), object())


def test_op_set_visibility_reports_changed_and_failed():
    tok = commands._registry.add('bdy', LightbulbObj(on=True))
    res = commands.op_set_visibility(None, {'tokens': [tok, 'bdy999'],
                                            'visible': False})
    assert res['changed'] == [tok]
    assert res['failed'][0]['token'] == 'bdy999'
    assert res['visible'] is False


class FakeApp:
    def __init__(self, fail_from=None):
        self.calls = []
        self._fail_from = fail_from

    def executeTextCommand(self, cmd):
        if self._fail_from is not None and len(self.calls) >= self._fail_from:
            raise RuntimeError('undo stack empty')
        self.calls.append(cmd)


def test_op_undo_runs_requested_steps_and_flags_stale_tokens():
    app = FakeApp()
    res = commands.op_undo(app, {'steps': 3})
    assert res['undone'] == 3
    assert res['tokens_may_be_stale'] is True
    assert all('UndoCommand' in c for c in app.calls)


def test_op_undo_stops_when_the_stack_runs_out():
    res = commands.op_undo(FakeApp(fail_from=1), {'steps': 5})
    assert res['undone'] == 1


class FakeSelections:
    def __init__(self):
        self.items = []

    def clear(self):
        self.items = []

    def add(self, entity):
        self.items.append(entity)


class FakeUIApp:
    def __init__(self):
        class UI:
            activeSelections = FakeSelections()
        self.userInterface = UI()


def test_op_highlight_selects_known_tokens_and_skips_unknown():
    app = FakeUIApp()
    obj = LightbulbObj()
    tok = commands._registry.add('fac', obj)
    res = commands.op_highlight(app, {'tokens': [tok, 'fac404']})
    assert res['highlighted'] == [tok]
    assert app.userInterface.activeSelections.items == [obj]


def test_op_highlight_empty_list_clears_selection():
    app = FakeUIApp()
    app.userInterface.activeSelections.items = ['old']
    res = commands.op_highlight(app, {'tokens': []})
    assert res['count'] == 0
    assert app.userInterface.activeSelections.items == []
