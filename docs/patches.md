# Optional upstream patches

Patches are not applied by KoreLab automatically. Each patch must be reviewed against its recorded source revision before use. Original upstream checkouts are never patch targets by default.

1. Create a disposable source worktree at the patch's documented compatible revision.
2. Review the patch and the required behavior.
3. Run `git apply --check /path/to/patch.patch` from the disposable worktree.
4. Apply with `git apply` only after the check succeeds.
5. Build and validate the isolated copy; discard the worktree to revert.

The retained OpenKore patches address Warp Portal memo/routing behavior (target `128e464c131623900d4542ceddf8fe9a061dbcc3`), XSTools Perl build flags for that same older revision, and macro `listItem` indexing (target `26a8ea5959119081689521a0a7e25fabd9a31980`). The rAthena patch guards a null character-session packet path at `1ca3e7700939ea40120cb51a777f8681ba5e84ae`. Each diff passed `git apply --check` against that exact source revision during release preparation. The XSTools flags correction is not needed by the inspected newer OpenKore revision, whose build script already includes Perl `ccflags`.

Patches are optional and are not KoreLab runtime dependencies. Their file-level license boundaries and provenance are in `THIRD_PARTY_NOTICES.md` and `LICENSE_REVIEW.md`. The OpenKore patches target GPL-2.0-only source; the rAthena patch targets GPL-3.0-only source. The Warp Portal patch's added `src/WarpPortal.pm` is independently authored KoreLab material under MIT, while its upstream-file changes remain under the applicable OpenKore terms. No patch should be applied twice or used to bypass source compatibility checks.
