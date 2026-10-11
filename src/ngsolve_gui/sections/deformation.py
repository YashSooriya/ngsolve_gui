from ngapp.components import *

from ..prop_widgets import Section, Segmented, field


class DeformationSection(Section):
    section_key = "deformation"

    def __init__(self, comp):
        self.comp = comp
        if comp.deformation is None and not (comp.cf.dim == 1 and comp.mesh.dim < 3):
            raise ValueError("Deformation not applicable")

        self._deform_scale2 = QSlider(
            ui_model_value=comp.deformation_scale2,
            ui_min=0.0, ui_max=1.0, ui_step=0.01, ui_dense=True, ui_label=True,
        )
        self._multiplier = QInput(
            ui_type="number",
            ui_model_value=comp.deformation_scale,
            ui_dense=True,
            ui_filled=True,
            ui_style="width: 110px;",
        )
        presets = Segmented(
            [("1", "1×"), ("10", "10×"), ("100", "100×"), ("1000", "1000×")],
            "1", self._set_scale,
        )
        controls = [
            field("Exaggeration multiplier", self._multiplier),
            field("Fine adjustment", self._deform_scale2),
            presets,
        ]
        if comp.deformation_complex:
            self._complex_part = QSelect(
                ui_options=[
                    {"label": "Real displacement", "value": "real"},
                    {"label": "Imaginary displacement", "value": "imag"},
                ],
                ui_model_value=comp.deformation_complex_part,
                ui_emit_value=True,
                ui_map_options=True,
                ui_dense=True,
            )
            controls.append(field("Complex displacement", self._complex_part))

        super().__init__(
            *controls,
            icon="mdi-arrow-expand-all",
            title="Deformation",
            switchable=True,
            observable=comp.deformation_enabled,
            info=(
                "Warp the mesh by the solution vector. The multiplier exaggerates "
                "small SI displacements; 1× is true scale. Complex vectors use "
                "their selected real or imaginary displacement part."
            ),
        )

    def _set_scale(self, val):
        try:
            self.comp.deformation_scale.value = float(val)
        except (ValueError, TypeError):
            pass
