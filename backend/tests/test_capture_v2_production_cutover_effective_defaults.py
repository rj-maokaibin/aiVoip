from pathlib import Path

from app.capture_v2.control import production_cutover_guarded as compat


def test_missing_v2_prestate_keys_use_application_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(
        compat,
        "_ORIGINAL_READ_SAFE_ENV",
        lambda _repo: (0, {"APP_ENV": "production", "REPRODUCTION_PLATFORM_MODE": "mock"}, ""),
    )

    rc, values, error = compat._read_safe_env_with_effective_defaults(tmp_path)

    assert rc == 0
    assert error == ""
    assert values["CAPTURE_ENGINE_VERSION"] == "V1"
    assert values["CAPTURE_V2_PRODUCTION_ENABLED"] == "false"
    assert values["CAPTURE_V2_ACTIVATION_REHEARSAL"] == "false"
    assert set(values[compat._DEFAULTED_MARKER].split(",")) == {
        "CAPTURE_ENGINE_VERSION",
        "CAPTURE_V2_PRODUCTION_ENABLED",
        "CAPTURE_V2_ACTIVATION_REHEARSAL",
    }


def test_explicit_v2_prestate_values_are_never_overridden(monkeypatch, tmp_path):
    supplied = {
        "APP_ENV": "production",
        "CAPTURE_ENGINE_VERSION": "V2",
        "CAPTURE_V2_PRODUCTION_ENABLED": "true",
        "CAPTURE_V2_ACTIVATION_REHEARSAL": "true",
    }
    monkeypatch.setattr(
        compat,
        "_ORIGINAL_READ_SAFE_ENV",
        lambda _repo: (0, dict(supplied), ""),
    )

    rc, values, error = compat._read_safe_env_with_effective_defaults(tmp_path)

    assert rc == 0
    assert error == ""
    assert values == supplied
    assert compat._DEFAULTED_MARKER not in values


def test_run_scopes_reader_override_and_surfaces_defaulted_keys(monkeypatch, tmp_path):
    original_reader = compat._base._read_safe_env

    def fake_run(*, repo_root: Path, authorization_path: Path):
        assert compat._base._read_safe_env is compat._read_safe_env_with_effective_defaults
        return 0, {
            "verdict": "PASS",
            "pre_env": {
                "APP_ENV": "production",
                compat._DEFAULTED_MARKER: "CAPTURE_ENGINE_VERSION,CAPTURE_V2_PRODUCTION_ENABLED",
            },
        }

    monkeypatch.setattr(compat._base, "run", fake_run)
    rc, payload = compat.run(repo_root=tmp_path, authorization_path=tmp_path / "auth.json")

    assert rc == 0
    assert compat._base._read_safe_env is original_reader
    assert payload["pre_env_defaulted_keys"] == [
        "CAPTURE_ENGINE_VERSION",
        "CAPTURE_V2_PRODUCTION_ENABLED",
    ]
    assert payload["pre_env_defaults_source"] == "APPLICATION_RUNTIME_DEFAULTS"
    assert compat._DEFAULTED_MARKER not in payload["pre_env"]
