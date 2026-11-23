import os
import tempfile
import unittest
from pathlib import Path

from korelab.adapters.openkore import OBSERVER, merge_plugin_list, persist_run_local_config
from korelab.openkore_runtime import (discover_config_keys, discover_control_files,
    requires_profile_authorization, stage_control_file_overrides, stage_plugin_overlays,
    validate_runtime_profile)


class OpenKoreRuntimeProfileTests(unittest.TestCase):
    def test_persisted_runtime_config_is_atomic_private_and_run_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "case"
            profile_dir = run_dir / "control"
            profile_dir.mkdir(parents=True)
            source = root / "runtime-config.txt"
            source.write_text("route_randomWalk 1\nsynthetic_state active\n")
            target = profile_dir / "config.txt"
            persist_run_local_config(source, target, run_dir)
            self.assertEqual(target.read_text(), source.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            outside = root / "outside.txt"
            with self.assertRaisesRegex(ValueError, "under run_dir"):
                persist_run_local_config(source, outside, run_dir)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "control").mkdir()
        (self.root / "src").mkdir()
        (self.root / "plugins" / "demo").mkdir(parents=True)
        (self.root / "control/config.txt").write_text("lockMap iz_int01\nbaseOption 1\n")
        (self.root / "control/mon_control.txt").write_text("all 0 0 0\n")
        (self.root / "src/Settings.pm").write_text("our $x = $config{'sourceOption'};\n")
        (self.root / "src/functions.pl").write_text("""Settings::addControlFile(Settings::getControlFilename('mon_control.txt'),
    internalName => 'mon_control.txt', loader => sub { }, autoSearch => 0);
""")
        (self.root / "plugins/demo/plugin.pl").write_text("our $x = $config{pluginOption};\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_discovers_config_keys_and_control_files_from_this_installation(self):
        keys = discover_config_keys(self.root)
        self.assertTrue({"lockMap", "baseOption", "sourceOption", "pluginOption"}.issubset(keys))
        self.assertIn("mon_control.txt", discover_control_files(self.root))
        self.assertNotIn("other.txt", discover_control_files(self.root))

    def test_accepts_general_config_and_registered_file_overlays(self):
        profile = validate_runtime_profile(self.root, {
            "ai_mode": "on",
            "config_overrides": {"lockMap": "prt_fild01", "pluginOption": 3, "baseOption": False},
            "control_file_overrides": {"mon_control.txt": {
                "mode": "append", "content": "1002 1 0 0 0 0 0 0 0 1\n"}},
        })
        self.assertEqual(profile["ai_mode"], "on")
        self.assertEqual(profile["config_overrides"], {
            "lockMap": "prt_fild01", "pluginOption": "3", "baseOption": "0"})
        overlay = self.root / "private-control"
        manifest = stage_control_file_overrides(self.root, overlay, profile["control_file_overrides"])
        effective = (overlay / "mon_control.txt").read_text()
        self.assertTrue(effective.startswith("all 0 0 0\n"))
        self.assertIn("1002 1 0 0 0 0 0 0 0 1", effective)
        self.assertEqual(len(manifest), 1)
        self.assertEqual((overlay / "mon_control.txt").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "control/mon_control.txt").read_text(), "all 0 0 0\n")

    def test_discovers_numbered_native_autoequip_settings_from_dynamic_source(self):
        (self.root / "src/AI.pm").write_text('my $key = "equipAuto_$i"; $config{"equipAuto_$i"};\n')
        keys = discover_config_keys(self.root)
        self.assertTrue({"equipAuto_0", "equipAuto_0_rightHand", "equipAuto_0_arrow"}.issubset(keys))
        profile = validate_runtime_profile(self.root, {"config_overrides": {
            "equipAuto_0": 1, "equipAuto_0_rightHand": "Bow", "equipAuto_0_arrow": "Arrow"}})
        self.assertEqual(profile["config_overrides"]["equipAuto_0_rightHand"], "Bow")

    def test_rejects_unknown_config_keys_control_files_and_paths(self):
        with self.assertRaisesRegex(ValueError, "not found in this installation"):
            validate_runtime_profile(self.root, {"config_overrides": {"typoOption": 1}})
        with self.assertRaisesRegex(ValueError, "not registered"):
            validate_runtime_profile(self.root, {"control_file_overrides": {"unknown.txt": {
                "mode": "replace", "content": "x"}}})
        with self.assertRaisesRegex(ValueError, "basename"):
            validate_runtime_profile(self.root, {"control_file_overrides": {"../mon_control.txt": {
                "mode": "replace", "content": "x"}}})

    def test_rejects_invalid_ai_mode_and_config_line_injection(self):
        with self.assertRaisesRegex(ValueError, "ai_mode"):
            validate_runtime_profile(self.root, {"ai_mode": "automatic"})
        with self.assertRaisesRegex(ValueError, "line break"):
            validate_runtime_profile(self.root, {"config_overrides": {"lockMap": "prt_fild01\nmaster evil"}})

    def test_local_observer_extensions_are_discovered_from_their_source(self):
        profile = validate_runtime_profile(self.root,
            {"config_overrides": {"stateObserver_destination_x": 22}},
            additional_config_sources=("$config{stateObserver_destination_x}",))
        self.assertEqual(profile["config_overrides"]["stateObserver_destination_x"], "22")

    def test_selected_profile_can_declare_run_local_persistent_macro_keys(self):
        selected = self.root / "private-selected-config.txt"
        selected.write_text("lockMap iz_int01\nkorelab_workflow_phase waiting_for_observation\n")
        profile = validate_runtime_profile(self.root,
            {"config_overrides": {"korelab_workflow_phase": "waiting_for_observation"}},
            additional_config_sources=(selected.read_text(),))
        self.assertEqual(profile["config_overrides"]["korelab_workflow_phase"],
                         "waiting_for_observation")

    def test_ai_or_behavior_overlays_require_explicit_agent_authorization(self):
        self.assertFalse(requires_profile_authorization({}))
        self.assertTrue(requires_profile_authorization({"ai_mode": "on"}))
        self.assertTrue(requires_profile_authorization({"config_overrides": {"lockMap": "prt_fild01"}}))
        self.assertTrue(requires_profile_authorization({"control_file_overrides": {"mon_control.txt": {}}}))

    def test_runtime_observer_preserves_profile_plugins_and_adds_only_missing_plugins(self):
        sys_value = "loadPlugins_list eventMacro, storageHelper\nother setting\n"
        merged = merge_plugin_list(sys_value, ["stateObserver", "eventMacro"])
        self.assertIn("loadPlugins_list eventMacro,storageHelper,stateObserver", merged)
        self.assertEqual(merged.count("eventMacro"), 1)
        self.assertTrue(merged.endswith("other setting\n"))

    def test_standalone_plugin_profile_can_disable_eventmacro(self):
        sys_value = "loadPlugins 2\nloadPlugins_list macro,eventMacro,map\n"
        merged = merge_plugin_list(sys_value, ["stateObserver", "sampleObserver"],
                                   ["eventMacro"])
        self.assertIn("loadPlugins_list macro,map,stateObserver,sampleObserver", merged)
        self.assertNotIn("eventMacro", merged)
        with self.assertRaisesRegex(ValueError, "both required and disabled"):
            merge_plugin_list(sys_value, ["eventMacro"], ["eventMacro"])

    def test_structured_observer_reports_live_map_geometry_for_route_diagnostics(self):
        self.assertIn("field_dimensions", OBSERVER)
        self.assertIn("server_position_walkable", OBSERVER)
        self.assertIn("$field->isWalkable($from->{x},$from->{y})", OBSERVER)

    def test_structured_observer_exposes_native_progress_and_task_state(self):
        for field in ("base_level", "job_level", "zeny", "weight", "max_weight",
                      "base_exp", "job_exp", "skill_points", "native_ai_queue"):
            self.assertIn(f"{field}=>", OBSERVER)

    @unittest.skipUnless(Path(os.environ.get("KORELAB_OPENKORE", "/__korelab_openkore_unset__")).is_dir(),
                         "set KORELAB_OPENKORE for OpenKore source compatibility")
    def test_skill_point_observation_tracks_the_native_runtime_value(self):
        openkore = Path(os.environ["KORELAB_OPENKORE"])
        openkore_receive = (openkore / "src/Network/Receive.pm").read_text()
        eventmacro = (openkore / "plugins/eventMacro/eventMacro/Condition/FreeSkillPoints.pm").read_text()
        self.assertIn("skill_points=>(defined $char->{points_skill}", OBSERVER)
        self.assertIn("$char->{points_skill}", eventmacro)
        self.assertIn("VAR_SPPOINT", openkore_receive)
        self.assertIn("$_[0]{points_skill} = $_[1]", openkore_receive)

    def test_external_plugin_is_hash_checked_indexed_and_staged_privately(self):
        plugin_source = self.root / "lockMapSweep.pl"
        plugin_source.write_text("our $enabled = $config{lockMapSweep_enabled};\\n")
        import hashlib
        digest = hashlib.sha256(plugin_source.read_bytes()).hexdigest()
        profile = validate_runtime_profile(self.root, {
            "plugin_overlays": [{"source": str(plugin_source), "sha256": digest}],
            "config_overrides": {"lockMapSweep_enabled": 1},
        })
        self.assertTrue(requires_profile_authorization({"plugin_overlays": profile["plugin_overlays"]}))
        target_dir = self.root / "private-plugins"
        manifest = stage_plugin_overlays(target_dir, profile["plugin_overlays"])
        self.assertEqual((target_dir / "lockMapSweep.pl").read_text(), plugin_source.read_text())
        self.assertEqual(manifest[0]["sha256"], digest)
        plugin_source.write_text("changed\\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_runtime_profile(self.root, {"plugin_overlays": [
                {"source": str(plugin_source), "sha256": digest}]})

    def test_identical_installed_plugin_is_reused_without_staging_duplicate(self):
        import hashlib
        installed = self.root / "plugins" / "lockMapSweep" / "lockMapSweep.pl"
        installed.parent.mkdir(parents=True)
        installed.write_text("our $enabled = $config{lockMapSweep_enabled};\n")
        source = self.root / "lockMapSweep.pl"
        source.write_text(installed.read_text())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        profile = validate_runtime_profile(self.root, {"plugin_overlays": [
            {"source": str(source), "sha256": digest}]})
        self.assertTrue(profile["plugin_overlays"][0]["already_installed"])
        self.assertEqual(stage_plugin_overlays(self.root / "private", []), [])
        self.assertTrue(installed.is_file())

    def test_installed_plugin_name_conflict_fails_closed(self):
        import hashlib
        installed = self.root / "plugins" / "lockMapSweep" / "lockMapSweep.pl"
        installed.parent.mkdir(parents=True)
        installed.write_text("our $version = 1;\n")
        source = self.root / "lockMapSweep.pl"
        source.write_text("our $version = 2;\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "conflicts with a different installed plugin"):
            validate_runtime_profile(self.root, {"plugin_overlays": [
                {"source": str(source), "sha256": digest}]})


if __name__ == "__main__":
    unittest.main()
