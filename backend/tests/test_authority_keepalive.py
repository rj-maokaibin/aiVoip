from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from app.infrastructure.device_authority.keepalive import AuthorityKeepalive


@dataclass(frozen=True)
class Token:
    epoch: int
    expires_at: datetime


class FakeAuthority:
    def __init__(self) -> None:
        self.renew_calls = 0
        self.release_calls = 0
        self.fail_renew = False

    def acquire(self, **_kwargs):
        raise AssertionError("keepalive must never acquire")

    def renew(self, token: Token) -> Token:
        self.renew_calls += 1
        if self.fail_renew:
            raise RuntimeError("fenced")
        return replace(
            token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
        )

    def validate(self, token: Token) -> Token:
        return token

    def release(self, token: Token) -> None:
        self.release_calls += 1


@pytest.mark.asyncio
async def test_keepalive_renews_same_authority_without_reacquire() -> None:
    authority = FakeAuthority()
    token = Token(
        epoch=7,
        expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=20),
    )
    keepalive = AuthorityKeepalive(authority, interval_seconds=0.01)
    keepalive.start(token)
    await asyncio.sleep(0.035)
    await keepalive.stop()

    assert authority.renew_calls >= 2
    assert keepalive.token.epoch == 7
    assert authority.release_calls == 0


@pytest.mark.asyncio
async def test_keepalive_stop_prevents_future_renewals_before_release() -> None:
    authority = FakeAuthority()
    token = Token(
        epoch=9,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    keepalive = AuthorityKeepalive(authority, interval_seconds=0.01)
    keepalive.start(token)
    await asyncio.sleep(0.025)
    await keepalive.stop()
    calls_after_stop = authority.renew_calls
    await asyncio.sleep(0.03)

    assert authority.renew_calls == calls_after_stop


@pytest.mark.asyncio
async def test_keepalive_failure_is_fenced_and_never_reacquires() -> None:
    authority = FakeAuthority()
    authority.fail_renew = True
    token = Token(
        epoch=11,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    keepalive = AuthorityKeepalive(authority, interval_seconds=0.01)
    keepalive.start(token)
    await asyncio.sleep(0.025)

    with pytest.raises(RuntimeError, match="AUTHORITY_KEEPALIVE_FAILED:RuntimeError"):
        await keepalive.stop()
    assert authority.renew_calls == 1
    assert authority.release_calls == 0
