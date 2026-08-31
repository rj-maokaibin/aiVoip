from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.integrations.secrets import SecretRef, SecretResolver, SecretResolutionError
from app.integrations.feishu.transport import FeishuLiveTransport
from app.integrations.poseidon import poseidon_bootstrap_configured


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    status: str
    blocking: bool
    category: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _secret(ref: SecretRef, name: str) -> str:
    try:
        return SecretResolver.resolve(ref, name=name, required=False)
    except SecretResolutionError:
        return ""


def runtime_release_readiness(*, profile_root: Path | None = None) -> dict[str, Any]:
    """Return production-readiness facts without converting missing runtime config to PASS."""
    items: list[ReadinessItem] = []

    # F2 implementation facts: these prove code contracts exist, not that production credentials/runtime are configured.
    items.extend([
        ReadinessItem("PRODUCTION_AUTH_IMPLEMENTATION", "PASS", True, "SECURITY", "gateway_hmac provider validates signed actor/role/timestamp assertions and rejects unsigned production headers."),
        ReadinessItem("SECRET_PROVIDER_IMPLEMENTATION", "PASS", True, "SECURITY", "SecretResolver supports mounted file, named environment and direct dev/e2e values without logging resolved secrets."),
        ReadinessItem("PRODUCTION_STORAGE_IMPLEMENTATION", "PASS", True, "STORAGE", "MinIO evidence backend supports immutable writes plus read/write probe and cleanup."),
        ReadinessItem("FEISHU_TRANSPORT_IMPLEMENTATION", "PASS", True, "INTEGRATION", "Feishu tenant-token send/update transport, persistent Case message binding and callback verification/handlers are implemented."),
    ])

    # EC-02 production adapter readiness.
    try:
        from app.platforms.registry import PlatformProfileRegistry
        root = profile_root or settings.profile_root
        loaded = PlatformProfileRegistry(root).get("RUIJIE_VOIP_AIM_V1")
        ready = loaded.definition.production_ready_for("AUTONOMOUS_REPRODUCTION")
        items.append(ReadinessItem(
            key="EC02_PLATFORM_PRODUCTION_READY",
            status="PASS" if ready else "BLOCKED",
            blocking=True,
            category="PLATFORM",
            detail="RUIJIE_VOIP_AIM_V1 autonomous reproduction contract is production-ready." if ready else "EC-02 remains partial; real DUT write/cleanup/event contracts are not fully confirmed.",
        ))
    except Exception as exc:
        items.append(ReadinessItem(
            key="EC02_PLATFORM_PRODUCTION_READY",
            status="UNVERIFIED",
            blocking=True,
            category="PLATFORM",
            detail=f"Platform contract could not be evaluated: {type(exc).__name__}",
        ))

    mock_mode = str(settings.reproduction_platform_mode).lower() == "mock"
    items.append(ReadinessItem(
        key="REAL_REPRODUCTION_PLATFORM",
        status="BLOCKED" if mock_mode else "PASS",
        blocking=True,
        category="PLATFORM",
        detail="Production release cannot run with REPRODUCTION_PLATFORM_MODE=mock." if mock_mode else "A non-mock reproduction platform is configured.",
    ))

    is_production = settings.app_env.lower() == "production"
    items.append(ReadinessItem(
        key="PRODUCTION_ENVIRONMENT",
        status="PASS" if is_production else "BLOCKED",
        blocking=True,
        category="RUNTIME",
        detail="APP_ENV=production." if is_production else f"APP_ENV={settings.app_env}; production release evidence must be collected with APP_ENV=production.",
    ))
    revision = str(getattr(settings, "build_revision", "")).strip()
    revision_ok = bool(revision and revision.lower() not in {"dev", "unknown", "local"})
    items.append(ReadinessItem(
        key="BUILD_REVISION_PINNED",
        status="PASS" if revision_ok else "BLOCKED",
        blocking=True,
        category="RUNTIME",
        detail=f"Build revision is pinned to {revision}." if revision_ok else "BUILD_REVISION must identify the immutable source/build revision; 'dev' is not release evidence.",
    ))

    provider = str(settings.credential_provider or "").lower()
    if provider == "api":
        credential_ok = bool(settings.credential_api_url)
        credential_detail = "API credential provider is configured." if credential_ok else "CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required for production DUT access."
    elif provider == "poseidon":
        credential_ok = poseidon_bootstrap_configured()
        credential_detail = "Poseidon credential provider is configured." if credential_ok else "CREDENTIAL_PROVIDER=poseidon requires the Poseidon bootstrap secret (sso.baichuan in /home/dev/secret.yaml)."
    else:
        credential_ok = False
        credential_detail = "CREDENTIAL_PROVIDER must be api or poseidon for production DUT access."
    items.append(ReadinessItem(
        key="PRODUCTION_CREDENTIAL_PROVIDER",
        status="PASS" if credential_ok else "BLOCKED",
        blocking=True,
        category="SECURITY",
        detail=credential_detail,
    ))

    storage_ok = str(settings.reproduction_storage_mode).lower() == "minio"
    items.append(ReadinessItem(
        key="PRODUCTION_REPRODUCTION_STORAGE",
        status="PASS" if storage_ok else "BLOCKED",
        blocking=True,
        category="STORAGE",
        detail="Reproduction evidence storage uses MinIO." if storage_ok else "REPRODUCTION_STORAGE_MODE must be minio for production; local storage is mock/dev only.",
    ))
    minio_access = _secret(SecretRef(settings.minio_access_key, settings.minio_access_key_file, settings.minio_access_key_env), "MINIO_ACCESS_KEY")
    minio_secret = _secret(SecretRef(settings.minio_secret_key, settings.minio_secret_key_file, settings.minio_secret_key_env), "MINIO_SECRET_KEY")
    defaults = {"", "voipminio", "voipminiosecret", "change-me", "minioadmin"}
    default_secret = minio_access in defaults or minio_secret in defaults
    items.append(ReadinessItem(
        key="PRODUCTION_DEFAULT_SECRETS_REPLACED",
        status="BLOCKED" if default_secret else "PASS",
        blocking=True,
        category="SECURITY",
        detail="Default MinIO credentials have been replaced." if not default_secret else "Default/example MinIO credentials must not be used for production release.",
    ))

    provider = str(getattr(settings, "production_auth_provider", "pending")).lower()
    auth_secret = _secret(SecretRef(settings.auth_gateway_hmac_secret, settings.auth_gateway_hmac_secret_file, settings.auth_gateway_hmac_secret_env), "AUTH_GATEWAY_HMAC")
    prod_auth_ok = provider == "gateway_hmac" and bool(auth_secret)
    items.append(ReadinessItem(
        key="PRODUCTION_AUTH_PROVIDER",
        status="PASS" if prod_auth_ok else "BLOCKED",
        blocking=True,
        category="SECURITY",
        detail="Signed gateway_hmac production authentication is configured." if prod_auth_ok else "Set PRODUCTION_AUTH_PROVIDER=gateway_hmac and configure its secret before production release.",
    ))

    anonymous_ok = not settings.auth_allow_anonymous_dev
    items.append(ReadinessItem(
        key="ANONYMOUS_DEV_AUTH_DISABLED",
        status="PASS" if anonymous_ok else "BLOCKED",
        blocking=True,
        category="SECURITY",
        detail="Anonymous development fallback is disabled." if anonymous_ok else "AUTH_ALLOW_ANONYMOUS_DEV must be false for production release.",
    ))

    origins = [x.strip() for x in str(getattr(settings, "cors_allow_origins", "*")).split(",") if x.strip()]
    cors_ok = bool(origins) and "*" not in origins
    items.append(ReadinessItem(
        key="PRODUCTION_CORS_RESTRICTED",
        status="PASS" if cors_ok else "BLOCKED",
        blocking=True,
        category="SECURITY",
        detail="Production CORS is restricted." if cors_ok else "Wildcard/empty CORS is forbidden in production.",
    ))

    feishu_transport = FeishuLiveTransport()
    feishu_enabled = bool(settings.feishu_live_enabled)
    feishu_configured = feishu_enabled and feishu_transport.configured()
    callback_security = bool(
        _secret(SecretRef(settings.feishu_verification_token, settings.feishu_verification_token_file, settings.feishu_verification_token_env), "FEISHU_VERIFICATION_TOKEN")
    )
    feishu_ready = feishu_configured and callback_security
    items.append(ReadinessItem(
        key="FEISHU_LIVE_TRANSPORT",
        status="PASS" if feishu_ready else "BLOCKED",
        blocking=True,
        category="INTEGRATION",
        detail="Live Feishu send/update and callback security are configured." if feishu_ready else "Feishu implementation is present; live app credentials, target and callback security still require production configuration.",
    ))

    blockers = [x for x in items if x.blocking and x.status != "PASS"]
    return {
        "schema_version": 2,
        "release_id": "VOIP_AI_V1.0",
        "app_version": settings.app_version,
        "status": "READY" if not blockers else "BLOCKED",
        "items": [x.as_dict() for x in items],
        "blocking_count": len(blockers),
    }
