"""D1 real-device verification: stale watcher exits on device-lock reassignment.

Verifies the D1 guard (commit 26ba891): when ANOTHER session holds the device's
ACTIVE diagnostic lock, a stale watcher must return DEVICE_LOCK_REASSIGNED
WITHOUT connecting to the device (no SSH, no build_orchestrator, no tcpdump),
preventing stacked watchers from racing on the same DUT (which previously caused
SSH_COMMAND_TIMEOUT).

Checks (run against the REAL Postgres DB):
  1. REAL device + lock held by session B -> session A's watcher exits early with
     DEVICE_LOCK_REASSIGNED, build_orchestrator is NEVER called, and it returns
     fast (no SSH handshake).
  2. UNREACHABLE ssh_port + lock held by session D -> session C's watcher STILL
     exits early with DEVICE_LOCK_REASSIGNED, proving the guard runs BEFORE any
     SSH connect attempt (if the guard were missing, the watcher would try to
     connect the dead port and fail).
  3. No false positive: the lock owner's own watcher is not reassigned.

Usage (inside backend container, /tools mounted):
    python /tools/dev_real_d1_verify.py
"""
import asyncio
import sys
import time
from uuid import uuid4

sys.path.insert(0, '/app')

from app.db.session import SessionLocal
from app.db.models import Case, CaseDevice, ReproductionSession
from app.reproduction.locks import acquire_device_lock
from app.workers.reproduction_event_tasks import _watch, _device_lock_reassigned

REAL_IP = '47.104.22.0'
REAL_PORT = 63061
SN = 'MACC1JZH3260M'
BAD_PORT = 63999  # deliberately unreachable: proves no SSH connect is attempted


def _mk_case_device(db, ip, port, tag):
    case = Case(case_no=f'D1-{tag}-{uuid4().hex[:8]}', summary='D1 lock reassignment verify', status='ANALYZING')
    db.add(case); db.flush()
    dev = CaseDevice(case_id=case.id, ip=ip, ssh_port=port, sn=SN, username='root')
    db.add(dev); db.flush()
    return case, dev


def _mk_session(db, case, dev):
    s = ReproductionSession(
        case_id=case.id, device_id=dev.id, profile_key='P', profile_version='1',
        profile_checksum='c', effective_profile_snapshot={}, state='WATCHING',
    )
    db.add(s); db.flush()
    return s


def _run_watch(session_id, max_seconds=2):
    """Run _watch synchronously; return (result, elapsed_seconds, orchestrator_calls)."""
    import app.reproduction.platform_factory as pf
    original = pf.build_orchestrator
    calls = {'n': 0}

    def spy(*a, **k):
        calls['n'] += 1
        return original(*a, **k)

    pf.build_orchestrator = spy
    try:
        t0 = time.monotonic()
        result = asyncio.run(_watch(session_id, max_seconds=max_seconds))
        elapsed = time.monotonic() - t0
        return result, elapsed, calls['n']
    finally:
        pf.build_orchestrator = original


def main():
    results = []

    def check(label, ok, detail=''):
        results.append((label, bool(ok), detail))
        print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))

    db = SessionLocal()
    try:
        # ---- Check 1: REAL device, lock held by B -> A exits early, no connect ----
        case1, dev1 = _mk_case_device(db, REAL_IP, REAL_PORT, 'real')
        sa = _mk_session(db, case1, dev1)
        sb = _mk_session(db, case1, dev1)
        db.commit()
        acquire_device_lock(db, session=sb, owner_worker='d1-verify', lease_seconds=300)
        db.commit()

        result, elapsed, calls = _run_watch(sa.id)
        check('real device: stale watcher returns DEVICE_LOCK_REASSIGNED',
              result.get('status') == 'DEVICE_LOCK_REASSIGNED' and result.get('session_id') == sa.id,
              f'result={result}')
        check('real device: no device connect attempt (build_orchestrator never called)',
              calls == 0, f'orchestrator_calls={calls}')
        check('real device: exits fast (<2s, no SSH handshake)',
              elapsed < 2.0, f'elapsed={elapsed:.2f}s')

        # ---- Check 2: unreachable port + lock held by D -> C STILL exits early ----
        case2, dev2 = _mk_case_device(db, REAL_IP, BAD_PORT, 'badport')
        sc = _mk_session(db, case2, dev2)
        sd = _mk_session(db, case2, dev2)
        db.commit()
        acquire_device_lock(db, session=sd, owner_worker='d1-verify', lease_seconds=300)
        db.commit()

        result2, elapsed2, calls2 = _run_watch(sc.id)
        check('bad port: guard runs BEFORE SSH connect (DEVICE_LOCK_REASSIGNED, zero connect)',
              result2.get('status') == 'DEVICE_LOCK_REASSIGNED' and calls2 == 0,
              f'result={result2} orchestrator_calls={calls2} elapsed={elapsed2:.2f}s')

        # ---- Check 3: no false positive - lock owner's own watcher not blocked ----
        check('no false positive: lock owner (sd) is not reassigned',
              _device_lock_reassigned(db, sd) is False)

        # ---- cleanup ----
        for s in (sa, sb, sc, sd):
            db.delete(s)
        db.delete(case1)
        db.delete(case2)
        db.commit()
    finally:
        db.close()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f'\nD1 real-device verification: {passed}/{total} PASS')
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
