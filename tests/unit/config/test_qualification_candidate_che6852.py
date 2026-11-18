import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/che6852_st008_event_map.v1.json"
TICK = chr(96)

EXPECTED_EVENTS = [
    "splash_scr_view",
    "obd_1_scr_view",
    "obd_2_scr_view",
    "obd_3_scr_view",
    "lfo_1_scr_view",
    "lfo_2_scr_view",
    "obd_6_scr_view",
    "obd_7_scr_view",
    "obd_8_scr_view",
    "obd_full_8_scr_view",
    "home_scr_view",
    "noti_permission_view",
    "noti_permission_allow",
    "microphone_permission_view",
    "microphone_permission_allow",
    "microphone_permission_denied",
    "inter_splash_scr_view",
    "inter_home_scr_view",
    "app_open_scr_view",
    "feature_speaking_scr_view",
    "feature_speaking_start",
    "feature_speaking_finish",
    "feature_speaking_fail",
    "feature_flash_card_scr_view",
    "feature_flash_card_start",
    "feature_flash_card_success",
    "feature_flash_card_fail",
    "paywall_scr_view",
    "paywall_scr_view",
    "paywall_btn_click",
    "paywall_btn_click",
    "in_app_purchase",
]

EXPECTED_REMOVALS = [
    ["app_open", "app_foreground", "app_background"],
    ["splash_bootstrap_started", "splash_bootstrap_completed", "splash_bootstrap_failed"],
    ["navigation_route_changed", "screen_view"],
    ["language_selected", "onboarding_started", "onboarding_next_tapped", "onboarding_completed"],
    [
        "permission_request_tapped",
        "permission_result",
        "account_notifications_enable_tapped",
        "account_notification_permission_result",
    ],
    [
        "login_google_start",
        "login_google_success",
        "login_google_cancelled",
        "login_google_failed",
        "login_backend_failed",
        "login_google_error",
        "logout_tapped",
        "logout_success",
    ],
    [
        "home_tab_selected",
        "practice_tab_selected",
        "redeem_tab_selected",
        "rank_tab_selected",
        "account_tab_selected",
    ],
    [
        "account_language_opened",
        "account_language_changed",
        "account_rate_app_tapped",
        "account_contact_us_tapped",
        "account_share_app_tapped",
    ],
    ["lesson_selected", "lesson_start", "lesson_end", "lesson_complete"],
    ["interstitial_show", "interstitial_failed"],
    [
        "premium_purchase_attempt",
        "premium_purchase_success",
        "premium_purchase_cancelled",
        "premium_purchase_failed",
        "premium_restore_attempt",
        "premium_restore_success",
        "premium_restore_failed",
    ],
    ["api_call_failed", "api_call_timeout"],
]


def _read_candidate():
    return json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))


def test_receipt_pins_runner_app_inputs_and_provider_authorization():
    receipt = _read_candidate()
    sources = receipt["source_candidate"]

    assert receipt["issue"] == "CHE-6852"
    assert receipt["parent_issue"] == "CHE-6843"
    assert sources["runner_source"]["commit_sha"] == ("676320699514778d7b589783cb5590ada8d4048f")
    assert sources["app_input_source"] == {
        "repository": "https://github.com/tevo-studio/st008-palios",
        "pull_request": 36,
        "commit_sha": "aeb9879c06ece7998aa5c85012e6fe750b15bbb9",
    }
    assert sources["map_source"]["sha256"] == (
        "e362eaf1564501a1986934508bce53d5fcd6e68300d464cbca5cef2e151d99c8"
    )
    assert sources["effective_lock"]["sha256"] == (
        "14f129e59028406dc1f77687c6c01ac83360926c91ea53c61a8f3a5b73dac080"
    )
    assert sources["tevo_ads_override"]["commit_sha"] == (
        "65e2050d61683151559580802de9697171788aa3"
    )

    execution = receipt["execution"]
    assert execution["status"] == "NOT-RUN"
    assert execution["target_serial"] == "assigned_at_execution"
    assert execution["model"]["provider"] == "openai"
    assert execution["model"]["credential_env"] == "OPENAI_API_KEY"
    assert execution["model"]["metered_spend_possible"] is True
    assert execution["model"]["provider_call_made"] is False
    assert execution["model"]["spend_decision_owner"] == "Android device squad leader"
    authorization = execution["model"]["spend_authorization"]
    assert authorization == {
        "status": "AUTHORIZED_WITH_CAP",
        "provider": "openai",
        "model": "gpt-6-sol",
        "max_usd_per_run": 5,
        "max_usd_total": 15,
    }
    assert "both independent reviews" in execution["model"]["open_point"]
    assert "AVD admission" in execution["model"]["open_point"]

    testcase = sources["testcase_source"]
    journey_path = REPO_ROOT / testcase["journey_path"]
    journey_bytes = journey_path.read_bytes()
    assert hashlib.sha256(journey_bytes).hexdigest() == testcase["journey_digest_sha256"]

    goals = receipt["goals"]
    journey = journey_bytes.decode("utf-8")
    assert goals
    for goal in goals:
        assert hashlib.sha256(goal["text"].encode("utf-8")).hexdigest() == goal["sha256"]
        assert goal["text"] in journey


