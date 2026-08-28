# Tool Plan: bcftools family (view, filter, sort, norm)

## Summary
A Galaxy tool family wrapping four bcftools subcommands — view, filter, sort, and norm — sharing a common macros.xml in one directory.

## Underlying Software
- **Repository:** https://github.com/samtools/bcftools
- **Version:** 1.20
- **License:** MIT
- **Bioconda:** bcftools 1.20 (bioconda)

## Command-Line Interface

### bcftools view
```
bcftools view [options] <in.vcf>
Options: -O TYPE (output type: z,b,u), -o FILE, -f INT (filter flags)
```

### bcftools filter
```
bcftools filter [options] <in.vcf>
Options: -e EXPR (exclude), -i EXPR (include), -s STRING (soft filter), -O TYPE, -o FILE
```

### bcftools sort
```
bcftools sort [options] <in.vcf>
Options: -o FILE, -O TYPE, -m INT (max memory)
```

### bcftools norm
```
bcftools norm [options] <in.vcf>
Options: -m <type> (merge: + or -), -f REF (reference fasta), -O TYPE, -o FILE, -d (remove duplicates)
```

## Proposed Galaxy Tool Wrapper
### Tool Structure
Tool family with 4 XMLs sharing macros.xml:
- bcftools_view.xml — view/convert VCF/BCF
- bcftools_filter.xml — filter VCF records by expression
- bcftools_sort.xml — sort VCF by coordinate
- bcftools_norm.xml — normalize VCF (split/merge multiallelics, left-align)

**Number of tool XML files:** 4

### Inputs
- view: input_vcf (vcf), output_format (select)
- filter: input_vcf (vcf), filter_mode (select), expression (text)
- sort: input_vcf (vcf)
- norm: input_vcf (vcf), merge_type (select), reference (fasta, optional)

### Outputs
- view: output_vcf (vcf)
- filter: output_vcf (vcf)
- sort: output_vcf (vcf)
- norm: output_vcf (vcf)

### Macros
Shared macros.xml with @TOOL_VERSION@ token, requirements macro, citations macro, bio_tools xref macro, output_format macro.

### Citations
DOI: 10.1093/gigascience/giab008

## Test Plan
- Test data: small VCF file (test-data/sample.vcf)
- Test cases: one per tool, each expects 1 output
