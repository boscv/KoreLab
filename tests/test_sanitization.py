import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from korelab.adapters.openkore import redact
from korelab.runner import PERL_BUILD_KEYS, preflight_perl
from korelab.openkore_profile import read_openkore_credentials


class RedactionTests(unittest.TestCase):
    def test_removes_identifiers_after_terminal_color_prefixes(self):
        sample = (
            "\x1b[0;32mAccount ID: 9876543210 DE AD BE EF\x1b[0m\n"
            "\x1b[0;32mSession ID: 1234567890 AA BB CC DD\x1b[0m\n"
            "\x1b[0;32mSessionID2: 2345678901 11 22 33 44\x1b[0m\n"
            "\x1b[0;32mChar ID: 87654321\x1b[0m\n"
            "password secret-value\n"
        )
        safe = redact(sample)
        for secret in ("9876543210", "DE AD BE EF", "1234567890", "2345678901",
                       "87654321", "secret-value"):
            self.assertNotIn(secret, safe)
        self.assertEqual(safe.count("[REDACTED]"), 5)

    def test_redacts_locally_configured_identity_values(self):
        safe = redact("master=Local Test username=lab-user character=SyntheticCharacter",
                      ("Local Test", "lab-user", "SyntheticCharacter"))
        self.assertEqual(safe, "master=[REDACTED] username=[REDACTED] character=[REDACTED]")

    def test_short_config_secret_does_not_corrupt_map_names_or_coordinates(self):
        safe = redact("iz_int01 (20,26) stats 131; secure token=1", ("1",))
        self.assertEqual(safe, "iz_int01 (20,26) stats 131; secure token=[REDACTED]")

    def test_secrets_read_from_profile_config_are_redacted_without_being_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.txt"
            config.write_text("username lab-user\npassword 000000\nsecureAdminPassword 1\n", encoding="utf-8")
            settings, secrets = read_openkore_credentials(config)
            self.assertEqual(settings["username"], "lab-user")
            safe = redact("username lab-user password 000000 enabled=1 map iz_int01", secrets + (settings["username"],))
            self.assertNotIn("lab-user", safe)
            self.assertNotIn("password 000000", safe)
            self.assertIn("enabled=1 map iz_int01", safe)


class PerlPreflightTests(unittest.TestCase):
    def test_perl_abi_preflight_uses_bundle_marker_without_starting_openkore(self):
        built = {
            "version": "5.44.0", "archname": "darwin-thread-multi-2level",
            "useithreads": "define", "ptrsize": "8", "byteorder": "12345678",
            "cc": "clang", "ccflags": "-fno-common", "lddlflags": "-bundle -undefined dynamic_lookup",
        }
        output = "\n".join(f"{key}={built[key]}" for key in PERL_BUILD_KEYS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auto = root / "src/auto/XSTools"
            auto.mkdir(parents=True)
            (auto / ".perl-config").write_text(output + "\n")
            (auto / "XSTools.bundle").write_bytes(b"fixture")
            with patch.dict(os.environ, {"KORELAB_PERL": "/fixture/perl"}), \
                    patch("korelab.runner.subprocess.run",
                          return_value=CompletedProcess([], 0, stdout=output, stderr="")):
                self.assertEqual(preflight_perl(root), Path("/fixture/perl"))


if __name__ == "__main__":
    unittest.main()