def test_each_event_and_removal_label_binds_one_fail_closed_journey_assertion():
    receipt = _read_candidate()
    journey = (
        REPO_ROOT / receipt["source_candidate"]["testcase_source"]["journey_path"]
    ).read_text(encoding="utf-8")
    coverage = receipt["coverage"]
    event_assertions = coverage["event_assertions"]
    removal_assertions = coverage["removal_assertions"]

    assert [item["event"] for item in event_assertions] == EXPECTED_EVENTS
    assert [item["removed_names"] for item in removal_assertions] == EXPECTED_REMOVALS
    assert len({item["assertion_id"] for item in event_assertions}) == len(event_assertions)
    assert len({item["assertion_id"] for item in removal_assertions}) == len(removal_assertions)
    assert event_assertions[27]["expected_trigger"] == "Weekly trial paywall actually visible"
    assert event_assertions[27]["expected_params"]["paywall_id"] == "premium_weekly_trial"
    assert (
        event_assertions[28]["expected_trigger"]
        == "Exit-discount sheet actually visible over weekly paywall"
    )
    assert event_assertions[28]["expected_params"]["paywall_id"] == "premium_yearly_exit"

    for index, item in enumerate(event_assertions, start=1):
        assert item["assertion_id"] == f"event_{index:02d}"
        expected_status = "DEFERRED" if item["event"] == "obd_full_8_scr_view" else "NOT-RUN"
        assert item["execution_status"] == expected_status
        assert item["observed_count"] == "unknown"
        assert item["evidence"] is None
        heading = f"### {item['assertion_id']}: {TICK}{item['event']}{TICK}"
        assert heading in journey
        block = journey.split(heading, 1)[1].split("\n### ", 1)[0]
        assert "**Fail closed:**" in block

    for index, item in enumerate(removal_assertions, start=1):
        assert item["assertion_id"] == f"removal_{index:02d}"
        assert item["execution_status"] == "NOT-RUN"
        assert item["observed_count"] == "unknown"
        assert item["evidence"] is None
        heading = f"### {item['assertion_id']}: {item['label']}"
        assert heading in journey
        block = journey.split(heading, 1)[1].split("\n### ", 1)[0]
        assert "**Fail closed:**" in block
        for name in item["removed_names"]:
            assert f"{TICK}{name}{TICK}" in block


def test_journey_carries_fixture_privacy_qualification_and_review_guards():
    receipt = _read_candidate()
    journey = (
        REPO_ROOT / receipt["source_candidate"]["testcase_source"]["journey_path"]
    ).read_text(encoding="utf-8")
    for required in (
        "assigned_at_execution",
        "DebugView",
        "store-sandbox",
        "obd_full_8_scr_view",
        "FA-SVC",
        "clean representative pass",
        "clean rerun",
        "deliberate failing assertion",
        "exit 1",
        "cleanup/release",
        "focus loss",
        "overlapping notification/microphone requests",
        "unavailable",
        "real-return counts",
        "actual SDK emissions",
    ):
        assert required in journey

    review = receipt["review_contract"]
    assert review["independent_non_author_reviewers"] == 2
    assert review["same_revision_required"] is True
    assert review["must_precede_execution"] is True
    assert review["author_approves_own_testcase"] is False


