"""Preview runtime profile selection (CHE-1288).

A per-PR preview runs the real FastAPI app against synthetic fixtures, so the
lifecycle hooks that touch devices, processes, host state or live data must not
run. The profile is chosen from the environment before the server's other
imports, because some of those effects (the language-server address write) run
at import time. This module stays stdlib-only so it is safe to load first.
"""

from collections.abc import Mapping
import os

ENV_PREVIEW_PROFILE = "ARTEMIS_PREVIEW_PROFILE"

_TRUTHY = {"1", "true"}


def preview_profile_selected(environ: Mapping[str, str] = os.environ) -> bool:
    """True only when the environment explicitly selects the preview profile."""
    return environ.get(ENV_PREVIEW_PROFILE, "").strip().lower() in _TRUTHY
