"""Coefficient-function adapters used only by the result renderer."""

import ngsolve as ngs


def visualization_cf(cf):
    """Return full volume-side values when rendering H(div) GridFunctions.

    H(div) fields expose a normal trace on boundary facets. Evaluating the raw
    vector GridFunction on a boundary therefore makes its tangential
    components appear to be zero, even when the adjacent volume solution is
    nonzero. The pick overlay continues to use the original GridFunction;
    BoundaryFromVolumeCF gives the surface renderer the corresponding
    one-sided volume value.
    """
    if (
        isinstance(cf, ngs.GridFunction)
        and str(getattr(cf.space, "type", "")).lower().startswith("hdiv")
    ):
        return ngs.BoundaryFromVolumeCF(cf)
    return cf
