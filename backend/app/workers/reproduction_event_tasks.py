from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.collectors.asyncssh_adapter import DeviceCommandError, DeviceConnectionError
from app.capture_v2.runtime import assert_v1_live_capture_allowed
from app.contracts.enums import (
    CallVerdict, CaptureChannel, CaptureSegmentStatus, ChannelHealth, EvidenceCompleteness,
    EventType, ReproductionState, TimestampSource,
)
from app.core.config import settings
from app.core.errors import AppError
from app.db.models import (
    CaptureChannelHealth, CaseDevice, ReproductionAttempt, ReproductionEventRecord,
    ReproductionSession,
)
from app.db.session import SessionLocal
from app.integrations.credentials import get_credential_provider, LocalSecretCredentialProvider
from app.reproduction.fxs_event_monitor import FxsEventMonitor
from app.reproduction.barriers import ArmReadinessBarrier
from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode
from app.reproduction.signal_observer import binding_relative_ms, observe_pcap_signals
from app.services.events import emit_event
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)

# States during which the monitor should keep listening for FXS activity / call
# capture. The real-mode watcher also advances through CALL_DETECTED/CAPTURING
# while a media-bound call is being captured.
_LISTENING_STATES = {
    ReproductionState.WATCHING.value,
    ReproductionState.ACTIVITY_DETECTED.value,
    ReproductionState.CALL_DETECTED.value,
    ReproductionState.CAPTURING.value,
}


