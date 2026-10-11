from ngapp.components import *
from ngsolve_webgpu import *
from .webgpu_tab import WebgpuTab, _usersettings
from .region_state import RegionState
from . import cerbsim_style as cb
from ._visualization import visualization_cf
import ngsolve as ngs
import math

from .slice_view import (
    ALL_REGIONS,
    material_element_mask,
    material_region,
    plane_normal,
    position_on_plane,
)


class _MMFEMColormap(Colormap):
    """Keep autoscale meaningful for physically small engineering fields.

    ``webgpu.Colormap`` treats every range below 1e-12 as numerical zero.
    That erases valid SI displacement fields, which can be much smaller than
    that threshold. Scale only the range passed through its cleanup logic,
    then put the displayed bounds back in the field's original units.
    """

    def widen_range(self, minval, maxval, timestamp=None):
        if not (math.isfinite(minval) and math.isfinite(maxval)) or minval > maxval:
            return super().widen_range(minval, maxval, timestamp=timestamp)

        if timestamp != getattr(self, "_mmfem_scale_timestamp", object()):
            magnitude = max(abs(minval), abs(maxval))
            if 0.0 < magnitude < 1e-12:
                exponent = min(308, max(0, math.ceil(-math.log10(magnitude))))
                self._mmfem_autoscale_factor = 10.0 ** exponent
            else:
                self._mmfem_autoscale_factor = 1.0
            self._mmfem_scale_timestamp = timestamp

        factor = self._mmfem_autoscale_factor
        super().widen_range(minval * factor, maxval * factor, timestamp=timestamp)
        if factor != 1.0:
            self.set_min_max(
                self.minval / factor,
                self.maxval / factor,
                set_autoscale=False,
            )


def _fmt_value(v):
    """Fixed-width numeric format for the pick overlay.

    Scientific notation keeps very large/small magnitudes readable; fixed-point
    is used in the [1e-2, 1e2) range. The result is padded to a constant width
    so the value doesn't jitter horizontally as it updates while hovering.
    """
    v = float(v)
    a = abs(v)
    if a != 0 and (a < 1e-2 or a >= 1e2):
        s = f"{v: .4e}"   # e.g. ' 1.2345e+05' / '-1.2345e+05'
    else:
        s = f"{v: .5f}"   # e.g. ' 12.34567'
    return s.rjust(11)


