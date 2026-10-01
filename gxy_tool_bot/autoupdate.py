"""Scheduled autoupdate flow: detect outdated tool versions and open PRs.

Unlike the issue-driven update flow, this runs unattended on a schedule —
no issue and no plan-approval step. A tool dir qualifies when conda reports
a newer version of the tool's main requirement package; an agent then
implements the bump and adapts the wrapper for upstream changes.

Dedup rules are modeled on planemo-autoupdate:
- an open PR on the tool's autoupdate branch → skip;
- a closed, unmerged autoupdate PR → only proceed when the detected version
  is newer than the one that PR targeted (closing without deleting the
  branch is how maintainers decline a particular version);
- a branch whose last commit wasn't authored by the bot → never clobber.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from gxy_tool_bot.address_feedback import update_tool
from gxy_tool_bot.config import BotConfig

logger = logging.getLogger(__name__)

ANACONDA_PACKAGE_API = "https://api.anaconda.org/package/{channel}/{package}"

# Commits on autoupdate branches are authored under this name by the
# workflow; a different last author means a human edited the branch and the
# bot must not overwrite it.
BOT_AUTHOR = "gxy-tool-bot"

BRANCH_PREFIX = "tool-bot/autoupdate-"

_REQUIREMENT_RE = re.compile(r'<requirement[^>]*\bversion="([^"]+)"[^>]*>\s*([^<]+?)\s*<')
_TOKEN_DEF_RE = re.compile(r'<token\s+name="(@[A-Za-z_]+@)"[^>]*>\s*([^<]+?)\s*<')
# Macros may carry versions as attributes, e.g. token_tool_version="1.4".
_TOKEN_ATTR_RE = re.compile(r"\btoken_([a-z_]+)=\"([^\"]+)\"")


@dataclass
class OutdatedTool:
    """A tool directory whose main requirement is behind the latest conda release."""

    tool_dir: str  # directory name under tools/
    package: str   # main conda requirement name
    current: str
    latest: str
    channel: str
    # Other outdated requirements: (package, current, latest)
    others: list[tuple[str, str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Version parsing and detection
# ---------------------------------------------------------------------------

def _load_tokens(tool_dir: Path) -> dict[str, str]:
    """Collect @TOKEN@ definitions from the dir's XML files (e.g. macros.xml)."""
    tokens: dict[str, str] = {}
    for xml_file in tool_dir.glob("*.xml"):
        text = xml_file.read_text(encoding="utf-8", errors="replace")
        for name, value in _TOKEN_DEF_RE.findall(text):
            if value.strip():
                tokens[name.lower()] = value.strip()
        for attr, value in _TOKEN_ATTR_RE.findall(text):
            # token_tool_version="X" (an attr on <tool>) defines @TOOL_VERSION@
            tokens.setdefault(f"@{attr}@", value.strip())
    return tokens


