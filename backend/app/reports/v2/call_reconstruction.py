from __future__ import annotations

from typing import Any, Mapping, Sequence


CallDict = Mapping[str, Any]


def reconstruct_call_v2(sip_call: CallDict) -> dict[str, Any]:
    """Build V2 lifecycle facts from one normalized SIP dialog/call record.

    The current packet analyzer already owns SIP parsing. This function consumes
    its normalized ladder and fixes lifecycle semantics for report V2. In
    particular, ACK means the successful INVITE transaction is established; it
    is never a call termination event.

    ``call_end_time`` is populated only when a protocol termination/failure is
    actually observed. Capture/signaling exhaustion is represented separately
    and must not be promoted into a synthetic call end.
    """

    ladder = _ordered_ladder(sip_call.get("ladder"))
    call_id = sip_call.get("call_id")

    invite = _first(ladder, method="INVITE")
    invite_time = _timestamp(invite)

    invite_responses = [
        item
        for item in ladder
        if _status(item) is not None and _cseq_method(item) == "INVITE"
    ]
    success = next(
        (item for item in invite_responses if 200 <= int(_status(item)) < 300),
        None,
    )
    answer_time = _timestamp(success)

    ack = next(
        (
            item
            for item in ladder
            if _method(item) == "ACK"
            and answer_time is not None
            and _timestamp(item) is not None
            and float(_timestamp(item)) >= float(answer_time)
        ),
        None,
    )
    ack_time = _timestamp(ack)
    established_time = ack_time if success is not None and ack is not None else None

    bye = next(
        (
            item
            for item in ladder
            if _method(item) == "BYE"
            and _timestamp(item) is not None
            and (
                established_time is None
                or float(_timestamp(item)) >= float(established_time)
            )
        ),
        None,
    )
    cancel = _first(ladder, method="CANCEL")

    final_failure = None
    if success is None:
        failures = [
            item
            for item in invite_responses
            if _status(item) is not None and int(_status(item)) >= 300
        ]
        if failures:
            final_failure = failures[-1]

    termination = _termination_fact(
        bye=bye,
        cancel=cancel,
        final_failure=final_failure,
        established=established_time is not None,
    )

    if termination["observed"] and termination["kind"] == "BYE":
        state = "TERMINATED"
    elif established_time is not None:
        state = "ESTABLISHED"
    elif cancel is not None:
        state = "CANCELLED"
    elif final_failure is not None:
        state = "FAILED"
    elif success is not None:
        state = "ANSWERED"
    else:
        state = "INCOMPLETE"

    signaling_timestamps = [
        float(ts)
        for ts in (_timestamp(item) for item in ladder)
        if ts is not None
    ]
    capture_last_signaling_time = max(signaling_timestamps) if signaling_timestamps else None
    call_end_time = termination["time"] if termination["observed"] else None

    duration_seconds = None
    if invite_time is not None and call_end_time is not None:
        duration_seconds = round(float(call_end_time) - float(invite_time), 6)

    return {
        "schema": "call-lifecycle-v2",
        "call_id": call_id,
        "caller": sip_call.get("caller"),
        "callee": sip_call.get("callee"),
        "state": state,
        "invite_time": invite_time,
        "answer_time": answer_time,
        "ack_time": ack_time,
        "established_time": established_time,
        "call_end_time": call_end_time,
        "duration_seconds": duration_seconds,
        "capture_last_signaling_time": capture_last_signaling_time,
        "termination": termination,
        "capture_completeness": sip_call.get("capture_completeness") or {},
        "source": {
            "kind": "PACKET_ANALYZER_SIP_LADDER",
            "legacy_state": sip_call.get("state"),
            "legacy_start_time": sip_call.get("start_time"),
            "legacy_end_time": sip_call.get("end_time"),
            "legacy_media_start_time": sip_call.get("media_start_time"),
            "legacy_media_end_time": sip_call.get("media_end_time"),
        },
    }


def _termination_fact(
    *,
    bye: Mapping[str, Any] | None,
    cancel: Mapping[str, Any] | None,
    final_failure: Mapping[str, Any] | None,
    established: bool,
) -> dict[str, Any]:
    if bye is not None:
        return {
            "observed": True,
            "kind": "BYE",
            "time": _timestamp(bye),
            "frame_number": bye.get("frame_number"),
            "status_code": None,
        }

    # CANCEL terminates an early INVITE attempt. It must not be used as the end
    # of an already-established dialog.
    if cancel is not None and not established:
        return {
            "observed": True,
            "kind": "CANCEL",
            "time": _timestamp(cancel),
            "frame_number": cancel.get("frame_number"),
            "status_code": None,
        }

    if final_failure is not None:
        return {
            "observed": True,
            "kind": "FINAL_RESPONSE",
            "time": _timestamp(final_failure),
            "frame_number": final_failure.get("frame_number"),
            "status_code": _status(final_failure),
        }

    return {
        "observed": False,
        "kind": None,
        "time": None,
        "frame_number": None,
        "status_code": None,
    }


def _ordered_ladder(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    items = [item for item in value if isinstance(item, Mapping)]
    return sorted(
        items,
        key=lambda item: (
            float(_timestamp(item)) if _timestamp(item) is not None else float("inf"),
            int(item.get("frame_number") or 0),
        ),
    )


def _first(ladder: Sequence[Mapping[str, Any]], *, method: str) -> Mapping[str, Any] | None:
    expected = method.upper()
    return next((item for item in ladder if _method(item) == expected), None)


def _method(item: Mapping[str, Any] | None) -> str | None:
    if item is None:
        return None
    value = item.get("method")
    return str(value).upper() if value else None


def _cseq_method(item: Mapping[str, Any] | None) -> str | None:
    if item is None:
        return None
    value = item.get("cseq_method")
    return str(value).upper() if value else None


def _status(item: Mapping[str, Any] | None) -> int | None:
    if item is None:
        return None
    value = item.get("status_code")
    if value is None:
        return None
    return int(value)


def _timestamp(item: Mapping[str, Any] | None) -> float | None:
    if item is None:
        return None
    value = item.get("timestamp")
    if value is None:
        return None
    return float(value)
