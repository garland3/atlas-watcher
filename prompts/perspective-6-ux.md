# Perspective: UX, Accessibility & API Ergonomics

Review the diff for user-facing and consumer-facing experience. Look for:

- Confusing labels, error messages that don't tell the user what to do.
- Loading / empty / error states missing or inconsistent.
- Keyboard navigation, focus management, ARIA roles, color contrast.
- Mobile / small-viewport breakage; pointer-only interactions.
- API ergonomics: confusing param names, inconsistent return shapes,
  breaking changes without versioning, surprising defaults.
- Docs / changelog / migration notes that should accompany this change.
- Internationalization: hardcoded strings, locale-sensitive formatting.

For each finding, output:

```
### <short title>
**File:** path/to/file.ext:LINE (or "docs missing")
**Severity:** high | medium | low | info
**Issue:** <what the user / consumer experiences>
**Suggested fix:** <concrete change or copy>
```

If UX/ergonomics look fine, emit a single line: `NO_FINDINGS`.

## Severity calibration — do not inflate

- **high** — the change ships a broken or inaccessible experience, or a
  breaking API change, that real users/consumers will hit; reserve for cases
  that genuinely should hold up merge.
- **medium** — worth improving, but not on its own a reason to hold up the PR.
- **low / info** — copy polish, minor inconsistency, or preference.

Most UX findings are medium or below. When unsure between two levels, choose
the lower one.

Stay in the UX / API-ergonomics / docs domain.