def _collect_requirements(tool_dir: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return (package, raw version spec) pairs from the dir's XMLs + tokens."""
    tokens = _load_tokens(tool_dir)
    reqs: list[tuple[str, str]] = []
    for xml_file in sorted(tool_dir.glob("*.xml")):
        text = xml_file.read_text(encoding="utf-8", errors="replace")
        for version_spec, package in _REQUIREMENT_RE.findall(text):
            reqs.append((package.strip(), version_spec.strip()))
    return reqs, tokens


def _resolve_version(spec: str, tokens: dict[str, str]) -> str | None:
    if spec.startswith("@"):
        return tokens.get(spec.lower())
    return spec


def _main_requirement(
    tool_dir_name: str,
    reqs: list[tuple[str, str]],
    tokens: dict[str, str],
) -> tuple[str, str] | None:
    """Pick the tool's main requirement: the one bound to @TOOL_VERSION@,
    else the one matching the dir name, else None (the tool is skipped —
    planemo-autoupdate likewise ignores tools it can't identify)."""
    for package, spec in reqs:
        if spec.lower() == "@tool_version@":
            version = _resolve_version(spec, tokens)
            if version:
                return package, version
    for package, spec in reqs:
        if package == tool_dir_name:
            version = _resolve_version(spec, tokens)
            if version:
                return package, version
    return None


def _version_key(version: str) -> tuple:
    """Rough version ordering key: numeric segments compare numerically."""
    key: list[tuple[int, int, str]] = []
    for part in re.split(r"[.\-_+~]", version):
        key.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return tuple(key)


def is_newer(latest: str, current: str) -> bool:
    if latest == current:
        return False
    try:
        return _version_key(latest) > _version_key(current)
    except (TypeError, ValueError):
        return True


def latest_package_version(
    package: str,
    channels: list[str],
    client: httpx.Client,
) -> tuple[str | None, str | None]:
    """Newest version of ``package`` across the given conda channels."""
    best_version: str | None = None
    best_channel: str | None = None
    for channel in channels:
        resp = client.get(ANACONDA_PACKAGE_API.format(channel=channel, package=package))
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        version = resp.json().get("latest_version")
        if version and (best_version is None or _version_key(version) > _version_key(best_version)):
            best_version, best_channel = version, channel
    return best_version, best_channel


def skip_dirs(config: BotConfig, base_dir: Path = Path(".")) -> set[str]:
    """Tool dirs to never autoupdate, from config's inline list and/or file."""
    entries = list(config.autoupdate.skip)
    if config.autoupdate.skip_file:
        path = base_dir / config.autoupdate.skip_file
        if path.is_file():
            entries += [
                line.strip() for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    return {_skip_entry_to_dir(e) for e in entries}


def _skip_entry_to_dir(entry: str) -> str:
    """Normalize a skip-list entry to a tool dir name.

    Accepts ``seqtk``, ``tools/seqtk``, ``seqtk/seqtk.xml`` or
    ``tools/seqtk/seqtk.xml`` — the latter two mirror planemo-autoupdate's
    skip lists, which name tool XML paths.
    """
    e = entry.strip().strip("/")
    if e.startswith("tools/"):
        e = e[len("tools/"):]
    if "/" in e:
        e = Path(e).parent.name if e.endswith(".xml") else e.split("/")[0]
    elif e.endswith(".xml"):
        e = Path(e).stem
    return e


def check_tool_dir(
    tool_dir: Path,
    config: BotConfig,
    client: httpx.Client,
) -> OutdatedTool | None:
    """Check one tools/<dir> for a newer version of its main requirement."""
    reqs, tokens = _collect_requirements(tool_dir)
    main = _main_requirement(tool_dir.name, reqs, tokens)
    if not main:
        return None
    package, current = main
    latest, channel = latest_package_version(package, config.autoupdate.channels, client)
    if not latest or not is_newer(latest, current):
        logger.info("%s: %s is current (%s)", tool_dir.name, package, current)
        return None
    # Only worth checking secondary requirements once we know the tool is
    # outdated — avoids a dirs × requirements fan-out of anaconda calls.
    others: list[tuple[str, str, str]] = []
    seen = {(package, current)}
    for pkg, spec in reqs:
        if pkg == package:
            continue
        resolved = _resolve_version(spec, tokens)
        if not resolved or (pkg, resolved) in seen:
            continue
        seen.add((pkg, resolved))
        o_latest, _ = latest_package_version(pkg, config.autoupdate.channels, client)
        if o_latest and is_newer(o_latest, resolved):
            others.append((pkg, resolved, o_latest))
    return OutdatedTool(tool_dir.name, package, current, latest, channel or "", others)


def detect_outdated_tools(
    tools_dir: Path,
    config: BotConfig,
    client: httpx.Client | None = None,
    max_tools: int | None = None,
) -> list[OutdatedTool]:
    """Scan tools/ for dirs whose main requirement has a newer conda version.

    At most ``max_tools`` (or ``autoupdate.max_tools_per_run`` when unset;
    0 means no cap) results, in dir-name order — bounds the number of agent
    runs a single workflow run can trigger."""
    if not config.autoupdate.enabled:
        return []
    cap = max_tools if max_tools is not None else config.autoupdate.max_tools_per_run
    skipped = skip_dirs(config, tools_dir.parent if tools_dir.name == "tools" else Path("."))
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        outdated: list[OutdatedTool] = []
        for child in sorted(tools_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name in skipped:
                continue
            result = check_tool_dir(child, config, client)
            if result is not None:
                outdated.append(result)
                if cap and len(outdated) >= cap:
                    logger.info(
                        "Reached max_tools_per_run=%d — %d tools returned",
                        cap, len(outdated),
                    )
                    break
        return outdated
    finally:
        if own_client:
            client.close()


# ---------------------------------------------------------------------------
# Dedup: don't rerun when a PR/branch already covers this tool
# ---------------------------------------------------------------------------

@dataclass
class AutoupdateDecision:
    proceed: bool
    reason: str
    # An open or closed-unmerged autoupdate PR that already exists for this
    # tool — the workflow pushes to its branch and updates/reopens it
    # instead of creating a new PR.
    existing_pr: int | None = None


def _title_version(title: str) -> str | None:
    """Version an autoupdate PR targets, from its '... to X.Y.Z' title."""
    match = re.search(r"to (\S+)\s*$", title)
    return match.group(1) if match else None


def _branch_exists(branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        capture_output=True,
    )
    return result.returncode == 0


def _last_commit_author(branch: str) -> str | None:
    subprocess.run(["git", "fetch", "origin", branch], capture_output=True)
    result = subprocess.run(
        ["git", "log", "-1", "--format=%an", f"origin/{branch}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or None


def check_autoupdate_pr_state(
    gh,
    tool_dir_name: str,
    detected_latest: str,
) -> AutoupdateDecision:
    """Decide whether an autoupdate run should proceed for a tool dir."""
    branch = f"{BRANCH_PREFIX}{tool_dir_name}"

    if _branch_exists(branch):
        author = _last_commit_author(branch)
        if author and author != BOT_AUTHOR:
            any_prs = gh.list_prs(branch, state="all")
            if any_prs:
                gh.add_comment(
                    any_prs[0]["number"],
                    f"A newer version ({detected_latest}) is available, but this branch "
                    "has manual commits. To allow auto-updates again, close the PR and "
                    f"delete the `{branch}` branch.",
                )
            return AutoupdateDecision(
                False, f"branch {branch} has manual commits by {author}"
            )

    open_prs = gh.list_prs(branch, state="open")
    if open_prs:
        pr = open_prs[0]
        target = _title_version(pr.get("title") or "")
        if target and is_newer(detected_latest, target):
            # planemo-autoupdate folds newer versions into the open PR rather
            # than waiting for it to merge — push updates it in place.
            return AutoupdateDecision(
                True,
                f"open PR #{pr['number']} targets {target} — folding in {detected_latest}",
                existing_pr=pr["number"],
            )
        return AutoupdateDecision(
            False, f"open PR #{pr['number']} already covers {target or detected_latest}"
        )

    declined = [
        p for p in gh.list_prs(branch, state="closed")
        if p.get("merged_at") is None
    ]
    if declined:
        pr = declined[0]
        declined_version = _title_version(pr.get("title") or "")
        if declined_version and not is_newer(detected_latest, declined_version):
            return AutoupdateDecision(
                False,
                f"maintainer declined up to {declined_version} in PR #{pr['number']}",
            )
        return AutoupdateDecision(
            True,
            f"newer version than declined PR #{pr['number']} — will reopen",
            existing_pr=pr["number"],
        )

    return AutoupdateDecision(True, "no existing PR or branch")


# ---------------------------------------------------------------------------
# Plan / PR text
# ---------------------------------------------------------------------------

def load_shed_metadata(tool_dir: Path) -> dict:
    path = tool_dir / ".shed.yml"
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def build_autoupdate_plan(outdated: OutdatedTool, links: list[str]) -> str:
    """Synthesize the plan an approved update would carry, for an auto bump."""
    parts = [
        f"# Update Plan: {outdated.tool_dir}",
        "",
        "## Summary",
        f"Automated version bump — `{outdated.package}` {outdated.current} → "
        f"{outdated.latest} (detected on the {outdated.channel} conda channel).",
        "",
        "## Proposed Changes",
        f"1. Update `@TOOL_VERSION@` to `{outdated.latest}` and reset "
        "`@VERSION_SUFFIX@` to `0` (upstream version bump).",
        f"2. Update the `<requirements>` version pin for `{outdated.package}`.",
    ]
    if outdated.others:
        others = ", ".join(f"`{pkg}` {cur} → {lat}" for pkg, cur, lat in outdated.others)
        parts.append(f"3. Other requirements with newer versions: {others}.")
    parts += [
        "",
        "## Upstream changes",
        "Research what changed upstream between the current and new version "
        "(release notes, changelog, commit history — use `search_web`, "
        "`fetch_url`, `search_github`). Specifically check for:",
        "- breaking changes: renamed/removed CLI flags, changed defaults, "
        "new required arguments",
        "- new parameters or options worth exposing",
        "- changed or new outputs (formats, file names, exit codes)",
        "Update `<command>`, `<inputs>`, `<outputs>` and `<tests>` accordingly.",
    ]
    if links:
        parts += ["", "Relevant links:"] + [f"- {link}" for link in links]
    return "\n".join(parts)


def build_autoupdate_description(outdated: OutdatedTool) -> str:
    return (
        f"Automated update of `tools/{outdated.tool_dir}`: the main dependency "
        f"`{outdated.package}` has a newer version on conda "
        f"({outdated.current} → {outdated.latest}). Apply the version bump and "
        "adapt the wrapper for any upstream breaking changes or new parameters."
    )


def build_autoupdate_links(outdated: OutdatedTool, shed: dict) -> list[str]:
    links = [f"https://anaconda.org/{outdated.channel}/{outdated.package}"]
    homepage = (shed.get("homepage_url") or "").strip("/")
    if homepage:
        if "github.com" in homepage and len(homepage.split("github.com")[1].split("/")) > 1:
            homepage += "/releases"
        links.append(homepage)
    return links


def build_autoupdate_pr_body(outdated: OutdatedTool, shed: dict) -> str:
    """PR body modeled on planemo-autoupdate's pr_text.py."""
    parts = [
        f"Hello! This is an automated update of `tools/{outdated.tool_dir}`. "
        f"Its main dependency `{outdated.package}` is out of date — a newer "
        f"version ({outdated.current} → {outdated.latest}) is available on "
        f"the {outdated.channel} conda channel.",
        "An agent applied the version bump and checked upstream for breaking "
        "changes and new parameters — review the diff as you would any "
        "automated change.",
    ]
    homepage = (shed.get("homepage_url") or "").strip("/")
    if homepage:
        if "github.com" in homepage and len(homepage.split("github.com")[1].split("/")) > 1:
            homepage += "/releases"
        parts.append(f"**Project home page:** {homepage}")
    if shed.get("maintainers"):
        parts.append(
            "**Maintainers:** " + ", ".join(f"@{m}" for m in shed["maintainers"])
        )
    parts += [
        "If you want to skip this version, close this PR without deleting the "
        "branch — it will be reopened only if an even newer version appears.",
        f"Any commit from another author than '{BOT_AUTHOR}' on this branch "
        "will prevent further auto-updates. To allow them again, delete the branch.",
    ]
    return "\n\n".join(parts)


def build_autoupdate_commit_msg(outdated: OutdatedTool) -> str:
    return f"Update {outdated.tool_dir} tool wrapper to {outdated.latest}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_autoupdate(
    tool_dir: Path,
    outdated: OutdatedTool,
    config: BotConfig,
    api_key: str,
    output_dir: Path,
    max_iterations_override: int | None = None,
    max_validation_retries_override: int | None = None,
):
    """Stage the tool dir and run the edit agent with the autoupdate prompt."""
    shed = load_shed_metadata(tool_dir)
    links = build_autoupdate_links(outdated, shed)
    plan = build_autoupdate_plan(outdated, links)
    description = build_autoupdate_description(outdated)

    return update_tool(
        description=description,
        links=links,
        plan_markdown=plan,
        config=config,
        api_key=api_key,
        src_tool_dir=tool_dir,
        output_dir=output_dir,
        tool_dir_name=tool_dir.name,
        system_template="autoupdate_system.txt",
        max_iterations_override=max_iterations_override,
        max_validation_retries_override=max_validation_retries_override,
    )
