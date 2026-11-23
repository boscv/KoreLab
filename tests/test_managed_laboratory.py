from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from korelab.laboratory import ManagedLaboratory


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    openkore = tmp_path / "openkore"
    (openkore / "tables").mkdir(parents=True)
    (openkore / "fields").mkdir()
    (openkore / "openkore.pl").write_text("# fixture\n", encoding="utf-8")
    (openkore / "tables/servers.txt").write_text("# fixture\n", encoding="utf-8")

    rathena = tmp_path / "rathena"
    for folder in ("conf/import", "db/import", "db/import-tmpl", "db/re", "npc", "sql-files"):
        (rathena / folder).mkdir(parents=True, exist_ok=True)
    (rathena / "db/import/custom_override.yml").write_text("fixture: preserved\n", encoding="utf-8")
    (rathena / "db/import-tmpl/template.yml").write_text("fixture: template\n", encoding="utf-8")
    (rathena / "conf/import/inter_conf.txt").write_text(
        "log_db_db: log\nlog_db_id: ragnarok\nlog_db_pw: stale\n", encoding="utf-8")
    for name in ("login-server", "char-server", "map-server"):
        (rathena / name).write_text("fixture executable\n", encoding="utf-8")
    (rathena / "npc/scripts_custom.conf").write_text("// local scripts\n", encoding="utf-8")
    (rathena / "npc/re").mkdir(parents=True)
    (rathena / "npc/re/scripts_monsters.conf").write_text(
        "npc: npc/re/mobs/championmobs.txt\nnpc: npc/re/mobs/fields/prontera.txt\n",
        encoding="utf-8")
    (rathena / "npc/re/mobs").mkdir()
    (rathena / "npc/re/mobs/championmobs.txt").write_text("// source fixture\n")
    (rathena / "npc/re/mobs/fields").mkdir()
    (rathena / "npc/re/mobs/fields/prontera.txt").write_text(
        "prt_fild08,0,0\tmonster\tPoring\t1002,20,5000\n"
        "prt_fild08,0,0\tmonster\tLunatic\t1063,20,5000\n"
        "prt_fild05,0,0\tmonster\tHornet\t1004,199,5000\n",
        encoding="utf-8")
    for name in ("login_athena.conf", "char_athena.conf", "map_athena.conf",
                 "inter_athena.conf", "lan_athena.conf"):
        (rathena / "conf" / name).write_text("bind_ip: 0.0.0.0\n", encoding="utf-8")
    (rathena / "conf/packet_athena.conf").write_text(
        "enable_ip_rules: yes\nddos_interval: 3000\nddos_count: 5\n",
        encoding="utf-8")

    xstools = tmp_path / "XSTools"
    xstools.mkdir()
    (xstools / ".perl-config").write_text("fixture\n", encoding="utf-8")
    (xstools / "XSTools.bundle").write_bytes(b"fixture")
    return openkore, rathena, xstools


def test_create_is_private_and_confined_to_managed_root(tmp_path: Path, sources) -> None:
    openkore, rathena, xstools = sources
    manager = ManagedLaboratory(tmp_path / "private-data")

    result = manager.create(openkore=openkore, rathena_runtime=rathena,
                            xstools_overlay=xstools)

    home = Path(result["path"])
    secrets_file = home / "secrets.json"
    profile_config = home / "openkore/config.txt"
    assert result["status"] == "PREPARED"
    assert result["id"] in home.name
    assert result["character"]
    assert "password" not in json.dumps(result).casefold()
    assert (home / "npc").is_symlink()
    assert (home / "db/import").is_dir()
    assert not (home / "db/import").is_symlink()
    assert (home / "db/import/custom_override.yml").read_text(encoding="utf-8") == "fixture: preserved\n"
    assert secrets_file.stat().st_mode & 0o077 == 0
    assert profile_config.stat().st_mode & 0o077 == 0
    assert "portalCompile 1" in profile_config.read_text(encoding="utf-8")
    assert (home / "openkore/KoreLabCreateCharacter.pl").stat().st_mode & 0o077 == 0
    login_config = (home / "conf/login_athena.conf").read_text(encoding="utf-8")
    assert "allowed_regs: 50" in login_config
    assert "time_allowed: 1" in login_config
    packet_config = (home / "conf/packet_athena.conf").read_text(encoding="utf-8")
    assert "enable_ip_rules: yes" in packet_config
    assert "allow: 127.0.0.1" in packet_config
    imported_db_config = (home / "conf/import/inter_conf.txt").read_text(encoding="utf-8")
    assert "log_db_db: ragnarok" in imported_db_config
    assert "log_db_pw: stale" not in imported_db_config
    assert json.loads((home / "openkore/profile.json").read_text(encoding="utf-8"))["server_type"] == "kRO_RagexeRE_2021_11_03"
    assert json.loads((home / "openkore/profile.json").read_text(encoding="utf-8"))["fields_folder"] == str((openkore / "fields").resolve())
    assert manager.destroy(result["id"]) == {"id": result["id"], "destroyed": True}
    assert not home.exists()
    assert (rathena / "db/import-tmpl").is_dir()
    assert (rathena / "db/import/custom_override.yml").read_text(encoding="utf-8") == "fixture: preserved\n"


