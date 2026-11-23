import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from korelab.scenario import load_scenario


class ScenarioConfigTests(unittest.TestCase):
    def test_expands_local_identity_without_committing_it(self):
        data = {"identity": {"username": "${KORELAB_OPENKORE_USERNAME}",
                             "character": "${KORELAB_CHARACTER}",
                             "character_slot": "${KORELAB_CHARACTER_SLOT}"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            path.write_text(json.dumps(data))
            with patch.dict(os.environ, {"KORELAB_OPENKORE_USERNAME": "fixture-user",
                                         "KORELAB_CHARACTER": "fixture-char",
                                         "KORELAB_CHARACTER_SLOT": "0"}):
                result = load_scenario(path)
        self.assertEqual(result["identity"], {"username": "fixture-user",
                                               "character": "fixture-char",
                                               "character_slot": "0"})

    def test_missing_local_identity_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            path.write_text('{"identity":{"character":"${KORELAB_CHARACTER}"}}')
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "KORELAB_CHARACTER"):
                    load_scenario(path)


if __name__ == "__main__":
    unittest.main()
