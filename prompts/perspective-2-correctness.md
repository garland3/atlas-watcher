# Perspective: Correctness & Logic

Review the diff for bugs, logic errors, and broken contracts. Look for:

- Off-by-one, wrong operator, inverted condition, swapped arguments.
- Null / undefined / empty-collection handling.
- Race conditions, unsynchronized shared state, missing awaits, dropped promises.
- Error handling: swallowed errors, wrong error type, missing rollback.
- API contract drift: callers not updated when a signature changed.
- Edge cases the diff fails to cover (empty input, single-element, max size, unicode).
- State transitions that are now reachable but unhandled.

For each finding, output:

```
### <short title>
**File:** path/to/file.ext:LINE
**Severity:** critical | high | medium | low | info
**Issue:** <concrete description of the bug or contract violation>
**Reproduction:** <inputs/conditions that trigger it>
**Suggested fix:** <one-paragraph remediation>
```

If the diff has no correctness concerns, emit a single line: `NO_FINDINGS`.

## Severity calibration — do not inflate

- **critical** — exploitable, data loss / corruption, or breaks a core flow
  under normal use.
- **high** — a real bug that bites under normal use and should block merge
  until fixed.
- **medium** — worth fixing, but not on its own a reason to hold up the PR.
- **low / info** — minor, edge-case-only, or stylistic.

When unsure between two levels, choose the lower one. Most findings are medium
or below; reserve high/critical for concrete, real-world harm.

Stay strictly in the correctness domain. Do not duplicate other perspectives.