def test_fleet_profiles_are_unique_private_and_use_a_private_combat_observer(tmp_path: Path, sources) -> None:
    openkore, rathena, xstools = sources
    manager = ManagedLaboratory(tmp_path / "private-data")
    original_custom = (rathena / "npc/scripts_custom.conf").read_text(encoding="utf-8")

    result = manager.create(openkore=openkore, rathena_runtime=rathena,
        xstools_overlay=xstools, agent_count=5, combat_observer=True)

    home = Path(result["path"])
    owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
    secrets_file = home / "secrets.json"
    secrets = json.loads(secrets_file.read_text(encoding="utf-8"))
    agents = owner["agents"]
    assert result["agent_count"] == len(agents) == 5
    assert len({agent["account"] for agent in agents}) == 5
    assert len({agent["character"] for agent in agents}) == 5
    assert len(secrets["fleet_agents"]) == 5
    assert secrets_file.stat().st_mode & 0o077 == 0
    assert "password" not in json.dumps(result).casefold()
    assert (home / "npc").is_dir() and not (home / "npc").is_symlink()
    custom = (home / "npc/scripts_custom.conf").read_text(encoding="utf-8")
    assert "npc: npc/korelab_fleet_observer.txt" in custom
    observer = (home / "npc/korelab_fleet_observer.txt").read_text()
    assert "OnNPCKillEvent:" in observer
    assert "getcharid(0)" in observer
    monsters_config = (home / "npc/re/scripts_monsters.conf").read_text(encoding="utf-8")
    assert "npc/re/mobs/championmobs.txt" not in monsters_config
    assert "npc/re/mobs/fields/prontera.txt" in monsters_config
    assert "npc/re/mobs/championmobs.txt" in (rathena / "npc/re/scripts_monsters.conf").read_text()
    safe_spawns = (home / "npc/re/mobs/fields/prontera.txt").read_text(encoding="utf-8")
    assert "prt_fild08,0,0\tmonster\tPoring" in safe_spawns
    assert "prt_fild08,0,0\tmonster\tLunatic" not in safe_spawns
    assert "prt_fild05,0,0\tmonster\tHornet" in safe_spawns
    assert "prt_fild08,0,0\tmonster\tLunatic" in (
        rathena / "npc/re/mobs/fields/prontera.txt").read_text(encoding="utf-8")
    assert owner["fleet_fixture_overrides"]
    assert (rathena / "npc/scripts_custom.conf").read_text(encoding="utf-8") == original_custom
    for agent in agents:
        profile_path = home / agent["profile_file"]
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        config_path = Path(profile["profile_config"])
        assert config_path.stat().st_mode & 0o077 == 0
        assert profile["character"] == agent["character"]
        assert profile["agent_id"] == agent["id"] or agent["id"] == "agent-001"
    assert manager.destroy(result["id"])["destroyed"] is True


def test_native_account_bootstrap_respects_the_private_rathena_registration_window(tmp_path: Path,
                                                                                   monkeypatch) -> None:
    import korelab.laboratory as laboratory

    home = tmp_path / "lab"
    (home / "conf/import").mkdir(parents=True)
    (home / "conf/login_athena.conf").write_text(
        "allowed_regs: 1\ntime_allowed: 10\n", encoding="utf-8")
    (home / "conf/import/login_conf.txt").write_text("time_allowed: 12\n", encoding="utf-8")
    assert ManagedLaboratory._account_registration_limits(home) == (1, 12)

    delays = []
    monkeypatch.setattr(laboratory.time, "time", lambda: 100.0)
    monkeypatch.setattr(laboratory.time, "sleep", delays.append)
    owner = {"account_registration_times": [95.0]}
    ManagedLaboratory(tmp_path / "data")._wait_for_account_registration_capacity(home, owner)
    assert delays == [pytest.approx(7.25)]
    assert owner["account_registration_times"] == [95.0]


