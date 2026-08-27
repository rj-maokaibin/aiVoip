from __future__ import annotations

import shlex

CONTROL_ROOT = "/tmp/aivoip_capture/control"


def q(value: object) -> str:
    return shlex.quote(str(value))


def _lock_prefix(operation_id: str) -> str:
    return f'''CONTROL={q(CONTROL_ROOT)}
LOCK="$CONTROL/op.lock"
mkdir -p "$CONTROL"
acquire_lock() {{
  if mkdir "$LOCK" 2>/dev/null; then return 0; fi
  oldpid=$(cat "$LOCK/owner_pid" 2>/dev/null || true)
  oldst=$(cat "$LOCK/owner_starttime" 2>/dev/null || true)
  # A freshly-created mkdir lock has a tiny metadata initialization window.
  # Never treat an incomplete lock as stale immediately; give its owner one
  # conservative second to publish PID/starttime before stale recovery.
  if [ -z "$oldpid" ] || [ -z "$oldst" ]; then
    sleep 1
    oldpid=$(cat "$LOCK/owner_pid" 2>/dev/null || true)
    oldst=$(cat "$LOCK/owner_starttime" 2>/dev/null || true)
  fi
  if [ -n "$oldpid" ] && [ -r "/proc/$oldpid/stat" ]; then
    curst=$(awk '{{print $22}}' "/proc/$oldpid/stat" 2>/dev/null || true)
    if [ -n "$curst" ] && [ "$curst" = "$oldst" ]; then
      echo AIVOIP_LOCK_BUSY
      exit 75
    fi
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || {{ echo AIVOIP_LOCK_BUSY; exit 75; }}
}}
acquire_lock
selfpid=$$
selfst=$(awk '{{print $22}}' "/proc/$$/stat" 2>/dev/null || echo 0)
printf '%s' "$selfpid" > "$LOCK/owner_pid"
printf '%s' "$selfst" > "$LOCK/owner_starttime"
printf '%s' {q(operation_id)} > "$LOCK/operation_id"
printf '%s' "$(date +%s 2>/dev/null || echo 0)" > "$LOCK/created_at"
cleanup_lock() {{ rm -rf "$LOCK"; }}
trap cleanup_lock EXIT INT TERM
'''


def publish_fence_script(*, lease_epoch: int, session_id: str, owner_worker: str, boot_id: str, operation_id: str) -> str:
    requested_epoch = int(lease_epoch)
    return _lock_prefix(operation_id) + f'''
requested={q(requested_epoch)}
current=$(cat "$CONTROL/lease_epoch" 2>/dev/null || true)
if [ -n "$current" ]; then
  case "$current" in *[!0-9]*) echo AIVOIP_FENCE_CORRUPT; exit 78 ;; esac
  if [ "$current" -gt "$requested" ]; then
    echo AIVOIP_FENCED
    exit 73
  fi
  if [ "$current" -eq "$requested" ]; then
    current_session=$(cat "$CONTROL/session_id" 2>/dev/null || true)
    current_owner=$(cat "$CONTROL/owner_worker" 2>/dev/null || true)
    if [ -n "$current_session" ] && [ "$current_session" != {q(session_id)} ]; then
      echo AIVOIP_FENCED
      exit 73
    fi
    if [ -n "$current_owner" ] && [ "$current_owner" != {q(owner_worker)} ]; then
      echo AIVOIP_FENCED
      exit 73
    fi
  fi
fi
write_atomic() {{ path="$1"; value="$2"; tmp="$path.tmp.$$"; printf '%s' "$value" > "$tmp" && mv "$tmp" "$path"; }}
write_atomic "$CONTROL/lease_epoch" "$requested"
write_atomic "$CONTROL/session_id" {q(session_id)}
write_atomic "$CONTROL/owner_worker" {q(owner_worker)}
write_atomic "$CONTROL/boot_id" {q(boot_id)}
echo AIVOIP_FENCE_PUBLISHED
'''


def fenced_script(*, lease_epoch: int, operation_id: str, body: str) -> str:
    return _lock_prefix(operation_id) + f'''
current=$(cat "$CONTROL/lease_epoch" 2>/dev/null || true)
if [ "$current" != {q(lease_epoch)} ]; then
  echo AIVOIP_FENCED
  exit 73
fi
{body}
'''


def release_fence_script(*, lease_epoch: int, operation_id: str) -> str:
    """Fenced removal of the DUT-side capture fence.

    Only the current lease authority may clear it, so a completed session leaves
    the DUT pristine and a stale worker cannot un-fence a live capture.  Removing
    the control files is the durable "capture fence released" signal for the next
    reproduction to publish a fresh epoch.
    """
    return _lock_prefix(operation_id) + f'''
current=$(cat "$CONTROL/lease_epoch" 2>/dev/null || true)
if [ "$current" != {q(lease_epoch)} ]; then
  echo AIVOIP_FENCED
  exit 73
fi
rm -f "$CONTROL/lease_epoch" "$CONTROL/session_id" "$CONTROL/owner_worker" "$CONTROL/boot_id"
echo AIVOIP_FENCE_RELEASED
'''


def clear_stale_fence_script(*, operation_id: str) -> str:
    """Unfenced removal of stale capture fence state.

    Caller must already have proven (via recovery scan) that no live capture
    producer exists on the DUT; otherwise the strict publish fence is preserved.
    """
    return _lock_prefix(operation_id) + f'''
rm -f "$CONTROL/lease_epoch" "$CONTROL/session_id" "$CONTROL/owner_worker" "$CONTROL/boot_id"
echo AIVOIP_STALE_FENCE_CLEARED
'''