def _session_listening(db, session_id: str) -> ReproductionSession | None:
    # The watcher and high-priority cancel worker use different DB sessions.
    # Force an authoritative read instead of returning SQLAlchemy's identity-map
    # copy, otherwise a watcher can keep seeing WATCHING after cancel committed
    # CANCELLED and continue capture/heartbeat work against a released lock.
    row = db.scalar(
        select(ReproductionSession)
        .where(ReproductionSession.id == session_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    return row


def _ring_segment_retainable(observation) -> bool:
    """A UDP-filtered ring segment is evidence only when it has a packet.

    A valid classic-PCAP global header is exactly 24 bytes, so file length alone
    cannot distinguish an empty capture from evidence.
    """
    return observation.parse_error is None and observation.udp_packets > 0


def _latch_first_end_anchor(current_ms: int | None, observed_ms: int, *, reset: bool = False) -> int | None:
    """End anchors are edge-triggered: hook bounce must not move the first edge.

    ``reset=True`` starts a fresh activity cycle (a new OFFHOOK was observed), so
    any previously latched End Anchor is invalidated (returns None) and the next
    ONHOOK becomes a new first edge. Without this, a hang-up followed quickly by a
    re-off-hook that carries no DTMF (RP-H02) is misread as hook bounce of the
    earlier call and the follow-up ONHOOK is swallowed as "duplicate ONHOOK
    ignored" (real session 108d0325). Hook bounce WITHIN one cycle (no reset)
    still keeps the first edge.
    """
    if reset:
        return None
    return observed_ms if current_ms is None else current_ms


def _onhook_precedes_offhook(onhook_ts: str, last_offhook_ts: str | None) -> bool:
    """True when an ONHOOK event carries a device timestamp no later than the most
    recent OFFHOOK. The FXS monitor streams events timestamped by the DUT clock
    (``YYYY-MM-DD HH:MM:SS.ffffff``); poll() may deliver several events per batch
    and a *late* ONHOOK from a previous activity cycle can be processed AFTER an
    OFFHOOK reset. That stale ONHOOK must never re-latch the End Anchor (real
    session 16300ddf: R04's delayed ONHOOK(61745) re-latched after R02's OFFHOOK
    reset, so R02's real ONHOOK was swallowed as bounce). String comparison is
    valid because all timestamps share the DUT clock and fixed-width format.
    """
    if last_offhook_ts is None:
        return False
    return onhook_ts <= last_offhook_ts


def _should_restart_ring_after_end(state: str, active_call_id: str | None) -> bool:
    return active_call_id is None and state in {
        ReproductionState.WATCHING.value,
        ReproductionState.ACTIVITY_DETECTED.value,
    }


def _binding_precedes_end(*, binding_event: str | None, bind_ms: int,
                          pending_onhook_ms: int | None,
                          segment_start_ms: int | None = None,
                          attempt_start_ms: int | None = None) -> bool:
    """Whether a deterministic call-binding signal may create a Call now.

    A progressing-RTP fallback is only trusted when it precedes the End Anchor:
    RTP observed strictly after ONHOOK can be a tail packet from a previous call,
    not proof that a new call started before hangup.

    A SIP INVITE, however, is unambiguous call-setup evidence — it only ever
    appears while a call is being established — so it is trusted even when the
    segment observation (download + analysis) completes AFTER the FXS ONHOOK.
    Real DUTs show this on short calls (a few seconds): APF1250 eb9d7edb and
    APF3260 052884a5 both captured the INVITE in a segment whose observation lag
    the ONHOOK by 10+s; requiring bind_ms <= ONHOOK there wrongly dropped the
    call and marked the attempt INVALID. The INVITE itself was emitted *during*
    the call, so late observation must not discard it.

    To keep the late-SIP binding safe against a stale INVITE from the previous
    call being attributed to a fresh no-call attempt, the observed segment must
    belong to the current attempt: its start must be no earlier than the
    attempt's own OFFHOOK anchor (when the segment/attempt anchors are known).
    """
    if binding_event == 'SIP_INVITE':
        if segment_start_ms is not None and attempt_start_ms is not None:
            return segment_start_ms >= attempt_start_ms
        return True
    return pending_onhook_ms is None or bind_ms <= pending_onhook_ms


def _persist_fxs_monitor_ready(db, session, orch) -> None:
    debug_health = db.scalar(select(CaptureChannelHealth).where(
        CaptureChannelHealth.session_id == session.id,
        CaptureChannelHealth.channel == CaptureChannel.DEBUG.value,
    ))
    if debug_health is not None:
        debug_health.status = ChannelHealth.HEALTHY.value
        debug_health.last_observed_at = datetime.now(timezone.utc)
        debug_health.health_json = {
            **(debug_health.health_json or {}),
            'reader_alive': True,
            'runtime_ready': True,
            'readiness_phase': 'WATCH_RUNTIME_READY',
            'debug_enable_acknowledged': True,
        }
    db.add(ReproductionEventRecord(
        session_id=session.id, case_id=session.case_id,
        event_type='FXS_MONITOR_READY', source='REAL_PLATFORM',
        session_relative_ms=orch.fxs_event_monitor.relative_ms(),
        timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
        payload_json={'reader_alive': True, 'debug_enable_acknowledged': True},
    ))
    emit_event(
        db,
        event_type=EventType.FXS_MONITOR_READY,
        case_id=session.case_id,
        entity_type='reproduction_session',
        entity_id=session.id,
        payload={
            'session_id': session.id,
            'runtime_ready': True,
            'reader_alive': True,
            'debug_enable_acknowledged': True,
        },
    )
    db.commit()
    try:
        from app.workers.device_provision_task import sync_case_card
        sync_case_card.apply_async(
            args=[session.case_id, 'FXS_MONITOR_READY'], queue='diagnosis')
    except Exception:
        log.exception('[repro %s] failed to enqueue Feishu ready card sync', session.id[:8])
    log.info('[repro %s] FXS_MONITOR_READY', session.id[:8])


def _persist_fxs_monitor_failed(db, session, orch, *, reason: str) -> None:
    debug_health = db.scalar(select(CaptureChannelHealth).where(
        CaptureChannelHealth.session_id == session.id,
        CaptureChannelHealth.channel == CaptureChannel.DEBUG.value,
    ))
    if debug_health is not None:
        debug_health.status = ChannelHealth.FAILED.value
        debug_health.health_json = {
            **(debug_health.health_json or {}),
            'reader_alive': False,
            'runtime_ready': False,
            'failure_reason': reason,
            'readiness_phase': 'NOT_READY',
        }
    session.capture_completeness = EvidenceCompleteness.PARTIAL.value
    payload = {'reason': reason, 'runtime_ready': False, 'session_id': session.id}
    db.add(ReproductionEventRecord(
        session_id=session.id, case_id=session.case_id,
        event_type='FXS_MONITOR_FAILED', source='REAL_PLATFORM',
        session_relative_ms=orch.fxs_event_monitor.relative_ms(),
        timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
        payload_json=payload,
    ))
    emit_event(
        db,
        event_type=EventType.FXS_MONITOR_FAILED,
        case_id=session.case_id,
        entity_type='reproduction_session',
        entity_id=session.id,
        payload=payload,
    )
    db.commit()
    try:
        from app.workers.device_provision_task import sync_case_card
        sync_case_card.apply_async(
            args=[session.case_id, 'FXS_MONITOR_FAILED'], queue='diagnosis')
    except Exception:
        log.exception('[repro %s] failed to enqueue Feishu failure card sync', session.id[:8])


def _device_lock_reassigned(db, session) -> bool:
    """True if another session now holds the device's ACTIVE diagnostic lock.

    D1: a stale watcher (from a cancelled/superseded session) must not race the
    new session's watcher for the same device — that concurrency previously caused
    SSH_COMMAND_TIMEOUT when two watchers tcpdump'd the device at once.
    """
    from sqlalchemy import select
    from app.db.models import DeviceDiagnosticLock
    from app.contracts.enums import LockStatus
    from datetime import datetime, timezone
    lock = db.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.device_id == session.device_id))
    if lock is None or lock.session_id == session.id:
        return False
    expires = lock.lease_expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return lock.status == LockStatus.ACTIVE.value and expires > datetime.now(timezone.utc)


async def _watch(session_id: str, *, max_seconds: int | None = None) -> dict:
    db = SessionLocal()
    try:
        session = _session_listening(db, session_id)
        if session is None:
            return {'status': 'SESSION_NOT_FOUND', 'session_id': session_id}
        device = db.get(CaseDevice, session.device_id)
        if device is None:
            return {'status': 'DEVICE_NOT_FOUND', 'session_id': session_id}

        # D1: do not connect the device if another session has since taken its lock.
        if _device_lock_reassigned(db, session):
            return {'status': 'DEVICE_LOCK_REASSIGNED', 'session_id': session.id}

        if resolve_platform_mode() == 'mock':
            configured=int(((session.effective_profile_snapshot or {}).get('timeouts') or {}).get('watching_timeout_seconds') or 900)
            return await _watch_mock(db, session, device, int(max_seconds or configured))
        # A stale queued V1 watcher must not become a second capture authority after
        # CAPTURE_ENGINE_VERSION flips to V2. Fail closed before _watch_real_v11
        # can construct/start the legacy segmented ring.
        assert_v1_live_capture_allowed()
        return await _watch_real_v11(db, session, device, max_seconds)
    finally:
        db.close()


