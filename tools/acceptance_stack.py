#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/acceptance_v2/docker-compose.yml"
PROJECT = "voip-acceptance-v2"


def run(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["up", "reset", "down", "status"])
    args = parser.parse_args()
    if args.command == "up":
        result = run("up", "-d", "--wait")
    elif args.command == "reset":
        down = run("down", "-v", "--remove-orphans")
        if down.returncode != 0:
            print(down.stdout)
            return down.returncode
        result = run("up", "-d", "--wait")
    elif args.command == "down":
        result = run("down", "-v", "--remove-orphans")
    else:
        result = run("ps")
    print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
