import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from korelab.technical_knowledge import load_catalog


class RuntimePathTests(unittest.TestCase):
    def test_runtime_state_defaults_are_outside_checkout_and_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "KORELAB_DATA_HOME": directory}
            result = subprocess.run(
                [sys.executable, "-c",
                 "from korelab.paths import RUNS_DIR, KNOWLEDGE_DIR, TECHNICAL_DIR; "
                 "print(RUNS_DIR); print(KNOWLEDGE_DIR); print(TECHNICAL_DIR)"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [
                str(Path(directory) / "runs"),
                str(Path(directory) / "knowledge"),
                str(Path(directory) / "technical"),
            ])

    def test_missing_catalog_has_actionable_refresh_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "catalog.json"
            with self.assertRaisesRegex(FileNotFoundError, "technical refresh --openkore"):
                load_catalog(missing)


if __name__ == "__main__":
    unittest.main()
