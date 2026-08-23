from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

LOCAL_TIMEZONE = timezone(timedelta(hours=8))
SUPPORTED_STATES = {"SUPPORTED", "STRONGLY_SUPPORTED", "CONFIRMED"}
ACTIONABLE_CONTRACT_VERSION = "actionable-finding-contract-v2"

_PERIODIC_FINDINGS = {
    "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE",
    "PERIODIC_INTERFERENCE_PATH_COMPARISON",
}
_NETWORK_FINDINGS = {
    "HIGH_DELTA",
    "PACKET_LOSS",
    "BURST_LOSS",
    "ONE_WAY_RTP_MEDIA",
}


def _local_iso(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TIMEZONE).isoformat()


def build_observation_window(payload: dict) -> dict:
    call = payload.get("display_call") or payload.get("call") or {}
    media_start = call.get("media_started_at") or call.get("media_start_time")
    media_end = call.get("media_ended_at") or call.get("media_end_time")
    call_start = call.get("started_at") or call.get("start_time")
    call_end = call.get("ended_at") or call.get("end_time")
    start = media_start or call_start
    end = media_end or call_end
    scope = "ACTIVE_MEDIA_WINDOW" if media_start or media_end else "CALL_WINDOW"
    return {
        "scope": scope,
        "absolute_start_utc": start,
        "absolute_end_utc": end,
        "absolute_start_local": _local_iso(start),
        "absolute_end_local": _local_iso(end),
        "timezone": "UTC+08:00",
        "exact_event_window_known": False,
        "boundary_statement": (
            "当前绝对时间表示可确认的媒体/Call观察边界；若Finding未给出独立的异常首末时刻，"
            "不得把该窗口伪装成异常精确发生区间。"
        ),
    }


def _top_supported(diagnosis: dict) -> dict:
    hypotheses = list(diagnosis.get("hypotheses") or [])
    supported = [item for item in hypotheses if str(item.get("status") or "").upper() in SUPPORTED_STATES]
    supported.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
    return supported[0] if supported else (hypotheses[0] if hypotheses else {})


def build_problem_scope(payload: dict, diagnosis: dict) -> dict:
    top = _top_supported(diagnosis)
    code = str(top.get("code") or "")
    title = top.get("title") or payload.get("headline") or "当前问题范围尚未收敛"
    fault_domain = top.get("fault_domain")
    if code == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE":
        affected_path = "被测设备本地音频采集链路（PCM RX → 上行 RTP）"
        statement = (
            "当前证据已将持续周期性干扰从整个VOIP网络链路收敛到被测设备本地采集链路："
            "异常在PCM RX阶段已经存在，并传播进入上行RTP；现有反向RTP证据不支持PBX/下行网络"
            "是该持续周期底噪的主要引入点。"
        )
    else:
        affected_path = fault_domain or "当前证据绑定范围"
        statement = f"当前最高置信诊断方向：{title}；影响范围：{affected_path}。"
    return {
        "hypothesis_code": code or None,
        "headline": title,
        "fault_domain": fault_domain,
        "affected_path": affected_path,
        "statement": statement,
        "excluded_or_weakened": list(diagnosis.get("excluded") or []),
        "unresolved": list(diagnosis.get("unknown") or []),
    }


def _hardware_ab_steps() -> list[str]:
    return [
        "基线A：保持当前可复现故障环境，固定号码、通话时长和采集Profile，采集PCAP、PCM RX、PCM TX。",
        "B1：只改变电源/接地条件；完成后恢复基线A确认现象是否回归。",
        "B2：只更换电话机/话柄/线路；完成后恢复基线A确认现象是否回归。",
        "B3：只切换FXS端口；完成后恢复基线A确认现象是否回归。",
        "B4：只替换同环境另一台被测设备；完成后恢复基线A确认现象是否回归。",
        "每轮比较PCM RX与上行RTP中的约20ms周期自相关及150/250/350/...Hz梳状谱强度，禁止同时改变多个变量。",
    ]


def _jitter_steps() -> list[str]:
    return [
        "在被测设备侧与PBX侧或中间链路增加同一时段多点PCAP。",
        "按SSRC、RTP sequence和payload对同一媒体包进行跨抓包点匹配。",
        "比较同一包在各抓包点的到达时间差与HIGH_DELTA出现位置，区分发送端、交换网络或PBX侧引入。",
    ]


