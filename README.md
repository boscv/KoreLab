# KoreLab

KoreLab is a local engineering and test laboratory for OpenKore and rAthena. It helps an external developer or AI agent inspect capabilities, prepare an isolated disposable server, run declarative scenarios through native OpenKore behavior, observe results, verify persisted state independently, and clean up owned resources.

KoreLab does not contain an AI planner or replace OpenKore, rAthena, their gameplay logic, or their source repositories. The agent plans and interprets experiments; OpenKore and rAthena remain authoritative for gameplay. KoreLab's database operations are limited to explicitly configured rAthena/MySQL-compatible environments and guarded laboratory state.

## Platform and prerequisites

- Python 3.10 or newer is declared; release-preparation checks used Python 3.14.7 on macOS arm64. Linux has not been validated. Native Windows x64 support is implemented in part but remains unverified on Windows.
- For offline capability inspection, Python is sufficient.
- Live scenarios need compatible local OpenKore and rAthena checkouts. Set `KORELAB_OPENKORE` and `KORELAB_RATHENA`, or pass source paths to commands that accept them.
- Live OpenKore also needs Perl and XSTools built for that Perl/runtime combination. `korelab doctor` reports source and toolchain findings; a successful diagnostic does not itself prove a game session works.
- rAthena integration requires a MySQL/MariaDB-protocol server and a compatible Python driver or client. KoreLab does not support arbitrary SQL engines for rAthena. Database credentials are supplied privately through environment variables; `doctor` does not connect unless `--verify-database` is explicitly requested.

The runtime uses Python's standard library. Optional test and MySQL dependencies are installed separately. Runtime reports, catalogs, credentials, locks, and session output are stored under `~/.korelab/` by default, outside the source checkout. Set `KORELAB_DATA_HOME` to select another data directory.

## Install and first run

```sh
git clone <KoreLab-repository-url>
cd KoreLab
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

korelab --help
korelab doctor
korelab capabilities
korelab setup                 # inspect a non-mutating dependency plan
```

The Windows bootstrap is `bootstrap-windows.ps1`; it creates a repository-local virtual environment and asks before using `winget` to install Python. Then activate `.venv` and run `python -m pip install -e '.[dev]'`. The bootstrap does not install or configure game servers.

For a clean offline smoke check, `korelab capabilities` lists implemented runner operations without connecting to a server. `korelab doctor` inspects local tools and configuration; it only verifies a database connection when explicitly passed `--verify-database`.

## Managed laboratory and scenarios

Inspect exact options before running an operation:

```sh
korelab lab --help
korelab lab plan --help
korelab lab create --help
korelab run --help
```

The managed-lab lifecycle is explicit: plan, create, start/bootstrap, run, inspect status, then stop or destroy the owned disposable lab. KoreLab uses ownership records and bounded cleanup; review the plan and identify the source checkouts and database before creating a lab. See the [managed laboratory guide](docs/managed-laboratory.md) for prerequisites and command sequence. Never point destructive operations at an unrelated installation.

Scenarios are JSON files supplied by the caller. This distribution does not bundle laboratory instances, local server layouts, historical characters, quest/NPC fixtures, or acceptance scenarios. Create and review a scenario for your own disposable server and pass its absolute path to KoreLab:

```sh
korelab plan --help
korelab run /absolute/path/to/scenario.json
```

For a managed native OpenKore fleet, see `korelab lab fleet-run --help`. It requires explicit OpenKore/rAthena source paths, an XSTools overlay, and a start point. It launches separate native OpenKore processes using the profiles plugin; it is not an emulator for gameplay.

## External AI agents

KoreLab is intended to be operated by an external engineering agent. Start with `korelab capabilities`, `korelab agent-capabilities`, and `korelab technical --help`; build a source-backed technical catalog with `korelab technical refresh --openkore PATH --rathena PATH` when verified checkouts are available. See [AGENT_GUIDE.md](AGENT_GUIDE.md) for discovery, execution, independent verification, failure investigation, and cleanup practices.

## Patches, safety, and limitations

Patches in `patches/` are optional and source-version-specific. They are not applied automatically. Review their provenance and applicability, then use a disposable source worktree and `git apply --check` before applying one. Do not modify original upstream checkouts by default.

KoreLab does not create a user's real accounts, change passwords, configure unrelated databases, install game servers automatically, or guarantee that a source checkout is compatible merely because its directory exists. Native Windows execution, Linux, and live scenarios on a fresh external installation have not been validated as part of this release preparation. In-game Warp Portal casting is not claimed as validated.

See [README.pt-BR.md](README.pt-BR.md) for the Portuguese guide. Test commands and test prerequisites are in [tests/README.md](tests/README.md). Independently authored KoreLab code and documentation are licensed under MIT, with public copyright identification `boscv`, 2026; see [LICENSE](LICENSE). The optional OpenKore and rAthena patches retain their applicable upstream terms, and the added Warp Portal module is identified separately. OpenKore and rAthena are separate projects and are not bundled. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [LICENSE_REVIEW.md](LICENSE_REVIEW.md) for exact boundaries and pinned revisions.
