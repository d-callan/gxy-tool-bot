# Code Review: gxy-tool-bot

## Recently Fixed Issues

The following were addressed in recent commits:

- **`api_client.py`**: Now retries on `ReadTimeout` and `ConnectError` (was only retrying on JSON parse errors / missing choices). Default `read_timeout` increased from 300s to 600s.
- **`address_feedback.py`**: Added `no_files_nudge` with `write_tools` tracking via tool call trace. Previously, feedback mode had no nudge at all — the agent could spend all iterations researching without any intervention.
- **`feedback_system.txt`**: Strengthened prompt to emphasize "Go straight to fixing" and "Start fixing immediately" in guidelines.
- **`_run_agent_with_validation`**: Now accepts `write_tools` param and checks `result.tool_call_trace` for write calls instead of comparing file key sets (which didn't work for feedback mode where files are overwritten).
- **Output label guidance**: Fixed planemo lint failures from duplicated output labels. Prompts now instruct using distinct descriptive labels (e.g. `${tool.name} log on ${on_string}`) for multiple outputs. Validation only flags the bare default label.
- **Shared conventions template**: Extracted common IUC conventions from `generator_system.txt` and `feedback_system.txt` into `templates/_conventions.txt`, included via Jinja2 `{% include %}`. Updating a shared convention now requires editing one file.
- **Validation module extracted**: `ValidationResult`, `validate_generated_files`, and `run_agent_with_validation` moved from `generator.py` to new `gxy_tool_bot/validation.py`. Both flows import from there. Renamed `_run_agent_with_validation` → `run_agent_with_validation` (public). Developer docs added in `DEVELOPMENT.md` with three-tier guidance for where to put conventions (prompts vs. validation vs. let planemo CI catch it).

### Remaining timeout concerns

- The `read_timeout` of 600s means worst case is 2 × 10 min = 20 min for a single LLM call that times out twice. This is long but acceptable given the alternative (failing the entire run).
- The retry logic in `api_client.py` now catches `ReadTimeout` and `ConnectError` but not `httpx.RemoteProtocolError` or `httpx.PoolTimeout` — these would still kill the run immediately. May want to add them if they occur in practice.
- The `run_agent_with_validation` retry loop does not catch `ReadTimeout` from `run_agent_loop` — if the LLM times out on a validation retry, the entire feedback/generate flow fails. The retry in `api_client.py` handles individual calls, but the outer loop doesn't have its own try/except. This is probably fine (if the LLM is consistently timing out, retrying at the validation level won't help), but worth noting.

---

## High Priority

### Issue 1: Remove redundant inline `import json` statements

**Description:**

`generator.py:309`, `:323`, `:330` all do `import json` inside the `_format_*` functions, but `json` is already imported at the top of the file (line 5). These inline imports are redundant and should be removed.

Similarly, `agent_loop.py:103` uses `__import__("json").dumps(...)` instead of just importing `json` at the top of the file. This is both non-idiomatic and slower (imports are cached but the `__import__` call is still evaluated each time).

**Fix:** Remove inline `import json` from `_format_*` functions in `generator.py`. Add `import json` at the top of `agent_loop.py` and replace `__import__("json").dumps(...)` with `json.dumps(...)`.

---

### Issue 2: Add `.shed.yml` presence validation

**Description:**

The system prompt instructs the agent to produce a `.shed.yml` file, but `validate_generated_files` never checks for it. CI will fail on a missing or malformed `.shed.yml`, but the bot's own validation won't catch it — meaning the agent won't get a retry opportunity for `.shed.yml` issues. The agent could go through all its iterations, pass validation, and still fail CI.

Additionally, if the agent references macros via `<expand>` but forgets to write `macros.xml` entirely, `macro_tokens` will be empty and every `<expand>` will be flagged as undefined — but there's no clear "you forgot to write macros.xml" message. A specific check for this would give the agent more actionable feedback.

**Fix:** Add a check in `validate_generated_files` for:
- `.shed.yml` file presence (at least one file ending in `.shed.yml`)
- `macros.xml` presence when any tool XML uses `<expand macro=...>`

