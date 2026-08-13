#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

API_BASE = os.getenv('E2E_API_BASE', 'http://backend:8000').rstrip('/')
API = f'{API_BASE}/api/v1'
TIMEOUT = float(os.getenv('E2E_TIMEOUT_SECONDS', '420'))
POLL = float(os.getenv('E2E_POLL_SECONDS', '1.0'))
RESULT_PATH = Path(os.getenv('E2E_RESULT_PATH', '/e2e/results/fullstack_result.json'))
EVIDENCE_PATH = os.getenv('E2E_EVIDENCE_PATH', '').strip()
FIXTURE_MODE = os.getenv('E2E_FIXTURE_MODE', 'synthetic-periodic').strip()
TOOLS_ROOT = Path(os.getenv('E2E_TOOLS_ROOT', '/e2e-tools'))
REQUIRE_WORKER_QUEUES = [x.strip() for x in os.getenv('E2E_REQUIRED_QUEUES', 'media,diagnosis').split(',') if x.strip()]
SOURCE_MANIFEST_SHA256 = os.getenv('SOURCE_MANIFEST_SHA256', '').strip()
EXPECTED_ALEMBIC_HEAD = os.getenv('EXPECTED_ALEMBIC_HEAD', '').strip()
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()


class CheckBook:
    def __init__(self):
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, actual: Any = None, expected: Any = None, detail: Any = None):
        row = {'name': name, 'passed': bool(passed)}
        if actual is not None: row['actual'] = actual
        if expected is not None: row['expected'] = expected
        if detail is not None: row['detail'] = detail
        self.checks.append(row)
        mark = 'PASS' if passed else 'FAIL'
        print(f'[{mark}] {name}' + (f' | actual={actual!r}' if actual is not None else ''))
        return passed

    @property
    def passed(self) -> bool:
        return all(x['passed'] for x in self.checks)


