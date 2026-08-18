#!/usr/bin/env python3
"""Weekly fork-only Hermes upgrade intake/prep.

Default mode is low-noise: write a durable report every run, print nothing when
there is no decision-worthy change, and never deploy/restart the live gateway.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fork_policy_guard import check_repo, matches_repo  # noqa: E402

VENDOR_REMOTE = "origin"
FORK_REMOTE = "fork"
APPROVAL_CHANNEL = "slack:C0BHCMN36TW"
HIGH_RISK_PREFIXES = (
    "gateway/",
    "tui_gateway/",
    "cron/",
    "tools/",
    "hermes_cli/",
    "agent/",
    "mcp_",
    "plugins/",
    "profiles/",
    "config",
    "SOUL.md",
    "AGENTS.md",
)


def run(cmd: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], cwd=repo, check=check).stdout.strip()


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def artifact_dir() -> Path:
    d = hermes_home() / "phase-artifacts" / "hermes-upgrades"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path() -> Path:
    d = hermes_home() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / "weekly-hermes-fork-upgrade.lock"


def current_ts() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def classify_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if p.startswith(HIGH_RISK_PREFIXES) or any(token in p.lower() for token in ("gateway", "cron", "mcp", "config", "soul", "kanban"))]


def acquire_lock(path: Path) -> int:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    return fd


def release_lock(fd: int, path: Path) -> None:
    try:
        os.close(fd)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def ensure_remote(repo: Path, name: str, owner: str, repo_name: str, *, push_required: bool) -> None:
    out = git(repo, "remote", "-v")
    urls = [line.split()[1] for line in out.splitlines() if line.startswith(name + "\t")]
    if not urls:
        raise RuntimeError(f"missing required remote '{name}'")
    if not any(matches_repo(url, owner, repo_name) for url in urls):
        raise RuntimeError(f"remote '{name}' does not point at {owner}/{repo_name}")
    if push_required and not any("(push)" in line and matches_repo(line.split()[1], owner, repo_name) for line in out.splitlines() if line.startswith(name + "\t")):
        raise RuntimeError(f"remote '{name}' is not push-capable for {owner}/{repo_name}")


def write_report(path: Path, title: str, lines: list[str]) -> None:
    path.write_text("\n".join([f"# {title}", "", *lines, ""]) , encoding="utf-8")


def prepare_branch(repo: Path, branch: str, vendor_ref: str, report_lines: list[str]) -> tuple[str | None, str | None]:
    root = hermes_home() / "worktrees" / "hermes-upgrades"
    root.mkdir(parents=True, exist_ok=True)
    worktree = root / branch.replace("/", "-")
    if worktree.exists():
        report_lines.append(f"- Reusing existing worktree: `{worktree}`")
        git(worktree, "checkout", branch)
        git(worktree, "reset", "--hard", f"{FORK_REMOTE}/main")
    else:
        git(repo, "worktree", "add", "-B", branch, str(worktree), f"{FORK_REMOTE}/main")
        report_lines.append(f"- Created isolated worktree: `{worktree}`")

    # Preserve the operator's local patch stack even when fork/main has moved
    # ahead of the live checkout. These commits are local/fork-owned policy and
    # must not be silently dropped during vendor intake.
    local_commits = [c for c in git(repo, "rev-list", "--reverse", f"{FORK_REMOTE}/main..HEAD").splitlines() if c]
    if local_commits:
        report_lines.append(f"- Local patch stack commits to replay: `{len(local_commits)}`")
    for commit in local_commits:
        try:
            git(worktree, "cherry-pick", "--keep-redundant-commits", commit)
        except subprocess.CalledProcessError as exc:
            git(worktree, "cherry-pick", "--abort", check=False)
            report_lines.append(f"- Local patch replay: BLOCKED at `{commit}`")
            report_lines.append("```text")
            report_lines.append(exc.output.strip())
            report_lines.append("```")
            return branch, None

    try:
        git(worktree, "merge", "--no-edit", vendor_ref)
    except subprocess.CalledProcessError as exc:
        git(worktree, "merge", "--abort", check=False)
        report_lines.append("- Merge result: BLOCKED by conflicts")
        report_lines.append("```text")
        report_lines.append(exc.output.strip())
        report_lines.append("```")
        return branch, None
    head = git(worktree, "rev-parse", "HEAD")
    report_lines.append(f"- Candidate branch: `{branch}`")
    report_lines.append(f"- Candidate head: `{head}`")
    return branch, head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path.home() / ".hermes" / "hermes-agent"))
    parser.add_argument("--prepare", action="store_true", help="Prepare an isolated fork upgrade branch when updates exist")
    parser.add_argument("--open-pr", action="store_true", help="Open an internal fork PR after branch prep and guard checks")
    parser.add_argument("--print-no-change", action="store_true", help="Print even when no change; useful for tests/dry-runs")
    parser.add_argument("--approval-channel", default=APPROVAL_CHANNEL)
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    ts = current_ts()
    report_path = artifact_dir() / f"{ts}-weekly-fork-upgrade.md"
    status = "FAILED"
    summary_lines: list[str] = []
    report_lines = [
        f"- Approval channel: `{args.approval_channel}`",
        f"- Repo: `{repo}`",
        "- Vendor upstream: read-only",
        "- Deploy/restart: not attempted",
    ]

    lock = lock_path()
    try:
        fd = acquire_lock(lock)
    except FileExistsError:
        write_report(report_path, "Hermes fork weekly upgrade - BLOCKED", [*report_lines, f"- Blocker: lock exists at `{lock}`"])
        print("Hermes fork upgrade BLOCKED — another run is active. Evidence saved internally.")
        return 2

    try:
        guard = check_repo(repo, fork_owner="msreadbot", fork_repo="hermes-agent", vendor_owner="NousResearch", vendor_repo="hermes-agent")
        if not guard.ok:
            status = "BLOCKED"
            report_lines.append("- Fork policy: BLOCKED")
            report_lines.extend(f"  - {r}" for r in guard.reasons)
            write_report(report_path, "Hermes fork weekly upgrade - BLOCKED", report_lines)
            print("Hermes fork upgrade BLOCKED — fork policy failed. Why it matters: upgrades must stay fork-only; NousResearch remains read-only. Evidence saved internally.")
            return 2
        ensure_remote(repo, VENDOR_REMOTE, "NousResearch", "hermes-agent", push_required=False)
        ensure_remote(repo, FORK_REMOTE, "msreadbot", "hermes-agent", push_required=True)

        status_short = git(repo, "status", "--short")
        if status_short:
            status = "BLOCKED"
            report_lines.append("- Dirty tree: BLOCKED")
            report_lines.append("```text")
            report_lines.append(status_short)
            report_lines.append("```")
            write_report(report_path, "Hermes fork weekly upgrade - BLOCKED", report_lines)
            print("Hermes fork upgrade BLOCKED — live checkout has uncommitted changes. Decision: clean/stash/commit local work before preparing upgrade. Why it matters: upgrade prep must not mix with unrelated edits. Evidence saved internally.")
            return 2

        hermes_version = run(["hermes", "--version"], cwd=repo, check=False).stdout.strip()
        update_check = run(["hermes", "update", "--check"], cwd=repo, check=False).stdout.strip()
        report_lines.append("## Read-only Hermes checks")
        report_lines.append("```text")
        report_lines.append(hermes_version)
        report_lines.append(update_check)
        report_lines.append("```")

        git(repo, "fetch", VENDOR_REMOTE, "main", "--tags", "--prune")
        git(repo, "fetch", FORK_REMOTE, "main", "--prune")
        vendor_ref = f"{VENDOR_REMOTE}/main"
        fork_ref = f"{FORK_REMOTE}/main"
        local_sha = git(repo, "rev-parse", "HEAD")
        fork_sha = git(repo, "rev-parse", fork_ref)
        vendor_sha = git(repo, "rev-parse", vendor_ref)
        ahead_behind = git(repo, "rev-list", "--left-right", "--count", f"{fork_ref}...{vendor_ref}")
        changed = git(repo, "diff", "--name-only", f"{fork_ref}...{vendor_ref}")
        changed_paths = [p for p in changed.splitlines() if p]
        high_risk = classify_paths(changed_paths)
        report_lines.extend([
            "## Git state",
            f"- Local SHA: `{local_sha}`",
            f"- Fork main SHA: `{fork_sha}`",
            f"- Vendor main SHA: `{vendor_sha}`",
            f"- Fork/vendor ahead-behind: `{ahead_behind}`",
            f"- Changed path count vendor vs fork: `{len(changed_paths)}`",
            f"- High-risk changed path count: `{len(high_risk)}`",
        ])
        if high_risk[:80]:
            report_lines.append("## High-risk changed paths sample")
            report_lines.extend(f"- `{p}`" for p in high_risk[:80])

        if fork_sha == vendor_sha:
            status = "NO_CHANGE"
            write_report(report_path, "Hermes fork weekly upgrade - NO_CHANGE", report_lines)
            if args.print_no_change:
                print("Hermes fork upgrade — no change. No Monty decision needed. Evidence saved internally.")
            return 0

        if not args.prepare:
            status = "BLOCKED"
            report_lines.append("- Recommendation: upstream changed; rerun with `--prepare` to create an isolated fork branch.")
            write_report(report_path, "Hermes fork weekly upgrade - BLOCKED", report_lines)
            print("Hermes fork upgrade — decision needed. Decision: approve preparing an isolated fork upgrade branch. Why it matters: upstream changed; deploy remains manual. Evidence saved internally.")
            return 1

        branch = "upgrade/vendor-" + dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        candidate_branch, candidate_head = prepare_branch(repo, branch, vendor_ref, report_lines)
        if not candidate_head:
            status = "BLOCKED"
            write_report(report_path, "Hermes fork weekly upgrade - MERGE_BLOCKED", report_lines)
            print("Hermes fork upgrade BLOCKED — merge conflicts. Decision: approve manual conflict resolution or skip this week. Why it matters: automated upgrade cannot safely reconcile this. Evidence saved internally.")
            return 2

        candidate_worktree = hermes_home() / "worktrees" / "hermes-upgrades" / branch.replace("/", "-")
        gates_cmd = [sys.executable, "scripts/run_fork_upgrade_gates.py", "--repo", str(candidate_worktree)]
        gates = run(gates_cmd, cwd=candidate_worktree, check=False)
        report_lines.append("## Candidate conformance gates")
        report_lines.append("```text")
        report_lines.append(gates.stdout.strip())
        report_lines.append("```")
        if gates.returncode != 0:
            status = "BLOCKED"
            write_report(report_path, "Hermes fork weekly upgrade - GATE_BLOCKED", report_lines)
            print("Hermes fork upgrade BLOCKED — conformance gates failed. Decision: fix the candidate branch before review. Why it matters: upgrade is not safe to review/deploy yet. Evidence saved internally.")
            return 2

        pr_url = None
        if args.open_pr:
            pr_guard = check_repo(
                candidate_worktree,
                fork_owner="msreadbot",
                fork_repo="hermes-agent",
                vendor_owner="NousResearch",
                vendor_repo="hermes-agent",
                base_target="msreadbot/hermes-agent",
                head_target="msreadbot/hermes-agent",
            )
            if not pr_guard.ok:
                status = "BLOCKED"
                report_lines.append("- Internal PR: BLOCKED by fork target guard")
                report_lines.extend(f"  - {r}" for r in pr_guard.reasons)
                write_report(report_path, "Hermes fork weekly upgrade - PR_GUARD_BLOCKED", report_lines)
                print("Hermes fork upgrade BLOCKED — internal PR target guard failed. Why it matters: fork-only policy prevented unsafe targeting. Evidence saved internally.")
                return 2
            if shutil.which("gh") is None:
                status = "BLOCKED"
                report_lines.append("- Internal PR: BLOCKED, `gh` not installed")
                write_report(report_path, "Hermes fork weekly upgrade - GH_MISSING", report_lines)
                print("Hermes fork upgrade BLOCKED — GitHub CLI unavailable. Decision: restore gh access or run prep without PR creation. Evidence saved internally.")
                return 2
            else:
                push = run(["git", "push", FORK_REMOTE, f"HEAD:{branch}"], cwd=candidate_worktree, check=False)
                report_lines.append("## Fork branch push")
                report_lines.append("```text")
                report_lines.append(push.stdout.strip())
                report_lines.append("```")
                if push.returncode != 0:
                    status = "BLOCKED"
                    write_report(report_path, "Hermes fork weekly upgrade - PUSH_BLOCKED", report_lines)
                    print("Hermes fork upgrade BLOCKED — fork branch push failed. Decision: fix GitHub push/auth before review. Evidence saved internally.")
                    return 2
                pr_body = artifact_dir() / f"{ts}-internal-pr-body.md"
                pr_body.write_text("\n".join(report_lines), encoding="utf-8")
                gh_repo = "msreadbot/hermes-agent"
                view = run(["gh", "pr", "view", "--repo", gh_repo, "--json", "url", "--jq", ".url", "--head", f"msreadbot:{branch}"], cwd=candidate_worktree, check=False)
                if view.returncode == 0 and view.stdout.strip().startswith("http"):
                    pr_url = view.stdout.strip()
                    edit = run(["gh", "pr", "edit", "--repo", gh_repo, branch, "--body-file", str(pr_body)], cwd=candidate_worktree, check=False)
                    report_lines.append(f"- Existing internal PR updated: {pr_url}")
                    if edit.returncode != 0:
                        report_lines.append("- PR body update warning:")
                        report_lines.append("```text")
                        report_lines.append(edit.stdout.strip())
                        report_lines.append("```")
                else:
                    cmd = [
                        "gh", "pr", "create",
                        "--repo", gh_repo,
                        "--base", "main",
                        "--head", f"msreadbot:{branch}",
                        "--title", f"chore: weekly Hermes vendor upgrade {dt.date.today().isoformat()}",
                        "--body-file", str(pr_body),
                    ]
                    pr = run(cmd, cwd=candidate_worktree, check=False)
                    if pr.returncode == 0:
                        pr_url = pr.stdout.strip().splitlines()[-1]
                        report_lines.append(f"- Internal PR: {pr_url}")
                    else:
                        status = "BLOCKED"
                        report_lines.append("- Internal PR: BLOCKED")
                        report_lines.append("```text")
                        report_lines.append(pr.stdout.strip())
                        report_lines.append("```")
                        write_report(report_path, "Hermes fork weekly upgrade - PR_BLOCKED", report_lines)
                        print("Hermes fork upgrade BLOCKED — internal PR create failed. Decision: fix GitHub PR/auth issue or create PR manually. Evidence saved internally.")
                        return 2

        status = "PREPARED_INTERNAL_BRANCH"
        write_report(report_path, f"Hermes fork weekly upgrade - {status}", report_lines)
        summary_lines.append(f"Hermes fork upgrade {status}")
        summary_lines.append("Decision: review the internal fork PR/branch; deploy remains manual.")
        summary_lines.append("Why it matters: upstream changed; this keeps NousResearch read-only and our fork isolated.")
        if pr_url:
            summary_lines.append(f"PR: {pr_url}")
        summary_lines.append("Confirmed: branch prepared and conformance gates passed.")
        summary_lines.append("Evidence saved internally.")
        summary_lines.append("Next: review/merge the PR if it looks right; no deploy/restart is included.")
        print("\n".join(summary_lines))
        return 0
    except Exception as exc:
        status = "FAILED"
        report_lines.append(f"- Error: `{type(exc).__name__}: {exc}`")
        write_report(report_path, "Hermes fork weekly upgrade - FAILED", report_lines)
        print(f"Hermes fork upgrade FAILED — {type(exc).__name__}: {exc}. Evidence saved internally.")
        return 2
    finally:
        try:
            release_lock(fd, lock)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
