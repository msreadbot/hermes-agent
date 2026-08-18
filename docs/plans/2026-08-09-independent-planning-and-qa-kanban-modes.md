# Independent Planning and QA Kanban Modes Implementation Plan

> **For Hermes:** Implement locally first in `msreadbot/hermes-agent`; do not push, open a PR, restart the gateway, or mutate production routing without separate approval.

**Goal:** Make planning/adversarial review and QA first-class independently runnable Kanban lanes, so they do not have to be children of the Linear → coder → QA implementation factory.

**Architecture:** Add a small `task_mode` field to Kanban tasks and surface it through CLI creation/listing, JSON readback, and worker context. The dispatcher still routes by assignee/status; mode is an explicit contract that tells workers and orchestrators which side effects are allowed. This preserves the safety guard that `delegate_task` children cannot mutate Kanban, while giving the writable Command Center/gateway a durable way to create standalone `plan_only` and `qa_only` cards.

**Tech Stack:** Python, SQLite-backed Kanban (`hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`), pytest.

---

## Design decisions

1. **Use modes, not a new board/profile as the first fix.** Boards remain domain/workstream ownership; profiles remain capabilities/personas. Mode is the missing contract.
2. **Keep `delegate_task` Kanban isolation intact.** The bug is not the guard; the bug is routing writable Kanban orchestration through a delegated child.
3. **Make plan and QA independently dispatchable.** `plan_only` and `qa_only` cards can be created without parents. Post-coder QA remains representable as `qa_gate`.
4. **No automatic downstream fan-out from plan-only cards.** Plans may recommend implementation/QA cards, but writable Command Center/gateway creates them after approval.
5. **Bind QA verdicts to exact evidence.** For PRs this means repo, PR number, base/head branches, exact head SHA, checks, and review state; the card body owns those details while `task_mode=qa_only|qa_gate` makes the lane machine-readable.

## Task modes

- `default` — existing behavior.
- `plan_only` — read-only planning/adversarial review; no implementation side effects.
- `implementation` — code/work implementation lane.
- `qa_only` — standalone QA/adversarial review of an existing PR, branch, diff, plan, or artifact.
- `qa_gate` — QA generated as the required gate after an implementation worker.
- `correction` — follow-up fix after QA/CodeRabbit/human review blocks.
- `srdja_handoff` — human review packet/handoff lane; no merge.

## Implementation tasks

### Task 1: Add task-mode constants and normalization

**Files:**
- Modify: `hermes_cli/kanban_db.py`

**Steps:**
1. Add `VALID_TASK_MODES` near existing Kanban constants.
2. Add `normalize_task_mode(mode)` that accepts `None`/empty as `default`, lowercases, converts `-` to `_`, and rejects unknown modes.
3. Unit-test valid aliases and invalid values.

### Task 2: Persist task mode in the tasks table

**Files:**
- Modify: `hermes_cli/kanban_db.py`
- Modify: `tests/hermes_cli/test_kanban_db.py`

**Steps:**
1. Add `task_mode TEXT NOT NULL DEFAULT 'default'` to `SCHEMA_SQL`.
2. Add optional-column migration for legacy boards.
3. Add `task_mode: str = 'default'` to the `Task` dataclass.
4. Parse `task_mode` in `Task.from_row` with backward-compatible default.
5. Update the create insert to write `task_mode`.
6. Update migration concurrency test so already-migrated schemas may include the new column without error.

### Task 3: Expose mode through CLI create/list/show JSON

**Files:**
- Modify: `hermes_cli/kanban.py`
- Modify: `tests/hermes_cli/test_kanban_cli.py`

**Steps:**
1. Add `kanban create --mode {default,plan_only,implementation,qa_only,qa_gate,correction,srdja_handoff}`.
2. Pass `task_mode` to `kb.create_task`.
3. Include `task_mode` in `_task_to_dict`.
4. Add `kanban list --mode <mode>` filtering.
5. Include mode in human `show` output when not `default`.
6. Add CLI tests for JSON create/readback and list filtering.

### Task 4: Surface mode contract in worker context

**Files:**
- Modify: `hermes_cli/kanban_db.py`
- Modify: `tests/hermes_cli/test_kanban_db.py`

**Steps:**
1. Add `Mode: <task_mode>` to `build_worker_context` for non-default modes.
2. For `plan_only`, inject an explicit contract: read-only planning, no repo edits/branches/commits/PRs, no external writes, no downstream Kanban creation; output durable plan and `changed_files=[]`.
3. For `qa_only`, inject an explicit contract: independent review, bind verdict to exact source/head/artifact, no code changes unless separately authorized, output approve/block/needs-source and `changed_files=[]`.
4. For `qa_gate`, inject current-head QA gate guidance.
5. Add tests proving these contracts appear in worker context.

### Task 5: Local verification

**Commands:**

```bash
cd /Users/moltbot-user/.hermes/hermes-agent
python -m pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py tests/tools/test_delegate_kanban_isolation.py -q
python -m ruff check hermes_cli/kanban.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py tests/tools/test_delegate_kanban_isolation.py
python -m compileall hermes_cli/kanban.py hermes_cli/kanban_db.py
```

**Expected:** all selected tests pass; lint/compile clean. If the environment lacks ruff or has unrelated failures, report the exact blocker.

## Non-goals for this local implementation

- No new profile yet.
- No new board yet.
- No Slack/gateway restart.
- No production dispatcher behavior change.
- No automatic CodeRabbit/Linear/GitHub integration.
- No PR/push/merge.

## Follow-up after local verification

1. Create mode-specific routing helpers/prompts in Command Center/SOUL after approval.
2. Add Slack slash/command aliases if needed, e.g. `plan`, `qa-pr`, `qa-plan`.
3. Add smoke cards:
   - standalone `plan_only` to `echlon-coder`;
   - standalone `qa_only` to `echlon-qa` for an existing PR;
   - normal implementation → `qa_gate` child.
4. Only then consider a locked-down `echlon-planner` profile if mode contracts are insufficient.