---

### Issue 3: Fix `generate_commit_message` redundant exception catch and unused `config` parameter

**Description:**

Two issues in `generate_commit_message` (`generator.py`):

1. **Redundant exception catch.** The function catches `(json.JSONDecodeError, Exception)` but `json.JSONDecodeError` is a subclass of `Exception`, so this is equivalent to just `except Exception`. The explicit `JSONDecodeError` is misleading — it suggests special handling that doesn't exist.

2. **Unused `config` parameter.** The function takes a `config` parameter but never uses it. The CLI passes `config=None` in tests. Should either use it (e.g. for model/temperature) or remove it.

**Fix:** Simplify to `except Exception`. Remove `config` parameter or use it for temperature/model configuration.

---

### Issue 4: Continue extracting shared code from `generator.py`

**Description:**

`generator.py` still serves two roles:
1. **Shared infrastructure** — `FileWriter`, `GeneratedFile`, `GeneratedTool`, tool definitions (`_build_tool_definitions`), template loading (`_load_template`), format utilities
2. **Generate-specific logic** — `generate_tool()` function, exemplar text building

`address_feedback.py` depends on role 1 but not role 2. It still imports `_build_tool_definitions` and `_load_template` — both private (underscore-prefixed) from `generator.py`. This is an antipattern: the private prefix signals internal implementation details, yet another module depends on them.

Additionally, several utility functions are duplicated across `generator.py` and `planner.py`:
- `_format_github` — identical in both
- `_format_web_results` — identical in both
- `_build_exemplar_text` — nearly identical
- `_load_template` — identical
- `GeneratedTool` construction — same pattern in both flows

**Fix:** Extract a shared module (e.g. `tool_agent.py` or expand `validation.py`) containing:
- `FileWriter`, `GeneratedFile`, `GeneratedTool`
- `build_tool_definitions` (rename from `_build_tool_definitions`)
- `load_template` (rename from `_load_template`)
- Shared format utilities (`format_github`, `format_web_results`, `build_exemplar_text`)

Then `generator.py`, `address_feedback.py`, and `planner.py` each import from the shared module and contain only their flow-specific logic.

---

## Medium Priority

### Issue 5: Remove unnecessary file re-writing in `address_feedback.py`

**Description:**

`address_feedback.py:310-315` writes all existing files to disk even though they were just read from disk. The `FileWriter.files` dict tracks them for the agent, but writing them back to disk is redundant — they're already there from the PR checkout.

**Fix:** Remove the `dest.write_text(...)` loop. Keep only the `file_writer.files[path] = content.encode("utf-8")` line to track files in the writer.

---

### Issue 6: Fix planner context truncation warning that does nothing

**Description:**

`planner.py:352-356` logs a warning when the prompt exceeds `max_context_chars` but doesn't actually truncate. The log message says "truncating exemplars" but no truncation happens. This is misleading — either implement the truncation or remove the warning.

**Fix:** Either truncate exemplars when the prompt exceeds `max_context_chars`, or change the log message to accurately reflect that no action is taken.

---

### Issue 7: Parameterize validation error message for generate vs feedback mode

**Description:**

The `error_msg` in `run_agent_with_validation` (`validation.py`) contains generate-specific guidance: "To fix macro/token errors: add the missing `<xml name="NAME">`..." and "To fix test data errors: write the missing test data files with `write_file`." This guidance is less relevant for feedback mode, where the agent is fixing existing files rather than generating from scratch.

Additionally, the default `write_tools` set should be `{"write_file", "compress_file", "download_file"}` for both flows (generate mode also uses `compress_file` and `download_file`), but currently `address_feedback.py` defines this locally while `generate_tool` doesn't pass it at all (defaulting to just `{"write_file"}`).

**Fix:**
- Accept an optional `error_msg` or `error_msg_builder` parameter in `run_agent_with_validation` so each flow can customize the validation error guidance.
- Set the default `write_tools` to `{"write_file", "compress_file", "download_file"}` in `run_agent_with_validation` so both flows detect all write operations.

