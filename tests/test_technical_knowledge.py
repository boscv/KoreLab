from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from korelab.technical_knowledge import (append_confirmed_fact, build_catalog, catalog_changes,
    check_freshness, confirmed_fact_entry, objective_catalog, preserve_unrequested_components, search_catalog,
    write_catalog, load_catalog)


class TechnicalKnowledgeTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        ok = root / "openkore"
        ra = root / "rathena"
        (ok / "src").mkdir(parents=True)
        (ok / "control").mkdir()
        (ok / "plugins/eventMacro/eventMacro/Condition").mkdir(parents=True)
        (ok / "plugins/sample").mkdir(parents=True)
        (ra / "src/map").mkdir(parents=True)
        (ra / "src/custom").mkdir(parents=True)
        (ok / "src/Commands.pm").write_text(
            "\t\t['move', [\n\t\t T(\"Move your character.\"),\n"
            "\t\t [T(\"<x> <y> [<map>]\"), T(\"move to coordinates\")],\n\t\t], \\&cmdMove],\n"
            "\t\t['storage', [\n\t\t T(\"Handle storage items.\"),\n"
            "\t\t [\"add <item #> [<amount>]\", T(\"add item\")],\n\t\t], \\&cmdStorage],\n",
            encoding="utf-8")
        (ok / "control/config.txt").write_text("# local-only configuration\nusername private-name\npassword private-secret\nstorageAuto 1\nattackAuto 2\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro.pl").write_text(
            "Plugins::register('eventMacro', 'allows usage of eventMacros', \\&Unload);\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro/FileParser.pm").write_text("# parser\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro/Runner.pm").write_text("# runner\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro/Utilities.pm").write_text("# vars\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro/Condition/CurrentHP.pm").write_text("# hp\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro/Condition/NoMobNear.pm").write_text("# mobs\n", encoding="utf-8")
        (ok / "plugins/eventMacro/eventMacro/Conditiontypes").mkdir()
        (ok / "plugins/eventMacro/eventMacro/Conditiontypes/NumericConditionState.pm").write_text("# numeric\n", encoding="utf-8")
        (ok / "plugins/sample/sample.pl").write_text(
            "Plugins::register('sample', 'sample helper', \\&unload);\n"
            "Plugins::addHooks(['mainLoop_post', \\&sample_hook]);\n"
            "Commands::register(['samplecmd', 'safe example', \\&run]);\n", encoding="utf-8")
        (ra / "src/map/atcommand.cpp").write_text(
            '#define ACMD_DEF(x) { #x }\nACMD_DEF(storage)\nACMD_DEF2("where2", where)\n', encoding="utf-8")
        (ra / "src/map/script.cpp").write_text(
            '#define BUILDIN_DEF(x,args) { #x }\nBUILDIN_DEF(mes,"v*")\nBUILDIN_DEF2(warp,"warpchar","sii?")\n', encoding="utf-8")
        (ra / "src/custom/atcommand_def.inc").write_text("ACMD_DEF(korelab_probe)\n", encoding="utf-8")
        return ok, ra

    def test_extracts_registries_plugins_config_and_versioned_references_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            catalog = build_catalog(ok, ra)
            entries = {entry["id"]: entry for entry in catalog["entries"]}
            self.assertIn("openkore.command.move", entries)
            self.assertEqual(entries["openkore.command.move"]["purpose"], "Move your character.")
            self.assertEqual(entries["openkore.command.move"]["syntax_hints"], ["<x> <y> [<map>]"])
            self.assertIn("openkore.plugin.sample", entries)
            self.assertIn("openkore.plugin_command.sample.samplecmd", entries)
            self.assertIn("openkore.plugin_hook.sample.mainLoop_post", entries)
            self.assertIn("rathena.atcommand.where2", entries)
            self.assertIn("rathena.atcommand.korelab_probe", entries)
            self.assertEqual(entries["rathena.script_builtin.warpchar"]["argument_signature"], "sii?")
            self.assertIn("eventMacro", {component["component"] for component in catalog["components"]})
            self.assertIn("CurrentHP", entries["eventmacro.conditions"]["discovered_conditions"])
            self.assertIn("NumericConditionState", entries["eventmacro.conditions"]["discovered_condition_types"])
            self.assertIn("source_fingerprint", entries["openkore.command.move"]["version"])
            serialized = json.dumps(catalog)
            self.assertNotIn("private-secret", serialized)
            self.assertNotIn("private-name", serialized)
            self.assertTrue(any(ref.startswith("control/") for ref in entries["openkore.config.storageAuto"]["source_references"]))
            self.assertEqual(catalog["catalog_type"], "technical_capabilities_not_server_knowledge")
            self.assertEqual(entries["openkore.command.move"]["maturity"], "indexed")
            self.assertEqual(entries["eventmacro.commands_lifecycle"]["maturity"], "documented")
            self.assertEqual(catalog["coverage"]["maturity_counts"]["indexed"] > 0, True)

    def test_objective_search_unsupported_and_ambiguous_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            catalog = build_catalog(ok, ra)
            storage = objective_catalog(catalog, "store selected inventory items in Kafra")
            self.assertEqual(storage["status"], "MATCHED")
            self.assertEqual(storage["guides"][0]["id"], "objective.inventory_storage")
            combat = objective_catalog(catalog, "fight monsters with skills")
            self.assertEqual(combat["guides"][0]["id"], "objective.combat")
            unknown = objective_catalog(catalog, "perform an unknown planetary ritual")
            self.assertEqual(unknown["status"], "UNSUPPORTED_OR_UNKNOWN")
            self.assertEqual(unknown["knowledge_assessment"], "insufficient")
            ambiguous_objective = objective_catalog(catalog, "storage combat")
            self.assertEqual(ambiguous_objective["status"], "AMBIGUOUS")
            self.assertIn("unresolved_server_facts", ambiguous_objective["investigation"])
            found = search_catalog(catalog, "native plugin macro pause stop variable", component="eventMacro")
            self.assertTrue(found["matches"])
            ambiguous = search_catalog(catalog, "storage")
            self.assertEqual(ambiguous["status"], "AMBIGUOUS")

    def test_source_change_detection_and_refresh_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            catalog1 = build_catalog(ok, ra)
            path = ok / "src/Commands.pm"
            path.write_text(path.read_text() + "# changed\n", encoding="utf-8")
            status = check_freshness(catalog1, {"openkore": ok, "rathena": ra})
            self.assertEqual(status["status"], "STALE")
            self.assertTrue(any(s.endswith("src/Commands.pm") for s in status["stale_sources"]))
            self.assertIn("openkore.command.move", status["invalidated_entries"])
            catalog2 = build_catalog(ok, ra)
            changes = catalog_changes(catalog1, catalog2)
            self.assertIn("openkore", changes["invalidated_components"])
            self.assertEqual(check_freshness(catalog2, {"openkore": ok, "rathena": ra})["status"], "CURRENT")
            plugin_dir = ok / "plugins/newplugin"
            plugin_dir.mkdir()
            (plugin_dir / "newplugin.pl").write_text("Plugins::register('newplugin','new',\\&unload);\n", encoding="utf-8")
            self.assertIn("openkore:plugins/newplugin/newplugin.pl",
                          check_freshness(catalog2, {"openkore": ok, "rathena": ra})["new_sources"])
            partial = preserve_unrequested_components(catalog2, build_catalog(ok), {"openkore", "eventMacro"})
            self.assertIn("rathena", {c["component"] for c in partial["components"]})
            self.assertEqual(partial["coverage"]["not_refreshed_components"], ["rathena"])

    def test_local_config_fingerprint_tracks_names_without_persisting_or_hashing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            catalog = build_catalog(ok, ra)
            config = ok / "control/config.txt"
            config.write_text("username changed-name\npassword changed-secret\nstorageAuto 0\nattackAuto 1\n", encoding="utf-8")
            self.assertEqual(check_freshness(catalog, {"openkore": ok, "rathena": ra})["status"], "CURRENT")
            config.write_text(config.read_text() + "newSetting enabled\n", encoding="utf-8")
            stale = check_freshness(catalog, {"openkore": ok, "rathena": ra})
            self.assertEqual(stale["status"], "STALE")
            serialized = json.dumps(catalog)
            self.assertNotIn("private-secret", serialized)

    def test_component_revision_change_invalidates_its_catalog_entries(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            for checkout in (ok, ra):
                subprocess.run(["git", "init", "-q", str(checkout)], check=True)
                subprocess.run(["git", "-C", str(checkout), "config", "user.email", "catalog-test@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Catalog Test"], check=True)
                subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], check=True)
            catalog = build_catalog(ok, ra)
            command_file = ok / "src/Commands.pm"
            command_file.write_text(command_file.read_text() + "# source revision changed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(ok), "add", "src/Commands.pm"], check=True)
            subprocess.run(["git", "-C", str(ok), "commit", "-qm", "update"], check=True)
            status = check_freshness(catalog, {"openkore": ok, "rathena": ra})
            self.assertIn("openkore", status["revision_mismatches"])
            self.assertIn("openkore", status["invalidated_components"])
            self.assertIn("openkore.command.move", status["invalidated_entries"])

    def test_catalog_round_trip_and_append_only_confirmed_facts_preserve_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            target = write_catalog(build_catalog(ok, ra), root / "catalog.json")
            loaded = load_catalog(target)
            self.assertEqual(loaded["schema_version"], 1)
            facts = root / "technical.jsonl"
            first = append_confirmed_fact(facts, fact_id="eventmacro.stop", statement="Stops the running macro",
                source_reference="plugins/eventMacro/eventMacro.pl", version="abc123", component="eventMacro")
            second = append_confirmed_fact(facts, fact_id="eventmacro.stop", statement="Pauses the running macro",
                source_reference="plugins/eventMacro/eventMacro.pl", version="abc123", component="eventMacro")
            self.assertFalse(first["conflicts_with_existing"])
            self.assertTrue(second["conflicts_with_existing"])
            self.assertEqual(confirmed_fact_entry(first, 0)["maturity"], "documented")
            self.assertIn("knowledge_conflict", confirmed_fact_entry(second, 1))
            self.assertEqual(len(facts.read_text().splitlines()), 2)
            with self.assertRaisesRegex(ValueError, "require evidence reference"):
                append_confirmed_fact(facts, fact_id="eventmacro.runtime", statement="Does a thing",
                    source_reference="plugins/eventMacro/eventMacro.pl", version="abc123", component="eventMacro",
                    status="experimentally_verified")
            validated = append_confirmed_fact(facts, fact_id="eventmacro.runtime", statement="Moves to target",
                source_reference="plugins/eventMacro/eventMacro.pl", version="abc123", component="eventMacro",
                status="experimentally_verified", validation_evidence={"reference": "runs/probe/report.json",
                    "environment": "OpenKore abc123 fixture", "conditions": "Target coordinate observed after run"})
            self.assertEqual(confirmed_fact_entry(validated, 2)["maturity"], "validated")
            with self.assertRaises(ValueError):
                append_confirmed_fact(facts, fact_id="leak", statement="bad source ref",
                    source_reference="/private/source/file.pm", version="abc123", component="eventMacro")

    def test_cli_retrieval_uses_catalog_without_game_server_and_keeps_fact_domains_separate(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ok, ra = self.fixture(root)
            catalog_path = write_catalog(build_catalog(ok, ra), root / "catalog.json")
            fact_path = root / "technical-facts.jsonl"
            append_confirmed_fact(fact_path, fact_id="storage.add", statement="Transfers the selected inventory stack to storage",
                source_reference="src/Commands.pm", version="fixture", component="openkore")
            command = [sys.executable, "-m", "korelab", "technical", "objective",
                "store selected inventory items in Kafra", "--catalog", str(catalog_path),
                "--facts", str(fact_path)]
            result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["guides"][0]["id"], "objective.inventory_storage")
            self.assertEqual(output["confirmed_technical_facts"][0]["name"], "storage.add")
            self.assertEqual(output["knowledge_domains"], ["technical catalog", "server-scoped knowledge must be queried separately for NPC/item/map/quest facts"])
            append_confirmed_fact(fact_path, fact_id="ritual.experimental", statement="Planetary ritual uses a tested native macro command",
                source_reference="plugins/eventMacro/eventMacro/Runner.pm", version="fixture", component="eventMacro")
            unknown_command = [sys.executable, "-m", "korelab", "technical", "objective",
                "perform an unknown planetary ritual", "--catalog", str(catalog_path), "--facts", str(fact_path)]
            rerun = subprocess.run(unknown_command, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True)
            self.assertEqual(rerun.returncode, 1, rerun.stderr)
            reused = json.loads(rerun.stdout)
            self.assertEqual(reused["status"], "UNSUPPORTED_OR_UNKNOWN")
            self.assertEqual(reused["knowledge_assessment"], "known_findings_without_reviewed_guide")
            self.assertEqual(reused["confirmed_technical_facts"][0]["name"], "ritual.experimental")


if __name__ == "__main__":
    unittest.main()
