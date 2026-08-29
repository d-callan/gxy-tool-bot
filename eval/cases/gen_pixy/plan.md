# Tool Plan: pixy

## Summary
A Galaxy wrapper for `pixy`, which calculates unbiased estimators of population genetic summary statistics (pi, dxy, Fst) from VCF files containing invariant sites.

## Underlying Software
- **Repository:** https://github.com/ksamuk/pixy
- **Version:** 2.0.0
- **License:** MIT
- **Conda:** pixy 2.0.0 (conda-forge)

## Command-Line Interface
```
pixy --stats [pi|dxy|fst] --vcf <input.vcf.gz> --populations <pops.txt> --output_folder <dir> --output_prefix <prefix> [--n_cores N]
```
VCFs must be bgzipped and tabix-indexed before use with pixy.

## Proposed Galaxy Tool Wrapper
### Tool Structure
Single tool: pixy.xml

**Number of tool XML files:** 1

### Inputs
- input_vcf (format: vcf, type: data) — bgzipped, tabix-indexed VCF
- populations_file (format: tabular, type: data)
- stats_type (select: pi, dxy, fst)

### Outputs
- output (format: tabular) — summary statistics table

### Command Template
```
pixy --stats $stats_type --vcf $input_vcf --populations $populations_file --output_folder outputs --output_prefix output
```

### Macros
Standard macros.xml with @TOOL_VERSION@ token, requirements, citations.

### Help Section
Calculate population genetic summary statistics (pi, dxy, Fst) from VCF data using pixy.

### Citations
DOI: 10.1038/s41588-020-0678-9

## Test Plan
- Test data: small bgzipped VCF (test-data/sample.vcf.gz) + populations file (test-data/populations.txt)
- Test case: calculate pi on sample data, expect 1 output
