from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import resolve_current_web_credential_env as resolver


def test_device_webpass_is_preferred_over_ssh_password() -> None:
    root = {
        "device": [{
            "host": "10.48.8.74",
            "username": "root",
            "password": "ssh-only-secret",
            "webpass": "web-only-secret",
        }]
    }
    candidates = resolver._matching_secret_candidates_from_root(
        root, allowed_hosts={"10.48.8.74": "device_host"}
    )
    assert len(candidates) == 1
    assert candidates[0].username == "root"
    assert candidates[0].password == "web-only-secret"
    assert "ssh-only-secret" not in repr(candidates[0])
