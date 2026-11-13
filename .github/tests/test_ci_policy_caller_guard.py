"""Behaviour check for the ci-policy caller's trust routing (CHE-3275, D-6).

The caller must never skip: a skipped job is a non-failing check, so a PR that edits the policy
files would pass without the D-6 human approval. Untrusted heads (fork PRs, `upstream-sync/*`)
run on a GitHub-hosted runner instead of X99; trusted heads and queue runs stay on X99.
The `runs-on` expression is evaluated here against each event payload.
"""
import json
import re
import unittest
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci-policy.yml")
X99 = ["self-hosted", "Linux", "X64", "cheese-x99-gate"]
HOSTED = ["ubuntu-latest"]


class Ctx(dict):
    """Attribute access like a GitHub context: a missing property is null."""

    def __getattr__(self, name):
        value = self.get(name)
        return Ctx(value) if isinstance(value, dict) else value


def evaluate(expression: str, github: dict):
    """Evaluate the small expression subset the caller uses (&&, ||, !, startsWith, fromJSON)."""
    def starts_with(text, prefix):
        return isinstance(text, str) and text.lower().startswith(prefix.lower())

    python = re.sub(r"!(?!=)", " not ", expression)
    python = python.replace("&&", " and ").replace("||", " or ")
    python = re.sub(r"\bstartsWith\(", "starts_with(", python)
    python = re.sub(r"\bfromJSON\(", "from_json(", python)
    scope = {
        "github": Ctx(github),
        "starts_with": starts_with,
        "from_json": json.loads,
    }
    return eval(python, {"__builtins__": {}}, scope)  # noqa: S307 - expression from this repo's own workflow


def runs_on() -> str:
    text = WORKFLOW.read_text()
    job = text.split("\njobs:\n", 1)[1]
    match = re.search(r"(?m)^    runs-on: \$\{\{ (.+) \}\}$", job)
    assert match, "ci-policy job needs a runs-on expression"
    return match[1]


def pr_event(name, head_ref="feature/x", head_repo="cheese-work/repo", owner="cheese-work"):
    return {
        "repository": f"{owner}/repo",
        "repository_owner": owner,
        "event_name": name,
        "head_ref": head_ref if name == "pull_request_target" else "",
        "ref_name": "123/merge",
        "event": {"pull_request": {"head": {"ref": head_ref, "repo": {"full_name": head_repo}}}},
    }


class CiPolicyCallerGuardTest(unittest.TestCase):
    def test_job_is_never_skipped(self):
        job = WORKFLOW.read_text().split("\njobs:\n", 1)[1]
        self.assertIsNone(re.search(r"(?m)^    if:", job), "a skipped ci-policy is a non-failing check")

    def test_trusted_same_repo_pr_runs_on_x99(self):
        for name in ("pull_request_target", "pull_request_review"):
            with self.subTest(event=name):
                self.assertEqual(evaluate(runs_on(), pr_event(name)), X99)

    def test_upstream_sync_head_runs_hosted_on_target_and_review(self):
        for name in ("pull_request_target", "pull_request_review"):
            with self.subTest(event=name):
                event = pr_event(name, head_ref="upstream-sync/2026-11-13")
                self.assertEqual(evaluate(runs_on(), event), HOSTED)

    def test_fork_pr_runs_hosted_on_target_and_review(self):
        for name in ("pull_request_target", "pull_request_review"):
            with self.subTest(event=name):
                event = pr_event(name, head_repo="someone/repo")
                self.assertEqual(evaluate(runs_on(), event), HOSTED)

    def test_merge_group_runs_on_x99(self):
        event = {
            "repository": "cheese-work/repo",
            "repository_owner": "cheese-work",
            "event_name": "merge_group",
            "head_ref": "",
            "ref_name": "gh-readonly-queue/main/pr-1-abc",
            "event": {"merge_group": {"head_ref": "refs/heads/gh-readonly-queue/main/pr-1-abc"}},
        }
        self.assertEqual(evaluate(runs_on(), event), X99)

    def test_other_owner_stays_hosted(self):
        event = pr_event("pull_request_target", owner="someone")
        self.assertEqual(evaluate(runs_on(), event), HOSTED)

    def test_upstream_sync_branch_name_is_case_insensitive_prefix_only(self):
        # `startsWith` ignores case in GitHub expressions; a name merely containing the text is trusted.
        self.assertEqual(
            evaluate(runs_on(), pr_event("pull_request_target", head_ref="Upstream-Sync/x")), HOSTED
        )
        self.assertEqual(
            evaluate(runs_on(), pr_event("pull_request_target", head_ref="fix/upstream-sync/x")), X99
        )


if __name__ == "__main__":
    unittest.main()
