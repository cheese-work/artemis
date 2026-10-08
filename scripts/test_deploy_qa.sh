#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY_SCRIPT="$ROOT/scripts/deploy_qa.sh"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/artemis-qa-deploy.XXXXXX")
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_eq() {
  [[ $1 == "$2" ]] || fail "$3 (expected '$2', got '$1')"
}

make_case() {
  local name=$1 initial_revision=$2 initial_head
  CASE_ROOT="$TEST_ROOT/$name"
  REPO="$CASE_ROOT/repo"
  STATE="$CASE_ROOT/state"
  STUBS="$CASE_ROOT/bin"
  mkdir -p "$CASE_ROOT" "$STATE" "$STUBS"
  git init -q --bare "$CASE_ROOT/remote.git"
  git init -q "$REPO"
  git -C "$REPO" config user.name "Deploy Test"
  git -C "$REPO" config user.email "deploy-test@example.invalid"
  mkdir -p "$REPO/apps/showcase_ui" "$REPO/apps/admin_console/routers"
  printf '@router.get("/server-status")\n' > "$REPO/apps/admin_console/routers/system.py"
  printf 'base\n' > "$REPO/app.txt"
  git -C "$REPO" add app.txt
  git -C "$REPO" commit -qm base
  git -C "$REPO" branch -M main
  git -C "$REPO" remote add origin "$CASE_ROOT/remote.git"
  git -C "$REPO" push -q -u origin main
  OLD_SHA=$(git -C "$REPO" rev-parse HEAD)
  printf 'candidate\n' >> "$REPO/app.txt"
  # Use the real decorator line so the detector is tested against the shipped signature.
  grep -F '@router.get("/service-readiness"' "$ROOT/apps/admin_console/routers/system.py" >> "$REPO/apps/admin_console/routers/system.py" ||
    fail "service-readiness decorator missing from apps/admin_console/routers/system.py"
  git -C "$REPO" add apps/admin_console/routers/system.py
  git -C "$REPO" commit -qam candidate
  git -C "$REPO" push -q origin main
  NEW_SHA=$(git -C "$REPO" rev-parse HEAD)
  if [[ $initial_revision == new ]]; then
    initial_head=$NEW_SHA
  else
    initial_head=$OLD_SHA
  fi
  git -C "$REPO" checkout -q --detach "$initial_head"
  printf '%s\n' "$OLD_SHA" > "$STATE/deployed_sha"
  BUILD_LOG="$CASE_ROOT/build.log"
  CURL_COUNT="$CASE_ROOT/curl.count"
  CURL_LOG="$CASE_ROOT/curl.log"
  SYSTEMCTL_LOG="$CASE_ROOT/systemctl.log"

  cat > "$STUBS/uv" <<'SH'
#!/usr/bin/env bash
sha=$(git -C "$REPO" rev-parse HEAD)
printf '%s\n' "$sha" >> "$BUILD_LOG"
[[ ${FAIL_BUILD_SHA:-} != "$sha" ]]
SH
  cat > "$STUBS/npm" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat > "$STUBS/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
[[ $* == "--user restart artemis-qa.service" ]]
SH
  cat > "$STUBS/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CURL_LOG"
[[ $* == *"http://127.0.0.1:8000/"* && $* == *"Host: smart-qa.tevo.vn"* ]] || exit 22
count=$(cat "$CURL_COUNT" 2>/dev/null || printf '0')
count=$((count + 1))
printf '%s\n' "$count" > "$CURL_COUNT"
case "$*" in
  *"/api/system/service-readiness"*)
    [[ $(git -C "$REPO" rev-parse HEAD) != "$OLD_SHA" ]] || exit 22
    case "$HEALTH_MODE:$count" in
      not_ready_then_ready:1) printf '{"service_ready":false}\n' ;;
      malformed_json_then_ready:1) printf '{"service_ready":' ;;
      malformed_shape_then_ready:1) printf '{"overall_ready":true}\n' ;;
      decorated_false:1) printf '{"service_ready":false}\n' ;;
      decorated_malformed:1) printf '{"service_ready":' ;;
      decorated_404:1) exit 22 ;;
      *) printf '{"service_ready":true}\n' ;;
    esac
    ;;
  *"/api/system/server-status"*)
    [[ $(git -C "$REPO" rev-parse HEAD) == "$OLD_SHA" || $HEALTH_MODE == decorated_* ]] || exit 22
    printf '{"running":true}\n'
    ;;
  *) exit 22 ;;
