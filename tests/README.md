# Tests and validation

Install the development extra from a checkout:

```sh
python -m pip install -e '.[dev]'
python -m pytest tests -q
python -m unittest discover -s tests -q
```

Pytest is the complete Python suite. `unittest` discovery is retained as a secondary check and does not collect all top-level pytest functions.

Most tests use isolated temporary fixtures. Tests that inspect installed OpenKore/rAthena source require compatible checkouts in `KORELAB_OPENKORE` and `KORELAB_RATHENA`; tests that inspect the native macro parser also accept `OPENKORE_SRC`. They are explicitly marked or skipped when those prerequisites are absent. Do not interpret an unavailable source check as a pass for that source revision.

Perl regressions use the upstream source only when explicitly configured:

```sh
OPENKORE_SRC="$KORELAB_OPENKORE" perl tests/perl/warp_portal_memos.t
```

The Warp Portal test uses the local stubs plus the selected OpenKore source. The macro parser test checks the pinned revision recorded in the test and requires that revision's Perl dependencies and built compatible XSTools to load; it is not an offline Perl-only check. The rAthena null-session integration check is opt-in and requires an owner-verified disposable char-server; it never starts or stops a service. See `tests/integration/verify_rathena_null_session_guard.py` for its exact arguments.

The release contains no bundled lab scenarios or environment-specific fixtures. Tests build their minimal synthetic inputs in temporary directories; no private character, server layout, quest script, or acceptance data is needed. Examples and live-run inputs must be supplied by the operator and reviewed for the selected source revisions. `patches/` contains optional, revision-specific upstream fixes; see `docs/patches.md`.
