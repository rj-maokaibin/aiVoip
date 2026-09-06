from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def device_args(output: Path) -> list[str]:
    args = [
        "--device-id", os.environ["DEVICE_ID"],
        "--model", os.environ["DEVICE_MODEL"],
        "--host", os.environ["DEVICE_HOST"],
        "--port", os.environ.get("DEVICE_PORT") or "22",
        "--username", os.environ["SIP_ABA_SSH_USERNAME"],
        "--password-env", "ENV:SIP_ABA_SSH_PASSWORD",
        "--output", str(output),
    ]
    platform = os.environ.get("DEVICE_PLATFORM") or ""
    if platform in {"mt7621", "mt7981"}:
        args += ["--platform-id", platform]
    return args


def run_structural_probes(evidence: Path) -> None:
    scripts = {
        "dut-web-auth-symbols": "probe_dut_web_auth_symbols.py",
        "dut-web-checkpasswd": "probe_dut_web_checkpasswd.py",
        "dut-web-checkpasswd-flow": "probe_dut_web_checkpasswd_flow.py",
        "dut-web-authres-flow": "probe_dut_web_authres_flow.py",
        "dut-web-login-flow": "probe_dut_web_login_flow.py",
        "dut-web-auth-source": "probe_dut_web_auth_source_ssh.py",
    }
    for name, script in scripts.items():
        run([sys.executable, str(ROOT / "tools" / script), *device_args(evidence / f"{name}.json")])


def run_web_public_and_binding(evidence: Path, runtime: Path) -> None:
    base_url = os.environ.get("WEB_BASE_URL") or "https://10.48.8.74:10003"
    run([
        sys.executable, str(ROOT / "tools/probe_current_web_auth_source.py"),
        "--base-url", base_url,
        "--output", str(evidence / "public-web-auth-source.json"),
    ])
    profile = ROOT / "profiles/web_api/apf3260m_reyeeos_2_421_voip_v1.yaml"
    out_env = runtime / "web_credential.env"
    out_evidence = evidence / "web-credential-resolution.json"
    args = [
        sys.executable, str(ROOT / "tools/resolve_current_web_credential_env.py"),
        "--base-url", base_url,
        "--device-id", os.environ["DEVICE_ID"], "--model", os.environ["DEVICE_MODEL"],
        "--device-host", os.environ["DEVICE_HOST"],
        "--ssh-port", os.environ.get("DEVICE_PORT") or "22",
        "--ssh-username", os.environ["SIP_ABA_SSH_USERNAME"],
        "--ssh-password-env", "ENV:SIP_ABA_SSH_PASSWORD",
        "--profile-path", str(profile), "--secret-file", str(runtime / "no-auto-secret.yaml"),
        "--username-env", "WEB_USERNAME", "--password-env", "WEB_PASSWORD",
        "--output", str(out_env), "--evidence-output", str(out_evidence), "--insecure-tls",
    ]
    platform = os.environ.get("DEVICE_PLATFORM") or ""
    if platform in {"mt7621", "mt7981"}:
        args += ["--platform-id", platform]
    run(args)
    out_env.chmod(0o600)


async def web_auth_runtime(evidence: Path) -> None:
    import httpx
    from app.automation.adapters.web_auth.apf3260m import build_apf3260m_luci_auth_provider
    from app.automation.adapters.web_auth.base import WebCredential
    from app.infrastructure.transport.http import HttpApiTransport

    base_url = os.environ.get("WEB_BASE_URL") or "https://10.48.8.74:10003"
    client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=15.0)
    transport = HttpApiTransport(base_url, client=client)
    provider = build_apf3260m_luci_auth_provider(timestamp_provider=lambda: str(int(time.time())))
    credential = WebCredential(username=os.environ["WEB_USERNAME"], password=os.environ["WEB_PASSWORD"])
    started = time.monotonic()
    try:
        session = await provider.authenticate(transport, credential)
        payload = {
            "auth": "PASS",
            "sid_present": bool(session.query.get("auth")),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "mutation_executed": False,
            "secret_values_emitted": False,
        }
        write_json(evidence / "web-auth-runtime.json", payload)
    finally:
        await client.aclose()


