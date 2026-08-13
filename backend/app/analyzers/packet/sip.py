from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Iterable

from .sdp import negotiate_codecs, parse_sdp
from .types import NormalizedPacket


STATUS_EXPLANATIONS = {
    100: ("PBX/对端已收到请求并正在处理", True, "等待 180/183 或最终响应"),
    180: ("被叫侧已经进入振铃阶段", True, "被叫接听后通常返回 200 OK"),
    183: ("会话进入 Early Media/进度提示阶段", True, "等待最终 2xx 或失败响应"),
    200: ("当前 SIP 请求处理成功", True, "INVITE 的 200 OK 后应看到 ACK"),
    401: ("服务器发起 Digest Authentication 认证挑战", True, "客户端通常携带 Authorization 重新发起请求"),
    403: ("服务器明确拒绝当前请求", False, "检查账号、权限、访问控制或 PBX 策略"),
    404: ("服务器无法找到目标用户或路由", False, "检查号码、域、路由与 PBX 配置"),
    407: ("代理服务器要求 Proxy Authentication", True, "客户端通常携带 Proxy-Authorization 重试"),
    408: ("SIP 请求超时", False, "检查网络可达性、对端服务与重传"),
    423: ("注册有效期过短，服务器要求更大的 Min-Expires", False, "按 Min-Expires 调整后重新 REGISTER"),
    486: ("被叫忙", False, "属于业务状态；确认是否符合现场实际"),
    487: ("请求已被取消/终止", True, "常见于 CANCEL 后的 INVITE 结束流程"),
}

METHOD_EXPLANATIONS = {
    "REGISTER": ("向 SIP Registrar 注册当前用户/Contact", "等待认证挑战或 200 OK"),
    "INVITE": ("发起或更新一条 SIP 会话/语音呼叫", "等待 100/180/183/最终响应"),
    "ACK": ("确认 INVITE 的最终成功响应", "呼叫建立后开始/继续媒体传输"),
    "BYE": ("结束一条已经建立的 SIP 会话", "对端应返回 200 OK"),
    "CANCEL": ("取消尚未完成的 INVITE", "常见后续为 INVITE 487"),
    "OPTIONS": ("探测 SIP 对端能力或存活状态", "等待最终响应"),
    "PRACK": ("确认可靠临时响应", "等待 PRACK 的 200 OK"),
    "UPDATE": ("在 Dialog 内更新会话参数", "等待最终响应"),
    "REFER": ("请求对端执行呼叫转移/引用操作", "等待 REFER 响应及后续 NOTIFY"),
}


def semantic_for(packet: NormalizedPacket, next_packet: NormalizedPacket | None = None) -> dict:
    sip = packet.sip
    assert sip is not None
    if sip.status_code is not None:
        text, expected, expected_next = STATUS_EXPLANATIONS.get(
            sip.status_code,
            (f"SIP 返回状态 {sip.status_code}", 200 <= sip.status_code < 300, "根据当前 Transaction 等待下一协议动作"),
        )
        case_note = text
        if sip.status_code in {401, 407} and next_packet and next_packet.sip and next_packet.sip.method:
            if next_packet.sip.method in {sip.cseq_method, "REGISTER", "INVITE"}:
                case_note += "；当前抓包随后出现同类请求重试，需要结合最终响应判断是否为正常认证流程"
        return {
            "what_it_is": text,
            "why_it_appears": f"这是 {sip.cseq_method or '当前'} Transaction 的 SIP 响应",
            "is_expected": expected,
            "expected_next": expected_next,
            "possible_problem": None if expected else text,
            "case_specific_explanation": case_note,
        }
    method = (sip.method or "UNKNOWN").upper()
    text, expected_next = METHOD_EXPLANATIONS.get(method, (f"发送 SIP {method} 请求", "等待对应 SIP 响应"))
    return {
        "what_it_is": text,
        "why_it_appears": f"当前端正在执行 {method} 协议动作",
        "is_expected": True,
        "expected_next": expected_next,
        "possible_problem": None,
        "case_specific_explanation": text,
    }


def reconstruct_sip(packets: Iterable[NormalizedPacket]) -> dict:
    messages = [p for p in packets if p.sip and p.sip.call_id]
    groups: dict[str, list[NormalizedPacket]] = defaultdict(list)
    for packet in messages:
        groups[packet.sip.call_id].append(packet)
    for group in groups.values():
        group.sort(key=lambda p: (p.timestamp, p.frame_number))

    registrations = []
    calls = []
    sip_packet_count = 0
    for call_id, group in groups.items():
        sip_packet_count += len(group)
        methods = [(p.sip.method or p.sip.cseq_method or "").upper() for p in group]
        if "REGISTER" in methods:
            registrations.append(_registration(call_id, group))
        if "INVITE" in methods:
            calls.append(_call(call_id, group))

    return {
        "sip_message_count": sip_packet_count,
        "registrations": registrations,
        "calls": calls,
    }


