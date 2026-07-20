"""Tests for the generator module."""

from __future__ import annotations

from pathlib import Path

from gxy_tool_bot.generator import GeneratedFile, ValidationResult, validate_generated_files


def test_validation_valid_xml(tmp_path: Path) -> None:
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <command>test --input $input --output $output</command>
    <inputs>
        <param name="input" type="data" format="fasta"/>
    </inputs>
    <outputs>
        <data name="output" format="fasta"/>
    </outputs>
    <tests>
        <test>
            <param name="input" value="sample.fasta"/>
        </test>
    </tests>
</tool>"""
    files = [
        GeneratedFile(path="test.xml", content=xml),
        GeneratedFile(path="test-data/sample.fasta", content=b">seq1\nACGT"),
    ]
    result = validate_generated_files(files)
    assert result.valid is True
    assert result.errors == []


def test_validation_no_files() -> None:
    """Validation should fail if no files were generated at all."""
    result = validate_generated_files([])
    assert result.valid is False
    assert any("No XML files" in e for e in result.errors)


def test_validation_malformed_xml() -> None:
    xml = b"<tool><command>broken"
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("XML parse error" in e for e in result.errors)


def test_validation_missing_test_data() -> None:
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <tests>
        <test>
            <param name="input" value="missing.bam"/>
        </test>
    </tests>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("missing.bam" in e for e in result.errors)


def test_validation_undefined_macro() -> None:
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <macros>
        <import>macros.xml</import>
    </macros>
    <expand macro="undefined_macro"/>
</tool>"""
    macros = b"""<?xml version="1.0"?>
<macros>
    <token name="defined_token">value</token>
</macros>"""
    files = [
        GeneratedFile(path="test.xml", content=xml),
        GeneratedFile(path="macros.xml", content=macros),
    ]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("undefined_macro" in e for e in result.errors)


def test_validation_defined_macro_ok() -> None:
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <macros>
        <import>macros.xml</import>
    </macros>
    <expand macro="defined_macro"/>
</tool>"""
    macros = b"""<?xml version="1.0"?>
<macros>
    <macro name="defined_macro">content</macro>
</macros>"""
    files = [
        GeneratedFile(path="test.xml", content=xml),
        GeneratedFile(path="macros.xml", content=macros),
    ]
    result = validate_generated_files(files)
    assert result.valid is True


def test_validation_html_in_help_fails() -> None:
    """Help section with HTML tags should fail validation."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <command>test --input $input</command>
    <inputs>
        <param name="input" type="data" format="fasta"/>
    </inputs>
    <outputs>
        <data name="output" format="fasta"/>
    </outputs>
    <help><p>This is <strong>HTML</strong> help.</p></help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("HTML" in e for e in result.errors)


def test_validation_markdown_in_help_ok() -> None:
    """Help section with Markdown (no HTML tags) should pass validation."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <command>test --input $input</command>
    <inputs>
        <param name="input" type="data" format="fasta"/>
    </inputs>
    <outputs>
        <data name="output" format="fasta"/>
    </outputs>
    <help>

## Overview

This tool does **important** things.

- Item one
- Item two

    </help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is True


def test_validation_xml_macro_definition_ok() -> None:
    """IUC macros.xml uses <xml name="..."> elements, not <macro name="...">."""
    tool_xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.0.0">
    <macros>
        <import>macros.xml</import>
    </macros>
    <expand macro="requirements"/>
    <expand macro="citations"/>
    <expand macro="bio_tools"/>
</tool>"""
    macros_xml = b"""<?xml version="1.0"?>
<macros>
    <token name="@TOOL_VERSION@">1.0</token>
    <xml name="requirements">
        <requirements>
            <requirement type="package" version="@TOOL_VERSION@">test</requirement>
        </requirements>
    </xml>
    <xml name="citations">
        <citations>
            <citation type="doi">10.1234/test</citation>
        </citations>
    </xml>
    <xml name="bio_tools">
        <xrefs>
            <xref type="bio.tools">test</xref>
        </xrefs>
    </xml>
</macros>"""
    files = [
        GeneratedFile(path="test.xml", content=tool_xml),
        GeneratedFile(path="macros.xml", content=macros_xml),
    ]
    result = validate_generated_files(files)
    assert result.valid is True
