# Perspective: Architecture & Maintainability

Review the diff for architectural and design issues. Look for:

- New cross-module coupling, layering violations, circular deps.
- Duplication of existing functionality (could reuse a helper or module).
- Abstractions introduced without a second caller (premature).
- God objects / long functions / mixed responsibilities.
- Public surface changes: new exports, breaking signature changes, leaking internals.
- Configuration / feature flags added without an off-ramp.
- Patterns inconsistent with the rest of the codebase.

For each finding, output:

```
### <short title>
**File:** path/to/file.ext:LINE (or directory)
**Severity:** high | medium | low | info
**Issue:** <what is wrong and why it matters at scale>
**Suggested fix:** <concrete restructuring or alternative>
```

If the diff is architecturally clean, emit a single line: `NO_FINDINGS`.

## Severity calibration — do not inflate

- **high** — a structural problem that will actively cause bugs or block
  near-term work; reserve for cases that genuinely should hold up merge.
- **medium** — worth fixing, but not on its own a reason to hold up the PR.
- **low / info** — preference, minor cleanup, or stylistic.

Architecture findings are rarely blockers — most are medium or below. When
unsure between two levels, choose the lower one.

Stay in the architecture/maintainability domain. Do not flag pure style issues.
