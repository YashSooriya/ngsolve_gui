"""Tests for direct NGSolve pickle loading."""

import ngsolve as ngs
from netgen.csg import unit_cube

import ngsolve_gui.file_loader as file_loader
from ngsolve_gui.prop_widgets import ColorbarLegend, _component_display_names


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
    assert "_ngsolve_gui_axisymmetric" not in captured


def test_axisymmetric_pickle_draw_enables_revolution_from_metadata(monkeypatch):
    from netgen.occ import Rectangle
    from netgen.occ import OCCGeometry

    mesh = ngs.Mesh(OCCGeometry(Rectangle(1, 1).Face(), dim=2).GenerateMesh(maxh=0.8))
    grid_function = ngs.GridFunction(ngs.H1(mesh, order=1))
    captured = {}

    monkeypatch.setattr(file_loader, "_is_axisymmetric_pickle", lambda _path: True)
    monkeypatch.setattr(
        file_loader,
        "DrawImpl",
        lambda obj, **options: captured.update(object=obj, **options),
    )

    file_loader._draw_pickle_object(
        grid_function, "axisymmetric field", field_path="/results/fields/field.pkl"
    )

    assert captured["object"] is grid_function
    assert captured["_ngsolve_gui_axisymmetric"] is True


def test_axisymmetric_vector_pickle_uses_radial_and_axial_component_names(monkeypatch):
    from netgen.occ import OCCGeometry, Rectangle

    mesh = ngs.Mesh(OCCGeometry(Rectangle(1, 1).Face(), dim=2).GenerateMesh(maxh=0.8))
    grid_function = ngs.GridFunction(ngs.VectorH1(mesh, order=1))
    captured = {}
    monkeypatch.setattr(file_loader, "_is_axisymmetric_pickle", lambda _path: True)
    monkeypatch.setattr(
        file_loader,
        "DrawImpl",
        lambda obj, **options: captured.update(object=obj, **options),
    )

    file_loader._draw_pickle_object(
        grid_function, "B_DC", field_path="/results/ngsolve_gui/fields/B_DC.pkl"
    )

    assert captured["object"] is grid_function
    assert captured["_ngsolve_gui_axisymmetric"] is True
    assert captured["_ngsolve_gui_component_names"] == ("r", "z")


def test_vector_component_selector_uses_axisymmetric_names():
    class Vector:
        dim = 2

    class Component:
        cf = Vector()
        component_names = ("r", "z")

    assert _component_display_names(Component()) == ("r", "z")

    class SelectorBuilder:
        def _set_component(self, value):
            self.selected_component = value

    builder = SelectorBuilder()
    selector = ColorbarLegend._build_component_selector(builder, Component())
    buttons = selector.ui_children[0]._btns
    assert [buttons[key].ui_children[0] for key in ("norm", "0", "1")] == [
        "|u|",
        "r",
        "z",
    ]
    selector.ui_children[0]._on_change("1")
    assert builder.selected_component == "1"


def test_pickle_loader_dispatches_through_result_view_defaults(tmp_path):
    source, compile_name = file_loader._build_loader_snippet(
        str(tmp_path / "result.pkl"), "result"
    )

    assert compile_name == "<ngsolve_gui:pickle>"
    assert (
        f"_draw_pickle_object(obj, 'result', field_path={str(tmp_path / 'result.pkl')!r})"
        in source
    )


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


def test_axisymmetric_pickle_detection_requires_matching_solver_metadata(tmp_path):
    gui_dir = tmp_path / "run" / "ngsolve_gui"
    fields_dir = gui_dir / "fields"
    fields_dir.mkdir(parents=True)
    field_path = fields_dir / "B_DC.pkl"
    field_path.write_bytes(b"field")
    (gui_dir / "metadata.json").write_text(
        '{"problem_domain":"axisymmetric","field_files":[{"file":"fields/B_DC.pkl"}]}',
        encoding="utf-8",
    )
    unrelated = fields_dir / "not_a_solver_field.pkl"
    unrelated.write_bytes(b"field")

    assert file_loader._is_axisymmetric_pickle(field_path)
    assert not file_loader._is_axisymmetric_pickle(unrelated)
