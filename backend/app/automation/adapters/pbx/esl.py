from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable
from xml.etree import ElementTree

from app.automation.adapters.pbx.profile import FusionPbxLabProfile


class FreeSwitchEslError(RuntimeError):
    pass


@dataclass(frozen=True)
class EslFrame:
    headers: dict[str, str]
    body: str = ""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class FreeSwitchRuntimeEvent:
    kind: str
    raw_event_name: str
    subclass: str | None = None
    uuid: str | None = None
    identity: str | None = None
    domain: str | None = None
    dtmf_digit: str | None = None
    hangup_cause: str | None = None
    probe_id: str | None = None
    observed_at: str | None = None

    def safe_dict(self) -> dict[str, str | None | bool]:
        return {
            "kind": self.kind,
            "raw_event_name": self.raw_event_name,
            "subclass": self.subclass,
            "uuid": self.uuid,
            "identity": self.identity,
            "domain": self.domain,
            "dtmf_digit": self.dtmf_digit,
            "hangup_cause": self.hangup_cause,
            "probe_id": self.probe_id,
            "observed_at": self.observed_at,
            "secret_values_emitted": False,
        }


def load_event_socket_password(profile: FusionPbxLabProfile) -> str:
    path = Path(profile.event_socket_config)
    if not path.is_file():
        raise FreeSwitchEslError("FREESWITCH_ESL_CONFIG_MISSING")
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise FreeSwitchEslError("FREESWITCH_ESL_CONFIG_INVALID") from exc
    for param in root.iter("param"):
        if param.attrib.get("name") == "password":
            value = str(param.attrib.get("value") or "")
            if value:
                return value
    raise FreeSwitchEslError("FREESWITCH_ESL_PASSWORD_MISSING")


def _read_frame(stream: BinaryIO) -> EslFrame:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise FreeSwitchEslError("FREESWITCH_ESL_CONNECTION_CLOSED")
        if line in {b"\n", b"\r\n"}:
            break
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length_text = headers.get("content-length")
    body = ""
    if length_text:
        try:
            length = int(length_text)
        except ValueError as exc:
            raise FreeSwitchEslError("FREESWITCH_ESL_CONTENT_LENGTH_INVALID") from exc
        data = stream.read(length)
        if len(data) != length:
            raise FreeSwitchEslError("FREESWITCH_ESL_BODY_TRUNCATED")
        body = data.decode("utf-8", errors="replace")
    return EslFrame(headers=headers, body=body)


def _event_value(payload: dict[str, object], *names: str) -> str | None:
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in {None, ""}:
            return str(value)
    return None


def map_runtime_event(payload: dict[str, object]) -> FreeSwitchRuntimeEvent | None:
    event_name = (_event_value(payload, "Event-Name") or "").upper()
    subclass = _event_value(payload, "Event-Subclass")
    kind_map = {
        "CHANNEL_CREATE": "CHANNEL_CREATE",
        "CHANNEL_PROGRESS": "RINGING",
        "CHANNEL_PROGRESS_MEDIA": "EARLY_MEDIA",
        "CHANNEL_ANSWER": "ANSWERED",
        "DTMF": "DTMF",
        "CHANNEL_HANGUP_COMPLETE": "HANGUP",
    }
    kind = kind_map.get(event_name)
    if event_name == "CUSTOM" and subclass in {"sofia::register", "sofia::pre_register"}:
        kind = "REGISTERED"
    elif event_name == "CUSTOM" and subclass in {"sofia::unregister", "sofia::expire"}:
        kind = "UNREGISTERED"
    elif event_name == "CUSTOM" and subclass == "aivoip::probe":
        kind = "PROBE"
    if kind is None:
        return None
    identity = _event_value(
        payload,
        "sip-auth-username",
        "variable_sip_auth_username",
        "from-user",
        "user",
        "username",
        "Caller-Username",
        "variable_user_name",
    )
    domain = _event_value(
        payload,
        "realm",
        "from-host",
        "domain-name",
        "variable_domain_name",
        "Caller-Destination-Number",
    )
    return FreeSwitchRuntimeEvent(
        kind=kind,
        raw_event_name=event_name,
        subclass=subclass,
        uuid=_event_value(payload, "Unique-ID", "Channel-Call-UUID", "variable_uuid"),
        identity=identity,
        domain=domain,
        dtmf_digit=_event_value(payload, "DTMF-Digit"),
        hangup_cause=_event_value(payload, "Hangup-Cause"),
        probe_id=_event_value(payload, "Probe-ID"),
        observed_at=datetime.now(timezone.utc).isoformat(),
    )


