#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def load_registry_mirror() -> str:
    path = Path('/etc/docker/daemon.json')
    if not path.exists():
        raise RuntimeError('/etc/docker/daemon.json is missing')
    cfg = json.loads(path.read_text(encoding='utf-8'))
    mirrors = cfg.get('registry-mirrors') or []
    if not mirrors:
        raise RuntimeError('Docker registry-mirrors is empty')
    return str(mirrors[0]).rstrip('/')


def main() -> int:
    parser = argparse.ArgumentParser(description='Fast Docker registry transport probe')
    parser.add_argument('--timeout-seconds', type=float, default=3.0)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()

    started = time.monotonic()
    payload: dict[str, object] = {
        'schema_version': 'cicd-registry-connectivity-v2.1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'FAIL',
    }
    try:
        mirror = load_registry_mirror()
        parsed = urlparse(mirror)
        if not parsed.hostname:
            raise RuntimeError(f'invalid registry mirror URL: {mirror}')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        ips = sorted({x[4][0] for x in socket.getaddrinfo(parsed.hostname, port, socket.AF_INET, socket.SOCK_STREAM)})
        endpoint = mirror + '/v2/'
        request = urllib.request.Request(endpoint, method='GET', headers={'User-Agent': 'voip-ai-cicd-v2.1'})
        code = None
        server = None
        distribution_api = None
        try:
            with urllib.request.urlopen(request, timeout=args.timeout_seconds) as response:
                code = response.status
                server = response.headers.get('Server')
                distribution_api = response.headers.get('Docker-Distribution-Api-Version')
        except urllib.error.HTTPError as exc:
            # 401/403 are valid registry transport responses; authentication is
            # handled by Docker. The probe is only proving DNS/TCP/HTTP reachability.
            code = exc.code
            server = exc.headers.get('Server')
            distribution_api = exc.headers.get('Docker-Distribution-Api-Version')

        if code is None or not (100 <= int(code) < 500):
            raise RuntimeError(f'unexpected registry HTTP status: {code}')
        payload.update({
            'status': 'PASS',
            'mirror': mirror,
            'endpoint': endpoint,
            'resolved_ipv4': ips,
            'http_status': int(code),
            'server': server,
            'docker_distribution_api_version': distribution_api,
        })
    except TimeoutError as exc:
        payload['error'] = f'registry probe timeout: {exc}'
    except urllib.error.URLError as exc:
        reason = getattr(exc, 'reason', exc)
        payload['error'] = f'registry probe failed: {reason}'
    except Exception as exc:
        payload['error'] = f'registry probe failed: {exc}'

    payload['duration_ms'] = int((time.monotonic() - started) * 1000)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload['status'] == 'PASS':
        print(f"REGISTRY_CONNECTIVITY=PASS duration_ms={payload['duration_ms']} http_status={payload.get('http_status')}")
        return 0
    print(f"REGISTRY_CONNECTIVITY=FAIL duration_ms={payload['duration_ms']} error={payload.get('error')}")
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
