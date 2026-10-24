#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 5 || "${4:-}" != "--" ]]; then
  printf 'Usage: %s <serial> <logcat-file> <metadata-file> -- <command> [args...]\n' "$0" >&2
  exit 64
fi

serial="$1"
logcat_file="$2"
metadata_file="$3"
shift 4

if [[ $# -eq 0 || ! "$serial" =~ ^[[:alnum:]_.:-]+$ ]]; then
  printf 'A serial and command are required.\n' >&2
  exit 64
fi
if [[ -e "$logcat_file" || -e "$metadata_file" ]]; then
  printf 'Refusing to overwrite attempt capture files.\n' >&2
  exit 73
fi
if [[ ! -d "$(dirname "$logcat_file")" || ! -d "$(dirname "$metadata_file")" ]]; then
  printf 'Capture parent directories must already exist.\n' >&2
  exit 73
fi

host_time_before_epoch="$(date +%s.%N)" || exit 70
device_start_epoch="$(adb -s "$serial" shell date +%s | tr -d '\r')" || exit 70
host_time_after_epoch="$(date +%s.%N)" || exit 70
if [[ ! "$device_start_epoch" =~ ^[0-9]+$ \
  || ! "$host_time_before_epoch" =~ ^[0-9]+(\.[0-9]+)?$ \
  || ! "$host_time_after_epoch" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  printf 'Could not read epoch clocks for log capture.\n' >&2
  exit 70
fi
start_cursor="${device_start_epoch}.000"
adb -s "$serial" logcat -v epoch -T "$start_cursor" -s TinosDemo:I '*:S' \
  >"$logcat_file" 2>"${logcat_file}.stderr" &
logcat_pid=$!

cleanup() {
  runner_status=$?
  trap - EXIT INT TERM
  logcat_wait_status=unknown
  logcat_stopped=false
  if kill -0 "$logcat_pid" 2>/dev/null; then
    kill -TERM "$logcat_pid" 2>/dev/null || true
  fi
  wait "$logcat_pid" 2>/dev/null
  logcat_wait_status=$?
  logcat_stopped=true
  {
    printf 'serial=%s\n' "$serial"
    printf 'start_cursor=%s\n' "$start_cursor"
    printf 'device_start_epoch=%s\n' "$device_start_epoch"
    printf 'host_time_before_epoch=%s\n' "$host_time_before_epoch"
    printf 'host_time_after_epoch=%s\n' "$host_time_after_epoch"
    printf 'logcat_pid=%s\n' "$logcat_pid"
    printf 'runner_exit_status=%s\n' "$runner_status"
    printf 'logcat_stopped=%s\n' "$logcat_stopped"
    printf 'logcat_wait_status=%s\n' "$logcat_wait_status"
  } >>"$metadata_file" || {
    printf 'Failed to record capture shutdown metadata.\n' >&2
    if [[ "$runner_status" -eq 0 ]]; then
      runner_status=70
    fi
  }
  exit "$runner_status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

sleep 0.1
logcat_running=false
for running_pid in $(jobs -pr); do
  if [[ "$running_pid" == "$logcat_pid" ]]; then
    logcat_running=true
    break
  fi
done
if [[ "$logcat_running" != true ]]; then
  printf 'TinosDemo logcat failed to start; refusing to run the command.\n' >&2
  exit 70
fi

if ! printf 'serial=%s\nstart_cursor=%s\ndevice_start_epoch=%s\nhost_time_before_epoch=%s\nhost_time_after_epoch=%s\nlogcat_pid=%s\n' \
  "$serial" "$start_cursor" "$device_start_epoch" "$host_time_before_epoch" \
  "$host_time_after_epoch" "$logcat_pid" \
  >"$metadata_file"; then
  printf 'Failed to record capture startup metadata.\n' >&2
  exit 70
fi

"$@"
exit $?
