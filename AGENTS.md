# KoreLab engineering instructions

- Treat KoreLab as laboratory tooling around OpenKore and rAthena. Inspect and prefer their native commands, routing, AI, plugins, and server mechanisms before implementing parallel behavior.
- Keep the AI agent external. KoreLab provides bounded operations, isolation, observations, verification, and cleanup; it is not an LLM or gameplay engine.
- Debug prerequisites first. Check effective configuration, session/task state, character requirements, items/equipment, map and interaction range, NPC/target identity, server permissions, and operation order before investigating internals.
- A command accepted or queued is intent, not proof. Distinguish process, authenticated session, action execution, server acceptance, and persisted outcome. Use independent rAthena observations when available.
- Preserve account/character ownership checks, offline mutation guards, snapshot comparisons, bounded timeouts/retries, secret redaction, failure isolation, and exact restoration. Report unresolved restoration as failure or uncertainty.
- Keep individual agent failures isolated from healthy agents and shared services. A failed acceptance window alone does not justify destroying a usable disposable laboratory. At actual scenario end, terminate owned processes, verify cleanup, and destroy only resources the lab owns.
- Before changing external source, use a disposable worktree/copy, record the compatible revision, inspect `git diff --check`, and keep the correction reversible. Never modify upstream checkouts by default.
- After two unsuccessful attempts at the same hypothesis, stop repeating it and choose a diagnostic that distinguishes alternatives. Continue reasonable investigation; a failed hypothesis is not itself a blocker.
- Keep changes small and tests proportional. Do not add a replacement AI, pathfinder, conversation engine, orchestration framework, or dependency framework without a demonstrated missing capability.
- Never commit credentials, private runtime profiles, generated runs, database dumps, or real account identities. Use synthetic fixtures and private `KORELAB_DATA_HOME` paths.
- Validate offline regressions separately from source-compatibility and live integration checks. State exactly which environment each result covers; do not claim Windows/Linux support without native validation.
