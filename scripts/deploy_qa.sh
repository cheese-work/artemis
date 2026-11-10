#!/usr/bin/env bash
set -uo pipefail
REPO=${REPO:-$HOME/services/artemis-qa}
STATE=${STATE:-$HOME/services/artemis-qa-deploy}
UNIT=artemis-qa.service
HEALTH_URL=http://127.0.0.1:8000/api/system/service-readiness
HEALTH_HOST=smart-qa.tevo.vn
HEALTH_ATTEMPTS=${HEALTH_ATTEMPTS:-60}
HEALTH_INTERVAL=${HEALTH_INTERVAL:-2}
DRAIN_URL=http://127.0.0.1:8000/api/system/drain
DRAIN_TIMEOUT_SECONDS=${DRAIN_TIMEOUT_SECONDS:-${ARTEMIS_DRAIN_TIMEOUT_SECONDS:-600}}
DRAIN_INTERVAL=${DRAIN_INTERVAL:-2}
MAX_DEFER_COUNT=${MAX_DEFER_COUNT:-${ARTEMIS_DEPLOY_MAX_DEFER_COUNT:-0}}
SKIP_DRAIN=0
if [[ ${1:-} == --skip-drain && $# == 1 ]]; then
  SKIP_DRAIN=1
elif (( $# > 0 )); then
  echo "usage: $0 [--skip-drain]" >&2
  exit 2
fi
[[ $DRAIN_TIMEOUT_SECONDS =~ ^[0-9]+$ && $MAX_DEFER_COUNT =~ ^[0-9]+$ ]] || exit 2
export PATH="${PATH:+$PATH:}$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
exec 9>"$STATE/.lock"; flock -n 9 || { echo "another deploy running"; exit 0; }
log() { echo "[artemis-qa-deploy] $*"; }

write_state() {
  local name=$1 value=$2 tmp
  tmp="$STATE/.${name}.tmp.$$"
  printf '%s\n' "$value" > "$tmp" && sync -f "$tmp" && mv -f -- "$tmp" "$STATE/$name" && sync -f "$STATE"
}

remove_state() {
  rm -f -- "$STATE/$1" && sync -f "$STATE"
}

build() {
  mkdir -p "$STATE/releases" || return 1
  release=$(mktemp -d "$STATE/releases/$1.XXXXXX") || return 1
  git -C "$REPO" worktree add -q --detach "$release" "$1" || return 1
  (cd "$release" && uv sync -q --locked) &&
  (cd "$release/apps/showcase_ui" && npm ci --no-audit --no-fund --loglevel=error && npm run build >/dev/null) &&
  printf '%s\n%s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$release/.DEPLOYED_SHA.tmp" &&
  mv -f -- "$release/.DEPLOYED_SHA.tmp" "$release/DEPLOYED_SHA"
}

activate() {
  local target=$1
  if [[ ! -L "$REPO" ]]; then
    [[ ! -e "$STATE/source" ]] || { log "source backup already exists; refusing to replace it"; return 1; }
    mv -- "$REPO" "$STATE/source" || return 1
    git -C "$STATE/source" worktree repair || return 1
  fi
  ln -s -- "$target" "$STATE/.current.$$" && mv -Tf -- "$STATE/.current.$$" "$REPO"
}

drain_request() {
  curl -fsS -m 5 -H "Host: $HEALTH_HOST" -H 'Content-Type: application/json' "$@"
}

reopen() {
  [[ -n ${drain_owner:-} ]] || return 0
  if drain_request -X DELETE "$DRAIN_URL?owner=$drain_owner" >/dev/null; then
    remove_state drain_owner
    drain_owner=
  else
    log "admission remains closed; next tick must recover owner $drain_owner"
    return 1
  fi
}

cleanup() {
  local result=$?
  trap - EXIT
  reopen || result=1
  exit "$result"
}

trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

begin_drain() {
  local deadline report
  drain_owner="deploy-$$"
  deadline=$(( $(date +%s) + DRAIN_TIMEOUT_SECONDS ))
  write_state drain_owner "$drain_owner" || return 1
  report=$(drain_request -X POST --data "{\"owner\":\"$drain_owner\",\"deadline\":$deadline}" "$DRAIN_URL") || return 1
  printf '%s' "$report" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["draining"] is True and value["owner"] == sys.argv[1] and value["deadline"] == int(sys.argv[2])' "$drain_owner" "$deadline"
}

wait_for_drain() {
  local started=$SECONDS report count deferred=0
  if [[ $(cat "$STATE/defer_sha" 2>/dev/null || true) == "$new" ]]; then
    deferred=$(cat "$STATE/defer_count" 2>/dev/null || printf '0')
    [[ $deferred =~ ^[0-9]+$ ]] || return 1
  fi
  while true; do
    report=$(drain_request "$DRAIN_URL") || return 1
    count=$(printf '%s' "$report" | python3 -c 'import json,sys; value=json.load(sys.stdin); count=value["active_run_count"]; assert value["draining"] is True and type(count) is int and count >= 0; print(count)') || return 1
    if (( count == 0 )); then
      log "deploy_drain waited_s=$((SECONDS - started)) cut=[]"
      return 0
    fi
    if (( SKIP_DRAIN == 1 || SECONDS - started >= DRAIN_TIMEOUT_SECONDS )); then
      deferred=$((deferred + 1))
      if (( SKIP_DRAIN == 1 || (MAX_DEFER_COUNT > 0 && deferred >= MAX_DEFER_COUNT) )); then
        report=$(drain_request -X POST --data "{\"owner\":\"$drain_owner\"}" "$DRAIN_URL/cut") || return 1
        printf '%s' "$report" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert isinstance(value["session_ids"],list)' || return 1
        log "deploy_drain waited_s=$((SECONDS - started)) cut=$report"
        return 0
      fi
      write_state defer_sha "$new" && write_state defer_count "$deferred" || return 1
      log "deploy_drain waited_s=$((SECONDS - started)) deferred=$deferred; abort, retry next tick"
      return 1
    fi
    sleep "$DRAIN_INTERVAL"
  done
}

healthy() {
  local revision=$1 attempt report endpoint ready_field
  if git -C "$REPO" grep -Fq '@router.get("/service-readiness"' "$revision" -- apps/admin_console/routers/system.py; then
    endpoint=service-readiness
    ready_field=service_ready
  else
    endpoint=server-status
    ready_field=running
  fi
  for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    if report=$(curl -fsS -m 3 -H "Host: $HEALTH_HOST" "${HEALTH_URL%/*}/$endpoint" 2>/dev/null) &&
      printf '%s' "$report" | python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(0 if isinstance(value,dict) and value.get(sys.argv[1]) is True else 1)' "$ready_field" >/dev/null 2>&1; then
      return 0
    fi
    if ((attempt < HEALTH_ATTEMPTS)); then sleep "$HEALTH_INTERVAL"; fi
  done
  return 1
}

recover() {
  local new=$deployed
  log "restoring last confirmed revision $deployed"
  if ! build "$deployed"; then
    return 1
  fi
  if [[ -z ${drain_owner:-} ]] && ! begin_drain; then
    return 1
  fi
  if ! wait_for_drain || ! activate "$release" || ! systemctl --user restart "$UNIT" || ! healthy "$deployed"; then
    log "recovery of $deployed FAILED"
    return 1
  fi
  remove_state deploying_sha || return 1
  recovered_healthy=1
  log "recovered $deployed"
}

if [[ ! -e "$REPO" && -d "$STATE/source/.git" && ${DRY_RUN:-} != 1 ]]; then
  ln -s -- "$STATE/source" "$REPO" || exit 1
fi
deployed=$(cat "$STATE/deployed_sha" 2>/dev/null || true)
if [[ ! $deployed =~ ^[0-9a-f]{40}$ ]] || ! git -C "$REPO" cat-file -e "$deployed^{commit}" 2>/dev/null; then
  log "missing or invalid confirmed deployment revision; refusing to deploy"
  exit 1
fi

if [[ ${DRY_RUN:-} == 1 ]]; then
  git -C "$REPO" fetch -q origin main || { log "fetch failed"; exit 1; }
  new=$(git -C "$REPO" rev-parse origin/main) || exit 1
  [[ $new =~ ^[0-9a-f]{40}$ ]] || { log "origin/main is not a commit SHA"; exit 1; }
  [[ $new == "$(cat "$STATE/bad_sha" 2>/dev/null || true)" ]] && { log "skip known-bad $new"; exit 0; }
  [[ $new == "$deployed" ]] && exit 0
  log "DRY_RUN would deploy $deployed -> $new"
  exit 0
fi

drain_owner=$(cat "$STATE/drain_owner" 2>/dev/null || true)

if [[ -e "$STATE/deploying_sha" ]]; then
  deploying=$(cat "$STATE/deploying_sha" 2>/dev/null || true)
  if [[ $deploying == "$deployed" ]]; then
    remove_state deploying_sha || exit 1
    remove_state bad_sha || exit 1
  elif ! recover; then
    exit 1
  fi
fi

cur=$(git -C "$REPO" rev-parse HEAD) || exit 1
if [[ $cur != "$deployed" ]]; then
  log "checkout $cur is not the last confirmed revision"
  recover || exit 1
fi

if [[ -n $drain_owner ]]; then
  { [[ ${recovered_healthy:-0} == 1 ]] || healthy "$deployed"; } && reopen || { log "previous drain could not be recovered"; exit 1; }
fi

git -C "$REPO" fetch -q origin main || { log "fetch failed"; exit 1; }
new=$(git -C "$REPO" rev-parse origin/main) || exit 1
[[ $new =~ ^[0-9a-f]{40}$ ]] || { log "origin/main is not a commit SHA"; exit 1; }
[[ $new == "$(cat "$STATE/bad_sha" 2>/dev/null || true)" ]] && { log "skip known-bad $new"; exit 0; }
[[ $new == "$deployed" ]] && exit 0
if ! build "$new"; then
  log "build failed; running service was not changed"
  exit 1
fi
begin_drain && wait_for_drain || exit 1
write_state deploying_sha "$new" || { log "could not persist deployment marker"; exit 1; }
log "deploying $deployed -> $new"
if activate "$release" && systemctl --user restart "$UNIT" && healthy "$new"; then
  if ! write_state deployed_sha "$new"; then
    log "service is healthy at $new but the confirmed revision could not be persisted"
    exit 1
  fi
  remove_state bad_sha || { log "deployed $new; could not clear bad revision state"; exit 1; }
  remove_state deploying_sha || { log "deployed $new; could not clear deployment marker"; exit 1; }
  remove_state defer_sha || exit 1
  remove_state defer_count || exit 1
  log "deployed $new"
  exit 0
fi

log "deploy of $new FAILED, rolling back to $deployed"
write_state bad_sha "$new" || log "could not persist known-bad revision $new"
if recover; then
  log "rolled back to $deployed"
else
  log "ROLLBACK FAILED, service needs a human"
fi
exit 1