async def read_dut_identity(evidence: Path, runtime: Path) -> dict[str, str]:
    from app.capture_v2.gate.context import build_asyncssh_adapter
    from app.capture_v2.gate.models import GateDeviceSpec
    from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
    from app.infrastructure.transport.ssh import SharedSshTransport

    spec = GateDeviceSpec(
        device_id=os.environ["DEVICE_ID"], model=os.environ["DEVICE_MODEL"],
        host=os.environ["DEVICE_HOST"], port=int(os.environ.get("DEVICE_PORT") or "22"),
        username=os.environ["SIP_ABA_SSH_USERNAME"],
        platform_id=os.environ.get("DEVICE_PLATFORM") or None,
    )
    adapter = build_asyncssh_adapter(spec, password_env="ENV:SIP_ABA_SSH_PASSWORD")
    transport = SharedSshTransport(adapter)
    await transport.connect()
    try:
        result = await ConfigFrameworkExecutor(transport, allowed_modules=("voipUserInfo",)).get("voipUserInfo", timeout=20.0)
    finally:
        await transport.disconnect()
    data = result.raw.get("data") if isinstance(result.raw, Mapping) else result.data
    if not result.success or not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        raise RuntimeError("CONSOLIDATED_DUT_IDENTITY_READ_FAILED")
    row = data[0]
    private = {k: str(row.get(k) or "") for k in ("number", "disName", "authId", "passwd")}
    private_path = runtime / "dut_identity_private.json"
    private_path.write_text(json.dumps(private, ensure_ascii=False), encoding="utf-8")
    private_path.chmod(0o600)
    safe = {
        "mutation_executed": False,
        "transport": "ssh",
        "backend": "config_framework",
        "operation": "get",
        "number": private["number"],
        "number_ascii_digits": bool(private["number"] and private["number"].isascii() and private["number"].isdigit()),
        "disname_equals_number": private["disName"] == private["number"],
        "auth_id_present": bool(private["authId"]),
        "auth_id_equals_number": private["authId"] == private["number"] if private["authId"] else None,
        "passwd_present": bool(private["passwd"]),
        "secret_values_emitted": False,
    }
    write_json(evidence / "dut-identity-safe.json", safe)
    return private


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pbx_source_contract(evidence: Path) -> None:
    root = Path(os.environ.get("FUSIONPBX_ROOT") or "/var/www/fusionpbx")
    paths = {
        "copy": root / "app/extensions/extension_copy.php",
        "extension": root / "app/extensions/resources/classes/extension.php",
        "database": root / "resources/classes/database.php",
        "require": root / "resources/require.php",
    }
    if not all(path.is_file() for path in paths.values()):
        raise RuntimeError("FUSIONPBX_CREATE_SOURCE_NOT_FOUND")
    copy = paths["copy"].read_text(encoding="utf-8", errors="ignore")
    ext = paths["extension"].read_text(encoding="utf-8", errors="ignore")
    copy_scalar_fields = [
        "accountcode", "effective_caller_id_name", "effective_caller_id_number",
        "outbound_caller_id_name", "outbound_caller_id_number", "emergency_caller_id_name",
        "emergency_caller_id_number", "directory_visible", "directory_exten_visible", "limit_max",
        "limit_destination", "user_context", "missed_call_app", "missed_call_data", "toll_allow",
        "call_timeout", "call_group", "user_record", "hold_music", "auth_acl", "cidr",
        "sip_force_contact", "nibble_account", "sip_force_expires", "mwi_account",
        "sip_bypass_media", "dial_string", "extension_type", "enabled",
    ]
    scalar_bound = all(
        re.search(r'\bsql\s*\.?=\s*["\']' + re.escape(field), copy)
        or (field + ",") in copy or (field + " ") in copy
        for field in copy_scalar_fields
    )
    copy_facts = {
        "array_root_extensions": "['extensions'][0]" in copy,
        "sets_domain_uuid": "['domain_uuid']" in copy,
        "sets_extension_uuid": "['extension_uuid']" in copy,
        "sets_extension": "['extension']" in copy,
        "sets_number_alias": "['number_alias']" in copy,
        "sets_password": "['password']" in copy,
        "sets_accountcode": "['accountcode']" in copy,
        "sets_enabled": "['enabled']" in copy,
        "database_save_array": bool(re.search(r"\$database->save\s*\(\$array\)", copy)),
        "copy_scalar_fields_source_bound": scalar_bound,
    }
    class_facts = {
        "exists": bool(re.search(r"public\s+function\s+exists\s*\(", ext)),
        "delete": bool(re.search(r"public\s+function\s+delete\s*\(", ext)),
        "exists_checks_number_alias": "number_alias = :extension" in ext,
    }
    if not all(copy_facts.values()) or not all(class_facts.values()):
        raise RuntimeError("FUSIONPBX_CREATE_CONTRACT_INCOMPLETE")
    expected = {
        "require": os.environ.get("EXPECTED_REQUIRE_SHA256"),
        "database": os.environ.get("EXPECTED_DATABASE_SHA256"),
        "extension": os.environ.get("EXPECTED_EXTENSION_SHA256"),
    }
    for key, expected_hash in expected.items():
        if expected_hash and sha256(paths[key]) != expected_hash:
            raise RuntimeError(f"FUSIONPBX_SOURCE_FENCE_FAILED:{key}")
    write_json(evidence / "fusionpbx-create-contract.json", {
        "mutation_executed": False,
        "secret_values_emitted": False,
        "source_sha256": {k: sha256(v) for k, v in paths.items()},
        "copy_contract": copy_facts,
        "class_contract": class_facts,
    })