esac
SH
  cat > "$STUBS/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$STUBS"/*
}

run_deployer() {
  PATH="$STUBS:$PATH" DRY_RUN="${DRY_RUN:-}" OLD_SHA="$OLD_SHA" \
    REPO="$REPO" STATE="$STATE" BUILD_LOG="$BUILD_LOG" \
    CURL_COUNT="$CURL_COUNT" CURL_LOG="$CURL_LOG" \
    SYSTEMCTL_LOG="$SYSTEMCTL_LOG" HEALTH_MODE="$HEALTH_MODE" \
    HEALTH_ATTEMPTS=1 HEALTH_INTERVAL=0 \
    bash "$DEPLOY_SCRIPT"
}

test_interrupted_checkout_recovers_and_retries() {
  make_case interrupted new
  HEALTH_MODE=ready
  run_deployer || fail "interrupted deployment recovery failed"
  assert_eq "$(cat "$STATE/deployed_sha")" "$NEW_SHA" "candidate was not recorded after recovery and retry"
  [[ ! -e "$STATE/deploying_sha" ]] || fail "deployment marker remained after successful recovery"
  [[ $(cat "$BUILD_LOG") == "$OLD_SHA"$'\n'"$NEW_SHA" ]] || fail "confirmed revision was not rebuilt before retry"
  [[ $(cat "$CURL_COUNT") == 2 ]] || fail "recovery and deployment were not both health-checked"
}

test_in_progress_marker_recovers_before_checkout() {
  make_case marker old
  printf '%s\n' "$NEW_SHA" > "$STATE/deploying_sha"
  HEALTH_MODE=ready
  run_deployer || fail "in-progress marker recovery failed"
  assert_eq "$(cat "$STATE/deployed_sha")" "$NEW_SHA" "candidate was not deployed after marker recovery"
  [[ $(cat "$BUILD_LOG") == "$OLD_SHA"$'\n'"$NEW_SHA" ]] || fail "in-progress marker did not restore confirmed revision first"
  [[ ! -e "$STATE/deploying_sha" ]] || fail "deployment marker remained after successful marker recovery"
}

test_failed_service_readiness_rolls_back() {
  make_case unhealthy old
  HEALTH_MODE=not_ready_then_ready
  if run_deployer; then fail "service_ready=false was accepted"; fi
  assert_eq "$(cat "$STATE/deployed_sha")" "$OLD_SHA" "failed candidate replaced the confirmed revision"
  assert_eq "$(cat "$STATE/bad_sha")" "$NEW_SHA" "failed candidate was not recorded"
  assert_eq "$(git -C "$REPO" rev-parse HEAD)" "$OLD_SHA" "rollback did not restore the confirmed checkout"
  [[ ! -e "$STATE/deploying_sha" ]] || fail "deployment marker remained after successful rollback"
  [[ $(cat "$CURL_COUNT") == 2 ]] || fail "candidate and rollback were not both health-checked"
}

test_legacy_rollback_clears_marker_and_allows_repair() {
  make_case legacy_rollback old
  HEALTH_MODE=not_ready_then_ready
  if run_deployer; then fail "failed candidate was accepted"; fi
  assert_eq "$(git -C "$REPO" rev-parse HEAD)" "$OLD_SHA" "legacy rollback did not restore confirmed checkout"
  [[ ! -e "$STATE/deploying_sha" ]] || fail "successful legacy rollback left deployment marker"
  [[ $(cat "$CURL_LOG") == *"/api/system/server-status"* ]] || fail "legacy recovery did not use non-diagnostic status endpoint"

  git -C "$REPO" checkout -q --detach "$NEW_SHA"
  printf 'repair\n' >> "$REPO/app.txt"
  git -C "$REPO" add app.txt
  git -C "$REPO" commit -qm repair
  REPAIR_SHA=$(git -C "$REPO" rev-parse HEAD)
  git -C "$REPO" push -q origin "$REPAIR_SHA:main"
  printf '0\n' > "$CURL_COUNT"
  HEALTH_MODE=ready
  run_deployer || fail "repaired main revision was not deployed after legacy rollback"
  assert_eq "$(cat "$STATE/deployed_sha")" "$REPAIR_SHA" "repair revision was not confirmed"
  [[ ! -e "$STATE/deploying_sha" ]] || fail "repair deployment marker remained"
}

test_dry_run_does_not_recover_or_mutate_state() {
  make_case dry_run_recovery new
  printf '%s\n' "$NEW_SHA" > "$STATE/deploying_sha"
  HEALTH_MODE=ready
  DRY_RUN=1
  run_deployer || fail "dry run failed"
  unset DRY_RUN
  assert_eq "$(git -C "$REPO" rev-parse HEAD)" "$NEW_SHA" "dry run changed checkout"
  assert_eq "$(cat "$STATE/deploying_sha")" "$NEW_SHA" "dry run changed in-progress marker"
  [[ ! -s "$BUILD_LOG" ]] || fail "dry run built application"
  [[ ! -s "$SYSTEMCTL_LOG" ]] || fail "dry run restarted service"
  [[ ! -s "$CURL_LOG" ]] || fail "dry run performed health requests"
}

test_malformed_readiness_is_rejected_and_rolled_back() {
  local mode
  for mode in malformed_json_then_ready malformed_shape_then_ready; do
    make_case "$mode" old
    HEALTH_MODE=$mode
    if run_deployer; then fail "$mode readiness report was accepted"; fi
    assert_eq "$(cat "$STATE/deployed_sha")" "$OLD_SHA" "$mode report promoted the candidate"
    assert_eq "$(cat "$STATE/bad_sha")" "$NEW_SHA" "$mode report did not mark candidate bad"
    assert_eq "$(git -C "$REPO" rev-parse HEAD)" "$OLD_SHA" "$mode report did not trigger rollback"
  done
}

test_decorated_service_readiness_rejects_invalid_reports() {
  local mode failures=0
  for mode in decorated_false decorated_malformed decorated_404; do
    make_case "$mode" old
    HEALTH_MODE=$mode
    if run_deployer; then
      echo "FAIL: decorated route accepted $mode health response" >&2
      failures=$((failures + 1))
      continue
    fi
    assert_eq "$(cat "$STATE/deployed_sha")" "$OLD_SHA" "$mode response promoted the candidate"
    assert_eq "$(cat "$STATE/bad_sha")" "$NEW_SHA" "$mode response did not mark candidate bad"
    assert_eq "$(git -C "$REPO" rev-parse HEAD)" "$OLD_SHA" "$mode response did not roll back"
    [[ $(cat "$CURL_LOG") == *"/api/system/service-readiness"* ]] || fail "$mode did not check service readiness"
  done
  [[ $failures == 0 ]] || fail "$failures decorated service-readiness regressions reproduced"
}

test_interrupted_checkout_recovers_and_retries
test_in_progress_marker_recovers_before_checkout
test_failed_service_readiness_rolls_back
test_legacy_rollback_clears_marker_and_allows_repair
test_dry_run_does_not_recover_or_mutate_state
test_malformed_readiness_is_rejected_and_rolled_back
test_decorated_service_readiness_rejects_invalid_reports
echo "PASS: interrupted recovery, legacy repair, dry-run safety, service-only readiness, rollback, malformed report"