async def _watch_mock(db, session, device, max_seconds: int) -> dict:
    """Mock-mode watcher: no device I/O; the mock platform reports no FXS events."""
    orch, _close = build_orchestrator(adapter=None, connect=False)
    events_handled = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < max_seconds:
            row = _session_listening(db, session.id)
            if row is None or row.state not in _LISTENING_STATES:
                break
            await asyncio.sleep(0.5)
    finally:
        _close()
    return {'status': 'DONE', 'session_id': session.id, 'events_handled': events_handled,
            'state': _session_listening(db, session.id).state if _session_listening(db, session.id) else 'GONE'}


async def _watch_real(db, session, device, max_seconds: int) -> dict:
    """Real-mode watcher: FXS events + real CALL analysis through the real platform.

    The platform owns the AsyncSSHDeviceAdapter on its dedicated bridge loop, so all
    asyncssh I/O (connect, AIM commands, raw AIM stream reads, PCM probes) share ONE
    event loop. A background reader pushes raw AIM chunks into a thread-safe queue
    and the synchronous orchestrator polls it — no cross-loop handoff, no deadlock.

    CALL-level analysis (the previously unconnected link) is driven by the real
    platform's media-binding signal: a call is bound when the PCM mirror stream
    (UDP 40000/50000) becomes active after an FXS OFFHOOK attempt, and ended when
    the FXS ONHOOK arrives while a call is bound. No AIM SIP plaintext is needed
    (verified: `de sip de` does not emit INVITE/BYE on the PTY).
    """
    provider = get_credential_provider()
    password = await provider.get_password(sn=device.sn, ip=device.ip)
    username = device.username
    if isinstance(provider, LocalSecretCredentialProvider):
        try:
            username = provider.resolve_username(ip=device.ip, fallback=username)
        except Exception:
            pass

    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
    from app.reproduction.quick import QuickAnalysisInput
    from app.contracts.enums import CallVerdict
    adapter = AsyncSSHDeviceAdapter(ip=device.ip, port=device.ssh_port, username=username, password=password)
    orch, _close = build_orchestrator(adapter=adapter, connect=True)
    try:
        # Start the bridge-loop AIM reader and wire its monitor into the orchestrator.
        orch.fxs_event_monitor = orch.platform.start_fxs_monitor()
        _persist_fxs_monitor_ready(db, session, orch)
        events_handled = 0
        calls_bound = 0
        calls_ended = 0
        active_call_id: str | None = None
        # Monotonic timestamp when the active call was bound; used for the ONHOOK
        # timeout fallback (B1): if the real DUT's ONHOOK is never observed within
        # ONHOOK_TIMEOUT_SECONDS, end the call anyway so the session cannot hang in
        # CAPTURING forever (the FXS ONHOOK is normally emitted, but a lost/delayed
        # event must not wedge the pipeline).
        call_bound_at: float | None = None
        ONHOOK_TIMEOUT_SECONDS = 90.0
        last_media_probe = 0.0
        media_probe_interval = 1.0
        last_media_capture = 0.0
        # The PCM mirror stream is bursty on real devices, so each build_live_probe
        # window is 8s; overlap the probes (every 2s) so a burst is very likely to be
        # inside at least one capture window during the conversation.
        media_capture_interval = 2.0
        started = time.monotonic()
        # Resolve the voice runtime context ONCE, before the watch loop. It requires
        # execute_cli on the same AIM PTY the FXS reader is draining; doing it once
        # is safe, doing it every iteration steals FXS events (see the loop NOTE).
        _VOICE_CTX = orch.platform.resolve_voice_context(device) if hasattr(orch.platform, 'resolve_voice_context') else None
        try:
            while time.monotonic() - started < max_seconds:
                row = _session_listening(db, session.id)
                if row is None:
                    break
                state = ReproductionState(row.state)
                # Continue listening only in the watching/activity/call states; stop
                # when the session left for capture/analysis/cleanup or finished.
                if state in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED,
                             ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                    pass
                else:
                    break
                # NOTE: voice context is resolved ONCE (before the loop) and reused.
                # Re-resolving here every iteration would run execute_cli on the SAME
                # AIM PTY as the FXS reader, and that CLI output read consumes any
                # OFFHOOK/ONHOOK lines arriving at that moment -> ONHOOK is lost and
                # the call never ends (session stuck in CAPTURING). The voice context
                # (vlan/interface/gateway) does not change during a session, so a
                # single resolution is correct and safe.
                ctx = _VOICE_CTX

                # 1. Poll FXS events (OFFHOOK -> record_activity; ONHOOK with no bound
                #    call -> end_activity_without_call inside record_fxs_event).
                for ev in orch.fxs_event_monitor.poll():
                    log.info('[repro %s] FXS %s%s', session.id[:8], ev.event,
                             f'<{ev.digit}>' if ev.event == 'DTMF' else '')
                    handled = orch.record_fxs_event(db, session=row, event=ev, actor='reproduction-worker')
                    if handled is not None:
                        events_handled += 1
                    db.commit()
                    # FXS ONHOOK while a call is bound -> REAL end_call (CALL_QUICK
                    # analysis on the real capture). record_fxs_event does not consume
                    # ONHOOK in CALL_DETECTED/CAPTURING, so we handle it here.
                    if ev.event == 'ONHOOK' and active_call_id is not None:
                        now_state = ReproductionState(_session_listening(db, session.id).state)
                        if now_state in {ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                            rel = orch.fxs_event_monitor.relative_ms()
                            log.info('[repro %s] ONHOOK -> end_call %s', session.id[:8], active_call_id[:8])
                            call, _decision = orch.end_call(
                                db, session=row, call_id=active_call_id, relative_ms=rel,
                                signal=QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE, findings=()),
                                end_anchor='FXS_ONHOOK', actor='reproduction-worker',
                            )
                            active_call_id = None
                            call_bound_at = None
                            calls_ended += 1
                            db.commit()

                now = time.monotonic()

                # 1.5 ONHOOK timeout fallback (B1): the real DUT emits ONHOOK on hangup,
                # but if the event is ever lost/delayed the session must not wedge in
                # CAPTURING forever. End the bound call after a generous timeout.
                if (active_call_id is not None and call_bound_at is not None
                        and (now - call_bound_at) >= ONHOOK_TIMEOUT_SECONDS):
                    cur_state = ReproductionState(_session_listening(db, session.id).state)
                    if cur_state in {ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                        rel = orch.fxs_event_monitor.relative_ms()
                        call, _decision = orch.end_call(
                            db, session=row, call_id=active_call_id, relative_ms=rel,
                            signal=QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE, findings=()),
                            end_anchor='FXS_ONHOOK_TIMEOUT', actor='reproduction-worker',
                        )
                        active_call_id = None
                        call_bound_at = None
                        calls_ended += 1
                        db.commit()

                # 2. Periodic media probe: if an FXS attempt is active (ACTIVITY_DETECTED)
                #    and no call is bound yet, bind on the PCM mirror stream becoming live.
                if (now - last_media_probe) >= media_probe_interval:
                    last_media_probe = now
                    cur_state = ReproductionState(_session_listening(db, session.id).state)
                    if (cur_state == ReproductionState.ACTIVITY_DETECTED and active_call_id is None
                            and orch.platform.pcm_media_active(context=ctx)):
                        try:
                            rel = orch.fxs_event_monitor.relative_ms()
                            call = orch.bind_call(
                                db, session=row, relative_ms=rel,
                                external_call_ref=orch.platform.media_binding_call_ref(),
                                binding_event='RTP_STREAM_START', actor='reproduction-worker',
                            )
                            active_call_id = call.id
                            call_bound_at = now
                            calls_bound += 1
                            log.info('[repro %s] PCM live -> CALL_BOUND call=%s', session.id[:8], call.id[:8])
                            db.commit()
                        except Exception as exc:
                            # bind_call may fail on a transient device SSH delay (e.g.
                            # its synchronous live-probe tcpdump timing out while the
                            # media probe holds a channel). Do NOT crash the watcher —
                            # that would lose pending FXS events (ONHOOK) and leave the
                            # session wedged in ACTIVITY_DETECTED. Roll back any partial
                            # call-creation state and keep listening so the next probe
                            # can retry the bind.
                            log.exception('bind_call failed (transient); continuing to watch')
                            try:
                                db.rollback()
                            except Exception:
                                pass

                # 3. Periodic media accumulation during the conversation: while a call is
                #    bound and still capturing, keep appending short PCM segments so the
                #    merged capture spans the whole call (not just the bind_call window).
                #    Probes are spawned ASYNC (A1) so the loop keeps polling FXS events
                #    during the capture window instead of blocking on tcpdump.
                if active_call_id is not None and (now - last_media_capture) >= media_capture_interval:
                    last_media_capture = now
                    cur_state = ReproductionState(_session_listening(db, session.id).state)
                    if cur_state in {ReproductionState.CAPTURING, ReproductionState.CALL_DETECTED}:
                        rel = orch.fxs_event_monitor.relative_ms()
                        call_id = active_call_id

                        def persist_live(pcap: bytes):
                            # Durably persist an in-call PCM segment on the bridge
                            # thread as a retained CaptureSegment, so a watcher crash
                            # cannot lose the in-call media: the final compensation can
                            # rebuild the call pcap from the retained segment store even
                            # if the in-memory _live_pcap_cache was lost.
                            sdb = SessionLocal()
                            try:
                                srow = sdb.get(ReproductionSession, session.id)
                                if srow is None:
                                    return
                                rel2 = orch.fxs_event_monitor.relative_ms()
                                seg = orch.capture.append_pcap(
                                    sdb, session=srow, start_ms=rel2, end_ms=rel2 + 8000,
                                    data=pcap, attempt_id=None, call_id=call_id,
                                    metadata={'phase': 'LIVE_PROBE', 'persisted': True},
                                )
                                orch.capture.preserve_new_segment(sdb, session=srow, row=seg)
                                sdb.commit()
                            except Exception as exc:
                                log.exception('[repro %s] persist_live FAILED call=%s: %s',
                                              session.id[:8], call_id[:8], exc)
                                try:
                                    sdb.rollback()
                                except Exception:
                                    pass
                            finally:
                                sdb.close()

                        orch.platform.spawn_live_probe(
                            context=ctx, start_ms=int(rel), call_id=call_id, on_segment=persist_live,
                        )
                        log.info('[repro %s] in-call live probe spawned call=%s', session.id[:8], call_id[:8])
                        db.commit()

                if events_handled == 0:
                    await asyncio.sleep(0.2)
        finally:
            try:
                orch.platform.stop_fxs_monitor()
            except Exception:
                pass
        return {'status': 'DONE', 'session_id': session.id, 'events_handled': events_handled,
                'calls_bound': calls_bound, 'calls_ended': calls_ended,
                'state': _session_listening(db, session.id).state if _session_listening(db, session.id) else 'GONE'}
    finally:
        _close()


