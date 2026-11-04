"""Opt-in CHE-984 D1 route and native assertion-exit contract."""

from artemis.config import load_agent_config
from artemis.config.llm import LLMConfig


def prepare_d1_config(config: LLMConfig) -> LLMConfig:
    selected = config.model_copy(
        update={
            name: getattr(config, name) or config.checker.model_copy(deep=True)
            for name in (
                "history_analyzer",
                "validator_pixel_safety_net",
                "planner_validation",
                "output_analyzer",
            )
        },
        deep=True,
    )

    def validate_route(node):
        if not isinstance(node, dict):
            return
        if "provider" in node:
            if (
                node["provider"] != "openai"
                or node.get("model") != "gpt-6.1-sol"
                or node.get("fix_model") not in (None, "gpt-6.1-sol")
            ):
                raise ValueError("CHE-984 D1 requires only the approved OpenAI gpt-6.1-sol route")
        for value in node.values():
            validate_route(value)

    validate_route(selected.model_dump())
    agent_config = load_agent_config()
    if any(
        model != "gpt-6.1-sol"
        for model in (
            agent_config.flash.step_summarizer.model,
            agent_config.memory.chunking.model,
        )
    ):
        raise ValueError("CHE-984 D1 requires only gpt-6.1-sol for native memory models")
    return selected


def d1_exit_code(result) -> int:
    if not isinstance(result, dict):
        return 2
    summary = result.get("test_summary")
    if not isinstance(summary, dict) or summary.get("task_status") != "completed":
        return 2
    counters = ("passed", "failed", "inconclusive", "unchecked", "retired")
    if any(type(summary.get(name)) is not int or summary[name] < 0 for name in counters):
        return 2
    if summary["inconclusive"] or summary["unchecked"] or summary["retired"]:
        return 2
    if summary.get("retired_items") != []:
        return 2
    failed_items = summary.get("failed_items")
    if not isinstance(failed_items, list) or len(failed_items) != summary["failed"]:
        return 2
    if any(
        not isinstance(item, dict)
        or item.get("kind") not in ("assert", "verify")
        or not isinstance(item.get("item_text"), str)
        or not item["item_text"].strip()
        or not isinstance(item.get("evidence"), str)
        or not item["evidence"].strip()
        for item in failed_items
    ):
        return 2
    if summary["failed"]:
        return 1
    return 0 if summary["passed"] > 0 else 2
