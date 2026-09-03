# Perspective: Tests & Observability

Review the diff for test coverage and runtime visibility. Look for:

- New behavior with no test, or tests that only assert the happy path.
- Tests that mock the system under test or assert on implementation details.
- Flaky patterns: real time, real network, ordering assumptions, sleeps.
- Missing negative tests (validation failures, permission denied, timeouts).
- Logging: missing for new failure paths, or noisy at info level.
- Metrics / traces: new code paths invisible to dashboards or alerts.
- Test data: fixtures that hide edge cases (always-valid inputs).

For each finding, output:

```
### <short title>
**File:** path/to/file.ext:LINE (or "missing")
**Severity:** high | medium | low | info
**Issue:** <what is untested or unobservable>
**Suggested fix:** <specific test case or log/metric to add>
```

If coverage and observability look adequate, emit a single line: `NO_FINDINGS`.

## Severity calibration — do not inflate

- **high** — an untested path where a regression would cause real harm under
  normal use; reserve for gaps that genuinely should hold up merge.
- **medium** — worth covering, but not on its own a reason to hold up the PR.
- **low / info** — nice-to-have coverage or minor observability gaps.

Missing tests are rarely a blocker on their own — most are medium or below.
When unsure between two levels, choose the lower one.

Stay in the test/observability domain.
