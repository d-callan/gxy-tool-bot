# Tool Plan: bcftools filter

## Summary
A Galaxy wrapper for `bcftools filter`, which filters VCF/BCF records based on quality, depth, and expression criteria with optional output in VCF or BCF format.

## Underlying Software
- **Repository:** https://github.com/samtools/bcftools
- **Version:** 1.20
- **License:** MIT
- **Bioconda:** bcftools 1.20 (bioconda)

## Command-Line Interface
```
bcftools filter [options] <in.vcf>
Options:
  -e EXPR    exclude expression (e.g. 'QUAL<20')
  -i EXPR    include expression
  -s STRING  soft filter (annotate, don't remove)
  -g TYPE    type of filter: + (add), - (remove)
  -O TYPE    output type: z (compressed vcf), b (bcf), u (uncompressed bcf)
  -o FILE    output file
  --SnpGap INT  filter SNPs within INT bp of an indel
  --IndelGap INT  filter indels within INT bp of another indel
```

## Proposed Galaxy Tool Wrapper
### Tool Structure
Single tool: bcftools_filter.xml

**Number of tool XML files:** 1

### Inputs
- input_vcf (format: vcf, type: data)
- filter_mode (type: select: exclude / include)
- expression (type: text, e.g. 'QUAL<20')
- soft_filter (type: boolean, argument: -s)
- output_format (type: select: compressed VCF / BCF)

### Outputs
- output_vcf (format: vcf or bcf depending on selection)

### Command Template
```
bcftools filter #$filter_mode $expression -O $output_format -o $output_vcf $input_vcf
```

### Macros
Standard macros.xml with @TOOL_VERSION@ token, requirements, citations, bio_tools xref.

### Help Section
Filter VCF/BCF records by quality, depth, or expression using bcftools filter.

### Citations
DOI: 10.1093/gigascience/giab008

## Test Plan
- Test data: small VCF file (test-data/sample.vcf)
- Test case 1: filter QUAL<20, exclude mode, VCF output, expect 1 output
- Test case 2: filter QUAL<20, include mode, BCF output, expect 1 output
