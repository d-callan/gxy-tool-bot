"""Shared utilities for planemo test output parsing."""

from __future__ import annotations

import json


def summarize_test_json(raw: str) -> str:
    """Parse planemo test JSON and extract a compact summary of failures only.

    The JSON has structure: {"tests": [{"id": "...", "data": {"status": "...", ...}}], "summary": {...}}
    We extract only failed/error tests with their output_problems, execution_problem, and job info.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:5000]

    tests = data.get("tests", [])
    failures = []
    for test in tests:
        test_data = test.get("data", {})
        status = test_data.get("status", "")
        if status in ("success", "skip"):
            continue
        entry = {"id": test.get("id", ""), "status": status}
        if test_data.get("output_problems"):
            entry["output_problems"] = test_data["output_problems"]
        if test_data.get("execution_problem"):
            entry["execution_problem"] = test_data["execution_problem"]
        if test_data.get("problem_log"):
            entry["problem_log"] = test_data["problem_log"][:2000]
        job = test_data.get("job")
        if job:
            entry["job"] = {
                k: v for k, v in job.items()
                if k in ("command_line", "stdout", "stderr")
            }
        failures.append(entry)

    if not failures:
        summary = data.get("summary", {})
        n = summary.get("num_tests", 0)
        return f"All {n} tests passed." if n else "No test results found."

    lines = []
    for f in failures:
        lines.append(f"### {f['id']} — {f['status']}")
        if "output_problems" in f:
            lines.append("Output problems:")
            for p in f["output_problems"]:
                lines.append(f"  - {p}")
        if "execution_problem" in f:
            lines.append(f"Execution problem: {f['execution_problem']}")
        if "problem_log" in f:
            lines.append(f"Problem log (truncated):\n{f['problem_log']}")
        if "job" in f:
            job = f["job"]
            if job.get("command_line"):
                lines.append(f"Command: {job['command_line']}")
            if job.get("stderr"):
                lines.append(f"Stderr: {job['stderr'][:2000]}")
            if job.get("stdout"):
                lines.append(f"Stdout: {job['stdout'][:2000]}")
        lines.append("")
    return "\n".join(lines)
