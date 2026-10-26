"""Generate the small STEP shield fixture used by the 3D importer tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from netgen.occ import Cylinder, OCCGeometry, Z


def make_magnet_shield(path):
    """Write a 4 K shield: 3 mm wall and 10 mm end caps, in STEP millimetres."""
    outer = Cylinder((0, 0, -250), Z, r=243, h=500)
    cavity = Cylinder((0, 0, -240), Z, r=240, h=480)
    shield = (outer - cavity).mat("4K_Shield")
    OCCGeometry(shield).shape.WriteStep(str(path))
    return Path(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("magnet_shield_4k.step"),
    )
    path = parser.parse_args().path
    path.parent.mkdir(parents=True, exist_ok=True)
    make_magnet_shield(path)
    print(f"Created {path}")


if __name__ == "__main__":
    main()
