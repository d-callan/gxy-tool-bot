"""Tests for the eval harness module."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from gxy_tool_bot.eval_harness import (
    CaseResult,
    EvalCase,
    _build_summary,
    _matches_filters,
    format_report_text,
    load_cases,
    run_assertion,
    run_assertions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    """Create a minimal set of eval cases in a temp directory."""
    base = tmp_path / "cases"
    # Generate case
    gen_dir = base / "gen_simple"
    gen_dir.mkdir(parents=True)
    (gen_dir / "case.yml").write_text(yaml.dump({
        "name": "gen_simple",
        "type": "generate",
        "difficulty": "easy",
        "description": "A simple generate case",
        "plan": "plan.md",
        "assertions": [{"type": "file_exists", "file": "tool.xml"}],
    }))
    (gen_dir / "plan.md").write_text("# Plan\nSimple tool.")

    # Feedback case
    fb_dir = base / "fb_fix_macro"
    fb_dir.mkdir(parents=True)
    (fb_dir / "case.yml").write_text(yaml.dump({
        "name": "fb_fix_macro",
        "type": "feedback",
        "difficulty": "easy",
        "description": "Fix undefined macro",
        "existing_files": ["tool.xml"],
        "feedback": {"review_comments": [{"author": "alice", "body": "Fix this"}]},
        "assertions": [{"type": "xml_element_exists", "file": "macros.xml", "xpath": ".//xml[@name='requirements']"}],
    }))
    (fb_dir / "tool.xml").write_text("<tool/>")

    # Hard generate case
    hard_dir = base / "gen_hard_family"
    hard_dir.mkdir(parents=True)
    (hard_dir / "case.yml").write_text(yaml.dump({
        "name": "gen_hard_family",
        "type": "generate",
        "difficulty": "hard",
        "description": "A hard generate case",
        "plan": "plan.md",
        "assertions": [],
    }))
    (hard_dir / "plan.md").write_text("# Plan\nHard tool family.")

    return base


# ---------------------------------------------------------------------------
# load_cases tests
# ---------------------------------------------------------------------------

def test_load_cases_discovers_all(cases_dir: Path) -> None:
    cases = load_cases(cases_dir)
    names = {c.name for c in cases}
    assert names == {"gen_simple", "fb_fix_macro", "gen_hard_family"}


def test_load_cases_filter_by_type(cases_dir: Path) -> None:
    cases = load_cases(cases_dir, ["type=generate"])
    assert len(cases) == 2
    assert all(c.type == "generate" for c in cases)


def test_load_cases_filter_by_difficulty(cases_dir: Path) -> None:
    cases = load_cases(cases_dir, ["difficulty=easy"])
    assert len(cases) == 2
    assert all(c.difficulty == "easy" for c in cases)


def test_load_cases_filter_by_name_glob(cases_dir: Path) -> None:
    cases = load_cases(cases_dir, ["name=gen_*"])
    assert len(cases) == 2
    assert all(c.name.startswith("gen_") for c in cases)


def test_load_cases_filter_multiple_keys(cases_dir: Path) -> None:
    cases = load_cases(cases_dir, ["type=generate", "difficulty=easy"])
    assert len(cases) == 1
    assert cases[0].name == "gen_simple"


def test_load_cases_filter_no_matches(cases_dir: Path) -> None:
    cases = load_cases(cases_dir, ["difficulty=impossible"])
    assert cases == []


def test_load_cases_no_filters(cases_dir: Path) -> None:
    cases = load_cases(cases_dir)
    assert len(cases) == 3


def test_load_cases_empty_dir(tmp_path: Path) -> None:
    cases = load_cases(tmp_path)
    assert cases == []


# ---------------------------------------------------------------------------
# _matches_filters tests
# ---------------------------------------------------------------------------

def _make_case(name="test", type="generate", difficulty="easy") -> EvalCase:
    return EvalCase(name=name, type=type, difficulty=difficulty, description="", case_dir=Path("."), raw={})


def test_matches_filters_no_filters() -> None:
    assert _matches_filters(_make_case(), None) is True
    assert _matches_filters(_make_case(), []) is True


def test_matches_filters_invalid_key() -> None:
    assert _matches_filters(_make_case(), ["nonexistent=foo"]) is False


def test_matches_filters_empty_value() -> None:
    assert _matches_filters(_make_case(), ["type="]) is True  # empty value is skipped


# ---------------------------------------------------------------------------
# run_assertion tests
# ---------------------------------------------------------------------------

def test_assertion_file_exists_pass() -> None:
    files = {"tool.xml": b"<tool/>"}
    passed, msg = run_assertion({"type": "file_exists", "file": "tool.xml"}, files)
    assert passed is True
    assert msg == ""


def test_assertion_file_exists_fail() -> None:
    files = {"tool.xml": b"<tool/>"}
    passed, msg = run_assertion({"type": "file_exists", "file": "missing.xml"}, files)
    assert passed is False
    assert "missing.xml" in msg


def test_assertion_file_contains_pass() -> None:
    files = {"macros.xml": b"<token name='@TOOL_VERSION@'>1.0</token>"}
    passed, msg = run_assertion({"type": "file_contains", "file": "macros.xml", "pattern": "@TOOL_VERSION@"}, files)
    assert passed is True


def test_assertion_file_contains_fail() -> None:
    files = {"macros.xml": b"<macros/>"}
    passed, msg = run_assertion({"type": "file_contains", "file": "macros.xml", "pattern": "@TOOL_VERSION@"}, files)
    assert passed is False
    assert "@TOOL_VERSION@" in msg


def test_assertion_xml_element_exists_pass() -> None:
    xml = b"""<?xml version="1.0"?>
