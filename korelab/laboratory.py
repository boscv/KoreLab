"""Ownership-scoped managed rAthena laboratory preparation and lifecycle."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

from .paths import USER_DATA_DIR
from .platform_support import set_private_permissions


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _source_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not (path / "conf").is_dir() or not (path / "db").is_dir() or not (path / "npc").is_dir():
        raise ValueError(f"rAthena runtime must contain conf/, db/, and npc/: {path}")
    for name in ("login-server", "char-server", "map-server"):
        if not (path / name).is_file():
            raise ValueError(f"rAthena runtime is missing {name}: {path}")
    return path


def _set_config(path: Path, values: dict[str, str]) -> None:
    import re
    text = path.read_text(encoding="utf-8", errors="replace")
    for key, value in values.items():
        pattern = re.compile(rf"(?m)^\s*{re.escape(key)}\s*:.*$")
        replacement = f"{key}: {value}"
        if pattern.search(text):
            text = pattern.sub(replacement, text, count=1)
        else:
            text += f"\n{replacement}\n"
    path.write_text(text, encoding="utf-8")
    set_private_permissions(path, 0o600)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    set_private_permissions(temporary, 0o600)
    temporary.replace(path)


class ManagedLaboratory:
    """Create and own an isolated, disposable rAthena runtime directory.

    Source trees are read-only inputs. Runtime configs, database files, process
    records, and generated credentials are confined to the laboratory root.
    """

    VERSION = 1
    OPENKORE_PACKET_TYPE = "kRO_RagexeRE_2021_11_03"

    def __init__(self, root: Path | None = None):
        self.root = (root or (USER_DATA_DIR / "laboratories")).expanduser().resolve()

    def discover(self, openkore: Path | None = None, rathena: Path | None = None) -> dict:
        openkore = openkore.expanduser().resolve() if openkore else None
        rathena = rathena.expanduser().resolve() if rathena else None
        db_server = shutil.which("mariadbd") or shutil.which("mysqld")
        db_install = shutil.which("mariadb-install-db") or shutil.which("mysql_install_db")
        return {
            "os": os.name,
            "openkore": {"path": str(openkore), "present": bool(openkore and (openkore / "openkore.pl").is_file())},
            "rathena": {"path": str(rathena), "present": bool(rathena and all((rathena / n).is_file() for n in ("login-server", "char-server", "map-server")))},
            "mysql_protocol_server": {"server": db_server, "initializer": db_install,
                                      "available": bool(db_server and db_install)},
            "managed_root": str(self.root),
        }

    def plan(self, *, openkore: Path, rathena_runtime: Path,
             name: str = "korelab", xstools_overlay: Path | None = None,
             start_point: dict | None = None) -> dict:
        source = _source_root(rathena_runtime)
        client = openkore.expanduser().resolve()
        if not (client / "openkore.pl").is_file() or not (client / "tables/servers.txt").is_file():
            raise ValueError("OpenKore checkout must contain openkore.pl and tables/servers.txt")
        db = self.discover(client, source)["mysql_protocol_server"]
        if not db["available"]:
            raise RuntimeError("managed rAthena needs mariadbd and mariadb-install-db; install them via `korelab setup`")
        if not shutil.which("mariadb") and not shutil.which("mysql"):
            raise RuntimeError("managed rAthena schema setup needs the mariadb or mysql client")
        if xstools_overlay is None:
            try:
                from .runner import preflight_perl
                preflight_perl(client)
            except Exception as exc:
                raise RuntimeError("OpenKore XSTools is not ready; build a compatible bundle in a private directory "
                    "and pass --xstools-overlay: " + str(exc)) from exc
        else:
            extension = xstools_overlay.expanduser().resolve()
            if not (extension / ".perl-config").is_file() or not any(
                    (extension / name).is_file() for name in ("XSTools.bundle", "XSTools.so", "XSTools.dll")):
                raise ValueError("XSTools overlay must contain a compatible bundle and .perl-config marker")
        verified_start = None
        if start_point is not None:
            from .navigation import verify_walkable_cell
            if not isinstance(start_point, dict) or set(start_point) != {"map", "x", "y"}:
                raise ValueError("managed start_point must contain exactly map, x, and y")
            verified_start = verify_walkable_cell(map_name=start_point["map"],
                x=start_point["x"], y=start_point["y"], openkore_fields=client / "fields",
                rathena=source)
        return {"status": "READY", "name": name, "openkore": str(client),
                "rathena_runtime": str(source), "database_provider": Path(db["server"]).name,
                "fields_folder": str((client / "fields").resolve()) if (client / "fields").is_dir() else None,
                "start_point": verified_start,
                "xstools_overlay": str(xstools_overlay.expanduser().resolve()) if xstools_overlay else None,
                "actions": ["create private runtime directory", "initialize private database",
                            "import active rAthena schema", "create isolated DB user",
                            "write loopback-only server configurations",
                            "generate disposable first-login account credentials"]}

    def create(self, *, openkore: Path, rathena_runtime: Path,
               name: str = "korelab", xstools_overlay: Path | None = None,
               start_point: dict | None = None, agent_count: int = 1,
               combat_observer: bool = False) -> dict:
        if type(agent_count) is not int or not 1 <= agent_count <= 100:
            raise ValueError("managed fleet size must be between 1 and 100")
        if type(combat_observer) is not bool:
            raise ValueError("combat_observer must be a boolean")
        plan = self.plan(openkore=openkore, rathena_runtime=rathena_runtime, name=name,
                         xstools_overlay=xstools_overlay, start_point=start_point)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        set_private_permissions(self.root, 0o700)
        lab_id = f"{name}-{uuid.uuid4().hex[:10]}"
        home = self.root / lab_id
        home.mkdir(mode=0o700)
        set_private_permissions(home, 0o700)
        runtime = Path(plan["rathena_runtime"])
        client = Path(plan["openkore"])
        for directory in ("conf", "log"):
            target = home / directory
            if directory == "conf":
                shutil.copytree(runtime / directory, target)
            else:
                target.mkdir(mode=0o700)
        # All managed clients connect from loopback. rAthena's default packet
        # config treats more than five connections from one address in three
        # seconds as DDoS; explicitly trust only the disposable lab's loopback
        # clients while retaining the default protection for other addresses.
        packet_config = home / "conf/packet_athena.conf"
        if not packet_config.is_file():
            raise RuntimeError("rAthena packet access configuration is missing")
        packet_text = packet_config.read_text(encoding="utf-8")
        if "allow: 127.0.0.1" not in {line.strip() for line in packet_text.splitlines()}:
            packet_config.write_text(packet_text.rstrip() +
                "\n\n// Managed KoreLab sessions connect only from loopback.\n"
                "allow: 127.0.0.1\n", encoding="utf-8")
        if combat_observer:
            # Keep all upstream scripts read-only, but shadow the private
            # custom-script index so the disposable map-server can load its
            # narrow server-authored combat evidence hook.
            private_npc = home / "npc"
            private_npc.mkdir(mode=0o700)
            fleet_fixture_overrides = []
            for entry in (runtime / "npc").iterdir():
                target = private_npc / entry.name
                if entry.name == "scripts_custom.conf":
                    shutil.copy2(entry, target)
                elif entry.name == "re":
                    target.mkdir(mode=0o700)
                    for re_entry in entry.iterdir():
                        re_target = target / re_entry.name
                        if re_entry.name == "scripts_monsters.conf":
                            text = re_entry.read_text(encoding="utf-8")
                            text, removed = re.subn(
                                r"(?m)^npc:\s*npc/re/mobs/championmobs\.txt\s*(?:\r?\n|$)",
                                "", text, count=1)
                            re_target.write_text(text, encoding="utf-8")
                            if removed:
                                fleet_fixture_overrides.append(
                                    "disabled unrelated rAthena champion-mob spawns in the private novice combat fixture")
                        elif re_entry.name == "mobs":
                            re_target.mkdir(mode=0o700)
                            for mob_entry in re_entry.iterdir():
                                mob_target = re_target / mob_entry.name
                                if mob_entry.name == "fields":
                                    mob_target.mkdir(mode=0o700)
                                    for field_entry in mob_entry.iterdir():
                                        field_target = mob_target / field_entry.name
                                        if field_entry.name == "prontera.txt":
                                            lines = field_entry.read_text(encoding="utf-8").splitlines(keepends=True)
                                            kept, removed = [], 0
                                            for line in lines:
                                                columns = line.rstrip("\r\n").split("\t")
                                                if (len(columns) >= 3
                                                        and columns[0].startswith("prt_fild08,")
                                                        and columns[1] == "monster"
                                                        and columns[2] not in {"Poring", "Little Poring"}):
                                                    removed += 1
                                                    continue
                                                kept.append(line)
                                            field_target.write_text("".join(kept), encoding="utf-8")
                                            if removed:
                                                fleet_fixture_overrides.append(
                                                    f"limited prt_fild08 to novice-safe Poring spawns in the private fixture ({removed} unrelated spawn rows removed)")
                                        else:
                                            field_target.symlink_to(field_entry,
                                                target_is_directory=field_entry.is_dir())
                                else:
                                    mob_target.symlink_to(mob_entry,
                                        target_is_directory=mob_entry.is_dir())
                        else:
                            re_target.symlink_to(re_entry, target_is_directory=re_entry.is_dir())
                else:
                    target.symlink_to(entry, target_is_directory=entry.is_dir())
            custom_scripts = private_npc / "scripts_custom.conf"
            with custom_scripts.open("a", encoding="utf-8") as stream:
                stream.write("\n// Private KoreLab fleet kill evidence.\n"
                             "npc: npc/korelab_fleet_observer.txt\n")
            fleet_spawns = ""
            if start_point and start_point["map"] == "prt_fild08":
                from .navigation import _openkore_field
                width, height, _cells, _path = _openkore_field(
                    Path(plan["fields_folder"]), start_point["map"])
                x1, x2 = max(1, start_point["x"] - 25), min(width - 2, start_point["x"] + 25)
                y1, y2 = max(1, start_point["y"] - 25), min(height - 2, start_point["y"] + 25)
                fleet_spawns = (f'OnInit:\n\tareamonster "{start_point["map"]}",'
                    f'{x1},{y1},{x2},{y2},"Poring",1002,150;\n\tend;\n')
            observer_script = private_npc / "korelab_fleet_observer.txt"
            observer_script.write_text(
                "-\tscript\tKoreLabFleetCombatObserver\t-1,{\n"
                + fleet_spawns +
                "OnNPCKillEvent:\n"
                # This rAthena build defines getcharid(0) as the character ID.
                "\t.@char_id = getcharid(0);\n"
                "\tif (.@char_id > 0) query_sql \"INSERT INTO `korelab_combat_events` "
                "(`occurred_at`,`char_id`,`mob_id`) VALUES (NOW(),\" + .@char_id + \",\" + killedrid + \")\";\n"
                "\tend;\n}\n", encoding="utf-8")
            set_private_permissions(custom_scripts, 0o600)
            set_private_permissions(observer_script, 0o600)
        else:
            (home / "npc").symlink_to(runtime / "npc", target_is_directory=True)
        managed_db = home / "db"
        managed_db.mkdir(mode=0o700)
        # rAthena's current db_path buffer is only 12 bytes, so retain its
        # native relative path and make a private overlay for writable imports.
        for item in (runtime / "db").iterdir():
            if item.name in {"import", "import-tmpl"}:
                continue
            (managed_db / item.name).symlink_to(item, target_is_directory=item.is_dir())
        # Keep the runtime's effective local import files private to this lab.
        # A built rAthena runtime commonly has db/import already populated from
        # db/import-tmpl; copying the template over a symlink would fail and
        # would also discard any intentional local database overrides.
        import_source = runtime / "db/import"
        if not import_source.is_dir():
            import_source = runtime / "db/import-tmpl"
        shutil.copytree(import_source, managed_db / "import")
        for executable in ("login-server", "char-server", "map-server"):
            shutil.copy2(runtime / executable, home / executable)
        (home / "database").mkdir(mode=0o700)
        ports = {key: _free_port() for key in ("db", "login", "char", "map")}
        # Avoid assigning the same ephemeral port to two listeners.
        while len(set(ports.values())) != len(ports):
            ports = {key: _free_port() for key in ports}
        db_user = "kl_" + secrets.token_hex(5)
        account = "kl" + secrets.token_hex(5)
        password = secrets.token_urlsafe(24)
        pin = ""
        while not pin:
            candidate = "".join(secrets.choice("0123456789") for _ in range(4))
            digits = [int(value) for value in candidate]
            if len(set(digits)) == 4 and len(set(digits[i + 1] - digits[i] for i in range(3))) > 1:
                pin = candidate
        db_password = secrets.token_urlsafe(28)
        _set_config(home / "conf/login_athena.conf", {
            "bind_ip": "127.0.0.1", "login_port": str(ports["login"]),
            "new_account": "yes", "allowed_regs": "50", "time_allowed": "1"})
        _set_config(home / "conf/char_athena.conf", {
            "bind_ip": "127.0.0.1", "login_ip": "127.0.0.1",
            "login_port": str(ports["login"]), "char_ip": "127.0.0.1",
            "char_port": str(ports["char"]), "char_new": "yes",
            "pincode_enabled": "no", "pincode_force": "no"})
        if start_point is not None:
            _set_config(home / "conf/char_athena.conf", {"start_point":
                f"{start_point['map']},{start_point['x']},{start_point['y']}"})
        _set_config(home / "conf/map_athena.conf", {
            "bind_ip": "127.0.0.1", "char_ip": "127.0.0.1",
            "char_port": str(ports["char"]), "map_ip": "127.0.0.1",
            "map_port": str(ports["map"]), "db_path": "db"})
        db_values = {"login_server_ip": "127.0.0.1", "login_server_port": str(ports["db"]),
                     "login_server_id": db_user, "login_server_pw": db_password,
                     "login_server_db": "ragnarok", "ipban_db_ip": "127.0.0.1",
                     "ipban_db_port": str(ports["db"]), "ipban_db_id": db_user,
                     "ipban_db_pw": db_password, "ipban_db_db": "ragnarok"}
        for prefix in ("char_server", "map_server", "web_server", "log_db"):
            db_values.update({f"{prefix}_ip": "127.0.0.1", f"{prefix}_port": str(ports["db"]),
                              f"{prefix}_id": db_user, f"{prefix}_pw": db_password,
                              f"{prefix}_db": "ragnarok"})
        _set_config(home / "conf/inter_athena.conf", db_values)
        # rAthena loads conf/import/inter_conf.txt after inter_athena.conf.
        # Leaving its stock log_db_db=log (or credentials) overrides the
        # private managed values and makes services reach outside this schema.
        imported_inter = home / "conf/import/inter_conf.txt"
        if imported_inter.is_file():
            _set_config(imported_inter, db_values)
        # rAthena's fixed server-link password field is NAME_LENGTH-sized.
        interserver_password = secrets.token_urlsafe(12)
        _set_config(home / "conf/char_athena.conf", {"userid": "s1", "passwd": interserver_password})
        _set_config(home / "conf/map_athena.conf", {"userid": "s1", "passwd": interserver_password})
        owner = {"format": self.VERSION, "id": lab_id, "created": time.time(),
                 "openkore_source": str(client), "rathena_runtime_source": str(runtime),
                 "rathena_runtime_sha256": {n: hashlib.sha256((runtime / n).read_bytes()).hexdigest()
                    for n in ("login-server", "char-server", "map-server")},
                 "home": str(home), "ports": ports,
                 "services": {}, "state": "prepared"}
        if combat_observer:
            owner["fleet_fixture_overrides"] = fleet_fixture_overrides
        if plan["start_point"]:
            owner["start_point"] = plan["start_point"]
        _atomic_json(home / "owner.json", owner)
        # Credentials are private, never included in return values or reports.
        _atomic_json(home / "secrets.json", {"account": account, "password": password, "login_pin": pin,
                    "database_user": db_user, "database_password": db_password,
                    "interserver_password": interserver_password})
        profile = home / "openkore"
        profile.mkdir(mode=0o700)
        character = "KL" + secrets.token_hex(4).upper()
        profile_name = "KoreLab_" + lab_id.split("-")[-1]
        overlay_extension = None
        if plan["xstools_overlay"]:
            source_extension = Path(plan["xstools_overlay"])
            extension = source_extension
            if extension.name == "XSTools":
                pass
            elif (extension / "auto/XSTools").is_dir():
                extension = extension / "auto/XSTools"
            else:
                raise ValueError("XSTools overlay path must name the XSTools bundle directory or its src root")
            target_extension = profile / "xstools/src/auto/XSTools"
            target_extension.mkdir(parents=True, mode=0o700)
            for filename in (".perl-config", "XSTools.bundle", "XSTools.so", "XSTools.dll"):
                if (extension / filename).is_file():
                    shutil.copy2(extension / filename, target_extension / filename)
            overlay_extension = target_extension
        profile_config = profile / "config.txt"
        profile_config.write_text(f"master {profile_name}\nserver 0\nusername {account}_M\n"
            f"password {password}\nloginPinCode {pin}\nchar 0\nai off\nportalCompile 1\n", encoding="utf-8")
        set_private_permissions(profile_config, 0o600)
        plugin = profile / "KoreLabCreateCharacter.pl"
        plugin.write_text(
            "package KoreLabCreateCharacter;\nuse strict; use warnings;\n"
            "use Globals qw(@chars %config %charSvrSet $net $messageSender); use Misc; use Plugins; use Network;\n"
            f"my $wanted = '{character}'; my $slot = 0; my $sent = 0; my $selected = 0; my $last_state = 1;\n"
            "Plugins::register('KoreLabCreateCharacter', 'Create one disposable novice if absent', \\&unload);\n"
            "my $state_hook = Plugins::addHook('Network::stateChanged', sub {\n"
            "  my $state = $net->getState();\n"
            "  if ($last_state == 3 && $state == 2 && $config{username} =~ /_M$/) {\n"
            "    configModify('username', substr($config{username}, 0, -2), 1);\n"
            "  }\n"
            "  $last_state = $state;\n"
            "});\n"
            "my $char_hook = Plugins::addHook('charSelectScreen', sub {\n"
            "  my ($hook_name, $args) = @_;\n"
            "  unless (defined $charSvrSet{sync_Count} && defined $charSvrSet{sync_received_characters} "
            "&& $charSvrSet{sync_received_characters} >= $charSvrSet{sync_Count}) {\n"
            "    $args->{pin_return} = 2; return;\n"
            "  }\n"
            "  if (grep { $_ && ($_->{name} // '') eq $wanted } @chars) {\n"
            "    unless ($selected) { $selected = 1; $messageSender->sendCharLogin($slot); }\n"
            "    configModify('char', $slot, 1);\n"
            "    $args->{pin_return} = 1;\n"
            "    return;\n"
            "  }\n"
            "  return if $sent;\n"
            "  $sent = 1;\n"
            "  if (Misc::createCharacter($slot, $wanted)) { $args->{pin_return} = 2; }\n"
            "});\n"
            "sub unload { Plugins::delHook($state_hook) if $state_hook; Plugins::delHook($char_hook) if $char_hook; }\n1;\n", encoding="utf-8")
        set_private_permissions(plugin, 0o600)
        _atomic_json(profile / "profile.json", {"openkore": str(client), "server_host": "127.0.0.1",
                    "server_port": ports["login"], "account": account + "_M",
                    "character": character, "character_slot": 0, "server_profile": profile_name,
                    "profile_config": str(profile_config), "create_character_plugin": str(plugin),
                    "agent_id": "agent-001",
                    "xstools_overlay": str(overlay_extension) if overlay_extension else None,
                    "fields_folder": plan["fields_folder"],
                    "server_type": self.OPENKORE_PACKET_TYPE,
                    "packet_profile_required": True})
        agents = [{"id": "agent-001", "account": account, "character": character,
                   "profile_file": "openkore/profile.json", "state": "pending"}]
        seen_accounts, seen_characters = {account}, {character}
        fleet_secrets = {"agent-001": {"account": account, "password": password,
                                        "login_pin": pin}}
        for index in range(2, agent_count + 1):
            agent_id = f"agent-{index:03d}"
            agent_account = ""
            while not agent_account or agent_account in seen_accounts:
                agent_account = "kl" + secrets.token_hex(5)
            seen_accounts.add(agent_account)
            agent_password = secrets.token_urlsafe(24)
            agent_pin = ""
            while not agent_pin:
                candidate = "".join(secrets.choice("0123456789") for _ in range(4))
                digits = [int(value) for value in candidate]
                if len(set(digits)) == 4 and len(set(digits[i + 1] - digits[i] for i in range(3))) > 1:
                    agent_pin = candidate
            agent_character = ""
            while not agent_character or agent_character in seen_characters:
                agent_character = "KL" + secrets.token_hex(4).upper()
            seen_characters.add(agent_character)
            agent_dir = home / "openkore-agents" / agent_id
            agent_dir.mkdir(parents=True, mode=0o700)
            agent_config = agent_dir / "config.txt"
            config_text = profile_config.read_text(encoding="utf-8")
            for key, value in (("username", agent_account + "_M"),
                               ("password", agent_password), ("loginPinCode", agent_pin)):
                config_text, changed = re.subn(rf"(?m)^{re.escape(key)}\s+.*$",
                    f"{key} {value}", config_text, count=1)
                if changed != 1:
                    raise RuntimeError(f"managed OpenKore template is missing {key}")
            agent_config.write_text(config_text, encoding="utf-8")
            set_private_permissions(agent_config, 0o600)
            agent_plugin = agent_dir / "KoreLabCreateCharacter.pl"
            plugin_text = plugin.read_text(encoding="utf-8")
            plugin_text, changed = re.subn(r"my \$wanted = '[A-Z0-9]+';",
                "my $wanted = '" + agent_character + "';", plugin_text, count=1)
            if changed != 1:
                raise RuntimeError("managed native character-creation plugin template is invalid")
            agent_plugin.write_text(plugin_text, encoding="utf-8")
            set_private_permissions(agent_plugin, 0o600)
            agent_profile_path = agent_dir / "profile.json"
            agent_profile = json.loads((profile / "profile.json").read_text(encoding="utf-8"))
            agent_profile.update({"account": agent_account, "character": agent_character,
                "profile_config": str(agent_config), "create_character_plugin": str(agent_plugin),
                "agent_id": agent_id})
            _atomic_json(agent_profile_path, agent_profile)
            agents.append({"id": agent_id, "account": agent_account,
                           "character": agent_character,
                           "profile_file": str(agent_profile_path.relative_to(home)),
                           "state": "pending"})
            fleet_secrets[agent_id] = {"account": agent_account, "password": agent_password,
                                       "login_pin": agent_pin}
        if agent_count > 1:
            secret_path = home / "secrets.json"
            secret_values = json.loads(secret_path.read_text(encoding="utf-8"))
            secret_values["fleet_agents"] = fleet_secrets
            _atomic_json(secret_path, secret_values)
        owner.update({"character": character, "server_profile": profile_name,
                      "agents": agents, "combat_observer": combat_observer})
        _atomic_json(home / "owner.json", owner)
        return {"status": "PREPARED", "id": lab_id, "path": str(home),
                "character": character,
                "agent_count": agent_count,
                "ports": ports, "account_created_on_first_native_login": True,
                "character_provisioning": "native OpenKore creation plugin is staged; authenticate with `lab bootstrap`"}

    def start(self, lab_id: str, *, timeout: float = 45) -> dict:
        home = self._owned(lab_id)
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        if owner["state"] == "running":
            current = self.inspect(lab_id)
            if current["healthy"]:
                return current
            # A stale running marker must not make a failed service appear
            # reusable. Stop only processes still matching this lab's owner
            # records, then allow a bounded restart against the existing DB.
            stopped = self.stop(lab_id)
            if stopped["unconfirmed"]:
                raise RuntimeError("managed lab is unhealthy and owned process cleanup is unconfirmed")
            owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        source = Path(owner["rathena_runtime_source"])
        sql_files = [source / "sql-files/main.sql", source / "sql-files/logs.sql"]
        if not all(file.is_file() for file in sql_files):
            raise RuntimeError("selected rAthena runtime must include sql-files/main.sql and logs.sql; use its source root as runtime")
        installer = shutil.which("mariadb-install-db") or shutil.which("mysql_install_db")
        server = shutil.which("mariadbd") or shutil.which("mysqld")
        client = shutil.which("mariadb") or shutil.which("mysql")
        if not all((installer, server, client)):
            raise RuntimeError("managed database prerequisites changed after planning; rerun `korelab doctor`")
        data = home / "database/data"
        if not (data / "mysql").is_dir():
            result = subprocess.run([installer, "--no-defaults", f"--datadir={data}",
                "--auth-root-authentication-method=normal", "--skip-test-db"],
                capture_output=True, text=True, timeout=120, check=False)
            if result.returncode:
                raise RuntimeError("private MariaDB initialization failed: " + _safe_tail(result.stderr))
        db_socket = home / "database/mysql.sock"
        db_log = home / "log/mariadb.log"
        db_args = [server, "--no-defaults", f"--datadir={data}", "--bind-address=127.0.0.1",
                   f"--port={owner['ports']['db']}", f"--socket={db_socket}",
                   f"--pid-file={home / 'database/mariadb.pid'}", f"--log-error={db_log}",
                   "--skip-name-resolve", "--skip-log-error"]
        service_records: dict[str, dict] = {}
        owner["state"] = "starting"
        owner["services"] = service_records
        _atomic_json(home / "owner.json", owner)
        try:
            service_records["database"] = self._launch(home, "database", db_args)
            _atomic_json(home / "owner.json", {**owner, "services": service_records})
            self._wait_port("127.0.0.1", owner["ports"]["db"], timeout, home, service_records["database"])
            credentials = json.loads((home / "secrets.json").read_text(encoding="utf-8"))
            auth = [client, "--no-defaults", f"--socket={db_socket}", "-uroot", "--batch", "--skip-column-names"]
            self._sql(auth, "CREATE DATABASE IF NOT EXISTS ragnarok CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            schema_check = subprocess.run([*auth, "--database=ragnarok", "--execute=SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='ragnarok' AND table_name IN ('login','char','inventory','skill','storage','quest','memo','loginlog')"],
                capture_output=True, text=True, timeout=15, check=False)
            if schema_check.returncode:
                raise RuntimeError("could not verify the managed rAthena schema before import")
            if schema_check.stdout.strip() != "8":
                if schema_check.stdout.strip() != "0":
                    raise RuntimeError("managed rAthena schema is incomplete; refusing to repeat a partial import; destroy and recreate this owned disposable laboratory")
                for sql_source in sql_files:
                    schema = sql_source.read_text(encoding="utf-8", errors="replace")
                    # A private schema uses InnoDB so KoreLab's guarded restoration
                    # can rely on atomic updates. Upstream source files are untouched.
                    schema = schema.replace("ENGINE=MyISAM", "ENGINE=InnoDB").replace("ENGINE = MyISAM", "ENGINE=InnoDB")
                    if sql_source.name == "main.sql":
                        import re
                        schema, changed = re.subn(
                            r"('1',\s*'s1',\s*)'p1'(\s*,\s*'S')",
                            lambda match: match.group(1) + "'" + credentials["interserver_password"] + "'" + match.group(2),
                            schema, count=1)
                        if changed != 1:
                            raise RuntimeError("active rAthena schema lacks its expected server-account fixture; refusing to start with unknown credentials")
                    imported = subprocess.run([*auth, "ragnarok"], input=schema, text=True,
                        capture_output=True, timeout=240, check=False)
                    if imported.returncode:
                        raise RuntimeError(f"isolated rAthena schema import failed ({sql_source.name}): " + _safe_tail(imported.stderr))
            user, secret = credentials["database_user"], credentials["database_password"]
            sql_secret = secret.replace("\\", "\\\\").replace("'", "\\'")
            self._sql(auth, f"CREATE USER IF NOT EXISTS '{user}'@'127.0.0.1' IDENTIFIED BY '{sql_secret}'; "
                f"GRANT ALL PRIVILEGES ON ragnarok.* TO '{user}'@'127.0.0.1'; FLUSH PRIVILEGES;")
            if owner.get("combat_observer"):
                self._sql(auth, "CREATE TABLE IF NOT EXISTS ragnarok.korelab_combat_events ("
                    "event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                    "occurred_at DATETIME NOT NULL, char_id INT UNSIGNED NOT NULL,"
                    "mob_id INT UNSIGNED NOT NULL, PRIMARY KEY(event_id),"
                    "INDEX char_event(char_id,occurred_at)) ENGINE=InnoDB")
            for config_path, flag in (("login_athena.conf", "--login-config"),
                                      ("char_athena.conf", "--char-config"),
                                      ("map_athena.conf", "--map-config")):
                executable = home / config_path.replace("_athena.conf", "-server")
                args = [str(executable), flag, str(home / "conf" / config_path)]
                if config_path != "login_athena.conf":
                    args += ["--inter-config", str(home / "conf/inter_athena.conf")]
                if config_path == "login_athena.conf":
                    args += ["--lan-config", str(home / "conf/lan_athena.conf")]
                service = config_path.split("_", 1)[0]
                service_records[service] = self._launch(home, service, args)
                _atomic_json(home / "owner.json", {**owner, "services": service_records})
                port_key = {"login": "login", "char": "char", "map": "map"}[service]
                self._wait_port("127.0.0.1", owner["ports"][port_key], timeout, home,
                                service_records[service])
            owner["services"] = service_records
            self._wait_gameplay_ready(home, timeout, service_records)
            owner["state"] = "running"
            _atomic_json(home / "owner.json", owner)
            return self.inspect(lab_id)
        except BaseException:
            owner["services"] = service_records
            _atomic_json(home / "owner.json", owner)
            self.stop(lab_id)
            raise

    def stop(self, lab_id: str) -> dict:
        home = self._owned(lab_id)
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        stopped, unresolved = [], []
        for name, record in reversed(list(owner.get("services", {}).items())):
            if not self._process_matches(home, record):
                if self._pid_alive(int(record.get("pid", -1))):
                    unresolved.append(name)
                continue
            try:
                os.kill(int(record["pid"]), signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and self._process_matches(home, record):
                    time.sleep(.1)
                if self._process_matches(home, record):
                    os.kill(int(record["pid"]), signal.SIGKILL)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and self._process_matches(home, record):
                        time.sleep(.1)
                if self._process_matches(home, record):
                    unresolved.append(name)
                else:
                    stopped.append(name)
            except (OSError, KeyError, ValueError):
                unresolved.append(name)
        owner["state"] = "stopped" if not unresolved else "cleanup_unconfirmed"
        owner["services"] = {name: record for name, record in owner.get("services", {}).items()
                              if name in unresolved}
        _atomic_json(home / "owner.json", owner)
        return {"id": lab_id, "stopped": stopped, "unconfirmed": unresolved,
                "state": owner["state"]}

    def bootstrap(self, lab_id: str, *, agent_id: str = "agent-001",
                  timeout: float = 120) -> dict:
        """Perform first native OpenKore login, creating its account and novice.

        rAthena's configured first-login `_M` registration and OpenKore's native
        character-creation request are used; KoreLab does not insert identity
        rows directly into SQL.
        """
        home = self._owned(lab_id)
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        if owner["state"] != "running":
            raise RuntimeError("start the managed laboratory before bootstrapping its native OpenKore identity")
        agents = owner.get("agents") or [{"id": "agent-001", "account": owner.get("character", ""),
                                           "character": owner.get("character", ""),
                                           "profile_file": "openkore/profile.json"}]
        agent = next((entry for entry in agents if entry.get("id") == agent_id), None)
        if agent is None:
            raise ValueError("requested agent is not part of this managed laboratory")
        profile_path = (home / agent.get("profile_file", "openkore/profile.json")).resolve()
        if not profile_path.is_relative_to(home.resolve()):
            raise RuntimeError("managed agent profile path escapes its owned laboratory")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        secrets_file = home / "secrets.json"
        if secrets_file.stat().st_mode & 0o077:
            raise RuntimeError("managed credential file permissions are too broad")
        credentials_all = json.loads(secrets_file.read_text(encoding="utf-8"))
        credentials = credentials_all.get("fleet_agents", {}).get(agent_id)
        if credentials is None and agent_id == "agent-001":
            credentials = credentials_all
        if not credentials:
            raise RuntimeError("managed agent credentials are unavailable")
        account_check = self._query_owned_db(home,
            f"SELECT COUNT(*) FROM login WHERE userid='{credentials['account']}'")
        if account_check.returncode:
            raise RuntimeError("could not verify whether the managed account already exists")
        account_preexisting = account_check.stdout.strip() == "1"
        if not account_preexisting:
            self._wait_for_account_registration_capacity(home, owner)
        xstools = Path(profile["xstools_overlay"]) if profile.get("xstools_overlay") else None
        if xstools is None:
            raise RuntimeError("managed OpenKore profile has no verified XSTools bundle")
        from .runner import preflight_perl
        from .adapters.openkore import OpenKoreAdapter
        perl = preflight_perl(Path(profile["openkore"]), xstools)
        run_dir = home / "runs" / ("bootstrap-" + uuid.uuid4().hex[:10])
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_name = profile["server_profile"]
        username = credentials["account"] if account_preexisting else credentials["account"] + "_M"
        runtime_config = run_dir / "config.txt"
        config_text = Path(profile["profile_config"]).read_text(encoding="utf-8")
        import re
        config_text, replacements = re.subn(r"(?m)^username\s+\S+\s*$", "username " + username, config_text, count=1)
        if replacements != 1:
            raise RuntimeError("managed OpenKore profile lacks exactly one username setting")
        runtime_config.write_text(config_text, encoding="utf-8")
        set_private_permissions(runtime_config, 0o600)
        tables = Path(profile["openkore"]) / "tables"
        table_folders = ["kRO/Ragexe_2021_11_03", "iRO"]
        if not all((tables / folder).is_dir() for folder in table_folders):
            raise RuntimeError("OpenKore checkout lacks the selected packet table folders")
        private_server = {"name": profile_name, "host": "127.0.0.1",
            "port": int(profile["server_port"]), "server_type": profile["server_type"],
            "table_folders": table_folders}
        if profile.get("fields_folder"):
            private_server["fields_folder"] = profile["fields_folder"]
        scenario = {"identity": {"master": profile_name, "username": username,
                    "character": profile["character"], "character_slot": 0},
            "openkore": {"profile_config_path": str(runtime_config),
                "profile_selection": {"profile": profile_name, "server_index": 0, "character_slot": 0},
                "server_profile": private_server,
                "xstools_overlay": str(xstools), "plugin_overlays": [
                    {"source": profile["create_character_plugin"]}], "ai_mode": "off"}}
        adapter = OpenKoreAdapter(Path(profile["openkore"]), scenario, run_dir,
                                  time.monotonic() + timeout, perl)
        try:
            adapter.start()
            adapter.state(lambda row: row.get("connected_in_game") is True, timeout=20)
        except BaseException:
            adapter.close()
            if not account_preexisting:
                self._record_new_account_registration(home, owner, credentials["account"])
            raise
        finally:
            if adapter.process and adapter.process.poll() is None:
                adapter.close()
        result = self._query_owned_db(home,
            "SELECT c.char_id,c.name,c.class,c.base_level,c.last_map,c.last_x,c.last_y,c.online "
            "FROM `char` c JOIN login l ON l.account_id=c.account_id "
            f"WHERE l.userid='{credentials['account']}' AND c.name='{profile['character']}'")
        if result.returncode or not result.stdout.strip():
            raise RuntimeError("server-side verification did not find the provisioned account and character")
        fields = result.stdout.strip().split("\t")
        if len(fields) != 8 or fields[1] != profile["character"] or fields[7] != "0":
            raise RuntimeError("server-side character identity or offline state did not match the requested fixture")
        if not account_preexisting:
            self._record_new_account_registration(home, owner, credentials["account"], confirmed=True)
        provisioned = {"agent_id": agent_id, "account_created_by": "rAthena-native-first-login",
            "character_created_by": "OpenKore-Misc.createCharacter", "char_id": int(fields[0]),
            "baseline": {"map": fields[4], "x": int(fields[5]), "y": int(fields[6]),
                         "class": int(fields[2]), "base_level": int(fields[3])}}
        agent.update({"char_id": int(fields[0]), "state": "provisioned", **provisioned})
        owner["agents"] = agents
        if agent_id == "agent-001":
            owner["provisioning"] = provisioned
        _atomic_json(home / "owner.json", owner)
        return {"status": "VERIFIED", "id": lab_id, "agent_id": agent_id,
                "account": credentials["account"], "character": fields[1],
                "char_id": int(fields[0]), "position": {"map": fields[4], "x": int(fields[5]),
                "y": int(fields[6])}, "class_id": int(fields[2]), "base_level": int(fields[3]),
                "account_authentication": "native rAthena first-login registration",
                "character_creation": "native OpenKore protocol request",
                "credentials": "stored privately; values omitted"}

    def prepare_fleet_character(self, lab_id: str, *, agent_id: str,
                                archetype: dict) -> dict:
        """Prepare one newly created, offline character in this disposable lab.

        Identity and offline checks guard each setup write. The fixtures use
        class-legal first-job skills and equipment validated against the
        installed rAthena database before the fleet starts.
        """
        home = self._owned(lab_id)
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        agent = next((item for item in owner.get("agents", [])
                      if item.get("id") == agent_id), None)
        if agent is None or not agent.get("char_id"):
            raise RuntimeError("fleet fixture requires a natively provisioned owned character")
        profile_path = (home / agent["profile_file"]).resolve()
        if not profile_path.is_relative_to(home.resolve()):
            raise RuntimeError("fleet profile escapes its owned laboratory")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        secret_file = home / "secrets.json"
        if secret_file.stat().st_mode & 0o077:
            raise RuntimeError("managed credential file permissions are too broad")
        secret_store = json.loads(secret_file.read_text(encoding="utf-8"))
        credentials = secret_store.get("fleet_agents", {}).get(agent_id, secret_store)
        account, character = credentials["account"], profile["character"]
        if not re.fullmatch(r"[A-Za-z0-9_]+", account + character):
            raise RuntimeError("managed fleet identity contains unsupported characters")

        row = self._query_owned_db(home,
            "SELECT c.char_id,c.account_id,c.online,c.class,c.base_level,c.job_level,c.zeny,"
            "c.status_point,c.str,c.agi,c.vit,c.`int`,c.dex,c.luk "
            "FROM `char` c JOIN login l ON l.account_id=c.account_id "
            f"WHERE l.userid='{account}' AND c.name='{character}'")
        if row.returncode or len(row.stdout.strip().split("\t")) != 14:
            raise RuntimeError("could not independently verify the newly created fleet character")
        values = list(map(int, row.stdout.strip().split("\t")))
        (char_id, account_id, online, old_class, old_base, old_job, old_zeny,
         old_status_points, old_str, old_agi, old_vit, old_int, old_dex, old_luk) = values
        if char_id != int(agent["char_id"]) or online != 0 or old_class != 0:
            raise RuntimeError("fleet character identity or offline novice baseline changed")
        job_class = int(archetype["class_id"])
        base_level, job_level = int(archetype["base_level"]), int(archetype["job_level"])
        attributes = archetype.get("attributes", {"str": old_str, "agi": old_agi,
            "vit": old_vit, "int": old_int, "dex": old_dex, "luk": old_luk})
        if set(attributes) != {"str", "agi", "vit", "int", "dex", "luk"}:
            raise ValueError("fleet character attributes must specify all six base stats")
        from .fleet_profiles import status_point_cost, uses_renewal_stat_cost
        renewal_stats = uses_renewal_stat_cost(Path(owner["rathena_runtime_source"]))
        spent_status_points = sum(status_point_cost(value, int(attributes[key]), renewal=renewal_stats)
            for key, value in (("str", old_str), ("agi", old_agi), ("vit", old_vit),
                               ("int", old_int), ("dex", old_dex), ("luk", old_luk)))
        if spent_status_points > old_status_points:
            raise ValueError("fleet attribute build exceeds the newly created character's available status points")
        remaining_status_points = old_status_points - spent_status_points
        updated = self._query_owned_db(home,
            f"UPDATE `char` SET `class`={job_class},base_level={base_level},job_level={job_level},"
            f"base_exp=0,job_exp=0,skill_point=0,status_point={remaining_status_points},"
            f"str={int(attributes['str'])},agi={int(attributes['agi'])},vit={int(attributes['vit'])},"
            f"`int`={int(attributes['int'])},dex={int(attributes['dex'])},luk={int(attributes['luk'])} "
            f"WHERE char_id={char_id} AND account_id={account_id} AND online=0 AND `class`=0 "
            f"AND base_level=1 AND job_level=1 AND status_point={old_status_points} "
            f"AND str={old_str} AND agi={old_agi} AND vit={old_vit} AND `int`={old_int} "
            f"AND dex={old_dex} AND luk={old_luk}; SELECT ROW_COUNT()")
        if updated.returncode or updated.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded first-job character setup did not affect one offline owned row")

        skill_id, skill_level = int(archetype["skill_id"]), int(archetype["skill_level"])
        skill = self._query_owned_db(home,
            # rAthena recalculates normal skills from progression on map entry.
            # PERM_GRANTED is its native flag for an explicit test fixture and
            # preserves this class/source-checked skill through that pass.
            f"INSERT INTO skill (char_id,id,lv,flag) SELECT c.char_id,{skill_id},{skill_level},3 "
            "FROM `char` c JOIN login l ON l.account_id=c.account_id "
            f"WHERE c.char_id={char_id} AND c.account_id={account_id} AND l.userid='{account}' "
            f"AND c.online=0 AND c.`class`={job_class} AND NOT EXISTS "
            f"(SELECT 1 FROM skill WHERE char_id={char_id} AND id={skill_id}); SELECT ROW_COUNT()")
        if skill.returncode or skill.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded native-class skill fixture could not be added")

        additional_skills = archetype.get("additional_skills", [])
        if not isinstance(additional_skills, list):
            raise ValueError("fleet additional_skills must be a list")
        verified_additional_skills = []
        for extra in additional_skills:
            if (not isinstance(extra, dict) or set(extra) != {"skill", "skill_id", "skill_level"}
                    or not re.fullmatch(r"[A-Z0-9_]+", str(extra.get("skill", "")))
                    or type(extra.get("skill_id")) is not int or extra["skill_id"] < 1
                    or type(extra.get("skill_level")) is not int or extra["skill_level"] < 1
                    or int(extra["skill_id"]) == skill_id
                    or any(int(row["skill_id"]) == int(extra["skill_id"])
                           for row in verified_additional_skills)):
                raise ValueError("fleet additional skill definitions must be unique and source-checked")
            extra_id, extra_level = int(extra["skill_id"]), int(extra["skill_level"])
            extra_skill = self._query_owned_db(home,
                f"INSERT INTO skill (char_id,id,lv,flag) SELECT c.char_id,{extra_id},{extra_level},3 "
                "FROM `char` c JOIN login l ON l.account_id=c.account_id "
                f"WHERE c.char_id={char_id} AND c.account_id={account_id} AND l.userid='{account}' "
                f"AND c.online=0 AND c.`class`={job_class} AND NOT EXISTS "
                f"(SELECT 1 FROM skill WHERE char_id={char_id} AND id={extra_id}); SELECT ROW_COUNT()")
            if extra_skill.returncode or extra_skill.stdout.strip().splitlines()[-1:] != ["1"]:
                raise RuntimeError("guarded additional native-skill fixture could not be added")
            extra_check = self._query_owned_db(home,
                f"SELECT s.lv,s.flag FROM skill s JOIN `char` c ON c.char_id=s.char_id "
                f"JOIN login l ON l.account_id=c.account_id WHERE s.char_id={char_id} "
                f"AND s.id={extra_id} AND c.account_id={account_id} AND l.userid='{account}' "
                f"AND c.online=0 AND c.`class`={job_class}")
            if (extra_check.returncode or extra_check.stdout.strip() != f"{extra_level}\t3"):
                raise RuntimeError("independent verification did not confirm the additional skill fixture")
            verified_additional_skills.append({"skill": extra["skill"], "skill_id": extra_id,
                                               "skill_level": extra_level})

        # The native novice starter knife is also equipped in the right hand.
        # Clear only competing right-hand equipment on this verified offline
        # character before equipping its class weapon; retain all inventory.
        competing = self._query_owned_db(home,
            f"SELECT i.id,i.equip FROM inventory i JOIN `char` c ON c.char_id=i.char_id "
            f"WHERE i.char_id={char_id} AND i.nameid<>{int(archetype['weapon_id'])} "
            f"AND (i.equip & 2)<>0 AND c.account_id={account_id} AND c.online=0 "
            f"AND c.`class`={job_class}")
        if competing.returncode:
            raise RuntimeError("could not inspect competing right-hand equipment")
        for line in competing.stdout.splitlines():
            if not line:
                continue
            item_id_row, old_equip = map(int, line.split("\t"))
            unequipped = self._query_owned_db(home,
                f"UPDATE inventory i JOIN `char` c ON c.char_id=i.char_id SET i.equip=0 "
                f"WHERE i.id={item_id_row} AND i.char_id={char_id} AND i.equip={old_equip} "
                f"AND (i.equip & 2)<>0 AND c.account_id={account_id} AND c.online=0 "
                f"AND c.`class`={job_class}; SELECT ROW_COUNT()")
            if unequipped.returncode or unequipped.stdout.strip().splitlines()[-1:] != ["1"]:
                raise RuntimeError("guarded competing-weapon unequip did not affect one owned row")

        equipment = [(int(archetype["weapon_id"]), 1, 2)]
        if archetype.get("ammo_id"):
            equipment.append((int(archetype["ammo_id"]), int(archetype["arrow_amount"]), 32768))
        for item_id, amount, equip in equipment:
            current_item = self._query_owned_db(home,
                f"SELECT i.id,i.amount,i.equip FROM inventory i JOIN `char` c ON c.char_id=i.char_id "
                f"WHERE i.char_id={char_id} AND i.nameid={item_id} AND c.account_id={account_id} "
                "AND c.online=0")
            if current_item.returncode:
                raise RuntimeError("could not inspect the owned character's existing equipment")
            item_rows = [line.split("\t") for line in current_item.stdout.splitlines() if line]
            if len(item_rows) > 1:
                raise RuntimeError("duplicate item rows prevent deterministic fleet equipment setup")
            if item_rows:
                item_row = list(map(int, item_rows[0]))
                item_id_row, old_amount, old_equip = item_row
                wanted_amount = max(old_amount, amount)
                if old_amount < amount or old_equip != equip:
                    equipped = self._query_owned_db(home,
                        f"UPDATE inventory i JOIN `char` c ON c.char_id=i.char_id "
                        f"SET i.amount={wanted_amount},i.equip={equip} WHERE i.id={item_id_row} "
                        f"AND i.char_id={char_id} AND i.nameid={item_id} AND i.amount={old_amount} "
                        f"AND i.equip={old_equip} AND c.account_id={account_id} AND c.online=0 "
                        "AND c.`class`=" + str(job_class) + "; SELECT ROW_COUNT()")
                    if equipped.returncode or equipped.stdout.strip().splitlines()[-1:] != ["1"]:
                        raise RuntimeError("guarded starter-equipment update did not affect one owned row")
            else:
                inserted = self._query_owned_db(home,
                    "INSERT INTO inventory (char_id,nameid,amount,equip,identify) "
                    f"SELECT {char_id},{item_id},{amount},{equip},1 FROM `char` c "
                    f"WHERE c.char_id={char_id} AND c.account_id={account_id} AND c.online=0 "
                    f"AND c.`class`={job_class} AND NOT EXISTS (SELECT 1 FROM inventory "
                    f"WHERE char_id={char_id} AND nameid={item_id}); SELECT ROW_COUNT()")
                if inserted.returncode or inserted.stdout.strip().splitlines()[-1:] != ["1"]:
                    raise RuntimeError("guarded class equipment fixture could not be added")
            checked_item = self._query_owned_db(home,
                f"SELECT i.amount,i.equip FROM inventory i JOIN `char` c ON c.char_id=i.char_id "
                f"WHERE i.char_id={char_id} AND i.nameid={item_id} AND c.account_id={account_id} "
                "AND c.online=0")
            if checked_item.returncode or len(checked_item.stdout.strip().split("\t")) != 2:
                raise RuntimeError("independent equipment verification could not find one owned item")
            got_amount, got_equip = map(int, checked_item.stdout.strip().split("\t"))
            if got_amount < amount or got_equip != equip:
                raise RuntimeError("independent equipment verification did not confirm its equipped state")
        zeny = int(archetype.get("zeny", old_zeny))
        if zeny != old_zeny:
            funded = self._query_owned_db(home,
                f"UPDATE `char` SET zeny={zeny} WHERE char_id={char_id} AND account_id={account_id} "
                f"AND online=0 AND zeny={old_zeny}; SELECT ROW_COUNT()")
            if funded.returncode or funded.stdout.strip().splitlines()[-1:] != ["1"]:
                raise RuntimeError("guarded disposable combat-resource fixture failed")

        verified = self._query_owned_db(home,
            "SELECT c.`class`,c.base_level,c.job_level,c.online,c.zeny,c.status_point,"
            "c.str,c.agi,c.vit,c.`int`,c.dex,c.luk,"
            f"(SELECT lv FROM skill WHERE char_id={char_id} AND id={skill_id}),"
            f"(SELECT flag FROM skill WHERE char_id={char_id} AND id={skill_id}),"
            f"(SELECT COUNT(*) FROM inventory WHERE char_id={char_id} AND nameid={int(archetype['weapon_id'])}) "
            f"FROM `char` c WHERE c.char_id={char_id} AND c.account_id={account_id}")
        expected = [job_class, base_level, job_level, 0, zeny, remaining_status_points,
            int(attributes["str"]), int(attributes["agi"]), int(attributes["vit"]),
            int(attributes["int"]), int(attributes["dex"]), int(attributes["luk"]),
            skill_level, 3, 1]
        if verified.returncode or list(map(int, verified.stdout.strip().split("\t"))) != expected:
            raise RuntimeError("independent server verification did not confirm the fleet character fixture")
        for extra in verified_additional_skills:
            extra_check = self._query_owned_db(home,
                f"SELECT s.lv,s.flag FROM skill s JOIN `char` c ON c.char_id=s.char_id "
                f"JOIN login l ON l.account_id=c.account_id WHERE s.char_id={char_id} "
                f"AND s.id={extra['skill_id']} AND c.account_id={account_id} AND l.userid='{account}' "
                f"AND c.online=0 AND c.`class`={job_class}")
            if (extra_check.returncode or extra_check.stdout.strip() !=
                    f"{extra['skill_level']}\t3"):
                raise RuntimeError("final server verification did not confirm an additional skill fixture")
        agent["archetype"] = {key: value for key, value in archetype.items()}
        owner["agents"] = owner.get("agents", [])
        _atomic_json(home / "owner.json", owner)
        return {"agent_id": agent_id, "char_id": char_id, "account_id": account_id,
                "class_id": job_class, "base_level": base_level, "job_level": job_level,
                "attributes": {key: int(value) for key, value in attributes.items()},
                "status_points_remaining": remaining_status_points,
                "skill": archetype["skill"], "skill_id": skill_id, "skill_level": skill_level,
                "additional_skills": verified_additional_skills,
                "weapon_id": int(archetype["weapon_id"]), "ammo_id": archetype.get("ammo_id"),
                "lock_map": archetype["map"], "target_monsters": list(archetype["targets"]),
                "server_verified": True}

    @staticmethod
    def _account_registration_limits(home: Path) -> tuple[int, int]:
        values = {"allowed_regs": "1", "time_allowed": "10"}
        for path in (home / "conf/login_athena.conf", home / "conf/import/login_conf.txt"):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.split("//", 1)[0].strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = (part.strip() for part in line.split(":", 1))
                if key in values:
                    values[key] = value
        try:
            allowed, interval = int(values["allowed_regs"]), int(values["time_allowed"])
        except ValueError as exc:
            raise RuntimeError("private rAthena account-registration limits are invalid") from exc
        if allowed < 1 or interval < 1:
            raise RuntimeError("private rAthena account-registration limit disables native fleet provisioning")
        return allowed, interval

    def _wait_for_account_registration_capacity(self, home: Path, owner: dict) -> None:
        allowed, interval = self._account_registration_limits(home)
        now = time.time()
        history = [float(value) for value in owner.get("account_registration_times", [])
                   if isinstance(value, (int, float)) and 0 <= now - float(value) < interval]
        owner["account_registration_times"] = history
        if len(history) < allowed:
            return
        ready_at = sorted(history)[-allowed] + interval + 0.25
        delay = max(0.0, ready_at - now)
        if delay:
            time.sleep(delay)

    def _record_new_account_registration(self, home: Path, owner: dict, account: str,
                                         *, confirmed: bool = False) -> None:
        result = self._query_owned_db(home, f"SELECT COUNT(*) FROM login WHERE userid='{account}'")
        if result.returncode or result.stdout.strip() != "1":
            if confirmed:
                raise RuntimeError("native rAthena account registration was not independently confirmed")
            return
        times = list(owner.get("account_registration_times", []))
        times.append(time.time())
        owner["account_registration_times"] = times
        _atomic_json(home / "owner.json", owner)

    @staticmethod
    def _query_owned_db(home: Path, sql: str):
        credentials = json.loads((home / "secrets.json").read_text(encoding="utf-8"))
        cli = shutil.which("mariadb") or shutil.which("mysql")
        env = os.environ.copy()
        env.update({"MYSQL_PWD": credentials["database_password"]})
        return subprocess.run([cli, "--no-defaults", "--protocol=tcp", "-h", "127.0.0.1",
            "-P", str(json.loads((home / "owner.json").read_text())["ports"]["db"]),
            "-u", credentials["database_user"], "--batch", "--skip-column-names", "ragnarok", "-e", sql],
            capture_output=True, text=True, timeout=15, check=False, env=env)

    def destroy(self, lab_id: str) -> dict:
        home = self._owned(lab_id)
        stop_result = self.stop(lab_id)
        if stop_result["unconfirmed"]:
            raise RuntimeError("refusing to remove laboratory files while owned processes remain unconfirmed")
        # Resolve and remove only the self-owned directory. Symlinks to source
        # checkouts are unlinked, never followed.
        shutil.rmtree(home)
        return {"id": lab_id, "destroyed": True}

    def _launch(self, home: Path, name: str, args: list[str]) -> dict:
        log = (home / "log" / f"{name}.stdout.log").open("ab")
        process = subprocess.Popen(args, cwd=home, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
        log.close()
        record = {"pid": process.pid, "executable": str(Path(args[0]).resolve()),
                  "argv_sha256": hashlib.sha256(json.dumps(args).encode()).hexdigest(),
                  "cwd": str(home)}
        if not self._process_matches(home, record):
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            raise RuntimeError(f"started {name} process identity could not be verified")
        return record

    def _process_matches(self, home: Path, record: dict) -> bool:
        pid = int(record.get("pid", -1))
        if not self._pid_alive(pid):
            return False
        if os.name == "nt":
            return False
        try:
            cwd = Path(f"/proc/{pid}/cwd").resolve() if Path("/proc").is_dir() else None
            if cwd is None:
                result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=3, check=False)
                cwd = Path(next((line[1:] for line in result.stdout.splitlines()
                                 if line.startswith("n")), "")).resolve()
            cmdline_path = Path(f"/proc/{pid}/exe")
            executable = cmdline_path.resolve() if cmdline_path.exists() else None
            if executable is None:
                result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                    capture_output=True, text=True, timeout=3, check=False)
                executable = Path(next((line[1:] for line in result.stdout.splitlines()
                                        if line.startswith("n")), "")).resolve()
            if cwd is None or not cwd.is_relative_to(home.resolve()):
                return False
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=3, check=False).stdout
            return (executable == Path(record.get("executable", "")).resolve()
                    and str(home.resolve()) in command)
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _wait_port(self, host: str, port: int, timeout: float, home: Path,
                   process: dict | None = None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process is not None and not self._process_matches(home, process):
                raise RuntimeError("managed service exited or lost its process identity before opening its endpoint")
            try:
                with socket.create_connection((host, port), timeout=.25):
                    return
            except OSError:
                time.sleep(.15)
        raise TimeoutError(f"managed service did not open its expected loopback endpoint {host}:{port}")

    @staticmethod
    def _gameplay_readiness(home: Path) -> dict:
        """Read rAthena's inter-server registration acknowledgements.

        Listening endpoints are insufficient: character selection requires a
        char-server login link and a map-server that has completed sending a
        non-empty map list to the char-server.
        """
        def log(name: str) -> str:
            path = home / "log" / f"{name}.stdout.log"
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

        login_text, char_text, map_text = log("login"), log("char"), log("map")
        login_ready = "The login-server is ready (Server is listening on the port" in login_text
        char_ready = "The char-server is ready (Server is listening on the port" in char_text
        char_login_link = "Connected to login-server (connection #" in char_text
        registered = list(re.finditer(r"Map-Server (\d+) connected: (\d+) maps,", char_text))
        map_registration = False
        map_count = 0
        if registered:
            match = registered[-1]
            map_id, map_count = match.group(1), int(match.group(2))
            completed = char_text.find(f"Map-server {map_id} loading complete.", match.end())
            disconnected = char_text.find(f"Map-server #{map_id} has disconnected.", match.end())
            map_registration = map_count > 0 and completed >= 0 and (disconnected < 0 or completed > disconnected)
        map_login_link = "Map-server connected to char-server" in map_text
        return {"login_ready": login_ready, "char_ready": char_ready,
                "char_login_registered": char_login_link,
                "map_registered": map_registration, "registered_map_count": map_count,
                "map_login_registered": map_login_link}

    def _wait_gameplay_ready(self, home: Path, timeout: float, services: dict) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, record in services.items():
                if not self._process_matches(home, record):
                    raise RuntimeError(f"managed {name} service exited before gameplay registration completed")
            readiness = self._gameplay_readiness(home)
            if all(readiness[key] for key in (
                    "login_ready", "char_ready", "char_login_registered",
                    "map_registered", "map_login_registered")):
                return readiness
            time.sleep(.15)
        readiness = self._gameplay_readiness(home)
        missing = [key for key, value in readiness.items()
                   if key != "registered_map_count" and not value]
        if readiness["registered_map_count"] == 0:
            missing.append("non_empty_registered_map_list")
        raise TimeoutError("managed rAthena gameplay readiness timed out; missing: " + ", ".join(missing))

    @staticmethod
    def _sql(command: list[str], sql: str) -> None:
        result = subprocess.run(command, input=sql, text=True, capture_output=True,
                                timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("managed database setup failed: " + _safe_tail(result.stderr))

    def inspect(self, lab_id: str) -> dict:
        home = self._owned(lab_id)
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        service_port = {"database": "db", "login": "login", "char": "char", "map": "map"}
        health = {}
        for name, record in owner.get("services", {}).items():
            healthy = self._process_matches(home, record)
            if healthy and name in service_port:
                try:
                    with socket.create_connection(("127.0.0.1", int(owner["ports"][service_port[name]])), timeout=.25):
                        pass
                except OSError:
                    healthy = False
            health[name] = healthy
        expected = {"database", "login", "char", "map"} if owner["state"] == "running" else set()
        readiness = self._gameplay_readiness(home)
        gameplay_ready = all(readiness[key] for key in (
            "login_ready", "char_ready", "char_login_registered", "map_registered",
            "map_login_registered"))
        if owner["state"] == "running":
            health["gameplay_registration"] = gameplay_ready
        healthy = (owner["state"] == "running" and expected.issubset(health)
                   and all(health.values()) and gameplay_ready)
        return {"id": lab_id, "state": owner["state"], "ports": owner["ports"],
                "services": owner["services"], "health": health,
                "readiness": readiness, "healthy": healthy,
                "path": str(home)}

    def _owned(self, lab_id: str) -> Path:
        if not lab_id or "/" in lab_id or "\\" in lab_id or lab_id.startswith("."):
            raise ValueError("invalid managed laboratory ID")
        home = (self.root / lab_id).resolve()
        if not home.is_relative_to(self.root) or not (home / "owner.json").is_file():
            raise ValueError("laboratory is not owned by this KoreLab data directory")
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        if owner.get("format") != self.VERSION or owner.get("id") != lab_id or owner.get("home") != str(home):
            raise RuntimeError("managed laboratory ownership record is invalid")
        return home


def _safe_tail(text: str) -> str:
    # Server errors can echo SQL; avoid echoing any lines that look like secrets.
    lines = [line for line in text.splitlines() if not any(word in line.casefold()
        for word in ("password", "identified by", "grant "))]
    return " ".join(lines)[-500:] or "no diagnostic was provided"
