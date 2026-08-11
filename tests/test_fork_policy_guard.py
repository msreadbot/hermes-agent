from __future__ import annotations

from scripts.fork_policy_guard import check_targets, matches_repo, parse_remote_v


def test_parse_and_accept_safe_fork_only_remote():
    remotes = parse_remote_v(
        "fork\thttps://github.com/msreadbot/hermes-agent.git (fetch)\n"
        "fork\thttps://github.com/msreadbot/hermes-agent.git (push)\n"
        "origin\thttps://github.com/NousResearch/hermes-agent.git (fetch)\n"
        "origin\tDISABLED_NO_PUSH_TO_NOUSRESEARCH (push)\n"
    )
    result = check_targets(
        remotes,
        fork_owner="msreadbot",
        fork_repo="hermes-agent",
        vendor_owner="NousResearch",
        vendor_repo="hermes-agent",
    )
    assert result.ok, result.reasons


def test_rejects_vendor_push_remote():
    remotes = parse_remote_v(
        "fork\thttps://github.com/msreadbot/hermes-agent.git (fetch)\n"
        "fork\thttps://github.com/msreadbot/hermes-agent.git (push)\n"
        "origin\thttps://github.com/NousResearch/hermes-agent.git (fetch)\n"
        "origin\thttps://github.com/NousResearch/hermes-agent.git (push)\n"
    )
    result = check_targets(
        remotes,
        fork_owner="msreadbot",
        fork_repo="hermes-agent",
        vendor_owner="NousResearch",
        vendor_repo="hermes-agent",
    )
    assert not result.ok
    assert any("vendor push URL" in reason for reason in result.reasons)


def test_rejects_missing_fork_push_remote():
    remotes = parse_remote_v("origin\tDISABLED_NO_PUSH_TO_NOUSRESEARCH (push)\n")
    result = check_targets(
        remotes,
        fork_owner="msreadbot",
        fork_repo="hermes-agent",
        vendor_owner="NousResearch",
        vendor_repo="hermes-agent",
    )
    assert not result.ok
    assert any("no push remote" in reason for reason in result.reasons)


def test_rejects_non_fork_pr_targets():
    result = check_targets(
        parse_remote_v("fork\thttps://github.com/msreadbot/hermes-agent.git (push)\n"),
        fork_owner="msreadbot",
        fork_repo="hermes-agent",
        vendor_owner="NousResearch",
        vendor_repo="hermes-agent",
        base_target="NousResearch/hermes-agent",
        head_target="msreadbot/hermes-agent",
    )
    assert not result.ok
    assert any("base target is not fork-only" in reason for reason in result.reasons)


def test_accepts_fork_pr_targets_and_url_forms():
    result = check_targets(
        parse_remote_v("fork\tgit@github.com:msreadbot/hermes-agent.git (push)\n"),
        fork_owner="msreadbot",
        fork_repo="hermes-agent",
        vendor_owner="NousResearch",
        vendor_repo="hermes-agent",
        base_target="https://github.com/msreadbot/hermes-agent",
        head_target="msreadbot/hermes-agent",
    )
    assert result.ok, result.reasons


def test_url_matcher_understands_ssh_and_https():
    assert matches_repo("git@github.com:NousResearch/hermes-agent.git", "NousResearch", "hermes-agent")
    assert matches_repo("https://github.com/msreadbot/hermes-agent.git", "msreadbot", "hermes-agent")
    assert not matches_repo("DISABLED_NO_PUSH_TO_NOUSRESEARCH", "NousResearch", "hermes-agent")
