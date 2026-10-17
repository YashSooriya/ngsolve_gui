from ngapp.components import Div

from ..cerbsim_style import field_label
from ..prop_widgets import Section


class QuarterSymmetrySection(Section):
    """Expand a tagged 3D quarter-domain result into its full model view."""

    section_key = "quarter_symmetry_expansion"

    def __init__(self, comp):
        self.comp = comp
        super().__init__(
            Div(
                "Mirror across the detected SymmX and SymmY planes.",
                ui_class=field_label,
            ),
            icon="mdi-mirror",
            title="Full model by symmetry",
            switchable=True,
            observable=comp.symmetry_expanded,
            info=(
                "Expand a quarter-domain result across its tagged symmetry "
                "planes for visualization. The saved solution is unchanged; "
                "magnetic flux density is reflected as an axial vector."
            ),
        )
