"""Read-only study metadata for solver-produced result fields."""

from ngapp.components import *

from ..cerbsim_style import field_label
from ..prop_widgets import Section


class StudyInfoSection(Section):
    section_key = "study_info"

    def __init__(self, comp):
        self.comp = comp
        self.metadata_rows = self._collect_rows(comp)
        body = [self._row(label, value) for label, value in self.metadata_rows]
        self.field_buttons = []
        for label, path, open_field in self._field_actions(comp):
            button = QBtn(
                ui_icon="mdi-function-variant",
                ui_label=label,
                ui_flat=True,
                ui_dense=True,
                ui_no_caps=True,
                ui_disable=not callable(open_field),
                ui_style="width:100%; justify-content:flex-start; text-align:left;",
            )
            if callable(open_field):
                button.on_click(
                    lambda _event=None, path=path, callback=open_field: callback(path)
                )
            self.field_buttons.append(button)
        if self.field_buttons:
            body.extend([
                Div("Other exported fields", ui_class=field_label),
                *self.field_buttons,
            ])
        super().__init__(
            *body,
            icon="mdi-clipboard-text-outline",
            title="Study information",
            opened=True,
        )

    @classmethod
    def _collect_rows(cls, comp):
        run = comp.run_metadata or {}
        mesh_data = run.get("mesh") if isinstance(run.get("mesh"), dict) else {}

        rows = [
            ("Model", run.get("model_name")),
            ("Physics", cls._physics_label(run.get("physics"))),
            ("Status", cls._label_value(run.get("status"))),
            ("Field", (comp.field_metadata or {}).get("label")),
            ("Frequency", cls._frequency(run.get("frequency_hz"))),
            ("FE order", cls._order(run.get("polynomial_order"))),
            (
                "Mesh cells",
                cls._count(mesh_data.get("elements", getattr(comp.mesh, "ne", None))),
            ),
            (
                "Mesh vertices",
                cls._count(mesh_data.get("vertices", getattr(comp.mesh, "nv", None))),
            ),
            ("Materials", cls._materials(run, comp.mesh)),
            ("Available fields", cls._field_count(run)),
        ]
        return [(label, str(value)) for label, value in rows if value]

    @staticmethod
    def _field_actions(comp):
        open_field = getattr(comp, "_ngsolve_gui_open_result_field", None)
        actions = []
        for item in getattr(comp, "available_result_fields", []):
            if not isinstance(item, dict) or not item.get("path"):
                continue
            label = item.get("label") or item.get("field_key") or "Result field"
            actions.append((str(label), str(item["path"]), open_field))
        return actions

    @staticmethod
    def _physics_label(value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return "Axisymmetric multiphysics"
        return None

    @staticmethod
    def _label_value(value):
        if not isinstance(value, str) or not value.strip():
            return None
        return value.replace("_", " ").capitalize()

    @staticmethod
    def _frequency(value):
        try:
            return f"{float(value):g} Hz"
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _order(value):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _count(value):
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _materials(run, mesh):
        materials = run.get("materials")
        if not isinstance(materials, (list, tuple)):
            try:
                materials = list(mesh.GetMaterials())
            except (AttributeError, TypeError):
                materials = []
        names = [str(name) for name in materials if str(name).strip()]
        return ", ".join(names) if names else None

    @staticmethod
    def _field_count(run):
        fields = run.get("fields")
        if not isinstance(fields, (list, tuple)):
            return None
        count = len(fields)
        return f"{count}" if count else None

    @staticmethod
    def _row(label, value):
        return Div(
            Div(label, ui_class=field_label),
            Div(
                str(value),
                ui_class="text-body2 text-weight-medium",
                ui_style="text-align:right; overflow-wrap:anywhere;",
            ),
            ui_class="row items-start justify-between no-wrap q-py-xs",
            ui_style="gap:12px;",
        )
