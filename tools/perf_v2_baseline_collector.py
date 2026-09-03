#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

BASELINE_SHA = "7964d39b6503f2a54bc0e858d7ae713ef06cb562"
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


def get(path: str):
    req = urllib.request.Request(
        f"{API}/repos/{REPO}{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aivoip-cicd-performance-v2",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ms(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return int((b - a).total_seconds() * 1000)


runs = get("/actions/workflows/production-deploy.yml/runs?branch=master&per_page=50")
candidates = [r for r in runs.get("workflow_runs", []) if r.get("head_sha") == BASELINE_SHA]
if not candidates:
    raise SystemExit(f"no Production Deploy run found for baseline {BASELINE_SHA}")
run = sorted(candidates, key=lambda r: r.get("run_attempt", 1), reverse=True)[0]
jobs = get(f"/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
rows = []
for job in jobs:
    steps = []
    for step in job.get("steps", []):
        steps.append({
            "name": step.get("name"),
            "status": step.get("status"),
            "conclusion": step.get("conclusion"),
            "started_at": step.get("started_at"),
            "completed_at": step.get("completed_at"),
            "duration_ms": ms(step.get("started_at"), step.get("completed_at")),
        })
    rows.append({
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "duration_ms": ms(job.get("started_at"), job.get("completed_at")),
        "steps": steps,
    })
payload = {
    "schema_version": "cicd-performance-baseline-v1",
    "baseline_sha": BASELINE_SHA,
    "workflow": "Production Deploy",
    "run": {
        "id": run.get("id"),
        "run_number": run.get("run_number"),
        "run_attempt": run.get("run_attempt"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "run_started_at": run.get("run_started_at"),
        "duration_ms": ms(run.get("run_started_at"), run.get("updated_at")),
        "html_url": run.get("html_url"),
    },
    "jobs": rows,
}
out = Path("validation/cicd_performance_baseline.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
