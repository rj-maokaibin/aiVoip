from __future__ import annotations

from app.automation.adapters.web_auth.base import WebCredential
from app.infrastructure.transport.http import HttpRequest


def test_web_credential_repr_never_exposes_password() -> None:
    secret = "runtime-web-secret-value"
    credential = WebCredential(username="device-user", password=secret)
    assert secret not in repr(credential)
    assert credential.password == secret


def test_http_request_repr_never_exposes_auth_material_or_payload() -> None:
    secret = "raw-secret"
    encrypted = "encrypted-secret"
    sid = "session-secret"
    request = HttpRequest(
        method="POST",
        path="/cgi-bin/luci/api/auth",
        headers={"Authorization": secret},
        query={"auth": sid},
        cookies={"sid": sid},
        json_body={"method": "login", "params": {"pwd": encrypted}},
        sensitive_values=(secret, encrypted, sid),
    )
    text = repr(request)
    assert secret not in text
    assert encrypted not in text
    assert sid not in text
    assert "login" not in text
    assert "HttpRequest(" in text