async def _watch_real_v11(db, session, device, max_seconds: int | None) -> dict:
    """Capability-aware segmented-ring watcher.

    One asynchronous full-UDP segment is active before and during an Attempt. PCM
    validates the capture data plane only; SIP INVITE or progressing RTP binds a
    Call. ONHOOK is deferred until the open segment has been inspected so a late
    binding signal cannot be discarded as an invalid Attempt.
    """
    provider = get_credential_provider()
    password = await provider.get_password(sn=device.sn, ip=device.ip)
    username = device.username
    if isinstance(provider, LocalSecretCredentialProvider):
        try:
            username = provider.resolve_username(ip=device.ip, fallback=username)
        except Exception:
            pass

    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
    from app.reproduction.quick import QuickAnalysisInput

    adapter = AsyncSSHDeviceAdapter(
        ip=device.ip, port=device.ssh_port, username=username, password=password)
    orch, close = build_orchestrator(adapter=adapter, connect=True)
    events_handled = calls_bound = calls_ended = 0
    active_call_id: str | None = None
    pending_onhook_ms: int | None = None
    last_offhook_ts: str | None = None
    call_bound_at: float | None = None
    segment_future = None
    segment_is_final_drain = False
    ring_sealed = False
    timeout_kind: str | None = None
    data_plane_validation_recorded = False
    try:
        orch.fxs_event_monitor = orch.platform.start_fxs_monitor()
        _persist_fxs_monitor_ready(db, session, orch)
        profile = session.effective_profile_snapshot or {}
        timeouts = profile.get('timeouts') or {}
        ring = profile.get('ring') or {}
        arm_barrier = profile.get('arm_barrier') or {}
        watch_limit = int(max_seconds or timeouts.get('watching_timeout_seconds') or 900)
        capture_limit = int(timeouts.get('max_capture_seconds') or 900)
        heartbeat_seconds = int(timeouts.get('heartbeat_seconds') or 15)
        segment_seconds = int(ring.get('segment_seconds') or 5)
        validation_seconds = int(arm_barrier.get('first_activity_validation_seconds') or 10)
        context = orch.platform.resolve_voice_context(device)
        started = last_heartbeat = time.monotonic()
        activity_deadline: float | None = None
        activity_degraded_reported = False
        segment_no = 0
        segment_start_ms = 0

        def start_segment(*, final_drain: bool = False):
            nonlocal segment_future, segment_no, segment_start_ms, segment_is_final_drain
            segment_no += 1
            segment_start_ms = orch.fxs_event_monitor.relative_ms()
            segment_is_final_drain = final_drain
            segment_future = orch.platform.spawn_ring_segment(
                context=context, seconds=segment_seconds,
                segment_key=f'{session.id[:8]}_{segment_no:06d}',
            )

        def health(channel: CaptureChannel, count: int):
            row = db.scalar(select(CaptureChannelHealth).where(
                CaptureChannelHealth.session_id == session.id,
                CaptureChannelHealth.channel == channel.value,
            ))
            if row is None:
                row = CaptureChannelHealth(session_id=session.id, channel=channel.value)
                db.add(row)
            row.packet_count = int(row.packet_count or 0) + int(count)
            if count > 0:
                row.status = ChannelHealth.HEALTHY.value
                row.last_observed_at = datetime.now(timezone.utc)
            detail = dict(row.health_json or {})
            detail.update({
                'packet_count': row.packet_count,
                'advancing': count > 0,
                'verification_pending': False if count > 0 else detail.get('verification_pending', True),
                'readiness_phase': 'DATA_PLANE_VERIFIED' if count > 0 else detail.get('readiness_phase', 'CAPTURE_PATH_READY'),
            })
            row.health_json = detail

        def pcm_verified() -> bool:
            rows = {item.channel: item for item in db.scalars(select(CaptureChannelHealth).where(
                CaptureChannelHealth.session_id == session.id,
                CaptureChannelHealth.channel.in_([
                    CaptureChannel.PCM_RX.value, CaptureChannel.PCM_TX.value]),
            ))}
            return all(int((rows.get(name) and rows[name].packet_count) or 0) > 0
                       for name in (CaptureChannel.PCM_RX.value, CaptureChannel.PCM_TX.value))

        start_segment()
        try:
            while True:
                row = _session_listening(db, session.id)
                if row is None or row.state not in _LISTENING_STATES:
                    break
                if (hasattr(orch.platform, 'fxs_monitor_healthy')
                        and not orch.platform.fxs_monitor_healthy()):
                    _persist_fxs_monitor_failed(
                        db, row, orch, reason='FXS_MONITOR_STOPPED')
                    timeout_kind = ('CAPTURE_TIMEOUT' if ReproductionState(row.state) in {
                        ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING,
                    } else 'WATCH_TIMEOUT')
                    log.error('[repro %s] FXS monitor unhealthy; fail closed', session.id[:8])
                    break

                for event in orch.fxs_event_monitor.poll():
                    log.info('[repro %s] FXS %s%s', session.id[:8], event.event,
                             f'<{event.digit}>' if event.event == 'DTMF' else '')
                    if event.event == 'ONHOOK':
                        observed_onhook_ms = orch.fxs_event_monitor.relative_ms()
                        # A poll() batch can deliver a stale ONHOOK from a previous
                        # activity cycle AFTER a newer OFFHOOK reset (real session
                        # 16300ddf: R04's delayed ONHOOK re-latched after R02's
                        # OFFHOOK, so R02's real ONHOOK was swallowed as bounce).
                        # Such an ONHOOK, whose device timestamp is no later than the
                        # most recent OFFHOOK, must not re-latch the End Anchor.
                        if _onhook_precedes_offhook(event.timestamp, last_offhook_ts):
                            log.info('[repro %s] stale ONHOOK before latest OFFHOOK ignored ts=%s',
                                     session.id[:8], event.timestamp)
                            continue
                        first_onhook = pending_onhook_ms is None
                        pending_onhook_ms = _latch_first_end_anchor(
                            pending_onhook_ms, observed_onhook_ms)
                        if not first_onhook:
                            log.info('[repro %s] duplicate ONHOOK ignored observed_ms=%s latched_ms=%s',
                                     session.id[:8], observed_onhook_ms, pending_onhook_ms)
                            continue
                        # COMPLETE is provisional until every file covering the End
                        # Anchor has been sealed, transferred and inspected.
                        row.capture_completeness = EvidenceCompleteness.PARTIAL.value
                        if not ring_sealed:
                            # Stop acquisition immediately at the end anchor, but
                            # retain DUT files. The in-flight fetch plus one explicit
                            # final drain below must consume every sealed file before
                            # call finalization/cleanup removes the ring directory.
                            orch.platform.seal_segmented_ring(session.id)
                            ring_sealed = True
                        db.commit()
                        continue
                    handled = orch.record_fxs_event(
                        db, session=row, event=event, actor='reproduction-worker')
                    if handled is not None:
                        events_handled += 1
                    if event.event == 'OFFHOOK':
                        if not data_plane_validation_recorded:
                            activity_deadline = time.monotonic() + validation_seconds
                            activity_degraded_reported = False
                        # A fresh off-hook begins a new activity cycle: the previous
                        # End Anchor latch must be invalidated, otherwise a follow-up
                        # hang-up (e.g. H02 "hang then re-offhook 0.5s later" without
                        # any DTMF) is misclassified as hook bounce of the previous
                        # call and swallowed as "duplicate ONHOOK ignored". Real
                        # session 108d0325: consecutive fast no-DTMF off/on-hooks
                        # were merged into the first call's window because the latch
                        # stayed non-None after the prior on-hook.
                        if pending_onhook_ms is not None:
                            log.info('[repro %s] new OFFHOOK resets previous End Anchor latch ms=%s',
                                     session.id[:8], pending_onhook_ms)
                        pending_onhook_ms = _latch_first_end_anchor(
                            pending_onhook_ms, 0, reset=True)
                        # Record the latest OFFHOOK device timestamp so a stale
                        # ONHOOK arriving in a later poll batch (but carrying an
                        # earlier DUT timestamp) cannot re-latch this cycle's
                        # End Anchor. The max() guards against out-of-order lines
                        # within a single poll batch.
                        if last_offhook_ts is None or event.timestamp > last_offhook_ts:
                            last_offhook_ts = event.timestamp
                    db.commit()

                now = time.monotonic()
                if segment_future is not None and segment_future.done():
                    # Cancel may commit after the loop-head read while a segment
                    # finishes. Do not persist that segment into a terminal session.
                    row = _session_listening(db, session.id)
                    if row is None or row.state not in _LISTENING_STATES:
                        segment_future = None
                        break
                    capture = segment_future.result()
                    end_ms = orch.fxs_event_monitor.relative_ms()
                    attempt = db.scalar(select(ReproductionAttempt).where(
                        ReproductionAttempt.session_id == session.id,
                        ReproductionAttempt.status == 'ACTIVE',
                    ).order_by(ReproductionAttempt.attempt_no.desc()))
                    segment = orch.capture.append_pcap(
                        db, session=row, start_ms=segment_start_ms, end_ms=end_ms,
                        data=capture.pcap, attempt_id=attempt.id if attempt else None,
                        call_id=active_call_id,
                        metadata={'phase': 'SEGMENTED_RING', 'segment_seconds': segment_seconds},
                    )
                    observation = observe_pcap_signals(segment.local_path)
                    segment.metadata_json = {
                        **(segment.metadata_json or {}),
                        'signal_observation': observation.as_dict(),
                    }
                    if _ring_segment_retainable(observation):
                        orch.capture.preserve_new_segment(db, session=row, row=segment)
                    else:
                        # Native tcpdump can seal a header-only 24-byte PCAP before
                        # the first Voice UDP packet. It is health state, never
                        # immutable evidence and never a finalize/merge input.
                        segment.status = CaptureSegmentStatus.EVICTED.value
                        segment.retained = False
                        segment.metadata_json = {
                            **(segment.metadata_json or {}),
                            'capture_empty': True,
                            'capture_empty_reason': 'NO_UDP_PACKETS',
                        }
                    orch.capture.evict_ring(db, session=row, current_end_ms=end_ms)
                    health(CaptureChannel.PCAP, observation.udp_packets)
                    health(CaptureChannel.PCM_RX, observation.pcm_rx_packets)
                    health(CaptureChannel.PCM_TX, observation.pcm_tx_packets)
                    if (pending_onhook_ms is None and pcm_verified()
                            and not data_plane_validation_recorded):
                        ArmReadinessBarrier.persist_activity_data_plane_validation(
                            db, session=row)
                        db.add(ReproductionEventRecord(
                            session_id=row.id, case_id=row.case_id,
                            event_type='PCM_DATA_PLANE_VERIFIED', source='PCAP_SIGNAL_OBSERVER',
                            session_relative_ms=end_ms, timestamp_source=TimestampSource.PCAP.value,
                            payload_json={
                                **observation.as_dict(),
                                'readiness_phase': 'DATA_PLANE_VERIFIED',
                            },
                        ))
                        data_plane_validation_recorded = True

                    # Make the segment and channel-health observations durable
                    # before call binding. A transient bind failure may roll back
                    # its own state, but must never discard the capture itself.
                    db.commit()
                    row = _session_listening(db, session.id)
                    attempt = db.get(ReproductionAttempt, attempt.id) if attempt else None
                    bind_ms = binding_relative_ms(
                        observation,
                        segment_start_ms=segment_start_ms,
                        segment_end_ms=end_ms,
                    )

                    # RTP captured only after the pending ONHOOK is a tail packet,
                    # not proof that a call started before hangup.  Never create a
                    # call whose binding anchor is later than its end anchor.
                    #
                    # Short calls (a few seconds) expose a race: the deterministic
                    # SIP-INVITE signal is observed by segment download/analysis
                    # that can lag the FXS ONHOOK by 10+s on real DUTs (APF1250
                    # eb9d7edb / APF3260 052884a5: INVITE in the segment AFTER the
                    # ONHOOK). The INVITE itself was emitted *during* the call, so
                    # binding to it is correct even though the observation is late.
                    # A SIP INVITE is unambiguous call evidence (it only ever
                    # appears while a call is being set up), so it is trusted over
                    # the observed-vs-anchor ordering. RTP-only fallback keeps the
                    # strict check: progressing RTP observed only after ONHOOK can
                    # be a tail packet from a previous call, not new-call proof.
                    # For the late-SIP-INVITE binding to be safe we also require the
                    # observed segment to belong to the CURRENT attempt (its start
                    # is not before the attempt's own anchor) — this prevents a
                    # stale INVITE segment from the previous call being attributed
                    # to a fresh no-call attempt.
                    binding_precedes_end = _binding_precedes_end(
                        binding_event=observation.call_binding_event,
                        bind_ms=bind_ms,
                        pending_onhook_ms=pending_onhook_ms,
                        segment_start_ms=segment_start_ms,
                        attempt_start_ms=(attempt.start_anchor_ms if attempt else None),
                    )
                    if (attempt is not None and active_call_id is None
                            and observation.call_binding_event and binding_precedes_end):
                        try:
                            call = orch.bind_call(
                                db, session=row, relative_ms=bind_ms,
                                external_call_ref=observation.external_call_ref,
                                binding_event=observation.call_binding_event,
                                actor='reproduction-worker',
                            )
                            active_call_id = call.id
                            segment.call_id = call.id
                            call_bound_at = now
                            calls_bound += 1
                            log.info('[repro %s] %s -> CALL_BOUND call=%s',
                                     session.id[:8], observation.call_binding_event, call.id[:8])
                        except Exception:
                            log.exception('[repro %s] deterministic call binding failed', session.id[:8])
                            db.rollback()
                    db.commit()

                    # Inspect-before-invalidate: the segment above may contain an
                    # INVITE/RTP signal emitted immediately before ONHOOK.
                    if pending_onhook_ms is not None and not segment_is_final_drain:
                        # The in-flight downloader may have selected its file list
                        # just before ONHOOK sealed the producer. Always perform one
                        # post-seal drain to collect the former open file and backlog.
                        start_segment(final_drain=True)
                    elif (pending_onhook_ms is not None and segment_is_final_drain
                          and capture.remaining_files > 0):
                        log.info('[repro %s] tail drain continues remaining_files=%s',
                                 session.id[:8], capture.remaining_files)
                        start_segment(final_drain=True)
                    elif pending_onhook_ms is not None:
                        row = _session_listening(db, session.id)
                        row.capture_completeness = (
                            EvidenceCompleteness.COMPLETE.value
                            if pcm_verified() else EvidenceCompleteness.PARTIAL.value
                        )
                        db.add(ReproductionEventRecord(
                            session_id=row.id, case_id=row.case_id,
                            event_type='CAPTURE_TAIL_DRAINED', source='SEGMENTED_RING_DOWNLOADER',
                            session_relative_ms=pending_onhook_ms,
                            timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
                            payload_json={'remaining_files': 0, 'end_anchor': 'FXS_ONHOOK'},
                        ))
                        log.info('[repro %s] tail drain complete remaining_files=0', session.id[:8])
                        if active_call_id is not None and ReproductionState(row.state) in {
                            ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING,
                        }:
                            orch.end_call(
                                db, session=row, call_id=active_call_id,
                                relative_ms=pending_onhook_ms,
                                signal=QuickAnalysisInput(
                                    verdict=CallVerdict.INCONCLUSIVE, findings=()),
                                end_anchor='FXS_ONHOOK', actor='reproduction-worker',
                            )
                            active_call_id = None
                            call_bound_at = None
                            calls_ended += 1
                        elif attempt is not None and attempt.status == 'ACTIVE':
                            orch.end_activity_without_call(
                                db, session=row, attempt_id=attempt.id,
                                relative_ms=pending_onhook_ms,
                                end_anchor='FXS_ONHOOK', actor='reproduction-worker',
                            )
                        pending_onhook_ms = None
                        last_offhook_ts = None
                        db.commit()

                    row = _session_listening(db, session.id)
                    if (row is not None and ring_sealed
                            and _should_restart_ring_after_end(row.state, active_call_id)):
                        # A no-call Attempt returns to WATCHING. Its producer was
                        # sealed to drain the End Anchor, so remove that sealed ring
                        # and let start_segment() create a fresh idle producer.
                        orch.platform.stop_segmented_ring(session.id)
                        ring_sealed = False
                        log.info('[repro %s] no-call tail drained; idle ring restarted',
                                 session.id[:8])
                    if pending_onhook_ms is not None:
                        # A final drain was started above; do not replace its future.
                        pass
                    elif row is not None and row.state in _LISTENING_STATES:
                        start_segment()
                    else:
                        segment_future = None

                if (activity_deadline is not None and not activity_degraded_reported
                        and not data_plane_validation_recorded
                        and now >= activity_deadline and not pcm_verified()):
                    row.capture_completeness = EvidenceCompleteness.PARTIAL.value
                    decision = ArmReadinessBarrier.persist_activity_data_plane_validation(
                        db, session=row)
                    db.add(ReproductionEventRecord(
                        session_id=row.id, case_id=row.case_id,
                        event_type='PCM_DATA_PLANE_DEGRADED', source='CAPTURE_HEALTH_MONITOR',
                        session_relative_ms=orch.fxs_event_monitor.relative_ms(),
                        timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
                        payload_json={
                            'reason': 'FIRST_ACTIVITY_PCM_NOT_VERIFIED',
                            'readiness_phase': decision.readiness_phase,
                            'failed_reasons': list(decision.failed_reasons),
                        },
                    ))
                    activity_degraded_reported = True
                    data_plane_validation_recorded = True
                    db.commit()

                if now - last_heartbeat >= heartbeat_seconds:
                    # A cancel can release the device lock between our refreshed
                    # state read and heartbeat. That is a normal external stop,
                    # not a watcher failure or retryable lease-expiry incident.
                    row = _session_listening(db, session.id)
                    if row is None or row.state not in _LISTENING_STATES:
                        break
                    try:
                        orch.heartbeat(db, session=row)
                        db.commit()
                    except AppError as exc:
                        if exc.code != 'REPRODUCTION_LEASE_EXPIRED':
                            raise
                        db.rollback()
                        row = _session_listening(db, session.id)
                        if row is not None and row.state in _LISTENING_STATES:
                            raise
                        log.info('[repro %s] external terminal state observed during heartbeat; watcher exits',
                                 session.id[:8])
                        break
                    last_heartbeat = now
                if active_call_id is None and now - started >= watch_limit:
                    timeout_kind = 'WATCH_TIMEOUT'
                    break
                if active_call_id is not None and call_bound_at is not None and now - call_bound_at >= capture_limit:
                    timeout_kind = 'CAPTURE_TIMEOUT'
                    break
                await asyncio.sleep(0.1)
        finally:
            try:
                orch.platform.stop_fxs_monitor()
            except Exception:
                pass
            if segment_future is not None and not segment_future.done():
                segment_future.cancel()

        row = _session_listening(db, session.id)
        if row is not None and timeout_kind == 'WATCH_TIMEOUT' and ReproductionState(row.state) in {
            ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED,
        }:
            orch.watch_timeout(db, session=row, actor='reproduction-worker')
            db.commit()
        elif row is not None and timeout_kind == 'CAPTURE_TIMEOUT' and ReproductionState(row.state) in {
            ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING,
        }:
            orch.capture_timeout(db, session=row, actor='reproduction-worker')
            db.commit()
        return {
            'status': 'DONE', 'session_id': session.id,
            'events_handled': events_handled, 'calls_bound': calls_bound,
            'calls_ended': calls_ended,
            'state': _session_listening(db, session.id).state if _session_listening(db, session.id) else 'GONE',
        }
    finally:
        close()


@celery_app.task(name='reproduction.watch_fxs_events', bind=True, queue='reproduction-watch',
                 autoretry_for=(DeviceConnectionError, DeviceCommandError),
                 retry_backoff=True, retry_backoff_max=30, max_retries=2)
def watch_fxs_events(self, session_id: str, max_seconds: int | None = None):
    """Watch a reproduction session's DUT for FXS activity and feed it to the
    orchestrator as real activity anchors. Runs until the session leaves the
    watching/activity-detected states or the timeout elapses.

    After the watcher finishes (session reached a terminal state — call captured
    and cleanup verified, or the watch window elapsed), hand the session to the
    diagnosis worker automatically, closing the "reproduction done but diagnosis
    must be clicked by hand" gap.
    """
    result = asyncio.run(_watch(session_id, max_seconds=max_seconds))
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    diag = ensure_reproduction_diagnosis(session_id)
    result['diagnosis'] = diag
    return result
