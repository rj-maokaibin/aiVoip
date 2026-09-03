import hashlib
import importlib.util
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "deploy" / "production_wrapper_migration.py"
WRAPPER = ROOT / "deploy" / "production_deploy_wrapper.sh"


def load_migration():
    spec = importlib.util.spec_from_file_location("production_wrapper_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_source_controlled_wrapper_never_writes_persistent_build_revision_and_does_not_double_verify():
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'python3 - "$ENV_FILE" "$TARGET"' not in text
    assert 'BUILD_REVISION={target}' not in text
    assert '--revision "$TARGET" deploy' in text
    assert '--revision "$TARGET" verify' not in text
    assert "verify_source=DEPLOY_RUNTIME_VERIFY" in text
    assert "PRODUCTION_WRAPPER_VERSION=source-controlled-v2" in text


def test_migration_removes_only_build_revision_and_preserves_private_mode(tmp_path):
    migration = load_migration()
    env = tmp_path / "production.env"
    env.write_bytes(
        b"APP_ENV=production\n"
        b"BUILD_REVISION=1111111111111111111111111111111111111111\n"
        b"SECRET_VALUE=keep-me-byte-for-byte\n"
    )
    env.chmod(0o600)
    removed = migration.normalize_persistent_env(env)
    assert removed == 1
    assert env.read_bytes() == b"APP_ENV=production\nSECRET_VALUE=keep-me-byte-for-byte\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_migration_refuses_unknown_privileged_wrapper(tmp_path):
    migration = load_migration()
    source = tmp_path / "source-wrapper"
    source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
    target = tmp_path / "target-wrapper"
    target.write_text("#!/bin/sh\necho unknown\n", encoding="utf-8")
    env = tmp_path / "production.env"
    env.write_text("APP_ENV=production\n", encoding="utf-8")
    env.chmod(0o600)
    try:
        migration.sync(source, target, env)
    except RuntimeError as exc:
        assert "unknown privileged production wrapper" in str(exc)
    else:
        raise AssertionError("unknown wrapper must fail closed")


def test_legacy_and_current_v2_wrapper_hashes_are_explicitly_pinned():
    migration = load_migration()
    assert migration.LEGACY_WRAPPER_SHA256 == {
        "77b2b30e448b1600a56e476dae9c359617d87706e1bf48e549ac4d4d35635edb"
    }
    assert migration.SOURCE_CONTROLLED_V2_PREDECESSOR_SHA256 == {
        "25b0aac88c7c4f09edda07e2e802e295fbb1a9d1e84639a1b7e4467f604355c0"
    }
    assert migration.ALLOWED_PREDECESSOR_SHA256 == (
        migration.LEGACY_WRAPPER_SHA256 | migration.SOURCE_CONTROLLED_V2_PREDECESSOR_SHA256
    )


def test_current_v2_predecessor_can_migrate_to_new_source_wrapper(tmp_path):
    migration = load_migration()
    source = tmp_path / "source-wrapper"
    source.write_text("#!/bin/sh\necho v2.2\n", encoding="utf-8")
    target = tmp_path / "target-wrapper"
    env = tmp_path / "production.env"
    env.write_text("APP_ENV=production\n", encoding="utf-8")
    env.chmod(0o600)

    predecessor_hash = next(iter(migration.SOURCE_CONTROLLED_V2_PREDECESSOR_SHA256))
    # sync() is tested against exact digests. Patch digest only for the target
    # predecessor path while leaving the real source/post-install digest intact.
    real_digest = migration.digest

    def controlled_digest(path: Path) -> str:
        if path == target and path.exists() and path.read_text(encoding="utf-8") == "installed-v2\n":
            return predecessor_hash
        return real_digest(path)

    target.write_text("installed-v2\n", encoding="utf-8")
    migration.digest = controlled_digest
    try:
        mode, removed, source_hash = migration.sync(source, target, env)
    finally:
        migration.digest = real_digest

    assert mode == "MIGRATED"
    assert removed == 0
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert target.read_bytes() == source.read_bytes()
