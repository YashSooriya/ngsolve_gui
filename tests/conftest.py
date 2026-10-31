import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

import psutil
import pytest
from ngapp.e2e import LocalAppRunner

os.environ.setdefault("NGAPP_DEFAULT_COLORMAP", "matlab:jet")

# Hermetic user settings: ngapp resolves its config dir via
# ``platformdirs.user_config_dir("ngapp")``, which honours ``XDG_CONFIG_HOME``.
# Point it at a throwaway directory so a developer's real
# ``~/.config/ngapp/NGSolve_GUI/config.json`` (discrete colormap, panel widths,
# theme, navcube visibility, ...) cannot bleed into the rendered baselines and
# make local runs diverge from CI.
_SESSION_TEST_CONFIG = tempfile.mkdtemp(prefix="ngsolve_gui_session_cfg_")
os.environ["XDG_CONFIG_HOME"] = _SESSION_TEST_CONFIG

pytest_plugins = ["ngapp.e2e_webgpu"]

TESTS_DIR = Path(__file__).parent
_ACTIVE_TEST_PAGES = []
PLOTLY_TEST_BUNDLE = TESTS_DIR / "vendor" / "plotly-2.35.2.min.js"
PLOTLY_CDN_URL = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


def pytest_configure(config):
    import ngapp.e2e_webgpu as e2e_webgpu

    e2e_webgpu.configure(
        output_dir=TESTS_DIR / "output",
        baseline_dir=TESTS_DIR / "baselines",
    )


@pytest.fixture
def browser():
    """Fresh browser process and context per test.

    WebGPU's native Instance is process-bound. A page or context reset does
    not guarantee that Chrome has discarded it, so each test gets a fresh
    process as well as a fresh app and context.
    """
    from playwright.sync_api import sync_playwright
    from ngapp.e2e_webgpu import CHROMIUM_WEBGPU_ARGS

    pw = sync_playwright().start()
    b = pw.chromium.launch(
        channel="chrome",
        headless=False,
        args=["--headless=new"] + CHROMIUM_WEBGPU_ARGS,
    )
    try:
        yield b
    finally:
        try:
            b.close()
        finally:
            pw.stop()


@pytest.fixture
def page(browser):
    """Fresh WebGPU page, registered so app_test can disconnect before stop."""
    from ngapp.e2e_webgpu import (
        _OFFSCREEN_CANVAS_JS,
        _READBACK_JS,
        _initialised_scenes,
    )

    # Use the desktop-sized viewport used when the WebGPU baselines were
    # captured. Some expanded workspaces reduce the canvas height further.
    current_page = browser.new_page(viewport={"width": 1441, "height": 864})
    current_page.add_init_script(_OFFSCREEN_CANVAS_JS)
    current_page.add_init_script(_READBACK_JS)
    current_page.route(
        PLOTLY_CDN_URL,
        lambda route: route.fulfill(
            path=str(PLOTLY_TEST_BUNDLE),
            content_type="application/javascript; charset=utf-8",
        ),
    )
    _ACTIVE_TEST_PAGES.append(current_page)
    try:
        yield current_page
    finally:
        if current_page in _ACTIVE_TEST_PAGES:
            _ACTIVE_TEST_PAGES.remove(current_page)
        _initialised_scenes.clear()
        if not current_page.is_closed():
            current_page.close()


