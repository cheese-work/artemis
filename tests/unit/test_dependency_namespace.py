from pathlib import Path
import tomllib


def test_android_retry_namespace_has_one_locked_owner():
    project_root = Path(__file__).resolve().parents[2]
    lock = tomllib.loads((project_root / "uv.lock").read_text())
    namespace_owners = {
        package["name"] for package in lock["package"] if package["name"] in {"retry", "retry2"}
    }

    assert namespace_owners == {"retry2"}, (
        "Android dependencies require retry2; retry overwrites the same Python namespace"
    )