def test_candidate_binds_executable_assertions_command_provider_and_fixture_limits(monkeypatch):
    receipt = _read_candidate()
    journey = (
        REPO_ROOT / receipt["source_candidate"]["testcase_source"]["journey_path"]
    ).read_text(encoding="utf-8")

    for goal in receipt["goals"]:
        assert goal["assertion_ids"]
        for assertion_id in goal["assertion_ids"]:
            assert assertion_id in goal["text"]
            assert assertion_id in journey

    goal_by_assertion = {
        assertion_id: goal["text"]
        for goal in receipt["goals"]
        for assertion_id in goal["assertion_ids"]
    }
    for item in receipt["coverage"]["event_assertions"]:
        assert f"{item['assertion_id']}={item['event']}" in goal_by_assertion[item["assertion_id"]]
    removal_goal = goal_by_assertion["removal_12"]
    for item in receipt["coverage"]["removal_assertions"]:
        assert item["assertion_id"] in removal_goal
        for name in item["removed_names"]:
            assert name in removal_goal

    command = receipt["execution"]["command_template"]
    for flag in (
        "--standalone",
        "--profile pro",
        "--assertion-exit",
        "--device-serial <leased serial>",
        "--locked-app com.palio.ai",
        "--disable-planner-validation",
    ):
        assert flag in command
    assert "No." in receipt["execution"]["locked_app_permission_dialogs"]
    assert "must remain NOT-RUN" in receipt["execution"]["locked_app_permission_dialogs"]

    config = receipt["execution"]["llm_config"]
    config_path = REPO_ROOT / config["path"]
    config_bytes = config_path.read_bytes()
    assert hashlib.sha256(config_bytes).hexdigest() == config["sha256"]
    config_text = config_bytes.decode("utf-8")
    assert '"validator_pixel_safety_net"' in config_text
    assert '"planner_validation"' in config_text
    config_data = json.loads(config_text)
    for node in ("validator_pixel_safety_net", "planner_validation"):
        assert config_data[node]["provider"] == "openai"
        assert config_data[node]["model"] == "gpt-6-sol"
        assert config_data[node]["fallback"]["provider"] == "openai"
        assert config_data[node]["fallback"]["model"] == "gpt-6-sol"

    from artemis.config.llm import parse_llm_config

    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(config_path))
    loaded = parse_llm_config()
    for node in ("validator_pixel_safety_net", "planner_validation"):
        model = loaded.get_agent(node)
        assert model.provider == "openai"
        assert model.model == "gpt-6-sol"
        assert model.fallback.provider == "openai"
        assert model.fallback.model == "gpt-6-sol"

    control = receipt["qualification"]["negative_control"]
    assert control == {
        "assertion_id": "negative_control_title",
        "wrong_value": "Palios — WRONG TITLE",
        "expected_exit": 1,
        "proof": "complete native summary has exactly this failed item, the observed title is different, marker ARTEMIS_ASSERTION_EXIT=1 is present, and process exit is 1",
    }
    assert control["assertion_id"] in journey
    assert control["wrong_value"] in journey

    fixture = receipt["fixture"]
    assert fixture["reset"]["app_data"].endswith("shell pm clear com.palio.ai")
    assert len(fixture["reset"]["permissions"]) == 4
    assert fixture["login"]["status"] == "NOT-RUN"
    assert "getprop debug.firebase.analytics.app" in fixture["fa_debug"]["restore"]
    assert "do not assume .none." in fixture["fa_debug"]["restore"]
    assert len(fixture["fa_debug"]["setup"]) == 4
    for scenario in ("speaking", "flash_cards", "ads", "paywall"):
        assert fixture[scenario]["status"] == "NOT-RUN"
    assert "obd_full_8_scr_view" in journey and "DEFERRED" in journey
