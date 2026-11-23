"""Version-aware preparation of temporary OpenKore runtime profiles."""
from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
from functools import lru_cache
from typing import Any

from .platform_support import set_private_permissions


CONFIG_REF = re.compile(r"\$config\s*\{\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))")
CONTROL_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CONFIG_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
AI_MODES = {"on", "manual", "off"}


def _source_files(root: pathlib.Path) -> list[pathlib.Path]:
    paths = []
    for folder in (root / "src", root / "plugins"):
        if folder.is_dir():
            paths.extend(path for suffix in ("*.pm", "*.pl") for path in folder.rglob(suffix)
                         if not {"auto", "deps", ".git"}.intersection(path.parts))
    return sorted(set(paths))


def _parenthesized_source(text: str, open_index: int) -> str | None:
    """Return one source call's body, respecting nesting and quoted strings."""
    depth, quote, escaped = 0, None, False
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#":
            newline = text.find("\n", index)
            if newline < 0:
                return None
            # Replace commented contents with spaces and continue counting.
            return _parenthesized_source(text[:index] + " " * (newline - index) + text[newline:], open_index)
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:index]
    return None


@lru_cache(maxsize=32)
def discover_config_keys(root: pathlib.Path, additional_sources: tuple[str, ...] = ()) -> set[str]:
    """Index literal config keys in this OpenKore checkout and its installed plugins."""
    root = root.expanduser().resolve()
    keys: set[str] = set()
    paths = _source_files(root)
    paths.extend(path for path in (root / "control/config.txt",)
                 if path.is_file())
    source_text = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix in (".pm", ".pl"):
            source_text.append(text)
            keys.update(next(group for group in match.groups() if group is not None)
                        for match in CONFIG_REF.finditer(text))
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\s+\S", stripped)
            if match:
                keys.add(match.group(1))
    for text in additional_sources:
        # A selected run-local config profile may predeclare persistent
        # macro lifecycle keys that are created by `conf -f` at runtime.
        # Accept its actual config lines as declarations without weakening
        # source validation for arbitrary override names.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\s+\S", stripped)
            if match:
                keys.add(match.group(1))
        keys.update(next(group for group in match.groups() if group is not None)
                    for match in CONFIG_REF.finditer(text))
        source_text.append(text)
    if any('useSelf_item_$i' in text for text in source_text):
        # OpenKore creates numbered useSelf_item entries dynamically; these
        # literal names are validated against the installed consumer below.
        for index in range(16):
            keys.update({f"useSelf_item_{index}", f"useSelf_item_{index}_hp",
                         f"useSelf_item_{index}_sp", f"useSelf_item_{index}_timeout",
                         f"useSelf_item_{index}_dcOnEmpty"})
    if any('equipAuto_$i' in text for text in source_text):
        # CoreLogic iterates numbered equipAuto profiles while Actor::Item
        # resolves their slot values by inventory item name.
        slots = ("topHead", "midHead", "lowHead", "leftHand", "rightHand",
                 "leftAccessory", "rightAccessory", "robe", "armor", "shoes", "arrow")
        for index in range(16):
            keys.add(f"equipAuto_{index}")
            keys.update(f"equipAuto_{index}_{slot}" for slot in slots)
            keys.update({f"equipAuto_{index}_weight", f"equipAuto_{index}_whileSitting",
                         f"equipAuto_{index}_target"})
    # OpenKore loads map-specific field_<map> settings by constructing their
    # names dynamically, so these cannot appear as literal hash keys in source.
    keys.add("field_<map>")
    return keys


