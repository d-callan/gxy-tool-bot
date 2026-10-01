# Developer Guide

This guide helps developers find their way around the codebase when adding new
conventions, validation checks, or prompt changes.

## Key Files

### Prompts (system + user templates)

| File | Used by | Purpose |
|------|---------|---------|
| `gxy_tool_bot/templates/generator_system.txt` | Generation flow | System prompt — tells the agent how to write Galaxy tool XML, IUC conventions, available tools |
| `gxy_tool_bot/templates/generator_user.txt` | Generation flow | User prompt — contains the plan, exemplar tools, and instructions |
| `gxy_tool_bot/templates/feedback_system.txt` | Feedback flow | System prompt — tells the agent how to fix existing tools based on CI/reviewer feedback |
| `gxy_tool_bot/templates/update_planner_system.txt` / `update_planner_user.txt` | Update flow (plan) | Update-plan prompts — agent inspects the existing wrapper and drafts a maintainer-reviewable plan |
| `gxy_tool_bot/templates/update_system.txt` | Update flow (implement) | System prompt — tells the agent to implement an approved update plan on the staged tool dir |
| `gxy_tool_bot/templates/review_system.txt` | Review flow | System prompt — tells the review agent how to review tool files, what categories to check, and how to format findings |
| `gxy_tool_bot/templates/_conventions.txt` | All flows | Shared IUC conventions included via Jinja2 `{% include %}` in all system prompts. Update this file once to change a convention everywhere. |
| `gxy_tool_bot/address_feedback.py` (`_build_feedback_user_prompt`) | Feedback flow | User prompt — built dynamically from PR comments, CI artifacts, and file listing |

### Validation

| File | Purpose |
|------|---------|
| `gxy_tool_bot/validation.py` | `ValidationResult`, `validate_generated_files`, and `run_agent_with_validation` — all validation logic lives here |
| `gxy_tool_bot/generator.py` | `FileWriter`, `GeneratedFile`, `GeneratedTool`, tool definitions, and the `generate_tool` entry point |
| `gxy_tool_bot/address_feedback.py` | Feedback collection, prompt building, `address_feedback` and `update_tool` entry points, and `_run_edit_agent` — the shared agent+validation tail both flows use |
| `gxy_tool_bot/review.py` | Review module — `ReviewFinding`, `ReviewResult`, `collect_review_context`, `run_review`, `parse_findings`, `run_integrated_review`. Used by both standalone review and integrated self-review. |
| `gxy_tool_bot/utils.py` | Shared helpers — `read_tool_files` (used by feedback and review flows) |

### Agent loop

| File | Purpose |
|------|---------|
| `gxy_tool_bot/agent_loop.py` | Core agent loop — handles tool calls, message history, iteration limits |

## When to Put Things Where

There are three places a convention can live. The goal is a concise prompt that
gets the agent writing quickly, with validation as a safety net — and letting
planemo CI catch the rest.

### 1. System prompts (proactive guidance)

**Use when:** The convention can't be tested by inspecting files, or requires
judgment/context the agent needs before writing.

**Files:** `templates/generator_system.txt`, `templates/feedback_system.txt`
(usually add to both).

**Examples:** "use `galaxy_slots` for threading", "make asserts strong and
specific", "use `mv` instead of `cp`".

**Keep concise** — every line costs tokens on every LLM call. Prefer getting
the agent to write something and iterate rather than bogging it down with
exhaustive rules upfront.

### 2. Validation checks (structural safety net)

**Use when:** The convention can be detected by inspecting the generated files
(XML structure, attribute patterns, missing test data, etc.).

**File:** `gxy_tool_bot/validation.py` → `validate_generated_files`

**Each check must produce a clear error message** telling the agent exactly
what to fix. These run after the agent writes, so they don't bloat the prompt
but still catch mistakes on retry.

**Examples:** "don't use the bare default output label", "missing
`expect_num_outputs` on `<test>`", "Cheetah in `<xml>` macros".

### 3. Neither (let planemo CI catch it)

**Use when:** Planemo already checks it explicitly (e.g. XML well-formedness,
shed metadata, duplicated output labels).

