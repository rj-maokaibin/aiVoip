from __future__ import annotations

from typing import Any, Iterable


SUBJECT_IDENTITY_UNAVAILABLE = "UNAVAILABLE"
SUBJECT_IDENTITY_UNIQUE = "UNIQUE"
SUBJECT_IDENTITY_AMBIGUOUS = "AMBIGUOUS"


def _endpoint_rows_from_streams(streams: Iterable[dict]) -> tuple[list[dict], set[str]]:
    by_ip: dict[str, dict[str, Any]] = {}
    populated_taps: set[str] = set()
    for stream in streams or []:
        tap_name = str((stream.get("tap") or {}).get("name") or "")
        if int(stream.get("packet_count") or 0) > 0:
            populated_taps.add(tap_name)
        for endpoint in stream.get("source_endpoints", []) or []:
            ip = str(endpoint.get("ip") or "").strip()
            if not ip:
                continue
            row = by_ip.setdefault(ip, {"ip": ip, "packet_count": 0, "taps": set(), "ports": set()})
            row["packet_count"] += int(endpoint.get("packet_count") or 0)
            if tap_name:
                row["taps"].add(tap_name)
            if endpoint.get("port") is not None:
                row["ports"].add(int(endpoint["port"]))
    candidates = [
        {
            "ip": row["ip"],
            "packet_count": row["packet_count"],
            "taps": sorted(row["taps"]),
            "ports": sorted(row["ports"]),
        }
        for row in by_ip.values()
    ]
    candidates.sort(key=lambda row: (-int(row["packet_count"]), row["ip"]))
    return candidates, populated_taps


def infer_pcm_source_device_identity(pcm: dict | None, *, source: str | None = None) -> dict:
    """Infer the device emitting diagnostic PCM from packet provenance only.

    A UNIQUE result is returned only when one source IP explains every populated
    PCM tap, or when there is only one source IP in the PCM evidence. The helper
    never consumes configured DUT IPs or Golden expected values.
    """
    if not isinstance(pcm, dict):
        return {
            "status": SUBJECT_IDENTITY_UNAVAILABLE,
            "source": source,
            "candidate_ips": [],
            "selected_ip": None,
            "reason": "PCM_RESULT_UNAVAILABLE",
        }
    candidates, populated_taps = _endpoint_rows_from_streams(pcm.get("streams", []) or [])
    if not candidates:
        return {
            "status": SUBJECT_IDENTITY_UNAVAILABLE,
            "source": source,
            "candidate_ips": [],
            "selected_ip": None,
            "reason": "PCM_SOURCE_ENDPOINTS_UNAVAILABLE",
        }
    complete = [row for row in candidates if populated_taps and set(row["taps"]) >= populated_taps]
    if len(complete) == 1:
        return {
            "status": SUBJECT_IDENTITY_UNIQUE,
            "source": source,
            "candidate_ips": candidates,
            "selected_ip": complete[0]["ip"],
            "populated_taps": sorted(populated_taps),
            "reason": "ONE_PCM_SOURCE_IP_COVERS_ALL_POPULATED_TAPS",
        }
    if len(candidates) == 1:
        return {
            "status": SUBJECT_IDENTITY_UNIQUE,
            "source": source,
            "candidate_ips": candidates,
            "selected_ip": candidates[0]["ip"],
            "populated_taps": sorted(populated_taps),
            "reason": "ONLY_ONE_PCM_SOURCE_IP",
        }
    return {
        "status": SUBJECT_IDENTITY_AMBIGUOUS,
        "source": source,
        "candidate_ips": candidates,
        "selected_ip": None,
        "populated_taps": sorted(populated_taps),
        "reason": "MULTIPLE_PCM_SOURCE_DEVICE_CANDIDATES",
    }


def infer_pcm_signal_source_identity(signal: dict | None) -> dict:
    """Infer source identity for one extracted PCM signal/session."""
    if not isinstance(signal, dict):
        return {"status": SUBJECT_IDENTITY_UNAVAILABLE, "selected_ip": None, "reason": "PCM_SIGNAL_UNAVAILABLE"}
    endpoints = list(signal.get("source_endpoints", []) or [])
    ips = sorted({str(x.get("ip")) for x in endpoints if x.get("ip")})
    if len(ips) == 1:
        return {
            "status": SUBJECT_IDENTITY_UNIQUE,
            "selected_ip": ips[0],
            "candidate_ips": ips,
            "reason": "ONE_PCM_SIGNAL_SOURCE_IP",
        }
    if not ips:
        return {"status": SUBJECT_IDENTITY_UNAVAILABLE, "selected_ip": None, "candidate_ips": [], "reason": "PCM_SIGNAL_SOURCE_ENDPOINTS_UNAVAILABLE"}
    return {"status": SUBJECT_IDENTITY_AMBIGUOUS, "selected_ip": None, "candidate_ips": ips, "reason": "MULTIPLE_PCM_SIGNAL_SOURCE_IPS"}


def endpoint_touches_subject(endpoint: Any, subject_ip: str | None) -> bool:
    if not subject_ip:
        return False
    if isinstance(endpoint, dict):
        return str(endpoint.get("src_ip") or "") == subject_ip or str(endpoint.get("dst_ip") or "") == subject_ip
    return str(getattr(endpoint, "src_ip", "") or "") == subject_ip or str(getattr(endpoint, "dst_ip", "") or "") == subject_ip
