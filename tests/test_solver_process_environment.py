import os
from pathlib import Path

from ngsolve_gui.app import _solver_subprocess_environment


def test_solver_subprocess_environment_resolves_pythonpath_before_cwd_change(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(("src", "native_modules", str(tmp_path / "absolute"))),
    )

    environment = _solver_subprocess_environment()
    paths = [Path(value) for value in environment["PYTHONPATH"].split(os.pathsep)]

    assert paths[0] == Path(__file__).resolve().parents[1] / "src"
    assert paths[1:3] == [tmp_path / "src", tmp_path / "native_modules"]
    assert paths[3] == tmp_path / "absolute"
    assert all(path.is_absolute() for path in paths)
