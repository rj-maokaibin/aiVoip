#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
import app.db.models  # noqa: F401
import app.automation.pbx_models  # noqa: F401
from app.automation.pbx_models import PbxCallSession
from app.automation.adapters.pbx.call_session import PbxCallSessionManager
from app.automation.adapters.pbx.esl import FreeSwitchEventSocketClient
from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider
from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
from app.automation.adapters.pbx.inventory import PbxInventoryService
from app.automation.adapters.pbx.profile import load_fusionpbx_profile
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe
from app.automation.adapters.pbx.resource_authority import PbxExtensionLeaseManager
from app.automation.adapters.pbx.resource_manager import PbxResourceManager
from app.automation.adapters.pbx.runtime_control import FreeSwitchRuntimeProvider
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence

GATE_ID = "G-CALL-001"
REQUIRED_EVENTS = ("CHANNEL_CREATE", "RINGING", "ANSWERED", "DTMF", "HANGUP")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Controlled G-CALL-001 SIP call E2E")
    p.add_argument("--profile", default="profiles/pbx/fusionpbx_srv_v1.json")
    p.add_argument("--state-db", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--extension", default="7900.a")
    p.add_argument("--dial-alias", default="7900")
    p.add_argument("--media-port", type=int, default=6000)
    p.add_argument("--registration-timeout", type=float, default=15.0)
    p.add_argument("--call-timeout", type=float, default=15.0)
    p.add_argument("--allow-live-mutation", action="store_true")
    return p.parse_args()


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"PBX_REQUIRED_BINARY_MISSING:{name}")
    return path


def run_registration(*, profile, scenario: Path, injection: Path, local_port: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            require_binary("sipp"), f"{profile.host}:{profile.internal_port}",
            "-sf", str(scenario), "-inf", str(injection),
            "-i", profile.host, "-p", str(local_port), "-m", "1",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        timeout=12, check=False,
    )


