from __future__ import annotations

import json
import hashlib
import os
import pathlib
import re
import queue
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable

from ..openkore_runtime import (stage_control_file_overrides, stage_plugin_overlays,
                                validate_runtime_profile)
from ..server_tables import native_table_search_path, stage_portal_los_cache
from ..platform_support import set_private_permissions
from ..paths import USER_DATA_DIR

if os.name != "nt":
    import pty


class UnexpectedCharacterDeath(RuntimeError):
    """Raised when a live scenario observes death outside an expected combat step."""


def merge_plugin_list(sys_value: str, required_plugins: list[str],
                      disabled_plugins: list[str] | None = None) -> str:
    """Keep profile plugins enabled while adding KoreLab's observers."""
    match = re.search(r"(?m)^loadPlugins_list\s+([^\r\n]*)", sys_value)
    existing = []
    if match:
        existing = [name.strip() for name in match.group(1).split(",") if name.strip()]
    disabled = {name.casefold() for name in (disabled_plugins or [])}
    required = {name.casefold() for name in required_plugins}
    if disabled & required:
        raise ValueError("a plugin cannot be both required and disabled")
    existing = [name for name in existing if name.casefold() not in disabled]
    seen = {name.casefold() for name in existing}
    for name in required_plugins:
        if name.casefold() not in seen:
            existing.append(name)
            seen.add(name.casefold())
    setting = f"loadPlugins_list {','.join(existing)}"
    if match:
        return sys_value[:match.start()] + setting + sys_value[match.end():]
    return sys_value.rstrip() + "\n" + setting + "\n"


