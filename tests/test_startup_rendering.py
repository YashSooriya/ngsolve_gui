"""Focused checks for the one-shot native startup compositor pulse."""

from ngsolve_gui.app import _schedule_initial_renderer_frame_pulse


class _JavaScript:
    def __init__(self):
        self.source = None

    def eval(self, source):
        self.source = source


def test_startup_frame_pulse_is_transient_and_restores_inline_styles():
    js = _JavaScript()

    _schedule_initial_renderer_frame_pulse(js)

    assert "document.querySelector('#q-app')" in js.source
    assert "root.style.willChange = 'transform'" in js.source
    assert "translateZ(0)" in js.source
    assert "requestAnimationFrame(() => requestAnimationFrame(() =>" in js.source
    assert "root.style.transform = originalTransform" in js.source
    assert "root.style.willChange = originalWillChange" in js.source
    assert "mmfemStartupPaintPulse = 'complete'" in js.source


def test_startup_frame_pulse_is_idempotent_for_one_document():
    js = _JavaScript()

    _schedule_initial_renderer_frame_pulse(js)

    assert "root.dataset.mmfemStartupPaintPulse === 'pending'" in js.source
    assert "root.dataset.mmfemStartupPaintPulse === 'complete'" in js.source
