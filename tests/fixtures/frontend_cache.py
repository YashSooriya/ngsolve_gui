"""Tiny test-only substitute for ngapp's optional frontend_cache import.

The UI runtime uses this helper only to set HTTP cache headers. The pinned
frontend archive is already cached for tests, so no cache policy is required
for rendering or WebGPU/WebGL behavior here.
"""


def cache_control_for_path(_path):
    return "no-cache, no-store, must-revalidate"