def build_next_actions(payload: dict, diagnosis: dict) -> list[dict]:
    actions = []
    plan = sorted(list(diagnosis.get("plan") or []), key=lambda item: int(item.get("priority") or 100))
    for item in plan:
        params = dict(item.get("params") or {})
        purpose = params.get("purpose")
        if purpose == "close_specific_hardware_root_cause":
            steps = _hardware_ab_steps()
            acceptance = (
                "某单一变量改变后，PCM RX的20ms周期/奇次谐波梳状谱显著下降或消失，且上行RTP对应特征同步下降或消失；"
                "恢复基线A后两者重新出现。只有完成B→A回归闭环，才可把该变量提升为强因果证据。"
            )
        elif purpose in {"locate_jitter_segment", "locate_loss_segment", "locate_one_way_media_segment"}:
            steps = _jitter_steps()
            acceptance = (
                "同一RTP包的跨点时间相关能够把异常增量明确夹在两个抓包点之间；"
                "若各点均无对应异常，则不得把单点HIGH_DELTA解释为网络段根因。"
            )
        else:
            steps = [str(item.get("reason") or "按计划补充证据并重新运行确定性诊断。")]
            acceptance = "补采后必须新增可复核证据，并使对应Hypothesis状态或Root Cause Boundary发生可解释变化；否则视为无进展。"
        actions.append({
            "action_type": item.get("action_type"),
            "priority": item.get("priority"),
            "risk_level": item.get("risk_level"),
            "auto_execute": bool(item.get("auto_execute")),
            "reason": item.get("reason"),
            "params": params,
            "execution_steps": steps,
            "acceptance_criteria": acceptance,
        })
    if not actions:
        recommended = (payload.get("preliminary_assessment") or {}).get("recommended_next_action")
        if recommended:
            actions.append({
                "action_type": "REVIEW_AND_VERIFY",
                "priority": 100,
                "risk_level": "USER",
                "auto_execute": False,
                "reason": recommended,
                "params": {},
                "execution_steps": [recommended],
                "acceptance_criteria": "复核结果必须绑定到明确Evidence/Finding时间窗，并更新当前诊断边界。",
            })
    return actions


def _display_call_id(payload: dict) -> str | None:
    call = payload.get("display_call") or payload.get("call") or {}
    return call.get("id") or call.get("call_id") or call.get("call_no")


def _has_bound_time(time_range: dict) -> bool:
    return any(time_range.get(key) is not None for key in ("start", "end", "representative"))


def _bind_time_semantics(finding: dict, window: dict) -> None:
    time_range = dict(finding.get("time_range") or {})
    ftype = str(finding.get("type") or "")
    if _has_bound_time(time_range):
        if ftype in _PERIODIC_FINDINGS:
            time_range.setdefault("semantics", "ANALYSIS_WINDOW")
            time_range.setdefault("exact_event_window_known", False)
        else:
            time_range.setdefault("semantics", "EVENT_OR_ANALYZER_RANGE")
            time_range.setdefault("exact_event_window_known", True)
        finding["time_range"] = time_range
        return

    start = window.get("absolute_start_utc")
    end = window.get("absolute_end_utc")
    if start is None and end is None:
        finding["time_range"] = {
            "start": None,
            "end": None,
            "representative": None,
            "semantics": "UNKNOWN",
            "exact_event_window_known": False,
            "boundary_statement": "当前 Evidence 未提供可绑定的异常或观察时间边界。",
        }
        return
    finding["time_range"] = {
        "start": start,
        "end": end,
        "representative": start,
        "semantics": "OBSERVATION_BOUNDARY",
        "exact_event_window_known": False,
        "boundary_statement": window.get("boundary_statement"),
    }


