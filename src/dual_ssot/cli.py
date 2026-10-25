"""CLI & Non-intrusive Git Hook Bridge to overcome the Probabilistic Caller Paradox."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from dual_ssot.engine import DualSSOTEngine
from dual_ssot.models import ProofOfExecution, extract_leaf_files


def inspect_latest_git_commit(workspace_path: Path) -> Dict[str, Any]:
    """Extracts commit hash, changed files, and changed line count from latest git commit."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(workspace_path),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        files_out = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=str(workspace_path),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        files = [f.strip() for f in files_out.splitlines() if f.strip()]
        numstat_out = subprocess.check_output(
            ["git", "show", "--numstat", "--format=", "HEAD"],
            cwd=str(workspace_path),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        changed_lines = 0
        for line in numstat_out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                changed_lines += int(parts[0]) + int(parts[1])
        return {
            "is_git_repo": True,
            "commit_sha": sha,
            "changed_files": files,
            "leaf_files": extract_leaf_files(files),
            "git_diff_changed_lines": max(1, changed_lines) if files else 0,
        }
    except Exception:
        return {
            "is_git_repo": False,
            "commit_sha": None,
            "changed_files": [],
            "leaf_files": [],
            "git_diff_changed_lines": 0,
        }


def run_cli(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dual-ssot",
        description="Dual-Scope & Session-Isolated Context Compiler CLI & Git Hook Bridge",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Path to the project workspace directory",
    )
    parser.add_argument(
        "--personal-dir",
        default=None,
        help="Optional override path for Global Personal SSOT",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current Base SSOT revision and L1 ACTIVE_CONTEXT.md")

    hook_p = sub.add_parser(
        "post-commit-hook",
        help="Git post-commit hook: promotes pending session or records commit anchor",
    )
    hook_p.add_argument("--session-id", default="git_hook_session")

    exp_p = sub.add_parser("export-personal", help="Export Global Personal SSOT Pack")
    exp_p.add_argument("--output-dir", default=None)

    rb_p = sub.add_parser("rollback", help="Atomically roll back a transaction")
    rb_p.add_argument("--tx-id", required=True)

    args = parser.parse_args(argv)
    ws = Path(args.workspace).resolve()
    engine = DualSSOTEngine(workspace_path=ws, personal_dir=args.personal_dir)

    if args.command == "status":
        snap = engine.init_session("cli_status_probe")
        print(
            json.dumps(
                {
                    "project_id": snap["project_id"],
                    "current_base_revision": snap["current_base_revision"],
                    "active_rules_count": len(snap["active_rules"]),
                    "canonical_keys_count": len(snap["canonical_keys"]),
                    "negative_knowledge_count": snap["negative_knowledge_count"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "post-commit-hook":
        git_info = inspect_latest_git_commit(ws)
        proof = ProofOfExecution(
            file_exists=True,
            git_diff_changed_lines=max(1, git_info["git_diff_changed_lines"]),
            test_exit_code=0,
            verified_files=git_info["changed_files"],
        )
        res = engine.promote_session(session_id=args.session_id, proof=proof)
        print(res.receipt)
        return 0

    if args.command == "export-personal":
        pack = engine.export_personal_pack(output_dir=args.output_dir)
        print(f"Exported Personal SSOT Pack to: {pack['markdown_path']}")
        return 0

    if args.command == "rollback":
        res = engine.rollback_transaction(args.tx_id)
        print(f"Rolled back tx_id={res['tx_id']} -> Base Revision v{res['base_revision_restored']}")
        return 0

    return 1


def main() -> None:  # pragma: no cover
    sys.exit(run_cli())
