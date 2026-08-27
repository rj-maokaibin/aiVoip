from __future__ import annotations

from pathlib import Path

from tools.github_actions_context_gate import invalid_job_env_runner_context, scan


def test_rejects_runner_context_in_job_level_env(tmp_path: Path):
    p = tmp_path / "bad.yml"
    p.write_text(
        """name: bad
jobs:
  test:
    runs-on: self-hosted
    env:
      VENV: ${{ runner.temp }}/venv
    steps:
      - run: echo ok
""",
        encoding="utf-8",
    )
    findings = invalid_job_env_runner_context(p)
    assert len(findings) == 1
    assert findings[0]["code"] == "RUNNER_CONTEXT_NOT_AVAILABLE_IN_JOB_ENV"


def test_allows_runner_context_inside_step_env(tmp_path: Path):
    p = tmp_path / "good.yml"
    p.write_text(
        """name: good
jobs:
  test:
    runs-on: self-hosted
    env:
      SAFE: ${{ github.run_id }}
    steps:
      - name: step
        env:
          VENV: ${{ runner.temp }}/venv
        run: echo ok
""",
        encoding="utf-8",
    )
    assert invalid_job_env_runner_context(p) == []


def test_repository_workflows_pass_job_env_context_gate():
    root = Path(__file__).resolve().parents[2]
    result = scan(root)
    assert result["status"] == "PASS", result
    assert result["finding_count"] == 0, result