The CI workflow reports these failures and the feedback flow picks them up on
the next iteration. Only add a check to validation or the prompt if the bot is
**consistently** making that specific mistake, wasting tokens and maintainer time 
on retries that a simple upfront rule would prevent.

### Quick reference

| Tier | Where | When | Token cost |
|------|-------|------|------------|
| Prompt | `templates/*_system.txt` | Can't test by inspection; needs judgment | Every LLM call |
| Validation | `validation.py` | Can inspect files; clear fix message | Only on retry |
| Neither | Planemo CI | Planemo already checks it | Zero |

### Adding a new tool for the agent

Add the tool definition to `_build_tool_definitions` in `gxy_tool_bot/generator.py`.
If the tool has a handler method, add it to the `FileWriter` class (for file-related
tools) or as a standalone function.

### Modifying the feedback prompt

The feedback user prompt is built dynamically in `_build_feedback_user_prompt`
in `gxy_tool_bot/address_feedback.py`. The system prompt is in
`templates/feedback_system.txt`.

### The update flow

A `tool-update` issue label routes the issue through the existing `plan` and
`generate` CLI commands, which detect the label and branch internally — no
extra workflows or CLI commands. `plan` calls `generate_update_plan`
(`gxy_tool_bot/planner.py`) which parses the issue with
`parse_update_issue_body` (handles GitHub issue-form `### Heading` output),
inlines the current wrapper files, and posts a plan under the same
`PLAN_MARKER` comment as new-tool plans. `generate` calls `update_tool`
(`gxy_tool_bot/address_feedback.py`), which stages `tools/<dir>` into the
output dir verbatim (binary test data survives as real bytes) and hands off
to `_run_edit_agent` — the shared tail also used by `address_feedback`.
Because the staged dir lands in `generated/` and `.tool-name` names the tool,
`on-ready-to-implement.yml` opens the PR unmodified.

Budget knobs are the same as generate: `max_tool_iterations`,
`max_validation_retries`, and `validation_retries_per_extra_tool_xml` — for
updates the scaling counts tool XMLs in the existing dir
(`count_tool_xmls_in_dir`) instead of the plan. The only new config key is
`labels.tool_update` (default `tool-update`).

Note: `labels.*` config only affects CLI-side detection — GitHub Actions
evaluates issue labels before the CLI runs, so the workflow `if:`
predicates hard-code the default label names (`tool-request`,
`tool-update`). Renaming a label in `.gxy-tool-bot.yml` means updating the
matching predicate in `on-tool-request.yml` (and the issue template) too.

### The autoupdate flow

`workflows/autoupdate.yml` runs scheduled version bumps with no issue or
plan step. `gxy_tool_bot/autoupdate.py` owns the logic; the workflow is
thin shell over two CLI commands:

- `gxy-tool-bot autoupdate-detect` scans `tools/*/` and prints a JSON
  array of outdated tools (main requirement behind the latest version on
  the configured conda channels, via the anaconda.org package API — no
  planemo dependency). The workflow feeds it into a per-tool matrix job.
- `gxy-tool-bot autoupdate --tool-dir tools/<dir>` re-checks that one dir,
  applies the dedup rules in `check_autoupdate_pr_state` (needs
  `GitHubClient.list_prs`), then calls `update_tool` with
  `system_template="autoupdate_system.txt"` — a narrowed prompt that tells
  the agent to check upstream for breaking changes and new parameters.
  Plan/description/link inputs are synthesized by `build_autoupdate_*`
  helpers since there's no issue to parse.

Dedup semantics mirror planemo-autoupdate: an open PR on
`tool-bot/autoupdate-<dir>` skips the run; a closed unmerged PR only
reopens when the detected version beats the declined one (parsed from the
PR title); a branch whose last commit isn't by `gxy-tool-bot` is never
overwritten. Results reach the workflow through marker files in
`$GITHUB_WORKSPACE` (`.autoupdate-skip`, `.autoupdate-pr`) plus the
same `generated/.tool-name` / `.commit-msg` / `.pr-body` outputs the other
flows use. An open PR isn't just skipped: when a newer version appears,
new commits fold into it (and its title is re-synced).

