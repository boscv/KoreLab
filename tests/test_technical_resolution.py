from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import unittest

from korelab.technical_resolution import assess_operation_evidence, resolve_agent_action


class TechnicalResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.ok = self.root / "openkore"
        (self.ok / "src").mkdir(parents=True)
        source = self.ok / "src/Commands.pm"
        source.write_text("# command move\nsub cmdMove { return }\n", encoding="utf-8")
        self.source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        self.catalog_path = self.root / "catalog.json"
        self.facts_path = self.root / "facts.jsonl"
        self.catalog = {"schema_version": 1, "generated_at": "fixture",
            "components": [{"component": "openkore", "revision": "abc123", "sources": [
                {"path": "src/Commands.pm", "sha256": self.source_hash, "fingerprint_type": "source_content"}]}],
            "entries": [
                {"id": "objective.npc_quest", "component": "openkore", "kind": "objective_guide",
                 "name": "NPC interaction", "maturity": "documented", "syntax": ["move <x> <y>"],
                 "source_references": ["src/Commands.pm#1"], "version": {"revision": "abc123"}},
                {"id": "openkore.command.move", "component": "openkore", "kind": "native_command",
                 "name": "move", "maturity": "indexed", "syntax_hints": ["<x> <y>"],
                 "source_references": ["src/Commands.pm#2"], "version": {"revision": "abc123"}}
            ]}
        self.write_catalog()

    def tearDown(self):
        self.temp.cleanup()

    def write_catalog(self):
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")

    def resolve(self, **kwargs):
        return resolve_agent_action("move", catalog_path=self.catalog_path, facts_path=self.facts_path,
            openkore_root=self.ok, supplied_arguments={"map": "prontera", "x": 10, "y": 20}, **kwargs)

    def test_documented_guide_resolves_only_with_known_contract_and_exact_source_excerpt(self):
        result = self.resolve()
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(result["knowledge_assessment"], "documented_contract")
        self.assertEqual(result["arguments_known"], {"map": "prontera", "x": 10, "y": 20})
        self.assertIn("src/Commands.pm#1", [row["reference"] for row in result["source_checks"]])
        self.assertIn("# command move", result["source_excerpts_for_investigation"][0]["excerpt"])

    def test_indexed_only_gap_does_not_invent_syntax(self):
        self.catalog["entries"] = [self.catalog["entries"][1]]
        self.write_catalog()
        result = self.resolve()
        self.assertEqual(result["status"], "INVESTIGATION_REQUIRED")
        self.assertIn("inspect the listed source excerpts", result["investigation"]["next"])
        self.assertEqual(result["arguments_known"], {"map": "prontera", "x": 10, "y": 20})
        self.assertNotIn("command", result["arguments_known"])

    def test_duplicate_capability_ids_are_ambiguous(self):
        self.catalog["entries"].append(dict(self.catalog["entries"][1]))
        self.write_catalog()
        result = self.resolve()
        self.assertIn("openkore.command.move", result["ambiguous_resource_ids"])
        self.assertEqual(result["status"], "INVESTIGATION_REQUIRED")

    def test_changed_source_invalidates_resolution(self):
        (self.ok / "src/Commands.pm").write_text("# edited after catalog refresh\n", encoding="utf-8")
        result = self.resolve()
        self.assertEqual(result["status"], "INVESTIGATION_REQUIRED")
        self.assertIn("src/Commands.pm#1", result["changed_or_unindexed_sources"])
        self.assertEqual(result["source_checks"][0]["source_status"], "CHANGED_OR_UNINDEXED")

    def test_conflicting_confirmed_fact_is_never_selected_silently(self):
        self.facts_path.write_text("\n".join([
            json.dumps({"fact_id": "move.contract", "statement": "Moves by coordinates", "component": "openkore",
                "source_reference": "src/Commands.pm#1", "version": "abc123", "status": "source_confirmed",
                "conflicts_with_existing": False}),
            json.dumps({"fact_id": "move.contract", "statement": "Uses map name only", "component": "openkore",
                "source_reference": "src/Commands.pm#1", "version": "abc123", "status": "source_confirmed",
                "conflicts_with_existing": True})]) + "\n", encoding="utf-8")
        result = self.resolve()
        self.assertIn("move.contract", result["conflicts"])
        self.assertEqual(result["status"], "INVESTIGATION_REQUIRED")
        self.assertEqual(len(result["confirmed_findings"]), 2)

    def test_missing_action_resource_stays_an_investigation_lead(self):
        self.catalog["entries"] = []
        self.write_catalog()
        result = self.resolve()
        self.assertEqual(result["status"], "INVESTIGATION_REQUIRED")
        self.assertIn("objective.npc_quest", result["unresolved_resource_ids"])

    def test_version_matched_confirmed_contract_reuses_an_indexed_capability(self):
        from korelab.technical_knowledge import append_confirmed_fact
        self.catalog["entries"] = [self.catalog["entries"][1]]
        self.write_catalog()
        append_confirmed_fact(self.facts_path, fact_id="openkore.command.move",
            statement="Move accepts integer map coordinates", source_reference="src/Commands.pm#2",
            version="abc123", component="openkore", status="source_confirmed",
            arguments={"x": "horizontal coordinate", "y": "vertical coordinate"},
            preconditions=["target map is reachable"])
        result = resolve_agent_action("command", catalog_path=self.catalog_path, facts_path=self.facts_path,
            openkore_root=self.ok, operation="move", supplied_arguments={"x": 10, "y": 20})
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(result["confirmed_argument_contracts"]["openkore.command.move"]["version"], "abc123")

    def test_investigated_unindexed_command_can_be_reused_after_source_hash_and_contract_are_recorded(self):
        from korelab.technical_knowledge import append_confirmed_fact
        source = self.ok / "src/Commands.pm"
        append_confirmed_fact(self.facts_path, fact_id="openkore.command.custom",
            statement="The custom command accepts a listed item index", source_reference="src/Commands.pm#1",
            version="abc123", component="openkore", status="source_confirmed",
            arguments={"item_index": "index in the active item list"},
            preconditions=["the item list has been observed"], source_sha256=self.source_hash)
        result = resolve_agent_action("command", catalog_path=self.catalog_path, facts_path=self.facts_path,
            openkore_root=self.ok, operation="custom", supplied_arguments={"operation": "custom"})
        self.assertEqual(result["status"], "RESOLVED")
        self.assertIn("openkore.command.custom", result["confirmed_argument_contracts"])
        source.write_text("# source changed\n", encoding="utf-8")
        changed = resolve_agent_action("command", catalog_path=self.catalog_path, facts_path=self.facts_path,
            openkore_root=self.ok, operation="custom", supplied_arguments={"operation": "custom"})
        self.assertEqual(changed["status"], "INVESTIGATION_REQUIRED")
        self.assertIn("src/Commands.pm#1", changed["changed_or_unindexed_sources"])

    def test_wait_reconnect_uses_versioned_source_and_runtime_confirmed_contract(self):
        from korelab.technical_knowledge import append_confirmed_fact
        source = self.ok / "src/DirectConnection.pm"
        source.write_text("sub reconnect { return }\n", encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        append_confirmed_fact(self.facts_path, fact_id="openkore.network.reconnect_after_disconnect",
            statement="OpenKore retries its native connection after a recoverable disconnect",
            source_reference="src/DirectConnection.pm#1", version="abc123", component="openkore",
            status="experimentally_verified", arguments={"dcOnDisconnect": "must allow retry"},
            preconditions=["the same OpenKore process remains alive"], source_sha256=source_hash,
            validation_evidence={"reference": "runs/reconnect/report.json", "environment": "OpenKore abc123",
                                 "conditions": "map server restarted"})
        resolved = resolve_agent_action("wait-reconnect", catalog_path=self.catalog_path,
            facts_path=self.facts_path, openkore_root=self.ok)
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(resolved["knowledge_assessment"], "documented_contract")
        contract = resolved["confirmed_argument_contracts"]["openkore.network.reconnect_after_disconnect"]
        self.assertEqual(contract["version"], "abc123")
        self.assertEqual(resolved["references"][0]["verification_status"], "experimentally_verified")
        self.assertEqual(resolved["source_checks"][0]["source_status"], "CURRENT")

    def test_config_set_resolves_against_installed_native_conf_implementation(self):
        from korelab.technical_knowledge import append_confirmed_fact
        source = self.ok / "src/Commands.pm"
        source.write_text("# native conf implementation\nsub cmdConf { return }\n", encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        self.catalog["components"][0]["sources"][0]["sha256"] = source_hash
        self.write_catalog()
        append_confirmed_fact(self.facts_path, fact_id="openkore.command.conf",
            statement="conf changes runtime configuration through configModify",
            source_reference="src/Commands.pm#1", version="abc123", component="openkore",
            status="source_confirmed", arguments={"syntax": "conf <key> <value>"},
            preconditions=["active managed session"], side_effects="runs config hooks",
            source_sha256=source_hash)
        resolved = resolve_agent_action("config-set", catalog_path=self.catalog_path,
            facts_path=self.facts_path, openkore_root=self.ok,
            supplied_arguments={"key": "test_option", "value": 1})
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertIn("openkore.command.conf", resolved["confirmed_argument_contracts"])
        self.assertEqual(resolved["source_checks"][0]["source_status"], "CURRENT")

    def test_chat_echo_after_macro_dispatch_is_not_successful_atcommand_execution(self):
        evidence = assess_operation_evidence(dispatched=True, server_response="public_chat_echo")
        self.assertEqual(evidence["evidence_level"], "dispatched_only")
        self.assertFalse(evidence["success"])
        acknowledged = assess_operation_evidence(dispatched=True, server_response="handler_success")
        self.assertEqual(acknowledged["evidence_level"], "server_acknowledged")
        self.assertFalse(acknowledged["success"])
        observed = assess_operation_evidence(dispatched=True, server_response="handler_success",
                                             observed_effect=True)
        self.assertEqual(observed["evidence_level"], "state_effect_observed")
        self.assertTrue(observed["success"])
        persisted = assess_operation_evidence(dispatched=True, server_response="handler_success",
                                              observed_effect=True, persisted_effect=True)
        self.assertEqual(persisted["evidence_level"], "persisted_state_verified")

    def test_persisted_state_cannot_be_claimed_without_observed_effect(self):
        with self.assertRaisesRegex(ValueError, "requires an independently observed effect"):
            assess_operation_evidence(dispatched=True, persisted_effect=True)

    def test_eventmacro_action_resolution_includes_native_do_dispatch_contract(self):
        from korelab.technical_knowledge import append_confirmed_fact
        plugin_source = self.ok / "plugins/eventMacro/eventMacro/Runner.pm"
        plugin_source.parent.mkdir(parents=True)
        plugin_source.write_text("sub parse_do { return queue_native_command() }\n", encoding="utf-8")
        source_hash = hashlib.sha256(plugin_source.read_bytes()).hexdigest()
        artifact = self.root / "macro.txt"
        artifact.write_text("macro sample {\n do c @monster ROCKER 1\n}\n", encoding="utf-8")
        self.catalog["components"].append({"component": "eventMacro", "revision": "abc123", "sources": []})
        self.write_catalog()
        append_confirmed_fact(self.facts_path, fact_id="eventmacro.command.do",
            statement="do queues an expanded native OpenKore command; later success needs separate evidence",
            source_reference="eventMacro/Runner.pm#1", version="abc123", component="eventMacro",
            status="source_confirmed", arguments={"command": "one parsed native command"},
            preconditions=["macro is running"], source_sha256=source_hash)
        result = resolve_agent_action("eventmacro", catalog_path=self.catalog_path,
            facts_path=self.facts_path, openkore_root=self.ok, artifact=artifact)
        self.assertIn("eventmacro.command.do", result["confirmed_argument_contracts"])
        self.assertIn("eventmacro.command.do", result["decision_evidence"]["resource_ids"])


if __name__ == "__main__":
    unittest.main()