def _ladder(group: list[NormalizedPacket]) -> list[dict]:
    out = []
    for idx, packet in enumerate(group):
        sip = packet.sip
        nxt = group[idx + 1] if idx + 1 < len(group) else None
        label = sip.method if sip.method else f"{sip.status_code} {sip.reason_phrase or ''}".strip()
        out.append({
            "frame_number": packet.frame_number,
            "timestamp": packet.timestamp,
            "src": _endpoint(packet.src_ip, packet.src_port),
            "dst": _endpoint(packet.dst_ip, packet.dst_port),
            "label": label,
            "method": sip.method,
            "status_code": sip.status_code,
            "cseq": sip.cseq,
            "cseq_method": sip.cseq_method,
            "semantic": semantic_for(packet, nxt),
        })
    return out


def _registration(call_id: str, group: list[NormalizedPacket]) -> dict:
    final_responses = [p.sip.status_code for p in group if p.sip.status_code is not None and p.sip.cseq_method == "REGISTER"]
    final = final_responses[-1] if final_responses else None
    status = "SUCCESS" if final and 200 <= final < 300 else "FAILED" if final and final >= 300 and final not in {401, 407} else "INCOMPLETE"
    auth_challenges = sum(1 for code in final_responses if code in {401, 407})
    return {
        "call_id": call_id,
        "aor": next((p.sip.from_uri for p in group if p.sip.from_uri), None),
        "registrar": next((p.dst_ip for p in group if p.sip.method == "REGISTER"), None),
        "status": status,
        "final_status_code": final,
        "auth_challenges": auth_challenges,
        "start_time": group[0].timestamp,
        "end_time": group[-1].timestamp,
        "ladder": _ladder(group),
    }


def _call(call_id: str, group: list[NormalizedPacket]) -> dict:
    invite = next((p for p in group if p.sip.method == "INVITE"), group[0])
    caller = invite.sip.from_uri
    callee = invite.sip.to_uri
    invite_responses = [p for p in group if p.sip.status_code is not None and p.sip.cseq_method == "INVITE"]
    status_codes = [p.sip.status_code for p in invite_responses]
    methods = [(p.sip.method or "").upper() for p in group]
    success_responses = [p for p in invite_responses if 200 <= p.sip.status_code < 300]
    established = bool(success_responses) and "ACK" in methods
    terminated = "BYE" in methods
    cancelled = "CANCEL" in methods
    if established and terminated:
        state = "TERMINATED"
    elif established:
        state = "ESTABLISHED"
    elif cancelled:
        state = "CANCELLED"
    elif any(code and code >= 300 for code in status_codes):
        state = "FAILED"
    else:
        state = "INCOMPLETE"

    offer_packet = next((p for p in group if p.sip.method == "INVITE" and p.sdp), None)
    answer_packet = next((p for p in group if p.sip.status_code and 200 <= p.sip.status_code < 300 and p.sip.cseq_method == "INVITE" and p.sdp), None)
    offer = parse_sdp(offer_packet.sdp) if offer_packet else None
    answer = parse_sdp(answer_packet.sdp) if answer_packet else None
    invite_final_status = None
    conflicting_final_responses = []
    if success_responses:
        invite_final_status = success_responses[0].sip.status_code
        success_time = success_responses[0].timestamp
        conflicting_final_responses = [
            {"status_code": p.sip.status_code, "timestamp": p.timestamp, "frame_number": p.frame_number}
            for p in invite_responses if p.timestamp > success_time and p.sip.status_code >= 300
        ]
    else:
        finals = [p for p in invite_responses if p.sip.status_code >= 200]
        invite_final_status = finals[-1].sip.status_code if finals else None

    media_start_time = None
    media_end_time = None
    if success_responses:
        success_time = success_responses[0].timestamp
        ack = next((p for p in group if p.sip.method == "ACK" and p.timestamp >= success_time), None)
        media_start_time = ack.timestamp if ack else success_time
        bye = next((p for p in group if p.sip.method == "BYE" and p.timestamp >= media_start_time), None)
        media_end_time = bye.timestamp if bye else group[-1].timestamp

    has_invite_request = any(p.sip.method == "INVITE" for p in group)
    has_final_response = any(p.sip.status_code is not None and p.sip.status_code >= 200 and p.sip.cseq_method == "INVITE" for p in group)
    capture_completeness = {
        "has_invite_request": has_invite_request,
        "has_invite_final_response": has_final_response,
        "has_ack_for_success": ("ACK" in methods) if success_responses else None,
        "is_partial": not has_invite_request or not has_final_response or (bool(success_responses) and "ACK" not in methods),
    }

    return {
        "call_id": call_id,
        "caller": caller,
        "callee": callee,
        "state": state,
        "start_time": group[0].timestamp,
        "end_time": group[-1].timestamp,
        "duration_seconds": round(group[-1].timestamp - group[0].timestamp, 6),
        "media_start_time": media_start_time,
        "media_end_time": media_end_time,
        "active_media_duration_seconds": round(media_end_time-media_start_time,6) if media_start_time is not None and media_end_time is not None else None,
        "invite_final_status": invite_final_status,
        "conflicting_final_responses": conflicting_final_responses,
        "capture_completeness": capture_completeness,
        "ladder": _ladder(group),
        "sdp": {
            "offer": offer.to_dict() if offer else None,
            "answer": answer.to_dict() if answer else None,
            "negotiated_codecs": negotiate_codecs(offer, answer),
        },
    }


def _endpoint(ip: str | None, port: int | None) -> str:
    return f"{ip or '?'}:{port}" if port is not None else (ip or "?")
