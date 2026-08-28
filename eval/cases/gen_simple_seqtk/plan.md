# Tool Plan: seqtk seq

## Summary
A Galaxy wrapper for `seqtk seq`, which converts FASTQ to FASTA and optionally reverses complements sequences.

## Underlying Software
- **Repository:** https://github.com/lh3/seqtk
- **Version:** 1.4
- **License:** MIT
- **Bioconda:** seqtk 1.4 (bioconda)

## Command-Line Interface
```
seqtk seq [options] <in.fq>
Options:
  -a    FASTA output
  -r    reverse complement
  -C    drop comments
```

## Proposed Galaxy Tool Wrapper
### Tool Structure
Single tool: seqtk_seq.xml

**Number of tool XML files:** 1

### Inputs
- input_fastq (format: fastq, type: data)

### Outputs
- output_fasta (format: fasta)

### Command Template
```
seqtk seq -a $input_fastq > $output_fasta
```

### Macros
Standard macros.xml with @TOOL_VERSION@ token, requirements, citations.

### Help Section
Convert FASTQ to FASTA using seqtk.

### Citations
DOI: 10.1038/nmeth.3287

## Test Plan
- Test data: small FASTQ file (test-data/sample.fastq)
- Test case: convert sample.fastq to FASTA, expect 1 output
