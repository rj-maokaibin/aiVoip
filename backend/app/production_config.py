from __future__ import annotations

from dataclasses import dataclass, asdict
from urllib.parse import urlparse
from typing import Any

from app.core.config import settings
from app.integrations.secrets import SecretRef, SecretResolver, SecretResolutionError
from app.integrations.feishu.transport import FeishuLiveTransport


@dataclass(frozen=True)
class ProductionConfigItem:
    key: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolved_secret(ref: SecretRef, *, name: str) -> str:
    try:
        return SecretResolver.resolve(ref, name=name, required=False)
    except SecretResolutionError:
        return ""


def _secret_configured(ref: SecretRef, *, name: str) -> bool:
    return bool(_resolved_secret(ref, name=name))


def production_config_readiness() -> dict[str, Any]:
    items: list[ProductionConfigItem] = []
    env_prod = settings.app_env.lower() == "production"
    items.append(ProductionConfigItem("APP_ENV_PRODUCTION", "PASS" if env_prod else "BLOCKED", f"APP_ENV={settings.app_env}"))

    revision = str(settings.build_revision or "").strip()
    revision_ok = bool(revision and revision.lower() not in {"dev", "unknown", "local"})
    items.append(ProductionConfigItem("BUILD_REVISION_PINNED", "PASS" if revision_ok else "BLOCKED", "immutable build revision configured" if revision_ok else "BUILD_REVISION is not pinned"))

    provider = str(settings.production_auth_provider or "").lower()
    auth_secret = _secret_configured(SecretRef(settings.auth_gateway_hmac_secret, settings.auth_gateway_hmac_secret_file, settings.auth_gateway_hmac_secret_env), name="AUTH_GATEWAY_HMAC")
    auth_ok = provider == "gateway_hmac" and auth_secret and not settings.auth_allow_anonymous_dev
    items.append(ProductionConfigItem("PRODUCTION_AUTH", "PASS" if auth_ok else "BLOCKED", "gateway_hmac configured and dev fallback disabled" if auth_ok else "configure gateway_hmac secret and disable anonymous development auth"))

    origins = [x.strip() for x in str(settings.cors_allow_origins).split(",") if x.strip()]
    cors_ok = bool(origins) and "*" not in origins and all(urlparse(x).scheme in {"http", "https"} and urlparse(x).netloc for x in origins)
    items.append(ProductionConfigItem("PRODUCTION_CORS", "PASS" if cors_ok else "BLOCKED", "explicit CORS origins configured" if cors_ok else "CORS origins must be explicit http(s) origins"))

    credential_ok = str(settings.credential_provider).lower() == "api" and bool(settings.credential_api_url)
    items.append(ProductionConfigItem("PRODUCTION_CREDENTIAL_PROVIDER", "PASS" if credential_ok else "BLOCKED", "API credential provider configured" if credential_ok else "CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required"))

    storage_ok = str(settings.reproduction_storage_mode).lower() == "minio"
    minio_access = _resolved_secret(SecretRef(settings.minio_access_key, settings.minio_access_key_file, settings.minio_access_key_env), name="MINIO_ACCESS_KEY")
    minio_secret = _resolved_secret(SecretRef(settings.minio_secret_key, settings.minio_secret_key_file, settings.minio_secret_key_env), name="MINIO_SECRET_KEY")
    defaults = {"voipminio", "voipminiosecret", "change-me", "minioadmin"}
    non_default = SecretResolver.is_non_default(minio_access, defaults) and SecretResolver.is_non_default(minio_secret, defaults)
    storage_config_ok = storage_ok and bool(minio_access) and bool(minio_secret) and non_default and bool(settings.minio_bucket)
    items.append(ProductionConfigItem("PRODUCTION_STORAGE_CONFIG", "PASS" if storage_config_ok else "BLOCKED", "MinIO backend and non-default secret refs configured" if storage_config_ok else "production MinIO backend, bucket and non-default credentials are required"))

    feishu_ok = bool(settings.feishu_live_enabled and FeishuLiveTransport().configured())
    callback_security = _secret_configured(SecretRef(settings.feishu_verification_token, settings.feishu_verification_token_file, settings.feishu_verification_token_env), name="FEISHU_VERIFICATION_TOKEN")
    items.append(ProductionConfigItem("FEISHU_LIVE_CONFIG", "PASS" if feishu_ok and callback_security else "BLOCKED", "Feishu transport target, app credential and callback security configured" if feishu_ok and callback_security else "Feishu live config/callback security is incomplete"))

    blockers = [x for x in items if x.status != "PASS"]
    return {"schema_version": 1, "status": "PASS" if not blockers else "BLOCKED", "items": [x.as_dict() for x in items], "blocking_count": len(blockers)}
