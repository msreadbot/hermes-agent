#!/usr/bin/env python3
"""Fork-only safety guard for Hermes upgrade automation.

This guard is intentionally independent of Hermes internals so it can run before
any update/install logic. It treats the vendor repository as read-only and fails
closed when a push/PR target is unsafe or ambiguous.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_VENDOR_OWNER = "NousResearch"
DEFAULT_VENDOR_REPO = "hermes-agent"
DEFAULT_FORK_OWNER = "msreadbot"
DEFAULT_FORK_REPO = "hermes-agent"
DISABLED_PUSH_MARKERS = ("DISABLED", "NO_PUSH", "no_push", "no-push", "read-only", "readonly")


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reasons: list[str]
    remotes: dict[str, dict[str, list[str]]]


def _run_git(repo: Path, args: list[str]) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.STDOUT)


def parse_remote_v(text: str) -> dict[str, dict[str, list[str]]]:
    remotes: dict[str, dict[str, list[str]]] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        name, url, kind = parts[0], parts[1], parts[2].strip("()")
        if kind not in {"fetch", "push"}:
            continue
        remotes.setdefault(name, {"fetch": [], "push": []})[kind].append(url)
    return remotes


def slug_from_url(url: str) -> tuple[str, str] | None:
    stripped = url.strip()
    if not stripped:
        return None
    if any(marker.lower() in stripped.lower() for marker in DISABLED_PUSH_MARKERS):
        return None
    if stripped.startswith("git@"):
        # git@github.com:Owner/repo.git
        path = stripped.split(":", 1)[1] if ":" in stripped else stripped
    else:
        parsed = urlparse(stripped)
        path = parsed.path if parsed.scheme else stripped
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    bits = [b for b in path.split("/") if b]
    if len(bits) < 2:
        return None
    return bits[-2], bits[-1]


def matches_repo(url: str, owner: str, repo: str) -> bool:
    slug = slug_from_url(url)
    if not slug:
        return False
    return slug[0].lower() == owner.lower() and slug[1].lower() == repo.lower()


def _owner_repo(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    # Accept OWNER/REPO, https URLs, git URLs, or PR URLs.
    slug = slug_from_url(cleaned)
    if slug:
        return slug
    match = re.match(r"^([^/\s]+)/([^/\s]+)$", cleaned)
    if match:
        repo = match.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return match.group(1), repo
    return None


def check_targets(
    remotes: dict[str, dict[str, list[str]]],
    *,
    fork_owner: str,
    fork_repo: str,
    vendor_owner: str,
    vendor_repo: str,
    base_target: str | None = None,
    head_target: str | None = None,
    require_fork_push: bool = True,
) -> GuardResult:
    reasons: list[str] = []
    fork_push_urls: list[str] = []
    for name, kinds in remotes.items():
        for url in kinds.get("push", []):
            if matches_repo(url, vendor_owner, vendor_repo):
                reasons.append(f"remote '{name}' has vendor push URL: {url}")
            if matches_repo(url, fork_owner, fork_repo):
                fork_push_urls.append(url)
    if require_fork_push and not fork_push_urls:
        reasons.append(f"no push remote points at required fork {fork_owner}/{fork_repo}")

    for label, target in (("base", base_target), ("head", head_target)):
        parsed = _owner_repo(target)
        if target and not parsed:
            reasons.append(f"{label} target is not parseable as owner/repo: {target}")
        elif parsed and not (parsed[0].lower() == fork_owner.lower() and parsed[1].lower() == fork_repo.lower()):
            reasons.append(f"{label} target is not fork-only: {parsed[0]}/{parsed[1]}")

    return GuardResult(ok=not reasons, reasons=reasons, remotes=remotes)


def check_repo(repo: Path, **kwargs) -> GuardResult:
    text = _run_git(repo, ["remote", "-v"])
    return check_targets(parse_remote_v(text), **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Git repository to inspect")
    parser.add_argument("--fork-owner", default=DEFAULT_FORK_OWNER)
    parser.add_argument("--fork-repo", default=DEFAULT_FORK_REPO)
    parser.add_argument("--vendor-owner", default=DEFAULT_VENDOR_OWNER)
    parser.add_argument("--vendor-repo", default=DEFAULT_VENDOR_REPO)
    parser.add_argument("--base-target", help="Optional PR/base target owner/repo or URL")
    parser.add_argument("--head-target", help="Optional PR/head target owner/repo or URL")
    parser.add_argument("--allow-no-fork-push", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = check_repo(
            Path(args.repo).expanduser().resolve(),
            fork_owner=args.fork_owner,
            fork_repo=args.fork_repo,
            vendor_owner=args.vendor_owner,
            vendor_repo=args.vendor_repo,
            base_target=args.base_target,
            head_target=args.head_target,
            require_fork_push=not args.allow_no_fork_push,
        )
    except subprocess.CalledProcessError as exc:
        payload = {"ok": False, "reasons": [exc.output.strip() or str(exc)], "remotes": {}}
        print(json.dumps(payload, indent=2) if args.json else "BLOCKED: " + payload["reasons"][0])
        return 2

    payload = {"ok": result.ok, "reasons": result.reasons, "remotes": result.remotes}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif result.ok:
        print("OK: fork-only Git remote/target policy satisfied")
    else:
        print("BLOCKED: fork-only policy failed")
        for reason in result.reasons:
            print(f"- {reason}")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
