"""Controls for an interactive 3-D result slice."""

from ngapp.components import *

from ..prop_widgets import Section, field
from ..cerbsim_style import gap_xs
from ..slice_view import ALL_REGIONS


class SliceViewSection(Section):
    section_key = "slice"

    def __init__(self, comp):
        self.comp = comp
        self.axis = QSelect(
            ui_options=["X", "Y", "Z", "Custom"],
            ui_model_value=comp.slice_axis.value.title(),
            ui_dense=True,
            ui_filled=True,
        )
        self.axis.on_update_model_value(self._set_axis)

        self.region = QSelect(
            ui_options=comp.slice_region_options,
            ui_model_value=comp.slice_region.value,
            ui_dense=True,
            ui_filled=True,
        )
        self.region.on_update_model_value(self._set_region)

        normals = []
        self.normal_inputs = []
        for label, observable in zip(
            ("nx", "ny", "nz"), comp.slice_custom_normal
        ):
            control = QInput(
                ui_type="number",
                ui_model_value=observable.value,
                ui_dense=True,
                ui_filled=True,
            )
            control.on_update_model_value(
                lambda event, obs=observable: self._set_normal(obs, event)
            )
            self.normal_inputs.append(control)
            normals.append(field(label, control))
        self.custom_normal = Row(*normals, ui_class="items-end no-wrap " + str(gap_xs))
        self.custom_normal.ui_hidden = comp.slice_axis.value != "custom"

        comp.slice_axis.on_change(self._sync_axis)
        if len(comp.slice_region_options) > 1:
            region_field = field("Slice region", self.region)
        else:
            region_field = None
        body = [
            field("Plane", self.axis),
            *([region_field] if region_field is not None else []),
            self.custom_normal,
            Div(
                "Move the vertical bar beside the viewport to position the slice. "
                "A selected material is hidden temporarily while other regions stay visible. "
                "All regions hides the volume to show a combined slice.",
                ui_class="text-caption text-grey-7",
            ),
        ]
        super().__init__(
            *body,
            icon="mdi-layers-triple-outline",
            title="Slice",
            switchable=True,
            observable=comp.slice_enabled,
            info=(
                "Show a field slice through the selected material. Other regions remain "
                "visible while the selected region is temporarily hidden."
            ),
        )

    def _set_axis(self, event):
        value = str(getattr(event, "value", event)).lower()
        self.comp.slice_axis.value = value

    def _set_region(self, event):
        self.comp.slice_region.value = str(getattr(event, "value", event))

    def _set_normal(self, observable, event):
        try:
            value = float(getattr(event, "value", event))
            observable.value = value
        except (TypeError, ValueError):
            pass

    def _sync_axis(self, value, _old):
        label = str(value).title()
        self.axis.ui_model_value = label
        self.custom_normal.ui_hidden = str(value).lower() != "custom"
