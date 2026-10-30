"""Small pieces of workspace chrome shared by both modeling workflows."""

from ngapp.components import Div, QBtn, QTooltip


def panel_header(title, *, on_collapse=None, icon=None, tooltip=None):
    """Build the standard heading used by model trees and property inspectors."""
    label = Div(
        str(title).upper(),
        ui_class="mmfem-workspace-panel-title",
        ui_style=(
            "font-size:11px; font-weight:700; letter-spacing:.08em; "
            "color:var(--fg-muted);"
        ),
    )
    children = [label]
    if on_collapse is not None:
        button = QBtn(
            QTooltip(tooltip or f"Collapse {title.lower()}"),
            ui_icon=icon or "mdi-chevron-left",
            ui_flat=True,
            ui_dense=True,
            ui_round=True,
            ui_size="sm",
            ui_class="mmfem-workspace-panel-collapse",
        )
        button._props["aria-label"] = tooltip or f"Collapse {title.lower()}"
        button.on_click(on_collapse)
        children.append(button)
    return Div(
        *children,
        ui_class="mmfem-workspace-panel-header",
        ui_style=(
            "display:flex; align-items:center; gap:8px; flex:none; "
            "min-height:42px; padding:10px 12px; box-sizing:border-box; "
            "border-bottom:1px solid var(--border); color:var(--fg-muted);"
        ),
    )


def property_editor_body(*children, gap=12):
    """Build the scrollable body for contextual property editors."""
    return Div(
        *children,
        ui_class="mmfem-property-editor-body",
        ui_style=(
            "display:flex; flex:1 1 auto; flex-direction:column; width:100%; "
            f"gap:{int(gap)}px; padding:12px; min-width:0; min-height:0; "
            "overflow:auto; box-sizing:border-box;"
        ),
    )
