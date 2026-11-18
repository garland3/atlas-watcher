"""Optional, read-only WindAnvil observation display.

Consumes NeonOxide sighting v1; it does not verify producer signatures, grant
authority, post comments, run agents, or influence review triage.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

SCHEMA = "neonoxide.wind-anvil-sighting.v1"
FIELDS = {
    "schema", "record_digest", "observation_digest", "source_sha256",
    "reported_verdict", "policy_id", "record_authentication",
    "evolution_eligible", "scan_authorized", "coordination",
}
SHA = re.compile(r"sha256:[a-f0-9]{64}\Z")
POLICY = re.compile(r"windanvil\.rizzer\.[a-z0-9_.-]{1,100}\Z")
LIMIT = 16 * 1024


class SightingError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SightingError("duplicate sighting field")
        result[key] = value
    return result


def _read(path: Path) -> bytes:
    path = path.absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise SightingError("symlinked sighting path")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= LIMIT:
            raise SightingError("invalid sighting size or type")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise SightingError("sighting exceeds size bound")
        return data
    finally:
        os.close(fd)


def read_sighting(path: str | Path) -> dict:
    try:
        data = json.loads(_read(Path(path)).decode("utf-8"),
                          object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SightingError("malformed sighting JSON") from exc
    if type(data) is not dict or set(data) != FIELDS:
        raise SightingError("unsupported sighting fields")
    if data["schema"] != SCHEMA:
        raise SightingError("unsupported sighting schema")
    for field in ("record_digest", "observation_digest", "source_sha256"):
        if type(data[field]) is not str or not SHA.fullmatch(data[field]):
            raise SightingError("invalid sighting digest")
    if (type(data["policy_id"]) is not str or
            not POLICY.fullmatch(data["policy_id"])):
        raise SightingError("unsupported WindAnvil policy")
    if data["reported_verdict"] not in ("PASS", "FAIL", "BLOCKED"):
        raise SightingError("invalid reported verdict")
    if (data["record_authentication"] != "NOT_ESTABLISHED" or
            data["coordination"] != "HOLD" or
            data["evolution_eligible"] is not False or
            data["scan_authorized"] is not False):
        raise SightingError("untrusted sighting attempts to grant authority")
    return data


def format_sighting(sighting: dict) -> str:
    """No health or authorization state is inferred from an unsigned record."""
    return (
        "WindAnvil reported " + sighting["reported_verdict"] +
        " (UNAUTHENTICATED); coordination HOLD; no scan/evolution authorization\n" +
        "Policy: " + sighting["policy_id"] + "\n" +
        "Record: " + sighting["record_digest"]
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sighting", type=Path)
    args = parser.parse_args(argv)
    try:
        sighting = read_sighting(args.sighting)
    except (SightingError, OSError) as exc:
        parser.error(str(exc))
    print(format_sighting(sighting))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
