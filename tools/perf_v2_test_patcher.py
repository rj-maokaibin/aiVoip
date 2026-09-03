#!/usr/bin/env python3
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "backend" / "tests" / "test_production_offline_build_fallback.py"
text = path.read_text(encoding="utf-8")
old = '''def test_production_cli_prefers_pull_and_uses_guarded_fallback():
    text = (Path(__file__).resolve().parents[2] / "deploy/voip-ai").read_text(encoding="utf-8")
    online_command = (
        'compose build --pull --build-arg '
        '"BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"'
    )
    offline_command = (
        'compose build --pull=false --build-arg '
        '"BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"'
    )
    online = text.index(online_command)
    guard = text.index("offline_build_fallback.py")
    offline = text.index(offline_command)
    assert online < guard < offline
    assert "VOIP_OFFLINE_BUILD_AUDIT" in text
    assert text.count('BUILD_REVISION=$(env_value BUILD_REVISION)') >= 2
'''
new = '''def test_production_cli_prefers_pull_and_uses_guarded_fallback():
    text = (Path(__file__).resolve().parents[2] / "deploy/voip-ai").read_text(encoding="utf-8")
    online_command = (
        'compose build --pull --build-arg '
        '"BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"'
    )
    offline_command = (
        'compose build --pull=false --build-arg '
        '"BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"'
    )
    probe = text.index("REGISTRY_PREFLIGHT=FAIL")
    preflight_guard = text.index("offline_build_fallback.py", probe)
    preflight_offline = text.index(offline_command, preflight_guard)
    online = text.index(online_command, preflight_offline)
    postbuild_guard = text.index("offline_build_fallback.py", online)
    postbuild_offline = text.index(offline_command, postbuild_guard)
    assert probe < preflight_guard < preflight_offline < online < postbuild_guard < postbuild_offline
    assert "VOIP_OFFLINE_BUILD_AUDIT" in text
    assert "VOIP_REGISTRY_PROBE_TIMEOUT_SECONDS" in text
    assert text.count('BUILD_REVISION=$(env_value BUILD_REVISION)') >= 3
'''
if old not in text:
    raise SystemExit("existing offline fallback test anchor missing")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
