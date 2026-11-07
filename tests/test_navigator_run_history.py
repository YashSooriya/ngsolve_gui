from ngsolve_gui.navigator import Navigator


def test_refresh_expands_newest_run_and_hides_previous_summary():
    runs = [{
        "id": "first-run",
        "name": "First coupled run",
        "status": "Complete",
        "diagnostic_summary": "Converged · 2/12 iterations",
    }]
    navigator = type("RunHistoryState", (), {})()
    navigator._run_history = []
    navigator._run_collapsed = {}
    navigator._get_run_history = lambda: runs
    navigator.update = lambda: None

    Navigator.refresh_run_history(navigator)
    assert navigator._run_collapsed["first-run"] is False

    runs.append({
        "id": "second-run",
        "name": "Second coupled run",
        "status": "Nonconverged",
        "diagnostic_summary": (
            "Not converged · maximum iterations reached · diagnostic fields only"
        ),
    })
    Navigator.refresh_run_history(navigator)

    assert navigator._run_collapsed["first-run"] is True
    assert navigator._run_collapsed["second-run"] is False
    assert navigator._run_history[-1]["diagnostic_summary"] == (
        "Not converged · maximum iterations reached · diagnostic fields only"
    )
