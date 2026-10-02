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
  mkdir -p "$REPO/apps/showcase_ui"
  printf 'base\n' > "$REPO/app.txt"
  git -C "$REPO" add app.txt
  git -C "$REPO" commit -qm base
  git -C "$REPO" branch -M main
  git -C "$REPO" remote add origin "$CASE_ROOT/remote.git"
  git -C "$REPO" push -q -u origin main
  OLD_SHA=$(git -C "$REPO" rev-parse HEAD)
  printf 'candidate\n' >> "$REPO/app.txt"
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
[[ $* == *"/api/system/service-readiness"* ]] || exit 22
[[ $* == *"http://127.0.0.1:8000/api/system/service-readiness"* && $* == *"Host: smart-qa.tevo.vn"* ]] || exit 22
count=$(cat "$CURL_COUNT" 2>/dev/null || printf '0')
count=$((count + 1))
printf '%s\n' "$count" > "$CURL_COUNT"
case "$HEALTH_MODE:$count" in
  not_ready_then_ready:1) printf '{"service_ready":false}\n' ;;
  malformed_json_then_ready:1) printf '{"service_ready":' ;;
  malformed_shape_then_ready:1) printf '{"overall_ready":true}\n' ;;
  *) printf '{"service_ready":true}\n' ;;
esac
SH
  cat > "$STUBS/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$STUBS"/*
}

run_deployer() {
  PATH="$STUBS:$PATH" \
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

test_interrupted_checkout_recovers_and_retries
test_in_progress_marker_recovers_before_checkout
test_failed_service_readiness_rolls_back
test_malformed_readiness_is_rejected_and_rolled_back
echo "PASS: interrupted recovery, service-only readiness, rollback, malformed report"