@lru_cache(maxsize=32)
def discover_control_files(root: pathlib.Path) -> dict[str, list[str]]:
    """Find control-file names registered by the installed client and plugins."""
    root = root.expanduser().resolve()
    result: dict[str, set[str]] = {}
    call = re.compile(r"Settings::addControlFile\s*\(")
    literal = re.compile(r"(?:getControlFilename|get[A-Za-z]+Filename)\s*\(\s*['\"]([^'\"]+\.txt)['\"]\s*\)")
    for path in _source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in call.finditer(text):
            open_index = match.end() - 1
            block = _parenthesized_source(text, open_index)
            if block is None:
                continue
            names = literal.findall(block)
            internal = re.search(r"internalName\s*=>\s*['\"]([^'\"]+\.txt)['\"]", block)
            if internal:
                names.append(internal.group(1))
            for name in names:
                if CONTROL_FILE.fullmatch(name):
                    result.setdefault(name, set()).add(path.relative_to(root).as_posix())
    result.pop("config.txt", None)
    return {name: sorted(paths) for name, paths in sorted(result.items())}


def validate_runtime_profile(root: pathlib.Path, openkore: dict[str, Any], *,
                             additional_config_sources: tuple[str, ...] = ()) -> dict[str, Any]:
    """Validate a declarative, temporary OpenKore runtime profile against local source."""
    if not isinstance(openkore, dict):
        raise ValueError("openkore runtime profile must be an object")
    unknown_fields = set(openkore) - {"ai_mode", "config_overrides", "control_file_overrides", "plugin_overlays"}
    if unknown_fields:
        raise ValueError(f"unsupported OpenKore runtime profile fields: {sorted(unknown_fields)}")
    ai_mode = openkore.get("ai_mode", "manual")
    if not isinstance(ai_mode, str) or ai_mode not in AI_MODES:
        raise ValueError("openkore.ai_mode must be on, manual, or off according to the installed OpenKore CLI")
    raw_config = openkore.get("config_overrides", {})
    if not isinstance(raw_config, dict):
        raise ValueError("openkore.config_overrides must be an object")
    raw_plugins = openkore.get("plugin_overlays", [])
    if not isinstance(raw_plugins, list) or len(raw_plugins) > 16:
        raise ValueError("OpenKore plugin_overlays must be a list of at most 16 local plugin files")
    plugin_specs = []
    plugin_sources = list(additional_config_sources)
    seen_plugin_names = set()
    for spec in raw_plugins:
        if not isinstance(spec, dict) or set(spec) - {"source", "sha256"} or "source" not in spec:
            raise ValueError("each plugin overlay requires source and may declare sha256")
        source = pathlib.Path(spec["source"]).expanduser().resolve()
        if source.suffix != ".pl" or not source.is_file():
            raise ValueError("plugin overlay source must be an existing .pl file")
        if source.stat().st_size > 1_000_000:
            raise ValueError("plugin overlay source exceeds the 1 MB limit")
        name = source.name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pl", name) or name == "stateObserver.pl":
            raise ValueError("plugin overlay filename is invalid or reserved")
        if name.casefold() in seen_plugin_names:
            raise ValueError("plugin overlay filenames must be unique")
        seen_plugin_names.add(name.casefold())
        content = source.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if "sha256" in spec and spec["sha256"] != digest:
            raise ValueError(f"plugin overlay hash mismatch for {name}")
        # Plugins::loadAll scans each configured plugin directory. If a
        # plugin is already installed under the same basename, staging a
        # second copy makes OpenKore load both implementations. Reuse the
        # installed file only when its bytes are identical; otherwise fail
        # closed instead of creating an ambiguous duplicate registration.
        installed_paths = [path for path in (root / "plugins").glob(name)
                           if path.is_file()]
        installed_paths.extend(path for path in (root / "plugins").glob(f"*/{name}")
                               if path.is_file())
        installed_hashes = {hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in installed_paths}
        if installed_hashes and installed_hashes != {digest}:
            raise ValueError(f"plugin overlay {name} conflicts with a different installed plugin")
        plugin_sources.append(content)
        plugin_specs.append({"name": name, "source": source, "sha256": digest, "content": content,
                             "already_installed": bool(installed_hashes)})
    config_keys = discover_config_keys(root, tuple(plugin_sources))
    normalized_config: dict[str, str | int | float | bool] = {}
    unsupported = []
    for key, value in raw_config.items():
        if not isinstance(key, str) or not CONFIG_KEY.fullmatch(key):
            raise ValueError("OpenKore config override names must be simple configuration keys")
        if key not in config_keys and not (key.startswith("field_") and re.fullmatch(r"field_[A-Za-z0-9_]+", key)):
            unsupported.append(key)
            continue
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError(f"OpenKore config override {key} must have a scalar value")
        # JSON booleans are common for native on/off settings; Perl config
        # values use their numeric spelling, not Python's True/False names.
        value = ("1" if value else "0") if isinstance(value, bool) else str(value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"OpenKore config override {key} cannot contain a line break")
        normalized_config[key] = value
    if unsupported:
        raise ValueError(f"OpenKore config key(s) not found in this installation's source or active config: {sorted(unsupported)}")

    raw_files = openkore.get("control_file_overrides", {})
    if not isinstance(raw_files, dict):
        raise ValueError("openkore.control_file_overrides must be an object")
    supported_files = discover_control_files(root)
    file_specs = {}
    unknown_files = []
    for filename, spec in raw_files.items():
        if not isinstance(filename, str) or not CONTROL_FILE.fullmatch(filename):
            raise ValueError("control-file overrides must use a simple basename")
        if filename not in supported_files:
            unknown_files.append(filename)
            continue
        if not isinstance(spec, dict) or set(spec) != {"mode", "content"}:
            raise ValueError(f"control-file override {filename} requires exactly mode and content")
        if spec["mode"] not in {"append", "replace"} or not isinstance(spec["content"], str):
            raise ValueError(f"control-file override {filename} must use append/replace and string content")
        if len(spec["content"].encode("utf-8")) > 1_000_000:
            raise ValueError(f"control-file override {filename} exceeds the 1 MB limit")
        file_specs[filename] = {"mode": spec["mode"], "content": spec["content"]}
    if unknown_files:
        raise ValueError(f"control file(s) are not registered by this OpenKore installation: {sorted(unknown_files)}")
    return {"ai_mode": ai_mode, "config_overrides": normalized_config,
            "control_file_overrides": file_specs,
            "plugin_overlays": plugin_specs,
            "discovered_control_files": supported_files}


