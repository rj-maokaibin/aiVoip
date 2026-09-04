from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "deploy" / "production_wrapper_migration.py"


def test_current_production_wrapper_is_an_exact_reviewed_predecessor() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert "ed785b9a6c9b019afcd9d02fb2bd9240cb39130b6d49776e76b37f59dcbdd41f" in text
    assert "current_hash not in ALLOWED_PREDECESSOR_SHA256" in text
    assert "refusing to replace unknown privileged production wrapper" in text
