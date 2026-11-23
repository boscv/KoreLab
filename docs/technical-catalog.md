# Technical capability catalog

KoreLab's technical catalog is generated from installed OpenKore, eventMacro, and optionally rAthena source. It records source references, version/revision information, provenance, and uncertainty. It is a discovery index, not a semantic parser and not proof that a capability is enabled or permitted on a particular server.

Catalogs and server-specific knowledge live in the private runtime data home, not in the public source tree. Create or refresh a catalog from the exact sources under test:

```sh
korelab technical refresh --openkore /path/to/openkore --rathena /path/to/rathena
korelab technical status --openkore /path/to/openkore --rathena /path/to/rathena
korelab technical coverage
korelab technical search "storage deposit items"
korelab technical objective "store selected inventory items"
```

Entries discovered from registries may have unknown arguments, preconditions, or effects. Inspect cited source before performing effectful operations. `AMBIGUOUS` and `UNSUPPORTED_OR_UNKNOWN` mean the request is unresolved; they must not be converted into guessed command syntax. Server-specific NPC, map, item, quest, and permission facts belong in separately scoped knowledge and require runtime or source evidence for the selected server.