def requires_profile_authorization(openkore: dict[str, Any]) -> bool:
    """AI activation or behavior overlays require explicit caller authorization."""
    return bool(openkore.get("ai_mode", "manual") != "manual"
                or openkore.get("config_overrides")
                or openkore.get("control_file_overrides")
                or openkore.get("plugin_overlays"))


def stage_plugin_overlays(directory: pathlib.Path, specs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Copy explicitly selected local plugin source into the private OpenKore runtime."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = []
    for spec in specs:
        target = directory / spec["name"]
        shutil.copyfile(spec["source"], target)
        set_private_permissions(target, 0o600)
        staged.append({"name": spec["name"], "sha256": spec["sha256"],
                       "staged_path": str(target)})
    return staged


def stage_control_file_overrides(root: pathlib.Path, directory: pathlib.Path,
                                 specs: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Build private control-file overlays while leaving the installation untouched."""
    if not specs:
        return []
    root = root.expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = []
    source_registry = discover_control_files(root)
    for filename, spec in sorted(specs.items()):
        original = root / "control" / filename
        original_bytes = original.read_bytes() if original.is_file() else b""
        content = spec["content"].encode("utf-8")
        if spec["mode"] == "append":
            prefix = original_bytes
            if prefix and not prefix.endswith(b"\n"):
                prefix += b"\n"
            effective = prefix + content
            if content and not content.endswith(b"\n"):
                effective += b"\n"
        else:
            effective = content
        target = directory / filename
        target.write_bytes(effective)
        set_private_permissions(target, 0o600)
        manifest.append({"file": filename, "mode": spec["mode"],
            "original_exists": original.is_file(),
            "original_sha256": hashlib.sha256(original_bytes).hexdigest() if original_bytes else None,
            "effective_sha256": hashlib.sha256(effective).hexdigest(),
            "source_references": source_registry.get(filename, [])})
    return manifest
