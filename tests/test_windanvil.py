"""Standalone observer tests; no network, agent, or bot credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from atlas_watcher.windanvil import SightingError, read_sighting, format_sighting

SIGHTING = {
    "schema": "neonoxide.wind-anvil-sighting.v1",
    "record_digest": "sha256:" + "a" * 64,
    "observation_digest": "sha256:" + "b" * 64,
    "source_sha256": "sha256:" + "c" * 64,
    "reported_verdict": "BLOCKED",
    "policy_id": "windanvil.rizzer.native-evidence.v1",
    "record_authentication": "NOT_ESTABLISHED",
    "evolution_eligible": False,
    "scan_authorized": False,
    "coordination": "HOLD",
}


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sighting.json"

    def read(self, data):
        self.path.write_text(json.dumps(data))
        return read_sighting(self.path)

    def test_hold_is_displayed_without_authorization(self):
        report = self.read(SIGHTING)
        result = format_sighting(report)
        self.assertIn("UNAUTHENTICATED", result)
        self.assertIn("coordination HOLD", result)
        self.assertIn("no scan/evolution authorization", result)

    def test_even_claimed_pass_is_untrusted(self):
        report = self.read({**SIGHTING, "reported_verdict": "PASS"})
        output = format_sighting(report)
        self.assertIn("reported PASS (UNAUTHENTICATED)", output)
        self.assertIn("coordination HOLD", output)

    def test_untrusted_upgrade_attempt_rejected(self):
        for change in ({"coordination": "RUN"}, {"scan_authorized": True},
                       {"evolution_eligible": True}, {"record_authentication": "VERIFIED"}):
            with self.subTest(change=change), self.assertRaises(SightingError):
                self.read({**SIGHTING, **change})

    def test_duplicate_key_and_symlink_rejected(self):
        self.path.write_text('{"schema":"x","schema":"y"}')
        with self.assertRaisesRegex(SightingError, "duplicate"):
            read_sighting(self.path)
        self.path.write_text(json.dumps(SIGHTING))
        link = Path(self.tmp.name) / "link.json"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(SightingError, "symlink"):
            read_sighting(link)

    def test_cli_is_readonly_and_outputs_hold(self):
        self.path.write_text(json.dumps(SIGHTING))
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root / "src")}
        result = subprocess.run(
            [sys.executable, "-m", "atlas_watcher.windanvil", str(self.path)],
            text=True, capture_output=True, check=True, env=env,
        )
        self.assertIn("UNAUTHENTICATED", result.stdout)
        self.assertIn("coordination HOLD", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertTrue(self.path.is_file())


if __name__ == "__main__":
    unittest.main()
