from pathlib import Path


def test_linux_portable_scrcpy_fallback_sets_resolver_override():
    repository_root = Path(__file__).resolve().parents[2]
    startup_script = (repository_root / "start.sh").read_text(encoding="utf-8")
    fallback_start = startup_script.index("# Fallback: if scrcpy is still missing")
    fallback_end = startup_script.index("# Fallback: if adb is missing", fallback_start)
    scrcpy_fallback = startup_script[fallback_start:fallback_end]

    assert (
        scrcpy_fallback.count(
            'export ARTEMIS_SCRCPY_PATH="${ARTEMIS_SCRCPY_PATH:-${SCRCPY_DIR}/scrcpy}"'
        )
        == 2
    )
