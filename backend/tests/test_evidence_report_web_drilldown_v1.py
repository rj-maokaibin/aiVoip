from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "frontend" / "src" / "evidence_report_page.tsx"


def _source() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_fr023_web_has_finding_metric_frame_and_event_drilldown():
    src = _source()
    assert "FindingCard" in src
    assert "关键指标 / 研发下钻" in src
    assert "metrics_json" in src
    assert "event_refs_json" in src
    assert "Frame / Event 引用" in src
    assert "extractFrameHints" in src


def test_fr023_web_renders_png_and_playable_wav_inline():
    src = _source()
    assert "content_type==='image/png'" in src
    assert "<img" in src
    assert "<audio controls" in src
    assert "preload=\"none\"" in src
    assert "content_url" in src


def test_fr023_web_exposes_html_manifest_and_audited_bundle_path():
    src = _source()
    assert "HTML 报告" in src
    assert "Manifest" in src
    assert "下载 INTERNAL_FULL" in src
    assert "下载 SHARE_SAFE" in src
    assert "download_url" in src

    api_src = (ROOT / "backend" / "app" / "api" / "v1" / "evidence_reports.py").read_text(encoding="utf-8")
    assert '/bundle/download' in api_src
    assert 'EVIDENCE_BUNDLE_DOWNLOADED' in api_src
    assert 'audited_download_url' in api_src
