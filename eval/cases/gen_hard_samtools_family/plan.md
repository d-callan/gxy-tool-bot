# Tool Plan: samtools family (view, sort, index)

## Summary
A Galaxy tool family wrapping three samtools subcommands — view, sort, and index — sharing a common macros.xml in one directory.

## Underlying Software
- **Repository:** https://github.com/samtools/samtools
- **Version:** 1.20
- **License:** MIT
- **Bioconda:** samtools 1.20 (bioconda)

## Command-Line Interface

### samtools view
```
samtools view [options] <in.bam|in.sam> [region]
Options: -b (BAM output), -h (include header), -f INT (required flags), -F INT (filter flags), -o FILE
```

### samtools sort
```
samtools sort [options] <in.bam>
Options: -n (sort by name), -o FILE (output), -@ INT (threads)
```

### samtools index
```
samtools index [options] <in.bam>
Options: -b (BAI index), -c (CSI index), -o FILE
```

## Proposed Galaxy Tool Wrapper
### Tool Structure
Tool family with 3 XMLs sharing macros.xml:
- samtools_view.xml — convert/view SAM/BAM
- samtools_sort.xml — sort BAM by coordinate or name
- samtools_index.xml — index BAM file

**Number of tool XML files:** 3

### Inputs
- view: input_bam (bam), header (boolean)
- sort: input_bam (bam), sort_by_name (boolean)
- index: input_bam (bam), index_format (select: BAI/CSI)

### Outputs
- view: output_bam (bam)
- sort: output_bam (bam)
- index: output_index (bai)

### Macros
Shared macros.xml with @TOOL_VERSION@ token, requirements macro, citations macro, bio_tools xref macro.

### Citations
DOI: 10.1093/bioinformatics/btp352

## Test Plan
- Test data: small BAM file (test-data/sample.bam)
- Test cases: one per tool, each expects 1 output
