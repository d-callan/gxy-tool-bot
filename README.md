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
4. **Generate:** An agent generates the tool XML, macros, and test data, then opens a PR with a `pr-opened` label on the issue. If generation fails, a `generation-failed` label is applied instead.

## Setup (for consuming repos)

### 1. Install the bot

The bot is installed from GitHub (not yet on PyPI):

```bash
pip install git+https://github.com/d-callan/gxy-tool-bot.git
```

### 2. Create a config file

Create `.gxy-tool-bot.yml` in the repo root:

```yaml
api:
  base_url: https://openrouter.ai/api/v1   # or https://api.openai.com/v1
  model: z-ai/glm-5.2                       # or gpt-4o, etc.
  max_tool_iterations: 25
  temperature_plan: 0.4
  temperature_generate: 0.2
  max_context_chars: 100000
  max_validation_retries: 3

exemplars:
  - url: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/bcftools/bcftools_view.xml
    macros: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/bcftools/macros.xml
  - url: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/seqtk/seqtk_seq.xml
    macros: https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/seqtk/macros.xml

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

Both workflows install the bot from GitHub:

```yaml
- run: pip install git+https://github.com/d-callan/gxy-tool-bot.git
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

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for guidance on the codebase structure, where to add new conventions (prompts vs. validation vs. let CI catch it), and how the generation and feedback flows are organized.

## License

MIT
