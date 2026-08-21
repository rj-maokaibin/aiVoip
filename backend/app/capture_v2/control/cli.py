from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import RemoteValidationRunner
from .schema import RemoteAction


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="capture-v2-control", description="Capture V2 Git-mediated remote validation control loop")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Poll/execute allowlisted validation actions")
    run.add_argument("--repo-root", default=".")
    run.add_argument("--branch", default="feat/capture-v2.1.1-real-gates")
    run.add_argument("--remote", default="origin")
    run.add_argument("--action-path", default="validation/control/next_action.json")
    run.add_argument("--poll-seconds", type=float, default=10.0)
    run.add_argument("--git-sync", action="store_true", help="ff-only pull actions and commit/push structured results")
    run.add_argument("--once", action="store_true")
    run.add_argument("--runner-id", default="")

    val = sub.add_parser("validate-action", help="Validate an action file without executing it")
    val.add_argument("--file", required=True)

    ack = sub.add_parser("ack", help="Acknowledge a requested physical/human step")
    ack.add_argument("--repo-root", default=".")
    ack.add_argument("--action-id", required=True)
    ack.add_argument("--token", required=True)

    status = sub.add_parser("status", help="Show current runner status")
    status.add_argument("--repo-root", default=".")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-action":
        action = RemoteAction.load(Path(args.file))
        print(json.dumps({"ok": True, "action_sha256": action.digest(), "action": action.canonical_dict()}, indent=2, ensure_ascii=False))
        return 0
    if args.command == "ack":
        root = Path(args.repo_root).resolve()
        path = root / "validation/control/human_ack.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"action_id": args.action_id, "token": args.token}, indent=2) + "\n", encoding="utf-8")
        print(str(path))
        return 0
    if args.command == "status":
        path = Path(args.repo_root).resolve() / "validation/control/status.json"
        if not path.exists():
            print(json.dumps({"state": "NO_STATUS"}, indent=2))
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0
    if args.command == "run":
        root = Path(args.repo_root).resolve()
        runner = RemoteValidationRunner(repo_root=root, action_path=root / args.action_path,
            branch=args.branch, remote=args.remote, git_sync=args.git_sync, runner_id=args.runner_id or None)
        if args.once:
            status = runner.process_once()
            print(json.dumps(status.as_dict() if status else {"state": "NO_ACTION"}, indent=2, ensure_ascii=False))
            return 0
        runner.run_forever(poll_seconds=args.poll_seconds)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
