# gxy-tool-bot

Because once upon a time Björn said to Danielle: Wouldn't an Agentic Galaxy Tool Bot be cool? :smile:

So this is an agentic bot that generates [Galaxy](https://galaxyproject.org/) tool wrappers from user requests, powered by LLM APIs and orchestrated through GitHub Actions.

## Overview

`gxy-tool-bot` is a Python library + companion GitHub Actions workflows that automate the creation of Galaxy tool wrappers. It is **not a tool repo itself** — it's a library consumed by repos that house Galaxy tools.

## Current Status

This repository is a rough draft. It is untested and largely unreviewed yet. It mostly represents a plan for how I imagine the library should work, rather than a promise for how it actually works. Use at your own risk.

## How it works

1. **Request:** Users submit tool requests via a static GitHub Pages form.
2. **Plan:** An agent researches the tool (bioconda, GitHub, publications) and posts a plan as an issue comment.
3. **Review:** A maintainer reviews and approves the plan.
4. **Generate:** An agent generates the tool XML, macros, and test data, then opens a PR.

## Setup

See [PLAN.md](PLAN.md) for full documentation. Quick start:

```bash
pip install gxy-tool-bot
```

Create a `.gxy-tool-bot.yml` config file, set up the required GitHub Actions secrets, and copy the workflow templates from `workflows/`.

## License

MIT