def test_destroy_refuses_to_remove_lab_when_owned_service_is_unconfirmed(
        tmp_path: Path, sources, monkeypatch) -> None:
    openkore, rathena, xstools = sources
    manager = ManagedLaboratory(tmp_path / "private-data")
    result = manager.create(openkore=openkore, rathena_runtime=rathena,
                            xstools_overlay=xstools)
    home = Path(result["path"])
    monkeypatch.setattr(manager, "stop", lambda _lab_id: {
        "id": result["id"], "stopped": [], "unconfirmed": ["map"],
        "state": "cleanup_unconfirmed"})

    with pytest.raises(RuntimeError, match="refusing to remove laboratory files"):
        manager.destroy(result["id"])

    assert home.is_dir()
    assert (home / "owner.json").is_file()


def test_generated_character_selector_sends_only_one_selection(tmp_path: Path, sources) -> None:
    perl = shutil.which("perl")
    if not perl:
        pytest.skip("Perl is required to execute OpenKore's generated plugin regression")
    openkore, rathena, xstools = sources
    manager = ManagedLaboratory(tmp_path / "private-data")
    result = manager.create(openkore=openkore, rathena_runtime=rathena,
                            xstools_overlay=xstools)
    plugin = Path(result["path"]) / "openkore/KoreLabCreateCharacter.pl"
    wanted = result["character"]
    perl_lib = tmp_path / "perl-lib"
    perl_lib.mkdir()
    (perl_lib / "Globals.pm").write_text(r"""
package Globals;
use strict;
our @chars; our %config; our %charSvrSet; our ($net, $messageSender);
sub configModify { my ($key,$value)=@_; $config{$key}=$value; }
sub import { my ($class,@names)=@_; my $caller=caller; no strict 'refs';
 for my $name (@names) { my $symbol=substr($name,1); *{"${caller}::$symbol"} = $name eq '@chars' ? \@chars :
   $name eq '%config' ? \%config : $name eq '%charSvrSet' ? \%charSvrSet :
   $name eq '$net' ? \$net : \$messageSender; }
 *{"${caller}::configModify"}=\&configModify;
}
1;
""", encoding="utf-8")
    (perl_lib / "Plugins.pm").write_text(r"""
package Plugins;
use strict;
our %hooks; our $next=0;
sub register { }
sub addHook { my ($name,$callback)=@_; my $id=++$next; $hooks{$name}=$callback; return $id; }
sub delHook { }
1;
""", encoding="utf-8")
    (perl_lib / "Misc.pm").write_text(r"""
package Misc;
use strict;
our $created=0;
sub createCharacter { my ($slot,$name)=@_; $created++; @Globals::chars=({name=>$name}); return 1; }
1;
""", encoding="utf-8")
    (perl_lib / "Network.pm").write_text("package Network; 1;\n", encoding="utf-8")
    harness = tmp_path / "exercise.pl"
    harness.write_text(f"""
    use strict; use warnings;
    use Globals;
use Plugins;
{{ package FakeNet; sub getState {{ 2 }} }}
{{ package FakeSender; our $count=0; sub sendCharLogin {{ $count++ }} }}
    $Globals::net=bless {{}}, 'FakeNet'; $Globals::messageSender=bless {{}}, 'FakeSender';
    $Globals::config{{username}}='lab-account';
require '{plugin}';
my $hook=$Plugins::hooks{{'charSelectScreen'}} or die "hook missing";
    if (($ARGV[0] // '') eq 'create') {{
      @Globals::chars=();
      my $pending={{}}; $hook->('charSelectScreen',$pending);
      die "creation must wait for page count" unless $pending->{{pin_return}}==2 && $FakeSender::count==0;
      die "creation must not run before page sync" unless $Misc::created==0;
      @Globals::charSvrSet{{sync_Count}}=5;
      @Globals::charSvrSet{{sync_received_characters}}=5;
      my $first={{}}; $hook->('charSelectScreen',$first);
      die "native creation should be requested once" unless $first->{{pin_return}}==2 && $FakeSender::count==0;
      my $second={{}}; $hook->('charSelectScreen',$second);
      die "created character selection should be intercepted" unless $second->{{pin_return}}==1;
      die "created character should be selected exactly once" unless $FakeSender::count==1;
    }} else {{
      @Globals::chars=({{name=>'{wanted}'}});
      my $pending={{}}; $hook->('charSelectScreen',$pending);
      die "selection must wait for page count" unless $pending->{{pin_return}}==2 && $FakeSender::count==0;
      @Globals::charSvrSet{{sync_Count}}=1;
      @Globals::charSvrSet{{sync_received_characters}}=1;
      my $first={{}}; $hook->('charSelectScreen',$first);
      my $second={{}}; $hook->('charSelectScreen',$second);
      die "selection should be intercepted on both callbacks" unless $first->{{pin_return}}==1 && $second->{{pin_return}}==1;
      die "native selection should be sent exactly once" unless $FakeSender::count==1;
      die "character slot should be configured" unless $Globals::config{{char}} eq '0';
    }}
""", encoding="utf-8")
    env = os.environ.copy()
    env["PERL5LIB"] = str(perl_lib)
    for mode in ("existing", "create"):
        completed = subprocess.run([perl, str(harness), mode], env=env, capture_output=True,
                                   text=True, timeout=10, check=False)
        assert completed.returncode == 0, completed.stderr
    manager.destroy(result["id"])


