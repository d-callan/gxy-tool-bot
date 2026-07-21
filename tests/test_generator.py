"""Tests for the generator module."""

from __future__ import annotations

import gzip
from pathlib import Path

from gxy_tool_bot.generator import GeneratedFile, ValidationResult, FileWriter, validate_generated_files


def test_validation_valid_xml(tmp_path: Path) -> None:
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test --input $input --output $output</command>
    <inputs>
        <param name="input" type="data" format="fasta"/>
    </inputs>
    <outputs>
        <data name="output" format="fasta"/>
    </outputs>
    <tests>
        <test expect_num_outputs="1">
            <param name="input" value="sample.fasta"/>
        </test>
    </tests>
    <help format="markdown">Help</help>
    <xrefs><xref type="bio.tools">test</xref></xrefs>
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
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <macros>
        <import>macros.xml</import>
    </macros>
    <expand macro="defined_macro"/>
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown">Help</help>
    <xrefs><xref type="bio.tools">test</xref></xrefs>
</tool>"""
    macros = b"""<?xml version="1.0"?>
<macros>
    <macro name="defined_macro">content</macro>
</macros>"""
    files = [
        GeneratedFile(path="test.xml", content=xml),
        GeneratedFile(path="macros.xml", content=macros),
        GeneratedFile(path="test-data/sample.fasta", content=b">seq1\nACGT"),
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
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test --input $input</command>
    <inputs>
        <param name="input" type="data" format="fasta"/>
    </inputs>
    <outputs>
        <data name="output" format="fasta"/>
    </outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown">

## Overview

This tool does **important** things.

- Item one
- Item two

    </help>
    <xrefs><xref type="bio.tools">test</xref></xrefs>
</tool>"""
    files = [
        GeneratedFile(path="test.xml", content=xml),
        GeneratedFile(path="test-data/sample.fasta", content=b">seq1\nACGT"),
    ]
    result = validate_generated_files(files)
    assert result.valid is True


def test_validation_xml_macro_definition_ok() -> None:
    """IUC macros.xml uses <xml name="..."> elements, not <macro name="...">."""
    tool_xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <macros>
        <import>macros.xml</import>
    </macros>
    <expand macro="requirements"/>
    <expand macro="citations"/>
    <expand macro="bio_tools"/>
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown">Help</help>
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
        GeneratedFile(path="test-data/sample.fasta", content=b">seq1\nACGT"),
    ]
    result = validate_generated_files(files)
    assert result.valid is True


def test_compress_file_creates_gz(tmp_path: Path) -> None:
    """compress_file should create a .gz version and track both files."""
    fw = FileWriter(tmp_path)
    fw.write_file({"path": "test-data/sample.fasta", "content": ">seq1\nACGTACGT"})
    result = fw.compress_file({"path": "test-data/sample.fasta"})
    assert "compressed" in result.lower()
    assert "test-data/sample.fasta.gz" in result
    # Both files tracked
    assert "test-data/sample.fasta" in fw.files
    assert "test-data/sample.fasta.gz" in fw.files
    # .gz file exists on disk
    gz_path = tmp_path / "test-data" / "sample.fasta.gz"
    assert gz_path.exists()
    # Content is valid gzip
    decompressed = gzip.decompress(fw.files["test-data/sample.fasta.gz"])
    assert decompressed == b">seq1\nACGTACGT"


def test_compress_file_missing_source(tmp_path: Path) -> None:
    """compress_file should error if source file doesn't exist."""
    fw = FileWriter(tmp_path)
    result = fw.compress_file({"path": "test-data/nonexistent.fasta"})
    assert "Error" in result
    assert "does not exist" in result


def test_compress_file_empty_path(tmp_path: Path) -> None:
    """compress_file should error on empty path."""
    fw = FileWriter(tmp_path)
    result = fw.compress_file({"path": ""})
    assert "Error" in result


def test_compress_file_path_traversal(tmp_path: Path) -> None:
    """compress_file should reject paths outside output dir."""
    fw = FileWriter(tmp_path)
    result = fw.compress_file({"path": "../../etc/passwd"})
    assert "Error" in result
    assert "outside" in result


