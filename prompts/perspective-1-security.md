# Perspective: Security

Review the diff strictly through a security lens. Look for:

- Injection (SQL, shell, template, XSS, path traversal).
- AuthN/AuthZ gaps: missing checks, IDOR, role confusion, privilege escalation.
- Secret handling: hardcoded keys, secrets in logs, weak crypto, weak randomness.
- Input validation and trust boundaries: data from network, files, env.
- Deserialization, SSRF, unsafe redirects, open CORS, prototype pollution.
- Dependency risk: new packages, version pins, known-vuln versions.
- Supply chain: install scripts, postinstall hooks, build-time fetches.
- Data exposure: PII, tokens, internal IDs leaked into responses or logs.

For each finding, output:

```
### <short title>
**File:** path/to/file.ext:LINE
**Severity:** critical | high | medium | low | info
**Issue:** <one-paragraph description of the concrete risk>
**Suggested fix:** <one-paragraph remediation>
```

If the diff has no security-relevant changes, emit a single line: `NO_FINDINGS`.

## Severity calibration — do not inflate

- **critical** — exploitable, data loss / corruption, or breaks a core flow
  under normal use.
- **high** — a real defect or vulnerability that bites under normal use and
  should block merge until fixed.
- **medium** — worth fixing, but not on its own a reason to hold up the PR.
- **low / info** — minor, optional, defense-in-depth, or stylistic.

When unsure between two levels, choose the lower one. Most findings are medium
or below; reserve high/critical for concrete, real-world harm.

Do not list findings outside the security domain. Do not summarize the PR.