def test_managed_character_page_gate_selects_once_after_full_sync(tmp_path: Path) -> None:
    perl = shutil.which("perl")
    if not perl:
        pytest.skip("Perl is required to execute OpenKore's managed selection gate regression")
    from korelab.adapters.openkore import CHARACTER_LIST_SYNC_GATE

    plugin = tmp_path / "KoreLabCharacterListSync.pl"
    plugin.write_text(CHARACTER_LIST_SYNC_GATE.replace("__KORELAB_CHARACTER_SLOT__", "0"),
                      encoding="utf-8")
    perl_lib = tmp_path / "perl-lib"
    perl_lib.mkdir()
    (perl_lib / "Globals.pm").write_text(r"""
package Globals;
use strict;
our @chars; our %charSvrSet; our $messageSender; our $net;
sub import { my ($class,@names)=@_; my $caller=caller; no strict 'refs';
 for my $name (@names) { my $symbol=substr($name,1); *{"${caller}::$symbol"} =
   $name eq '@chars' ? \@chars : $name eq '%charSvrSet' ? \%charSvrSet :
   $name eq '$net' ? \$net : \$messageSender; }
}
1;
""", encoding="utf-8")
    (perl_lib / "Plugins.pm").write_text(r"""
package Plugins;
use strict;
our %hooks;
sub register { }
sub addHook { my ($name,$callback)=@_; $hooks{$name}=$callback; return 1; }
1;
""", encoding="utf-8")
    (perl_lib / "Network.pm").write_text("package Network; 1;\n", encoding="utf-8")
    harness = tmp_path / "exercise_gate.pl"
    harness.write_text(f"""
use strict; use warnings; use Globals; use Plugins;
{{ package FakeNet; sub getState {{ $main::network_state }} }}
{{ package FakeSender; our $count=0; our $slot=-1; sub sendCharLogin {{ $count++; $slot=$_[1]; }} }}
$main::network_state=1;
$Globals::net=bless {{}}, 'FakeNet'; $Globals::messageSender=bless {{}}, 'FakeSender';
require '{plugin}';
my $hook=$Plugins::hooks{{'charSelectScreen'}} or die "hook missing";
$Globals::chars[0]={{name=>'KoreLabTest'}};
my $pending={{}}; $hook->('charSelectScreen',$pending);
die "selection sent before current connection page sync" unless $pending->{{pin_return}}==2 && !$FakeSender::count;
$Globals::charSvrSet{{sync_Count}}=5;
$Globals::charSvrSet{{sync_received_characters}}=4;
my $partial={{}}; $hook->('charSelectScreen',$partial);
die "selection sent with incomplete pages" unless $partial->{{pin_return}}==2 && !$FakeSender::count;
$Globals::charSvrSet{{sync_received_characters}}=5;
my $ready={{}}; $hook->('charSelectScreen',$ready);
my $repeat={{}}; $hook->('charSelectScreen',$repeat);
die "selection must be handled by the one-shot gate" unless $ready->{{pin_return}}==1 && $repeat->{{pin_return}}==2;
die "exactly one configured slot must be selected" unless $FakeSender::count==1 && $FakeSender::slot==0;
$main::network_state=3; $Plugins::hooks{{'Network::stateChanged'}}->('Network::stateChanged',{{}});
$main::network_state=2; $Plugins::hooks{{'Network::stateChanged'}}->('Network::stateChanged',{{}});
$Globals::charSvrSet{{sync_received_characters}}=4;
my $new_session_pending={{}}; $hook->('charSelectScreen',$new_session_pending);
die "new session selection must still wait for complete character pages" unless $new_session_pending->{{pin_return}}==2 && $FakeSender::count==1;
$Globals::charSvrSet{{sync_received_characters}}=5;
my $new_session_ready={{}}; $hook->('charSelectScreen',$new_session_ready);
my $new_session_repeat={{}}; $hook->('charSelectScreen',$new_session_repeat);
die "relog must allow exactly one selection after the new session sync" unless $new_session_ready->{{pin_return}}==1 && $new_session_repeat->{{pin_return}}==2 && $FakeSender::count==2;
""", encoding="utf-8")
    env = os.environ.copy()
    env["PERL5LIB"] = str(perl_lib)
    completed = subprocess.run([perl, str(harness)], env=env, capture_output=True,
                               text=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr


def test_health_inspection_does_not_trust_stale_running_marker(tmp_path: Path, sources,
                                                               monkeypatch) -> None:
    openkore, rathena, xstools = sources
    manager = ManagedLaboratory(tmp_path / "private-data")
    result = manager.create(openkore=openkore, rathena_runtime=rathena,
                            xstools_overlay=xstools)
    home = Path(result["path"])
    owner_path = home / "owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner.update({"state": "running", "services": {name: {"pid": 999999}
        for name in ("database", "login", "char", "map")}})
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    monkeypatch.setattr(manager, "_process_matches", lambda *_: False)

    state = manager.inspect(result["id"])

    assert state["state"] == "running"
    assert state["healthy"] is False
    assert state["health"] == {**{name: False for name in ("database", "login", "char", "map")},
                                "gameplay_registration": False}
    manager.destroy(result["id"])


