#!/usr/bin/env python3
"""Minimal conformance gates for weekly fork upgrade candidates."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fork_policy_guard import check_repo  # noqa: E402

FORBIDDEN_PROMPT_PATTERNS = tuple(
    f"{verb} {obj} {owner}"
    for verb, obj in (
        ("submit", "a PR to"),
        ("open", "a PR to"),
        ("push", "to"),
        ("comment", "on"),
    )
    for owner in ("Nous" + "Research",)
)


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    failures: list[str] = []
    guard = check_repo(repo, fork_owner="msreadbot", fork_repo="hermes-agent", vendor_owner="NousResearch", vendor_repo="hermes-agent")
    if not guard.ok:
        failures.extend(guard.reasons)
    for cmd in (["hermes", "--version"], ["hermes", "config", "check"]):
        code, out = run(cmd, repo)
        print(f"$ {' '.join(cmd)}\n{out}\n")
        if code != 0:
            failures.append(f"command failed: {' '.join(cmd)}")
    # Scan only local automation prompt/template files to prevent future generated upstream-write asks.
    for path in [repo / "scripts", repo / ".github"]:
        if not path.exists():
            continue
        for file in path.rglob("*"):
            if not file.is_file() or file.suffix.lower() not in {".py", ".md", ".yml", ".yaml", ".sh"}:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            for pattern in FORBIDDEN_PROMPT_PATTERNS:
                if pattern.lower() in text.lower():
                    failures.append(f"forbidden upstream-write prompt text in {file}: {pattern}")
    if failures:
        print("CONFORMANCE: BLOCKED")
        for failure in failures:
            print(f"- {failure}")
        return 2
    print("CONFORMANCE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