def _bind_scope(finding: dict, payload: dict) -> None:
    scope = dict(finding.get("scope") or {})
    # Compatibility migration: older persisted/report fixtures may carry the
    # authoritative Finding scope only in Evidence Card V1. Reuse only explicit
    # non-UNKNOWN values; never invent a layer that is absent from both sources.
    legacy_card_scope = dict(((finding.get("evidence_card") or {}).get("scope") or {}))
    for key in ("layer", "call_id", "rtp_stream_id", "direction", "pcm_tap", "ssrc", "call_direction_role", "path_role"):
        if scope.get(key) in (None, "", "UNKNOWN") and legacy_card_scope.get(key) not in (None, "", "UNKNOWN"):
            scope[key] = legacy_card_scope[key]

    ftype = str(finding.get("type") or "")
    call_id = _display_call_id(payload)
    if call_id and not scope.get("call_id"):
        scope["call_id"] = call_id

    if ftype == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE":
        scope.setdefault("layer", "PCM_RX_TO_RTP_UPSTREAM")
        scope.setdefault("pcm_tap", "pcm_rx")
        scope.setdefault("direction", "LOCAL_CAPTURE_TO_UPSTREAM_RTP")
        scope.setdefault("path_role", "LOCAL_CAPTURE_PATH")
        metrics = finding.get("metrics") or {}
        scope.setdefault("rtp_stream_id", metrics.get("upstream_rtp_stream_id"))
    elif ftype == "PERIODIC_LOW_FREQUENCY_INTERFERENCE" and scope.get("pcm_tap"):
        scope.setdefault("layer", str(scope.get("pcm_tap")).upper())
    finding["scope"] = scope
    finding["scope_binding_status"] = "BOUND" if any(
        scope.get(key) not in (None, "") for key in ("layer", "pcm_tap", "rtp_stream_id", "call_id")
    ) else "UNKNOWN"

    # If an older Finding already declared OBSERVED_BOUNDARY but omitted the
    # redundant layer field, backfill it only from the now-bound explicit scope.
    correlation = dict(finding.get("correlation") or {})
    first = dict(correlation.get("first_observable_boundary") or {})
    if first.get("status") == "OBSERVED_BOUNDARY" and not first.get("first_observable_layer"):
        layer = scope.get("layer") or scope.get("pcm_tap")
        if layer not in (None, "", "UNKNOWN"):
            first["first_observable_layer"] = layer
            correlation["first_observable_boundary"] = first
            finding["correlation"] = correlation


def _choose_action(finding: dict, actions: list[dict]) -> dict | None:
    if not actions:
        return None
    ftype = str(finding.get("type") or "")
    if ftype in _PERIODIC_FINDINGS:
        match = next((a for a in actions if (a.get("params") or {}).get("purpose") == "close_specific_hardware_root_cause"), None)
        if match:
            return match
    if ftype in _NETWORK_FINDINGS:
        match = next((a for a in actions if (a.get("params") or {}).get("purpose") in {
            "locate_jitter_segment", "locate_loss_segment", "locate_one_way_media_segment"
        }), None)
        if match:
            return match
    return actions[0]


def bind_actionable_findings(payload: dict) -> dict:
    window = payload.get("observation_window") or build_observation_window(payload)
    actions = list(payload.get("next_actions") or [])
    for finding in payload.get("findings") or []:
        _bind_scope(finding, payload)
        _bind_time_semantics(finding, window)
        action = _choose_action(finding, actions)
        if action:
            steps = list(action.get("execution_steps") or [])
            finding["next_action"] = action.get("reason") or (steps[0] if steps else None)
            finding["verification_acceptance"] = action.get("acceptance_criteria")
            finding["action_contract"] = {
                "contract_version": ACTIONABLE_CONTRACT_VERSION,
                "action_type": action.get("action_type"),
                "priority": action.get("priority"),
                "risk_level": action.get("risk_level"),
                "execution_steps": steps,
                "acceptance_criteria": action.get("acceptance_criteria"),
            }
        else:
            finding.setdefault("next_action", "复核该Finding的原始Evidence及相邻层对照，再决定是否进入确定性补采/A-B验证。")
            finding.setdefault("verification_acceptance", "新增证据必须绑定到该Finding的明确Scope与时间边界，并使证据边界发生可解释变化。")
        finding["actionable_contract_version"] = ACTIONABLE_CONTRACT_VERSION
    return payload


def attach_actionable_summary(payload: dict, diagnosis: dict | None = None) -> dict:
    diagnosis = diagnosis or payload.get("diagnosis") or {}
    if diagnosis:
        payload["diagnosis"] = diagnosis
    payload["problem_scope"] = build_problem_scope(payload, diagnosis)
    payload["observation_window"] = build_observation_window(payload)
    payload["next_actions"] = build_next_actions(payload, diagnosis)
    assessment = payload.setdefault("preliminary_assessment", {})
    if payload["next_actions"]:
        assessment["recommended_next_action"] = payload["next_actions"][0]["reason"]
        payload["verification_acceptance"] = payload["next_actions"][0]["acceptance_criteria"]
    bind_actionable_findings(payload)
    payload["actionable_contract_version"] = ACTIONABLE_CONTRACT_VERSION
    return payload