def test_gameplay_readiness_requires_registered_nonempty_maps(tmp_path: Path) -> None:
    from korelab.laboratory import ManagedLaboratory

    home = tmp_path / "lab"
    logs = home / "log"
    logs.mkdir(parents=True)
    (logs / "login.stdout.log").write_text(
        "The login-server is ready (Server is listening on the port 6900).\n", encoding="utf-8")
    (logs / "char.stdout.log").write_text(
        "The char-server is ready (Server is listening on the port 6121).\n"
        "Connected to login-server (connection #4).\n"
        "Map-Server 0 connected: 0 maps, from IP 127.0.0.1 port 5121.\n"
        "Map-server 0 loading complete.\n", encoding="utf-8")
    (logs / "map.stdout.log").write_text(
        "Map-server connected to char-server 'local'.\n", encoding="utf-8")

    state = ManagedLaboratory._gameplay_readiness(home)

    assert state == {"login_ready": True, "char_ready": True,
                     "char_login_registered": True, "map_registered": False,
                     "registered_map_count": 0, "map_login_registered": True}


def test_gameplay_readiness_requires_map_registration_after_load(tmp_path: Path) -> None:
    from korelab.laboratory import ManagedLaboratory

    home = tmp_path / "lab"
    logs = home / "log"
    logs.mkdir(parents=True)
    (logs / "login.stdout.log").write_text(
        "The login-server is ready (Server is listening on the port 6900).\n", encoding="utf-8")
    (logs / "char.stdout.log").write_text(
        "The char-server is ready (Server is listening on the port 6121).\n"
        "Connected to login-server (connection #4).\n"
        "Map-Server 0 connected: 42 maps, from IP 127.0.0.1 port 5121.\n"
        "Map-server 0 loading complete.\n", encoding="utf-8")
    (logs / "map.stdout.log").write_text(
        "Map-server connected to char-server 'local'.\n", encoding="utf-8")

    state = ManagedLaboratory._gameplay_readiness(home)

    assert state["map_registered"] is True
    assert state["registered_map_count"] == 42
