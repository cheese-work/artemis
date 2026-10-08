"""Disposable archive check only; never access Docker or shared services."""

import os

import pytest

from scripts.preview_import import SandboxedPrecheck
from tests.unit.preview_host.test_import import image_archive

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("ARTEMIS_TEST_ARCHIVE_SANDBOX") != "1", reason="native namespace check is opt-in"
)
@pytest.mark.parametrize("tags", [None, ["real-site:latest"]])
def test_native_unprivileged_precheck(tmp_path, tags):
    payload, image_id = image_archive(tags=tags)
    path = tmp_path / "synthetic.tar"
    path.write_bytes(payload)
    with path.open("rb") as source:
        if tags:
            with pytest.raises(ValueError, match="import stopped"):
                SandboxedPrecheck()(source, image_id)
        else:
            assert SandboxedPrecheck()(source, image_id) == image_id
