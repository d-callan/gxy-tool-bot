# Code Review: gxy-tool-bot

## 1. Code Duplication

### `_format_github` and `_format_web_results` duplicated across modules
`_format_github` is identical in `generator.py:306-317` and `planner.py:200-210`. Same for `_format_web_results` (`generator.py:320-324` vs `planner.py:239-246`). These should be shared utilities.

### `_build_exemplar_text` duplicated
`generator.py:343-352` and `planner.py:300-307` are nearly identical (generator version also includes `.shed.yml`).

### `_load_template` duplicated
`generator.py:355-360` and `planner.py:310-317` are identical.

### `GeneratedTool` construction duplicated
The pattern at `generator.py:779-784` and `address_feedback.py:331-336` is identical. Could be a factory method or shared helper.

## 2. Prompt vs Validation Misalignment

### `feedback_system.txt` line 37: "Include `<xrefs>` with bio.tools cross-reference"
This is **stale** — bio.tools xref was made optional in the generator, but the feedback prompt still says to always include it. It should say "use `search_bio_tools` to check, only include if found" like the generator prompt does. The feedback prompt also doesn't mention `search_bio_tools` at all, so the agent has no way to look it up during feedback.

### `feedback_system.txt` line 34: "Use `argument="--flag"` instead of bare `name="flag"`"
This is less nuanced than the generator prompt (line 40), which has the full guidance about when to use both. The feedback prompt should match.

### `feedback_system.txt` missing CI warnings note
The generator prompt has "Planemo CI treats warnings as failures" (line 52) but the feedback prompt doesn't. This is arguably more important in the feedback flow since the agent is fixing CI failures.

### Validation checks not in the feedback prompt
The feedback prompt doesn't mention several things that `validate_generated_files` checks: `stdio` redundancy, `truevalue="true"` on booleans, missing `ftype` on test outputs, redundant label pattern. If the agent is supposed to fix CI issues, it should know about these.

## 3. Redundant `import json` inside functions

`generator.py:309`, `:323`, `:330` all do `import json` inside the `_format_*` functions, but `json` is already imported at the top of the file (line 5). These should be removed.

Similarly, `agent_loop.py:103` uses `__import__("json").dumps(...)` instead of just importing json at the top.

## 4. Brittleness

### Tool directory extraction from PR title
`on-pr-feedback.yml:36` extracts the tool directory by `cut -d':' -f1` from the PR title. If the tool directory name contains a colon, or the title format changes, this breaks silently. The `.tool-name` fallback helps, but the primary path is fragile.

### `address_feedback.py` re-writes existing files to disk unnecessarily
`address_feedback.py:310-315` writes all existing files to disk even though they were just read from disk at line 110-119. The `FileWriter.files` dict tracks them, but writing them back is redundant — they're already there.

### `generate_commit_message` catches `(json.JSONDecodeError, Exception)`
`generator.py:867` — `json.JSONDecodeError` is a subclass of `Exception`, so this is redundant. Just `except Exception` suffices.

### Exemplar cache is per-run only
`exemplars.py:30` creates a temp dir that's never cleaned up. It's also pointless — each run fetches exemplars once, so the cache check at line 37 (`if cache_path.exists()`) never hits. The cache would only help if `fetch_exemplars` was called multiple times in the same run, which it isn't.

## 5. Overcomplicated Solutions

### `_run_agent_with_validation` retry logic is complex
`generator.py:650-725` — The no-files nudge path has two sub-paths (keep history vs start fresh), and the retry loop re-constructs `files` and `validation` at the end of each iteration AND at the top of the next. The flow is hard to follow. A simpler approach: extract the "run agent and validate" into a helper, then have a clean retry loop that calls it.

### `validate_generated_files` is a 250-line monolith
`generator.py:363-610` — Each validation check is a separate concern but they're all inline. This makes it hard to test individual checks or to enable/disable checks. Could be split into individual check functions composed together.

### Repeated `if "macros.xml" in path: continue` pattern
Throughout `validate_generated_files`, nearly every check loop starts with `if "macros.xml" in path: continue`. This is repeated ~8 times. Could filter non-macro XML files once at the top.

## 6. Incompleteness

### No `.shed.yml` validation
The system prompt says to produce a `.shed.yml` (line 18), but `validate_generated_files` never checks for it. The CI will fail on a missing or malformed `.shed.yml`, but the bot's own validation won't catch it — meaning the agent won't get a retry opportunity for `.shed.yml` issues.

### No validation for missing `macros.xml`
If the agent references macros via `<expand>` but forgets to write `macros.xml`, the validation catches undefined tokens (line 438-441), but only if `macros.xml` exists to define them. If `macros.xml` is entirely missing, `macro_tokens` will be empty and every `<expand>` will be flagged — but there's no clear "you forgot to write macros.xml" message.

### `generate_commit_message` doesn't use `config` parameter
`generator.py:788-790` takes `config` but never uses it. The CLI passes `config=None` in tests. Should either use it (e.g. for model/temperature) or remove it.

### `planner.py` context truncation warning does nothing
`planner.py:352-356` logs a warning when the prompt exceeds `max_context_chars` but doesn't actually truncate. The comment says "truncating exemplars" but no truncation happens.

## 7. Other Issues

### `AgentResult.messages` defaults to `None` but is typed `list[dict]`
`agent_loop.py:43` — `messages: list[dict] = None` should be `messages: list[dict] | None = None`. The `| None` is missing from the type annotation.

### Inconsistent retry behavior
`retry.py` defaults to `max_attempts=2` (1 retry), while `api_client.py:63` also uses `max_retries=2`. But the GitHub client uses the same `retry()` with defaults. For GitHub API calls (rate-limited), 2 attempts with 1s backoff may be too aggressive.

### `config.py` duplicates defaults
`config.py:60-69` manually repeats every default from the `ApiConfig` dataclass. If a new field is added to `ApiConfig`, `load_config` must be updated in sync. Could use `**api_raw` with defaults from the dataclass, or a library like `pydantic` or `dacite`.

### `FileWriter.compress_file` imports `gzip` inline
`generator.py:155` — `import gzip` inside the function. Should be at the top of the file.

### `address_feedback.py` imports private functions
`address_feedback.py:18-20` imports `_build_tool_definitions`, `_load_template`, `_run_agent_with_validation` — all private (underscore-prefixed) from `generator.py`. These should either be public or the shared logic should be in a separate module.

---

## Summary of Recommended Actions

**High priority:**
- Fix stale `feedback_system.txt` prompt (bio.tools xref, argument/name guidance, CI warnings, missing validation guidance)
- Remove redundant `import json` inside functions
- Add `.shed.yml` presence validation
- Fix `generate_commit_message` redundant exception catch and unused `config` param

**Medium priority:**
- Extract shared utilities (`_format_github`, `_format_web_results`, `_load_template`, `_build_exemplar_text`) into a common module
- Make `_build_tool_definitions`, `_load_template`, `_run_agent_with_validation` public or move to a shared module
- Remove unnecessary file re-writing in `address_feedback.py`
- Fix planner context truncation to actually truncate or remove the warning

**Low priority:**
- Split `validate_generated_files` into composable check functions
- Simplify `_run_agent_with_validation` retry logic
- Clean up exemplar temp dir or remove pointless cache
- Fix `AgentResult.messages` type annotation
- Reduce config duplication