<tool>
    <command detect_errors="aggressive">test</command>
</tool>"""
    files = {"tool.xml": xml}
    passed, msg = run_assertion(
        {"type": "xml_element_exists", "file": "tool.xml", "xpath": ".//command[@detect_errors='aggressive']"},
        files,
    )
    assert passed is True


def test_assertion_xml_element_exists_fail() -> None:
    xml = b"""<?xml version="1.0"?>
<tool>
    <command>test</command>
</tool>"""
    files = {"tool.xml": xml}
    passed, msg = run_assertion(
        {"type": "xml_element_exists", "file": "tool.xml", "xpath": ".//command[@detect_errors='aggressive']"},
        files,
    )
    assert passed is False
    assert "xpath" in msg.lower() or "not found" in msg.lower()


def test_assertion_xml_element_count_pass() -> None:
    xml = b"""<?xml version="1.0"?>
<tool>
    <tests>
        <test expect_num_outputs="1"/>
        <test expect_num_outputs="1"/>
    </tests>
</tool>"""
    files = {"tool.xml": xml}
    passed, msg = run_assertion(
        {"type": "xml_element_count", "file": "tool.xml", "xpath": ".//test", "count": 2},
        files,
    )
    assert passed is True


def test_assertion_xml_element_count_fail() -> None:
    xml = b"""<?xml version="1.0"?>
<tool>
    <tests>
        <test expect_num_outputs="1"/>
    </tests>
</tool>"""
    files = {"tool.xml": xml}
    passed, msg = run_assertion(
        {"type": "xml_element_count", "file": "tool.xml", "xpath": ".//test", "count": 2},
        files,
    )
    assert passed is False
    assert "1" in msg and "2" in msg


def test_assertion_xml_attribute_equals_pass() -> None:
    xml = b"""<?xml version="1.0"?>
<tool>
    <help format="markdown">Help</help>
</tool>"""
    files = {"tool.xml": xml}
    passed, msg = run_assertion(
        {"type": "xml_attribute_equals", "file": "tool.xml", "xpath": ".//help", "attribute": "format", "value": "markdown"},
        files,
    )
    assert passed is True


def test_assertion_xml_attribute_equals_fail() -> None:
    xml = b"""<?xml version="1.0"?>
<tool>
    <help>Help</help>
