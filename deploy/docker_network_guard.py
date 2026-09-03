#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

MARKER = Path('/run/voip-ai-registry-route.json')
EVIDENCE = Path('validation/docker_network_guard.json')
DEFAULT_SUBNET = '172.30.250.0/24'
DEFAULT_NETWORK_NAME = 'aivoip-production'


def run(*args, check=True):
    cp = subprocess.run(args, text=True, capture_output=True)
    if check and cp.returncode:
        raise SystemExit(f"command failed ({cp.returncode}): {' '.join(args)}\n{cp.stderr.strip()}")
    return cp.stdout.strip()


def docker_networks():
    out = []
    names = run('docker', 'network', 'ls', '--format', '{{.Name}}').splitlines()
    for name in names:
        raw = run('docker', 'network', 'inspect', name)
        item = json.loads(raw)[0]
        subnets = []
        for cfg in (item.get('IPAM') or {}).get('Config') or []:
            if cfg.get('Subnet'):
                try:
                    subnets.append(ipaddress.ip_network(cfg['Subnet'], strict=False))
                except ValueError:
                    pass
        containers = sorted((item.get('Containers') or {}).keys())
        out.append({'name': name, 'subnets': subnets, 'containers': containers})
    return out


def default_route():
    lines = run('ip', '-4', 'route', 'show', 'default').splitlines()
    if not lines:
        raise SystemExit('no IPv4 default route')
    parts = lines[0].split()
    if 'via' not in parts or 'dev' not in parts:
        raise SystemExit(f'unsupported default route: {lines[0]}')
    return parts[parts.index('via') + 1], parts[parts.index('dev') + 1]


def registry_endpoint():
    path = Path('/etc/docker/daemon.json')
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise SystemExit(f'invalid /etc/docker/daemon.json: {exc}')
    mirrors = cfg.get('registry-mirrors') or []
    if not mirrors:
        return None
    parsed = urlparse(mirrors[0])
    host = parsed.hostname
    if not host:
        raise SystemExit(f'invalid registry mirror URL: {mirrors[0]}')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    ips = sorted({x[4][0] for x in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)})
    if not ips:
        raise SystemExit(f'cannot resolve registry mirror: {host}')
    return {'url': mirrors[0], 'host': host, 'port': port, 'ips': ips}


def route_get(ip):
    return run('ip', '-4', 'route', 'get', ip).splitlines()[0]


def physical_route_conflicts(desired, docker_nets):
    docker_subnets = {str(s) for n in docker_nets for s in n['subnets']}
    conflicts = []
    for raw in run('ip', '-4', 'route', 'show', 'table', 'main').splitlines():
        parts = raw.split()
        if not parts or parts[0] == 'default':
            continue
        try:
            net = ipaddress.ip_network(parts[0], strict=False)
        except ValueError:
            continue
        if str(net) in docker_subnets:
            continue
        if desired.overlaps(net):
            conflicts.append({'route': raw, 'network': str(net)})
    return conflicts


def desired_network_conflicts(desired, network_name, nets):
    intended = [n for n in nets if n['name'] == network_name]
    if len(intended) > 1:
        return 'DUPLICATE_PRODUCTION_NETWORK_NAME', [
            {'network': n['name'], 'subnets': [str(s) for s in n['subnets']]} for n in intended
        ]
    if intended:
        observed = intended[0]['subnets']
        if len(observed) != 1 or observed[0] != desired:
            return 'EXISTING_PRODUCTION_NETWORK_SUBNET_MISMATCH', [{
                'network': network_name,
                'expected_subnet': str(desired),
                'observed_subnets': [str(s) for s in observed],
            }]

    conflicts = []
    for n in nets:
        for subnet in n['subnets']:
            # A repeat deployment must accept the already-created authoritative
            # production network when its single subnet exactly matches desired.
            if n['name'] == network_name and subnet == desired:
                continue
            if desired.overlaps(subnet):
                conflicts.append({'network': n['name'], 'subnet': str(subnet)})
    if conflicts:
        return 'DESIRED_SUBNET_OVERLAPS_EXISTING_DOCKER_NETWORK', conflicts
    return None, []


