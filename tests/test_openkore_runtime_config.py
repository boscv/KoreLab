from __future__ import annotations

import re
import time
import unittest

from korelab.adapters.openkore import (OpenKoreAdapter, native_movement_config,
                                       stage_native_profile)


class OpenKoreRuntimeConfigTests(unittest.TestCase):
    def test_native_profile_is_staged_with_private_config_and_plugin_selection_files(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runtime-config.txt"
            source.write_text("username private-account\npassword private-secret\n")
            workdir = root / "session"
            self.assertEqual(stage_native_profile(workdir, "bot001", source), workdir)
            profile = workdir / "profiles/bot001"
            self.assertEqual((profile / "config.txt").read_text(), source.read_text())
            self.assertEqual((profile / "sys.txt").read_text(), "loadPlugins_list stateObserver\n")
            self.assertEqual((profile / "config.txt").stat().st_mode & 0o777, 0o600)

    def test_native_profile_rejects_path_components(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.txt"
            source.write_text("config\n")
            with self.assertRaisesRegex(ValueError, "profile name is invalid"):
                stage_native_profile(Path(directory) / "profiles", "../outside", source)

    def test_native_movement_uses_profile_route_step_explicitly(self):
        self.assertEqual(native_movement_config({}, {"route_step": "10"}),
                         {"route_step": "10"})

    def test_native_movement_has_safe_default_when_profile_omits_route_step(self):
        self.assertEqual(native_movement_config({}, {}), {"route_step": "10"})

    def test_explicit_scenario_route_step_takes_precedence(self):
        self.assertEqual(native_movement_config({"route_step": "4"}, {"route_step": "10"}),
                         {"route_step": "4"})

    def test_native_conf_result_includes_interleaved_plugin_events(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ""
        adapter.deadline = time.monotonic() + 5
        adapter.sensitive_values = ()

        def command(value: str) -> None:
            adapter.output += (f"{value}\nConfig 'lockMap_randY' set to 0 (was 8)\n"
                               "[lockMapSweep] Invalidated pending route to 18, 32 after config change to lockMap_randY.\n")

        def wait_after(pattern: str, cursor: int, timeout: float = 30):
            match = re.search(pattern, adapter.output[cursor:], re.I | re.S)
            assert match
            return match

        adapter.command = command
        adapter._wait_after = wait_after
        adapter._read = lambda timeout=0.1: None
        result = adapter.set_config_value("lockMap_randY", "0", timeout=1)
        self.assertIn("Config 'lockMap_randY' set to 0", result["native_response"])
        self.assertEqual(len(result["plugin_events"]), 1)
        self.assertIn("Invalidated pending route to 18, 32", result["plugin_events"][0])


if __name__ == "__main__":
    unittest.main()
