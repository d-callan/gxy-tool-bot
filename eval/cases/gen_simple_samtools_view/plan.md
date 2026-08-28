# Tool Plan: samtools view

## Summary
A Galaxy wrapper for `samtools view`, which converts/views SAM/BAM files with optional filtering by region or flag.

## Underlying Software
- **Repository:** https://github.com/samtools/samtools
- **Version:** 1.20
- **License:** MIT
- **Bioconda:** samtools 1.20 (bioconda)

## Command-Line Interface
```
samtools view [options] <in.bam|in.sam> [region]
Options:
  -b    output BAM
  -h    include header in SAM output
  -f INT  required flags
  -F INT  filter flags
  -o FILE  output file
```

## Proposed Galaxy Tool Wrapper
### Tool Structure
Single tool: samtools_view.xml

**Number of tool XML files:** 1

### Inputs
- input_bam (format: bam, type: data)
- header (type: boolean, argument: -h)

### Outputs
- output_bam (format: bam)

### Command Template
```
samtools view -b #if $header: -h #end if $input_bam -o $output_bam
```

### Macros
Standard macros.xml with @TOOL_VERSION@ token, requirements, citations.

### Help Section
View and convert SAM/BAM files using samtools.

### Citations
DOI: 10.1093/bioinformatics/btp352

## Test Plan
- Test data: small BAM file (test-data/sample.bam)
- Test case: convert sample.bam, expect 1 output
