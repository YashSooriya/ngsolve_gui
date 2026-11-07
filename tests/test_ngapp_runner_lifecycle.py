"""Regression tests for cleanup around ngapp's in-process test runner."""

import pytest

from ngapp.cli import serve_standalone
from ngapp.e2e import LocalAppRunner


def test_in_process_start_timeout_stops_the_host_thread(monkeypatch):
    def wait_until_stopped(*_args, stop_event=None, **_kwargs):
        assert stop_event is not None
        stop_event.wait(timeout=5)

    monkeypatch.setattr(
        serve_standalone, "host_local_app", wait_until_stopped
    )
    runner = LocalAppRunner("ngsolve_gui.appconfig", timeout=0.1)

    with pytest.raises(TimeoutError, match="Timed out waiting for in-process app URL"):
        runner.start(in_process=True)

    # tests/conftest wraps start/stop to guarantee this timeout path joins the
    # thread instead of leaving an ngapp host running into the next test.
    assert runner._thread is None
    assert runner._stop_event is None
