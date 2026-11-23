# Managed disposable laboratory

The `lab` commands create and manage rAthena resources under `KORELAB_DATA_HOME` (default `~/.korelab`). The source repositories are inputs; the lab creates private configs, data, profiles, credentials, logs, and service ownership records separately. It binds managed endpoints to loopback and only stops or destroys resources recorded as owned by the selected laboratory.

Inspect commands before operating:

```sh
korelab lab --help
korelab lab plan --help
korelab lab create --help
korelab lab bootstrap --help
```

Typical lifecycle:

```sh
korelab lab plan --openkore PATH --rathena-runtime PATH --xstools-overlay PATH
korelab lab create --openkore PATH --rathena-runtime PATH --xstools-overlay PATH --start-point MAP,X,Y
korelab lab start LAB_ID
korelab lab bootstrap LAB_ID
korelab lab status LAB_ID
korelab run /absolute/path/to/scenario.json --openkore PATH --rathena PATH
korelab lab stop LAB_ID
korelab lab destroy LAB_ID
```

Use the ID printed by `create`; review every plan and status before destructive actions. `bootstrap` provisions through native rAthena/OpenKore mechanisms. The fleet path is available with `korelab lab fleet-run --help`; it requires an explicit roster or supported defaults, a compatible OpenKore build, XSTools overlay, and a valid start location.

Managed profiles enable OpenKore's native portal compiler automatically so a headless bootstrap never blocks on a console prompt. Compiler output stays in the private run profile. The lab also copies rAthena's effective `db/import` files into the lab so the disposable server can use them without writing into the source runtime.

The controller uses bounded waits and owned-process checks. If termination or restoration cannot be confirmed, preserve the lab state for diagnosis and report uncertainty; do not claim successful cleanup. Do not point the managed lifecycle at an unrelated database or modify the original OpenKore/rAthena checkouts to make a scenario pass.
