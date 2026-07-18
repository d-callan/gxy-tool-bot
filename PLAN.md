# gxy-tool-bot — Plan & Ideas

> An agentic bot that generates Galaxy tool wrappers from user requests, powered by LLM APIs (e.g. GLM 5.2 via OpenRouter, or an institutional endpoint) and orchestrated through GitHub Actions.

---

## Overview

`gxy-tool-bot` is a small Python library + companion GitHub Actions workflows that automate the creation of [Galaxy](https://galaxyproject.org/) tool wrappers. It is **not a tool repo itself** — it does not house any Galaxy tools, run planemo, or lint/test tool XML. Instead, it is a library consumed by other repos (via GitHub Actions) that do house tools. Those consuming repos handle planemo, linting, and testing.

The library is designed to be used by any repo that houses Galaxy tool wrappers (e.g. `galaxyproject/tools-iuc`, or a community fork) to:

1. Let people **request** new tools via a static GitHub Pages site.
2. Have an agent **plan** the tool (researching bioconda, repos, DOIs, etc.) and post the plan as an issue.
3. Have an agent **implement** the tool (XML, macros, tests, test data) and open a PR — once a maintainer approves the plan.

The human stays in the loop at the plan-approval stage; the bot handles research and boilerplate generation.

---

## Core Components

### 1. `planner` — Generate a tool plan (Markdown)

**Input:** A `ToolRequest` dataclass:

```python
@dataclass
class ToolRequest:
    tool_name: str           # e.g. "samtools sort"
    description: str         # what the user wants the tool to do
    links: list[str]         # URLs the user provided (repo, publication, etc.)
    contact: str | None      # GitHub handle or email, optional
```

**What it does:**

The planner runs a **two-phase process**: targeted web lookups (phase 1) + LLM plan generation with a tool-use loop (phase 2).

#### Phase 1: Targeted web lookups (pre-fetch)

Before calling the LLM, we proactively fetch structured data we know we'll need. This is done by dedicated lookup functions (see §"Web Lookup Functions" below) so we get reliable, parseable results rather than relying on the agent to browse:

1. **Bioconda lookup** — search for the tool on bioconda. Returns package name, latest version, channel, and a link.
2. **GitHub repo lookup** — search GitHub for the tool. Returns repo URL, description, stars, primary language, license.
3. **DOI / publication lookup** — if the user provided a DOI link, fetch metadata via CrossRef. Otherwise, search for the tool name on PubMed / Europe PMC.
4. **README fetch** — if a GitHub repo was found, fetch the raw README to understand the CLI interface.

All lookups are best-effort — failures return `None` and don't block the flow. Results are assembled into a `LookupContext` dataclass:

```python
@dataclass
class LookupContext:
    bioconda: BiocondaInfo | None
    github: GitHubRepoInfo | None
    publications: list[PublicationInfo]  # may be empty; CrossRef result wrapped in list
    readme: str | None
    raw_urls: list[str]       # any URLs the user provided that we fetched
```

#### Phase 2: LLM plan generation with tool-use loop

We call the LLM API with:
- The `ToolRequest` (user's input).
- The `LookupContext` (pre-fetched data from phase 1).
- One or two **exemplar tool XMLs** from `tools-iuc` (fetched by `exemplars.py`, see below).
- A system prompt instructing the agent to produce a structured Markdown plan following Galaxy IUC conventions.
- A set of **tool/function definitions** the agent can call if it needs more info beyond what we pre-fetched.

The tool-use loop works as follows (implemented in `agent_loop.py`):

```
1. Send messages + tool definitions to the API.
2. If the API returns tool_calls:
   a. Execute each tool call (our lookup functions or fetch_url).
   b. Append tool results to the message history.
   c. Go to step 1.
3. If the API returns a normal completion (no tool_calls):
   a. That's the final plan Markdown.
   b. Return it.
```

This is a lightweight agent harness — no external framework needed. We implement the loop ourselves using the OpenAI-compatible function-calling API. Max iterations configurable (default: 10) to prevent infinite loops.

**Tool functions available to the agent during planning:**

| Function | Description |
|----------|-------------|
| `search_bioconda(query)` | Search bioconda for a package |
| `fetch_doi_metadata(doi)` | Get publication metadata via CrossRef |
| `search_github(query)` | Search GitHub repos |
| `fetch_url(url)` | Fetch raw content of a URL (for READMEs, docs, etc.) |
| `search_pubmed(query)` | Search PubMed for publications |
| `search_web(query)` | General web search fallback (DuckDuckGo) for info not found via other tools |

**Output:** A Markdown plan document with the following structure:

```markdown
# Tool Plan: <tool_name>

## Summary
<one-paragraph synopsis>

## Underlying Software
- **Repository:** <url>
- **Version:** <version>
- **License:** <license>
- **Bioconda:** <package name, version, channel>

## Publication(s)
- <DOI> — <title> (<authors>, <year>)

## Command-Line Interface
<summary of CLI flags, inputs, outputs, with examples>

## Proposed Galaxy Tool Wrapper
### Inputs
- <name> (format: <format>, type: <data/param>)
### Outputs
- <name> (format: <format>)
### Command Template
<high-level Cheetah template sketch>
### Macros
<which macros to define/use>
### Help Section
<draft help text>
### Citations
<citation entries>

## Test Plan
- Test data: <source URLs or description>
- Test cases:
  1. <description> — input: <...>, expected: <...>

## Open Questions / Assumptions
- <any unresolved items>
```

**Key design notes:**
- The exemplar tools are configurable — the repo owner specifies which IUC tools they want the agent to mimic.
- The system prompt explicitly instructs the agent to follow Galaxy IUC conventions (naming, macros usage, test structure, help section, citations, etc.).
- The agent should cite sources (URLs) for factual claims in the plan.
- Temperature: moderate (0.4) to allow some creativity in the plan while staying grounded.
- The plan is posted as an issue comment with a hidden HTML marker `<!-- gxy-tool-bot-plan -->` at the top. This allows the generator to reliably find the plan comment later (issues may have other comments).

---

### 2. `generator` — Produce tool XML + macros + tests from a plan

**Input:** A Markdown plan document (the one produced by `planner`, or a maintainer-edited version), plus the same exemplar tools.

**What it does:**

Calls the LLM API with the plan + exemplars, using a tool-use loop. The agent is given a `write_file` tool function so it can produce multiple files:

```
1. Send plan + exemplars + tool definitions to the API.
2. If the API returns tool_calls:
   a. Execute each tool call (write_file, fetch_url for test data, etc.).
   b. Append tool results to the message history.
   c. Go to step 1.
3. If the API returns a normal completion:
   a. Collect all files written via write_file calls.
   b. Return the file set.
```

**Tool functions available to the agent during generation:**

| Function | Description |
|----------|-------------|
| `write_file(path, content)` | Write a file to the output directory |
| `fetch_url(url)` | Fetch text content of a URL (for docs, reference) |
| `download_file(url, path)` | Download a binary file directly to the output directory (for test data) |
| `search_github(query)` | Search GitHub repos — useful for verifying CLI flags, checking upstream examples |
| `search_web(query)` | General web search fallback (e.g. DuckDuckGo HTML endpoint) for info not found via other tools |

**Output:** A `GeneratedTool` dataclass:

```python
@dataclass
class GeneratedFile:
    path: str          # relative path, e.g. "macros.xml", "test-data/sample.bam"
    content: bytes     # file content (bytes to support binary test data)

@dataclass
class GeneratedTool:
    files: list[GeneratedFile]
    summary: str       # brief summary from the agent of what it produced
```

**Post-generation validation (lightweight, no planemo):**
- XML well-formedness check on all `.xml` files (using `xml.etree.ElementTree`).
- Check that all test data files referenced in `<tests>` blocks exist in the generated file set.
- Check that all macro tokens referenced in tool XML are defined in macros XML.
- These are basic sanity checks — the consuming repo's own CI (planemo, etc.) does the real validation.

**Key design notes:**
- Temperature: low (0.2) for more deterministic output.
- The generator should produce the plan Markdown alongside the tool files so reviewers can compare plan to implementation.
- Test data: the agent should prefer fetching small real datasets from public URLs when the plan specifies them, using `download_file` for binary files (BAM, FASTQ, etc.) and `write_file` for small synthetic text-based files. Keep test data small — the consuming repo can swap in larger data later.
- Multi-file tools: if the tool needs wrapper scripts (R, Python, shell), the agent should produce those via `write_file` as well. The plan should call this out so the generator knows to do it.
- The `write_file` tool function restricts paths to the output directory (no path traversal).
- `search_github` and `search_web` are provided so the agent can verify CLI usage, check upstream examples, or look up details not fully specified in the plan. These are fallbacks — the plan should contain most needed info.

---

### 3. `site` — Static HTML request form (GitHub Pages)

**What it does:**

Generates a small static HTML site (single page, no backend) with a form where people can:
- Enter the **tool name** and a **description** of what it should do.
- Provide **links** (repo, publication, bioconda, tutorial, etc.) as extra context — free-form, multiple URL fields.
- Optionally provide **contact info** (GitHub handle preferred) for follow-up.

**How submission works:**

The form creates a GitHub issue directly via the GitHub Issues API (`POST /repos/{owner}/{repo}/issues`) from the browser. A fine-grained PAT with **`issues: write` only** (no `actions: write`, no `contents: read`) is injected into `submit.js` at build time. This token can create issues and add labels but cannot trigger workflows or read repo contents. The worst someone can do with the exposed token is spam issues — which a maintainer triages anyway.

The issue is created with the `tool-request` label. This label is what triggers the planner workflow (via the `issues: [opened]` event with a label check), so no `repository_dispatch` is needed.

Flow:
1. Form submit -> `fetch()` POST to GitHub Issues API with structured body + `tool-request` label.
2. Issue creation fires the `issues: [opened]` event.
3. `on-tool-request.yml` triggers (filtered by `tool-request` label), reads the issue body, runs the planner, posts the plan as a comment, adds `plan-ready` label.

**Site generation details:**

The `site.py` module generates the following files:

```
site-output/
├── index.html        # the form page
├── style.css         # simple styling
└── submit.js         # form handling, POSTs to GitHub Issues API
```

`site.py` function signature:

```python
def generate_site(
    config: SiteConfig,
    output_dir: Path,
    issue_token: str,        # injected into submit.js at build time
) -> None
```

Where `SiteConfig` includes:

```python
@dataclass
class SiteConfig:
    title: str               # page title
    repo: str                # "owner/repo" where issues are created
    description: str | None  # optional intro text for the page
```

**Key design notes:**
- The site is purely static — no server needed.
- Styled simply with plain CSS (no framework dependency). Clean, accessible, mobile-friendly.
- Client-side validation: required fields (tool name + description), URL format validation for link fields.
- The form includes a honeypot field for basic spam prevention.
- The generated site is deployed to GitHub Pages via the `deploy-site.yml` workflow.

---

### 4. GitHub Actions Workflows (canned / templates)

The repo ships ready-to-use workflow files that a consuming repo can drop into `.github/workflows/`. These are templates — the consuming repo copies them and adjusts the config file path / Python version as needed.

#### 4a. `deploy-site.yml` — Deploy the request form to GitHub Pages

**Trigger:**
- Push to `main` that changes `.gxy-tool-bot.yml` or the `gxy-tool-bot` package version.
- Manual `workflow_dispatch`.

**Steps:**
```yaml
on:
  push:
    branches: [main]
    paths:
      - '.gxy-tool-bot.yml'
      - 'pyproject.toml'
  workflow_dispatch:

jobs:
  deploy-site:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install gxy-tool-bot
      - name: Generate site
        env:
          GXY_ISSUE_TOKEN: ${{ secrets.GXY_ISSUE_TOKEN }}
        run: gxy-tool-bot generate-site --config .gxy-tool-bot.yml --output site-output
      - name: Deploy to GitHub Pages
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./site-output
```

#### 4b. `on-tool-request.yml` — Plan a newly requested tool

**Trigger:** `issues` event with `opened` action, filtered by `tool-request` label.

The form creates the issue directly (with `tool-request` label). The issue `opened` event triggers this workflow.

**Steps:**
1. Read the issue body for tool name, description, links, contact.
2. Install `gxy-tool-bot`.
3. Run the planner: `gxy-tool-bot plan --issue $ISSUE_NUMBER --config .gxy-tool-bot.yml`.
   - This reads the issue body, runs web lookups, calls the LLM API, and posts the plan as a comment on the issue (with a hidden `<!-- gxy-tool-bot-plan -->` marker for later retrieval).
4. Add `plan-ready` label to the issue.
5. The maintainer reviews the plan, edits if needed (by editing the issue comment), and when satisfied changes the label to `ready-to-implement`.

```yaml
on:
  issues:
    types: [opened]

jobs:
  plan:
    if: contains(github.event.issue.labels.*.name, 'tool-request')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install gxy-tool-bot
      - name: Generate plan
        env:
          GXY_TOOL_BOT_API_KEY: ${{ secrets.GXY_TOOL_BOT_API_KEY }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gxy-tool-bot plan \
            --issue ${{ github.event.issue.number }} \
            --config .gxy-tool-bot.yml
      - name: Add plan-ready label
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh issue edit ${{ github.event.issue.number }} \
            --add-label "plan-ready"
```

#### 4c. `on-ready-to-implement.yml` — Generate the tool and open a PR

**Trigger:** `issues` event with `labeled` action, where label is `ready-to-implement`.

**Steps:**
1. Fetch the plan from the issue — find the comment containing the `<!-- gxy-tool-bot-plan -->` marker (the maintainer may have edited this comment, so read the current version).
2. Install `gxy-tool-bot`.
3. Run the generator: `gxy-tool-bot generate --issue $ISSUE_NUMBER --config .gxy-tool-bot.yml --output generated/`.
4. Create a new branch (`tool-bot/issue-$N`), commit generated files, push.
5. Open a PR with `Closes #N` in the body, plus the agent's summary.
6. Comment on the issue with a link to the PR.
7. Add `pr-opened` label to the issue.

```yaml
on:
  issues:
    types: [labeled]

jobs:
  generate:
    if: github.event.label.name == 'ready-to-implement'
    runs-on: ubuntu-latest
    # Note: if allowed_maintainers is configured, the CLI should verify
    # github.event.actor is in the list and exit with code 1 if not.
    # The workflow's if condition can't easily check a config list, so
    # enforcement is done in the generate step itself.
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install gxy-tool-bot
      - name: Generate tool files
        id: generate
        env:
          GXY_TOOL_BOT_API_KEY: ${{ secrets.GXY_TOOL_BOT_API_KEY }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gxy-tool-bot generate \
            --issue ${{ github.event.issue.number }} \
            --config .gxy-tool-bot.yml \
            --output generated/
      - name: Create branch and PR
        if: steps.generate.outcome == 'success'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          BRANCH="tool-bot/issue-${{ github.event.issue.number }}"
          git checkout -b "$BRANCH"
          cp -r generated/* .
          git add -A
          git commit -m "Generate tool for issue #${{ github.event.issue.number }}

          Closes #${{ github.event.issue.number }}"
          git push origin "$BRANCH"
          gh pr create \
            --head "$BRANCH" \
            --base main \
            --title "Tool: <auto-generated> (issue #${{ github.event.issue.number }})" \
            --body "Generated by gxy-tool-bot for issue #${{ github.event.issue.number }}"
          gh issue edit ${{ github.event.issue.number }} --add-label "pr-opened"
      - name: Handle generation failure
        if: steps.generate.outcome == 'failure'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh issue edit ${{ github.event.issue.number }} --add-label "generation-failed"
```

#### Workflow diagram

```
User submits form (GitHub Pages)
        │
        ▼  (POST to GitHub Issues API)
  Issue created (label: tool-request)
        │
        ▼  (issues: opened event)
  [on-tool-request.yml]
        │
        └──► planner runs ──► plan posted as issue comment
                                    │
                                    ▼
                          (label: plan-ready)
                                    │
                                    ▼
                    Maintainer reviews / edits plan
                                    │
                                    ▼
                    Maintainer sets label: ready-to-implement
                                    │
                                    ▼
                    [on-ready-to-implement.yml]
                                    │
                                    ├──► generator runs
                                    │
                                    ├──► PR opened with tool files
                                    │
                                    └──► Issue labelled pr-opened, PR linked
```

---

## Configuration

A consuming repo provides a config file `.gxy-tool-bot.yml` at repo root:

```yaml
# .gxy-tool-bot.yml — example

api:
  base_url: "https://openrouter.ai/api/v1"   # OpenAI-compatible endpoint
  model: "z-ai/glm-4.5"                       # model slug
  # API key is read from env: GXY_TOOL_BOT_API_KEY
  # For institutional endpoints, just change base_url and model
  max_tool_iterations: 10                      # max tool-use loop iterations
  temperature_plan: 0.4                        # planner temperature
  temperature_generate: 0.2                    # generator temperature
  max_context_chars: 100000                    # prompt size budget before truncation

exemplars:
  # Paths to exemplar tool XMLs. Can be local paths or GitHub URLs.
  # These are fetched at runtime and included in the prompt.
  - url: "https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/samtools_bamtofastq/samtools_bamtofastq.xml"
    macros: "https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/samtools_bamtofastq/macros.xml"
  - url: "https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/fastp/fastp.xml"
    macros: "https://raw.githubusercontent.com/galaxyproject/tools-iuc/main/tools/fastp/macros.xml"

site:
  title: "Galaxy Tool Request"
  repo: "d-callan/some-tool-repo"   # where issues are created via the form
  description: "Request a new Galaxy tool wrapper"
  # issue token is read from env: GXY_ISSUE_TOKEN (issues:write only PAT)

labels:
  request: "tool-request"
  plan_ready: "plan-ready"
  ready_to_implement: "ready-to-implement"
  pr_opened: "pr-opened"
  generation_failed: "generation-failed"

# Optional: restrict which GitHub users can trigger implementation
# (if not set, any maintainer who can label issues can proceed)
# allowed_maintainers:
#   - d-callan
#   - some-other-user
```

The config is loaded by `config.py`:

```python
@dataclass
class ApiConfig:
    base_url: str
    model: str
    max_tool_iterations: int = 10
    temperature_plan: float = 0.4
    temperature_generate: float = 0.2
    max_context_chars: int = 100_000  # prompt size budget before truncation

@dataclass
class ExemplarConfig:
    url: str
    macros: str | None = None

@dataclass
class SiteConfig:
    title: str
    repo: str
    description: str | None = None

@dataclass
class LabelConfig:
    request: str = "tool-request"
    plan_ready: str = "plan-ready"
    ready_to_implement: str = "ready-to-implement"
    pr_opened: str = "pr-opened"
    generation_failed: str = "generation-failed"

@dataclass
class BotConfig:
    api: ApiConfig
    exemplars: list[ExemplarConfig]
    site: SiteConfig
    labels: LabelConfig
    allowed_maintainers: list[str] | None = None

def load_config(path: Path) -> BotConfig:
    """Load and validate .gxy-tool-bot.yml"""
    ...
```

---

## Python Library Structure

```
gxy-tool-bot/
├── gxy_tool_bot/
│   ├── __init__.py
│   ├── config.py              # load & validate .gxy-tool-bot.yml
│   ├── api_client.py          # OpenAI-compatible API client (chat completions + tool calling)
│   ├── agent_loop.py          # generic tool-use loop (send -> tool_calls -> execute -> repeat)
│   ├── planner.py             # plan generation: pre-fetch lookups + agent loop + plan formatting
│   ├── generator.py           # tool generation: agent loop with write_file tool + validation
│   ├── site.py                # static HTML site generation
│   ├── exemplars.py           # fetch & cache exemplar tool XMLs from GitHub URLs
│   ├── lookups/
│   │   ├── __init__.py        # re-exports all lookup functions
│   │   ├── bioconda.py        # search_bioconda(query) -> BiocondaInfo | None
│   │   ├── github.py          # search_github(query) -> GitHubRepoInfo | None
│   │   ├── doi.py             # fetch_doi_metadata(doi) -> PublicationInfo | None
│   │   ├── pubmed.py          # search_pubmed(query) -> list[PublicationInfo]
│   │   ├── fetch.py           # fetch_url(url) -> str + download_file(url, path) (SSRF protected)
│   │   └── web.py             # search_web(query) -> list[SearchResult] (DuckDuckGo HTML fallback)
│   ├── github_client.py       # GitHub API client (create issues, add labels, get comments)
│   ├── cli.py                 # CLI entry points: plan, generate, generate-site
│   └── templates/             # prompt templates (Jinja2)
│       ├── planner_system.txt
│       ├── planner_user.txt
│       ├── generator_system.txt
│       └── generator_user.txt
├── workflows/                 # canned GitHub Actions workflow templates
│   ├── deploy-site.yml
│   ├── on-tool-request.yml
│   └── on-ready-to-implement.yml
├── tests/
│   ├── test_config.py
│   ├── test_api_client.py
│   ├── test_agent_loop.py
│   ├── test_planner.py
│   ├── test_generator.py
│   ├── test_site.py
│   ├── test_lookups.py
│   ├── test_github_client.py
│   └── fixtures/              # sample tool XMLs, sample plans, mock API responses
├── pyproject.toml
├── PLAN.md                    # this file
└── README.md
```

### Module Details

#### `api_client.py`

Thin wrapper around the OpenAI-compatible chat completions endpoint. Uses `httpx` for HTTP calls.

```python
class ApiClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        ...

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.4,
    ) -> ChatResponse:
        """Send a chat completion request. Returns message + tool_calls."""
        ...

@dataclass
class ChatResponse:
    content: str | None              # text content (None if only tool_calls)
    tool_calls: list[ToolCall] | None
    finish_reason: str               # "stop", "tool_calls", "length"

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict                  # parsed JSON arguments
```

Dependencies: `httpx`

#### `agent_loop.py`

The core agent harness — a simple tool-use loop with no external framework.

```python
def run_agent_loop(
    client: ApiClient,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolDefinition],
    max_iterations: int = 10,
    temperature: float = 0.4,
) -> AgentResult:
    """
    Run a tool-use loop:
    1. Send messages + tool definitions.
    2. If response has tool_calls, execute them and append results.
    3. Repeat until response has no tool_calls or max_iterations reached.
    4. Return final content + trace of all tool calls made.
    """
    ...

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict          # JSON schema for parameters
    handler: Callable[[dict], str]  # function that executes the tool call

@dataclass
class AgentResult:
    content: str              # final text output from the agent
    tool_call_trace: list[dict]  # log of all tool calls + results
    iterations: int           # how many loop iterations occurred
```

This is the "agent harness" — it's ~50 lines of logic. No need for LangChain, AutoGen, or any other framework. The OpenAI-compatible function-calling API is the harness; we just implement the loop.

#### `planner.py`

```python
def generate_plan(
    request: ToolRequest,
    config: BotConfig,
    api_key: str,
) -> str:
    """
    Full planning pipeline:
    1. Run targeted web lookups (bioconda, github, doi, pubmed).
    2. Fetch exemplar tool XMLs.
    3. Build system + user prompts (from templates).
    4. Run agent loop with lookup tool functions available.
    5. Return the plan Markdown.
    """
    ...
```

#### `generator.py`

```python
def generate_tool(
    plan_markdown: str,
    config: BotConfig,
    api_key: str,
    output_dir: Path,
) -> GeneratedTool:
    """
    Full generation pipeline:
    1. Fetch exemplar tool XMLs.
    2. Build system + user prompts (from templates).
    3. Run agent loop with write_file, fetch_url, download_file, search_github, search_web tools.
    4. Validate generated files (XML well-formedness, test data refs).
    5. Return GeneratedTool with all files.
    """
    ...
```

#### `exemplars.py`

```python
def fetch_exemplars(config: list[ExemplarConfig]) -> list[Exemplar]:
    """
    Fetch exemplar tool XMLs (and optional macros) from GitHub raw URLs.
    Caches to a temp directory to avoid re-fetching within a single run.
    """
    ...

@dataclass
class Exemplar:
    tool_xml: str             # raw XML content
    macros_xml: str | None    # raw macros XML content, if provided
    name: str                 # derived from URL or config
```

#### `lookups/bioconda.py`

```python
@dataclass
class BiocondaInfo:
    package_name: str
    version: str
    channel: str              # e.g. "bioconda"
    url: str                  # anaconda.org link

def search_bioconda(query: str) -> BiocondaInfo | None:
    """
    Search bioconda via the anaconda.org API:
    GET https://api.anaconda.org/search?name=<query>
    Parse results, prefer bioconda channel matches.
    """
    ...
```

#### `lookups/github.py`

```python
@dataclass
class GitHubRepoInfo:
    full_name: str            # "owner/repo"
    url: str
    description: str
    stars: int
    language: str
    license: str | None

def search_github(query: str, token: str | None = None) -> GitHubRepoInfo | None:
    """
    Search GitHub via REST API:
    GET https://api.github.com/search/repositories?q=<query>&sort=stars
    Return top result. Token optional but helps with rate limits.
    """
    ...
```

#### `lookups/doi.py`

```python
@dataclass
class PublicationInfo:
    doi: str | None
    title: str
    authors: list[str]
    year: int
    journal: str
    url: str

def fetch_doi_metadata(doi: str) -> PublicationInfo | None:
    """
    Fetch publication metadata via CrossRef API:
    GET https://api.crossref.org/works/<doi>
    """
    ...
```

#### `lookups/pubmed.py`

```python
def search_pubmed(query: str, max_results: int = 3) -> list[PublicationInfo]:
    """
    Search PubMed via E-utilities API:
    GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=<query>
    Then fetch summaries via esummary.fcgi.
    """
    ...
```

#### `lookups/fetch.py`

```python
def fetch_url(url: str, max_bytes: int = 500_000) -> str:
    """
    Fetch raw content of a URL. Truncates at max_bytes.
    Used for READMEs, documentation pages, etc.
    SSRF protected: only http/https schemes, blocks private IP ranges
    (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x), rejects localhost.
    """
    ...

def download_file(url: str, dest_path: str, max_bytes: int = 10_000_000) -> str:
    """
    Download a binary file directly to the output directory.
    Used for test data (BAM, FASTQ, etc.). Same SSRF protection as fetch_url.
    dest_path is validated to be within the output directory (no path traversal).
    If the file exceeds max_bytes, abort the download and return an error string
    to the agent (does not write a partial file).
    Returns the path the file was saved to.
    """
    ...
```

#### `lookups/web.py`

```python
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

def search_web(query: str, max_results: int = 5) -> list[SearchResult]:
    """
    General web search fallback using DuckDuckGo HTML endpoint:
    GET https://html.duckduckgo.com/html/?q={query}
    Parses result titles, URLs, and snippets from the HTML.
    No API key required. Sends a User-Agent header to avoid being blocked.
    Best-effort: DuckDuckGo's HTML format may change without notice;
    returns empty list on parse failure. Same SSRF protection applied to
    result URLs when subsequently fetched via fetch_url.
    """
    ...
```

#### `github_client.py`

```python
class GitHubClient:
    def __init__(self, token: str, repo: str):
        ...

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        """Create an issue, return issue number."""
        ...

    def add_comment(self, issue_number: int, body: str) -> None:
        """Add a comment to an issue."""
        ...

    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue."""
        ...

    def get_issue(self, issue_number: int) -> Issue:
        """Fetch issue details (title, body, labels)."""
        ...

    def get_issue_comments(self, issue_number: int) -> list[Comment]:
        """Fetch all comments on an issue."""
        ...

@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    author: str

@dataclass
class Comment:
    id: int
    body: str
    author: str
```

This uses the GitHub REST API directly via `httpx`. Could also use `PyGithub` but direct API calls keep dependencies lighter and give us more control. Branch/PR creation is handled by git CLI in the workflow directly, so `GitHubClient` focuses on issue operations only.

#### `cli.py`

```python
# CLI commands (using click or typer):

gxy-tool-bot plan --issue <N> --config <path>
  # Reads issue, runs planner, posts plan as comment, adds plan-ready label
  # Requires env: GXY_TOOL_BOT_API_KEY, GH_TOKEN

gxy-tool-bot generate --issue <N> --config <path> --output <dir>
  # Reads plan from issue, runs generator, writes files to output dir
  # Requires env: GXY_TOOL_BOT_API_KEY, GH_TOKEN

gxy-tool-bot generate-site --config <path> --output <dir>
  # Generates static HTML site
  # Requires env: GXY_ISSUE_TOKEN (fine-grained PAT with issues:write)
```

#### `templates/` — Prompt templates

**`planner_system.txt`** — System prompt for the planner agent. Key contents:
- Role: "You are an expert Galaxy tool wrapper developer. You follow IUC conventions."
- Instructions to use provided lookup data and tool functions for research.
- Required output format (the Markdown plan structure shown above).
- Instructions to cite sources.
- Instructions to be thorough about CLI interface, inputs, outputs, formats.

**`planner_user.txt`** — User prompt template (Jinja2):
- Tool request details (name, description, links, contact).
- Pre-fetched lookup context (bioconda, github, doi, readme).
- Exemplar tool XMLs (inline).
- "Produce a plan following the format specified in the system prompt."

**`generator_system.txt`** — System prompt for the generator agent. Key contents:
- Role: "You are an expert Galaxy tool wrapper developer. You produce valid, complete tool XML."
- Instructions to use `write_file` to produce each file.
- Instructions to follow the plan closely.
- Instructions to follow IUC conventions (macros, tests, help, citations).
- Instructions to keep test data small.

**`generator_user.txt`** — User prompt template:
- The plan Markdown.
- Exemplar tool XMLs (inline).
- "Generate the tool files following the plan. Use write_file for each file."

---

## Web Lookup Functions — Detail

> **This is the single source of truth for lookup function behavior.** The module detail sections above show dataclass definitions and signatures; full behavior is documented here.

All lookup functions are in `gxy_tool_bot/lookups/`. They use `httpx` for HTTP and return dataclasses or `None`.

### `search_bioconda(query: str) -> BiocondaInfo | None`
- **API:** `GET https://api.anaconda.org/search?name={query}`
- Parses JSON response, filters for `channel_name == "bioconda"`, takes top result by downloads.
- Returns package name, latest version, channel, anaconda.org URL.

### `search_github(query: str, token: str | None) -> GitHubRepoInfo | None`
- **API:** `GET https://api.github.com/search/repositories?q={query}&sort=stars&order=desc`
- Takes top result. Returns full_name, url, description, stars, language, license.
- Token is optional (from `GITHUB_TOKEN` env or config) — helps with rate limits (60/hr unauthenticated vs 5000/hr authenticated).

### `fetch_doi_metadata(doi: str) -> PublicationInfo | None`
- **API:** `GET https://api.crossref.org/works/{doi}`
- Parses CrossRef JSON. Returns DOI, title, authors, year, journal, URL.
- Handles DOIs with or without `https://doi.org/` prefix.

### `search_pubmed(query: str, max_results: int = 3) -> list[PublicationInfo]`
- **API:** Two-step via NCBI E-utilities:
  1. `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={query}&retmax={max_results}`
  2. `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={ids}`
- Returns list of PublicationInfo (DOI may be None if not in PubMed summary).

### `fetch_url(url: str, max_bytes: int = 500_000) -> str`
- Generic URL fetcher. Used by the agent (via `fetch_url` tool function) and internally for READMEs.
- Truncates at `max_bytes` to prevent downloading huge files.
- Follows redirects. Only accepts `text/*` and `application/json` content types (skips binary).
- **SSRF protection:** Only `http`/`https` schemes allowed. Blocks private IP ranges (`10.x`, `172.16-31.x`, `192.168.x`, `127.x`, `169.254.x`) and `localhost`. Resolves DNS before connecting and checks the resolved IP.

### `search_web(query: str, max_results: int = 5) -> list[SearchResult]`
- General web search fallback using DuckDuckGo HTML endpoint (no API key needed).
- Sends a `User-Agent` header to avoid being blocked.
- Parses result titles, URLs, and snippets from HTML.
- Returns `SearchResult` dataclass (`title`, `url`, `snippet`).
- Best-effort: DuckDuckGo's HTML format may change without notice; returns empty list on parse failure.
- Available to both planner and generator agents as a last-resort lookup when targeted APIs don't have what's needed.

---

## Dependencies

### Runtime dependencies (`pyproject.toml`)

| Package | Version | Purpose |
|---------|---------|---------|
| `httpx` | `>=0.27` | HTTP client for API calls (LLM, GitHub, bioconda, CrossRef, PubMed) |
| `pyyaml` | `>=6.0` | Parse `.gxy-tool-bot.yml` config |
| `click` | `>=8.1` | CLI framework |
| `jinja2` | `>=3.1` | Prompt template rendering |

That's it. Four runtime dependencies. No LangChain, no AutoGen, no PyGithub, no planemo. The agent loop is ~50 lines of our own code. The GitHub client is a thin `httpx` wrapper. Everything else is stdlib.

### Dev dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | `>=8.0` | Testing |
| `pytest-mock` | `>=3.12` | Mocking HTTP calls in tests |
| `respx` | `>=0.21` | Mock `httpx` responses in tests |
| `ruff` | `>=0.5` | Linting + formatting |

### Python version

Python 3.12+ (uses `type | None` syntax, dataclass slots, etc.).

---

## Error Handling & Timeouts

### HTTP timeouts
All `httpx` calls use explicit timeouts:
- Lookup functions (bioconda, GitHub, CrossRef, PubMed, DuckDuckGo): **30s** connect + read.
- `fetch_url` / `download_file`: **30s** connect, **60s** read (large files).
- LLM API (`ApiClient.chat`): **30s** connect, **120s** read (large completions can be slow).

### Retry strategy
- Transient HTTP errors (5xx, connection errors, timeouts): retry up to **3 times** with exponential backoff (1s, 2s, 4s). Implemented via a small `retry` helper wrapping `httpx` calls.
- 4xx errors (auth failures, bad requests): no retry, raise immediately with a clear error message.
- LLM API errors: same retry strategy. If all retries fail, the CLI command exits with a non-zero code and posts an error comment on the issue explaining what went wrong.

### Agent loop failure modes
- **Max iterations reached:** return current content (if any) + append a warning to the output: "⚠️ Agent did not naturally terminate after N iterations. Output may be incomplete." Post as-is to the issue so the maintainer can decide.
- **Tool execution error:** if a tool function raises an exception, catch it, return an error string to the agent (e.g. `"Error: connection timed out"`), and let the agent decide whether to retry or proceed without that data. Don't crash the loop.
- **API returns empty/malformed response:** treat as a tool-call with no content, log a warning, and continue.

### Validation failures (generator)
If post-generation validation fails (malformed XML, missing test data refs, undefined macros):
- Don't create a PR.
- Post an error comment on the issue listing the specific validation failures.
- Add a `generation-failed` label so the maintainer knows to investigate.

### CLI exit codes
- `0`: success.
- `1`: configuration error (missing config file, invalid YAML, missing env vars).
- `2`: API error (LLM or lookup API failures after retries).
- `3`: validation error (generated files failed sanity checks).
- `4`: agent loop did not terminate naturally (partial output).

---

## Context Window Management

Exemplar XMLs + lookup context + plan can be large. Strategy to stay within model limits:

- **Exemplars:** truncate each exemplar tool XML to **8,000 characters** (configurable). If truncated, append `<!-- ... truncated ... -->` so the agent knows. Most IUC tools are under this limit; a few very large ones (e.g. `bcftools_filter`) may need it.
- **READMEs:** truncate to **4,000 characters**. Summarize if longer — the agent can `fetch_url` specific sections if needed.
- **Lookup context:** dataclasses are small (a few hundred chars each), no truncation needed.
- **Plan Markdown (for generator):** plans should be under ~4,000 words. If a plan is excessively long, the generator prompt may need to split it — but this is unlikely for single-tool plans.
- **Total prompt budget:** before sending, estimate total token count (rough: chars / 4). If over a configurable threshold (default: 100,000 chars ≈ 25K tokens), log a warning and truncate the largest sections first (exemplars, then README).
- **Model context limit:** configurable in `ApiConfig` as `max_context_chars` (default: 100,000). The planner/generator check prompt size before sending and truncate as needed.

---

## Logging & Observability

All CLI commands log to **stdout** (visible in GitHub Actions logs) using Python's `logging` module:

- **DEBUG:** full prompts sent to API, raw tool call arguments, raw API responses.
- **INFO:** high-level progress (e.g. "Fetching bioconda info for samtools...", "Agent iteration 3/10", "Plan posted to issue #42").
- **WARNING:** lookups that returned None, truncated content, max iterations reached.
- **ERROR:** API failures, validation failures.

Default level is INFO. Set via `--verbose` (DEBUG) or `--quiet` (WARNING) CLI flags.

Additionally, the `AgentResult.tool_call_trace` is logged at DEBUG level and included in a collapsible `<details>` block in the issue comment (so it's available but not visually noisy). This lets maintainers see exactly what the agent searched for and what it got back, which is critical for debugging poor plans or generations.

---

## Consuming Repo Setup

A consuming repo must do the following one-time setup:

### 1. Create labels
The five labels must exist in the repo before the bot is used. GitHub silently fails when adding a non-existent label. Create them via `gh` CLI:

```bash
gh label create "tool-request" --description "New tool request from the form" --color "0075ca"
gh label create "plan-ready" --description "Agent has generated a plan, ready for review" --color "0e8a16"
gh label create "ready-to-implement" --description "Maintainer approved the plan, ready for generation" --color "5319e7"
gh label create "pr-opened" --description "Tool files generated and PR opened" --color "1f883d"
gh label create "generation-failed" --description "Tool generation failed validation" --color "d73a4a"
```

Label names are configurable in `.gxy-tool-bot.yml`, but these are the defaults.

### 2. Create secrets
The consuming repo needs these GitHub Actions secrets:
- `GXY_TOOL_BOT_API_KEY` — API key for the LLM provider (OpenRouter, institutional, etc.).
- `GXY_ISSUE_TOKEN` — fine-grained PAT with `issues: write` only on the repo. Used by the static site form to create issues.
- `GITHUB_TOKEN` — automatically provided by GitHub Actions, no setup needed.

### 3. Copy workflow files
Copy the three workflow templates from `workflows/` into `.github/workflows/`:
- `deploy-site.yml`
- `on-tool-request.yml`
- `on-ready-to-implement.yml`

### 4. Create config file
Create `.gxy-tool-bot.yml` at repo root (see Configuration section above for the full example).

### 5. Enable GitHub Pages
In repo Settings → Pages, set source to "GitHub Actions" (the `deploy-site.yml` workflow handles deployment).

---

## Open Questions / Decisions (Resolved)

- **API provider:** Start with OpenAI-compatible client. Covers OpenRouter and most institutional endpoints. Just change `base_url` and `model` in config. Resolved.
- **Web access for the agent:** Two-tier approach. Phase 1: we pre-fetch known data (bioconda, GitHub, DOI, PubMed) via dedicated lookup functions. Phase 2: the agent gets `fetch_url`, `search_bioconda`, `search_github`, `fetch_doi_metadata`, `search_pubmed`, and `search_web` (DuckDuckGo fallback) as tool functions it can call in the tool-use loop if it needs more. No external agent harness — we implement the loop ourselves (~50 lines). Resolved.
- **Planemo / linting / testing:** Not in scope for this repo. This is a library consumed by tool repos. Those repos have their own CI with planemo. We do only basic XML well-formedness checks. Resolved.
- **Rate limits / cost:** Not a concern for now. Target is institutional model (free/near-free). Testing via OpenRouter is fine for a few runs. Resolved.
- **Token security for the static site:** Form creates issues directly via GitHub Issues API using a fine-grained PAT with `issues: write` only. Token is embedded in client-side JS at build time — worst case is spam issues, not workflow triggers or repo access. No `repository_dispatch` needed. Resolved.
- **Test data size:** Keep generated test data small. Agent should fetch small real datasets or generate tiny synthetic files. Consuming repo can swap in larger data. Resolved.
- **Multi-file tools:** Generator handles via `write_file` tool function. Plan should call out if wrapper scripts are needed. Resolved.
- **Idempotency / re-runs:** If branch already exists (re-run), update it rather than creating a new PR. Resolved.

---

## Milestones

1. **M1 — Library skeleton + config + API client + agent loop.**
   - `pyproject.toml`, package structure, `config.py`, `api_client.py`, `agent_loop.py`.
   - Unit tests for config loading and agent loop (mock API responses).
   - Verify agent loop works with OpenRouter (manual test).

2. **M2 — Web lookups + exemplars.**
   - All `lookups/*.py` modules.
   - `exemplars.py`.
   - Unit tests with mocked HTTP (`respx`).
   - Verify lookups work against real APIs (manual test).

3. **M3 — Planner.**
   - `planner.py`, prompt templates.
   - End-to-end: given a `ToolRequest`, produce a Markdown plan.
   - Test with 2-3 real tools (e.g. samtools sort, fastp, bcftools filter).
   - Verify plan quality manually.

4. **M4 — Generator.**
   - `generator.py`, prompt templates.
   - End-to-end: given a plan Markdown, produce XML + macros + tests.
   - Basic validation (XML well-formedness, test data refs).
   - Test with the plans from M3.

5. **M5 — Static site + GitHub client.**
   - `site.py`, `github_client.py`.
   - Generate the request form.
   - Verify issue creation via `github_client.py` (manual test in a test repo).

6. **M6 — CLI + Actions workflows.**
   - `cli.py` with all commands.
   - Three canned workflow templates in `workflows/`.
   - End-to-end test in a real repo: form -> issue -> plan -> label -> PR.

7. **M7 — Polish.**
   - README, edge cases, additional tests.
   - Implement error handling & timeout strategy (see Error Handling section).
   - Implement logging & observability (see Logging section).
   - PyPI publish (optional).
