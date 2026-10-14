import json
from unittest.mock import patch

import pytest

from artemis.graph.checkpoints import (
    RunOutcome,
    TestSummary as OutcomeSummary,
    compute_run_outcome,
    read_run_outcome,
    write_run_outcome,
)
from artemis.utils.plan_grammar import parse_plan


def test_outcome_replaces_atomically_and_preserves_old_file_until_rename(tmp_path):
    path = tmp_path / "run_outcome.json"
    path.write_text('{"task_status":"partial"}')
    from pathlib import Path

    original_replace = Path.replace
    replacements = []

    def inspect_replace(source, target):
        assert json.loads(path.read_text()) == {"task_status": "partial"}
        assert source != path
        assert source.parent == path.parent
        assert json.loads(source.read_text())["task_status"] == "completed"
        replacements.append(target)
        return original_replace(source, target)

    with patch.object(Path, "replace", inspect_replace):
        write_run_outcome(tmp_path, RunOutcome(task_status="completed", tests=OutcomeSummary()))
    assert replacements == [path]
    assert read_run_outcome(tmp_path)["task_status"] == "completed"
    assert list(tmp_path.iterdir()) == [path]


def test_failed_atomic_write_raises_and_cleans_temp_file(tmp_path):
    path = tmp_path / "run_outcome.json"
    path.write_text('{"task_status":"partial"}')
    with patch("pathlib.Path.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            write_run_outcome(tmp_path, RunOutcome(task_status="completed", tests=OutcomeSummary()))
    assert read_run_outcome(tmp_path) == {"task_status": "partial"}
    assert list(tmp_path.iterdir()) == [path]


def test_pro_outcome_preserves_raw_checker_items_and_failure_anchor():
    snapshot = parse_plan("- [x] Open settings\n  - assert: The title is visible")
    records = [
        {
            "kind": "assert",
            "item_text": "The title is visible",
            "status": "failed",
            "anchor_step_id": "step-3",
            "evidence": "Wrong title",
        },
        {
            "kind": "check",
            "item_text": "The page is open",
            "status": "passed",
            "anchor_step_id": "step-2",
            "evidence": "Page visible",
        },
    ]
    outcome = compute_run_outcome(snapshot, records, verify_blocked=False).model_dump()
    assert outcome["checker_items"] == records
    assert outcome["tests"]["failed_items"][0]["anchor_step_id"] == "step-3"
    assert "verdict" not in outcome
