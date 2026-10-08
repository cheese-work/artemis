"""Start the isolated preview with the device-free demo board (docs/preview-fixtures.md).

    uv run python scripts/preview_demo.py [--port 8000]

Fixture data lives in a fresh ``.preview-demo/run-*`` directory, removed on exit. Select an
identity with ``X-Artemis-Preview-Identity: qa-a|qa-b|admin``.
"""

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "ARTEMIS_PREVIEW_PROFILE": "1",
    "ARTEMIS_PREVIEW_IDENTITY_SWITCH": "1",
    "ARTEMIS_PREVIEW_DEMO": "1",
    "ARTEMIS_PREVIEW_JWKS_BUNDLE": "",
    "ARTEMIS_AUTH_MODE": "cloudflare",
    "ARTEMIS_CF_ACCESS_TEAM_DOMAIN": "demo.cloudflareaccess.com",
    "ARTEMIS_CF_ACCESS_AUD": "demo-audience",
    "ARTEMIS_PREVIEW_QA_EMAILS": "qa-a@example.test,qa-b@example.test",
    "ARTEMIS_ADMIN_EMAILS": "admin@example.test",
    "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    port = parser.parse_args().port
    # Inside the checkout: the media route serves recordings only under the workspace root.
    scratch = ROOT / ".preview-demo"
    scratch.mkdir(exist_ok=True)
    parent = tempfile.mkdtemp(prefix="run-", dir=scratch)
    os.environ.update(ENV, ARTEMIS_APP_DIR=parent, TMPDIR=parent)
    sys.path.insert(0, str(ROOT))
    import uvicorn

    print(
        f"Demo board: http://127.0.0.1:{port}/api/devices (header X-Artemis-Preview-Identity: admin)"
    )
    try:
        uvicorn.run("apps.admin_console.server:app", host="127.0.0.1", port=port)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


if __name__ == "__main__":
    main()