Config lives under `autoupdate:` (`enabled`, `channels`, `skip`,
`skip_file`, `max_tools_per_run` — blast-radius cap, default 10; the
matrix additionally caps concurrency with `max-parallel`) — the run
frequency itself can only live in the workflow's `cron:` line. Budget
knobs (`max_tool_iterations`, `max_validation_retries`,
`validation_retries_per_extra_tool_xml`) apply as-is.

## Running Tests

```bash
conda run -n gxy-tool-bot python -m pytest tests/ -v
```

Validation tests are in `tests/test_generator.py` (they test
`validate_generated_files` from `gxy_tool_bot/validation.py`).

## CI Environment

The CI workflows install `planemo` and `micromamba` (for the `run_in_conda` tool).
Micromamba is installed from `https://micro.mamba.pm/api/micromamba/linux-64/latest`
and added to `PATH`. The `run_in_conda` tool uses it to create cached conda
environments from bioconda/conda-forge and run commands directly. If neither
micromamba nor conda is available, the tool is not added to the agent's toolset.

## Eval Harness

The eval harness (`gxy_tool_bot/eval_harness.py`) runs generate and feedback
cases against real LLM calls to measure bot performance. See the [README](README.md#eval-harness)
for usage details.

### Key files

| File | Purpose |
|------|---------|
| `gxy_tool_bot/eval_harness.py` | Core eval harness: case loading, case runners, assertions, report generation |
| `gxy_tool_bot/cli.py` (`eval` command) | CLI entry point for running evals |
| `eval/cases/` | Eval case fixtures (YAML + supporting files) |
| `workflows/eval.yml` | CI workflow for manual eval runs |
| `tests/test_eval_harness.py` | Unit tests for the harness (case loading, assertions, summaries) |

### How it works

- **Generate cases** call `generate_tool()` directly with a plan from the case fixture, then run assertions and (optionally) planemo on the output.
- **Feedback cases** construct a `FeedbackContext` from the case YAML (simulated reviewer comments + CI failures), then call `run_agent_with_validation()` directly — no real GitHub PR needed.
- **Update cases** (`type: update`) stage the case's `existing_files` into a `src/` dir and call `update_tool()` with the case's `plan.md` and `update.description`/`update.links` — the real production path.
- Both paths reuse the real production code, so eval results reflect actual bot behavior.
- The harness measures: validation pass/fail, planemo lint/test pass/fail, agent iteration count, validation retry count, and structural assertions (XML element existence, file content patterns, etc.).
- `run_agent_with_validation` now returns a 4th value (`validation_retries: int`) — both `generate_tool` and `address_feedback` unpack it.

## Tool Review

The review module (`gxy_tool_bot/review.py`) provides two modes:

### Standalone review

Triggered by the `review` label on a PR. The CLI `review` command collects context (files, CI, exemplars), runs a review agent with read-only tools, and posts structured findings as a PR comment.

### Integrated self-review

When `integrated_review_mode` is enabled in config, `generate_tool` and `address_feedback` call `run_integrated_review()` after the main agent loop completes. This runs a review agent (fresh context), feeds findings back to the original agent (continuing from its conversation history), and repeats for up to `max_review_fix_rounds` rounds.

### Context management

- **Review agent**: always fresh context (no inherited conversation). This gives it "fresh eyes" — it reviews files as they are, not as the writer intended.
- **Post-review fix agent**: continues from the original conversation history (same pattern as validation retries in `run_agent_with_validation`). Efficient because the agent already knows what it did; context bloat is auto-handled by `run_agent_loop`'s summarization.
- **Re-review (round 2+)**: fresh context each time. Each review is independent and unbiased.

### Review agent tools

The review agent has read-only tools only: `read_file`, `planemo_lint`, `planemo_test`, `run_in_conda`, `search_github`, `search_web`, `search_bio_tools`. No write tools — it analyzes, doesn't fix. The fix is done by the original agent in the integrated fix rounds.

### Findings format

The review agent outputs structured findings:
```
### [severity] category: file:line
Description.
Suggestion: How to fix.
```

Severity: `critical`, `warning`, `suggestion`. Categories: `validation`, `conventions`, `completeness`, `test_coverage`, `security_bugs`, `brittleness`. The `parse_findings()` function parses this into `ReviewFinding` objects, with a graceful fallback for malformed output.