class FunctionComponent(WebgpuTab):
    def _resolve_deformation(self, deform):
        """Normalize the ``deformation`` Draw argument into a 3d displacement CF."""
        if deform is None or deform is False:
            return None
        if deform is True:
            deform = self.cf
        if not isinstance(deform, ngs.CoefficientFunction):
            print(f"Warning: ignoring deformation of type {type(deform).__name__}")
            return None
        # FacetFESpace functions can't be evaluated inside elements
        if isinstance(deform, ngs.GridFunction) and isinstance(
            deform.space, ngs.FacetFESpace
        ):
            return None
        if deform.dim == 3:
            return deform
        if deform.dim == 2:
            return ngs.CF((deform[0], deform[1], 0))
        if deform.dim == 1 and self.mesh.dim < 3:
            return ngs.CF((0, 0, deform))
        print(
            f"Warning: cannot use a {deform.dim}-dimensional field as deformation "
            f"on a {self.mesh.dim}d mesh"
        )
        return None

    def __init__(self, name, data, app_data):
        self.app_data = app_data
        cf = data["obj"]
        self.name = name
        self.mdata = None
        self.cf = cf
        component_names = data.get("_ngsolve_gui_component_names")
        if component_names is not None and len(component_names) == getattr(cf, "dim", 1):
            self.component_names = tuple(str(name) for name in component_names)
        else:
            self.component_names = None
        self.visualization_cf = visualization_cf(cf)
        self.region_or_mesh = data["mesh"]
        self.draw_vol = data.get("draw_vol", True)
        self.draw_surf = data.get("draw_surf", True)
        self.draw_edges = data.get("edges", False)
        self.mesh = (
            self.region_or_mesh.mesh
            if isinstance(self.region_or_mesh, ngs.Region)
            else self.region_or_mesh
        )
        self.slice_available = self.mesh.dim == 3 and bool(self.draw_vol)
        self.slice_region_options = (
            [ALL_REGIONS] + [str(name) for name in self.mesh.GetMaterials()]
            if self.slice_available
            else []
        )
        self.fieldline_seed_region_options = (
            [ALL_REGIONS] + [str(name) for name in self.mesh.GetMaterials()]
            if self.cf.dim == self.mesh.dim
            else []
        )
        self.order = data.get("order", None)
        if self.order is None:
            self.order = 2
            if isinstance(cf, ngs.GridFunction):
                self.order = min(2, cf.space.globalorder)
        self.deformation = self._resolve_deformation(data.get("deformation", None))
        self.deformation_order = data.get("deformation_order", self.order)
        self.facet = data.get("facet", None)
        self.contact = data.get("contact", None)
        self.contact_pairs = None
        self.facet_renderer = None
        self._facet_supported = bool(self.draw_vol) and self.mesh.dim in (2, 3)

        # -- Resolve initial values from data args + saved settings ---------
        tab = app_data.get_tab(name)
        saved = tab.get("settings", {}) if tab else {}
        minval = data.get("min", 0.0)
        maxval = data.get("max", 1.0)
        autoscale = not ("min" in data or "max" in data) and not data.get(
            "autoscale", False
        )
        discrete_colormap = data.get(
            "discrete_colormap", _usersettings.get("default_discrete_colormap", False)
        )
        if any([v in data for v in ("min", "max", "discrete_colormap", "autoscale")]):
            saved["colormap"] = (autoscale, discrete_colormap, minval, maxval)

        if (
            self.deformation is None
            and not "deformation" in data
            and self.cf.dim == 1
            and self.mesh.dim < 3
        ):
            self.deformation = self._resolve_deformation(True)

        self.axisymmetric_revolution_available = bool(
            data.get("_ngsolve_gui_axisymmetric", False)
            and self.mesh.dim == 2
            and isinstance(cf, ngs.GridFunction)
        )
        self.axisymmetric_revolved = Observable(False, "axisymmetric_revolved")
        self._axisymmetric_revolution_mesh = None
        self._axisymmetric_revolution_field = None
        self._axisymmetric_original_state = None
        self._axisymmetric_camera_state = None
        if self.axisymmetric_revolution_available:
            from .sections.axisymmetric_revolution import AxisymmetricRevolutionSection

            sections = list(type(self).property_sections)
            sections.insert(1, AxisymmetricRevolutionSection)
            self.property_sections = sections
            self._axisymmetric_original_state = {
                "mesh": self.mesh,
                "region_or_mesh": self.region_or_mesh,
                "cf": self.cf,
                "visualization_cf": self.visualization_cf,
                "deformation": self.deformation,
            }

        self._quarter_symmetry_planes = ()
        self.quarter_symmetry_expansion_available = False
        self.symmetry_expanded = Observable(False, "symmetry_expanded")
        self._symmetry_expanded_mesh = None
        self._symmetry_expanded_field = None
        self._symmetry_original_state = None
        self._symmetry_camera_state = None
        if self.mesh.dim == 3 and isinstance(cf, ngs.GridFunction):
            from .quarter_symmetry import find_quarter_symmetry_planes

            self._quarter_symmetry_planes = tuple(
                data.get("_ngsolve_gui_symmetry_planes")
                or find_quarter_symmetry_planes(self.mesh)
            )
            self.quarter_symmetry_expansion_available = (
                len(self._quarter_symmetry_planes) == 2
            )
            if self.quarter_symmetry_expansion_available:
                from .sections.quarter_symmetry import QuarterSymmetrySection

                sections = list(type(self).property_sections)
                sections.insert(1, QuarterSymmetrySection)
                self.property_sections = sections
                self._symmetry_original_state = {
                    "mesh": self.mesh,
                    "region_or_mesh": self.region_or_mesh,
                    "cf": self.cf,
                    "visualization_cf": self.visualization_cf,
                    "deformation": self.deformation,
                }
                field_name = str(name).lower()
                self._symmetry_vector_kind = data.get(
                    "_ngsolve_gui_symmetry_vector_kind",
                    "axial"
                    if any(
                        marker in field_name
                        for marker in (
                            "magnetic flux density",
                            "magnetic induction",
                            "magnetic field",
                            "gfbdc",
                            "gfbac",
                            "b_dc",
                            "b_ac",
                            "h_dc",
                            "h_ac",
                        )
                    )
                    else "polar",
                )

        if self.slice_available:
            sections = list(
                getattr(self, "property_sections", type(self).property_sections)
            )
            if SliceViewSection not in sections:
                sections.insert(1, SliceViewSection)
            self.property_sections = sections

        cv = data.get("clipping_vectors", False)
        sv = data.get("surface_vectors", False)
        fl = data.get("field_lines", False)
        lic = data.get("lic", False)

        # -- Observable properties ------------------------------------------
        s = saved
        self.wireframe_visible = Observable(
            data.get("wireframe", s.get("wireframe_visible", True)), "wireframe_visible"
        )
        self.elements2d_visible = Observable(
            s.get("elements2d_visible", True), "elements2d_visible"
        )
        self.subdivision = Observable(
            data.get("subdivision",
                     s.get("subdivision",
                           int(_usersettings.get("default_subdivision", -1)))),
            "subdivision", converter=int,
        )
        self.facet_visible = Observable(
            bool(self.facet) if self.facet is not None else s.get("facet_visible", False),
            "facet_visible",
        )
        self.facet_thickness = Observable(
            s.get("facet_thickness", 0.008), "facet_thickness", converter=float
        )
        self.clipping_vectors_visible = Observable(
            bool(cv) if cv else s.get("clipping_vectors", False), "clipping_vectors"
        )
        self.surface_vectors_visible = Observable(
            bool(sv) if sv else s.get("surface_vectors", False), "surface_vectors"
        )
        self.field_lines_visible = Observable(
            bool(fl) if fl else s.get("field_lines", False), "field_lines"
        )
        self.clipping_visible = Observable(
            data.get("clipping_function", s.get("clipping_visible", True)),
            "clipping_visible",
        )
        # -- LIC (line integral convolution) on the clipping plane --
        self.lic_visible = Observable(
            bool(lic) if lic else s.get("lic_visible", False), "lic_visible"
        )
        self.lic_kernel_length = Observable(
            s.get("lic_kernel_length", 30), "lic_kernel_length", converter=int
        )
        self.lic_oriented = Observable(
            s.get("lic_oriented", False), "lic_oriented"
        )
        self.lic_thickness = Observable(
            s.get("lic_thickness", 2), "lic_thickness", converter=int
        )
        self.lic_contrast = Observable(
            s.get("lic_contrast", 1.0), "lic_contrast", converter=float
        )
        # Supersampling (SSAA) checkbox: off → 1 sample, on → 2 samples.
        self.lic_supersample = Observable(
            s.get("lic_supersample", False), "lic_supersample"
        )
        self.vector_grid_size = Observable(
            (cv if not isinstance(cv, bool) else None)
            or (sv if not isinstance(sv, bool) else None)
            or s.get("vector_grid_size", int(_usersettings.get("default_vector_grid_size", 20))),
            "vector_grid_size",
        )
        self.vector_scale = Observable(
            s.get("vector_scale", 1.0), "vector_scale", converter=float
        )
        self.vector_scale_by_value = Observable(
            s.get("vector_scale_by_value", False), "vector_scale_by_value",
        )
        self.deformation_enabled = Observable(
            ("deformation" in data and self.deformation is not None)
            or s.get("deformation_enabled", False),
            "deformation_enabled",
        )
        self.deformation_scale = Observable(
            s.get("deformation_scale", 1.0), "deformation_scale", converter=float
        )
        self.deformation_scale2 = Observable(
            s.get("deformation_scale2", 1.0), "deformation_scale2", converter=float
        )
        cm = s.get("colormap", (autoscale, discrete_colormap, minval, maxval))
        self.colormap_autoscale = Observable(cm[0], "colormap_autoscale")
        self.colormap_discrete = Observable(cm[1], "colormap_discrete")
        self.colormap_min = Observable(cm[2], "colormap_min", converter=float, formatter=lambda v: f"{v:.4g}")
        self.colormap_max = Observable(cm[3], "colormap_max", converter=float, formatter=lambda v: f"{v:.4g}")
        self.colormap_name = Observable(
            s.get("colormap_name", cb.default_colormap(_usersettings)), "colormap_name"
        )
        self.ncolors_colormap = Observable(
            s.get("ncolors_colormap", int(_usersettings.get("default_ncolors", 8))),
            "ncolors_colormap", converter=int,
        )
        self.contact_enabled = Observable(
            s.get("contact_enabled", True), "contact_enabled"
        )
        self.fieldlines_num_lines = Observable(
            data.get("fieldlines_num_lines", s.get("fieldlines_num_lines", 20)), "fieldlines_num_lines", converter=int
        )
        self.fieldlines_length = Observable(
            s.get("fieldlines_length", data.get("fieldlines_length", 0.5)), "fieldlines_length", converter=float
        )
        self.fieldlines_thickness = Observable(
            data.get("fieldlines_thickness", s.get("fieldlines_thickness", 0.0001)), "fieldlines_thickness", converter=float
        )
        self.fieldlines_direction = Observable(
            s.get("fieldlines_direction", 0), "fieldlines_direction", converter=int
        )
        default_fieldline_seed_region = data.get(
            "_ngsolve_gui_fieldline_seed_material", ALL_REGIONS
        )
        if default_fieldline_seed_region not in self.fieldline_seed_region_options:
            default_fieldline_seed_region = ALL_REGIONS
        saved_fieldline_seed_region = s.get(
            "fieldline_seed_region", default_fieldline_seed_region
        )
        if saved_fieldline_seed_region not in self.fieldline_seed_region_options:
            saved_fieldline_seed_region = default_fieldline_seed_region
        self.fieldline_seed_region = Observable(
            saved_fieldline_seed_region, "fieldline_seed_region"
        )

        self.hidden_regions = Observable(
            list(s.get("hidden_regions", [])), "hidden_regions"
        )
        self.boundary_overrides = Observable(
            dict(s.get("boundary_overrides", {})), "boundary_overrides"
        )

        default_slice_region = (
            self.slice_region_options[1]
            if len(self.slice_region_options) > 1
            else ALL_REGIONS
        )
        saved_slice_region = s.get("slice_region", default_slice_region)
        if saved_slice_region not in self.slice_region_options:
            saved_slice_region = default_slice_region
        self.slice_enabled = Observable(
            bool(s.get("slice_enabled", False)) and self.slice_available,
            "slice_enabled",
        )
        self.slice_axis = Observable(
            str(s.get("slice_axis", "z")).lower(), "slice_axis"
        )
        if self.slice_axis.value not in ("x", "y", "z", "custom"):
            self.slice_axis.value = "z"
        self.slice_position = Observable(
            min(1.0, max(0.0, float(s.get("slice_position", 0.5)))),
            "slice_position",
            converter=float,
        )
        self.slice_region = Observable(saved_slice_region, "slice_region")
        custom_normal = s.get("slice_custom_normal", (0.0, 0.0, 1.0))
        try:
            if len(custom_normal) != 3:
                custom_normal = (0.0, 0.0, 1.0)
        except TypeError:
            custom_normal = (0.0, 0.0, 1.0)
        self.slice_custom_normal = []
        for index, axis in enumerate(("x", "y", "z")):
            normal_value = Observable(
                float(s.get(f"slice_normal_{axis}", custom_normal[index])),
                f"slice_normal_{axis}",
                converter=float,
            )
            setattr(self, f"slice_normal_{axis}", normal_value)
            self.slice_custom_normal.append(normal_value)
        self.slice_renderer = None
        self._slice_function_data = None
        self._slice_clipping = None
        self._slice_overlay = None
        self._slice_position_slider = None
        self._slice_position_label = None
        self._slice_bounds_cache = None

        if self.cf.is_complex:
            self.complex_mode = Observable(
                s.get("complex_mode", "real"), "complex_mode"
            )
            self.complex_animate = Observable(False, "complex_animate")
            self.complex_speed = Observable(
                s.get("complex_speed", 1.0), "complex_speed", converter=float
            )

        # -- Entity number observables --
        self.entity_number_entities = ["vertices", "edges", "facets", "segments", "surface_elements"]
        if self.mesh.dim == 3:
            self.entity_number_entities.append("volume_elements")
        self.entity_number_entities += ["surface_indices", "segment_indices"]
        if self.mesh.dim == 3:
            self.entity_number_entities.append("volume_indices")
        for entity in self.entity_number_entities:
            key = f"{entity}_numbers_visible"
            setattr(self, key, Observable(saved.get(key, False), key))
        self.numbers_one_based = Observable(
            saved.get("numbers_one_based", False), "numbers_one_based"
        )

        super().__init__(name, data, app_data)

        # -- Wire GPU side-effects -----------------------------------------
        self.wireframe_visible.on_change(self._apply_wireframe)
        self.elements2d_visible.on_change(self._apply_elements2d)
        self.subdivision.on_change(self._apply_subdivision)
        self.facet_visible.on_change(self._apply_facet)
        self.facet_thickness.on_change(self._apply_facet_thickness)
        self.clipping_vectors_visible.on_change(self._apply_clipping_vectors)
        self.surface_vectors_visible.on_change(self._apply_surface_vectors)
        self.field_lines_visible.on_change(self._apply_fieldlines)
        self.fieldline_seed_region.on_change(self._apply_fieldline_seed_region)
        self.clipping_visible.on_change(self._apply_clipping_function)
        self.lic_visible.on_change(self._apply_lic)
        self.lic_kernel_length.on_change(self._apply_lic_kernel_length)
        self.lic_oriented.on_change(self._apply_lic_oriented)
        self.lic_thickness.on_change(self._apply_lic_thickness)
        self.lic_contrast.on_change(self._apply_lic_contrast)
        self.lic_supersample.on_change(self._apply_lic_supersample)
        self.vector_grid_size.on_change(self._apply_vector_grid_size)
        self.vector_scale.on_change(self._apply_vector_scale)
        self.vector_scale_by_value.on_change(self._apply_vector_scale_by_value)
        self.deformation_enabled.on_change(self._apply_deformation_toggle)
        self.deformation_scale.on_change(self._apply_deformation_scale)
        self.deformation_scale2.on_change(self._apply_deformation_scale)
        self.contact_enabled.on_change(self._apply_contact)
        self.colormap_autoscale.on_change(self._apply_autoscale)
        self.colormap_discrete.on_change(self._apply_discrete)
        self.colormap_name.on_change(self._apply_colormap_name)
        if self.cf.is_complex:
            self.complex_mode.on_change(self._apply_complex_mode)
            self.complex_animate.on_change(self._apply_complex_animate)
            self.complex_speed.on_change(self._apply_complex_speed)
        for entity in self.entity_number_entities:
            obs = getattr(self, f"{entity}_numbers_visible")
            obs.on_change(lambda val, _old, e=entity: self._apply_entity_numbers(e, val))
        self.numbers_one_based.on_change(self._apply_numbers_one_based)
        self.hidden_regions.on_change(self._apply_region_change)
        self.boundary_overrides.on_change(self._apply_region_change)
        if self.slice_available:
            self.slice_enabled.on_change(self._apply_slice_enabled)
            self.slice_axis.on_change(self._apply_slice_plane)
            self.slice_position.on_change(self._apply_slice_plane)
            self.slice_region.on_change(self._apply_slice_region)
            for normal_value in self.slice_custom_normal:
                normal_value.on_change(self._apply_slice_plane)
        if self.axisymmetric_revolution_available:
            self.axisymmetric_revolved.on_change(self._apply_axisymmetric_revolved)
        if self.quarter_symmetry_expansion_available:
            self.symmetry_expanded.on_change(self._apply_symmetry_expanded)

    # -- GPU side-effect handlers -------------------------------------------

    def _apply_wireframe(self, val, _old):
        self.wireframe.active = val
        self.wgpu.scene.render()

    def _apply_elements2d(self, val, _old):
        self._sync_surface_elements()
        self.wgpu.scene.render()

    SUBDIVISION_MAX = 40

    def _subdivision_override(self):
        """UI subdivision → MeshData.subdivision. -1 = auto (None, derived from
        the mesh curve/deformation order); 0 = linear (subdivision 1); N =
        subdivision N+1. Raising it tessellates each element into more render
        triangles, so a high-order function renders smoothly instead of
        piecewise-flat."""
        v = int(self.subdivision.value)
        v = max(-1, min(self.SUBDIVISION_MAX, v))
        return None if v < 0 else v + 1

    def _apply_subdivision(self, val, _old):
        clamped = max(-1, min(self.SUBDIVISION_MAX, int(val)))
        if clamped != int(val):
            self.subdivision.value = clamped
            return
        func_data = getattr(self, "func_data", None)
        if func_data is None:
            return
        mdata = func_data.mesh_data
        mdata.subdivision = self._subdivision_override()
        mdata.set_needs_update()
        for obj in self.scene.render_objects:
            obj.set_needs_update()
        self.wgpu.scene.render()

    def _sync_surface_elements(self):
        """Show the flat surface field unless a surface LIC is replacing it.

        A SurfaceLIC is itself the surface renderer, so drawing elements2d on top
        of it z-fights; hide elements2d while the surface LIC is visible (the 3D
        ClippingLIC, by contrast, is independent of elements2d)."""
        if self.elements2d is None:
            return
        hide_for_lic = (
            self._lic_is_surface
            and self.lic is not None
            and self.lic_visible.value
        )
        self.elements2d.active = self.elements2d_visible.value and not hide_for_lic

    def _create_facet_renderer(self):
        """Build the element-boundary renderer on demand.

        Extracting the CF on all element boundaries is expensive, so this only
        runs once the user actually switches element-boundary rendering on."""
        if self.facet_renderer is not None or not self._facet_supported:
            return self.facet_renderer
        facet_cf = (
            self.facet if isinstance(self.facet, ngs.CoefficientFunction) else self.cf
        )
        try:
            if self.mesh.dim == 2:
                # deformation is baked in unconditionally; the deformation_scale
                # uniform (0 when disabled) switches it on and off
                facet_data = FacetFunctionData(
                    self._mesh_data, facet_cf, order=self.order,
                    deformation_cf=self.deformation,
                )
                self.facet_renderer = FacetCFRenderer(
                    facet_data, colormap=self.colormap, clipping=self.clipping,
                    thickness=self.facet_thickness.value,
                )
            else:
                from ngsolve_webgpu.facet_cf import FacetCFRenderer3D

                self.facet_renderer = FacetCFRenderer3D(
                    self._mesh_data, facet_cf, order=self.order,
                    colormap=self.colormap, clipping=self.clipping,
                )
        except Exception as e:
            import traceback
            print(f"Warning: facet renderer creation failed: {e}")
            traceback.print_exc()
            self.facet_renderer = None
            return None
        self.facet_renderer.region_visibility = self.region_visibility
        return self.facet_renderer

    def _apply_facet(self, visible, _old):
        if getattr(self, "_mesh_data", None) is None:
            return
        if visible and self.facet_renderer is None:
            if self._create_facet_renderer() is None:
                return
            self.scene.render_objects.append(self.facet_renderer)
        if self.facet_renderer is not None:
            self.facet_renderer.active = visible
            self.wgpu.scene.render()

    def _apply_facet_thickness(self, val, _old):
        if self.facet_renderer is not None:
            self.facet_renderer.thickness = val
            self.facet_renderer.set_needs_update()
            self.wgpu.scene.render()

    def _apply_clipping_vectors(self, val, _old):
        if self.clipping_vectors is not None:
            self.clipping_vectors.active = val
        self.wgpu.scene.render()

    def _apply_surface_vectors(self, val, _old):
        if self.surface_vectors is not None:
            self.surface_vectors.active = val
        self.wgpu.scene.render()

    def _selected_fieldline_seed_region(self):
        """Return the selected streamline seed region, including the all option."""
        selected = getattr(self, "fieldline_seed_region", None)
        if selected is not None:
            return selected.value
        data = self.data if isinstance(getattr(self, "data", None), dict) else {}
        name = getattr(
            getattr(self, "fieldlines", None),
            "_ngsolve_gui_start_region_name",
            None,
        )
        if name is None:
            if data.get("_ngsolve_gui_fast_fieldlines", False):
                name = data.get("_ngsolve_gui_fieldline_seed_material")
        if name is None:
            return None
        return name if name in {str(m) for m in self.mesh.GetMaterials()} else None

    def _sync_fieldline_seed_visibility(self, active):
        """Hide active streamline/slice seed materials without changing user state."""
        region_state = getattr(self, "region_state", None)
        if region_state is None:
            return False
        auto_hidden = set()
        fieldlines_active = (
            bool(active)
            if active is not None
            else bool(self.field_lines_visible.value)
        )
        seed = self._selected_fieldline_seed_region() if fieldlines_active else None
        if seed == ALL_REGIONS:
            auto_hidden.update(region_state.unique_materials)
        elif seed is not None:
            auto_hidden.add(seed)
        if getattr(self, "slice_available", False) and self.slice_enabled.value:
            if self.slice_region.value == ALL_REGIONS:
                auto_hidden.update(region_state.unique_materials)
            else:
                auto_hidden.add(self.slice_region.value)
        if region_state.auto_hidden == auto_hidden:
            return False
        region_state.auto_hidden = auto_hidden
        return True

    def _apply_fieldlines(self, val, _old):
        if getattr(self, "fieldlines", None) is not None:
            self.fieldlines.active = val
        if self._sync_fieldline_seed_visibility(bool(val)):
            self._apply_region_change()
        else:
            self.wgpu.scene.render()

    def _apply_fieldline_seed_region(self, value, _old):
        """Update the streamline start region and refresh its cached trace."""
        renderer = getattr(self, "fieldlines", None)
        if renderer is None:
            return
        region_name = str(value)
        renderer.start_region = material_region(self.mesh, region_name)
        renderer._ngsolve_gui_start_region_name = region_name
        renderer.set_needs_update()
        if self._sync_fieldline_seed_visibility(None):
            self._apply_region_change()
        else:
            self.wgpu.scene.render()

    def _build_additional_viewport_overlays(self):
        """Add the tall slice-position scrollbar beside a 3-D result view."""
        if not self.slice_available:
            return []
        self._slice_position_slider = QSlider(
            ui_model_value=self.slice_position.value,
            ui_min=0.0,
            ui_max=1.0,
            ui_step=0.001,
            ui_vertical=True,
            ui_dense=True,
            ui_color="primary",
            ui_track_size="4px",
            ui_thumb_size="16px",
            ui_style="flex:1; min-height:0; width:28px;",
        )
        self._slice_position_slider.on_update_model_value(
            self._on_slice_position_update
        )
        self._slice_position_label = Div(
            "0 m", ui_style="font-size:10px; color:var(--fg-muted);"
        )
        self._slice_overlay = Div(
            Div(
                QIcon(ui_name="mdi-layers-triple-outline"),
                "Slice",
                ui_class="row items-center justify-center no-wrap q-gutter-x-xs",
                ui_style="font-size:11px; font-weight:600;",
            ),
            Div("max", ui_style="font-size:9px; color:var(--fg-muted);"),
            self._slice_position_slider,
            Div("min", ui_style="font-size:9px; color:var(--fg-muted);"),
            self._slice_position_label,
            ui_style=(
                "position:absolute; z-index:12; left:12px; top:7%; height:86%; "
                "width:48px; display:flex; flex-direction:column; align-items:center; "
                "justify-content:space-between; padding:8px 4px; border-radius:10px; "
                "background:color-mix(in srgb, var(--surface) 92%, transparent); "
                "border:1px solid var(--border); box-shadow:0 2px 12px #0002;"
            ),
        )

        self._slice_overlay.ui_hidden = not self.slice_enabled.value
        self._sync_slice_position_overlay()
        return [self._slice_overlay]

    def _slice_bounds(self):
        if self._slice_bounds_cache is None:
            import numpy as np

            coordinates = np.asarray(self.mesh.ngmesh.Coordinates(), dtype=float)
            if (
                coordinates.ndim != 2
                or coordinates.shape[1] < 3
                or not len(coordinates)
            ):
                raise ValueError("Could not determine the 3-D mesh bounds")
            xyz = coordinates[:, :3]
            self._slice_bounds_cache = (xyz.min(axis=0), xyz.max(axis=0))
        return self._slice_bounds_cache

    def _slice_normal(self):
        return plane_normal(
            self.slice_axis.value,
            [value.value for value in self.slice_custom_normal],
        )

    def _slice_plane_data(self):
        return position_on_plane(
            self._slice_bounds(), self._slice_normal(), self.slice_position.value
        )

    def _sync_slice_position_overlay(self):
        if self._slice_position_slider is not None:
            self._slice_position_slider.ui_model_value = self.slice_position.value
        if self._slice_position_label is None:
            return
        try:
            _center, _offset, distance = self._slice_plane_data()
            self._slice_position_label.ui_children = [f"{distance:.4g} m"]
        except (ValueError, TypeError):
            self._slice_position_label.ui_children = ["—"]

    def _on_slice_position_update(self, event):
        try:
            value = float(getattr(event, "value", event))
            self.slice_position.value = min(1.0, max(0.0, value))
        except (TypeError, ValueError):
            pass

    def _apply_slice_plane(self, _value=None, _old=None):
        if self._slice_clipping is None:
            self._sync_slice_position_overlay()
            return
        try:
            center, offset, _distance = self._slice_plane_data()
            self._slice_clipping.center = [float(v) for v in center]
            self._slice_clipping.normal = [float(v) for v in self._slice_normal()]
            self._slice_clipping.offset = float(offset)
        except (ValueError, TypeError):
            # A custom zero normal is temporarily invalid while the user edits
            # its three components; keep the last valid plane until corrected.
            return
        self._sync_slice_position_overlay()
        self._invalidate_slice_renderer()
        if getattr(self, "wgpu", None) is not None and self.wgpu.scene is not None:
            self.wgpu.scene.render()

    def _invalidate_slice_renderer(self):
        if self.slice_renderer is None:
            return
        # Plane movement only changes clipping uniforms. Invalidate the render
        # object without forcing the potentially expensive field interpolation
        # data to be evaluated again.
        from webgpu.renderer import Renderer

        Renderer.set_needs_update(self.slice_renderer)

    def _apply_slice_enabled(self, value, _old):
        if self.slice_renderer is not None:
            self.slice_renderer.active = bool(value)
            if value:
                self._invalidate_slice_renderer()
        if self._slice_overlay is not None:
            self._slice_overlay.ui_hidden = not bool(value)
        changed = self._sync_fieldline_seed_visibility(None)
        if changed:
            self._apply_region_change()
        elif self.wgpu.scene is not None:
            self.wgpu.scene.render()

    def _apply_slice_region(self, _value, _old):
        self._sync_fieldline_seed_visibility(None)
        if getattr(self, "wgpu", None) is not None and self.wgpu.scene is not None:
            self.draw()

    def _create_slice_renderer(self):
        """Create the cross-section field pass for the selected volume region."""
        if not self.slice_available:
            return None
        import numpy as np
        from webgpu.clipping import Clipping

        from ngsolve_webgpu import ClippingIsolineRenderer, FunctionData, MeshData

        material = self.slice_region.value
        mask = material_element_mask(self.mesh, material)
        mesh_scope = (
            self.mesh
            if material == ALL_REGIONS
            else self.mesh.Materials(material)
        )
        mesh_data = MeshData(mesh_scope, el3d_bitarray=mask)
        function_data = FunctionData(
            mesh_data, self.visualization_cf, order=self.order
        )
        try:
            center, offset, _distance = self._slice_plane_data()
            normal = self._slice_normal()
        except ValueError:
            # Keep a valid last-resort plane while the user is editing a
            # temporarily zero custom normal.
            normal = np.asarray((0.0, 0.0, 1.0), dtype=float)
            center, offset, _distance = position_on_plane(
                self._slice_bounds(), normal, self.slice_position.value
            )
        clipping = Clipping(
            mode=Clipping.Mode.PLANE,
            center=np.asarray(center, dtype=float).tolist(),
            normal=np.asarray(normal, dtype=float).tolist(),
            offset=float(offset),
        )
        renderer = ClippingIsolineRenderer(
            function_data,
            clipping=clipping,
            n_lines=0,
            show_field=True,
            colormap=self.colormap,
        )
        # The selected region is temporarily hidden in the regular mesh render;
        # don't apply that alpha override to the slice's own cut-face renderer.
        renderer.region_visibility = None
        renderer.active = self.slice_enabled.value
        if self.cf.is_complex:
            renderer.set_complex_mode(self.complex_mode.value)
        self._slice_function_data = function_data
        self._slice_clipping = clipping
        self.slice_renderer = renderer
        return renderer

    def _apply_clipping_function(self, val, _old):
        if self.clippingcf is not None:
            self.clippingcf.active = val
        self.wgpu.scene.render()

    def _apply_lic(self, val, _old):
        if self.lic is not None:
            self.lic.active = val
        self._sync_surface_elements()
        self.wgpu.scene.render()

    def _apply_lic_kernel_length(self, val, _old):
        if self.lic is not None:
            self.lic.set_kernel_length(val)
        self.wgpu.scene.render()

    def _apply_lic_oriented(self, val, _old):
        if self.lic is not None:
            self.lic.set_oriented(val)
        self.wgpu.scene.render()

    def _apply_lic_thickness(self, val, _old):
        if self.lic is not None:
            self.lic.set_thickness(val)
        self.wgpu.scene.render()

    def _apply_lic_contrast(self, val, _old):
        if self.lic is not None:
            self.lic.set_contrast(val)
        self.wgpu.scene.render()

    def _apply_lic_supersample(self, val, _old):
        if self.lic is not None:
            # Checkbox toggles between 1 (off) and 2 (on) samples per pixel.
            self.lic.set_supersample(2 if val else 1)
        self.wgpu.scene.render()

    def _apply_vector_grid_size(self, val, _old):
        if self.clipping_vectors is not None:
            self.clipping_vectors.set_grid_size(val)
            self.clipping_vectors.set_needs_update()
        if self.surface_vectors is not None:
            self.surface_vectors.set_grid_size(val)
            self.surface_vectors.set_needs_update()
        self.wgpu.scene.render()

    def _apply_vector_scale(self, val, _old):
        for r in self._vector_renderers:
            r.user_scale = val
            r.set_needs_update()
        self.wgpu.scene.render()

    def _apply_vector_scale_by_value(self, val, _old):
        for r in self._vector_renderers:
            r.scale_by_value = val
            r.set_needs_update()
        self.wgpu.scene.render()

    def _apply_deformation_toggle(self, val, _old):
        if self.mdata is None:
            return
        if val:
            self.mdata.deformation_scale = (
                self.deformation_scale.value * self.deformation_scale2.value
            )
        else:
            self.mdata.deformation_scale = 0.0
        self._refresh_deformed_renderers()

    def _apply_deformation_scale(self, _val, _old):
        if self.mdata is None:
            return
        if self.deformation_enabled.value:
            self.mdata.deformation_scale = (
                self.deformation_scale.value * self.deformation_scale2.value
            )
            self._refresh_deformed_renderers()

    def _refresh_deformed_renderers(self):
        """Renderers that read the deformation scale on their own (i.e. not via
        the shared mesh uniform) need an explicit update."""
        if self.clippingcf is not None:
            self.clippingcf.set_needs_update()
        if self.facet_renderer is not None:
            self.facet_renderer.set_needs_update()
        self.wgpu.scene.render()

    def _pick_info(self, result):
        """Element / region / position / value — value uses the *undeformed*
        point so it stays correct when deformation is on."""
        header, rows, _ = super()._pick_info(result)
        val = self._eval_cf(self._probe_mesh_point(result.world_pos))
        if val is not None:
            if val.size == 1:
                rows.append(("value", _fmt_value(val)))
            else:
                rows.append(("value", "[" + ", ".join(_fmt_value(v) for v in val.flat) + "]"))
            return ("Picked value", rows, True)
        return ("Picked value", rows, False)

    def _eval_cf(self, P):
        try:
            import numpy as np
            mip = self.mesh(*[float(P[i]) for i in range(self.mesh.dim)])
            return np.real(np.asarray(self.cf(mip)))
        except Exception:
            return None

    def _apply_contact(self, val, _old):
        if self.contact_pairs is not None:
            self.contact_pairs.active = val
        self.wgpu.scene.render()

    def _apply_entity_numbers(self, entity, val):
        self._entity_number_renderers[entity].active = val
        self.wgpu.scene.render()

    def _apply_numbers_one_based(self, val, _old):
        for r in self._entity_number_renderers.values():
            r.zero_based = not val
            r.set_needs_update()
        self.wgpu.scene.render()

    def _apply_axisymmetric_revolved(self, enabled, _old):
        if not self.axisymmetric_revolution_available:
            return

        if enabled:
            if self._axisymmetric_revolution_mesh is None:
                from .axisymmetric_revolution import revolve_gridfunction

                try:
                    (
                        self._axisymmetric_revolution_mesh,
                        self._axisymmetric_revolution_field,
                    ) = revolve_gridfunction(self._axisymmetric_original_state["cf"])
                except Exception as error:
                    print(f"Could not create axisymmetric 3D view: {error}")
                    self.axisymmetric_revolved.value = False
                    return

            camera = self.camera
            self._axisymmetric_camera_state = {
                "shared": bool(self.camera_shared.value),
                "transform": camera.transform.copy(),
                "orthographic": camera.orthographic,
            }
            if self.camera_shared.value:
                self.camera_shared.value = False

            self.mesh = self._axisymmetric_revolution_mesh
            self.region_or_mesh = self._axisymmetric_revolution_mesh
            self.cf = self._axisymmetric_revolution_field
            self.visualization_cf = visualization_cf(self.cf)
            self.deformation = None
        else:
            original = self._axisymmetric_original_state
            self.mesh = original["mesh"]
            self.region_or_mesh = original["region_or_mesh"]
            self.cf = original["cf"]
            self.visualization_cf = original["visualization_cf"]
            self.deformation = original["deformation"]

        self.region_state = None
        self.region_visibility = None
        self._full_range = None
        self._mesh_data = None
        self.mdata = None
        self._slice_bounds_cache = None
        self._facet_supported = bool(self.draw_vol) and self.mesh.dim in (2, 3)
        self.draw()
        self._rebuild_dimension_controls()

        if enabled:
            self.reset_camera()
        elif self._axisymmetric_camera_state is not None:
            camera_state = self._axisymmetric_camera_state
            if camera_state["shared"]:
                self.camera_shared.value = True
            else:
                camera = self.camera
                camera.transform = camera_state["transform"]
                camera.orthographic = camera_state["orthographic"]
                self.scene.render()
            self._axisymmetric_camera_state = None

    def _apply_symmetry_expanded(self, enabled, _old):
        if not self.quarter_symmetry_expansion_available:
            return

        if enabled:
            if self._symmetry_expanded_mesh is None:
                from .quarter_symmetry import mirror_quarter_gridfunction

                try:
                    (
                        self._symmetry_expanded_mesh,
                        self._symmetry_expanded_field,
                    ) = mirror_quarter_gridfunction(
                        self._symmetry_original_state["cf"],
                        self._quarter_symmetry_planes,
                        vector_kind=self._symmetry_vector_kind,
                    )
                except Exception as error:
                    print(f"Could not create full symmetry view: {error}")
                    self.symmetry_expanded.value = False
                    return

            camera = self.camera
            self._symmetry_camera_state = {
                "shared": bool(self.camera_shared.value),
                "transform": camera.transform.copy(),
                "orthographic": camera.orthographic,
            }
            if self.camera_shared.value:
                self.camera_shared.value = False

            self.mesh = self._symmetry_expanded_mesh
            self.region_or_mesh = self._symmetry_expanded_mesh
            self.cf = self._symmetry_expanded_field
            self.visualization_cf = visualization_cf(self.cf)
            self.deformation = None
        else:
            original = self._symmetry_original_state
            self.mesh = original["mesh"]
            self.region_or_mesh = original["region_or_mesh"]
            self.cf = original["cf"]
            self.visualization_cf = original["visualization_cf"]
            self.deformation = original["deformation"]

        self.region_state = None
        self.region_visibility = None
        self._full_range = None
        self._mesh_data = None
        self.mdata = None
        self._slice_bounds_cache = None
        self._facet_supported = bool(self.draw_vol) and self.mesh.dim in (2, 3)
        self.draw()
        self._rebuild_dimension_controls()

        if enabled:
            self.reset_camera()
        elif self._symmetry_camera_state is not None:
            camera_state = self._symmetry_camera_state
            if camera_state["shared"]:
                self.camera_shared.value = True
            else:
                camera = self.camera
                camera.transform = camera_state["transform"]
                camera.orthographic = camera_state["orthographic"]
                self.scene.render()
            self._symmetry_camera_state = None

    def _rebuild_dimension_controls(self):
        """Rebuild viewport controls whose availability depends on mesh dimension."""
        self._tool_dock = self._build_tool_dock()
        self._clip_toolbar = self._build_clip_toolbar()
        overlays = [self.wgpu, self._tool_dock, self.pick_overlay]
        if self._clip_toolbar is not None:
            overlays.insert(2, self._clip_toolbar)
        if self._legend is not None:
            overlays.append(self._legend)
        if self._probe_panel is not None:
            overlays.extend((self._probe_preview, self._probe_panel))
        self._sync_viewport_layout(overlays)
        self._sync_clip_ui(self.clipping_enabled.value, None)
        self._sync_camera_link_ui(self.camera_shared.value, None)

    def _sync_region_state(self):
        self.region_state.hidden = set(self.hidden_regions.value)
        self.region_state.overrides = dict(self.boundary_overrides.value)

    def _apply_region_change(self, _val=None, _old=None):
        """Push the current region state to the GPU alpha buffer and re-render.

        The alpha buffer write is a compute-pass trigger for the clip fill and
        the arrow generators; the LIC field texture is dispatched Python-side,
        so poke it explicitly. With autoscale on, the colormap range follows
        the visible regions only.
        """
        self._sync_region_state()
        st = self.region_state
        self.region_visibility.set_alphas(
            vol=st.vol_alphas(), surf=st.surf_alphas()
        )
        if self.lic is not None:
            self.lic._refresh()
        self._update_autoscale_range()
        self.wgpu.scene.render()

    def _push_region_undo(self, label):
        prev_hidden = list(self.hidden_regions.value)
        prev_overrides = dict(self.boundary_overrides.value)

        def restore():
            with observable_batch():
                self.hidden_regions.value = prev_hidden
                self.boundary_overrides.value = prev_overrides

        self.undo_stack.push(label, restore)

    def set_region_visible(self, name, visible):
        hidden = set(self.hidden_regions.value)
        if (name not in hidden) == bool(visible):
            return
        self._push_region_undo(("show " if visible else "hide ") + name)
        if visible:
            hidden.discard(name)
        else:
            hidden.add(name)
        self.hidden_regions.value = sorted(hidden)

    def set_boundary_override(self, name, state):
        """state: True = force show, False = force hide, None = auto."""
        overrides = dict(self.boundary_overrides.value)
        if overrides.get(name) == state:
            return
        word = "auto" if state is None else ("show" if state else "hide")
        self._push_region_undo(f"boundary {name}: {word}")
        if state is None:
            overrides.pop(name, None)
        else:
            overrides[name] = state
        self.boundary_overrides.value = overrides

    def show_all_regions(self):
        if not self.hidden_regions.value and not self.boundary_overrides.value:
            return
        self._push_region_undo("show all")
        with observable_batch():
            self.hidden_regions.value = []
            self.boundary_overrides.value = {}

    def _region_under_cursor(self):
        """Volume region name under the cursor (via the last hover pick)."""
        result = self._last_pick
        if result is None:
            return None
        st = self.region_state
        if self.mesh.dim == 2 or result.kind == "clipping":
            return st.material_name(result.region_index)
        if result.kind == "surface":
            # Boundary pick: resolve through the fd adjacency, preferring a
            # currently visible adjacent volume region (hiding the visible one
            # is what "hide what I see" means on an interface).
            if result.region_index >= len(st.fd_doms):
                return None
            doms = st.fd_doms[result.region_index]
            names = [st.material_name(d - 1) for d in doms if d >= 1]
            names = [n for n in names if n is not None]
            visible = [n for n in names if st.material_visible(n)]
            return (visible or names or [None])[0]
        return None

    def hide_region_under_cursor(self):
        name = self._region_under_cursor()
        if name is not None:
            self.set_region_visible(name, False)

    def isolate_region_under_cursor(self):
        name = self._region_under_cursor()
        if name is None:
            return
        others = sorted(m for m in self.region_state.unique_materials if m != name)
        if others == sorted(self.hidden_regions.value):
            return
        self._push_region_undo(f"isolate {name}")
        self.hidden_regions.value = others

    # -- Autoscale over visible regions --------------------------------------

    def _update_autoscale_range(self):
        """Scope the function range to the visible regions.

        The renderers re-widen the colormap from ``func_data.minval/maxval``
        every frame when autoscale is on, so overriding those (and restoring
        the cached full range when nothing is hidden) is all it takes.
        """
        func_data = getattr(self, "func_data", None)
        if func_data is None:
            return
        if self._full_range is None and func_data._timestamp >= 0:
            self._full_range = (list(func_data.minval), list(func_data.maxval))
        if not self.region_state.any_hidden():
            if self._full_range is not None:
                func_data.minval, func_data.maxval = (
                    list(self._full_range[0]), list(self._full_range[1]))
        else:
            mm = self._visible_minmax()
            if mm is not None:
                func_data.minval, func_data.maxval = mm
        if self.colormap_autoscale.value:
            self.wgpu.scene.redraw(blocking=True)
            self.colormap_min.value = float(self.colormap.minval)
            self.colormap_max.value = float(self.colormap.maxval)

    def _visible_minmax(self):
        """(minval, maxval) lists ([norm, comp0, ...]) over the visible
        regions, evaluated like ``evaluate_cf`` does; None if not computable."""
        import numpy as np
        import re

        st = self.region_state
        visible_mats = [m for m in st.unique_materials if st.material_visible(m)]
        if not visible_mats:
            return None
        regions = []
        mat_pattern = "|".join(re.escape(m) for m in visible_mats)
        if self.mesh.dim == 3:
            if self.clippingcf is not None and self.clipping_visible.value:
                regions.append((self.mesh.Materials(mat_pattern),
                                (ngs.ET.TET, ngs.ET.HEX, ngs.ET.PRISM, ngs.ET.PYRAMID)))
            if self.elements2d is not None and self.elements2d_visible.value:
                bnd_names = st.visible_boundary_names()
                if bnd_names:
                    bnd_pattern = "|".join(re.escape(b) for b in bnd_names)
                    regions.append((self.mesh.Boundaries(bnd_pattern),
                                    (ngs.ET.TRIG, ngs.ET.QUAD)))
            if not regions:
                regions.append((self.mesh.Materials(mat_pattern),
                                (ngs.ET.TET, ngs.ET.HEX, ngs.ET.PRISM, ngs.ET.PYRAMID)))
        else:
            regions.append((self.mesh.Materials(mat_pattern),
                            (ngs.ET.TRIG, ngs.ET.QUAD)))

        order = max(2 * self.order, 2)
        minval = None
        maxval = None
        for region, eltypes in regions:
            try:
                rules = {et: ngs.IntegrationRule(et, order) for et in eltypes}
                with ngs.TaskManager():
                    pts = self.mesh.MapToAllElements(rules, region)
                    vals = self.cf(pts)
            except Exception:
                continue
            vals = np.asarray(vals).reshape(len(pts), -1)
            comps = np.abs(vals) if self.cf.is_complex else np.real(vals)
            if comps.size == 0:
                continue
            norm = np.linalg.norm(comps, axis=1)
            mn = [float(np.min(norm))] + [float(v) for v in np.min(comps, axis=0)]
            mx = [float(np.max(norm))] + [float(v) for v in np.max(comps, axis=0)]
            if minval is None:
                minval, maxval = mn, mx
            else:
                minval = [min(a, b) for a, b in zip(minval, mn)]
                maxval = [max(a, b) for a, b in zip(maxval, mx)]
        if minval is None:
            return None
        return minval, maxval

    # -- Keybinding support -------------------------------------------------

    _COLORMAPS = ["rainbow", "turbo", "viridis", "plasma", "cet_l20", "matlab:jet", "matplotlib:coolwarm"]

    def get_keybindings(self):
        kb = super().get_keybindings()
        kb["flat"].append(("w", self.toggle_wireframe, "Toggle wireframe", "General"))

        # s → Show
        show = [("w", self.toggle_wireframe, "Toggle wireframe")]
        if self.draw_surf:
            show.append(("s", self.toggle_surface_solution, "Toggle surface"))
        if self._facet_supported:
            show.append(("e", self.toggle_facet, "Toggle element boundaries"))
        if self.surface_vectors is not None:
            show.append(("v", self.toggle_surface_vectors, "Toggle surface vectors"))
        if self.clipping_vectors is not None:
            show.append(("c", self.toggle_clipping_vectors, "Toggle clipping vectors"))
        if self.lic is not None:
            show.append(("l", self.toggle_lic, "Toggle LIC"))
        if self.fieldlines is not None:
            show.append(("f", self.toggle_fieldlines, "Toggle field lines"))
        if self.surface_vectors is not None or self.clipping_vectors is not None:
            show.append(("+", self.increase_vector_density, "Increase vector density"))
            show.append(("-", self.decrease_vector_density, "Decrease vector density"))
        show += self._gizmo_show_bindings()
        kb["modes"].append(("s", "Show", show))

        # c → Clipping (3D only)
        if self.mesh.dim == 3:
            clip = list(self._clipping_mode_bindings())
            if self.clippingcf is not None:
                clip.append(
                    ("f", self.toggle_clipping_function, "Toggle clipping function")
                )
            if self.lic is not None:
                clip.append(("l", self.toggle_lic, "Toggle LIC"))
            kb["modes"].append(("c", "Clipping", clip))

        # d → Deformation
        if self.deformation is not None or (self.cf.dim == 1 and self.mesh.dim < 3):
            kb["modes"].append(
                (
                    "d",
                    "Deformation",
                    [
                        ("d", self.toggle_deformation, "Toggle deformation"),
                        ("+", self.increase_deformation, "Increase scale"),
                        ("-", self.decrease_deformation, "Decrease scale"),
                        ("0", self.reset_deformation, "Reset scale to 1.0"),
                    ],
                )
            )

        # m → Colormap
        kb["modes"].append(
            (
                "m",
                "Colormap",
                [
                    ("a", self.toggle_autoscale, "Toggle autoscale"),
                    ("d", self.toggle_discrete, "Toggle discrete"),
                    ("n", self.cycle_colormap_next, "Next colormap"),
                    ("p", self.cycle_colormap_prev, "Previous colormap"),
                ],
            )
        )

        if self.cf.is_complex:
            kb["modes"].append(
                (
                    "x",
                    "Complex",
                    [
                        ("r", lambda: setattr(self.complex_mode, 'value', 'real'), "Real part"),
                        ("i", lambda: setattr(self.complex_mode, 'value', 'imag'), "Imag part"),
                        ("a", lambda: setattr(self.complex_mode, 'value', 'abs'), "Absolute value"),
                        ("p", lambda: setattr(self.complex_mode, 'value', 'arg'), "Phase/Arg"),
                        ("space", lambda: self.complex_animate.toggle(), "Toggle animation"),
                    ],
                )
            )

        # g → Regions (hide/isolate what's under the cursor; u undoes)
        if len(self.region_state.unique_materials) > 1:
            kb["modes"].append(
                (
                    "g",
                    "Regions",
                    [
                        ("h", self.hide_region_under_cursor, "Hide region under cursor"),
                        ("i", self.isolate_region_under_cursor, "Isolate region under cursor"),
                        ("a", self.show_all_regions, "Show all regions"),
                    ],
                )
            )

        num_bindings = [
            ("v", lambda: self._toggle_numbers("vertices"), "Vertex numbers"),
            ("e", lambda: self._toggle_numbers("edges"), "Edge numbers"),
            ("f", lambda: self._toggle_numbers("facets"), "Facet numbers"),
            ("s", lambda: self._toggle_numbers("surface_elements"), "Surface el. numbers"),
        ]
        if self.mesh.dim == 3:
            num_bindings.append(("3", lambda: self._toggle_numbers("volume_elements"), "Volume el. numbers"))
        kb["modes"].append(("n", "Numbers", num_bindings))

        return kb

    # -- Toggle methods (now one-liners) ------------------------------------

    def toggle_wireframe(self):
        self.wireframe_visible.toggle()

    def toggle_surface_solution(self):
        self.elements2d_visible.toggle()

    def toggle_facet(self):
        self.facet_visible.toggle()

    def toggle_clipping_vectors(self):
        self.clipping_vectors_visible.toggle()

    def toggle_surface_vectors(self):
        self.surface_vectors_visible.toggle()

    def toggle_fieldlines(self):
        self.field_lines_visible.toggle()

    def toggle_clipping_function(self):
        self.clipping_visible.toggle()

    def toggle_lic(self):
        self.lic_visible.toggle()

    def _toggle_numbers(self, entity):
        getattr(self, f"{entity}_numbers_visible").toggle()

    def _change_vector_density(self, factor):
        grid_size = max(10, int(self.vector_grid_size.value * factor))
        self.vector_grid_size.value = grid_size

    def increase_vector_density(self):
        self._change_vector_density(1.25)

    def decrease_vector_density(self):
        self._change_vector_density(0.8)

    def toggle_deformation(self):
        self.deformation_enabled.toggle()

    def increase_deformation(self):
        self._step_deformation(1.25)

    def decrease_deformation(self):
        self._step_deformation(0.8)

    def _step_deformation(self, factor):
        if self.mdata is None:
            return
        self.deformation_scale.value = self.deformation_scale.value * factor

    def reset_deformation(self):
        if self.mdata is None:
            return
        with observable_batch():
            self.deformation_scale.value = 1.0
            self.deformation_scale2.value = 1.0

    def _apply_autoscale(self, val, _old):
        self.colormap.autoscale = val
        if val:
            self.wgpu.scene.redraw(blocking=True)
            self.colormap_min.value = float(self.colormap.minval)
            self.colormap_max.value = float(self.colormap.maxval)
        else:
            self.wgpu.scene.render()

    def _apply_discrete(self, val, _old):
        self.colormap.set_discrete(val)
        self.wgpu.scene.render()

    def _apply_colormap_name(self, val, _old):
        self.colormap.set_colormap(val)
        self.wgpu.scene.render()

    @property
    def _complex_renderers(self):
        return [
            r
            for r in [
                self.elements2d,
                self.clippingcf,
                self.slice_renderer,
                self.clipping_vectors,
                self.surface_vectors,
                self.lic,
            ]
            if r is not None
        ]

    @property
    def _vector_renderers(self):
        return [r for r in [self.clipping_vectors, self.surface_vectors] if r is not None]

    def _apply_complex_mode(self, val, _old):
        for r in self._complex_renderers:
            r.set_complex_mode(val)
        self.wgpu.scene.render()

    def _apply_complex_animate(self, val, _old):
        if val:
            if self.colormap_autoscale.value:
                self.colormap_autoscale.value = False
            for r in self._complex_renderers:
                r.animate_phase(self.scene, speed=self.complex_speed.value)
        else:
            for r in self._complex_renderers:
                r.stop_animation()
                r.set_complex_mode(self.complex_mode.value)
            self.wgpu.scene.render()

    def _apply_complex_speed(self, val, _old):
        for r in self._complex_renderers:
            if r._phase_animation is not None:
                r._phase_animation.speed = val

    def toggle_autoscale(self):
        self.colormap_autoscale.toggle()

    def toggle_discrete(self):
        self.colormap_discrete.toggle()

    def _cycle_colormap(self, direction):
        current = self.colormap_name.value
        try:
            idx = self._COLORMAPS.index(current)
        except ValueError:
            idx = 0
        idx = (idx + direction) % len(self._COLORMAPS)
        self.colormap_name.value = self._COLORMAPS[idx]

    def cycle_colormap_next(self):
        self._cycle_colormap(1)

    def cycle_colormap_prev(self):
        self._cycle_colormap(-1)

    def property_subtitle(self):
        kind = "Vector" if self.cf.dim > 1 else "Scalar"
        return f"Function · {self.mesh.dim}D · {kind}"

    def property_xref(self):
        return {"label": "Open as mesh", "icon": "mdi-vector-triangle",
                "callback": self._open_as_mesh}

    def _build_viewport_legend(self):
        # The colorbar (colormap, range, autoscale, component) lives entirely in
        # the in-viewport legend now — not in the side panel.
        from .prop_widgets import ColorbarLegend
        return ColorbarLegend(self)

    def _open_as_mesh(self):
        from .mesh import MeshComponent
        self.app_data.add_tab(
            "Mesh_" + self.name, MeshComponent, {"obj": self.mesh}, self.app_data
        )

    def draw(self):
        # Per-tab region visibility: shared alpha buffer (like clipping /
        # colormap), fed from the saved hidden-regions state.
        if getattr(self, "region_state", None) is None:
            self.region_state = RegionState(self.mesh)
            self.region_visibility = RegionVisibility()
            self._full_range = None
        self._sync_region_state()
        self._sync_fieldline_seed_visibility(self.field_lines_visible.value)
        self.region_visibility.set_alphas(
            vol=self.region_state.vol_alphas(),
            surf=self.region_state.surf_alphas(),
        )

        func_data = self.app_data.get_function_gpu_data(
            self.visualization_cf, self.region_or_mesh, order=self.order
        )
        mdata = func_data.mesh_data

        if self.deformation is not None:
            deform_data = self.app_data.get_function_gpu_data(
                self.deformation, self.region_or_mesh,
                order=self.deformation_order
            )
            if getattr(self, "mdata", None) is None:
                self.mdata = MeshData(self.region_or_mesh)
            mdata = self.mdata
            mdata.deformation_data = deform_data
            mdata.deformation_scale = (
                self.deformation_scale.value * self.deformation_scale2.value
            )
            if not self.deformation_enabled.value:
                mdata.deformation_scale = 0.0
            func_data.mesh_data = mdata
        subdiv = self._subdivision_override()
        if subdiv is not None:
            mdata.subdivision = subdiv
        self.wireframe = MeshWireframe2d(mdata, clipping=self.clipping)
        # geometry edges (1d mesh elements)
        self.edges = None
        if self.draw_edges:
            self.edges = MeshSegments(mdata, clipping=self.clipping)
            # edges=<float> sets the line thickness
            if not isinstance(self.draw_edges, bool):
                self.edges.thickness = float(self.draw_edges)
        self.wireframe.active = self.wireframe_visible.value

        autoscale = self.colormap_autoscale.value
        discrete = self.colormap_discrete.value
        minval = self.colormap_min.value
        maxval = self.colormap_max.value
        self.colormap = _MMFEMColormap(
            minval=minval, maxval=maxval, colormap=self.colormap_name.value
        )
        self.colormap.autoscale = autoscale
        self.colormap.discrete = discrete
        self.clipping_vectors = None
        self.lic = None
        # True when self.lic is a SurfaceLIC (2D) that REPLACES the flat surface
        # field, vs a ClippingLIC (3D) that overlays the cutting plane.
        self._lic_is_surface = False
        if self.cf.dim == self.mesh.dim:
            vec3 = self.visualization_cf
            if self.cf.dim == 2:
                vec3 = ngs.CF((self.visualization_cf[0], self.visualization_cf[1], 0))
            vec_data = self.app_data.get_function_gpu_data(
                vec3, self.region_or_mesh, order=self.order
            )
            self.surface_vectors = SurfaceVectors(
                vec_data,
                clipping=self.clipping,
                colormap=self.colormap,
                grid_size=self.vector_grid_size.value,
                scale_by_value=self.vector_scale_by_value.value,
            )
            self.surface_vectors.user_scale = self.vector_scale.value
            self.surface_vectors.active = self.surface_vectors_visible.value
            # Surface LIC: paint the 2D vector field's flow as streamlines on the
            # mesh itself (replaces the flat surface field, like the 3D LIC
            # replaces the clip-plane field). Only for a 2D vector field on a 2D
            # mesh; the 3D case below uses ClippingLIC on the cutting plane.
            if self.mesh.dim == 2:
                self.lic = SurfaceLIC(
                    vec_data,
                    clipping=self.clipping,
                    colormap=self.colormap,
                    kernel_length=self.lic_kernel_length.value,
                    oriented=self.lic_oriented.value,
                    thickness=self.lic_thickness.value,
                    contrast=self.lic_contrast.value,
                    supersample=2 if self.lic_supersample.value else 1,
                )
                self.lic.active = self.lic_visible.value
                self._lic_is_surface = True
        else:
            self.surface_vectors = None
        self.fieldlines = None
        if self.cf.dim == self.mesh.dim:
            from ngsolve_webgpu.cf import FieldLines

            vec3 = self.cf if self.cf.dim == 3 else ngs.CF((self.cf[0], self.cf[1], 0))
            self.fieldlines = FieldLines(
                vec3,
                self.region_or_mesh,
                num_lines=self.fieldlines_num_lines.value,
                length=self.fieldlines_length.value,
                thickness=self.fieldlines_thickness.value,
                direction=self.fieldlines_direction.value,
                colormap=self.colormap,
                clipping=self.clipping,
            )
            seed_region = self.fieldline_seed_region.value
            self.fieldlines.start_region = material_region(self.mesh, seed_region)
            self.fieldlines._ngsolve_gui_start_region_name = seed_region
            draw_data = self.data if isinstance(self.data, dict) else {}
            if self.mesh.dim == 3 and draw_data.get(
                "_ngsolve_gui_fast_fieldlines", False
            ):
                from .fast_fieldlines import install_fast_fieldline_update

                install_fast_fieldline_update(self.fieldlines)
            self.fieldlines.active = self.field_lines_visible.value
        if self.mesh.dim == 3 and self.draw_vol:
            self.clippingcf = ClippingIsolineRenderer(func_data, clipping=self.clipping,
                                                     n_lines=0, show_field=True, colormap=self.colormap)
            self.clippingcf.active = self.clipping_visible.value
            if self.cf.dim == 3:
                self.clipping_vectors = ClippingVectors(
                    func_data,
                    clipping=self.clipping,
                    colormap=self.colormap,
                    grid_size=self.vector_grid_size.value,
                    scale_by_value=self.vector_scale_by_value.value,
                )
                self.clipping_vectors.user_scale = self.vector_scale.value
                self.clipping_vectors.active = self.clipping_vectors_visible.value
                self.lic = ClippingLIC(
                    func_data,
                    clipping=self.clipping,
                    colormap=self.colormap,
                    kernel_length=self.lic_kernel_length.value,
                    oriented=self.lic_oriented.value,
                    thickness=self.lic_thickness.value,
                    contrast=self.lic_contrast.value,
                    supersample=2 if self.lic_supersample.value else 1,
                )
                self.lic.active = self.lic_visible.value
        else:
            self.clippingcf = None
        self.slice_renderer = self._create_slice_renderer()
        if self.draw_surf:
            self.elements2d = IsolineRenderer(
                func_data, n_lines=0, show_field=True,
                clipping=self.clipping, colormap=self.colormap
            )
            self.elements2d.active = self.elements2d_visible.value
            # Hide the flat field if a surface LIC is replacing it.
            self._sync_surface_elements()
        else:
            self.elements2d = None

        self.facet_renderer = None
        self._mesh_data = mdata
        if self.facet_visible.value and self._create_facet_renderer() is not None:
            self.facet_renderer.active = True
        if self.cf.is_complex:
            for r in self._complex_renderers:
                r._scene = self.scene
                r.set_complex_mode(self.complex_mode.value)
        self.colorbar = Colorbar(self.colormap)
        self.colorbar.width = 0.8
        self.colorbar.position = (-0.5, 0.9)

        if self.contact is not None:
            from ngsolve_webgpu.contact import ContactPairs
            from webgpu.renderer import MultipleRenderer

            if isinstance(self.contact, list):
                from .region_colors import get_random_colors

                colors = get_random_colors(len(self.contact))
                self.contact_pairs = MultipleRenderer(
                    [
                        ContactPairs(self.region_or_mesh, cb, color=c)
                        for cb, c in zip(self.contact, colors)
                    ]
                )
            else:
                self.contact_pairs = ContactPairs(
                    self.region_or_mesh,
                    self.contact,
                )
            self.contact_pairs.active = self.contact_enabled.value

        for r in [self.wireframe, self.elements2d, self.clippingcf,
                  self.clipping_vectors, self.surface_vectors, self.lic]:
            if r is not None:
                r.region_visibility = self.region_visibility

        render_objects = [
            obj
            for obj in [
                self.clippingcf,
                self.slice_renderer,
                self.lic,
                self.elements2d,
                self.facet_renderer,
                self.wireframe,
                self.edges,
                # colorbar is shown in the UI (FieldSummary), not in the scene
                self.contact_pairs,
                self.clipping_vectors,
                self.surface_vectors,
                self.fieldlines,
                self.coordinate_axes,
                self.navigation_cube,
            ]
            if obj is not None
        ]
        self._entity_number_renderers = {}
        for entity in self.entity_number_entities:
            r = EntityNumbers(mdata, entity=entity, clipping=self.clipping, zero_based=not self.numbers_one_based.value)
            r.active = getattr(self, f"{entity}_numbers_visible").value
            self._entity_number_renderers[entity] = r
        render_objects += list(self._entity_number_renderers.values())
        self.wgpu.draw(render_objects, camera=self.camera)
        self._sync_slice_position_overlay()

        pickable = [(r, k) for r, k in [
            (self.elements2d, "surface"),
            # The surface LIC replaces elements2d while visible, so keep picking
            # working on it too (it's a CFRenderer with a select pipeline).
            (self.lic if self._lic_is_surface else None, "surface"),
            (self.clippingcf, "clipping"),
            (self.slice_renderer, "clipping"),
        ] if r is not None]
        self.setup_picking(pickable, self.mesh)

        def set_min_max():
            self.colormap_min.value = float(self.colormap.minval)
            self.colormap_max.value = float(self.colormap.maxval)

        self.wgpu.on_mounted(set_min_max)

        self.func_data = func_data


# Register with the component registry
from .registry import register_component
from .sections import (
    FunctionDisplaySection,
    ClippingSection,
    DeformationSection,
    VectorsFlowSection,
    ComplexSection,
    EntityNumbersSection,
    RegionsSection,
    SliceViewSection,
)

# Colormap/colorbar lives in the always-visible FieldSummary (property_summary),
# not as a section — matching the designer's function panel. LIC is folded into
# VectorsFlowSection (grouped with streamlines), so it has no section of its own.
FunctionComponent.property_sections = [
    FunctionDisplaySection,
    RegionsSection,
    DeformationSection,
    VectorsFlowSection,
    ComplexSection,
    EntityNumbersSection,
]

register_component(
    "function",
    icon="mdi-function-variant",
    component_class=FunctionComponent,
)
