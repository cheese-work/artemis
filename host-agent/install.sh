#!/bin/sh
set -eu
server=${1:?Pass the SmartQA server HTTPS URL}
code=${2:?Pass the enrollment code}
option=${3:-}
case "$server" in https://*) ;; *) echo 'HTTPS server required' >&2; exit 1 ;; esac
case "$option" in ''|--no-adb-download) ;; *) echo 'Unknown install option' >&2; exit 1 ;; esac
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) artifact=smartqa-host-linux-amd64 ;;
  Darwin/arm64) artifact=smartqa-host-darwin-arm64 ;;
  *) echo 'This installer supports Linux amd64 and macOS arm64 only' >&2; exit 1 ;;
esac
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
umask 077
header="X-Artemis-Enrollment-Code: $code"
curl --proto '=https' --fail --silent --show-error -H "$header" "$server/api/agent/dist/SHA256SUMS" -o "$temporary/SHA256SUMS"
curl --proto '=https' --fail --silent --show-error -H "$header" "$server/api/agent/dist/$artifact" -o "$temporary/$artifact"
expected=$(awk -v filename="$artifact" '$2 == filename {print $1}' "$temporary/SHA256SUMS")
case "$expected" in *[!0-9a-f]*|'') echo 'Invalid SHA256 manifest' >&2; exit 1 ;; esac
[ ${#expected} -eq 64 ] || { echo 'Invalid SHA256 length' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$temporary/$artifact" | awk '{print $1}'); else actual=$(shasum -a 256 "$temporary/$artifact" | awk '{print $1}'); fi
[ "$actual" = "$expected" ] || { echo 'SQH-E202: checksum mismatch' >&2; exit 1; }
[ ! -L "$HOME/.smartqa" ] || { echo 'Refusing a symbolic-link state directory' >&2; exit 1; }
mkdir -p "$HOME/.local/bin" "$HOME/.smartqa"
chmod 700 "$HOME/.smartqa"
destination="$HOME/.local/bin/smartqa-host"
[ ! -L "$destination" ] || { echo 'Refusing a symbolic-link install destination' >&2; exit 1; }
if [ -e "$destination" ]; then
  cmp -s "$destination" "$temporary/$artifact" || { echo 'Existing binary differs. Use the launcher-owned update path; installer will not overwrite it.' >&2; exit 1; }
else
  install -m 700 "$temporary/$artifact" "$destination"
fi
export ARTEMIS_HOST_AGENT=1
if [ -n "$option" ]; then
  "$destination" enroll --server "$server" --code "$code" "$option"
  "$destination" doctor "$option"
else
  "$destination" enroll --server "$server" --code "$code"
  "$destination" doctor
fi
"$destination" service install
"$destination" service status