</tool>"""
    files = {"tool.xml": xml}
    passed, msg = run_assertion(
        {"type": "xml_attribute_equals", "file": "tool.xml", "xpath": ".//help", "attribute": "format", "value": "markdown"},
        files,
    )
    assert passed is False


def test_assertion_xml_parse_error() -> None:
    files = {"tool.xml": b"<tool><broken"}
    passed, msg = run_assertion(
        {"type": "xml_element_exists", "file": "tool.xml", "xpath": ".//command"},
        files,
    )
    assert passed is False
    assert "parse error" in msg.lower()


def test_assertion_unknown_type() -> None:
    files = {"tool.xml": b"<tool/>"}
    passed, msg = run_assertion({"type": "nonexistent", "file": "tool.xml"}, files)
    assert passed is False
    assert "unknown" in msg.lower()


def test_assertion_file_not_found_for_xml() -> None:
    files = {"other.xml": b"<tool/>"}
    passed, msg = run_assertion(
        {"type": "xml_element_exists", "file": "missing.xml", "xpath": ".//command"},
        files,
    )
    assert passed is False
    assert "not found" in msg.lower()


# ---------------------------------------------------------------------------
# run_assertions tests
# ---------------------------------------------------------------------------

def test_run_assertions_all_pass() -> None:
    files = {"tool.xml": b"<tool><command detect_errors='aggressive'>t</command></tool>", "macros.xml": b"@TOOL_VERSION@"}
    assertions = [
        {"type": "file_exists", "file": "tool.xml"},
        {"type": "file_contains", "file": "macros.xml", "pattern": "@TOOL_VERSION@"},
        {"type": "xml_element_exists", "file": "tool.xml", "xpath": ".//command[@detect_errors='aggressive']"},
    ]
    passed, failures = run_assertions(assertions, files)
    assert passed is True
    assert failures == []


def test_run_assertions_some_fail() -> None:
    files = {"tool.xml": b"<tool/>"}
    assertions = [
        {"type": "file_exists", "file": "tool.xml"},
        {"type": "file_exists", "file": "missing.xml"},
        {"type": "file_contains", "file": "tool.xml", "pattern": "nonexistent"},
    ]
    passed, failures = run_assertions(assertions, files)
    assert passed is False
    assert len(failures) == 2


def test_run_assertions_empty() -> None:
    passed, failures = run_assertions([], {})
    assert passed is True
    assert failures == []


# ---------------------------------------------------------------------------
# _build_summary tests
# ---------------------------------------------------------------------------

def _make_result(name="case", type="generate", difficulty="easy", passed=True,
                 validation=True, iters=10, retries=0, duration=60.0) -> CaseResult:
    return CaseResult(
        name=name, type=type, difficulty=difficulty, description="",
        passed=passed, validation_passed=validation,
        planemo_lint_passed=None, planemo_test_passed=None,
        assertions_passed=passed, assertions_failed=[],
        agent_iterations=iters, validation_retries=retries,
        agent_terminated_naturally=True, gave_up=False,
        files_generated=3, error=None, duration_seconds=duration,
    )


def test_build_summary_empty() -> None:
    assert _build_summary([]) == {}


def test_build_summary_overall() -> None:
    results = [
        _make_result("a", passed=True),
        _make_result("b", passed=False),
    ]
    s = _build_summary(results)
    assert s["overall"]["count"] == 2
    assert s["overall"]["pass_rate"] == 0.5
    assert s["overall"]["avg_iterations"] == 10.0


def test_build_summary_by_difficulty() -> None:
    results = [
        _make_result("a", difficulty="easy", passed=True),
        _make_result("b", difficulty="easy", passed=True),
        _make_result("c", difficulty="hard", passed=False),
    ]
    s = _build_summary(results)
    assert s["by_difficulty"]["easy"]["pass_rate"] == 1.0
    assert s["by_difficulty"]["easy"]["count"] == 2
    assert s["by_difficulty"]["hard"]["pass_rate"] == 0.0
    assert s["by_difficulty"]["hard"]["count"] == 1


def test_build_summary_by_type() -> None:
    results = [
        _make_result("a", type="generate", passed=True),
        _make_result("b", type="feedback", passed=False),
    ]
    s = _build_summary(results)
    assert s["by_type"]["generate"]["pass_rate"] == 1.0
    assert s["by_type"]["feedback"]["pass_rate"] == 0.0


def test_build_summary_with_planemo() -> None:
    r1 = _make_result("a")
    r1.planemo_lint_passed = True
    r1.planemo_test_passed = False
    r2 = _make_result("b")
    r2.planemo_lint_passed = False
    r2.planemo_test_passed = None
    s = _build_summary([r1, r2])
    assert s["planemo"]["lint_pass_rate"] == 0.5
    assert s["planemo"]["test_pass_rate"] == 0.0  # only r1 has a non-None test result


# ---------------------------------------------------------------------------
# format_report_text tests
# ---------------------------------------------------------------------------

def test_format_report_text_basic() -> None:
    from gxy_tool_bot.eval_harness import EvalReport
    results = [
        _make_result("gen_simple", type="generate", difficulty="easy", passed=True),
        _make_result("fb_fix", type="feedback", difficulty="easy", passed=False),
    ]
    report = EvalReport(results=results, summary=_build_summary(results))
    text = format_report_text(report)
    assert "EVAL REPORT" in text
    assert "gen_simple" in text
    assert "fb_fix" in text
    assert "SUMMARY" in text
    assert "FAILURES DETAIL" in text


def test_format_report_text_no_failures() -> None:
    from gxy_tool_bot.eval_harness import EvalReport
    results = [_make_result("case1", passed=True)]
    report = EvalReport(results=results, summary=_build_summary(results))
    text = format_report_text(report)
    assert "FAILURES DETAIL" not in text


def test_case_result_to_dict() -> None:
    r = _make_result("test", duration=42.5)
    d = r.to_dict()
    assert d["name"] == "test"
    assert d["passed"] is True
    assert d["duration_seconds"] == 42.5
    assert d["planemo_lint_passed"] is None


def test_eval_report_to_dict() -> None:
    from gxy_tool_bot.eval_harness import EvalReport
    results = [_make_result("a"), _make_result("b")]
    report = EvalReport(results=results, summary=_build_summary(results))
    d = report.to_dict()
    assert len(d["results"]) == 2
    assert "summary" in d
    assert d["summary"]["overall"]["count"] == 2
