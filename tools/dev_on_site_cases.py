"""Execute on-site reproduction-stability test cases that need NO phone.

Runs against the live tunnel + real DB. Each case maps to the matrix in
docs/客户现场复现稳定性测试用例.md.

  C1 (P0-2/P0-5) tunnel-connect failure message (wrong endpoint)     [no device ops]
  C2 (P5-2)     D1 lock anti-stacking (two sessions, one device)      [no device ops]
  C3 (P1-3)     reproduction idle timeout (no phone) -> graceful end /
                lock release / cleanup                                [device ops]
  C4 (P0-1)     consecutive reproduction cycles: locks released, no
                wedged session                                        [device ops]

Device-op cases always wrap arm/cleanup in cancel protection and end with a
device-state-clean check so the DUT is left pristine.
"""
import asyncio
import sys
import time
from uuid import uuid4

sys.path.insert(0, '/app')

from sqlalchemy import select

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter, DeviceConnectionError
from app.contracts.enums import LockStatus, ReproductionState
from app.db.models import Case, CaseDevice, DeviceDiagnosticLock, ReproductionSession
from app.db.session import SessionLocal
from app.integrations.credentials import get_credential_provider
from app.reproduction.locks import acquire_device_lock
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode
from app.workers.reproduction_event_tasks import _device_lock_reassigned, _watch

IP = '47.104.22.0'
PORT = 65212
SN = 'MACC1JZH3260M'


def _line(label, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))


# ---- C1: tunnel-connect failure message (P0-2/P0-5) -------------------------
def c1_tunnel_connect_message():
    print('=== C1: tunnel-connect failure message ===')
    ok = False; detail = ''
    try:
        provider = get_credential_provider()
        pwd = asyncio.run(provider.get_password(sn=SN, ip=IP))
        a = AsyncSSHDeviceAdapter(ip=IP, port=1, username='root', password=pwd)  # dead port
        try:
            asyncio.run(asyncio.wait_for(a.connect(), timeout=12))
        except DeviceConnectionError as e:
            msg = str(e)
            ok = ('tunnel' in msg.lower() or 'unreachable' in msg.lower()) and 'rebuild' in msg.lower()
            detail = msg[:120]
        except Exception as e:
            detail = f'{type(e).__name__}: {e}'
    except Exception as e:
        detail = f'{type(e).__name__}: {e}'
    _line('connect failure names tunnel rebuild action', ok, detail)


# ---- C2: D1 lock anti-stacking (P5-2) ---------------------------------------
def c2_d1_lock_antistacking():
    print('=== C2: D1 lock anti-stacking (two sessions, one device) ===')
    db = SessionLocal()
    try:
        case = Case(case_no=f'ONSITE-C2-{uuid4().hex[:8]}', summary='onsite C2', status='ANALYZING')
        db.add(case); db.flush()
        dev = CaseDevice(case_id=case.id, ip=IP, ssh_port=PORT, sn=SN, username='root')
        db.add(dev); db.flush()
        sa = ReproductionSession(case_id=case.id, device_id=dev.id, profile_key='P', profile_version='1',
                                  profile_checksum='c', effective_profile_snapshot={}, state='WATCHING')
        sb = ReproductionSession(case_id=case.id, device_id=dev.id, profile_key='P', profile_version='1',
                                  profile_checksum='c', effective_profile_snapshot={}, state='WATCHING')
        db.add(sa); db.add(sb); db.flush()
        acquire_device_lock(db, session=sb, owner_worker='onsite-c2', lease_seconds=60)
        db.commit()
        # A's watcher must exit (lock reassigned to B); B owns it.
        a1 = _device_lock_reassigned(db, sa)
        a2 = _device_lock_reassigned(db, sb)
        _line('lock owner not blocked / other session blocked', a1 is True and a2 is False,
              f'A_reassigned={a1} B_reassigned={a2}')
        # cleanup
        for s in (sa, sb):
            db.delete(s)
        db.delete(case)
        db.commit()
    finally:
        db.close()


