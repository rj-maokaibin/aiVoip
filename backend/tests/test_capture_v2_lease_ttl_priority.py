from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.capture_v2.factory import _lease_ttl


def _profile_with_ttl(ttl: float):
    # _lease_ttl only reads effective_profile.resolved; SimpleNamespace is enough.
    return SimpleNamespace(resolved={"lease": {"ttl_seconds": ttl}})


def test_explicit_lease_ttl_wins_over_profile():
    """The SIP A-B-A gate requests a long explicit TTL (900s); it must win over
    the profile's short default so the lease survives between capture phases."""
    profile = _profile_with_ttl(30.0)
    assert _lease_ttl(effective_profile=profile, explicit=900.0) == 900.0


def test_profile_ttl_used_when_no_explicit():
    """Without an explicit TTL the resolved profile lease.ttl_seconds applies
    (production capture path), preserving the existing behavior."""
    profile = _profile_with_ttl(30.0)
    assert _lease_ttl(effective_profile=profile, explicit=None) == 30.0


def test_settings_default_when_no_profile_and_no_explicit():
    from app.core.config import settings

    assert _lease_ttl(effective_profile=None, explicit=None) == float(
        settings.capture_v2_lease_ttl_seconds
    )
