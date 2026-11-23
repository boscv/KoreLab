# Platforms and dependencies

## Validation status

macOS arm64 is the only platform exercised for this release-preparation snapshot. Linux has not been validated. Windows x64 contains native code paths and a PowerShell Python bootstrap, but is not supported as a validated platform until Windows integration tests pass.

## Required tools by workflow

| Workflow | Requirements |
| --- | --- |
| `capabilities`, scenario parsing, offline assessment | Python 3.10+ |
| Source-backed technical catalog | OpenKore source; rAthena source is optional for OpenKore-only information |
| Live OpenKore session | OpenKore checkout, Perl, compatible built XSTools, native server profile/configuration |
| Managed rAthena lab | Built rAthena runtime, compatible source/configuration, MariaDB/MySQL server/client, compatible Perl/XSTools for OpenKore |
| rAthena DB observation | MySQL/MariaDB protocol plus a compatible Python DB-API driver or supported client; use an authorized configured endpoint |

The Python runtime has no mandatory third-party package dependencies. PyMySQL is an optional driver (`pip install -e '.[mysql]'`); `korelab setup` can also display providers and perform a specifically selected installation after `--apply`. The managed disposable database still requires a local MariaDB/MySQL server installation; setup does not install, start, or configure it automatically.

XSTools is compiled native code and must match the Perl used to run OpenKore. Build it from the selected OpenKore checkout in a disposable copy, then pass the compatible `src/auto/XSTools` directory with `--xstools-overlay`; KoreLab checks the Perl build marker and refuses to launch on a mismatch. In the validated macOS arm64 environment, `doctor` correctly reported that the initial upstream checkout had no compatible bundle. Building the pinned OpenKore source required using the installed Homebrew `readline` prefix and adapting the old SCons script's removed `Environment.has_key` calls in the temporary build copy. KoreLab does not modify an upstream checkout or silently rebuild XSTools. Other Perl/SCons/Homebrew combinations remain to be validated.

The fresh-clone wheel installation and CLI smoke check passed on the same host. The documented `python3 -m venv` setup could not bootstrap `pip` with this Homebrew Python 3.14 installation: `ensurepip` returned an externally-managed-environment error (PEP 668). This is a Python installation constraint, not a KoreLab runtime failure; users who encounter it need a working isolated Python environment before installing the development extra.

## Diagnostics and setup

```sh
korelab doctor --help
korelab doctor --openkore /path/to/openkore --rathena /path/to/rathena
korelab setup
korelab setup --help
```

Doctor reports installed tools, configured paths, providers, and local service/process observations separately. A database connection is attempted only with explicit `--verify-database`, using the connection variables described in the main README. Setup is a plan by default. An apply action requires an exact capability and provider; elevation needs the explicit `--allow-elevation` option. It does not provision database users or alter upstream checkouts.

KoreLab supports rAthena database access over MySQL/MariaDB protocol. SQLite is available only for KoreLab's optional local knowledge storage. PostgreSQL and SQL Server are not implemented. An arbitrary SQL engine must not be treated as rAthena-compatible.
