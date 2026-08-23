from __future__ import annotations

from typing import Any

from app.capture_v2.producer.identity import parse_process_record
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


CONTROL_ROOT = "/tmp/aivoip_capture/control"
_FENCE_FILES = ("lease_epoch", "session_id", "owner_worker", "boot_id")


def stale_fence_cleanup_script() -> str:
    """Return the fail-closed DUT mutation used only by activation rehearsal.

    A bounded rehearsal uses an isolated PostgreSQL project, so its lease epoch can
    legitimately restart at 1 while /tmp on the DUT still carries a higher fence
    epoch from an earlier rehearsal.  Production fencing must remain monotonic, so
    the generic publish_fence path must never reset that state.  This script is a
    deliberately narrow rehearsal preflight: it refuses to run while an operation
    lock is live or while an AIVOIP tcpdump producer is visible, and deletes only
    the control-plane fence identity files after those checks pass.
    """
    return r'''
CONTROL=/tmp/aivoip_capture/control
LOCK="$CONTROL/op.lock"

# Never disturb an in-flight fenced mutation.  A stale mkdir lock is removable
# only after PID + /proc starttime prove that its original owner is no longer live.
if [ -d "$LOCK" ]; then
  oldpid=$(cat "$LOCK/owner_pid" 2>/dev/null || true)
  oldst=$(cat "$LOCK/owner_starttime" 2>/dev/null || true)
  if [ -n "$oldpid" ] && [ -n "$oldst" ] && [ -r "/proc/$oldpid/stat" ]; then
    curst=$(awk '{print $22}' "/proc/$oldpid/stat" 2>/dev/null || true)
    if [ -n "$curst" ] && [ "$curst" = "$oldst" ]; then
      echo AIVOIP_REHEARSAL_OP_LOCK_BUSY
      exit 75
    fi
  fi
  rm -rf "$LOCK"
fi

# Re-check directly on the DUT immediately before mutation.  Do not rely only on
# the controller-side process snapshot because another process could appear in the
# small interval between read and mutation.
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  cmd=$(tr '\000' ' ' < "$p/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *tcpdump*'/tmp/aivoip_capture/epochs/'*)
      echo AIVOIP_REHEARSAL_PRODUCER_PRESENT
      exit 76
      ;;
  esac
done

mkdir -p "$CONTROL"
rm -f \
  "$CONTROL/lease_epoch" \
  "$CONTROL/session_id" \
  "$CONTROL/owner_worker" \
  "$CONTROL/boot_id"
echo AIVOIP_REHEARSAL_STALE_FENCE_CLEARED
'''


async def clear_stale_fence_for_rehearsal(adapter: Any) -> dict[str, Any]:
    """Clear only stale rehearsal fence metadata after proving the DUT is idle.

    The caller must already be running under the explicit V2 activation-rehearsal
    contract.  This function independently re-checks that contract so it cannot be
    reused as a production escape hatch.
    """
    from app.capture_v2.runtime import assert_selected_v2_live_capture_allowed
    from app.core.config import settings

    selected = assert_selected_v2_live_capture_allowed()
    if selected.get("mode") != "ACTIVATION_REHEARSAL":
        raise RuntimeError("REHEARSAL_STALE_FENCE_CLEAR_NOT_AUTHORIZED")
    if bool(settings.capture_v2_production_enabled):
        raise RuntimeError("REHEARSAL_STALE_FENCE_CLEAR_PRODUCTION_V2_ENABLED")

    reader = ReadOnlyDeviceTransport(adapter)
    records = await reader.list_tcpdump_processes()
    identities = [parse_process_record(row.pid, row.starttime, row.cmdline) for row in records]
    owned = [item for item in identities if item.owned_by_aivoip]
    if owned:
        raise RuntimeError(
            "REHEARSAL_STALE_FENCE_CLEAR_PRODUCER_PRESENT:"
            + ",".join(str(item.pid) for item in owned)
        )

    previous = {
        name: await reader.read_text(f"{CONTROL_ROOT}/{name}", missing_ok=True)
        for name in _FENCE_FILES
    }
    had_lock = await reader.run(f"[ -d {CONTROL_ROOT}/op.lock ] && echo yes || echo no")

    result = await adapter.execute_shell(stale_fence_cleanup_script(), retries=0)
    status = int(result.exit_status or 0)
    if status == 75:
        raise RuntimeError("REHEARSAL_STALE_FENCE_CLEAR_OP_LOCK_BUSY")
    if status == 76:
        raise RuntimeError("REHEARSAL_STALE_FENCE_CLEAR_PRODUCER_RACE")
    if status != 0:
        raise RuntimeError(
            f"REHEARSAL_STALE_FENCE_CLEAR_FAILED:exit={status}:"
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
        )

    remaining = {
        name: await reader.read_text(f"{CONTROL_ROOT}/{name}", missing_ok=True)
        for name in _FENCE_FILES
    }
    if any(value is not None for value in remaining.values()):
        raise RuntimeError("REHEARSAL_STALE_FENCE_CLEAR_VERIFY_FAILED")

    records_after = await reader.list_tcpdump_processes()
    owned_after = [
        parse_process_record(row.pid, row.starttime, row.cmdline)
        for row in records_after
        if parse_process_record(row.pid, row.starttime, row.cmdline).owned_by_aivoip
    ]
    if owned_after:
        raise RuntimeError("REHEARSAL_STALE_FENCE_CLEAR_POSTCHECK_PRODUCER_PRESENT")

    return {
        "cleared": True,
        "scope": "ACTIVATION_REHEARSAL_ONLY",
        "production_v2_enabled": False,
        "producer_count_before": 0,
        "producer_count_after": 0,
        "op_lock_present_before": had_lock.strip() == "yes",
        "previous_fence": previous,
        "remaining_fence": remaining,
    }
