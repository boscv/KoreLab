"""Read and validate OpenKore server profiles without exposing profile credentials."""
from __future__ import annotations

import pathlib
import re


class ProfileError(ValueError):
    pass


_PROFILE = re.compile(r"^[^\[\]\r\n]{1,120}$")


def parse_profile_file(path: pathlib.Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            current = sections.setdefault(name.casefold(), {"_name": name})
            continue
        if current is None:
            continue
        key, separator, value = line.partition(" ")
        if separator:
            current[key.strip().casefold()] = value.strip()
    return sections


def find_server_profile(openkore_checkout: pathlib.Path, profile: str) -> dict[str, str]:
    """Resolve profile data from the checkout's server tables; never returns credentials."""
    if not _PROFILE.fullmatch(profile):
        raise ProfileError("server profile name is invalid")
    checkout = openkore_checkout.expanduser().resolve()
    for path in (checkout / "tables/servers.txt", checkout / "control/servers.txt"):
        if not path.is_file():
            continue
        rows = parse_profile_file(path)
        selected = rows.get(profile.casefold())
        if selected is None:
            continue
        if selected.get("dead") == "1":
            raise ProfileError("selected OpenKore server profile is marked unavailable")
        host = selected.get("ip") or selected.get("hostname")
        port_text = selected.get("port")
        try:
            port = int(port_text or "")
        except ValueError as exc:
            raise ProfileError("selected OpenKore server profile has no valid login endpoint") from exc
        if (not host or len(host) > 253 or re.search(r"[\s/\\]", host)
                or not 1 <= port <= 65535):
            raise ProfileError("selected OpenKore server profile has an invalid login endpoint")
        return {"name": selected["_name"], "host": host, "port": str(port),
                "server_type": selected.get("servertype", "")}
    raise ProfileError("selected server profile is not present in this OpenKore checkout's server tables")


def read_openkore_credentials(path: pathlib.Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """Read a local OpenKore config and return settings plus values to redact from output."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ProfileError("OpenKore profile config does not exist or is not a regular file")
    settings: dict[str, str] = {}
    secret_keys = {"password", "loginpincode", "otpseed", "adminpassword",
                   "xkore_id", "sessionid", "sessionid1", "sessionid2"}
    secrets: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition(" ")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        settings[key.casefold()] = value
        if key.casefold() in secret_keys and value:
            secrets.append(value)
    return settings, tuple(secrets)


def validate_selection(profile: str, server_index: int, character: str,
                       character_slot: int) -> dict[str, str | int]:
    if not _PROFILE.fullmatch(profile):
        raise ProfileError("server profile name is invalid")
    if type(server_index) is not int or server_index < 0:
        raise ProfileError("server index must be a non-negative integer")
    if not isinstance(character, str) or not character.strip() or len(character) > 24:
        raise ProfileError("character name must be explicitly selected")
    if type(character_slot) is not int or not 0 <= character_slot <= 8:
        raise ProfileError("character slot must be between 0 and 8")
    return {"profile": profile, "server_index": server_index,
            "character": character.strip(), "character_slot": character_slot}
