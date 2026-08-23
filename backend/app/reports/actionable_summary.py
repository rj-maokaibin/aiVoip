from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

LOCAL_TIMEZONE = timezone(timedelta(hours=8))
SUPPORTED_STATES = {"SUPPORTED", "STRONGLY_SUPPORTED", "CONFIRMED"}


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
    media_start = call.get("media_started_at")
    media_end = call.get("media_ended_at")
    call_start = call.get("started_at")
    call_end = call.get("ended_at")
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
        "B1：只改变电源/接地条件；完成后恢复A确认现象是否回归。",
        "B2：只更换电话机/话柄/线路；完成后恢复A确认现象是否回归。",
        "B3：只切换FXS端口；完成后恢复A确认现象是否回归。",
        "B4：只替换同环境另一台被测设备；完成后恢复A确认现象是否回归。",
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
    return payload
