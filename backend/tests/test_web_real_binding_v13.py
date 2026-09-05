from pathlib import Path

import pytest

from app.automation.adapters.entries.web import WebEntryAdapter, WebEntryError
from app.automation.adapters.web_auth.legacy_luci import _protocol_success
from app.automation.adapters.web_profiles.schema import WebApiProfile
from app.infrastructure.transport.http import HttpEvidence, HttpResponse, mask_http_secrets


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/web_api/apf3260m_reyeeos_2_421_voip_v1.yaml"
READ_MODULES = (
    "voice_vlan",
    "voipServInfo",
    "voipUserInfo",
    "voipFxsTbl",
    "voipRegState",
    "voipZoneInfo",
    "voipAdvanced",
)
WRITE_MODULES = (
    "voice_vlan",
    "voipServInfo",
    "voipUserInfo",
    "voipFxsTbl",
    "voipAdvanced",
)


def _response(body, status=200):
    evidence = HttpEvidence(
        request_id="req-1",
        attempt=1,
        method="POST",
        path="/cgi-bin/luci/api/cmd",
        request={},
        response={},
        elapsed_ms=1.0,
    )
    return HttpResponse(
        status_code=status,
        headers={},
        json_body=body,
        text="",
        request_id="req-1",
        elapsed_ms=1.0,
        evidence=evidence,
    )


def test_real_profile_is_source_bound_and_preserves_har_order_and_side_effects():
    profile = WebApiProfile.load_yaml(PROFILE)
    read = profile.operation("voip.account.read")
    write = profile.operation("voip.account.configure")

    assert read.source_bound is True
    assert write.source_bound is True
    assert read.endpoint == "/cgi-bin/luci/api/cmd"
    assert write.endpoint == "/cgi-bin/luci/api/cmd"
    assert read.rpc_method == write.rpc_method == "cmdArr"
    assert tuple(item.module for item in read.rpc_items) == READ_MODULES
    assert tuple(item.module for item in write.rpc_items) == WRITE_MODULES
    assert tuple(item.method for item in read.rpc_items) == (
        "devConfig.get", "devConfig.get", "devConfig.get", "devConfig.get",
        "devSta.get", "devConfig.get", "devConfig.get",
    )
    assert tuple(item.method for item in write.rpc_items) == ("devConfig.set",) * 5
    assert write.writable_modules == WRITE_MODULES
    assert "HAR:10.48.8.74.har" in profile.source_evidence


def test_cmd_array_read_payload_has_explicit_request_index_mapping_contract():
    profile = WebApiProfile.load_yaml(PROFILE)
    read = profile.operation("voip.account.read")
    payload = WebEntryAdapter._payload(read, {})

    assert payload["method"] == "cmdArr"
    assert payload["params"]["device"] == "pc"
    requests = payload["params"]["params"]
    assert [item["params"]["module"] for item in requests] == list(READ_MODULES)
    assert all(item["params"]["noParse"] is False for item in requests)
    assert all(item["params"]["async"] is None for item in requests)
    assert all(item["params"]["remoteIp"] is False for item in requests)


def test_cmd_array_write_requires_complete_five_module_bundle():
    profile = WebApiProfile.load_yaml(PROFILE)
    write = profile.operation("voip.account.configure")
    bundle = {module: {"module": module} for module in WRITE_MODULES}
    payload = WebEntryAdapter._payload(write, {"bundle": bundle})

    requests = payload["params"]["params"]
    assert [item["params"]["module"] for item in requests] == list(WRITE_MODULES)
    assert [item["params"]["data"] for item in requests] == [bundle[m] for m in WRITE_MODULES]

    incomplete = dict(bundle)
    incomplete.pop("voipAdvanced")
    with pytest.raises(WebEntryError, match="WEB_CMD_ARRAY_BUNDLE_MODULE_MISSING:voipAdvanced"):
        WebEntryAdapter._payload(write, {"bundle": incomplete})


def test_web_save_accepts_only_http_top_level_and_all_five_subrequests_success():
    profile = WebApiProfile.load_yaml(PROFILE)
    write = profile.operation("voip.account.configure")
    success = {"rcode": "00000000", "rmsg": "success"}

    accepted = WebEntryAdapter._to_result(
        _response({"code": 0, "error": None, "data": [dict(success) for _ in range(5)]}),
        write,
    )
    assert accepted.accepted is True
    assert accepted.error is None

    failed_items = [dict(success) for _ in range(5)]
    failed_items[2] = {"rcode": "02870001", "rmsg": "failed"}
    rejected = WebEntryAdapter._to_result(
        _response({"code": 0, "error": None, "data": failed_items}),
        write,
    )
    assert rejected.accepted is False
    assert rejected.error == "WEB_CMD_ARRAY_SUBREQUEST_REJECTED"

    top_rejected = WebEntryAdapter._to_result(
        _response({"code": 1, "error": "denied", "data": [dict(success) for _ in range(5)]}),
        write,
    )
    assert top_rejected.accepted is False
    assert top_rejected.error == "WEB_CMD_ARRAY_TOP_LEVEL_REJECTED"


def test_web_read_maps_response_index_to_module_and_masks_sip_password():
    profile = WebApiProfile.load_yaml(PROFILE)
    read = profile.operation("voip.account.read")
    data = [
        {"data": {"value": module}}
        for module in READ_MODULES
    ]
    data[2] = {
        "data": {
            "data": [{"number": "7102", "authId": "7102", "passwd": "secret-value"}]
        }
    }
    result = WebEntryAdapter._to_result(
        _response({"code": 0, "error": None, "data": data}),
        read,
    )

    assert result.accepted is True
    assert result.output["request_index_to_module"]["2"] == "voipUserInfo"
    account = result.output["modules"]["voipUserInfo"]["data"][0]
    assert account["number"] == "7102"
    assert account["passwd"] == "***"


def test_http_masking_covers_luci_pwd_auth_sid_token_and_referer_stok():
    masked = mask_http_secrets(
        {
            "json": {"params": {"pwd": "cipher", "passwd": "sip-secret"}},
            "query": {"auth": "sid-secret"},
            "response": {"data": {"sid": "sid-secret", "token": "tok-secret"}},
            "headers": {
                "Referer": "https://10.48.8.74:10003/cgi-bin/luci/;stok=tok-secret/admin",
                "Authorization": "Bearer abc",
            },
        }
    )
    assert masked["json"]["params"]["pwd"] == "***"
    assert masked["json"]["params"]["passwd"] == "***"
    assert masked["query"]["auth"] == "***"
    assert masked["response"]["data"]["sid"] == "***"
    assert masked["response"]["data"]["token"] == "***"
    assert ";stok=***/" in masked["headers"]["Referer"]
    assert masked["headers"]["Authorization"] == "***"


def test_luci_login_requires_protocol_success_not_only_http_200():
    assert _protocol_success(_response({"code": 0, "error": None, "data": {"sid": "x"}})) is True
    assert _protocol_success(_response({"code": 1, "error": "bad", "data": {"sid": "x"}})) is False


def test_web_entry_adapter_has_no_ssh_fallback_dependency():
    source = (ROOT / "backend/app/automation/adapters/entries/web.py").read_text(encoding="utf-8")
    assert "SharedSshTransport" not in source
    assert "AsyncSSHDeviceAdapter" not in source
    assert "dev_config set" not in source