def stage_native_profile(profile_home: pathlib.Path, profile_name: str,
                         config_source: pathlib.Path) -> pathlib.Path:
    """Stage a private native profiles-plugin directory and return its cwd."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", profile_name):
        raise ValueError("native OpenKore profile name is invalid")
    profile_dir = profile_home / "profiles" / profile_name
    profile_dir.mkdir(parents=True, mode=0o700)
    shutil.copyfile(config_source, profile_dir / "config.txt")
    set_private_permissions(profile_dir / "config.txt", 0o600)
    (profile_dir / "sys.txt").write_text("loadPlugins_list stateObserver\n", encoding="utf-8")
    set_private_permissions(profile_dir / "sys.txt", 0o600)
    return profile_home


OBSERVER = r'''package stateObserver;
use strict; use warnings;
use Plugins; use Globals qw(%config $field $char $net %monsters @monstersID %items @itemsID $npcsList @skillsID @ai_seq); use Utils qw(calcPosition actorFinishedMovement);
use Skill;
use Network; use Time::HiRes qw(time); use JSON::PP (); use POSIX qw(strftime);
my ($fh, $last_sample, $inventory_observed, $session_generation, $last_in_game,
    $auto_candidate_count, $best_target_candidate_count);
my $last_confirmed_skill_time = 0;
$session_generation=0;
Plugins::register('stateObserver', 'KoreLab structured state observer', sub { close $fh if $fh; });
Plugins::addHooks(['mainLoop_post', \&sample_state],
  ['Network::stateChanged', \&sample_network_state],
  ['packet/item_list_end', \&sample_inventory_list_end],
  ['equipped_item', \&sample_state],
  ['unequipped_item', \&sample_state],
  ['checkMonsterAutoAttack', \&observe_auto_attack_candidate],
  ['getBestTarget', \&observe_best_target_candidates]);
sub observe_auto_attack_candidate {
    my ($hook,$args)=@_; $auto_candidate_count++;
}
sub observe_best_target_candidates {
    my ($hook,$args)=@_; my $targets=$args->{possibleTargets}||[];
    $best_target_candidate_count += scalar(@$targets);
}
Plugins::addHooks(['npc_talk_responses', \&sample_dialogue]);
Plugins::addHooks(['item_appeared', \&sample_state]);
Plugins::addHook('packet_pre/sendSkillUse', \&observe_skill_use);
sub observe_skill_use {
    my ($hook,$args)=@_;
    return unless defined($args->{ID}) && $args->{ID} =~ /^\d+$/;
    my $path=$config{stateObserver_file}; return unless $path;
    open my $out,'>>:encoding(UTF-8)',$path or die $!;
    print {$out} JSON::PP->new->canonical->encode({type=>'skill_use_request',
      skill_id=>0+$args->{ID},skill_level=>0+($args->{lv}||0),
      time=>strftime('%Y-%m-%dT%H:%M:%SZ',gmtime())}),"\n";
    close $out;
}
sub sample_dialogue {
    my ($hook, $args)=@_;
    my $path=$config{stateObserver_file}; return unless $path;
    open my $out,'>>:encoding(UTF-8)',$path or die $!;
    print {$out} JSON::PP->new->canonical->encode({type=>'dialogue',name=>$args->{name},
      responses=>$args->{responses},time=>strftime('%Y-%m-%dT%H:%M:%SZ',gmtime())}),"\n";
    close $out;
}
sub update_session_state {
    my $state=$net ? $net->getState() : undef;
    my $in_game=defined($state) && $state==Network::IN_GAME() ? 1 : 0;
    my $changed=!defined($last_in_game) || $last_in_game != $in_game;
    if ($changed) {
      $session_generation++ if $in_game;
      $inventory_observed=0;
      $last_in_game=$in_game;
    }
    return ($in_game,$changed);
}
sub sample_network_state {
    update_session_state();
    sample_state(undef,undef,1);
}
sub sample_inventory_list_end {
    my ($hook,$args)=@_;
    my ($in_game)=update_session_state();
    $inventory_observed=1 if $in_game && defined($args->{type}) && $args->{type}==0;
    sample_state(undef,undef,1);
}
sub sample_state {
    my ($hook,$args,$force)=@_;
    return unless $char;
    my ($in_game,$session_changed)=update_session_state();
    my $now=time;
    return if !$force && !$session_changed && defined($last_sample) && $now-$last_sample < 0.2;
    $last_sample=$now;
    my $state=$net ? $net->getState() : undef;
    my $map=$field && eval {$field->can('baseName')} ? eval {$field->baseName()} : undef;
    my $from=$char->{pos}; my $to=$char->{pos_to};
    my $estimate=eval { calcPosition($char,0,1) };
    my $moving=eval { !actorFinishedMovement($char,undef,0,1) };
    my @nearby;
    for my $index (0..$#monstersID) {
      my $id=$monstersID[$index]; next unless defined($id) && length($id);
      my $mob=$monsters{$id}; next unless $mob;
      my $mon_control=eval { Misc::mon_control($mob->name,$mob->{nameID}) } || {};
      push @nearby,{client_index=>$index,id=>unpack('H*',$id),name=>$mob->name,
        position=>$mob->{pos_to},damage_to=>$mob->{dmgTo}||0,damage_from=>$mob->{dmgFrom}||0,
        damage_from_you=>$mob->{dmgFromYou}||0,damage_to_you=>$mob->{dmgToYou}||0,
        attacks_from_you=>$mob->{numAtkFromYou}||0,
        mon_control_attack_auto=>0+($mon_control->{attack_auto}||0)};
    }
    my @nearby_npcs;
    my $npc_items=$npcsList ? $npcsList->getItems() : [];
    for my $index (0..$#$npc_items) {
      my $npc=$npc_items->[$index]; next unless $npc;
      push @nearby_npcs,{client_index=>$index,name=>$npc->name,
        name_id=>0+($npc->{nameID}||0),position=>$npc->{pos_to}};
    }
    my @inventory;
    my $inventory_items=$char->inventory ? $char->inventory->getItems() : [];
    for my $item (@{$inventory_items}) {
      next unless $item && $item->{nameID};
      push @inventory,{item_id=>0+$item->{nameID},amount=>0+($item->{amount}||0),
        equipped=>0+($item->{equipped}||0),client_index=>0+($item->{binID}||0),name=>$item->{name}};
    }
    my @ground_items;
    for my $index (0..$#itemsID) {
      my $id=$itemsID[$index]; next unless defined($id) && length($id);
      my $item=$items{$id}; next unless $item && $item->{nameID};
      push @ground_items,{client_index=>$index,item_id=>0+$item->{nameID},amount=>0+($item->{amount}||1),
        name=>$item->{name},position=>$item->{pos_to}};
    }
    my %character_skills;
    for my $handle (keys %{$char->{skills} || {}}) {
      my $entry=$char->{skills}{$handle}; next unless $entry;
      my $skill=Skill->new(handle=>$handle); my $idn=$skill->getIDN(); next unless defined($idn);
      $character_skills{"$idn"}=$entry->{lv};
    }
    # last_skill_used/time are set when OpenKore receives the corresponding
    # server skill-use packet. This works for packet senders that bypass the
    # optional packet_pre/sendSkillUse hook.
    my $skill_time=$char->{last_skill_time}||0;
    if ($skill_time && $skill_time != $last_confirmed_skill_time
        && defined($char->{last_skill_used}) && $char->{last_skill_used} =~ /^\d+$/) {
      my $skill_id=0+$char->{last_skill_used};
      print {$fh} JSON::PP->new->canonical->encode({type=>'skill_use_confirmed',
        skill_id=>$skill_id,time=>strftime('%Y-%m-%dT%H:%M:%SZ',gmtime())}),"\n";
      $last_confirmed_skill_time=$skill_time;
    }
    my $native_attack_target;
    if (AI::action() eq 'attack') {
      my $attack_args=AI::args(); my $attack_id=$attack_args ? $attack_args->{ID} : undef;
      my $attack_mob=defined($attack_id) ? $monsters{$attack_id} : undef;
      $native_attack_target={id=>defined($attack_id)?unpack('H*',$attack_id):undef,
        name=>$attack_mob ? $attack_mob->name : undef,
        position=>$attack_mob ? $attack_mob->{pos_to} : undef,
        target_present=>$attack_mob?JSON::PP::true:JSON::PP::false};
    }
    if (!$fh) { my $path=$config{stateObserver_file}; return unless $path; open($fh,'>>:encoding(UTF-8)',$path) or die $!; select((select($fh),$|=1)[0]); }
    print {$fh} JSON::PP->new->canonical->encode({type=>'state',time=>strftime('%Y-%m-%dT%H:%M:%SZ',gmtime()),
      connected_in_game=>(defined($state) && $state==Network::IN_GAME())?JSON::PP::true:JSON::PP::false,
      session_generation=>0+$session_generation,
      inventory_observed=>$inventory_observed?JSON::PP::true:JSON::PP::false,
      map=>$map, server_packet_position_from=>$from, server_packet_position_to=>$to,
      client_estimated_position=>$estimate,
      field_dimensions=>($field?{width=>0+($field->{width}||0),height=>0+($field->{height}||0)}:undef),
      server_position_walkable=>($field && $from && defined($from->{x}) && defined($from->{y})
        ? ($field->isWalkable($from->{x},$from->{y})?JSON::PP::true:JSON::PP::false)
        : JSON::PP::null),
      character_attributes=>{(map { $_ => $char->{$_} } qw(str agi vit int dex luk hp sp)),
        max_hp=>$char->{hp_max},max_sp=>$char->{sp_max},
        base_level=>0+($char->{lv}||0),job_level=>0+($char->{lv_job}||0),
        zeny=>0+($char->{zeny}||0),weight=>0+($char->{weight}||0),
        max_weight=>0+($char->{weight_max}||0),
        base_exp=>0+($char->{exp}||0),job_exp=>0+($char->{exp_job}||0),
        is_dead=>($char->{dead}?JSON::PP::true:JSON::PP::false),
        # eventMacro's FreeSkillPoints condition and OpenKore's current
        # post-creation packet state use points_skill. skill_point is the
        # character-create/account snapshot field and can remain stale after
        # native skill allocation in a running session.
        class=>$char->{jobID},skill_points=>(defined $char->{points_skill}
          ? $char->{points_skill} : $char->{skill_point})},
      character_skills=>\%character_skills,
      native_ai_queue=>[ @ai_seq ],
      native_ai_idle=>AI::isIdle()?JSON::PP::true:JSON::PP::false,
      field_is_city=>($field && eval {$field->isCity()})?JSON::PP::true:JSON::PP::false,
      field_raw_map_loaded=>($field && length($field->{rawMap}||''))?JSON::PP::true:JSON::PP::false,
      native_attack_target=>$native_attack_target,
      native_auto_attack_candidate_count=>0+($auto_candidate_count||0),
      native_best_target_candidate_count=>0+($best_target_candidate_count||0),
      native_ai_config=>{state=>0+AI::state(),
        effective_attack_auto=>0+(eval { Misc::getAttackAutoMode() }||0),
        attack_auto=>0+($config{attackAuto}||0),
        attack_auto_route_to_lock=>0+($config{attackAuto_routeToLock}||0),
        attack_auto_out_of_lock=>0+($config{attackAuto_outOfLock}||0),
        attack_auto_only_when_safe=>0+($config{attackAuto_onlyWhenSafe}||0),
        attack_auto_not_in_town=>0+($config{attackAuto_notInTown}||0),
        attack_distance=>0+($config{attackDistance}||0),
        attack_distance_auto=>0+($config{attackDistanceAuto}||0),
        attack_max_distance=>0+($config{attackMaxDistance}||0),
        route_random_walk=>0+($config{route_randomWalk}||0),
        route_random_walk_in_lock_only=>0+($config{route_randomWalk_inLockOnly}||0),
        route_step=>0+($config{route_step}||0),
        portal_compile=>0+($config{portalCompile}||0),
        lock_map=>$config{lockMap},lock_x=>0+($config{lockMap_x}||0),
        lock_y=>0+($config{lockMap_y}||0)},
      escape_destination_walkable=>($field && defined($config{stateObserver_destination_x})
        && defined($config{stateObserver_destination_y})
        ? ($field->isWalkable($config{stateObserver_destination_x},
           $config{stateObserver_destination_y})?JSON::PP::true:JSON::PP::false)
        : JSON::PP::null),
      combat_stats=>{map { $_ => $char->{$_} } qw(attack attack_bonus def def_bonus flee hit attack_speed)},
      nearby_monsters=>\@nearby,
      nearby_npcs=>\@nearby_npcs,
      nearby_ground_items=>\@ground_items,
      inventory=>\@inventory,
      movement_state=>!defined($moving)?'unknown':$moving?'moving':'stationary'}),"\n";
    $auto_candidate_count=0; $best_target_candidate_count=0;
}
1;
'''


CHARACTER_LIST_SYNC_GATE = r'''package KoreLabCharacterListSync;
use strict; use warnings;
use Globals qw(@chars %charSvrSet $net $messageSender); use Plugins; use Network;
Plugins::register('KoreLabCharacterListSync', 'Wait for all rAthena character pages before selection', sub { });
my $selection_sent = 0;
my $entered_game = 0;
Plugins::addHook('Network::stateChanged', sub {
    my $state = $net->getState();
    if ($state == 3) {
        $entered_game = 1;
    } elsif ($state == 2 && $entered_game) {
        $selection_sent = 0;
        $entered_game = 0;
    }
});
Plugins::addHook('charSelectScreen', sub {
    my ($hook, $args) = @_;
    my $complete = defined $charSvrSet{sync_Count}
        && defined $charSvrSet{sync_received_characters}
        && $charSvrSet{sync_received_characters} >= $charSvrSet{sync_Count};
    unless ($complete) { $args->{pin_return} = 2; return; }
    my $slot = __KORELAB_CHARACTER_SLOT__;
    unless ($chars[$slot] && !$selection_sent) { $args->{pin_return} = 2; return; }
    $selection_sent = 1;
    $messageSender->sendCharLogin($slot);
    $args->{pin_return} = 1;
});
1;
'''


def config_values(path: pathlib.Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            key, _, value = line.partition(" ")
            values[key] = value.strip()
    return values


def persist_run_local_config(source: pathlib.Path, destination: pathlib.Path,
                             run_root: pathlib.Path) -> None:
    """Atomically persist one private OpenKore config inside its declared run directory."""
    root = run_root.expanduser().resolve()
    target = destination.expanduser().resolve()
    if not target.is_relative_to(root):
        raise ValueError("persistent runtime config must remain under run_dir")
    if not source.is_file() or not target.parent.is_dir():
        raise RuntimeError("persistent runtime config source or destination is unavailable")
    fd, staged_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=target.parent)
    staged = pathlib.Path(staged_name)
    try:
        if os.name == "nt":
            set_private_permissions(staged, 0o600)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        os.replace(staged, target)
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def position(record: dict, prefer_estimate: bool = True) -> dict[str, int] | None:
    keys = ("client_estimated_position", "server_packet_position_to", "server_packet_position_from")
    if not prefer_estimate:
        keys = tuple(reversed(keys))
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict) and type(value.get("x")) is int and type(value.get("y")) is int:
            return {"x": value["x"], "y": value["y"]}
    return None


def inventory_counts(items: list[dict]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in items:
        item_id = item.get("item_id")
        amount = item.get("amount")
        if isinstance(item_id, int) and isinstance(amount, int):
            counts[item_id] = counts.get(item_id, 0) + amount
    return counts


def render_private_server_profile(profile: dict) -> str:
    """Render a bounded private server entry, including per-map field overrides."""
    required = {"name", "host", "port", "server_type", "table_folders"}
    allowed = required | {"fields_folder", "field_overrides"}
    if (not isinstance(profile, dict) or not required.issubset(profile)
            or not set(profile).issubset(allowed)
            or not re.fullmatch(r"KoreLab_[A-Za-z0-9_]{1,32}", str(profile["name"]))
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", str(profile["host"]))
            or type(profile["port"]) is not int or not 1 <= profile["port"] <= 65535
            or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(profile["server_type"]))
            or not isinstance(profile["table_folders"], list)
            or any(not isinstance(folder, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", folder)
                   or ".." in pathlib.PurePosixPath(folder).parts for folder in profile["table_folders"])
            or not isinstance(profile.get("field_overrides", {}), dict)
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,32}", name)
                   or not isinstance(source, str) or not re.fullmatch(r"[a-z0-9_]{1,32}", source)
                   for name, source in profile.get("field_overrides", {}).items())):
        raise ValueError("private OpenKore server profile is invalid")
    fields_folder = profile.get("fields_folder")
    fields_path = pathlib.Path(fields_folder).expanduser().resolve() if fields_folder else None
    if fields_path is not None and not fields_path.is_dir():
        raise ValueError("private OpenKore fields_folder must be an existing directory")
    folders = ";".join(profile["table_folders"])
    return (f"[{profile['name']}]\n"
            f"ip {profile['host']}\nport {profile['port']}\nmaster_version 1\nversion 19\nsecureLogin 0\n"
            f"serverType {profile['server_type']}\n"
            + (f"fields_folder {fields_path}\n" if fields_path is not None else "")
            + "".join(f"field_{name} {source}\n" for name, source in sorted(profile.get("field_overrides", {}).items()))
            + "serverEncoding Western\ncharBlockSize 175\n"
            + f"addTableFolders {folders}\n")


def native_movement_config(overrides: dict[str, object], profile_values: dict[str, str]) -> dict[str, object]:
    """Keep OpenKore's route step explicit in KoreLab's isolated runtime config."""
    result = dict(overrides)
    if "route_step" not in result:
        result["route_step"] = profile_values.get("route_step", "").strip() or "10"
    return result


def redact(value: str, sensitive_values: tuple[str, ...] = ()) -> str:
    # OpenKore color codes can prefix a label and defeat line-anchored rules.
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"(?im)^(\s*password\s+).*$", r"\1[REDACTED]", value)
    value = re.sub(r"(?im)^(\s*session\s*id2?\s*:).*$", r"\1 [REDACTED]", value)
    value = re.sub(r"(?im)^(\s*account\s*id\s*:).*$", r"\1 [REDACTED]", value)
    value = re.sub(r"(?im)^(\s*char\s*id\s*:).*$", r"\1 [REDACTED]", value)
    value = re.sub(r"(?i)(Char ID:\s*)[^\r\n]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(AID/CID:\s*')[^']+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(account_id:\s*)\d+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(login_id[12]:)-?\d+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(char_id:\s*)\d+", r"\1[REDACTED]", value)
    for sensitive in sorted({item for item in sensitive_values if item}, key=len, reverse=True):
        token = rf"(?<![A-Za-z0-9_]){re.escape(sensitive)}(?![A-Za-z0-9_])"
        value = re.sub(token, "[REDACTED]", value, flags=re.I)
    return value


class OpenKoreStageError(RuntimeError):
    def __init__(self, stage: str, classification: str):
        super().__init__(f"OpenKore {stage} failed ({classification})")
        self.stage, self.classification = stage, classification


class OpenKoreAdapter:
    def __init__(self, checkout: pathlib.Path, scenario: dict, run_dir: pathlib.Path,
                 deadline: float, perl_path: pathlib.Path):
        self.checkout = checkout.expanduser().resolve()
        self.scenario, self.run_dir, self.deadline = scenario, run_dir, deadline
        self.sensitive_values = tuple(str(scenario.get("identity", {}).get(key, ""))
                                      for key in ("master", "username", "character"))
        self.perl_path = perl_path
        self.persist_runtime_config = bool(scenario.get("openkore", {}).get("persist_runtime_config", False))
        self.persisted_runtime_config_path: pathlib.Path | None = None
        self.temp = tempfile.TemporaryDirectory(prefix="korelab-")
        self.temp_path = pathlib.Path(self.temp.name)
        set_private_permissions(self.temp_path, 0o700)
        self.observer_file = self.temp_path / "observer.jsonl"
        self.process = None
        self.output = ""
        self.log_pending = ""
        self.archived_observer = False
        self.eventmacro_control_dir = None
        self.runtime_control_dir = None
        self.eventmacro_revision = 0
        self.runtime_profile_report: dict = {}
        self.runtime_profile_config_keys: list[str] = []
        self.runtime_baseline_config: dict[str, str | None] = {}
        self.runtime_baseline_control: dict[str, bytes | None] = {}
        self.master_fd = None
        self._pipe_output: queue.Queue[bytes | None] = queue.Queue()
        self._pipe_reader = None

    def start(self, *, socket_ready_only: bool = False) -> None:
        from ..openkore_profile import read_openkore_credentials
        profile_config = self.scenario.get("openkore", {}).get("profile_config_path")
        base = pathlib.Path(profile_config).expanduser().resolve() if profile_config else self.checkout / "control/config.txt"
        if self.persist_runtime_config:
            run_root = self.run_dir.expanduser().resolve()
            if not base.is_relative_to(run_root):
                raise ValueError("persistent runtime config must be a run-local profile under run_dir")
            self.persisted_runtime_config_path = base
        settings, configured_secrets = read_openkore_credentials(base)
        values = config_values(base)
        identity = self.scenario["identity"]
        self.sensitive_values = tuple(dict.fromkeys(self.sensitive_values + configured_secrets))
        profile_selection = self.scenario.get("openkore", {}).get("profile_selection")
        managed_server = self.scenario.get("openkore", {}).get("server_profile")
        needs_character_list_gate = bool(
            isinstance(managed_server, dict)
            and managed_server.get("server_type") == "kRO_RagexeRE_2021_11_03"
        )
        for field, setting in (("username", "username"),):
            if values.get(setting) != str(identity[field]):
                raise RuntimeError(f"selected OpenKore profile config does not match scenario {field}")
        if not profile_selection:
            for field, setting in (("master", "master"), ("character_slot", "char")):
                if values.get(setting) != str(identity[field]):
                    raise RuntimeError(f"existing OpenKore config does not match scenario {field}")
        openkore_settings = self.scenario.get("openkore", {})
        stage_timeout = float(openkore_settings.get("connection_stage_timeout", 30))
        if not 5 <= stage_timeout <= 300:
            raise ValueError("OpenKore connection_stage_timeout must be between 5 and 300 seconds")
        native_profile = openkore_settings.get("native_profile")
        if native_profile is not None and (
                not isinstance(native_profile, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", native_profile)):
            raise ValueError("openkore.native_profile must be a simple OpenKore profile name")
        profile_home = None
        if native_profile:
            plugin_source = self.checkout / "plugins/profiles/profiles.pl"
            if not plugin_source.is_file():
                raise RuntimeError("native OpenKore profiles plugin is unavailable in this checkout")
        overrides = native_movement_config(openkore_settings.get("config_overrides", {}), values)
        portal_profile_requested = bool(openkore_settings.get("portal_profile_id"))
        portal_los_target = None
        portal_los_fingerprint_value = None
        portal_los_cached = None
        if portal_profile_requested:
            configured_compile = overrides.get("portalCompile")
            if configured_compile not in (None, 1, "1"):
                raise ValueError("server portal synchronization requires native portalCompile=1 in its isolated profile")
            # OpenKore's native startup compiler is required to refresh its LOS
            # cache after a route change. The cache is staged in the same
            # private table overlay below, so compilation cannot touch the
            # shared OpenKore tables.
            overrides["portalCompile"] = 1
        if profile_selection:
            overrides = {**overrides,
                         "master": profile_selection["profile"],
                         "server": int(profile_selection["server_index"]),
                         "char": "" if needs_character_list_gate
                                 else int(profile_selection["character_slot"])}
        runtime_profile = validate_runtime_profile(self.checkout, {
            "ai_mode": openkore_settings.get("ai_mode", "manual"),
            "config_overrides": overrides,
            "control_file_overrides": openkore_settings.get("control_file_overrides", {}),
            "plugin_overlays": openkore_settings.get("plugin_overlays", []),
        }, additional_config_sources=(OBSERVER, base.read_text(encoding="utf-8", errors="replace")))
        config = self.temp_path / "config.txt"
        shutil.copyfile(base, config)
        set_private_permissions(config, 0o600)
        overrides = runtime_profile["config_overrides"]
        self.runtime_profile_config_keys = sorted(
            key for key in overrides if key not in {"master", "username", "password", "server", "char"})
        self.runtime_baseline_config = {key: values.get(key) or None
                                        for key in self.runtime_profile_config_keys}
        macro_source = self.scenario.get("openkore", {}).get("macro_source")
        eventmacro_source = self.scenario.get("openkore", {}).get("eventmacro_source")
        control_dirs = ["control"]
        control_manifest = []
        if runtime_profile["control_file_overrides"]:
            control_dir = self.temp_path / "control"
            self.runtime_control_dir = control_dir
            for filename in runtime_profile["control_file_overrides"]:
                source = self.checkout / "control" / filename
                self.runtime_baseline_control[filename] = source.read_bytes() if source.is_file() else None
            control_manifest = stage_control_file_overrides(self.checkout, control_dir,
                runtime_profile["control_file_overrides"])
            control_dirs.insert(0, str(control_dir))
        if macro_source:
            control_dir = self.temp_path / "control"
            control_dir.mkdir(mode=0o700, exist_ok=True)
            macro_file = control_dir / "korelab-macros.txt"
            shutil.copyfile(macro_source, macro_file)
            set_private_permissions(macro_file, 0o600)
            control_dirs.insert(0, str(control_dir))
            overrides = {**overrides, "macro_file": "korelab-macros.txt"}
        if eventmacro_source:
            control_dir = self.temp_path / "eventmacro-control"
            control_dir.mkdir(mode=0o700)
            eventmacro_file = control_dir / "korelab-eventmacros.txt"
            shutil.copyfile(eventmacro_source, eventmacro_file)
            set_private_permissions(eventmacro_file, 0o600)
            self.eventmacro_control_dir = control_dir
            control_dirs.insert(0, str(control_dir))
            overrides = {**overrides, "eventMacro_file": "korelab-eventmacros.txt"}
        with config.open("a") as stream:
            stream.write("\n# KoreLab temporary scenario overrides\n")
            for key, value in overrides.items():
                stream.write(f"{key} {value}\n")
            if self.scenario.get("action", {}).get("intent") == "escape":
                destination = self.scenario["action"].get("destination", {})
                stream.write(f"stateObserver_destination_x {int(destination.get('x', -1))}\n")
                stream.write(f"stateObserver_destination_y {int(destination.get('y', -1))}\n")
            stream.write(f"stateObserver_file {self.observer_file}\n")
        process_cwd = self.checkout
        if native_profile:
            # profiles.pl resolves its profile_folder relative to the process
            # working directory. Stage the complete profile in the run-local
            # workspace, then let the installed plugin select it natively.
            profile_home = self.temp_path / "profile-home"
            process_cwd = stage_native_profile(profile_home, native_profile, config)
            control_dirs = [str(self.checkout / "control") if item == "control" else item
                            for item in control_dirs]
        sys_file = self.temp_path / "sys.txt"
        sys_value = (self.checkout / "control/sys.txt").read_text()
        plugins = ["stateObserver"]
        if native_profile:
            plugins.append("profiles")
        if needs_character_list_gate:
            plugins.append("KoreLabCharacterListSync")
        if macro_source:
            plugins.append("macro")
        if eventmacro_source:
            plugins.append("eventMacro")
        plugins.extend(spec["name"][:-3] for spec in runtime_profile["plugin_overlays"])
        disabled_plugins = openkore_settings.get("disabled_plugins", [])
        if (not isinstance(disabled_plugins, list)
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                       for name in disabled_plugins)):
            raise ValueError("openkore.disabled_plugins must be a list of plugin names")
        sys_value = merge_plugin_list(sys_value, plugins, disabled_plugins)
        sys_file.write_text(sys_value)
        plugin_dir = self.temp_path / "plugins"
        plugin_dir.mkdir(mode=0o700)
        (plugin_dir / "stateObserver.pl").write_text(OBSERVER)
        if needs_character_list_gate:
            (plugin_dir / "KoreLabCharacterListSync.pl").write_text(
                CHARACTER_LIST_SYNC_GATE.replace(
                    "__KORELAB_CHARACTER_SLOT__", str(int(identity["character_slot"]))
                ), encoding="utf-8")
        private_plugin_specs = [spec for spec in runtime_profile["plugin_overlays"]
                                if not spec.get("already_installed")]
        staged_plugins = stage_plugin_overlays(plugin_dir, private_plugin_specs)
        logs = self.temp_path / "logs"
        logs.mkdir(mode=0o700)
        command = [str(self.perl_path), str(self.checkout / "openkore.pl"),
             f"--control={os.pathsep.join(control_dirs)}",
             f"--sys={sys_file}", f"--logs={logs}",
             f"--plugins={os.pathsep.join([str(plugin_dir), str(self.checkout / 'plugins')])}",
             f"--ai={runtime_profile['ai_mode']}"]
        if native_profile:
            fields_dir = pathlib.Path(
                (openkore_settings.get("server_profile") or {}).get("fields_folder")
                or self.checkout / "fields").expanduser().resolve()
            if not fields_dir.is_dir():
                raise RuntimeError(f"native profile fields directory is unavailable: {fields_dir}")
            command.append(f"--fields={fields_dir}")
        if native_profile:
            command.append(f"--profile={native_profile}")
        else:
            command.append(f"--config={config}")
        table_overlay = openkore_settings.get("table_overlay_directory")
        table_overlay_manifest = None
        private_server = openkore_settings.get("server_profile")
        portal_profile_id = openkore_settings.get("portal_profile_id")
        portal_profile_report = None
        if portal_profile_id and (table_overlay or not isinstance(private_server, dict)):
            raise ValueError("incremental portal profiles require a managed server_profile and cannot be combined with a separate table_overlay_directory")
        if private_server and table_overlay:
            raise ValueError("choose one private server profile or server table correction overlay")
        if private_server:
            overlay = self.run_dir / "managed-server-tables"
            overlay.mkdir(mode=0o700)
            server_text = render_private_server_profile(private_server)
            (overlay / "servers.txt").write_text(server_text, encoding="utf-8")
            if portal_profile_id:
                source_root = openkore_settings.get("portal_rathena_source")
                if not source_root:
                    raise ValueError("portal_profile_id requires the selected rAthena source for evidence validation")
                from ..portal_sync import materialize_portal_profile
                from ..portal_sync import load_portal_los_cache, portal_los_fingerprint
                from ..portal_sync import select_openkore_portals
                _source_table, native_folder = select_openkore_portals(
                    self.checkout / "tables", tuple(private_server["table_folders"]))
                effective_folder = overlay / native_folder
                effective_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                effective = effective_folder / "portals.txt"
                portal_profile_report = materialize_portal_profile(
                    server_id=portal_profile_id, data_home=USER_DATA_DIR,
                    openkore_tables=self.checkout / "tables",
                    table_folders=tuple(private_server["table_folders"]),
                    rathena_root=pathlib.Path(source_root), destination=effective,
                    profile_name=str(private_server["name"]))
                fingerprint = portal_los_fingerprint(
                    portals_file=effective, openkore_tables=self.checkout / "tables",
                    table_folders=tuple(private_server["table_folders"]),
                    fields_folder=pathlib.Path(private_server.get("fields_folder") or
                                                self.checkout / "fields"),
                    field_overrides=private_server.get("field_overrides", {}),
                    openkore_source=self.checkout)
                portal_los_fingerprint_value = fingerprint["sha256"]
                portal_los_cached = load_portal_los_cache(
                    server_id=portal_profile_id, data_home=USER_DATA_DIR,
                    fingerprint=portal_los_fingerprint_value)
                # A cache from a different portal graph or field/source build
                # must be emptied before native compilation; compilePortals_check
                # otherwise preserves stale values and never removes old edges.
                los_target = stage_portal_los_cache(
                    self.checkout / "tables", overlay, native_folder,
                    content=portal_los_cached.encode() if portal_los_cached is not None else b"")
                portal_los_target = los_target
                set_private_permissions(los_target, 0o600)
                portal_profile_report["line_of_sight_cache"] = {
                    "run_local": str(los_target),
                    "fingerprint": portal_los_fingerprint_value,
                    "status": "reused" if portal_los_cached is not None else "rebuild_required",
                }
            table_paths = native_table_search_path(overlay, self.checkout / "tables",
                tuple(private_server["table_folders"]), server_list_overlay=True)
            command.append(f"--tables={table_paths}")
        if table_overlay:
            overlay = pathlib.Path(table_overlay).expanduser().resolve()
            if not overlay.is_relative_to(self.run_dir.expanduser().resolve()):
                raise ValueError("server table overlays must be generated inside the current run directory")
            if not (overlay / "overlay-manifest.json").is_file():
                raise ValueError("server table overlay is missing its generation manifest")
            manifest_path = overlay / "overlay-manifest.json"
            table_overlay_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            effective_portals = pathlib.Path(table_overlay_manifest.get("effective_portals", "")).resolve()
            if not effective_portals.is_relative_to(overlay) or not effective_portals.is_file():
                raise ValueError("server table overlay manifest points outside its run-local effective table")
            server_list_overlay = bool(table_overlay_manifest.get("effective_servers"))
            if server_list_overlay:
                effective_servers = pathlib.Path(table_overlay_manifest["effective_servers"]).resolve()
                if not effective_servers.is_relative_to(overlay) or not effective_servers.is_file():
                    raise ValueError("server table overlay points outside its run-local effective server list")
            # Settings searches folders in order. The run-local complete files
            # are resolved through the profile's native addTableFolders; the
            # upstream tablepack remains the fallback for all other files.
            table_paths = native_table_search_path(
                overlay, self.checkout / "tables",
                tuple(table_overlay_manifest.get("native_table_folders", ())),
                server_list_overlay=server_list_overlay)
            command.append(f"--tables={table_paths}")
        if openkore_settings.get("console_interface") == "Socket":
            command.append("--interface=Socket")
        if os.name == "nt":
            environment = os.environ.copy()
            xstools_overlay = openkore_settings.get("xstools_overlay")
            if xstools_overlay:
                extension_root = pathlib.Path(xstools_overlay).expanduser().resolve()
                perl_paths = [str(self.checkout / "src"), str(extension_root.parent.parent)]
                if environment.get("PERL5LIB"):
                    perl_paths.append(environment["PERL5LIB"])
                environment["PERL5LIB"] = os.pathsep.join(perl_paths)
            self.process = subprocess.Popen(command, cwd=process_cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, env=environment)
            self._pipe_reader = threading.Thread(target=self._drain_pipe, daemon=True,
                                                 name="korelab-openkore-output")
            self._pipe_reader.start()
        else:
            environment = os.environ.copy()
            xstools_overlay = openkore_settings.get("xstools_overlay")
            if xstools_overlay:
                extension_root = pathlib.Path(xstools_overlay).expanduser().resolve()
                perl_paths = [str(self.checkout / "src"), str(extension_root.parent.parent)]
                if environment.get("PERL5LIB"):
                    perl_paths.append(environment["PERL5LIB"])
                environment["PERL5LIB"] = os.pathsep.join(perl_paths)
            master_fd, slave_fd = pty.openpty()
            try:
                self.process = subprocess.Popen(command, cwd=process_cwd,
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, start_new_session=True, env=environment)
            finally:
                os.close(slave_fd)
            os.set_blocking(master_fd, False)
            self.master_fd = master_fd
        self.runtime_profile_report = {
            "ai_mode": runtime_profile["ai_mode"],
            "config_override_keys": sorted(overrides),
            "control_file_overlays": control_manifest,
            "plugin_overlays": [{"name": spec["name"], "sha256": spec["sha256"],
                                 "source": "installed_identical_source" if spec.get("already_installed")
                                           else "private_overlay"}
                                for spec in runtime_profile["plugin_overlays"]],
            "source": "installed OpenKore key and control-file registries",
            "temporary_workspace": True,
        }
        if table_overlay_manifest is not None:
            self.runtime_profile_report["server_table_overlay"] = {
                "effective_portals_sha256": table_overlay_manifest.get("effective_portals_sha256"),
                "upstream_servers_sha256": table_overlay_manifest.get("upstream_servers_sha256"),
                "directory": str(overlay),
                "effective_portals": str(effective_portals),
                "loading_order": table_paths.split(os.pathsep),
            }
        if portal_profile_report is not None:
            self.runtime_profile_report["server_portal_profile"] = portal_profile_report
        self.log = (self.run_dir / "openkore.log").open("w", encoding="utf-8")
        self._wait(r"Loading plugin .*stateObserver\.pl")
        if portal_profile_report is not None:
            self._finish_portal_los_refresh(portal_profile_report, portal_los_target,
                                            portal_los_fingerprint_value, portal_los_cached)
        if socket_ready_only:
            return
        cursor = len(self.output)
        for pattern, stage in ((r"Connecting \([^\r\n]+\).*?connected", "login"),
                               (r"Connecting \([^\r\n]+\).*?connected", "authentication")):
            match = self._wait_stage(pattern, cursor, stage, timeout=stage_timeout)
            cursor += match.end()
        character_slot = int(identity["character_slot"])
        character_name = str(identity["character"])
        slot_pattern = rf"Slot\s+{character_slot}:\s*{re.escape(character_name)}\b"
        match = self._wait_stage(slot_pattern, cursor, "character_selection", timeout=stage_timeout)
        cursor += match.end()
        match = self._wait_stage(r"Connecting \([^\r\n]+\).*?connected", cursor,
                                 "map_entry", timeout=stage_timeout)
        cursor += match.end()
        self._wait_stage(r"You are now in the game", cursor, "map_entry", timeout=stage_timeout)

    def _finish_portal_los_refresh(self, report: dict, cache_path: pathlib.Path | None,
                                   fingerprint: str | None, cached_content: str | None) -> None:
        if cache_path is None or fingerprint is None:
            raise RuntimeError("portal LOS cache staging was incomplete; refusing to start with unverified routes")
        from ..portal_sync import store_portal_los_cache

        check = self._wait_log(r"Checking for new portals\.\.\.\s*(?:none found|found new portals!)",
            timeout=min(120, max(1, self.deadline - time.monotonic())))
        wrote_pattern = rf"Wrote portals Line of Sight table to ['\"]?{re.escape(str(cache_path))}['\"]?"
        log_text = pathlib.Path(self.log.name).read_text(encoding="utf-8", errors="replace")
        wrote_cache = bool(re.search(wrote_pattern, log_text, re.I))
        if "found new portals!" in check.group(0).lower() and not wrote_cache:
            self._wait_log(wrote_pattern,
                timeout=min(120, max(1, self.deadline - time.monotonic())))
            wrote_cache = True
        if wrote_cache:
            log_text = pathlib.Path(self.log.name).read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?m)^\s*Missing:\s+\S+\.fld2", log_text):
                raise RuntimeError("OpenKore portal compiler reported missing field data; LOS cache was not accepted")
            try:
                content = cache_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError("OpenKore reported a portal LOS rebuild but its run-local file is unavailable") from exc
            result = store_portal_los_cache(server_id=self.scenario["openkore"]["portal_profile_id"],
                data_home=USER_DATA_DIR, fingerprint=fingerprint, content=content)
            report["line_of_sight_cache"].update(result)
            report["line_of_sight_cache"]["status"] = "rebuilt"
            return
        if cached_content is not None and cache_path.is_file():
            report["line_of_sight_cache"]["status"] = "reused"
            return
        result = store_portal_los_cache(server_id=self.scenario["openkore"]["portal_profile_id"],
            data_home=USER_DATA_DIR, fingerprint=fingerprint, content="")
        report["line_of_sight_cache"].update(result)
        report["line_of_sight_cache"]["status"] = "built_empty"

    def _wait_log(self, pattern: str, timeout: float = 30) -> re.Match:
        end = min(self.deadline, time.monotonic() + timeout)
        rx = re.compile(pattern, re.I | re.S)
        while time.monotonic() < end:
            self.log.flush()
            try:
                log_text = pathlib.Path(self.log.name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
            match = rx.search(log_text)
            if match:
                return match
            if self.process.poll() is not None:
                raise RuntimeError(f"OpenKore exited with status {self.process.returncode}")
            self._read(min(0.2, end - time.monotonic()))
        raise TimeoutError(f"OpenKore did not report expected state: {pattern}")

    def restore_runtime_profile(self, restore_ai_mode: str, handoff_macro: str,
                                handoff_marker: str, timeout: float = 20) -> dict:
        """Restore the source profile in the private workspace, then run native handoff code."""
        if restore_ai_mode not in {"on", "manual", "off"}:
            raise ValueError("profile restoration AI mode must be on, manual, or off")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", handoff_macro):
            raise ValueError("profile handoff requires a declared eventMacro name")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handoff_marker):
            raise ValueError("profile handoff marker must be a simple literal token")
        if not self.runtime_profile_config_keys and not self.runtime_baseline_control:
            raise RuntimeError("this session did not apply a restorable OpenKore runtime profile")
        if self.eventmacro_control_dir is None:
            raise RuntimeError("runtime profile handoff requires the native eventMacro plugin")

        restored_config = []
        for key in self.runtime_profile_config_keys:
            value = self.runtime_baseline_config.get(key)
            command = f"conf {key} {value}" if value is not None else f"conf {key} none"
            output = self._capture_raw_command(command, timeout=3)
            if value is None:
                valid = (f"Config '{key}' unset" in output or
                         f"Config '{key}' is already *None*" in output)
            else:
                valid = (f"Config '{key}' is already {value}" in output or
                         f"Config '{key}' set to {value} " in output)
            if not valid:
                raise RuntimeError(f"OpenKore did not confirm restoration of config key {key}")
            restored_config.append(key)

        reloaded_files = []
        if self.runtime_baseline_control:
            if self.runtime_control_dir is None:
                raise RuntimeError("runtime control-file overlay directory is unavailable")
            for filename, content in self.runtime_baseline_control.items():
                target = self.runtime_control_dir / filename
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(content)
                    set_private_permissions(target, 0o600)
                reloaded_files.append(filename)
            pattern = "|".join(re.escape(filename) for filename in reloaded_files)
            output = self._capture_raw_command(f"reload {pattern}", timeout=8)
            if "All files were loaded" not in output or any(name not in output for name in reloaded_files):
                raise RuntimeError("OpenKore did not confirm reloading the restored control files")

        ai_output = self._capture_raw_command(f"ai {restore_ai_mode}", timeout=3)
        ai_patterns = {"on": r"AI (?:set to auto mode|is already set to auto mode)",
                       "manual": r"AI (?:set to manual mode|is already set to manual mode)",
                       "off": r"AI (?:turned off|is already turned off)"}
        if not re.search(ai_patterns[restore_ai_mode], ai_output, re.I):
            raise RuntimeError("OpenKore did not confirm the requested profile handoff AI mode")

        listing = self.capture_command("eventMacro list", timeout=5)
        if not re.search(rf"(?m)^\s*{re.escape(handoff_macro)}\s*$", listing):
            raise RuntimeError("declared native handoff macro is not loaded")
        cursor = len(self.output)
        self.command(f"eventMacro {handoff_macro}")
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            excerpt = self.output[cursor:]
            if handoff_marker in excerpt and re.search(
                    rf"\[eventMacro\] Macro '{re.escape(handoff_macro)}' ended\.", excerpt):
                resumed = bool(re.search(r"AI set to auto mode|AI is already set to auto mode", excerpt, re.I))
                if not resumed:
                    raise RuntimeError("native handoff macro completed without resuming OpenKore AI")
                return {"status": "PASS", "restored_config_keys": restored_config,
                    "restored_control_files": [{"file": name,
                        "sha256": hashlib.sha256(self.runtime_baseline_control[name]).hexdigest()
                            if self.runtime_baseline_control[name] is not None else None}
                        for name in reloaded_files],
                    "restored_ai_mode": restore_ai_mode, "resumed_ai_mode": "on",
                    "handoff_macro": handoff_macro,
                    "handoff_marker_observed": handoff_marker in excerpt,
                    "native_macro_completed": True,
                    "evidence": "original config values and control-file snapshots restored in the private runtime, native files reloaded, and declared eventMacro handoff completed"}
            if self.process and self.process.poll() is not None:
                raise RuntimeError("OpenKore exited before the native profile handoff macro completed")
            self._read(0.1)
        raise TimeoutError("native profile handoff macro did not emit its completion marker")

    def _capture_raw_command(self, command: str, timeout: float = 5,
                             quiet_period: float = 0.35) -> str:
        cursor = len(self.output)
        self.command(command)
        deadline = min(self.deadline, time.monotonic() + timeout)
        last_change, seen = time.monotonic(), ""
        while time.monotonic() < deadline:
            self._read(min(0.1, max(0.01, deadline - time.monotonic())))
            current = self.output[cursor:]
            if current != seen:
                seen, last_change = current, time.monotonic()
            elif current and time.monotonic() - last_change >= quiet_period:
                return current
        return self.output[cursor:]

    @staticmethod
    def _connection_failure(stage: str, output: str, timed_out: bool) -> str:
        if re.search(r"(?i)(login|auth|password|account).{0,100}(fail|error|invalid|reject|incorrect)|"
                     r"(fail|error|invalid|reject|incorrect).{0,100}(login|auth|password|account)", output):
            return "authentication_failed"
        if re.search(r"(?i)(connection refused|failed to connect|could not connect|server is offline|connect timeout)", output):
            return "connection_failed"
        if stage == "character_selection":
            return "character_selection_failed"
        if stage == "map_entry":
            return "map_entry_failed" if not timed_out else "map_entry_timeout"
        return "authentication_timeout" if timed_out and stage == "authentication" else "connection_timeout"

    def _wait_stage(self, pattern: str, cursor: int, stage: str, timeout: float = 30) -> re.Match:
        end = min(self.deadline, time.monotonic() + timeout)
        rx = re.compile(pattern, re.I | re.S)
        while time.monotonic() < end:
            match = rx.search(self.output[cursor:])
            if match:
                return match
            if self.process.poll() is not None:
                code = self._connection_failure(stage, self.output[cursor:], False)
                raise OpenKoreStageError(stage, code)
            self._read(min(0.2, end - time.monotonic()))
        code = self._connection_failure(stage, self.output[cursor:], True)
        raise OpenKoreStageError(stage, code)

    def _read(self, timeout: float = 0.2) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("scenario exceeded maximum duration")
        if os.name == "nt":
            try:
                raw = self._pipe_output.get(timeout=min(timeout, remaining))
            except queue.Empty:
                return
            if raw is None:
                return
            chunk = raw.decode("utf-8", "replace")
        else:
            ready, _, _ = select.select([self.master_fd], [], [], min(timeout, remaining))
            if not ready:
                return
            try:
                chunk = os.read(self.master_fd, 65536).decode("utf-8", "replace")
            except OSError:
                return
        if chunk:
            self.output += chunk
            self.log_pending += chunk
            lines = self.log_pending.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                self.log_pending = lines.pop()
            else:
                self.log_pending = ""
            if lines:
                self.log.write(redact("".join(lines), self.sensitive_values))
            self.log.flush()

    def _drain_pipe(self) -> None:
        try:
            while self.process and self.process.stdout:
                chunk = self.process.stdout.read(65536)
                if not chunk:
                    break
                self._pipe_output.put(chunk)
        finally:
            self._pipe_output.put(None)

    def _wait(self, pattern: str, timeout: float = 30) -> re.Match:
        end = min(self.deadline, time.monotonic() + timeout)
        rx = re.compile(pattern, re.I | re.S)
        cursor = max(0, len(self.output) - 65536)
        while time.monotonic() < end:
            match = rx.search(self.output[cursor:])
            if match:
                return match
            if self.process.poll() is not None:
                raise RuntimeError(f"OpenKore exited with status {self.process.returncode}")
            self._read(min(0.2, end-time.monotonic()))
        raise TimeoutError(f"OpenKore did not report expected state: {pattern}")

    def _wait_after(self, pattern: str, cursor: int, timeout: float = 30) -> re.Match:
        end = min(self.deadline, time.monotonic() + timeout)
        rx = re.compile(pattern, re.I | re.S)
        while time.monotonic() < end:
            rows = self._observer_rows()
            if rows:
                self._raise_for_unexpected_death(rows[-1:])
            match = rx.search(self.output[cursor:])
            if match:
                return match
            if self.process.poll() is not None:
                raise RuntimeError(f"OpenKore exited with status {self.process.returncode}")
            self._read(min(0.2, end-time.monotonic()))
        raise TimeoutError(f"OpenKore did not report expected new state: {pattern}")

    @staticmethod
    def _raise_for_unexpected_death(rows: list[dict]) -> None:
        if any(row.get("type") == "state" and row.get("connected_in_game") is True
               and row.get("character_attributes", {}).get("is_dead") is True
               for row in rows):
            raise UnexpectedCharacterDeath(
                "OpenKore observed character death during an operation that expected survival")

    def state(self, predicate: Callable[[dict], bool], timeout: float = 15,
              *, allow_dead: bool = False) -> dict:
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            try:
                rows = [json.loads(line) for line in self.observer_file.read_text().splitlines()]
            except (OSError, json.JSONDecodeError):
                rows = []
            current = next((row for row in reversed(rows) if row.get("type") == "state"), None)
            if current and not allow_dead:
                self._raise_for_unexpected_death([current])
            if current and predicate(current):
                return current
            self._read(0.1)
        raise TimeoutError("structured OpenKore observer did not report the requested state")

    def wait_for_initial_inventory(self, session_generation: int,
                                   predicate: Callable[[dict], bool], timeout: float = 20) -> dict:
        """Require a complete inventory list from the same live connection that was selected."""
        if type(session_generation) is not int or session_generation < 1:
            raise RuntimeError("OpenKore observer did not identify the active connection generation")
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            rows = self._observer_rows()
            current = next((row for row in reversed(rows) if row.get("type") == "state"), None)
            if current:
                self._raise_for_unexpected_death([current])
                if current.get("session_generation") != session_generation:
                    raise RuntimeError("OpenKore connection changed before initial inventory readiness")
                if current.get("connected_in_game") is not True:
                    raise RuntimeError("OpenKore disconnected before its initial inventory list completed")
                if current.get("inventory_observed") is True and predicate(current):
                    return current
            self._read(min(0.1, max(0, end-time.monotonic())))
        raise TimeoutError("OpenKore did not complete the initial inventory observation in the active session")

    def state_after(self, sample_count: int, predicate: Callable[[dict], bool], timeout: float = 15,
                    *, allow_dead: bool = False, failure_patterns: tuple[str, ...] = (),
                    failure_cursor: int | None = None) -> dict:
        """Wait for a matching observation written after the current sample count."""
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            try:
                rows = [json.loads(line) for line in self.observer_file.read_text().splitlines()]
            except (OSError, json.JSONDecodeError):
                rows = []
            if len(rows) > sample_count and not allow_dead:
                self._raise_for_unexpected_death(rows[sample_count:])
            if len(rows) > sample_count and predicate(rows[-1]):
                return rows[-1]
            self._read(0.1)
            if failure_patterns:
                output = self.output[max(0, failure_cursor or 0):]
                if any(re.search(pattern, output, re.I) for pattern in failure_patterns):
                    raise RuntimeError("OpenKore reported a native route failure before the requested state was observed")
        raise TimeoutError("OpenKore did not report the requested new state")

    def wait_position_change(self, map_name: str, x: int, y: int, timeout: float = 30) -> dict:
        """Wait for a new structured observer sample at a position different from the supplied origin."""
        if not isinstance(map_name, str) or not re.fullmatch(r"[a-z0-9_]+", map_name):
            raise ValueError("wait-position-change map is invalid")
        if type(x) is not int or type(y) is not int or not 0 <= x <= 1000 or not 0 <= y <= 1000:
            raise ValueError("wait-position-change coordinates must be integers from 0 to 1000")
        sample_count = len(self._observer_rows())
        row = self.state_after(sample_count, lambda state: state.get("connected_in_game") is True
            and (state.get("map") != map_name or position(state) != {"x": x, "y": y}), timeout=timeout)
        return {"status": "OBSERVED", "from": {"map": map_name, "x": x, "y": y},
                "observation": row,
                "completion": "a new structured OpenKore state sample confirmed movement or map transition; this does not prove the final objective"}

    def wait_native_ai_outcome(self, monster_name: str, marker: str,
                               inventory_before: dict[int, int],
                               inventory_delta: dict[int, int], timeout: float) -> dict:
        """Observe a native AI kill and eventMacro outcome without issuing combat commands."""
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", marker):
            raise ValueError("eventMacro outcome marker must be a simple literal token")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", monster_name):
            raise ValueError("controlled monster name is invalid")
        sample_count = len(self._observer_rows())
        # Markers are unique to the loaded scenario and may arrive just before
        # the runner reaches this wait step, so inspect this session from startup.
        cursor = 0
        start = time.monotonic()
        marker_seen = False
        trigger_seen = False
        absent_state = None
        inventory_state = None
        while time.monotonic() - start < timeout:
            self._read(0.1)
            excerpt = self.output[cursor:]
            marker_seen = marker_seen or marker in excerpt
            trigger_seen = trigger_seen or bool(re.search(
                r"(?:Conditions met for automacro|activated automacro)", excerpt, re.I))
            rows = [row for row in self._observer_rows()[sample_count:]
                    if row.get("type") == "state" and row.get("connected_in_game") is True]
            if rows:
                latest = rows[-1]
                present = any(str(mob.get("name", "")).casefold() == monster_name.casefold()
                              for mob in latest.get("nearby_monsters", []))
                counts = inventory_counts(latest.get("inventory", []))
                inventory_ok = all(counts.get(item_id, 0) - inventory_before.get(item_id, 0) == delta
                                   for item_id, delta in inventory_delta.items())
                if not present:
                    absent_state = latest
                if inventory_ok:
                    inventory_state = latest
                if marker_seen and trigger_seen and absent_state and inventory_state:
                    return {"status": "PASS", "marker_observed": True,
                        "automacro_trigger_observed": trigger_seen,
                        "monster_absent_observation": absent_state,
                        "inventory_observation": inventory_state,
                        "inventory_before": {str(key): value for key, value in inventory_before.items()},
                        "inventory_after": {str(key): value for key, value in counts.items()},
                        "expected_inventory_delta": {str(key): value for key, value in inventory_delta.items()},
                        "lifecycle": [
                            {"state": "started", "evidence": "native eventMacro automacro entered its turn-in macro"},
                            {"state": "progress", "evidence": "OpenKore stateObserver confirmed spawned monster disappearance"},
                            {"state": "completed", "evidence": f"native eventMacro marker {marker} and inventory delta observed"}],
                        "evidence": "OpenKore AI profile selected combat; structured actor/inventory observations and native eventMacro marker confirmed the objective"}
            if self.process and self.process.poll() is not None:
                raise RuntimeError("OpenKore disconnected or exited before native AI objective completed")
        raise TimeoutError(f"native AI objective incomplete: automacro_trigger={trigger_seen}, marker={marker_seen}, monster_absent={absent_state is not None}, inventory_delta={inventory_state is not None}")

    def wait_eventmacro_dialogue(self, *, npc_name: str, trigger: str, marker: str,
                                 expected_choices: list[str], inventory_before: dict[int, int],
                                 inventory_delta: dict[int, int], observer_sample_count: int,
                                 branch_message: str,
                                 timeout: float = 30) -> dict:
        """Verify an eventMacro-owned NPC branch through plugin, dialogue, and state evidence."""
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", marker):
            raise ValueError("eventMacro dialogue marker must be a simple literal token")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", trigger):
            raise ValueError("eventMacro dialogue trigger must be a declared automacro name")
        if not isinstance(expected_choices, list) or not expected_choices or any(
                not isinstance(choice, str) or not choice for choice in expected_choices):
            raise ValueError("eventMacro dialogue requires an expected live response list")

        trigger_pattern = re.compile(
            rf"Conditions met for automacro ['\"]{re.escape(trigger)}['\"]", re.I)
        marker_pattern = re.compile(re.escape(marker), re.I)
        # Markers are scoped to a fresh managed OpenKore process. Searching from
        # startup also handles an automacro that fires while the NPC is loaded.
        self._wait_after(marker_pattern.pattern, 0, timeout=timeout)
        if not trigger_pattern.search(self.output):
            raise RuntimeError(f"eventMacro marker {marker} appeared without declared automacro {trigger}")
        if not branch_message or not re.search(re.escape(branch_message), self.output, re.I):
            raise RuntimeError("eventMacro response did not include the declared NPC branch message")

        dialogue_rows = [row for row in self._observer_rows()[observer_sample_count:]
                         if row.get("type") == "dialogue"
                         and str(row.get("name", "")).casefold() == npc_name.casefold()]
        if not dialogue_rows:
            raise RuntimeError("eventMacro dialogue marker appeared without an observed NPC response menu")
        observed_choices = [str(choice) for choice in dialogue_rows[-1].get("responses", [])
                            if str(choice) != "Cancel Chat"]
        if observed_choices != expected_choices:
            raise RuntimeError(f"unexpected NPC dialogue choices: {observed_choices}; expected {expected_choices}")

        def has_delta(row: dict) -> bool:
            if row.get("connected_in_game") is not True:
                return False
            counts = inventory_counts(row.get("inventory", []))
            return all(counts.get(item_id, 0) - inventory_before.get(item_id, 0) == delta
                       for item_id, delta in inventory_delta.items())

        state = self.state(has_delta, timeout=timeout)
        after = inventory_counts(state.get("inventory", []))
        return {"status": "PASS", "driver": "native_eventMacro", "npc": npc_name,
                "eventmacro_trigger": trigger, "eventmacro_marker": marker,
                "available_choices": observed_choices,
                "inventory_before": {str(key): value for key, value in inventory_before.items()},
                "inventory_after": {str(key): value for key, value in after.items()},
                "expected_inventory_delta": {str(key): value for key, value in inventory_delta.items()},
                "branch_message_observed": True,
                "evidence": "native eventMacro trigger and marker, live OpenKore NPC menu, NPC response, and server inventory packets"}

    def relog(self, timeout: float = 30) -> dict:
        """Refresh server-owned character state after offline fixture preparation."""
        self.state(lambda row: row.get("connected_in_game") is True)
        cursor = len(self.output)
        self.command("relog 1")
        self._wait_after(r"Relogging in 1 seconds", cursor, timeout=10)
        self._wait_after(r"You are now in the game", cursor, timeout=timeout)
        return self.state(lambda row: row.get("connected_in_game") is True, timeout=timeout)

    def command(self, value: str) -> None:
        payload = (value + "\n").encode()
        if os.name == "nt":
            if not self.process or not self.process.stdin:
                raise RuntimeError("OpenKore process input is unavailable")
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        else:
            os.write(self.master_fd, payload)

    def set_ai_mode(self, mode: str, timeout: float = 10) -> dict:
        if mode not in {"on", "manual", "off"}:
            raise ValueError("OpenKore AI mode must be on, manual, or off")
        cursor = len(self.output)
        self.command(f"ai {mode}")
        messages = {
            "on": r"AI (?:set to auto mode|is already set to auto mode)",
            "manual": r"AI (?:set to manual mode|is already set to manual mode)",
            "off": r"AI (?:turned off|is already off|is already turned off)",
        }
        match = self._wait_after(messages[mode], cursor, timeout=timeout)
        return {"mode": mode, "native_response": match.group(0),
                "evidence": "OpenKore native ai command confirmed the requested AI mode"}

    def set_config_value(self, key: str, value: str, timeout: float = 10) -> dict:
        """Change one pre-authorized runtime key through OpenKore's native conf command."""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            raise ValueError("OpenKore configuration key is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
            raise ValueError("OpenKore configuration value is invalid")
        cursor = len(self.output)
        self.command(f"conf {key} {value}")
        match = self._wait_after(
            rf"Config '{re.escape(key)}' (?:set to {re.escape(value)}(?: \(was [^\r\n]*\))?|is already {re.escape(value)})",
            cursor, timeout=timeout)
        quiet_deadline = min(self.deadline, time.monotonic() + 0.25)
        while time.monotonic() < quiet_deadline:
            self._read(min(0.05, quiet_deadline - time.monotonic()))
        output_segment = self.output[cursor:]
        plugin_events = [redact(line, self.sensitive_values) for line in output_segment.splitlines()
                         if "[lockMapSweep]" in line]
        return {"key": key, "native_response": match.group(0),
                "plugin_events": plugin_events,
                "evidence": "OpenKore native conf command reported the requested runtime value"}

    def capture_command(self, value: str, *, timeout: float = 5, quiet_period: float = 0.35) -> str:
        cursor = len(self.output)
        self.command(value)
        deadline = min(self.deadline, time.monotonic() + timeout)
        last_change, seen = time.monotonic(), ""
        while time.monotonic() < deadline:
            self._read(min(0.1, max(0.01, deadline - time.monotonic())))
            current = self.output[cursor:]
            if current != seen:
                seen, last_change = current, time.monotonic()
            elif current and time.monotonic() - last_change >= quiet_period:
                break
        return redact(self.output[cursor:], self.sensitive_values)

    def observe_npc_dialogue(self, name: str, timeout: float = 15) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("NPC name is required")
        sample_count = len(self._observer_rows())
        npc = self._talknpc_by_observed_id(name, "x", timeout)
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            rows = [row for row in self._observer_rows()[sample_count:]
                    if row.get("type") == "dialogue" and str(row.get("name", "")).casefold() == name.casefold()]
            if rows:
                state = self.state(lambda row: row.get("type") == "state" and row.get("connected_in_game") is True,
                                   timeout=max(0.1, end - time.monotonic()))
                return {"status": "OBSERVED", "npc": name, "position": npc.get("position"),
                        "responses": rows[-1].get("responses", []), "state": state,
                        "completion": "live NPC dialogue responses captured; no choice selected"}
            self._read(0.1)
        raise TimeoutError("OpenKore did not expose a dialogue response for the selected NPC")

    def replace_eventmacro_source(self, source: pathlib.Path, expected_names: list[str], timeout: float = 10) -> dict:
        if self.eventmacro_control_dir is None:
            raise RuntimeError("eventMacro is not loaded in this OpenKore session")
        self.eventmacro_revision += 1
        filename = f"korelab-eventmacros-{self.eventmacro_revision}.txt"
        target = self.eventmacro_control_dir / filename
        shutil.copyfile(source, target)
        set_private_permissions(target, 0o600)
        cursor = len(self.output)
        self.command(f"conf eventMacro_file {filename}")
        self._wait_after(r"eventMacro_file", cursor, timeout=timeout)
        output = self.capture_command("eventMacro list", timeout=timeout)
        missing = [name for name in expected_names if not re.search(rf"(?m)^\s*{re.escape(name)}\s*$", output)]
        if missing:
            raise RuntimeError("native eventMacro source reload did not expose the expected macro names")
        return {"status": "LOADED", "revision": self.eventmacro_revision,
                "macro_names_verified": expected_names, "source": str(source),
                "evidence": "native eventMacro_file reload followed by eventMacro list"}

    def archive_observer(self) -> None:
        if self.archived_observer or not self.observer_file.exists():
            return
        destination = self.run_dir / "openkore-state.jsonl"
        destination.write_bytes(self.observer_file.read_bytes())
        set_private_permissions(destination, 0o600)
        self.archived_observer = True

    def move(self, map_name: str, target: dict[str, int], timeout: float) -> dict:
        destination = {axis: target[axis] for axis in ("x", "y")}
        current = self.state(lambda row: row.get("connected_in_game") is True, timeout=min(timeout, 10))
        sample_count = len(self._observer_rows())
        output_cursor = len(self.output)
        if current.get("map") == map_name:
            command_output = self._capture_raw_command(f"move {destination['x']} {destination['y']}")
        else:
            # OpenKore's native `move <map> <x> <y>` routes through its known portals.
            command_output = self._capture_raw_command(
                f"move {map_name} {destination['x']} {destination['y']}")
        if re.search(r"(?i)Coordinates\s+\d+\s+\d+\s+are not walkable", command_output):
            raise RuntimeError("OpenKore rejected the movement destination as not walkable; "
                               "verify the selected server fields_folder and map data")
        if re.search(r"(?i)Cannot load field|No corresponding field file", command_output):
            raise RuntimeError("OpenKore could not load the field data required for native movement")
        return self.state_after(sample_count, lambda row:
            row.get("connected_in_game") is True and row.get("map") == map_name
            and position(row) == destination and row.get("movement_state") == "stationary", timeout,
            failure_patterns=(r"Stuck during route", r"No path found", r"Failed to find path"),
            failure_cursor=output_cursor)

    def _talknpc_by_observed_id(self, name: str, sequence: str,
                                timeout: float = 15) -> dict:
        state = self._observe_nearby_npc(name, timeout, require_name_id=True)
        npc = next(npc for npc in state["nearby_npcs"]
                   if npc.get("name", "").casefold() == name.casefold()
                   and int(npc.get("name_id", 0)) > 0)
        # OpenKore's ID_<nameID> form remains stable when NPC name aliases are
        # refreshed on a reconnect.
        self.command(f'talknpc ID_{int(npc["name_id"])} {sequence}')
        return npc

    def _observe_nearby_npc(self, name: str, timeout: float = 15,
                           *, require_name_id: bool = False) -> dict:
        """Require a fresh client observation for an NPC-dependent action."""
        sample_count = len(self._observer_rows())
        return self.state_after(sample_count, lambda row: row.get("connected_in_game") is True and any(
            npc.get("name", "").casefold() == name.casefold()
            and (not require_name_id or int(npc.get("name_id", 0)) > 0)
            for npc in row.get("nearby_npcs", [])), timeout)

    def interact_with_npc(self, name: str, relative_file: str, material_id: int,
                          reward_id: int, material_name: str, reward_name: str,
                          timeout: float = 25, amount: int = 1,
                          sequence: str = "x c") -> dict:
        if not re.fullmatch(r"KoreLabM3[A-Z0-9]{6}", name):
            raise ValueError("temporary NPC name is invalid")
        if not re.fullmatch(r"npc/custom/korelab_m3_[a-z0-9]{6}\.txt", relative_file):
            raise ValueError("temporary NPC source path is invalid")
        if not isinstance(sequence, str) or not re.fullmatch(
                r"(?:x|k|c|n|b|r\d+|w\d+)(?: (?:x|k|c|n|b|r\d+|w\d+))*", sequence):
            raise ValueError("NPC conversation sequence contains unsupported native steps")
        current = self.state(lambda row: row.get("connected_in_game") is True)
        visible = None
        already_loaded = any(npc.get("name", "").casefold() == name.casefold()
                             for npc in current.get("nearby_npcs", []))
        if not already_loaded:
            try:
                visible = self._observe_nearby_npc(name, min(timeout, 2), require_name_id=True)
                already_loaded = True
            except TimeoutError:
                visible = None
        if already_loaded:
            # A managed scenario may load its private fixture before this
            # session starts. Reuse the server's live NPC instead of loading
            # it again and forcing a character-server round trip.
            visible = visible or current
        else:
            self.command(f"c @loadnpc {relative_file}")
            self._wait(r"Script loaded\.", timeout=15)
            self.relog()
            visible = self.state(lambda row: row.get("connected_in_game") is True and any(
                npc.get("name", "").casefold() == name.casefold()
                for npc in row.get("nearby_npcs", [])), timeout=15)
        before_inventory = inventory_counts(visible.get("inventory", []))
        self._talknpc_by_observed_id(name, sequence, timeout)
        self._wait(r"\[TalkNPC\] Done talking with .*\(end\)", timeout=timeout)
        removed = self._wait(rf"Inventory Item Removed:\s*{re.escape(material_name)}\b.*\bx {amount}\b", timeout=timeout)
        added = self._wait(rf"Item added to inventory:\s*{re.escape(reward_name)}\b.*\bx {amount}\b", timeout=timeout)
        self._wait(r"Exchange complete\.", timeout=timeout)
        after = self.state(lambda row: row.get("connected_in_game") is True, timeout=5)
        after_inventory = inventory_counts(after.get("inventory", []))
        return {"status": "PASS", "npc_name": name, "npc_observed": True,
                "npc_preloaded_before_session": already_loaded,
                "npc_position": next(npc.get("position") for npc in visible["nearby_npcs"]
                    if npc.get("name", "").casefold() == name.casefold()),
                "dialogue_completed": True, "sequence": sequence,
                "inventory_before": {str(k): v for k, v in before_inventory.items()},
                "inventory_after": {str(k): v for k, v in after_inventory.items()},
                "client_item_removed_message": bool(removed), "client_item_added_message": bool(added),
                "evidence": "OpenKore resolved the live NPC, completed talknpc, and received server item-removal/addition packets"}

    def load_npc(self, name: str, relative_file: str, timeout: float = 30) -> dict:
        if not re.fullmatch(r"KoreLab[A-Za-z0-9_]{1,40}", name):
            raise ValueError("temporary NPC name is invalid")
        if not re.fullmatch(r"npc/custom/korelab_[a-z0-9_-]{1,64}\.txt", relative_file, re.I):
            raise ValueError("temporary NPC source path is invalid")
        self.state(lambda row: row.get("connected_in_game") is True)
        cursor = len(self.output)
        self.command(f"c @loadnpc {relative_file}")
        response = self._wait_after(r"Script loaded\.|Script could not be loaded\.|@loadnpc failed\.",
                                    cursor, timeout=min(timeout, 15))
        if not response.group(0).casefold().startswith("script loaded"):
            raise RuntimeError(f"rAthena rejected controlled NPC fixture load: {response.group(0)}")
        state = self.relog(timeout)
        visible = self._observe_nearby_npc(name, timeout)
        return {"state": visible, "npc_observed": True, "refreshed_after_load": True}

    def interact_with_branch(self, name: str, choice_id: str, label: str,
                             choices: list[dict], item_names: dict[int, str],
                             consume: dict, grant: dict, message: str,
                             timeout: float = 25,
                             quest_states: dict[int, str] | None = None,
                             expected_available_choices: list[str] | None = None) -> dict:
        visible = self._observe_nearby_npc(name, timeout)
        before = inventory_counts(visible.get("inventory", []))
        eligible = []
        unavailable = {}
        for choice in choices:
            missing_items = [{"item_id": int(req["item_id"]),
                              "required": int(req["amount"]),
                              "observed": before.get(int(req["item_id"]), 0)}
                             for req in choice["requires"]
                             if before.get(int(req["item_id"]), 0) < int(req["amount"])]
            quest_states = quest_states or {}
            missing_quests = [{"quest_id": int(req["id"]),
                               "required_state": str(req["state"]),
                               "observed_state": quest_states.get(int(req["id"]))}
                              for req in choice.get("quest_requires", [])
                              if quest_states.get(int(req["id"])) != str(req["state"])]
            if not missing_items and not missing_quests:
                eligible.append(str(choice["label"]))
            else:
                unavailable[str(choice.get("id", choice["label"]))] = {
                    "missing_inventory": missing_items, "missing_quest_state": missing_quests}
        if label not in eligible:
            raise RuntimeError(f"declared dialogue objective is not available: {choice_id}; "
                               f"unsatisfied prerequisites={unavailable.get(choice_id, {})}")
        sample_count = len(self._observer_rows())
        cursor = len(self.output)
        self._talknpc_by_observed_id(name, "x", timeout)
        dialogue_rows = []
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            dialogue_rows = [row for row in self._observer_rows()[sample_count:]
                             if row.get("type") == "dialogue"
                             and row.get("name", "").casefold() == name.casefold()]
            if dialogue_rows:
                break
            self._read(0.1)
        if not dialogue_rows:
            raise TimeoutError("OpenKore did not expose the live NPC response list before selection")
        live_responses = [str(option) for option in dialogue_rows[-1].get("responses", [])]
        observed = [option for option in live_responses if option != "Cancel Chat"]
        expected = (list(expected_available_choices) if expected_available_choices is not None
                    else eligible)
        if observed != expected or label not in observed:
            if live_responses:
                self.command(f"talk resp {len(live_responses) - 1}")
                try:
                    self._wait_after(r"\[TalkNPC\].*Done talking \(close\)", cursor, timeout=5)
                except TimeoutError:
                    pass
            raise RuntimeError(f"unexpected NPC dialogue choices: {observed}; expected {expected}")
        self.command(f"talk resp {observed.index(label)}")
        self._wait_after(r"\[TalkNPC\] Done talking with .*\(end\)", cursor, timeout=timeout)
        removed_name = str(item_names[int(consume["item_id"])])
        granted_name = str(item_names[int(grant["item_id"])])
        removed = self._wait_after(rf"Inventory Item Removed:\s*{re.escape(removed_name)}\b.*\bx {int(consume['amount'])}\b",
                                   cursor, timeout=timeout)
        added = self._wait_after(rf"Item added to inventory:\s*{re.escape(granted_name)}\b.*\bx {int(grant['amount'])}\b",
                                 cursor, timeout=timeout)
        self._wait_after(re.escape(message), cursor, timeout=timeout)
        after = self.state_after(sample_count, lambda row: row.get("type") == "state"
            and row.get("connected_in_game") is True
            and inventory_counts(row.get("inventory", [])).get(int(consume["item_id"]), 0)
                == before.get(int(consume["item_id"]), 0) - int(consume["amount"])
            and inventory_counts(row.get("inventory", [])).get(int(grant["item_id"]), 0)
                == before.get(int(grant["item_id"]), 0) + int(grant["amount"]), timeout=timeout)
        after_counts = inventory_counts(after.get("inventory", []))
        if (after_counts.get(int(consume["item_id"]), 0) != before.get(int(consume["item_id"]), 0) - int(consume["amount"])
                or after_counts.get(int(grant["item_id"]), 0) != before.get(int(grant["item_id"]), 0) + int(grant["amount"])):
            raise RuntimeError(f"OpenKore inventory did not match branch outcome {choice_id}")
        return {"id": choice_id, "status": "PASS", "type": "npc_dialogue",
                "selected_choice": label, "available_choices": observed,
                "inventory_before": {str(key): value for key, value in before.items()},
                "inventory_after": {str(key): value for key, value in after_counts.items()},
                "client_item_removed_message": bool(removed), "client_item_added_message": bool(added),
                "branch_message_observed": bool(re.search(re.escape(message), self.output[cursor:], re.I)),
                "evidence": "OpenKore native talknpc text-matched choice, NPC response event, and server inventory packets"}

    def unload_npc(self, name: str, timeout: float = 15) -> None:
        self.command(f"c @unloadnpc {name}")
        self._wait(r"Npc Disabled\.", timeout=timeout)
        self.state(lambda row: row.get("connected_in_game") is True and not any(
            npc.get("name", "").casefold() == name.casefold()
            for npc in row.get("nearby_npcs", [])), timeout=timeout)

    def unload_npc_file(self, relative_file: str, npc_name: str, timeout: float = 20) -> dict:
        """Remove one controlled NPC source file and verify its NPC is no longer visible."""
        if not re.fullmatch(r"npc/custom/korelab_[a-z0-9_-]{1,64}\.txt", relative_file, re.I):
            raise ValueError("temporary NPC source path is invalid")
        if not re.fullmatch(r"KoreLab[A-Za-z0-9_]{1,40}", npc_name):
            raise ValueError("temporary NPC name is invalid")
        cursor = len(self.output)
        self.command(f"c @unloadnpcfile {relative_file}")
        response = self._wait_after(r"File unloaded\.|File not found\.|@unloadnpcfile failed\.",
                                    cursor, timeout=timeout)
        if not response.group(0).casefold().startswith("file unloaded"):
            raise RuntimeError(f"rAthena rejected controlled NPC source unload: {response.group(0)}")
        state = self.state(lambda row: row.get("connected_in_game") is True and not any(
            npc.get("name", "").casefold() == npc_name.casefold()
            for npc in row.get("nearby_npcs", [])), timeout=timeout)
        return {"npc_name": npc_name, "relative_file": relative_file,
                "native_response": response.group(0), "state": state,
                "evidence": "rAthena @unloadnpcfile acknowledgement and fresh OpenKore stateObserver absence"}

    def reload_quest_database(self, timeout: float = 15) -> None:
        cursor = len(self.output)
        self.command("c @reloadquestdb")
        self._wait_after(r"Quest database has been reloaded", cursor, timeout=timeout)

    def run_macro(self, name: str, marker: str, expected_position: dict[str, int],
                  map_name: str, timeout: float = 30, *,
                  progress_marker: str | None = None,
                  progress_target: dict[str, int] | None = None,
                  interrupt_after_progress: bool = False) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not re.fullmatch(r"KoreLabMacro_[A-Z0-9]{8}", marker):
            raise ValueError("macro name/marker is invalid")
        if interrupt_after_progress and (
                not progress_marker
                or not re.fullmatch(r"KoreLabProgress_[A-Z0-9]{8}", progress_marker)
                or not isinstance(progress_target, dict)
                or not all(type(progress_target.get(axis)) is int for axis in ("x", "y"))):
            raise ValueError("controlled macro interruption requires a progress marker and integer progress target")
        cursor = len(self.output)
        self.command(f"macro {name}")
        lifecycle = [{"state": "started", "evidence": "native macro command issued to OpenKore"}]
        if interrupt_after_progress:
            self._wait_after(rf"\[macro log\] {re.escape(progress_marker)}", cursor, timeout=timeout)
            progress = self.state(lambda row: row.get("connected_in_game") is True
                and row.get("map") == map_name and position(row) == progress_target
                and row.get("movement_state") == "stationary", timeout=timeout)
            lifecycle.append({"state": "progress", "position": position(progress),
                              "evidence": "structured observer confirmed the macro's first movement"})
            interrupt_cursor = len(self.output)
            self.command("relog 1")
            self._wait_after(r"Relogging in 1 seconds", interrupt_cursor, timeout=10)
            offline_match = self._wait_after(r"(?:Disconnecting|disconnected|Logging out)",
                                             interrupt_cursor, timeout=timeout)
            self._wait_after(r"You are now in the game", interrupt_cursor, timeout=timeout)
            reconnected = self.state(lambda row: row.get("connected_in_game") is True
                and row.get("map") == map_name, timeout=timeout)
            if re.search(rf"\[macro log\] {re.escape(marker)}", self.output[interrupt_cursor:], re.I):
                raise RuntimeError("macro interruption failed: completion marker appeared after reconnect")
            if position(reconnected) != progress_target:
                raise RuntimeError("macro interruption did not preserve the independently observed partial position")
            lifecycle.extend([
                {"state": "interrupted", "classification": "controlled_relog",
                 "evidence": "OpenKore logout transition observed while macro had not completed"},
                {"state": "reconnected", "position": position(reconnected),
                 "evidence": "structured observer confirmed a new in-game session"},
            ])
            return {"type": "macro", "name": name, "status": "INTERRUPTED",
                    "failure_classification": "controlled_relog",
                    "completion_marker_observed": False, "lifecycle": lifecycle,
                    "partial_position": position(reconnected),
                    "evidence": "progress and logout/reconnect are independently visible; completion marker absent"}
        if progress_marker:
            self._wait_after(rf"\[macro log\] {re.escape(progress_marker)}", cursor, timeout=timeout)
            lifecycle.append({"state": "progress", "evidence": "native macro progress marker observed"})
        self._wait_after(rf"\[macro log\] {re.escape(marker)}", cursor, timeout=timeout)
        observed = self.state(lambda row: row.get("type") == "state"
            and row.get("connected_in_game") is True and row.get("map") == map_name
            and position(row) == expected_position and row.get("movement_state") == "stationary",
            timeout=timeout)
        lifecycle.append({"state": "completed", "position": position(observed),
                          "evidence": "completion marker and final structured position both observed"})
        return {"type": "macro", "name": name, "status": "PASS", "completion_marker": marker,
                "completion_marker_observed": True, "lifecycle": lifecycle,
                "openkore_position": position(observed),
                "evidence": "native macro completion marker and final structured observer state"}

    def equip_item(self, item_id: int, item_name: str, *, expect_success: bool,
                   timeout: float = 15) -> dict:
        before = self.state(lambda row: row.get("connected_in_game") is True and any(
            int(candidate.get("item_id", -1)) == item_id
            for candidate in row.get("inventory", [])), timeout=timeout)
        candidates = [item for item in before.get("inventory", [])
                      if int(item.get("item_id", -1)) == item_id and not int(item.get("equipped", 0))]
        if not candidates:
            raise RuntimeError(f"OpenKore has no unequipped copy of item {item_name}")
        item = candidates[0]
        sample_count = len(self._observer_rows())
        cursor = len(self.output)
        self.command(f"eq {int(item['client_index'])}")
        item_name_end = rf"(?:\b|(?=\s|\(|$))"
        item_name = re.escape(item_name)
        if expect_success:
            self._wait_after(rf"(?:You equip {item_name}{item_name_end}|"
                             rf"Arrow/Bullet equipped: {item_name}\s*\()", cursor, timeout=timeout)
            state = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
                and any(int(candidate.get("item_id", -1)) == item_id
                        and int(candidate.get("equipped", 0)) != 0
                        for candidate in row.get("inventory", [])), timeout=timeout)
            return {"status": "PASS", "item_id": item_id, "item_name": item_name,
                    "equipped": True, "openkore_equipped_mask": next(
                        int(candidate["equipped"]) for candidate in state["inventory"]
                        if int(candidate.get("item_id", -1)) == item_id and int(candidate.get("equipped", 0))),
                    "evidence": "OpenKore native eq command acknowledgement and structured inventory observer"}
        self._wait_after(rf"You can't put on {item_name}{item_name_end}", cursor, timeout=timeout)
        state = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and any(int(candidate.get("item_id", -1)) == item_id
                    and int(candidate.get("equipped", 0)) == 0
                    for candidate in row.get("inventory", [])), timeout=timeout)
        return {"status": "PASS", "item_id": item_id, "item_name": item_name,
                "equipped": False, "server_rejection_observed": True,
                "evidence": "OpenKore received rAthena's equip rejection and observer still shows unequipped"}

    def unequip_item(self, item_id: int, item_name: str, timeout: float = 15) -> dict:
        before = self.state(lambda row: row.get("connected_in_game") is True and any(
            int(candidate.get("item_id", -1)) == item_id and int(candidate.get("equipped", 0))
            for candidate in row.get("inventory", [])), timeout=timeout)
        candidates = [item for item in before.get("inventory", [])
                      if int(item.get("item_id", -1)) == item_id and int(item.get("equipped", 0))]
        if not candidates:
            raise RuntimeError(f"OpenKore has no equipped copy of item {item_name}")
        item = candidates[0]
        sample_count = len(self._observer_rows())
        cursor = len(self.output)
        self.command(f"uneq {int(item['client_index'])}")
        item_name_pattern = re.escape(item_name)
        self._wait_after(rf"(?:You unequip {item_name_pattern}\b|"
                         rf"Arrow/Bullet unequipped: {item_name_pattern}\s*\()", cursor, timeout=timeout)
        state = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and any(int(candidate.get("item_id", -1)) == item_id
                    and int(candidate.get("equipped", 0)) == 0
                    for candidate in row.get("inventory", [])), timeout=timeout)
        return {"status": "PASS", "item_id": item_id, "item_name": item_name,
                "equipped": False,
                "evidence": "OpenKore native uneq command acknowledgement and structured inventory observer"}

    def _observer_rows(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.observer_file.read_text().splitlines()]
        except (OSError, json.JSONDecodeError):
            return []

    def spawn_monster(self, aegis_name: str, test_name: str, event_label: str | None,
                      timeout: float = 15, require_clear: bool = False,
                      count: int = 1) -> dict:
        if not re.fullmatch(r"[A-Z0-9_]+", aegis_name):
            raise ValueError("monster AegisName has unsupported characters")
        if not re.fullmatch(r"KoreLab[A-Z][0-9]{6}", test_name):
            raise ValueError("test monster name must be a unique KoreLab name")
        if event_label not in (None, "KoreLabCombat::OnMobDeath"):
            raise ValueError("unsupported rAthena test-monster event label")
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("controlled monster count must be from 1 to 100")
        before = self.state(lambda row: row.get("connected_in_game") is True)
        if require_clear and before.get("nearby_monsters"):
            raise RuntimeError("combat test requires a clear local view before spawning its monster")
        tag = f" {event_label}" if event_label else ""
        self.command(f'c @monster "{test_name}" {aegis_name} {count}{tag}')
        return self.state(lambda row: row.get("connected_in_game") is True
            and sum(m.get("name", "").casefold() == test_name.casefold()
                    for m in row.get("nearby_monsters", [])) >= count, timeout)

    def attack_monster(self, test_name: str, timeout: float = 60) -> dict:
        initial = self.state(lambda row: row.get("connected_in_game") is True
            and any(m.get("name", "").casefold() == test_name.casefold()
                    for m in row.get("nearby_monsters", [])))
        sample_count = len(self.observer_file.read_text().splitlines())
        target = next(m for m in initial["nearby_monsters"]
                      if m.get("name", "").casefold() == test_name.casefold())
        # OpenKore's registered native attack command is the `a` alias.
        self.command(f"a {int(target['client_index'])}")
        return self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and not any(m.get("name", "").casefold() == test_name.casefold()
                        for m in row.get("nearby_monsters", [])), timeout)

    def collect_monster_drop(self, item_id: int, item_name: str, timeout: float = 20,
                             client_index: int | None = None) -> dict:
        """Use native take for an observed drop and confirm its inventory delta."""
        if type(item_id) is not int or item_id <= 0:
            raise ValueError("pickup requires a positive item ID")
        if client_index is not None and (type(client_index) is not int or client_index < 0):
            raise ValueError("pickup client index must be a non-negative integer")
        before = self.state(lambda row: row.get("connected_in_game") is True)
        before_amount = inventory_counts(before.get("inventory", [])).get(item_id, 0)
        end = min(self.deadline, time.monotonic() + timeout)
        if client_index is not None and not any(
                int(drop.get("item_id", -1)) == item_id and int(drop.get("amount", 0)) > 0
                and int(drop.get("client_index", -1)) == client_index
                for drop in before.get("nearby_ground_items", [])):
            raise RuntimeError("the observed ground item is no longer visible at the requested client index")
        current = self.state(lambda row: row.get("connected_in_game") is True and any(
            int(drop.get("item_id", -1)) == item_id and int(drop.get("amount", 0)) > 0
            and (client_index is None or int(drop.get("client_index", -1)) == client_index)
            for drop in row.get("nearby_ground_items", [])), timeout=max(0.1, end-time.monotonic()))
        drop = next(drop for drop in current["nearby_ground_items"]
                    if int(drop.get("item_id", -1)) == item_id and int(drop.get("amount", 0)) > 0
                    and (client_index is None or int(drop.get("client_index", -1)) == client_index))
        sample_count = len(self._observer_rows())
        self.command(f"take {int(drop['client_index'])}")
        collected = self.state_after(sample_count, lambda row:
            row.get("connected_in_game") is True
            and inventory_counts(row.get("inventory", [])).get(item_id, 0) >= before_amount + 1,
            timeout=max(0.1, end-time.monotonic()))
        return {"status": "PASS", "item_id": item_id, "item_name": item_name,
                "client_index": int(drop["client_index"]),
                "ground_item_position": drop.get("position"),
                "inventory_before": before_amount,
                "inventory_after": inventory_counts(collected.get("inventory", [])).get(item_id, 0),
                "evidence": "OpenKore native take command and structured inventory observer confirmed the drop pickup"}

    def attack_until_dead(self, test_name: str, timeout: float = 60) -> dict:
        initial = self.state(lambda row: row.get("connected_in_game") is True
            and any(m.get("name", "").casefold() == test_name.casefold()
                    for m in row.get("nearby_monsters", [])))
        sample_count = len(self.observer_file.read_text().splitlines())
        target = next(m for m in initial["nearby_monsters"]
                      if m.get("name", "").casefold() == test_name.casefold())
        self.command(f"a {int(target['client_index'])}")
        return self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and (row.get("character_attributes", {}).get("is_dead") is True
                 or not any(m.get("name", "").casefold() == test_name.casefold()
                            for m in row.get("nearby_monsters", []))), timeout,
            allow_dead=True)

    def flee_from_monster(self, test_name: str, destination: dict[str, int],
                          timeout: float = 30) -> dict:
        initial = self.state(lambda row: row.get("connected_in_game") is True
            and any(m.get("name", "").casefold() == test_name.casefold()
                    for m in row.get("nearby_monsters", [])))
        if initial.get("escape_destination_walkable") is not True:
            raise ValueError("declared escape destination is not walkable on the current OpenKore map")
        start = position(initial, False)
        sample_count = len(self.observer_file.read_text().splitlines())
        target = next(m for m in initial["nearby_monsters"]
                      if m.get("name", "").casefold() == test_name.casefold())
        self.command(f"a {int(target['client_index'])}")
        engaged = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and any(m.get("name", "").casefold() == test_name.casefold()
                    and int(m.get("damage_from", 0)) > 0 for m in row.get("nearby_monsters", [])),
            min(timeout, 15))
        engage_count = len(self.observer_file.read_text().splitlines())
        self.command("as")
        self.command(f"move {int(destination['x'])} {int(destination['y'])}")
        result = self.state_after(engage_count, lambda row: row.get("connected_in_game") is True
            and position(row) == destination and row.get("movement_state") == "stationary", timeout)
        end = position(result)
        if start is None or end is None or start == end:
            raise RuntimeError("OpenKore did not move away during the flee action")
        if result.get("character_attributes", {}).get("is_dead") is True:
            raise RuntimeError("character died before the flee destination was reached")
        return {"start_position": start, "destination": end,
                "character_attributes": result.get("character_attributes"),
                "nearby_monsters": result.get("nearby_monsters", []),
                "combat_stats": result.get("combat_stats"),
                "opponent_damage_received": next((int(m.get("damage_from", 0))
                    for m in engaged.get("nearby_monsters", [])
                    if m.get("name", "").casefold() == test_name.casefold()), 0)}

    def revive_test_character(self, timeout: float = 10) -> dict:
        sample_count = len(self.observer_file.read_text().splitlines())
        self.command("c @alive")
        self._wait(r"You've been revived!", timeout=timeout)
        return self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and row.get("character_attributes", {}).get("is_dead") is not True
            and int(row.get("character_attributes", {}).get("hp", 0)) > 0, timeout=timeout)

    def use_self_skill(self, skill_id: str, level: int, timeout: float = 15) -> dict:
        if not skill_id.isdigit() or not 1 <= level <= 10:
            raise ValueError("self skill requires a numeric ID and level from 1 to 10")
        before = self.state(lambda row: row.get("connected_in_game") is True)
        skills = before.get("character_skills", {})
        if int(skills.get(skill_id, 0)) < level:
            raise RuntimeError("OpenKore did not observe the requested skill at the required level")
        hp_before = int(before.get("character_attributes", {}).get("hp", 0))
        sp_before = int(before.get("character_attributes", {}).get("sp", 0))
        sample_count = len(self.observer_file.read_text().splitlines())
        self.command(f"ss {int(skill_id)} {level}")
        result = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and int(row.get("character_attributes", {}).get("sp", sp_before)) < sp_before
            and int(row.get("character_attributes", {}).get("hp", hp_before)) > hp_before, timeout)
        return {"hp_before": hp_before,
                "hp_after": int(result["character_attributes"]["hp"]),
                "sp_before": sp_before,
                "sp_after": int(result["character_attributes"]["sp"]),
                "skill_level": int(result["character_skills"].get(skill_id, 0)),
                "evidence": "OpenKore observer saw self-skill HP and SP changes"}

    def change_job(self, job_name: str, timeout: float = 10) -> dict:
        if not re.fullmatch(r"(?:[A-Za-z_]+|[0-9]{1,4})", job_name):
            raise ValueError("job name or ID contains unsupported characters")
        sample_count = len(self.observer_file.read_text().splitlines())
        self.command(f"c @jobchange {job_name}")
        self._wait(r"Your job has been changed", timeout=timeout)
        return self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and row.get("character_attributes", {}).get("class") is not None, timeout)

    def grant_skill_points(self, count: int, timeout: float = 10) -> None:
        if not 1 <= count <= 10:
            raise ValueError("temporary skill point grant must be between 1 and 10")
        self.command(f"c @skillpoint {count}")
        self._wait(r"Number of skill points changed", timeout=timeout)

    def heal_test_character(self, timeout: float = 10) -> dict:
        before = self.state(lambda row: row.get("connected_in_game") is True)
        sample_count = len(self.observer_file.read_text().splitlines())
        self.command("c @heal")
        return self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and int(row.get("character_attributes", {}).get("sp", 0))
            >= int(row.get("character_attributes", {}).get("max_sp", 1)), timeout)

    def learn_skill(self, skill_id: str, level: int, timeout: float = 10) -> dict:
        if not skill_id.isdigit() or not 1 <= level <= 10:
            raise ValueError("learned skill requires a numeric ID and level from 1 to 10")
        for _ in range(level):
            current = self.state(lambda row: row.get("connected_in_game") is True)
            current_level = int(current.get("character_skills", {}).get(skill_id, 0))
            if current_level >= level:
                return current
            sample_count = len(self.observer_file.read_text().splitlines())
            self.command(f"skills add {int(skill_id)}")
            current = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
                and int(row.get("character_skills", {}).get(skill_id, 0)) > current_level, timeout)
        return current

    def use_monster_skill(self, skill_id: str, test_name: str, level: int,
                          timeout: float = 20, cast_range: int = 2) -> dict:
        if not skill_id.isdigit() or not 1 <= level <= 10:
            raise ValueError("monster skill requires a numeric ID and level from 1 to 10")
        if type(cast_range) is not int or not 1 <= cast_range <= 14:
            raise ValueError("monster skill cast range must be from 1 to 14 cells")
        initial = self.state(lambda row: row.get("connected_in_game") is True
            and any(m.get("name", "").casefold() == test_name.casefold()
                    for m in row.get("nearby_monsters", [])))
        target = next(m for m in initial["nearby_monsters"]
                      if m.get("name", "").casefold() == test_name.casefold())
        target_position = target.get("position")
        current_position = position(initial)
        if (isinstance(target_position, dict) and current_position
                and max(abs(target_position[axis] - current_position[axis]) for axis in ("x", "y")) > cast_range):
            move_sample_count = len(self.observer_file.read_text().splitlines())
            self.command(f"move {int(target_position['x'])} {int(target_position['y'])}")
            initial = self.state_after(move_sample_count, lambda row: row.get("connected_in_game") is True
                and row.get("movement_state") == "stationary"
                and position(row) is not None
                and any(m.get("name", "").casefold() == test_name.casefold()
                        for m in row.get("nearby_monsters", [])), timeout=timeout)
            target = next(m for m in initial["nearby_monsters"]
                          if m.get("name", "").casefold() == test_name.casefold())
        before_damage = int(target.get("damage_to", 0))
        before_sp = int(initial.get("character_attributes", {}).get("sp", 0))
        sample_count = len(self.observer_file.read_text().splitlines())
        self.command(f"sm {int(skill_id)} {int(target['client_index'])} {level}")
        result = self.state_after(sample_count, lambda row: row.get("connected_in_game") is True
            and (int(row.get("character_attributes", {}).get("sp", before_sp)) < before_sp
                 or not any(m.get("name", "").casefold() == test_name.casefold()
                            for m in row.get("nearby_monsters", []))), timeout)
        final_target = next((m for m in result.get("nearby_monsters", [])
                             if m.get("name", "").casefold() == test_name.casefold()), None)
        damage_after = int(final_target.get("damage_to", 0)) if final_target else None
        if int(result.get("character_attributes", {}).get("sp", before_sp)) >= before_sp:
            raise RuntimeError("OpenKore skill command did not consume SP")
        if damage_after is not None and damage_after <= before_damage:
            raise RuntimeError("OpenKore skill command did not damage its selected monster")
        return {"skill_id": skill_id, "skill_level": level,
                "monster_damage_before": before_damage, "monster_damage_after": damage_after,
                "hp_before": int(initial["character_attributes"].get("hp", 0)),
                "hp_after": int(result["character_attributes"].get("hp", 0)),
                "sp_before": before_sp, "sp_after": int(result["character_attributes"]["sp"]),
                "evidence": "OpenKore observer saw skill SP cost and monster damage"}

    def remove_test_monsters(self, map_name: str, event_label: str, *,
                             owned_names: tuple[str, ...] = (),
                             must_preserve_names: tuple[str, ...] = ()) -> dict:
        if not re.fullmatch(r"[a-z0-9_]+", map_name):
            raise ValueError("unsupported map name for monster cleanup")
        if event_label != "KoreLabCombat::OnMobDeath":
            raise ValueError("unsupported rAthena test-monster event label")
        if self.process and self.process.poll() is None:
            sample_count = len(self.observer_file.read_text().splitlines())
            before = self.state(lambda row: row.get("connected_in_game") is True)
            owned = {name.casefold() for name in owned_names}
            owned_ids = {mob.get("id") for mob in before.get("nearby_monsters", [])
                         if str(mob.get("name", "")).casefold() in owned}
            unrelated_ids = {mob.get("id") for mob in before.get("nearby_monsters", [])
                             if str(mob.get("name", "")).casefold() not in owned}
            self.command(f"c @killmonster2 {map_name} {event_label}")
            # rAthena's @killmonster2 reports this only after applying the map
            # command. Keep OpenKore alive until that acknowledgement arrives.
            self._wait(r"All monsters killed!", timeout=10)
            required_preserved = {name.casefold() for name in must_preserve_names}
            after = self.state_after(
                sample_count,
                lambda row: row.get("connected_in_game") is True
                and not (owned & {str(mob.get("name", "")).casefold()
                                  for mob in row.get("nearby_monsters", [])})
                and required_preserved.issubset(
                    {str(mob.get("name", "")).casefold()
                     for mob in row.get("nearby_monsters", [])}),
                timeout=8)
            after_ids = {mob.get("id") for mob in after.get("nearby_monsters", [])}
            remaining_names = {str(mob.get("name", "")).casefold()
                               for mob in after.get("nearby_monsters", [])}
            if owned & remaining_names:
                raise RuntimeError("rAthena event cleanup left a scenario-owned monster alive")
            if not required_preserved.issubset(remaining_names):
                raise RuntimeError("selective cleanup did not preserve its unrelated control monster")
            return {"removed_nearby_ids": sorted(owned_ids),
                    "preserved_nearby_ids": sorted(unrelated_ids & after_ids),
                    "remaining_names": sorted(remaining_names)}
        return {}

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.command("quit")
                self.process.wait(timeout=5)
            except Exception:
                if os.name == "nt":
                    try:
                        self.process.send_signal(signal.CTRL_BREAK_EVENT)
                        self.process.wait(timeout=5)
                    except Exception:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=3)
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGINT)
                        self.process.wait(timeout=5)
                    except Exception:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait(timeout=3)
        if self.process:
            if self.log_pending:
                self.log.write(redact(self.log_pending, self.sensitive_values))
                self.log.flush()
            if self.master_fd is not None:
                os.close(self.master_fd)
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.stdout:
                self.process.stdout.close()
            self.log.close()
        if self.persisted_runtime_config_path is not None and self.process:
            source = self.temp_path / "config.txt"
            persist_run_local_config(source, self.persisted_runtime_config_path, self.run_dir)
        self.temp.cleanup()
