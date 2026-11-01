from types import SimpleNamespace

from ngsolve_gui.sections.study_info import StudyInfoSection


def test_study_info_section_shows_run_field_mesh_and_material_details():
    mesh = SimpleNamespace(
        ne=12_345,
        nv=3_210,
        GetMaterials=lambda: ["Copper", "Air"],
    )
    component = SimpleNamespace(
        mesh=mesh,
        run_metadata={
            "schema": "mm-fem.run",
            "status": "complete",
            "kind": "study",
            "model_name": "Copper coil",
            "physics": "time-harmonic electromagnetics",
            "frequency_hz": 50,
            "polynomial_order": 1,
            "fields": ["gfAAC.pkl", "gfBAC.pkl"],
            "materials": ["Copper", "Air"],
        },
        field_metadata={"label": "AC magnetic flux density [T]"},
    )

    rows = dict(StudyInfoSection._collect_rows(component))

    assert rows["Model"] == "Copper coil"
    assert rows["Physics"] == "time-harmonic electromagnetics"
    assert rows["Status"] == "Complete"
    assert rows["Field"] == "AC magnetic flux density [T]"
    assert rows["Frequency"] == "50 Hz"
    assert rows["FE order"] == "1"
    assert rows["Mesh cells"] == "12,345"
    assert rows["Mesh vertices"] == "3,210"
    assert rows["Materials"] == "Copper, Air"
    assert rows["Available fields"] == "2"


def test_study_info_section_falls_back_to_mesh_material_names():
    component = SimpleNamespace(
        mesh=SimpleNamespace(
            ne=8,
            nv=5,
            GetMaterials=lambda: ["Steel", "Air"],
        ),
        run_metadata={"schema": "mm-fem.run", "status": "complete"},
        field_metadata=None,
    )

    assert dict(StudyInfoSection._collect_rows(component))["Materials"] == "Steel, Air"


def test_study_info_section_displays_material_names_for_stable_solver_regions():
    internal_solid = "_mmfem_g0_MMFEM_SOLID_a1b2c3"
    internal_air = "_mmfem_air"
    component = SimpleNamespace(
        mesh=SimpleNamespace(
            ne=8,
            nv=5,
            GetMaterials=lambda: [internal_solid, internal_air],
        ),
        run_metadata={
            "schema": "mm-fem.run",
            "status": "complete",
            "materials": [internal_air, internal_solid, "Air"],
            "material_display_names": {
                internal_solid: "Copper",
                internal_air: "Air",
            },
        },
        field_metadata=None,
    )

    assert dict(StudyInfoSection._collect_rows(component))["Materials"] == "Air, Copper"


def test_other_result_field_action_opens_the_selected_export():
    opened = []
    component = SimpleNamespace(
        available_result_fields=[{
            "path": "C:/results/flux.pkl",
            "label": "Magnetic flux density [T]",
        }],
        _ngsolve_gui_open_result_field=opened.append,
    )

    actions = StudyInfoSection._field_actions(component)
    label, path, open_field = actions[0]
    open_field(path)

    assert label == "Magnetic flux density [T]"
    assert opened == ["C:/results/flux.pkl"]
