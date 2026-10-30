import pytest

from apps.admin_console.services.agent_qa import flow as flow_module
from apps.admin_console.services.agent_qa.flow import FlowError, build_flow, verify_route
from apps.admin_console.services.agent_qa.session import load_session
from tests.support.agent_qa_fixtures import ROUTE, write_png, write_session

FIVE = ROUTE[:5]


def test_t4_secret_route_requires_a_server_secret_placeholder():
    validator = getattr(flow_module, "validate_route", None)
    assert callable(validator), "secret route validation is missing"

    with pytest.raises(FlowError) as exc:
        validator([{"type": "type", "secret": True, "value": "SMARTQA-SENTINEL-7f3c"}])

    assert exc.value.code == "route_secret_literal"
    validator([{"type": "type", "secret": True, "value": "${secret:TEST_LOGIN_PASSWORD}"}])


def test_t1_five_step_session_produces_ordered_flow_with_ten_real_pngs(tmp_path):
    session = load_session(write_session(tmp_path, themes=("light",), route=FIVE))
    flow = build_flow(session, "light")
    assert [s["step"] for s in flow.steps] == [1, 2, 3, 4, 5]
    assert all(s["action"]["type"] for s in flow.steps)
    pngs = [tmp_path / s[k] for s in flow.steps for k in ("before_png", "after_png")]
    assert len(pngs) == 10 and all(p.is_file() for p in pngs)


def test_screen_map_lists_each_cluster_once_with_actions_leading_out(tmp_path):
    session = load_session(write_session(tmp_path, themes=("light",)))
    flow = build_flow(session, "light")
    assert sorted(flow.screen_map) == ["Home", "NativePool", "Pager"]
    out_of_home = [a["to"] for a in flow.screen_map["Home"]]
    assert "NativePool" in out_of_home and "Pager" in out_of_home


def test_t2_wrong_expected_screen_fails_with_flow_mismatch_at_step(tmp_path):
    session = load_session(write_session(tmp_path, themes=("light",)))
    flow = build_flow(session, "light")
    expected = [after for _, after, _ in ROUTE]
    verify_route(flow, expected)  # the true route passes
    expected[2] = "Pager"
    with pytest.raises(FlowError) as exc:
        verify_route(flow, expected)
    assert exc.value.code == "flow_mismatch at step 3"


def test_t2_missing_after_png_fails_flow_incomplete(tmp_path):
    session = load_session(write_session(tmp_path, themes=("light",), route=FIVE))
    (tmp_path / "light" / "4-after.png").unlink()
    with pytest.raises(FlowError) as exc:
        build_flow(session, "light")
    assert exc.value.code == "flow_incomplete"


def test_blank_png_is_incomplete(tmp_path):
    session = load_session(write_session(tmp_path, themes=("light",), route=FIVE))
    write_png(tmp_path / "light" / "2-before.png", blank=True, fg=(255, 255, 255))
    with pytest.raises(FlowError) as exc:
        build_flow(session, "light")
    assert exc.value.code == "flow_incomplete"


def test_route_length_mismatch_fails(tmp_path):
    flow = build_flow(load_session(write_session(tmp_path, themes=("light",))), "light")
    with pytest.raises(FlowError) as exc:
        verify_route(flow, ["Home"])
    assert exc.value.code == "flow_mismatch at step 2"


def test_t3_shape_known_route_both_themes_is_stable_and_a_changed_route_fails(tmp_path):
    """Fixture-level T3 shape; the real-device run of T3 belongs to S6."""
    session = load_session(write_session(tmp_path))
    expected = [after for _, after, _ in ROUTE]
    first = {t: build_flow(session, t) for t in ("light", "dark")}
    second = {t: build_flow(session, t) for t in ("light", "dark")}
    for theme, flow in first.items():
        assert len(flow.steps) == 6
        assert sorted(flow.screen_map) == ["Home", "NativePool", "Pager"]
        verify_route(flow, expected)
        assert [(s["step"], s["screen_after"]) for s in flow.steps] == [
            (s["step"], s["screen_after"]) for s in second[theme].steps
        ]
        changed = ["Home", "Pager", *expected[2:]]
        with pytest.raises(FlowError) as exc:
            verify_route(flow, changed)
        assert exc.value.code == "flow_mismatch at step 2"
