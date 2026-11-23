# Third-party notices

KoreLab's independently authored code and documentation are licensed under MIT as described in the root [LICENSE](LICENSE), with public copyright identification `boscv`, 2026. This does not grant rights over third-party works or patches that modify them.

The repository includes four optional patch files. They are not applied automatically and are not required by the KoreLab package runtime.

| Patch | Target | Applicable upstream terms |
| --- | --- | --- |
| `patches/openkore/macro-listitem-index-26a8ea5.patch` | OpenKore `26a8ea5959119081689521a0a7e25fabd9a31980` | GNU GPL version 2; classified `GPL-2.0-only` |
| `patches/openkore/warp-portal-routing-128e464c.patch` | OpenKore `128e464c131623900d4542ceddf8fe9a061dbcc3` | Its upstream-file changes/context are GNU GPL version 2 (`GPL-2.0-only`). Its added `src/WarpPortal.pm` is independently authored KoreLab material under MIT. |
| `patches/openkore/xstools-perl-ccflags-128e464c.patch` | OpenKore `128e464c131623900d4542ceddf8fe9a061dbcc3` | GNU GPL version 2; classified `GPL-2.0-only` |
| `patches/rathena/1ca3e770-null-character-session.patch` | rAthena `1ca3e7700939ea40120cb51a777f8681ba5e84ae` | GNU GPL version 3; classified `GPL-3.0-only` |

Verbatim license texts from the named OpenKore and rAthena revisions are included in [`LICENSES/`](LICENSES/). OpenKore's target source identifies GNU GPL version 2 without an evidenced later-version option, so the OpenKore patches are classified as `GPL-2.0-only`. See [LICENSE_REVIEW.md](LICENSE_REVIEW.md) for the file-level boundary, provenance record, and limits of this review.

KoreLab does not bundle OpenKore or rAthena source trees, binaries, database dumps, or credentials. Users obtain those separately under the respective projects' terms. Optional `PyMySQL` is fetched at installation time and is not vendored. The standalone patch files do not make a licensing claim about a user's separate upstream checkout or assert that it and KoreLab core form a combined work.
