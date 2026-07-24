# gxy-tool-bot

Because once upon a time Björn said to Danielle: Wouldn't an Agentic Galaxy Tool Bot be cool? :smile:

So this is an agentic bot that generates [Galaxy](https://galaxyproject.org/) tool wrappers from user requests, powered by LLM APIs and orchestrated through GitHub Actions.

## Overview

`gxy-tool-bot` is a Python library + companion GitHub Actions workflows that automate the creation of Galaxy tool wrappers. It is **not a tool repo itself** — it's a library consumed by repos that house Galaxy tools.

## Current Status

This repository is under active development. The bot can plan tool wrappers, generate XML/macros/test data, and open PRs automatically. It has been tested end-to-end on real tool requests. Expect rough edges and iterate on configuration as needed.

## How it works

1. **Request:** Users file a GitHub issue using the "Tool Request" issue template (structured fields for tool name, description, links, contact).
2. **Plan:** The `tool-request` label triggers a workflow. An agent researches the tool (bioconda, GitHub, publications, web) and posts a plan as an issue comment with a `plan-ready` label.
3. **Review:** A maintainer reviews the plan and adds the `ready-to-implement` label.
4. **Generate:** An agent generates the tool XML, macros, and test data, then opens a PR with a `pr-opened` label on the issue. If the agent gives up or crashes, a `generation-failed` label is applied instead. If generation completes but validation finds issues, the PR is still created with the validation errors noted in the description — apply the `address-feedback` label to have the bot attempt fixes.

## Setup (for consuming repos)

### 1. Install the bot

The bot can be installed from PyPI or directly from GitHub:

```bash
pip install gxy-tool-bot              # from PyPI
pip install git+https://github.com/d-callan/gxy-tool-bot.git  # latest from GitHub
```

### 2. Create a config file

Create `.gxy-tool-bot.yml` in the repo root:

```yaml
api:
  base_url: https://openrouter.ai/api/v1   # or https://api.openai.com/v1
  model: z-ai/glm-5.2                       # or gpt-4o, etc.
  fallback_models:                          # optional; tried in order if primary fails
    - deepseek-ai/deepseek-r1
    - gpt-4o
  max_tool_iterations: 25
  temperature_plan: 0.4
  temperature_generate: 0.2
  max_context_chars: 100000
  max_validation_retries: 3
  read_timeout: 600                         # seconds; increase for slow models

exemplars:
  - url: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/bcftools/bcftools_view.xml
    macros: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/bcftools/macros.xml
    shed_yml: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/bcftools/.shed.yml
  - url: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/seqtk/seqtk_seq.xml
    macros: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/seqtk/macros.xml
    shed_yml: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/seqtk/.shed.yml

# repo is optional — falls back to GITHUB_REPOSITORY env var (set automatically in GitHub Actions)
repo: your-org/your-repo

allowed_maintainers:
  - your-github-handle
```

### 3. Create GitHub labels

Create these labels in the repo (Settings → Labels):

| Label | Color | Purpose |
|-------|-------|---------|
| `tool-request` | `#0075ca` | Applied automatically by issue template; triggers planning |
| `plan-ready` | `#a2eeef` | Applied by bot after plan is posted |
| `ready-to-implement` | `#0e8a16` | Applied by maintainer to approve plan; triggers generation |
| `pr-opened` | `#1d76db` | Applied by bot after PR is created |
| `generation-failed` | `#b60205` | Applied by bot if generation or planning fails |
| `retry-plan` | `#fbca04` | Applied by user/maintainer to re-trigger planning after a failure |
| `retry-generate` | `#fbca04` | Applied by user/maintainer to re-trigger generation after a failure |
| `address-feedback` | `#5319e7` | Applied to a PR to have the bot address review comments and CI failures |

### 4. Add the issue template

Copy `examples/issue-template-tool-request.yml` from this repo into your repo's `.github/ISSUE_TEMPLATE/tool-request.yml`. The template auto-applies the `tool-request` label so the planning workflow triggers automatically.

### 5. Add workflow files

Copy the workflow templates from the [`workflows/`](workflows/) directory in this repo into your repo's `.github/workflows/`:

- **`on-tool-request.yml`** → `.github/workflows/gxy-on-tool-request.yml` — triggers on new issues with `tool-request` label or when `retry-plan` label is added; runs the planner
- **`on-ready-to-implement.yml`** → `.github/workflows/gxy-on-ready-to-implement.yml` — triggers when `ready-to-implement` or `retry-generate` label is added; runs the generator and opens a PR
- **`on-pr-feedback.yml`** → `.github/workflows/gxy-on-pr-feedback.yml` — triggers when `address-feedback` label is added to a PR; reads review comments and CI failures, pushes fixes as new commits

> **CI artifact assumption:** The feedback workflow fetches CI failure details from GitHub Actions artifacts. This assumes the CI workflow uploads failure reports as artifacts (e.g. lint reports as `.txt` files, planemo test results as `.json`), following the same conventions as the [tools-iuc](https://github.com/galaxyproject/tools-iuc) repo's `pr.yaml` workflow. If your repo uses a different CI setup that doesn't upload artifacts on failure, the bot will not be able to include CI failure details in its feedback context.

> **Validation flag file:** When generation or feedback addressing completes but validation finds issues, the CLI writes a `.validation-failed` flag file to `GITHUB_WORKSPACE` (a built-in GitHub Actions env var). The workflows check for this file to post a comment on the PR noting that validation issues exist. The file is cleaned up after use and is never committed.

Both workflows install the bot from PyPI (pinned to a specific version):

```yaml
- run: pip install gxy-tool-bot==0.1.1
```

### 6. Add repo secrets

Go to Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value | Used by |
|--------|-------|---------|
| `GXY_TOOL_BOT_API_KEY` | Your LLM API key (e.g. OpenRouter key) | Plan + generate workflows |

The `GITHUB_TOKEN` (automatically provided by GitHub Actions) is used for issue comments, labels, and PR creation — no extra secret needed.

### 7. Enable Issues

If the repo is a fork, Issues may be disabled by default. Enable them under Settings → General → Features → check "Issues".

### 8. Enable Actions

Make sure Actions are enabled: Settings → Actions → General → "Allow all actions and reusable workflows".

### 9. Test it

1. Go to Issues → New Issue → "Tool Request" template
2. Fill in the fields and submit
3. Check the Actions tab — the planning workflow should run
4. After the plan is posted, add the `ready-to-implement` label
5. The generation workflow should run and open a PR

## Philosophy & Design Decisions

### Run in GitHub Actions, not a black box

The bot runs entirely in GitHub Actions. This keeps agent traces, plans, generated files, and CI results publicly inspectable. Every run produces logs and artifacts you can review after the fact — to diagnose why a specific tool was written a certain way, or to identify patterns to improve the bot. The Actions logs capture tool calls, context size per iteration, and validation results; combined with file-based artifacts (generated files, test data) and summary comments posted to issues and PRs, this provides enough transparency to understand what the bot did and why.

### Validate files, don't over-prompt

Best practices don't go in the system prompt — they go in a [validation loop](gxy_tool_bot/validation.py) that inspects generated files for common mistakes (missing test data, bare output labels, Cheetah in macros, etc.) and instructs the agent to fix them in a retry loop. This keeps prompts concise (every line costs tokens on every LLM call) and prevents unnecessary bloat that might distract or confuse the agent, while still catching errors structurally. See [DEVELOPMENT.md](DEVELOPMENT.md) for the full tiered guidance on where to put conventions.

### Incremental feedback, not infinite loops

The validation loop only runs a limited number of times before forcing the result back to human hands — a commit is pushed to a PR that a maintainer can review and provide further direction on. The maintainer adds the `address-feedback` label to trigger another loop with CI failures and review comments incorporated. This keeps the bot autonomous for the common case but avoids burning tokens indefinitely on a problem it can't solve alone.

### Let planemo CI catch the rest

Things planemo already checks (XML well-formedness, shed metadata, duplicated output labels) are deliberately not duplicated in validation. The CI workflow reports these failures and the feedback flow picks them up on the next iteration. Only add a validation check if the bot is consistently making a specific mistake that wastes tokens and maintainer time.

### Give the agent a way out

The agent has a `give_up` tool that lets it stop and explain why it can't proceed — open assumptions, unresolved questions, or fundamental issues with the request. A tool request might produce a recommendation not to make a tool rather than a plan to make one. A feedback request might result in push back with no commit until something the agent flags is resolved. This prevents forcing the agent into action when it doesn't have enough information, which would produce low-quality output. Better to surface the problem to a human than to generate a confident but wrong tool wrapper.

## Tips & Tricks

The bot is automated but not magic. It implements what's requested — it doesn't "think" about whether a request makes sense. If a request is vague or contradictory, the bot may realize it needs to `give_up` and instead iterate on a wrong path and/or produce nonsense. The more detail you provide upfront, the better the results.

- **Edit the plans it generates.** The plan is your chance to course-correct before any code is written. Refine inputs, outputs, and parameters before adding the `ready-to-implement` label.
- **Give detailed review feedback.** Instead of "this is wrong", explain what's wrong and how to fix it. The bot follows instructions literally.
- **Resolve comments you don't need addressed.** During feedback loops, the bot sees all unresolved review comments. Stale comments it no longer needs to act on will confuse and distract it. Resolve them so the bot can focus on what still matters.
- **Help it with test failures.** Look at the CI failures yourself and tell the bot specifically how to fix them in a PR comment. The bot can struggle with planemo test failures that require domain knowledge or environment-specific context.
- **CI failures not uploaded as artifacts are invisible to the bot.** The feedback flow reads CI failure details from GitHub Actions artifacts. If a failure isn't uploaded as an artifact (e.g. a missing dependency, a runner error), the bot can't see it — you need to describe it in a PR comment explicitly.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for guidance on the codebase structure, where to add new conventions (prompts vs. validation vs. let CI catch it), and how the generation and feedback flows are organized.

## License

MIT
