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
| `gxy_tool_bot/templates/_conventions.txt` | Both flows | Shared IUC conventions included via Jinja2 `{% include %}` in both system prompts. Update this file once to change a convention in both flows. |
| `gxy_tool_bot/address_feedback.py` (`_build_feedback_user_prompt`) | Feedback flow | User prompt — built dynamically from PR comments, CI artifacts, and file listing |

### Validation

| File | Purpose |
|------|---------|
| `gxy_tool_bot/validation.py` | `ValidationResult`, `validate_generated_files`, and `run_agent_with_validation` — all validation logic lives here |
| `gxy_tool_bot/generator.py` | `FileWriter`, `GeneratedFile`, `GeneratedTool`, tool definitions, and the `generate_tool` entry point |
| `gxy_tool_bot/address_feedback.py` | Feedback collection, prompt building, and the `address_feedback` entry point |

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

## Running Tests

```bash
conda run -n gxy-tool-bot python -m pytest tests/ -v
```

Validation tests are in `tests/test_generator.py` (they test
`validate_generated_files` from `gxy_tool_bot/validation.py`).
