import importlib.util
import ipaddress
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('docker_network_guard', ROOT / 'deploy/docker_network_guard.py')
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(guard)


def _net(name: str, subnet: str, containers=None):
    return {
        'name': name,
        'subnets': [ipaddress.ip_network(subnet)],
        'containers': list(containers or []),
    }


def _bind(monkeypatch, tmp_path, route: str):
    nets = [
        _net('aivoip-production', '172.30.250.0/24'),
        _net('deploy_default', '172.18.0.0/16'),
    ]
    snapshots = [nets, nets]
    monkeypatch.setattr(guard, 'docker_networks', lambda: snapshots.pop(0))
    monkeypatch.setattr(guard, 'registry_endpoint', lambda: {'ips': ['172.18.34.132']})
    monkeypatch.setattr(guard, 'route_get', lambda _ip: route)
    monkeypatch.setattr(guard, 'MARKER', tmp_path / 'marker.json')
    monkeypatch.setattr(guard, 'EVIDENCE', tmp_path / 'evidence.json')
    calls = []
    monkeypatch.setattr(guard, 'run', lambda *args, **kwargs: calls.append(args) or '')
    return calls


def test_cleanup_allows_unrelated_subnet_conflict_when_registry_route_is_physical(monkeypatch, tmp_path):
    calls = _bind(monkeypatch, tmp_path, '172.18.34.132 via 10.51.230.1 dev ens18 src 10.51.230.212')
    guard.cleanup('aivoip', '172.30.250.0/24', 'aivoip-production')
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    assert evidence['status'] == 'PASS'
    assert evidence['remaining_registry_subnet_conflicts'] == [
        {'network': 'deploy_default', 'subnet': '172.18.0.0/16', 'registry_ip': '172.18.34.132'}
    ]
    assert not any(call[:3] == ('docker', 'network', 'rm') for call in calls)


def test_cleanup_still_fails_closed_when_registry_route_is_docker_hijacked(monkeypatch, tmp_path):
    _bind(monkeypatch, tmp_path, '172.18.34.132 dev br-deploy src 172.18.0.1')
    with pytest.raises(SystemExit) as exc:
        guard.cleanup('aivoip', '172.30.250.0/24', 'aivoip-production')
    assert exc.value.code == 3
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    assert evidence['status'] == 'FAIL'
    assert evidence['reason'] == 'REGISTRY_ROUTE_STILL_HIJACKED_AFTER_CLEANUP'
    assert evidence['routes'][0]['route'].startswith('172.18.34.132 dev br-')
