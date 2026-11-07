"""Generate the small STEP shield fixture used by the 3D importer tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from realistic_sample import realistic_shield, make_golay_traces


def make_magnet_shield(path):
    """Write the realistic magnet's open-bore 4 K shield in STEP millimetres."""
    realistic_shield().Scale((0, 0, 0), 1000).WriteStep(str(path))
    return Path(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("magnet_shield_4k.step"),
    )
    parser.add_argument("--projects", action="store_true")
    args = parser.parse_args()
    path = args.path
    path.parent.mkdir(parents=True, exist_ok=True)
    make_magnet_shield(path)
    source = Path(__file__).resolve().parents[3] / "ngsolve-ui" / "coupled_solver" / "coils_curves" / "golay"
    scale = make_golay_traces(source, path.parent)
    if args.projects:
        from ngsolve_gui.three_d_project import add_step_geometry, add_wire_trace, new_3d_project, package_3d_project
        for axis in ("x", "y"):
            project = new_3d_project(f"Realistic magnet - {axis.upper()} Golay")
            assets = {}
            add_step_geometry(project, assets, path.name, path.read_bytes(), units="mm", material="4K_Shield")
            trace_path = path.with_name(f"golay_{axis}_trace.txt")
            trace = add_wire_trace(project, assets, trace_path.name, trace_path.read_bytes())
            trace["direction"] = axis
            path.with_name(f"Realistic-magnet-{axis.upper()}.mmfem3d").write_bytes(package_3d_project(project, assets))
    print(f"Created {path}; uniform Golay scale={scale:.8g}")


if __name__ == "__main__":
    main()
