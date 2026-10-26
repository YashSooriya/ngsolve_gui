from types import SimpleNamespace

from ngapp.components import Component

from ngsolve_gui.three_d_workspace import ThreeDProjectDialog


def test_three_d_workspace_opens_and_keeps_unsaved_imports(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    dialog = ThreeDProjectDialog(SimpleNamespace())

    assert dialog.project["geometries"] == []
    assert dialog.project["wire_trace_sets"] == []
    assert dialog._read_units(dialog._geometry_units) == "auto"
    dialog._geometry_unit = "mm"
    assert dialog._read_units(dialog._geometry_units) == "mm"
    assert dialog.show_new() is True
    dialog.project["name"] = "Unsaved shield"
    dialog._mark_changed()
    dialog.ui_model_value = False

    assert dialog.show_new() is False
    assert dialog.ui_model_value is True
    assert dialog.project["name"] == "Unsaved shield"
    assert dialog.dirty is True