class FreeSwitchEventSocketClient:
    DEFAULT_EVENTS = (
        "CUSTOM",
        "sofia::register",
        "sofia::pre_register",
        "sofia::unregister",
        "sofia::expire",
        "CHANNEL_CREATE",
        "CHANNEL_PROGRESS",
        "CHANNEL_PROGRESS_MEDIA",
        "CHANNEL_ANSWER",
        "DTMF",
        "CHANNEL_HANGUP_COMPLETE",
    )

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        password_loader=load_event_socket_password,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.profile = profile
        self.password_loader = password_loader
        self.timeout_seconds = float(timeout_seconds)
        self._socket: socket.socket | None = None
        self._stream: BinaryIO | None = None

    def connect(self) -> None:
        if self._socket is not None:
            return
        sock = socket.create_connection(
            (self.profile.event_socket_host, self.profile.event_socket_port),
            timeout=self.timeout_seconds,
        )
        sock.settimeout(self.timeout_seconds)
        stream = sock.makefile("rb")
        try:
            request = _read_frame(stream)
            if request.header("content-type") != "auth/request":
                raise FreeSwitchEslError("FREESWITCH_ESL_AUTH_REQUEST_MISSING")
            password = self.password_loader(self.profile)
            sock.sendall(f"auth {password}\n\n".encode("utf-8"))
            reply = _read_frame(stream)
            if not str(reply.header("reply-text") or "").startswith("+OK"):
                raise FreeSwitchEslError("FREESWITCH_ESL_AUTH_FAILED")
        except Exception:
            stream.close()
            sock.close()
            raise
        self._socket = sock
        self._stream = stream

    def _require_connection(self) -> tuple[socket.socket, BinaryIO]:
        if self._socket is None or self._stream is None:
            raise FreeSwitchEslError("FREESWITCH_ESL_NOT_CONNECTED")
        return self._socket, self._stream

    def api(self, command: str) -> str:
        if not command or "\n" in command or "\r" in command:
            raise FreeSwitchEslError("FREESWITCH_ESL_COMMAND_INVALID")
        sock, stream = self._require_connection()
        sock.sendall(f"api {command}\n\n".encode("utf-8"))
        frame = _read_frame(stream)
        if frame.header("content-type") != "api/response":
            raise FreeSwitchEslError("FREESWITCH_ESL_API_RESPONSE_INVALID")
        return frame.body

    def subscribe(self, events: Iterable[str] | None = None) -> None:
        selected = tuple(events or self.DEFAULT_EVENTS)
        if not selected or any(
            not event or any(not (ch.isalnum() or ch in {"_", ":", "-"}) for ch in event)
            for event in selected
        ):
            raise FreeSwitchEslError("FREESWITCH_ESL_EVENT_FILTER_INVALID")
        sock, stream = self._require_connection()
        sock.sendall(("event json " + " ".join(selected) + "\n\n").encode("ascii"))
        frame = _read_frame(stream)
        if not str(frame.header("reply-text") or "").startswith("+OK"):
            raise FreeSwitchEslError("FREESWITCH_ESL_SUBSCRIBE_FAILED")


    def send_custom_probe_event(self, probe_id: str) -> None:
        if not probe_id or len(probe_id) > 128 or any(
            not (ch.isalnum() or ch in {"-", "_"}) for ch in probe_id
        ):
            raise FreeSwitchEslError("FREESWITCH_ESL_PROBE_ID_INVALID")
        sock, stream = self._require_connection()
        command = (
            "sendevent CUSTOM\n"
            "Event-Name: CUSTOM\n"
            "Event-Subclass: aivoip::probe\n"
            f"Probe-ID: {probe_id}\n\n"
        )
        sock.sendall(command.encode("ascii"))
        frame = _read_frame(stream)
        if not str(frame.header("reply-text") or "").startswith("+OK"):
            raise FreeSwitchEslError("FREESWITCH_ESL_PROBE_EVENT_FAILED")

    def read_event(self) -> FreeSwitchRuntimeEvent | None:
        _, stream = self._require_connection()
        while True:
            frame = _read_frame(stream)
            if frame.header("content-type") != "text/event-json":
                continue
            try:
                payload = json.loads(frame.body)
            except json.JSONDecodeError as exc:
                raise FreeSwitchEslError("FREESWITCH_ESL_EVENT_JSON_INVALID") from exc
            if not isinstance(payload, dict):
                raise FreeSwitchEslError("FREESWITCH_ESL_EVENT_OBJECT_REQUIRED")
            return map_runtime_event(payload)

    def safe_health(self) -> dict[str, object]:
        self.connect()
        version = self.api("version").strip()
        self.subscribe()
        return {
            "connected": True,
            "authenticated": True,
            "subscribed": True,
            "event_socket_host": self.profile.event_socket_host,
            "event_socket_port": self.profile.event_socket_port,
            "version": version,
            "secret_values_emitted": False,
        }

    def close(self) -> None:
        stream, sock = self._stream, self._socket
        self._stream = None
        self._socket = None
        if stream is not None:
            stream.close()
        if sock is not None:
            sock.close()
