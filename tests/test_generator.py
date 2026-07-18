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
