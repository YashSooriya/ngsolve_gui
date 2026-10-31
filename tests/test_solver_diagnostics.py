import subprocess

from ngsolve_gui.app import _reap_solver_process, _solver_process_failure_detail


def test_solver_failure_message_retains_windows_native_status_and_log_path():
    detail = _solver_process_failure_detail(0xC0000409)

    assert "3221226505" in detail
    assert "0xC0000409" in detail
    assert "solver.log" in detail


def test_solver_process_cleanup_kills_and_reaps_a_child_that_ignores_terminate():
    class FakeProcess:
        def __init__(self):
            self.calls = []
            self.killed = False

        def poll(self):
            self.calls.append("poll")
            return 0 if self.killed else None

        def terminate(self):
            self.calls.append("terminate")

        def wait(self, timeout=None):
            self.calls.append(("wait", timeout))
            if not self.killed:
                raise subprocess.TimeoutExpired("solver", timeout)
            return 0

        def kill(self):
            self.calls.append("kill")
            self.killed = True

    process = FakeProcess()
    assert _reap_solver_process(process) is True

    assert "terminate" in process.calls
    assert "kill" in process.calls
    assert process.calls.count(("wait", 5)) == 2


def test_solver_process_cleanup_reports_a_child_that_survives_kill():
    class StuckProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("solver", timeout)

    assert _reap_solver_process(StuckProcess()) is False
