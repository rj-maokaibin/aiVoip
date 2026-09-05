from __future__ import annotations

import inspect

from app.automation.adapters.pbx.base import TemporaryExtensionSpec
from app.automation.adapters.pbx.fusionpbx_local import FusionPbxLocalProvider


def test_temporary_extension_secret_is_not_repr_or_compare_material() -> None:
    a = TemporaryExtensionSpec(
        extension_uuid="11111111-1111-4111-8111-111111111111",
        extension="7900",
        password="raw-runtime-secret-a",
    )
    b = TemporaryExtensionSpec(
        extension_uuid=a.extension_uuid,
        extension=a.extension,
        password="raw-runtime-secret-b",
    )
    assert "raw-runtime-secret-a" not in repr(a)
    assert a == b


def test_fusionpbx_provider_is_exact_source_fenced_to_controlled_probe_hashes() -> None:
    assert FusionPbxLocalProvider.EXPECTED_SOURCE_SHA256 == {
        "resources/require.php": "2d29ea99b786c5c111df4cfcc06319138f1544b30300f6aea70635f1100fd761",
        "resources/classes/database.php": "6a0b95eb29d1c27b24d4dcc4a8582b959c627a9173b973b19b2435e1e399dbbb",
        "app/extensions/resources/classes/extension.php": "842c070880ebe82cca676d8b04cda8377ca931180c91f72a499e813c3f3eaed9",
        "app/extensions/extension_copy.php": "5be1b1f491559553f78f1af2573d982a73583b1cafae54823fd0be42c2827d76",
    }


def test_fusionpbx_create_contract_uses_official_database_shape_and_no_http_api() -> None:
    source = FusionPbxLocalProvider._PHP
    for field in (
        "domain_uuid",
        "extension_uuid",
        "extension",
        "number_alias",
        "password",
        "accountcode",
        "enabled",
    ):
        assert f"['{field}']" in source
    assert "$database->save($array)" in source
    assert "$database->delete($array)" in source
    assert "$extension->exists($domain_uuid, $target)" in source
    assert "http://" not in source and "https://" not in source


def test_fusionpbx_provider_observes_before_cleanup_and_never_retries_create() -> None:
    delete_source = inspect.getsource(FusionPbxLocalProvider.delete)
    create_source = inspect.getsource(FusionPbxLocalProvider.create)
    assert delete_source.index("self.inspect(spec)") < delete_source.index('"action": "delete"')
    assert create_source.count('"action": "create"') == 1
    assert "retry" not in create_source.lower()


def test_fusionpbx_secret_transport_uses_stdin_not_argv_or_environment() -> None:
    source = inspect.getsource(FusionPbxLocalProvider._php_payload)
    assert "input=json.dumps" in source
    assert "pass_fds" in source
    assert '"password"' not in source
    assert "stderr=subprocess.DEVNULL" in source
