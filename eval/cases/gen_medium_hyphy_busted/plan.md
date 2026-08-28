# Tool Plan: HyPhy BUSTED

## Summary
A Galaxy wrapper for HyPhy BUSTED (Branch-Site Unrestricted Statistical Test for Episodic Diversification), which detects episodic positive selection at individual branches or branch groups in a phylogenetic tree.

## Underlying Software
- **Repository:** https://github.com/veg/hyphy
- **Version:** 2.5.96
- **License:** MIT
- **Bioconda:** hyphy 2.5.96 (bioconda)

## Command-Line Interface
```
hyphy busted --alignment <file> --tree <file> [options]
Options:
  --type <type>          Test type: 'branch' or 'branchsite' (default: branchsite)
  --branches <list>      Comma-separated list of branches to test (default: all)
  --srv <yes|no>         Include site-to-site rate variation (default: yes)
  --output <file>        Write JSON results to file
```

## Proposed Galaxy Tool Wrapper
### Tool Structure
Single tool: busted.xml

**Number of tool XML files:** 1

### Inputs
- alignment (format: fasta, type: data)
- tree (format: nexus/newick, type: data)
- test_type (type: select: branch / branchsite)
- srv (type: boolean, argument: --srv, truevalue=yes, falsevalue=no)

### Outputs
- results_json (format: json)

### Command Template
```
hyphy busted --alignment $alignment --tree $tree --type $test_type --srv $srv --output $results_json
```

### Macros
Standard macros.xml with @TOOL_VERSION@ token, requirements, citations, bio_tools xref.

### Help Section
BUSTED detects episodic positive selection at individual branches in a phylogenetic tree.

### Citations
DOI: 10.1093/molbev/msv127

## Test Plan
- Test data: small FASTA alignment (test-data/sample.fasta) and tree (test-data/sample.nwk)
- Test case: run BUSTED with branchsite type, srv=yes, expect 1 output
