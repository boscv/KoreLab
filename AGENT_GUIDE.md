# Working with KoreLab as an external agent

KoreLab does not perform natural-language planning. The agent is responsible for understanding the objective, selecting existing native operations, inspecting prerequisites, composing a scenario, interpreting evidence, and deciding whether an outcome is established.

## Discover and ground capabilities

1. Run `korelab capabilities` and `korelab agent-capabilities` to see implemented operations.
2. Run `korelab technical --help`; after identifying local source checkouts, use `korelab technical refresh --openkore PATH --rathena PATH` to build a version-aware source index. Confirm relevant entries in the referenced installed source; a catalog hit is an index, not an execution guarantee.
3. Use `korelab doctor --openkore PATH --rathena PATH` for local prerequisites. Database verification is opt-in with `--verify-database` and must use an authorized endpoint.
4. Inspect effective OpenKore config, character/session state, scenario inputs, server permissions, reachability, and independent verification methods before execution.

## Prepare and execute

- Prefer supplied declarative JSON scenarios or a small caller-owned scenario. Inspect `korelab run --help`, `korelab plan --help`, and the scenario structure before running.
- Use native OpenKore behavior for authentication, movement, combat, skills, dialogue, inventory, and profiles. KoreLab may prepare controlled disposable state, but it must not fabricate the gameplay outcome.
- For a managed server, inspect `korelab lab --help` and `korelab lab plan --help` first. Use only a KoreLab-owned disposable lab for destructive setup or fault injection.
- Keep credentials in private environment/configuration files. Do not send them in chat, command output, reports, or version control.

## Verify and recover

Record initial state, observed client behavior, and confirmed outcome separately. A process being alive, a command being queued, or an attack being issued is not proof of a server-side effect. Prefer persisted rAthena state or an authoritative server event where the objective permits it.

When something fails, check prerequisites and current state first. Inspect native implementations only after confirming the operation started under valid conditions. Before retrying a non-idempotent action, query server state to determine whether it already completed. Recover one failed agent at a time when shared infrastructure remains healthy; keep retries bounded and retain diagnostic evidence.

At actual completion, stop owned sessions, verify termination/disconnection, restore authorized snapshots, confirm restoration independently, then destroy only the disposable resources created by the run. Report any state that could not be confirmed.

## Preserve corrections

If a genuine defect is demonstrated, reproduce it on a compatible source revision, make the smallest isolated correction, add focused behavioral regression coverage, and document source revision, evidence, and patch lifecycle. Keep upstream source trees untouched. Do not turn a configuration or prerequisite error into a code patch.