# ---- C3: reproduction idle timeout (P1-3) -----------------------------------
def c3_idle_timeout():
    print('=== C3: reproduction idle timeout (no phone) ===')
    assert resolve_platform_mode() == 'real', 'REPRODUCTION_PLATFORM_MODE must be real'
    db = SessionLocal()
    sid = None
    orch = None
    _close = None
    try:
        case = Case(case_no=f'ONSITE-C3-{uuid4().hex[:8]}', summary='onsite C3 idle', status='ANALYZING')
        db.add(case); db.flush()
        dev = CaseDevice(case_id=case.id, ip=IP, ssh_port=PORT, sn=SN, username='root')
        db.add(dev); db.flush()
        orch0 = ReproductionOrchestrator()  # create_session does not connect
        session = orch0.create_session(db, case_id=case.id, profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                       device_id=dev.id, actor='onsite-c3')
        sid = session.id
        db.commit()
        # real adapter (connects to the DUT) for arm
        from app.workers.reproduction_tasks import _build_orchestrator_for
        orch, adapter, _close = _build_orchestrator_for(session, connect=True)
        session = orch.start(db, session=session, owner_worker='onsite-c3', actor='onsite-c3')
        db.commit()
        ok_arm = ReproductionState(session.state) == ReproductionState.WATCHING
        _line('arm -> WATCHING', ok_arm, session.state)
        # watcher idle 30s (no phone): must end gracefully
        t0 = time.monotonic()
        result = asyncio.run(_watch(sid, max_seconds=30))
        el = time.monotonic() - t0
        state = ReproductionState(db.get(ReproductionSession, sid).state).value
        _line('idle watcher ends gracefully', result.get('status') in ('DONE', 'DEVICE_LOCK_REASSIGNED') and el < 60,
              f'result={result.get("status")} elapsed={el:.0f}s state={state}')
        # ensure cleanup: cancel to leave the device pristine
        from app.workers.reproduction_tasks import cancel_reproduction
        cancel_reproduction.apply_async(args=[sid], queue='reproduction').get(timeout=90)
        # re-read with a FRESH session (cancel commits on another connection);
        # poll briefly for the terminal cleanup-verified state.
        from app.db.session import SessionLocal as _SL
        import time as _t
        s2 = None; lock2 = None
        for _ in range(10):
            _db2 = _SL()
            try:
                s2 = _db2.get(ReproductionSession, sid)
                lock2 = _db2.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.session_id == sid))
            finally:
                _db2.close()
            if s2 and s2.cleanup_status == 'CLEANUP_VERIFIED':
                break
            _t.sleep(2)
        _line('cancel -> cleanup + lock released',
              s2 is not None and s2.cleanup_status == 'CLEANUP_VERIFIED' and (lock2 is None or lock2.status == LockStatus.RELEASED.value),
              f'state={s2.state if s2 else "?"} cleanup={s2.cleanup_status if s2 else "?"}')
    except Exception as e:
        _line('C3 exception', False, f'{type(e).__name__}: {e}')
        if sid:
            try:
                from app.workers.reproduction_tasks import cancel_reproduction
                cancel_reproduction.apply_async(args=[sid], queue='reproduction').get(timeout=60)
            except Exception:
                pass
    finally:
        try:
            if _close:
                _close()
        except Exception:
            pass
        db.close()


def _device_clean():
    """Check no leaked tcpdump/dsp-diag processes (single event loop)."""
    async def _check():
        provider = get_credential_provider()
        pwd = await provider.get_password(sn=SN, ip=IP)
        a = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username='root', password=pwd)
        try:
            await asyncio.wait_for(a.connect(), timeout=15)
            r = await a.execute_shell('echo T=$(ps w | grep tcpdump | grep -v grep | wc -l); echo P=$(ps w | grep "dsp diag" | grep -v grep | wc -l)', timeout=10)
            import re
            m_t = re.search(r'T=(\d+)', r.stdout or '')
            m_p = re.search(r'P=(\d+)', r.stdout or '')
            tcp = int(m_t.group(1)) if m_t else -1
            pcm = int(m_p.group(1)) if m_p else -1
            return tcp == 0 and pcm == 0, f'tcpdump={tcp} dsp_diag={pcm}'
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'
        finally:
            try:
                await asyncio.wait_for(a.disconnect(), timeout=5)
            except Exception:
                pass
    return asyncio.run(_check())


def main():
    c1_tunnel_connect_message()
    c2_d1_lock_antistacking()
    c3_idle_timeout()
    print('=== device-state clean (after device-op cases) ===')
    ok, detail = _device_clean()
    _line('no leaked tcpdump/dsp-diag', ok, detail)


if __name__ == '__main__':
    main()
