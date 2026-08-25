from app.core.config import Settings


def test_feishu_timeout_default_is_safe_for_multi_mb_evidence_uploads():
    settings = Settings(_env_file=None)
    assert settings.feishu_timeout_seconds >= 120.0


def test_stale_eight_second_env_override_cannot_reintroduce_write_timeout():
    settings = Settings(_env_file=None, feishu_timeout_seconds=8.0)
    assert settings.feishu_timeout_seconds == 120.0


def test_feishu_timeout_remains_configurable_upward():
    settings = Settings(_env_file=None, feishu_timeout_seconds=180.0)
    assert settings.feishu_timeout_seconds == 180.0
