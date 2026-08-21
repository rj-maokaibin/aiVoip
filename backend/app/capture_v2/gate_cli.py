"""Compatibility module: python -m app.capture_v2.gate_cli ..."""

from app.capture_v2.gate.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