def test_validation_missing_detect_errors() -> None:
    """Command without detect_errors=aggressive should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command>test --input $input</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("detect_errors" in e for e in result.errors)


def test_validation_missing_expect_num_outputs() -> None:
    """Test without expect_num_outputs should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("expect_num_outputs" in e for e in result.errors)


def test_validation_missing_help_format() -> None:
    """Help without format=markdown should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help>Help text</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any('format="markdown"' in e for e in result.errors)


def test_validation_bad_tool_id() -> None:
    """Tool ID with uppercase or invalid chars should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="MyTool" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("invalid characters" in e for e in result.errors)


def test_validation_hardcoded_version() -> None:
    """Hardcoded version string should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="1.2.3">
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("hardcoded" in e for e in result.errors)


def test_validation_missing_xrefs() -> None:
    """Missing xrefs/bio.tools should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("bio.tools" in e for e in result.errors)


def test_validation_cheetah_in_xml_macro() -> None:
    """Cheetah directives in <xml> macros should fail."""
    macros = b"""<?xml version="1.0"?>
<macros>
    <token name="@TOOL_VERSION@">1.0</token>
    <xml name="bad_macro">
        #if str($foo)
            --flag
        #end if
    </xml>
</macros>"""
    files = [GeneratedFile(path="macros.xml", content=macros)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("Cheetah" in e for e in result.errors)


def test_validation_optional_with_value() -> None:
    """optional=true with a value attribute should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs>
        <param name="score" type="float" value="0.5" optional="true" label="Score"/>
    </inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("optional" in e for e in result.errors)


def test_validation_display_checkboxes() -> None:
    """display=checkboxes on multi-select should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs>
        <param name="items" type="select" multiple="true" display="checkboxes" label="Items">
            <option value="A">A</option>
        </param>
    </inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("display" in e for e in result.errors)


def test_validation_stdio_with_detect_errors() -> None:
    """<stdio> with detect_errors=aggressive should fail (redundant)."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <stdio><exit_code range="1:" level="fatal"/></stdio>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("stdio" in e.lower() and "redundant" in e.lower() for e in result.errors)


def test_validation_boolean_truevalue_true() -> None:
    """Boolean param with truevalue='true' should fail (should be CLI flag)."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test --verbose $verbose</command>
    <inputs>
        <param name="verbose" type="boolean" truevalue="true" falsevalue="false" label="Verbose"/>
    </inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="s.fa"/></test></tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("truevalue" in e and "true" in e for e in result.errors)


def test_validation_test_output_missing_ftype() -> None:
    """Test <output> without ftype should fail."""
    xml = b"""<?xml version="1.0"?>
<tool id="test" name="Test" version="@TOOL_VERSION@+galaxy0">
    <command detect_errors="aggressive">test</command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests>
        <test expect_num_outputs="1">
            <param name="input" value="sample.fasta"/>
            <output name="output" file="result.fasta"/>
        </test>
    </tests>
    <help format="markdown">Help</help>
</tool>"""
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("ftype" in e for e in result.errors)


def test_validation_all_conventions_ok() -> None:
    """A tool that follows all IUC conventions should pass."""
    xml = b"""<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@" profile="@PROFILE@">
    <macros><import>macros.xml</import></macros>
    <expand macro="requirements"/>
    <command detect_errors="aggressive"><![CDATA[test --input $input]]></command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown"><![CDATA[## Overview\n\nDoes things.]]></help>
    <xrefs><xref type="bio.tools">test_tool</xref></xrefs>
    <expand macro="citations"/>
</tool>"""
    macros = b"""<?xml version="1.0"?>
<macros>
    <token name="@TOOL_VERSION@">1.0</token>
    <token name="@VERSION_SUFFIX@">0</token>
    <token name="@PROFILE@">25.0</token>
    <xml name="requirements">
        <requirements><requirement type="package" version="@TOOL_VERSION@">test</requirement></requirements>
    </xml>
    <xml name="citations">
        <citations><citation type="doi">10.1234/test</citation></citations>
    </xml>
</macros>"""
    files = [
        GeneratedFile(path="test_tool.xml", content=xml),
        GeneratedFile(path="macros.xml", content=macros),
        GeneratedFile(path="test-data/sample.fasta", content=b">seq1\nACGT"),
    ]
    result = validate_generated_files(files)
    assert result.valid is True, f"Expected valid but got errors: {result.errors}"


def test_validation_redundant_name_with_argument() -> None:
    """name attribute is redundant when argument is used and they match."""
    xml = b'''<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@" profile="25.0">
    <command detect_errors="aggressive"><![CDATA[test --input $input]]></command>
    <inputs><param name="input" argument="--input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown">Help</help>
    <xrefs><xref type="bio.tools">test_tool</xref></xrefs>
</tool>'''
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("redundant name" in e for e in result.errors)


def test_validation_name_ok_when_differs_from_argument() -> None:
    """name attribute is fine when it differs from the argument."""
    xml = b'''<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@" profile="25.0">
    <command detect_errors="aggressive"><![CDATA[test --input-file $input]]></command>
    <inputs><param name="input" argument="--input-file" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown">Help</help>
    <xrefs><xref type="bio.tools">test_tool</xref></xrefs>
</tool>'''
    files = [GeneratedFile(path="test.xml", content=xml),
             GeneratedFile(path="test-data/sample.fasta", content=b">seq1\nACGT")]
    result = validate_generated_files(files)
    assert result.valid is True, f"Expected valid but got errors: {result.errors}"


def test_validation_redundant_output_label() -> None:
    """label='${tool.name} on ${on_string}' on <data> is redundant and should fail."""
    xml = b'''<?xml version="1.0"?>
<tool id="test_tool" name="Test Tool" version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@" profile="25.0">
    <command detect_errors="aggressive"><![CDATA[test --input $input]]></command>
    <inputs><param name="input" type="data" format="fasta"/></inputs>
    <outputs><data name="output" format="fasta" label="${tool.name} on ${on_string}"/></outputs>
    <tests><test expect_num_outputs="1"><param name="input" value="sample.fasta"/></test></tests>
    <help format="markdown">Help</help>
    <xrefs><xref type="bio.tools">test_tool</xref></xrefs>
</tool>'''
    files = [GeneratedFile(path="test.xml", content=xml)]
    result = validate_generated_files(files)
    assert result.valid is False
    assert any("label" in e and "redundant" in e for e in result.errors)


def test_write_file_rejects_null_bytes(tmp_path: Path) -> None:
    """write_file should reject content with null bytes (binary detection)."""
    fw = FileWriter(tmp_path)
    result = fw.write_file({"path": "test-data/sample.tar", "content": b"\x00\x01\x02\x03"})
    assert "Error" in result
    assert "binary" in result.lower()
    assert "sample.tar" not in fw.files


def test_write_file_rejects_non_utf8(tmp_path: Path) -> None:
    """write_file should reject content that isn't valid UTF-8."""
    fw = FileWriter(tmp_path)
    result = fw.write_file({"path": "test-data/sample.bin", "content": b"\x80\x81\xfe\xff"})
    assert "Error" in result
    assert "binary" in result.lower()
    assert "sample.bin" not in fw.files


def test_write_file_accepts_normal_text(tmp_path: Path) -> None:
    """write_file should accept normal text content without false-positive binary detection."""
    fw = FileWriter(tmp_path)
    result = fw.write_file({"path": "test.xml", "content": "<?xml version=\"1.0\"?>\n<tool/>"})
    assert "File written" in result
    assert "test.xml" in fw.files


def test_give_up_sets_reason(tmp_path: Path) -> None:
    """give_up should set give_up_reason and return a 'Gave up' message."""
    fw = FileWriter(tmp_path)
    assert fw.give_up_reason is None
    result = fw.give_up({"reason": "Test data too large to download"})
    assert "Gave up" in result
    assert "Test data too large" in result
    assert fw.give_up_reason == "Test data too large to download"


def test_give_up_requires_reason(tmp_path: Path) -> None:
    """give_up should error if no reason is provided."""
    fw = FileWriter(tmp_path)
    result = fw.give_up({"reason": ""})
    assert "Error" in result
    assert fw.give_up_reason is None
