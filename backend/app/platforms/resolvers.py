from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from typing import Any


class PlatformResolverError(ValueError):
    pass


def _object(output: str, parser_id: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlatformResolverError(f'{parser_id}:INVALID_JSON') from exc
    if not isinstance(value, dict):
        raise PlatformResolverError(f'{parser_id}:ROOT_NOT_OBJECT')
    return value


def resolve_voip_service_gateway_v1(output: str) -> str:
    parser_id = 'DEV_CONFIG_VOIP_SERVICE_GATEWAY_V1'
    value = _object(output, parser_id)
    rows = value.get('data')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise PlatformResolverError(f'{parser_id}:EXPECTED_EXACTLY_ONE_SERVICE')
    gateway = rows[0].get('svrName')
    if not isinstance(gateway, str) or not gateway.strip():
        raise PlatformResolverError(f'{parser_id}:MISSING_SVR_NAME')
    try:
        return str(ipaddress.ip_address(gateway.strip()))
    except ValueError as exc:
        raise PlatformResolverError(f'{parser_id}:INVALID_GATEWAY_IP') from exc


def resolve_voice_vlan_id_v1(output: str) -> str:
    parser_id = 'DEV_CONFIG_VOICE_VLAN_ID_V1'
    value = _object(output, parser_id)
    if str(value.get('enable')) != '1':
        raise PlatformResolverError(f'{parser_id}:VOICE_VLAN_DISABLED')
    vlan_id = value.get('vlanid')
    if isinstance(vlan_id, bool):
        raise PlatformResolverError(f'{parser_id}:INVALID_VLAN_ID')
    try:
        parsed = int(vlan_id)
    except (TypeError, ValueError) as exc:
        raise PlatformResolverError(f'{parser_id}:INVALID_VLAN_ID') from exc
    if not 1 <= parsed <= 4094:
        raise PlatformResolverError(f'{parser_id}:VLAN_ID_OUT_OF_RANGE')
    return str(parsed)


def resolve_voice_interface_v1(output: str, *, voice_vlan_id: str) -> str:
    parser_id = 'IP_LINK_VOICE_INTERFACE_V1'
    try:
        vlan_id = int(voice_vlan_id)
    except (TypeError, ValueError) as exc:
        raise PlatformResolverError(f'{parser_id}:INVALID_VLAN_ID') from exc
    if not 1 <= vlan_id <= 4094:
        raise PlatformResolverError(f'{parser_id}:VLAN_ID_OUT_OF_RANGE')
    expected = f'br-lan_{vlan_id}'
    matches: list[set[str]] = []
    pattern = re.compile(r'^\s*\d+:\s+([^:@]+)(?:@[^:]+)?:\s+<([^>]*)>')
    for line in output.splitlines():
        found = pattern.search(line)
        if found and found.group(1) == expected:
            matches.append({flag.strip() for flag in found.group(2).split(',')})
    if len(matches) != 1:
        raise PlatformResolverError(f'{parser_id}:EXPECTED_EXACTLY_ONE_INTERFACE')
    if not {'UP', 'LOWER_UP'} <= matches[0]:
        raise PlatformResolverError(f'{parser_id}:INTERFACE_NOT_READY')
    return expected


_AIM_EVENT_PATTERN = re.compile(
    r'^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}) '
    r'\[(?P<line>\d+)\].*?(?P<event>OFFHOOK|ONHOOK|DTMF<(?P<digit>[0-9A-D#*])>)\s*$'
)


def resolve_aim_fxs_events_v1(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        found = _AIM_EVENT_PATTERN.match(raw_line.strip())
        if not found:
            continue
        raw_event = found.group('event')
        events.append({
            'timestamp': found.group('timestamp'),
            'line': int(found.group('line')),
            'event': 'DTMF' if raw_event.startswith('DTMF<') else raw_event,
            'digit': found.group('digit'),
        })
    if not events:
        raise PlatformResolverError('AIM_FXS_EVENT_V1:NO_EVENTS')
    return events


PARSERS: dict[str, Callable[..., Any]] = {
    'DEV_CONFIG_VOIP_SERVICE_GATEWAY_V1': resolve_voip_service_gateway_v1,
    'DEV_CONFIG_VOICE_VLAN_ID_V1': resolve_voice_vlan_id_v1,
    'IP_LINK_VOICE_INTERFACE_V1': resolve_voice_interface_v1,
    'AIM_FXS_EVENT_V1': resolve_aim_fxs_events_v1,
}


def resolve_platform_value(parser_id: str, output: str, **context: Any) -> Any:
    try:
        parser = PARSERS[parser_id]
    except KeyError as exc:
        raise PlatformResolverError(f'UNKNOWN_PLATFORM_PARSER:{parser_id}') from exc
    return parser(output, **context)