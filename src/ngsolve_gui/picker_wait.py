"""Extend synchronous browser file-picker waits without slowing other UI requests.

The webgpu bridge uses a fixed 120-second Event.wait for synchronous browser
calls. A user can reasonably spend longer navigating to a file. This scoped
override applies only while a browser file picker call is waiting.
"""
from contextlib import contextmanager
import threading


BROWSER_PICKER_WAIT_SECONDS = 30 * 60
_picker_wait_state = threading.local()
_install_lock = threading.Lock()
_original_event_wait = None


def _picker_aware_event_wait(event, timeout=None):
    override = getattr(_picker_wait_state, "timeout", None)
    if override is not None:
        timeout = override if timeout is None else max(float(timeout), float(override))
    return _original_event_wait(event, timeout)


def _install_picker_aware_wait():
    global _original_event_wait
    with _install_lock:
        current = threading.Event.wait
        if getattr(current, "_mmfem_picker_aware", False):
            return
        _original_event_wait = current
        _picker_aware_event_wait._mmfem_picker_aware = True
        threading.Event.wait = _picker_aware_event_wait


@contextmanager
def browser_file_picker_wait(timeout_seconds=BROWSER_PICKER_WAIT_SECONDS):
    """Allow a user up to 30 minutes to complete a browser file selection."""
    _install_picker_aware_wait()
    previous = getattr(_picker_wait_state, "timeout", None)
    _picker_wait_state.timeout = max(120.0, float(timeout_seconds))
    try:
        yield
    finally:
        if previous is None:
            try:
                del _picker_wait_state.timeout
            except AttributeError:
                pass
        else:
            _picker_wait_state.timeout = previous