---

### Issue 8: Extract CLI helper for `generate_commit_message` calls

**Description:**

The CLI has duplicated patterns for `generate_commit_message` calls (`cli.py:189-208` and `cli.py:281-297`). Both create an `ApiClient`, call `generate_commit_message`, write the result to a file, and catch exceptions with a warning log. The only difference is the mode (`"generate"` vs `"feedback"`) and whether `pr_body_path` is used.

**Fix:** Extract a shared CLI helper function like `write_commit_message(client, config, mode, tool_name, number, summary, commit_msg_path, pr_body_path=None)` to eliminate the duplication.

---

### Issue 9: Separate data collection from prompt formatting in feedback flow

**Description:**

The planner and generator flows use Jinja2 templates (`planner_user.txt`, `generator_user.txt`) for the user prompt, but the feedback flow builds it dynamically in `_build_feedback_user_prompt()` (`address_feedback.py`). This is justified — the feedback prompt requires significant preprocessing (filtering bot comments, truncating CI output, parsing test JSON, conditionally including sections) that would make for a messy template.

However, the data preprocessing and prompt assembly are mixed together in one 70-line function. Separating the data collection/filtering from the prompt formatting would improve testability and consistency with the other flows.

**Fix:** Split `_build_feedback_user_prompt` into two functions:
1. A data preparation function that returns a structured dict/object with filtered comments, summarized CI artifacts, etc.
2. A prompt formatting function (or template) that takes the structured data and renders the prompt text.

---

## Low Priority

### Issue 10: Code cleanup and minor improvements

**Description:**

Several minor issues that are worth addressing but not urgent:

1. **Split `validate_generated_files` into composable check functions.** `validation.py` — `validate_generated_files` is a 250-line monolith where each validation check is a separate concern but all inline. This makes it hard to test individual checks or enable/disable checks. Could be split into individual check functions composed together.

2. **Simplify `run_agent_with_validation` retry logic.** `validation.py` — The no-files nudge path has two sub-paths (keep history vs start fresh), and the retry loop re-constructs `files` and `validation` at the end of each iteration AND at the top of the next. Extract the "run agent and validate" into a helper for a cleaner retry loop.

3. **Repeated `if "macros.xml" in path: continue` pattern.** Throughout `validate_generated_files`, nearly every check loop starts with this guard. Repeated ~8 times. Could filter non-macro XML files once at the top.

4. **Clean up exemplar temp dir.** `exemplars.py:30` creates a temp dir that's never cleaned up. The cache is also pointless — each run fetches exemplars once, so the cache check never hits. Either clean up the temp dir or remove the caching logic entirely.

5. **Fix `AgentResult.messages` type annotation.** `agent_loop.py:43` — `messages: list[dict] = None` should be `messages: list[dict] | None = None`. The `| None` is missing from the type annotation.

6. **Reduce `config.py` default duplication.** `config.py:60-69` manually repeats every default from the `ApiConfig` dataclass. If a new field is added to `ApiConfig`, `load_config` must be updated in sync. Could use `**api_raw` with defaults from the dataclass, or a library like `dacite`.

7. **Move `import gzip` to top of `generator.py`.** `generator.py:155` imports `gzip` inside `FileWriter.compress_file`. Should be at the top of the file.

8. **Brittle tool directory extraction from PR title.** `on-pr-feedback.yml:36` extracts the tool directory by `cut -d':' -f1` from the PR title. If the tool directory name contains a colon, or the title format changes, this breaks silently. The `.tool-name` fallback helps, but the primary path is fragile.

---

## TODO: Workflow sync

The `tools-iuc` repo's `main` branch has a stale `gxy-on-pr-feedback.yml` workflow that doesn't pass `--commit-msg-path`. The updated version is on the `d-callan/main` branch (commit `6ec8ae785`) but hasn't been merged to `main`. This causes generic commit messages because the workflow never writes `.commit-msg`. Need to merge the workflow update to `main` on `tools-iuc`.
