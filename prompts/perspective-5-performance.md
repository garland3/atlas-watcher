# Perspective: Performance & Resources

Review the diff for performance and resource issues. Look for:

- N+1 queries, unbatched I/O, fetch-in-loop patterns.
- Quadratic or worse algorithms over user-controllable input sizes.
- Unbounded memory growth: caches without eviction, accumulated lists, leaks.
- Blocking calls on hot paths or event loops; missing timeouts.
- Serialization round-trips that could be avoided (JSON parse/stringify chains).
- Inefficient re-renders, layout thrash, unnecessary work in tight loops (frontend).
- Missing pagination, missing index usage, full-table scans (backend/DB).
- File / socket handles not closed; goroutines / tasks not joined.

For each finding, output:

```
### <short title>
**File:** path/to/file.ext:LINE
**Severity:** high | medium | low | info
**Issue:** <what gets slow or expensive, under what load>
**Suggested fix:** <concrete change>
```

If performance looks fine, emit a single line: `NO_FINDINGS`.

## Severity calibration — do not inflate

- **high** — a measurable slowdown, leak, or resource exhaustion that will hit
  under realistic load; reserve for cases that genuinely should hold up merge.
- **medium** — worth fixing, but not on its own a reason to hold up the PR.
- **low / info** — micro-optimization or only matters at implausible scale.

Anchor severity to realistic load, not worst-case hypotheticals — most
findings are medium or below. When unsure between two levels, choose the lower
one.

Stay in the performance/resources domain.