def write_evidence(payload):
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def prepare(project, desired_text, network_name):
    desired = ipaddress.ip_network(desired_text, strict=False)
    nets = docker_networks()
    physical = physical_route_conflicts(desired, nets)
    if physical:
        write_evidence({
            'status': 'FAIL', 'reason': 'DESIRED_SUBNET_OVERLAPS_HOST_ROUTE',
            'desired_subnet': str(desired), 'network_name': network_name, 'conflicts': physical,
        })
        raise SystemExit(2)

    conflict_reason, docker_overlap = desired_network_conflicts(desired, network_name, nets)
    if conflict_reason:
        write_evidence({
            'status': 'FAIL', 'reason': conflict_reason,
            'desired_subnet': str(desired), 'network_name': network_name, 'conflicts': docker_overlap,
        })
        raise SystemExit(2)

    endpoint = registry_endpoint()
    repaired = []
    registry_conflicts = []
    if endpoint:
        gateway, dev = default_route()
        for ip_text in endpoint['ips']:
            ip = ipaddress.ip_address(ip_text)
            if ip in desired:
                write_evidence({
                    'status': 'FAIL', 'reason': 'DESIRED_SUBNET_CONTAINS_REGISTRY_MIRROR',
                    'desired_subnet': str(desired), 'network_name': network_name, 'registry_ip': ip_text,
                })
                raise SystemExit(2)
            hits = [
                {'network': n['name'], 'subnet': str(s)}
                for n in nets for s in n['subnets'] if ip in s
            ]
            if not hits:
                continue
            registry_conflicts.extend([dict(x, registry_ip=ip_text) for x in hits])
            before = route_get(ip_text)
            already_physical = f'dev {dev}' in before and 'dev br-' not in before and 'dev docker0' not in before
            if not already_physical:
                run('ip', 'route', 'replace', f'{ip_text}/32', 'via', gateway, 'dev', dev)
            after = route_get(ip_text)
            if f'dev {dev}' not in after:
                write_evidence({
                    'status': 'FAIL', 'reason': 'REGISTRY_ROUTE_REPAIR_FAILED',
                    'registry_ip': ip_text, 'before': before, 'after': after,
                })
                raise SystemExit(2)
            repaired.append({
                'ip': ip_text, 'gateway': gateway, 'dev': dev,
                'before': before, 'after': after,
                'created_by_guard': not already_physical,
            })
        managed = [item for item in repaired if item['created_by_guard']]
        if managed:
            MARKER.parent.mkdir(parents=True, exist_ok=True)
            MARKER.write_text(json.dumps({'routes': managed}, indent=2) + '\n', encoding='utf-8')

    write_evidence({
        'status': 'PASS', 'phase': 'prepare', 'project': project,
        'network_name': network_name, 'desired_subnet': str(desired), 'registry': endpoint,
        'registry_conflicts': registry_conflicts, 'temporary_routes': repaired,
    })


def cleanup(project, desired_text, network_name):
    desired = ipaddress.ip_network(desired_text, strict=False)
    endpoint = registry_endpoint()
    removed = []
    nets = docker_networks()
    registry_ips = [ipaddress.ip_address(x) for x in (endpoint or {}).get('ips', [])]
    legacy_name = f'{project}_default'

    intended = [n for n in nets if n['name'] == network_name]
    if len(intended) != 1 or len(intended[0]['subnets']) != 1 or intended[0]['subnets'][0] != desired:
        write_evidence({
            'status': 'FAIL', 'reason': 'PRODUCTION_NETWORK_NOT_MATERIALIZED_AS_EXPECTED',
            'network_name': network_name, 'desired_subnet': str(desired),
            'observed': [
                {'network': n['name'], 'subnets': [str(s) for s in n['subnets']]} for n in intended
            ],
        })
        raise SystemExit(3)

    for n in nets:
        conflicts_registry = any(ip in s for ip in registry_ips for s in n['subnets'])
        if n['name'] == legacy_name and conflicts_registry:
            if n['containers']:
                write_evidence({
                    'status': 'FAIL', 'reason': 'LEGACY_CONFLICT_NETWORK_STILL_IN_USE',
                    'network': n['name'], 'containers': n['containers'],
                })
                raise SystemExit(3)
            run('docker', 'network', 'rm', n['name'])
            removed.append(n['name'])

    remaining = docker_networks()
    remaining_conflicts = []
    for n in remaining:
        for s in n['subnets']:
            for ip in registry_ips:
                if ip in s:
                    remaining_conflicts.append({'network': n['name'], 'subnet': str(s), 'registry_ip': str(ip)})
    if remaining_conflicts:
        write_evidence({
            'status': 'FAIL', 'reason': 'REGISTRY_DOCKER_ROUTE_CONFLICT_REMAINS',
            'conflicts': remaining_conflicts, 'removed': removed,
        })
        raise SystemExit(3)

    deleted_routes = []
    if MARKER.exists():
        try:
            marker = json.loads(MARKER.read_text(encoding='utf-8'))
        except Exception:
            marker = {'routes': []}
        for item in marker.get('routes', []):
            run('ip', 'route', 'del', f"{item['ip']}/32", 'via', item['gateway'], 'dev', item['dev'], check=False)
            deleted_routes.append(item)
        MARKER.unlink(missing_ok=True)

    for ip in registry_ips:
        route = route_get(str(ip))
        if 'dev br-' in route or 'dev docker0' in route:
            write_evidence({
                'status': 'FAIL', 'reason': 'REGISTRY_ROUTE_STILL_HIJACKED_AFTER_CLEANUP',
                'registry_ip': str(ip), 'route': route,
            })
            raise SystemExit(3)

    write_evidence({
        'status': 'PASS', 'phase': 'cleanup', 'project': project,
        'network_name': network_name, 'desired_subnet': str(desired),
        'removed_legacy_networks': removed, 'removed_temporary_routes': deleted_routes,
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'cleanup'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--subnet', default=os.environ.get('VOIP_DOCKER_SUBNET', DEFAULT_SUBNET))
    parser.add_argument('--network-name', default=os.environ.get('VOIP_DOCKER_NETWORK_NAME', DEFAULT_NETWORK_NAME))
    args = parser.parse_args()
    if args.phase == 'prepare':
        prepare(args.project, args.subnet, args.network_name)
    else:
        cleanup(args.project, args.subnet, args.network_name)


if __name__ == '__main__':
    main()
