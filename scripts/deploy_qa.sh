#!/usr/bin/env bash
set -uo pipefail
REPO=${REPO:-$HOME/services/artemis-qa}
STATE=${STATE:-$HOME/services/artemis-qa-deploy}
UNIT=artemis-qa.service
HEALTH_URL=http://127.0.0.1:8000/api/system/service-readiness
HEALTH_HOST=smart-qa.tevo.vn
HEALTH_ATTEMPTS=${HEALTH_ATTEMPTS:-60}
HEALTH_INTERVAL=${HEALTH_INTERVAL:-2}
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
  git -C "$REPO" checkout -q --detach "$1" &&
  (cd "$REPO" && uv sync -q) &&
  (cd "$REPO/apps/showcase_ui" && npm ci --no-audit --no-fund --loglevel=error && npm run build >/dev/null)
}

healthy() {
  local attempt report
  for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    if report=$(curl -fsS -m 3 -H "Host: $HEALTH_HOST" "$HEALTH_URL" 2>/dev/null) &&
      printf '%s' "$report" | python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(0 if isinstance(value,dict) and value.get("service_ready") is True else 1)' >/dev/null 2>&1; then
      return 0
    fi
    if ((attempt < HEALTH_ATTEMPTS)); then sleep "$HEALTH_INTERVAL"; fi
  done
  return 1
}

recover() {
  log "restoring last confirmed revision $deployed"
  if ! build "$deployed" || ! systemctl --user restart "$UNIT" || ! healthy; then
    log "recovery of $deployed FAILED"
    return 1
  fi
  remove_state deploying_sha || return 1
  log "recovered $deployed"
}

deployed=$(cat "$STATE/deployed_sha" 2>/dev/null || true)
if [[ ! $deployed =~ ^[0-9a-f]{40}$ ]] || ! git -C "$REPO" cat-file -e "$deployed^{commit}" 2>/dev/null; then
  log "missing or invalid confirmed deployment revision; refusing to deploy"
  exit 1
fi

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

git -C "$REPO" fetch -q origin main || { log "fetch failed"; exit 1; }
new=$(git -C "$REPO" rev-parse origin/main) || exit 1
[[ $new =~ ^[0-9a-f]{40}$ ]] || { log "origin/main is not a commit SHA"; exit 1; }
[[ $new == "$(cat "$STATE/bad_sha" 2>/dev/null || true)" ]] && { log "skip known-bad $new"; exit 0; }
[[ $new == "$deployed" ]] && exit 0
[[ ${DRY_RUN:-} == 1 ]] && { log "DRY_RUN would deploy $deployed -> $new"; exit 0; }

write_state deploying_sha "$new" || { log "could not persist deployment marker"; exit 1; }
log "deploying $deployed -> $new"
if build "$new" && systemctl --user restart "$UNIT" && healthy; then
  if ! write_state deployed_sha "$new"; then
    log "service is healthy at $new but the confirmed revision could not be persisted"
    exit 1
  fi
  remove_state bad_sha || { log "deployed $new; could not clear bad revision state"; exit 1; }
  remove_state deploying_sha || { log "deployed $new; could not clear deployment marker"; exit 1; }
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
