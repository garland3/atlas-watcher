# Optional WindAnvil assurance sighting

This **opt-in, read-only** Atlas Watcher module displays the
neonoxide.wind-anvil-sighting.v1 envelope produced by NeonOxide's separate
WindAnvil observation intake. It is not part of the normal PR/issue reviewer,
security triage, agent roles, audit ledger, or posting path.

Example:

    PYTHONPATH=src python3 -m atlas_watcher.windanvil /path/to/sighting.json

The display prints the WindAnvil-reported verdict with UNAUTHENTICATED clearly
marked, the policy identifier, the self-digest reference, and coordination HOLD.

The input parser is bounded, rejects duplicate JSON keys, symlinks, malformed
digests, and any sighting that attempts to authorize a scan, evolution, or
coordination transition. Even a producer-claimed PASS remains a HOLD.

This module never calls a model, GitHub, an MCP tool, a remote service,
or a local execution provider. It does not establish trusted time, signers,
verifier authority, or a confirmed vulnerability.

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_windanvil.py -v