def pbx_runtime_source(evidence: Path) -> None:
    root = Path(os.environ.get("FUSIONPBX_ROOT") or "/var/www/fusionpbx")
    relpaths = [
        "app/extensions/resources/classes/extension.php",
        "app/extensions/extension_edit.php",
        "app/extensions/extension_imports.php",
        "resources/require.php",
        "resources/classes/database.php",
    ]
    texts = {rel: (root / rel).read_text(encoding="utf-8", errors="ignore") if (root / rel).is_file() else "" for rel in relpaths}
    if not all((root / rel).is_file() for rel in relpaths):
        raise RuntimeError("FUSIONPBX_PROVIDER_SOURCE_NOT_FOUND")
    class_text = texts[relpaths[0]]
    edit_text = texts[relpaths[1]]
    methods = sorted(set(re.findall(r"public\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", class_text)))
    fs_cli = shutil.which("fs_cli")
    fs_rc = None
    current_7102 = None
    pool: set[int] = set()
    if fs_cli:
        cp = subprocess.run([fs_cli, "-x", "list_users"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False)
        fs_rc = cp.returncode
        if fs_rc == 0:
            current_7102 = False
            for raw in cp.stdout.splitlines():
                first = re.split(r"[|,\s]", raw.strip(), maxsplit=1)[0].strip()
                if first == "7102": current_7102 = True
                if re.fullmatch(r"79\d\d", first): pool.add(int(first))
    payload = {
        "mutation_executed": False, "secret_values_emitted": False,
        "source_files": {rel: {"present": True, "sha256": sha256(root / rel), "size": (root / rel).stat().st_size} for rel in relpaths},
        "extension_class_public_methods": methods,
        "extension_contract_facts": {
            "exists_method_present": "exists" in methods,
            "delete_method_present": "delete" in methods,
            "exists_checks_number_alias": "number_alias = :extension" in class_text,
            "web_add_permission_guard_present": "permission_exists('extension_add')" in edit_text,
            "web_edit_permission_guard_present": "permission_exists('extension_edit')" in edit_text,
            "web_extension_field_present": '$_POST["extension"]' in edit_text,
            "web_number_alias_field_present": '$_POST["number_alias"]' in edit_text,
            "web_password_field_present": '$_POST["password"]' in edit_text,
        },
        "runtime": {"fs_cli_path": fs_cli, "fs_cli_list_users_rc": fs_rc},
        "numeric_pool_7900_7999": {
            "runtime_observable": fs_rc == 0,
            "occupied_count": len(pool) if fs_rc == 0 else None,
            "first_available": next((v for v in range(7900, 8000) if v not in pool), None) if fs_rc == 0 else None,
            "existing_7102_observed": current_7102,
        },
    }
    write_json(evidence / "fusionpbx-provider-source.json", payload)


def pbx_db_and_identity(evidence: Path, runtime: Path) -> None:
    php = runtime / "consolidated_pbx_probe.php"
    php.write_text("""<?php
declare(strict_types=1);
$root = getenv('FUSIONPBX_ROOT'); $runtime = getenv('CONSOLIDATED_RUNTIME_ROOT');
if (!$root || !$runtime) { fwrite(STDERR, "PBX_RUNTIME_REQUIRED\n"); exit(2); }
require_once $root . '/resources/require.php';
require_once $root . '/app/extensions/resources/classes/extension.php';
global $database;
if (!$database) { fwrite(STDERR, "FUSIONPBX_DATABASE_UNAVAILABLE\n"); exit(3); }
$domains = $database->select("select domain_uuid, domain_name from v_domains where domain_enabled = true order by domain_name asc", null, 'all');
if (!is_array($domains) || count($domains) === 0) { fwrite(STDERR, "FUSIONPBX_ACTIVE_DOMAIN_REQUIRED\n"); exit(4); }
$private = json_decode(file_get_contents($runtime . '/dut_identity_private.json'), true);
if (!is_array($private)) { fwrite(STDERR, "DUT_IDENTITY_PRIVATE_REQUIRED\n"); exit(5); }
$number = strval($private['number'] ?? ''); $auth_id = strval($private['authId'] ?? ''); $passwd = strval($private['passwd'] ?? '');
$result = ['mutation_executed'=>false,'secret_values_emitted'=>false,'active_domain_count'=>count($domains),'domains'=>[]];
foreach ($domains as $domain) {
  $domain_uuid = strval($domain['domain_uuid'] ?? ''); $domain_name = strval($domain['domain_name'] ?? '');
  if ($domain_uuid === '' || $domain_name === '') continue;
  $extension = new extension(['database'=>$database,'domain_uuid'=>$domain_uuid,'domain_name'=>$domain_name,'user_uuid'=>'']);
  $occupied = [];
  for ($candidate=7900; $candidate<=7999; $candidate++) if ($extension->exists($domain_uuid, strval($candidate))) $occupied[]=$candidate;
  $rows = $database->select("select extension, number_alias, password from v_extensions where domain_uuid = :domain_uuid and enabled = true and (extension = :number or number_alias = :number or extension = :auth_id or number_alias = :auth_id)", ['domain_uuid'=>$domain_uuid,'number'=>$number,'auth_id'=>$auth_id], 'all');
  if (!is_array($rows)) $rows=[];
  $auth_password_matches=false; foreach ($rows as $row) { $ext=strval($row['extension']??''); $alias=strval($row['number_alias']??''); $pw=strval($row['password']??''); if ($auth_id!=='' && ($ext===$auth_id || $alias===$auth_id) && $passwd!=='' && hash_equals($pw,$passwd)) $auth_password_matches=true; }
  $first_available=null; for ($candidate=7900; $candidate<=7999; $candidate++) if (!in_array($candidate,$occupied,true)) { $first_available=$candidate; break; }
  $result['domains'][] = [
    'domain_identity_sha256'=>hash('sha256',$domain_uuid.'|'.$domain_name),
    'pool_occupied_count'=>count($occupied),'first_available'=>$first_available,
    'extension_or_alias_7102_exists'=>$extension->exists($domain_uuid,'7102'),
    'matching_row_count'=>count($rows),'auth_password_matches'=>$auth_password_matches,
  ];
}
echo json_encode($result, JSON_UNESCAPED_SLASHES) . PHP_EOL;
?>
""", encoding="utf-8")
    php.chmod(0o600)
    cp = run(["php", str(php)], timeout=30)
    data = json.loads(cp.stdout)
    if data.get("mutation_executed") is not False or data.get("secret_values_emitted") is not False:
        raise RuntimeError("PBX_READ_ONLY_CONTRACT_BROKEN")
    if int(data.get("active_domain_count") or 0) < 1 or not data.get("domains"):
        raise RuntimeError("PBX_ACTIVE_DOMAIN_REQUIRED")
    write_json(evidence / "fusionpbx-db-and-identity.json", data)


def credential_source_metadata(evidence: Path, env_file: Path) -> None:
    key_re = re.compile(r"(?:WEB|LUCI|GUI|HTTP|ADMIN|LOGIN|DEVICE).*(?:USER|USERNAME|PASS|PASSWORD|CRED|AUTH)|(?:USER|USERNAME|PASS|PASSWORD|CRED|AUTH).*(?:WEB|LUCI|GUI|HTTP|ADMIN|LOGIN|DEVICE)", re.I)
    keys = []
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key = line.split("=", 1)[0].removeprefix("export ").strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key_re.search(key): keys.append(key)
    write_json(evidence / "web-credential-source.json", {"mutation_executed": False, "secret_values_emitted": False, "host_env_keys": sorted(set(keys))})


def registration_observer(evidence: Path) -> None:
    from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe
    probe = FusionPbxRegistrationProbe()
    registered, details, _refs = probe._observe_once("7102")
    write_json(evidence / "pbx-registration-observer.json", {
        "current_7102_observed": bool(registered),
        "mutation_executed": False,
        "secret_values_emitted": details.get("secret_values_emitted") is False,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidated exact-head read-only VOIP probes")
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    evidence = Path(args.evidence_root); runtime = Path(args.runtime_root); env_file = Path(args.env_file)
    evidence.mkdir(parents=True, exist_ok=True); runtime.mkdir(parents=True, exist_ok=True)
    os.environ["CONSOLIDATED_RUNTIME_ROOT"] = str(runtime)
    os.environ.setdefault("FUSIONPBX_ROOT", "/var/www/fusionpbx")
    run_structural_probes(evidence)
    credential_source_metadata(evidence, env_file)
    run_web_public_and_binding(evidence, runtime)
    asyncio.run(web_auth_runtime(evidence))
    asyncio.run(read_dut_identity(evidence, runtime))
    pbx_source_contract(evidence)
    pbx_runtime_source(evidence)
    pbx_db_and_identity(evidence, runtime)
    registration_observer(evidence)
    write_json(evidence / "summary.json", {
        "CONSOLIDATED_EXACT_HEAD_PROBES": "PASS",
        "mutation_executed": False,
        "secret_values_emitted": False,
    })
    print("CONSOLIDATED_EXACT_HEAD_PROBES=PASS mutation=false secret_values_emitted=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
