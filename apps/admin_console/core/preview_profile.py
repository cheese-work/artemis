"""Preview runtime profile selection (CHE-1288).

A per-PR preview runs the real FastAPI app against synthetic fixtures, so the
lifecycle hooks that touch devices, processes, host state or live data must not
run. The profile is chosen from the environment before the server's other
imports, because some of those effects (the language-server address write) run
at import time. This module stays stdlib-only so it is safe to load first.
"""

from collections.abc import Mapping, MutableMapping
import os
from pathlib import Path
import tempfile

ENV_PREVIEW_PROFILE = "ARTEMIS_PREVIEW_PROFILE"
ENV_PREVIEW_IDENTITY_SWITCH = "ARTEMIS_PREVIEW_IDENTITY_SWITCH"
ENV_PREVIEW_DEMO = "ARTEMIS_PREVIEW_DEMO"

_TRUTHY = {"1", "true"}


def preview_profile_selected(environ: Mapping[str, str] = os.environ) -> bool:
    """True only when the environment explicitly selects the preview profile."""
    return environ.get(ENV_PREVIEW_PROFILE, "").strip().lower() in _TRUTHY


def preview_identity_switch_selected(
    preview_profile: bool, environ: Mapping[str, str] = os.environ
) -> bool:
    value = environ.get(ENV_PREVIEW_IDENTITY_SWITCH, "").strip().lower()
    if value not in {"", "0", "false", *_TRUTHY}:
        raise ValueError("ARTEMIS_PREVIEW_IDENTITY_SWITCH must be 0, false, 1 or true.")
    enabled = value in _TRUTHY
    if enabled and not preview_profile:
        raise ValueError("The identity switch requires the isolated preview profile.")
    return enabled


def preview_demo_selected(preview_profile: bool, environ: Mapping[str, str] = os.environ) -> bool:
    value = environ.get(ENV_PREVIEW_DEMO, "").strip().lower()
    if value not in {"", "0", "false", *_TRUTHY}:
        raise ValueError("ARTEMIS_PREVIEW_DEMO must be 0, false, 1 or true.")
    enabled = value in _TRUTHY
    if enabled and not preview_profile:
        raise ValueError("The demo board requires the isolated preview profile.")
    return enabled


def prepare_preview_environment(environ: MutableMapping[str, str] = os.environ) -> Path:
    parent = environ.get("ARTEMIS_APP_DIR", "")
    if not parent or not Path(parent).is_absolute():
        raise ValueError("A preview requires an explicit absolute ARTEMIS_APP_DIR fixture parent.")
    Path(parent).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="preview-", dir=parent))
    traces = root / "traces"
    environ["ARTEMIS_APP_DIR"] = str(root)
    environ["ARTEMIS_TRACES_DIR"] = str(traces)
    environ["TRACES_PATH"] = str(traces)
    environ["DATA_ENGINE_DB_PATH"] = str(traces / "data_engine.db")
    return root