def start_peer(*, profile, scenario: Path, local_port: int, media_port: int):
    return subprocess.Popen(
        [
            require_binary("sipp"), f"{profile.host}:{profile.internal_port}",
            "-sf", str(scenario), "-i", profile.host, "-p", str(local_port),
            "-min_rtp_port", str(media_port), "-max_rtp_port", str(media_port + 2),
            "-m", "1", "-rtp_echo", "-trace_err",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def start_media_capture(*, media_port: int, path: Path):
    return subprocess.Popen(
        [
            "sudo", "-n", require_binary("tcpdump"), "-Z", "dev", "-i", "lo",
            "-U", "-n", "-s", "256", "-w", str(path), f"udp port {media_port}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def media_frame_count(path: Path, media_port: int) -> int:
    cp = subprocess.run(
        [require_binary("tshark"), "-r", str(path), "-Y", f"udp.port == {media_port}",
         "-T", "fields", "-e", "frame.number"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        timeout=10, check=False,
    )
    return sum(1 for line in cp.stdout.splitlines() if line.strip()) if cp.returncode == 0 else 0


def build_context(args):
    profile = load_fusionpbx_profile(args.profile)
    config = FusionPbxConfigProvider(profile)
    fence = FusionPbxSourceFence(profile)
    runtime = FreeSwitchRuntimeReadProbe(profile)
    state = Path(args.state_db).resolve()
    state.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{state}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    inventory_service = PbxInventoryService(
        Session, profile, config_provider=config, source_fence=fence,
    )
    inventory = inventory_service.sync()
    domains = config.discover_domains()
    if len(domains) != 1:
        raise RuntimeError("PBX_DOMAIN_CARDINALITY_INVALID")
    domain = domains[0]
    resource = inventory_service.ensure_automation_identity(
        pbx_node_id=inventory["node_id"], domain_id=domain.domain_id,
        extension=args.extension,
    )
    authority = PbxExtensionLeaseManager(Session, ttl_seconds=180)
    token = authority.acquire_endpoint(
        pbx_node_id=inventory["node_id"], extension=resource.extension,
        dial_alias=args.dial_alias, run_id=args.run_id, owner_worker_id=args.owner,
    )
    mutation = FusionPbxMutationProvider(
        profile, authority=authority, source_fence=fence, timeout_seconds=20,
    )
    manager = PbxResourceManager(
        Session, authority=authority, config=config, mutation=mutation, runtime=runtime,
    )
    calls = PbxCallSessionManager(Session, authority=authority)
    return profile, Session, config, runtime, domain, authority, token, manager, calls


def main() -> int:
    args = parse_args()
    if not args.allow_live_mutation or os.getenv("REAL_LIVE_MUTATION") != "EXPLICIT_ONLY":
        raise RuntimeError("PBX_GOLDEN_LIVE_MUTATION_NOT_EXPLICITLY_AUTHORIZED")
    if not (1024 <= args.media_port <= 65532):
        raise RuntimeError("PBX_MEDIA_PORT_INVALID")
    evidence_path = Path(args.evidence).resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[1]
    register_scenario = repo_root / "profiles/pbx/sipp/g_call_001_register.xml"
    answer_scenario = repo_root / "profiles/pbx/sipp/g_call_001_answer.xml"
    (profile, Session, config, runtime, domain, authority,
     token, manager, calls) = build_context(args)

    payload: dict = {
        "schema": "g-call-001-v1", "gate": GATE_ID, "run_id": args.run_id,
        "extension_identity": token.extension, "dialed_digits": args.dial_alias,
        "resolved_identity": None, "secret_values_emitted": False,
    }
    password = secrets.token_urlsafe(24)
    injection_path = None
    peer = capture = control = events = call = None
    release_last = False
    cleanup_ok = False
    extension_cleaned = False
    probe = None
    media_frames = 0
    event_kinds: list[str] = []
    try:
        provision = manager.provision_extension(
            token, password=password, template_identity="7102", dial_alias=args.dial_alias,
        )
        payload["provision"] = provision.safe_dict()
        resolution = runtime.resolve_directory_identity(args.dial_alias, domain_name=domain.domain_name)
        payload["alias_resolution"] = resolution
        payload["resolved_identity"] = (resolution or {}).get("resolved_identity")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="aivoip-g-call-", suffix=".csv",
            dir="/tmp", delete=False,
        ) as stream:
            injection_path = Path(stream.name)
            stream.write(
                "SEQUENTIAL\n"
                f"{token.extension};[authentication username={token.extension} password={password}];"
                f"{domain.domain_name};120\n"
            )
        injection_path.chmod(0o600)
        reg = run_registration(
            profile=profile, scenario=register_scenario, injection=injection_path,
            local_port=5090,
        )
        payload["sipp_register_rc"] = reg.returncode
        probe = FusionPbxRegistrationProbe(
            fs_cli_bin=profile.fs_cli_bin, poll_interval_seconds=0.25,
        )
        registered = asyncio.run(
            probe.wait_registered(number=token.extension, timeout_seconds=args.registration_timeout)
        )
        payload["registered"] = registered.registered
        active_contact = runtime.registration_contact_visible(
            token.extension, domain_name=domain.domain_name,
        )
        payload["active_contact_registered"] = active_contact
        if reg.returncode != 0 or not registered.registered or not active_contact:
            raise RuntimeError("G_CALL_001_REGISTER_FAILED")

        peer = start_peer(
            profile=profile, scenario=answer_scenario, local_port=5090,
            media_port=args.media_port,
        )
        events = FreeSwitchEventSocketClient(profile, timeout_seconds=2.0)
        events.connect(); events.subscribe()
        control = FreeSwitchRuntimeProvider(profile, authority=authority)
        control.connect()
        capture_path = evidence_path.with_suffix(".rtp.pcap")
        capture_path.unlink(missing_ok=True)
        capture = start_media_capture(media_port=args.media_port, path=capture_path)
        time.sleep(0.25)
        call = calls.create(run_id=args.run_id, callee=token, direction="OUTBOUND")
        originated = control.originate_alias(
            token, dial_alias=args.dial_alias, domain_name=domain.domain_name, tone_ms=3000,
        )
        payload["originate"] = originated.safe_dict()
        call = calls.bind_uuid(call.id, originated.call_uuid)
        deadline = time.monotonic() + float(args.call_timeout)
        dtmf_injected = False
        while time.monotonic() < deadline:
            try:
                event = events.read_event(timeout_seconds=0.75)
            except socket.timeout:
                continue
            if event is None or event.uuid != originated.call_uuid:
                continue
            if event.kind not in set(REQUIRED_EVENTS) | {"EARLY_MEDIA"}:
                continue
            call = calls.apply_event(call.id, event)
            if event.kind in REQUIRED_EVENTS:
                event_kinds.append(event.kind)
            if event.kind == "ANSWERED" and not dtmf_injected:
                time.sleep(0.35)
                control.inject_received_dtmf(token, originated.call_uuid, "5")
                dtmf_injected = True
            if event.kind == "HANGUP":
                break
        payload["event_kinds"] = event_kinds
        payload["call_state"] = call.state
        payload["hangup_cause"] = call.hangup_cause
        payload["dtmf_injected"] = dtmf_injected
        if call.state != "COMPLETED":
            try:
                control.hangup(token, originated.call_uuid)
            except Exception:
                pass
            call = calls.fail(call.id, state="TIMEOUT", cause="G_CALL_001_TIMEOUT")
            raise RuntimeError("G_CALL_001_CALL_TIMEOUT")

        peer_rc = peer.wait(timeout=5)
        payload["sipp_peer_rc"] = peer_rc
        if capture is not None:
            capture.terminate(); capture.wait(timeout=3); capture = None
        media_frames = media_frame_count(capture_path, args.media_port)
        calls.record_rtp(call.id, {"pcap_media_frames": media_frames})
        payload["rtp"] = {"pcap_media_frames": media_frames, "evidence": str(capture_path)}

        cleanup = manager.deprovision_extension(token)
        payload["cleanup"] = cleanup.safe_dict()
        extension_cleaned = True
        control.flush_registration(
            token, identity=token.extension, domain_name=domain.domain_name,
        )
        contact_absent = runtime.wait_registration_absent(
            token.extension, domain_name=domain.domain_name,
            timeout_seconds=args.registration_timeout, poll_seconds=0.25,
        )
        stale_registration = probe.observe_registered_once(number=token.extension)
        payload["active_contact_after_cleanup"] = not contact_absent
        payload["stale_registration_record_present"] = stale_registration.registered
        extension_absent = not config.extension_exists(token.extension)
        alias_absent = not config.extension_exists(args.dial_alias)
        runtime_extension_absent = not runtime.user_visible(
            token.extension, domain_name=domain.domain_name,
        )
        runtime_alias_absent = not runtime.user_visible(
            args.dial_alias, domain_name=domain.domain_name,
        )
        payload["cleanup_verify"] = {
            "extension_absent": extension_absent,
            "dial_alias_absent": alias_absent,
            "runtime_extension_absent": runtime_extension_absent,
            "runtime_alias_absent": runtime_alias_absent,
            "active_registration_contact_absent": contact_absent,
        }
        cleanup_ok = all(payload["cleanup_verify"].values())
        if cleanup_ok:
            manager.release_extension(token)
            release_last = True
        payload["release_last"] = release_last
        payload["7102_still_exists"] = config.extension_exists("7102")
        payload["7102_runtime_visible"] = runtime.user_visible(
            "7102", domain_name=domain.domain_name,
        )
        order_cursor = 0
        for kind in event_kinds:
            if order_cursor < len(REQUIRED_EVENTS) and kind == REQUIRED_EVENTS[order_cursor]:
                order_cursor += 1
        event_order_ok = order_cursor == len(REQUIRED_EVENTS)
        payload["required_event_order_pass"] = event_order_ok
        with Session() as session:
            row = session.get(PbxCallSession, call.id)
            dtmf_refs = list(row.dtmf_evidence_refs_json or []) if row else []
            rtp_refs = list(row.rtp_evidence_refs_json or []) if row else []
        payload["dtmf_evidence"] = dtmf_refs
        payload["rtp_evidence"] = rtp_refs
        verdict = bool(
            payload.get("resolved_identity") == token.extension
            and event_order_ok
            and call.state == "COMPLETED"
            and call.hangup_cause == "NORMAL_CLEARING"
            and any(item.get("digit") == "5" for item in dtmf_refs)
            and media_frames > 0
            and payload.get("sipp_peer_rc") == 0
            and cleanup_ok
            and release_last
            and payload["7102_still_exists"] is True
            and payload["7102_runtime_visible"] is True
        )
        payload["verdict"] = "PASS" if verdict else "FAIL"
    except Exception as exc:
        payload["execution_error"] = type(exc).__name__
        payload["execution_error_code"] = getattr(exc, "code", None)
        payload["verdict"] = "FAIL"
    finally:
        if capture is not None and capture.poll() is None:
            capture.terminate()
            try: capture.wait(timeout=3)
            except subprocess.TimeoutExpired: capture.kill()
        if peer is not None and peer.poll() is None:
            peer.terminate()
            try: peer.wait(timeout=3)
            except subprocess.TimeoutExpired: peer.kill()
        if not release_last and authority.validate(token):
            try:
                active = calls.active_for_resource(token.resource_id)
                for active_call in active:
                    if control is not None and active_call.freeswitch_uuid:
                        try: control.hangup(token, active_call.freeswitch_uuid)
                        except Exception: pass
                    calls.fail(active_call.id, state="FAILED", cause="G_CALL_001_CLEANUP")
                if not extension_cleaned:
                    payload["cleanup"] = manager.deprovision_extension(token).safe_dict()
                    extension_cleaned = True
                if control is not None:
                    control.flush_registration(token, identity=token.extension, domain_name=domain.domain_name)
                if probe is None:
                    probe = FusionPbxRegistrationProbe(fs_cli_bin=profile.fs_cli_bin, poll_interval_seconds=0.25)
                contact_absent = runtime.wait_registration_absent(
                    token.extension, domain_name=domain.domain_name,
                    timeout_seconds=10.0, poll_seconds=0.25,
                )
                if contact_absent:
                    manager.release_extension(token)
                    release_last = True
            except Exception as cleanup_exc:
                payload["final_cleanup_error"] = type(cleanup_exc).__name__
                payload["final_cleanup_error_code"] = getattr(cleanup_exc, "code", None)
        payload["release_last"] = release_last
        if events is not None: events.close()
        if control is not None: control.close()
        if injection_path is not None: injection_path.unlink(missing_ok=True)

    evidence_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("verdict") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