# ngapp's PlotlyComponent caches the plotly.js load state on the *class*
# (``_state``/``_plotly``/``_waiting``) so the library only loads once per app.
# That cache leaks across tests: each test gets a fresh browser but the same
# Python process, so after the first plot test ``_state`` stays ``"ready"`` and
# ``_plotly`` points at the first (now-closed) browser's ``window.Plotly``. The
# next plot test then skips loading the lib and draws through a dead proxy, so
# ``.js-plotly-plot`` never appears and the screenshot times out. Reset the
# class state before every test so each fresh browser loads plotly afresh.
@pytest.fixture(autouse=True)
def _isolate_ngapp_test_lifecycle(monkeypatch):
    """Give each UI test private app state and fail on leaked app workers.

    ngapp.e2e.app_test starts its in-process host before entering its cleanup
    try/finally. A startup timeout can therefore leave the host thread and its
    frontend subprocess alive. The wrappers below close that gap and verify
    both resources are gone before the next test starts.
    """
    from ngapp.components.visualization import PlotlyComponent

    def reset_plotly_state():
        # PlotlyComponent caches window.Plotly on the class. A fresh Chrome
        # process per test means that proxy must never cross test boundaries.
        PlotlyComponent._state = "none"
        PlotlyComponent._plotly = None
        PlotlyComponent._waiting = []

    reset_plotly_state()

    config_dir = tempfile.mkdtemp(prefix="ngsolve_gui_test_cfg_")
    monkeypatch.setenv("XDG_CONFIG_HOME", config_dir)

    # host_local_app defaults to 8765. A port per test avoids collisions with
    # a previous test, another pytest worker, or a separately running app.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        frontend_port = sock.getsockname()[1]
    monkeypatch.setenv("NGAPP_FRONTEND_PORT", str(frontend_port))

    runner_threads = []
    renderer_cleanup_failures = []
    original_start = LocalAppRunner.start
    original_stop = LocalAppRunner.stop

    def checked_stop(runner):
        thread = runner._thread
        process = runner._proc
        if thread is not None and thread not in runner_threads:
            runner_threads.append(thread)
        app = runner.app
        # Finish already-submitted GPU work before disposing the Python scene
        # proxies. Closing the page while Dawn still has queued mapAsync work
        # can leave an external WebGPU Instance reference invalid for the next
        # test's browser.
        for page in tuple(_ACTIVE_TEST_PAGES):
            try:
                if page.is_closed():
                    continue
                drain = page.evaluate("""async () => {
                    const devices = new Set();
                    for (const canvas of document.querySelectorAll('canvas')) {
                        let context;
                        try { context = canvas.getContext('webgpu'); }
                        catch (_) { continue; }
                        const device = context && context.__offscreenConfig
                            && context.__offscreenConfig.device;
                        if (device) devices.add(device);
                    }
                    const work = Promise.allSettled(
                        Array.from(devices, device => device.queue.onSubmittedWorkDone())
                    ).then(results => ({
                        devices: devices.size,
                        failures: results.filter(result => result.status === 'rejected').length,
                    }));
                    return Promise.race([
                        work,
                        new Promise(resolve => setTimeout(
                            () => resolve({devices: devices.size, timedOut: true}), 5000
                        )),
                    ]);
                }""")
                if drain.get("timedOut"):
                    renderer_cleanup_failures.append(
                        f"WebGPU queue drain timed out: {drain!r}"
                    )
                elif drain.get("failures"):
                    renderer_cleanup_failures.append(
                        f"WebGPU device rejected submitted work during teardown: {drain!r}"
                    )
            except Exception as error:
                renderer_cleanup_failures.append(
                    f"WebGPU queue drain failed: {type(error).__name__}: {error}"
                )
        if app is not None:
            from ngapp.components.visualization import WebgpuComponent

            components = list(
                getattr(
                    getattr(app, "context", None), "components_by_id", {}
                ).values()
            )
            # Some app test helpers retain the active visualization directly
            # on TabPanel.comp while swapping it outside the registered tree.
            tab_component = getattr(getattr(app, "tab_panel", None), "comp", None)
            components.extend(
                [tab_component, getattr(tab_component, "wgpu", None)]
            )
            seen_components = set()
            webgpu_components = []
            for component in components:
                if not isinstance(component, WebgpuComponent):
                    continue
                if id(component) in seen_components:
                    continue
                seen_components.add(id(component))
                webgpu_components.append(component)
                canvas = component.canvas
                resize_state = getattr(canvas, "_debounce_resize", None)
                scene = component.scene
                had_live_scene = scene is not None and scene.canvas is not None
                if had_live_scene:
                    # Mirror the frontend unmount event while the WebSocket is
                    # still live: dispose the JS render engine, unregister
                    # resize/input callbacks, and unconfigure the canvas before
                    # the host resets its WebGPU platform globals.
                    component._handle("unmount")
                    if component.scene is not None and component.scene.canvas is not None:
                        renderer_cleanup_failures.append(
                            f"WebGPU scene {getattr(component, '_id', None)!r} retained its canvas after unmount"
                        )
                    if component.canvas is not None and component.canvas.context is not None:
                        renderer_cleanup_failures.append(
                            f"WebGPU component {getattr(component, '_id', None)!r} retained a configured canvas"
                        )
                if resize_state is not None:
                    # Unmount disconnects the ResizeObserver. Drain its last
                    # debounced callback afterwards so it cannot race the
                    # host's WebGPU platform reset.
                    with resize_state._lock:
                        resize_timer = resize_state._timer
                        resize_state._pending = None
                        if resize_timer is not None:
                            resize_timer.cancel()
                            resize_state._timer = None
                    if resize_timer is not None and resize_timer.is_alive():
                        resize_timer.join(timeout=5)
                        if resize_timer.is_alive():
                            renderer_cleanup_failures.append(
                                "WebGPU canvas resize callback remained active during teardown"
                            )
        # app_test stops its host before pytest finalizes the page fixture.
        # Keep the browser connected for a short graceful window because
        # platform.js.close() uses that connection. The upstream runner waits
        # ten seconds before returning control here; if the synchronous
        # WebSocket close is stuck, that leaves no useful time to release it.
        if thread is not None and thread.is_alive():
            stop_event = runner._stop_event
            if stop_event is not None:
                stop_event.set()
            thread.join(timeout=2)
            if thread.is_alive():
                # A lost WebGPU device can prevent the synchronous close RPC
                # from completing. Disconnect the browser and stop the link
                # explicitly, then wait for host_local_app to unwind.
                for page in tuple(_ACTIVE_TEST_PAGES):
                    try:
                        if not page.is_closed():
                            page.close()
                    except Exception:
                        pass
                try:
                    from webgpu import platform as webgpu_platform

                    server = webgpu_platform.websocket_server
                    if server is not None:
                        # platform.init() blocks on this event before it can
                        # publish js. If frontend startup failed before the
                        # socket connected, setting the event lets its pending
                        # get() unwind when stop() releases outstanding RPCs.
                        # Without this the host thread can survive teardown.
                        server._event_is_connected.set()
                        server.stop()
                except Exception as error:
                    renderer_cleanup_failures.append(
                        f"WebGPU websocket shutdown failed: {type(error).__name__}: {error}"
                    )
                thread.join(timeout=5)
            if thread.is_alive():
                frame = sys._current_frames().get(thread.ident)
                stack = "".join(traceback.format_stack(frame)) if frame else "<stack unavailable>"
                raise RuntimeError(
                    "ngapp application host thread survived stop and WebGPU shutdown:\n"
                    + stack
                )
        # Clear runner fields only after its host has exited. Subprocess based
        # tests still use ngapp's regular stop implementation.
        original_stop(runner)
        from webgpu import platform as webgpu_platform, utils as webgpu_utils
        if (
            webgpu_platform.js is not None
            or webgpu_platform.websocket_server is not None
            or webgpu_utils._device is not None
        ):
            renderer_cleanup_failures.append(
                "WebGPU platform globals remained set after ngapp host shutdown"
            )
        # app_test returns before pytest finalizes the page fixture, so close
        # it as soon as the backend has shut down to dispose its WebGPU
        # context before the next app starts.
        for page in tuple(_ACTIVE_TEST_PAGES):
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def checked_start(runner, *args, **kwargs):
        try:
            result = original_start(runner, *args, **kwargs)
        except BaseException as startup_error:
            thread = runner._thread
            if thread is not None and thread not in runner_threads:
                runner_threads.append(thread)
            try:
                checked_stop(runner)
            except BaseException as cleanup_error:
                if hasattr(startup_error, "add_note"):
                    startup_error.add_note(
                        f"ngapp startup cleanup also failed: {cleanup_error}"
                    )
            raise
        thread = runner._thread
        if thread is not None and thread not in runner_threads:
            runner_threads.append(thread)
        return result

    monkeypatch.setattr(LocalAppRunner, "start", checked_start)
    monkeypatch.setattr(LocalAppRunner, "stop", checked_stop)

    baseline_pids = {
        process.pid
        for process in psutil.Process().children(recursive=True)
    }
    yield

    reset_plotly_state()
    shutil.rmtree(config_dir, ignore_errors=True)

    cleanup_errors = []
    cleanup_errors.extend(renderer_cleanup_failures)
    leaked_threads = [thread.name for thread in runner_threads if thread.is_alive()]
    if leaked_threads:
        cleanup_errors.append(f"ngapp test host threads were not stopped: {leaked_threads}")

    def owned_test_workers():
        workers = []
        for process in psutil.Process().children(recursive=True):
            if process.pid in baseline_pids:
                continue
            try:
                command_line = " ".join(process.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if any(
                marker in command_line
                for marker in (
                    "ngapp.cli.serve_frontend",
                    "run_gui_model.py",
                    "run_gui_3d_model.py",
                )
            ):
                workers.append((process, command_line))
        return workers

    deadline = time.monotonic() + 3
    workers = owned_test_workers()
    while workers and time.monotonic() < deadline:
        time.sleep(0.1)
        workers = owned_test_workers()
    if workers:
        details = []
        for process, command_line in workers:
            details.append(f"pid={process.pid}: {command_line}")
            try:
                process.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs([process for process, _ in workers], timeout=3)
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass
        _, still_alive = psutil.wait_procs(alive, timeout=3)
        if still_alive:
            details.extend(
                f"pid={process.pid}: still alive after kill"
                for process in still_alive
            )
        cleanup_errors.append("ngapp test left child workers alive; cleaned up: " + "; ".join(details))

    if cleanup_errors:
        pytest.fail("; ".join(cleanup_errors))


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_TEST_CONFIG, ignore_errors=True)
