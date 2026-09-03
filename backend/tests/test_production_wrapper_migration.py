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


def test_source_controlled_wrapper_never_writes_persistent_build_revision():
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'python3 - "$ENV_FILE" "$TARGET"' not in text
    assert 'BUILD_REVISION={target}' not in text
    assert '--revision "$TARGET" deploy' in text
    assert '--revision "$TARGET" verify' in text
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


def test_legacy_wrapper_hash_is_explicitly_pinned():
    migration = load_migration()
    assert migration.LEGACY_WRAPPER_SHA256 == {
        "77b2b30e448b1600a56e476dae9c359617d87706e1bf48e549ac4d4d35635edb"
    }
