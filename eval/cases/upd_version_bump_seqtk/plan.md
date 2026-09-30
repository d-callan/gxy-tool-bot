# Update Plan: seqtk

**Number of tool XML files:** 1

## Summary

Bump the seqtk wrapper from upstream version 1.4 to 1.5.

## Current State

- Wrapper version: 1.4 (`@TOOL_VERSION@` = 1.4, `@VERSION_SUFFIX@` = 0)
- Latest upstream: 1.5
- Files: `seqtk.xml`, `macros.xml`, `test-data/reads.fasta`

## Proposed Changes

- `seqtk.xml`: update the `<requirement>` package version to 1.5.
- `macros.xml`: set `@TOOL_VERSION@` to 1.5 and reset `@VERSION_SUFFIX@` to 0.

## Version handling

Upstream version bump → bump `@TOOL_VERSION@` to 1.5, reset `@VERSION_SUFFIX@` to 0, update requirement pin to 1.5.

## Test impact

No CLI changes in 1.5 that affect this wrapper — existing tests remain valid.

## Open Questions

None.
