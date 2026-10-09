"""Tests for direct NGSolve pickle loading."""

import ngsolve as ngs
from netgen.csg import unit_cube

import ngsolve_gui.file_loader as file_loader


def test_gridfunction_pickle_draw_uses_result_view_defaults(monkeypatch):
    mesh = ngs.Mesh(unit_cube.GenerateMesh(maxh=0.8))
    mesh.Curve(6)
    space = ngs.VectorH1(mesh, order=1)
    grid_function = ngs.GridFunction(space)
    captured = {}
    result = object()

    def fake_draw(obj, **options):
        captured["object"] = obj
        captured.update(options)
        return result

    monkeypatch.setattr(file_loader, "DrawImpl", fake_draw)
    assert file_loader._draw_pickle_object(grid_function, "magnetic_field") is result

    assert mesh.GetCurveOrder() == 4
    assert captured["object"] is grid_function
    assert captured["wireframe"] is False
    assert captured["subdivision"] == -1
    assert captured["fieldlines_num_lines"] == 20
    assert captured["fieldlines_thickness"] == 0.0001
    assert captured["_ngsolve_gui_fast_fieldlines"] is True
    assert captured["_ngsolve_gui_fieldline_seed_material"] is None


def test_pickle_loader_dispatches_through_result_view_defaults(tmp_path):
    source, compile_name = file_loader._build_loader_snippet(
        str(tmp_path / "result.pkl"), "result"
    )

    assert compile_name == "<ngsolve_gui:pickle>"
    assert "_draw_pickle_object(obj, 'result')" in source


def test_draw_preserves_gridfunction_for_function_component(monkeypatch):
    mesh = ngs.Mesh(unit_cube.GenerateMesh(maxh=0.8))
    grid_function = ngs.GridFunction(ngs.HDiv(mesh, order=1))
    captured = {}
    result = object()

    class FakeAppData:
        def add_tab(self, name, component, data, app_data):
            captured.update(
                name=name,
                component=component,
                data=data,
                app_data=app_data,
            )
            return result

    fake_app_data = FakeAppData()
    monkeypatch.setattr(file_loader, "_appdata", fake_app_data, raising=False)

    assert file_loader.DrawImpl(grid_function, name="BDC") is result
    assert captured["name"] == "BDC"
    assert captured["component"] is file_loader.FunctionComponent
    assert captured["data"]["obj"] is grid_function
    assert captured["data"]["mesh"] is mesh


def test_pickle_loader_escapes_windows_paths():
    filename = (
        r"C:\Users\yashw\AppData\Local\Temp\ngsolve_gui_test\field.pkl"
    )

    source, compile_name = file_loader._build_loader_snippet(filename, "field")

    compile(source, compile_name, "exec")
    assert repr(filename) in source