def _wait_until(label, fn, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            last = value
            if value:
                return value
        except Exception as exc:
            last = f'{type(exc).__name__}: {exc}'
        time.sleep(POLL)
    raise TimeoutError(f'{label} timed out after {timeout}s; last={last!r}')


def _prepare_evidence() -> Path:
    if EVIDENCE_PATH:
        p = Path(EVIDENCE_PATH)
        if not p.exists():
            raise FileNotFoundError(f'E2E evidence not found: {p}')
        return p
    if FIXTURE_MODE != 'synthetic-periodic':
        raise ValueError(f'unsupported fixture mode: {FIXTURE_MODE}')
    p = Path('/tmp/voip-fullstack-periodic.pcap')
    subprocess.run([sys.executable, str(TOOLS_ROOT / 'fullstack_fixture.py'), str(p)], check=True)
    return p


def _wait_backend(client: httpx.Client) -> dict:
    def probe():
        r = client.get('/health/ready')
        if r.status_code == 200:
            return r.json()
        return None
    return _wait_until('backend readiness', probe, timeout=120)


def _verify_migration_runtime() -> dict:
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is required to verify migration runtime')
    if not EXPECTED_ALEMBIC_HEAD:
        raise RuntimeError('EXPECTED_ALEMBIC_HEAD is required to verify migration runtime')
    from sqlalchemy import create_engine, text
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = [str(x[0]) for x in conn.execute(text('select version_num from alembic_version')).fetchall()]
    finally:
        engine.dispose()
    return {
        'expected_head': EXPECTED_ALEMBIC_HEAD,
        'database_heads': sorted(rows),
        'verified': rows == [EXPECTED_ALEMBIC_HEAD],
    }


def _wait_workers() -> dict:
    from app.workers.celery_app import celery_app
    found: dict[str, list[str]] = {}

    def probe():
        nonlocal found
        inspect = celery_app.control.inspect(timeout=1.5)
        active = inspect.active_queues() or {}
        found = {worker: sorted({q.get('name') for q in queues if q.get('name')}) for worker, queues in active.items()}
        available = {q for qs in found.values() for q in qs}
        return found if all(q in available for q in REQUIRE_WORKER_QUEUES) else None

    return _wait_until(f'celery queues {REQUIRE_WORKER_QUEUES}', probe, timeout=120)


def _get_json(client: httpx.Client, path: str) -> Any:
    r = client.get(path)
    r.raise_for_status()
    return r.json()


def _wait_diagnosis(client: httpx.Client, case_id: str) -> dict:
    terminal = {'DIAGNOSED', 'WAITING_USER', 'FAILED'}

    def probe():
        r = client.get(f'{API}/cases/{case_id}/diagnosis/latest')
        if r.status_code == 404:
            return None
        r.raise_for_status()
        row = r.json()
        print(f"diagnosis status={row.get('status')} cycle={row.get('cycle')}")
        return row if row.get('status') in terminal else None

    return _wait_until('diagnosis terminal status', probe)


def main() -> int:
    started = time.time()
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    checks = CheckBook()
    result: dict[str, Any] = {'schema_version': 1, 'evidence_type': 'DOCKER_FULLSTACK_E2E', 'started_at_epoch': started, 'api_base': API_BASE, 'checks': checks.checks, 'source_manifest_aggregate_sha256': SOURCE_MANIFEST_SHA256}

    try:
        evidence = _prepare_evidence()
        result['evidence_path'] = str(evidence)
        result['evidence_size_bytes'] = evidence.stat().st_size
        result['mode'] = 'field' if EVIDENCE_PATH else 'synthetic-periodic'

        with httpx.Client(base_url=API_BASE, timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True) as client:
            ready = _wait_backend(client)
            result['readiness'] = ready
            checks.add('backend dependencies ready', ready.get('status') == 'ok', ready.get('checks'))
            checks.add('source manifest bound to runtime evidence', len(SOURCE_MANIFEST_SHA256) == 64, SOURCE_MANIFEST_SHA256 or None, '64-char SHA256')

            migration = _verify_migration_runtime()
            result['migration_runtime'] = migration
            result['migration_runtime_verified'] = bool(migration.get('verified'))
            checks.add('PostgreSQL Alembic revision is current head', bool(migration.get('verified')), migration.get('database_heads'), [migration.get('expected_head')])

            workers = _wait_workers()
            result['workers'] = workers
            available_queues = sorted({q for qs in workers.values() for q in qs})
            checks.add('required celery queues active', all(q in available_queues for q in REQUIRE_WORKER_QUEUES), available_queues, REQUIRE_WORKER_QUEUES)

            r = client.post(f'{API}/rules/bootstrap?actor=system'); r.raise_for_status(); rules_bootstrap = r.json()
            r = client.post(f'{API}/knowledge/bootstrap?actor=system'); r.raise_for_status(); knowledge_bootstrap = r.json()
            result['bootstrap'] = {'rules': rules_bootstrap, 'knowledge': knowledge_bootstrap}
            checks.add('reviewed rules persisted and activated', int(rules_bootstrap.get('count', 0)) >= 10, rules_bootstrap.get('count'), '>=10')
            checks.add('seed knowledge persisted', int(knowledge_bootstrap.get('count', 0)) >= 1, knowledge_bootstrap.get('count'), '>=1')

            case_req = {
                'summary': 'APF1250 现场持续电流音，验证本地采集周期性干扰全栈诊断闭环',
                'ip': '192.0.2.10',
                'ssh_port': 22,
                'sn': 'E2E-FULLSTACK-001',
                'created_by': 'fullstack-e2e',
            }
            r = client.post(f'{API}/cases', json=case_req)
            r.raise_for_status(); case = r.json(); case_id = case['id']
            result['case'] = case
            checks.add('case persisted', bool(case_id) and case['status'] == 'NEW', {'id': case_id, 'status': case['status']})

            with evidence.open('rb') as fh:
                files = {'file': (evidence.name, fh, 'application/vnd.tcpdump.pcap')}
                r = client.post(f'{API}/cases/{case_id}/evidences/upload', files=files)
            r.raise_for_status(); ev = r.json(); result['evidence'] = ev
            checks.add('evidence persisted with SHA256', len(ev.get('sha256', '')) == 64 and ev.get('size_bytes') == evidence.stat().st_size,
                       {'sha256': ev.get('sha256'), 'size_bytes': ev.get('size_bytes')})

            r = client.post(f'{API}/cases/{case_id}/diagnosis/start')
            r.raise_for_status(); diagnosis_job = r.json(); result['diagnosis_job'] = diagnosis_job
            checks.add('diagnosis job accepted', diagnosis_job.get('type') == 'AI_DIAGNOSIS', diagnosis_job)

            diagnosis = _wait_diagnosis(client, case_id); result['diagnosis'] = diagnosis
            checks.add('diagnosis reaches DIAGNOSED', diagnosis.get('status') == 'DIAGNOSED', diagnosis.get('status'), 'DIAGNOSED')
            checks.add('diagnosis required at least two cycles', int(diagnosis.get('cycle', 0)) >= 2, diagnosis.get('cycle'), '>=2')

            jobs_page = _get_json(client, f'{API}/jobs/by-case/{case_id}'); jobs = jobs_page.get('items', []); result['jobs'] = jobs_page
            job_types = {j['type']: j['status'] for j in jobs}
            checks.add('media worker job persisted', 'ANALYZE_MEDIA' in job_types, job_types)
            checks.add('media job succeeded or degraded-success', job_types.get('ANALYZE_MEDIA') in {'SUCCESS', 'PARTIAL_SUCCESS'}, job_types.get('ANALYZE_MEDIA'))
            checks.add('diagnosis parent job succeeded', job_types.get('AI_DIAGNOSIS') == 'SUCCESS', job_types.get('AI_DIAGNOSIS'))

            runs_page = _get_json(client, f'{API}/cases/{case_id}/analyzer-runs'); runs = runs_page.get('items', []); result['analyzer_runs'] = runs_page
            media_runs = [x for x in runs if x.get('analyzer_name') == 'media_intelligence']
            checks.add('AnalyzerRun persisted', len(media_runs) >= 1, len(media_runs), '>=1')
            if not media_runs:
                raise AssertionError('media AnalyzerRun missing')
            media_run = media_runs[-1]
            checks.add('media AnalyzerRun result ready', media_run.get('status') in {'SUCCESS', 'PARTIAL_SUCCESS'}, media_run.get('status'))
            media_result = _get_json(client, f"{API}/analyzer-runs/{media_run['id']}/result")
            result['media_summary'] = media_result.get('summary')
            periodic_count = int((media_result.get('summary') or {}).get('periodic_interference_count') or 0)
            checks.add('periodic interference detected', periodic_count >= 1, periodic_count, '>=1')
            checks.add('RTP media streams decoded', int((media_result.get('summary') or {}).get('decoded_rtp_track_count') or 0) >= 2,
                       (media_result.get('summary') or {}).get('decoded_rtp_track_count'), '>=2')

            hypotheses = _get_json(client, f"{API}/diagnosis-runs/{diagnosis['id']}/hypotheses"); result['hypotheses'] = hypotheses
            hyp = {h['code']: h for h in hypotheses}
            target = hyp.get('LOCAL_CAPTURE_PERIODIC_INTERFERENCE')
            checks.add('periodic-interference hypothesis exists', target is not None, sorted(hyp))
            checks.add('periodic-interference hypothesis SUPPORTED', bool(target) and target.get('status') == 'SUPPORTED', target and target.get('status'), 'SUPPORTED')
            checks.add('periodic-interference confidence high', bool(target) and float(target.get('confidence', 0)) >= 0.90, target and target.get('confidence'), '>=0.90')
            bad_confirmed = [h['code'] for h in hypotheses if h.get('status') == 'CONFIRMED']
            checks.add('AI does not auto-confirm hardware root cause', not bad_confirmed, bad_confirmed, [])
            loss = hyp.get('RTP_PACKET_LOSS_PATH')
            checks.add('does not misdiagnose RTP packet loss', loss is None or loss.get('status') not in {'SUPPORTED', 'CONFIRMED'}, loss and loss.get('status'))

            artifacts = _get_json(client, f'{API}/cases/{case_id}/artifacts'); result['artifact_count'] = len(artifacts)
            types = sorted({a['type'] for a in artifacts})
            checks.add('media artifacts stored in MinIO metadata', len(artifacts) >= 3, {'count': len(artifacts), 'types': types}, '>=3')
            checks.add('WAV artifact generated', 'AUDIO_WAV' in types, types)

            audit_page = _get_json(client, f'{API}/cases/{case_id}/audit'); audit = audit_page.get('items', []); result['audit_count'] = len(audit)
            audit_types = [a['event_type'] for a in audit]
            checks.add('audit trail includes upload', 'EVIDENCE_UPLOADED' in audit_types, audit_types)
            checks.add('audit trail includes media completion', 'MEDIA_ANALYSIS_FINISHED' in audit_types, audit_types)
            checks.add('audit trail includes diagnosis cycle', 'DIAGNOSIS_CYCLE' in audit_types, audit_types)

            r = client.post(f'{API}/cases/{case_id}/reports/diagnosis', json={'actor': 'fullstack-e2e'})
            r.raise_for_status(); report = r.json(); result['report'] = report
            checks.add('diagnosis report persisted', report.get('status') == 'GENERATED', report.get('status'), 'GENERATED')
            links = _get_json(client, f"{API}/reports/{report['id']}/links"); result['report_links'] = links
            html = client.get(links['html_url']); html.raise_for_status()
            report_html = html.text
            checks.add('HTML report retrievable from MinIO', len(report_html) > 500, len(report_html), '>500 bytes')
            checks.add('HTML report contains target diagnosis', '本地音频采集链路存在稳定周期性干扰' in report_html or 'LOCAL_CAPTURE_PERIODIC_INTERFERENCE' in report_html,
                       'target text present' if ('LOCAL_CAPTURE_PERIODIC_INTERFERENCE' in report_html) else 'target text absent')

            final_case = _get_json(client, f'{API}/cases/{case_id}'); result['final_case'] = final_case
            checks.add('Case final state is DIAGNOSED', final_case.get('status') == 'DIAGNOSED', final_case.get('status'), 'DIAGNOSED')

        result['passed'] = checks.passed
    except Exception as exc:
        result['passed'] = False
        result['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        print(f'[FATAL] {type(exc).__name__}: {exc}', file=sys.stderr)
    finally:
        result['duration_seconds'] = round(time.time() - started, 3)
        result['checks'] = checks.checks
        result['checks_passed'] = sum(1 for x in checks.checks if x['passed'])
        result['checks_total'] = len(checks.checks)
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': result.get('passed'), 'checks_passed': result['checks_passed'], 'checks_total': result['checks_total'], 'duration_seconds': result['duration_seconds'], 'result': str(RESULT_PATH)}, ensure_ascii=False, indent=2))
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
