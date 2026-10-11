from ngapp.components import Div

from ..cerbsim_style import field_label
from ..prop_widgets import Section


class AxisymmetricRevolutionSection(Section):
    """Switch an explicitly axisymmetric 2D result to its 3D revolution view."""

    section_key = "axisymmetric_revolution"

    def __init__(self, comp):
        self.comp = comp
        super().__init__(
            Div(
                "Show the r–z result revolved through 360°.",
                ui_class=field_label,
            ),
            icon="mdi-rotate-3d-variant",
            title="3D of revolution",
            switchable=True,
            observable=comp.axisymmetric_revolved,
            info=(
                "Create a 3D view by revolving this axisymmetric solution around "
                "its axial axis. Vector components are rotated with the geometry."
            ),
        )
