# Licensing review

## KoreLab-authored material

The repository owner's release decision authorizes the MIT license for independently authored KoreLab code and documentation for which the owner holds the necessary rights. The root [LICENSE](LICENSE) identifies the public holder as `boscv` and the year as 2026. This grant does not cover third-party material identified below. No claim is made here about material outside the checked-in distribution.

The Python package metadata declares `MIT` for the KoreLab package. The wheel packages `korelab*` only; it does not package the optional source patches or upstream source trees. Repository documentation, tests, and other original KoreLab-authored files follow the same MIT grant where the owner holds the necessary rights.

## Retained optional patches

The patch files are optional diffs and are not required by KoreLab's runtime. Their patches contain or modify upstream source expression and are distributed with the upstream project's applicable terms. The notices identify each target and the exact license text copied from that pinned revision. These classifications apply to the corresponding upstream-derived patch material; they do not extend the upstream license to independent KoreLab modules or claim that the runtime and a separately applied upstream checkout form one combined work.

| File | Exact upstream target | File-level classification and contents | Provenance |
| --- | --- | --- | --- |
| `patches/openkore/macro-listitem-index-26a8ea5.patch` | OpenKore `26a8ea5959119081689521a0a7e25fabd9a31980`, `plugins/macro/Macro/Utilities.pm` | `GPL-2.0-only` for the patch modifying OpenKore source | Patch purpose and attribution are recorded in the KoreLab development history. |
| `patches/openkore/warp-portal-routing-128e464c.patch` | OpenKore `128e464c131623900d4542ceddf8fe9a061dbcc3`; `control/config.txt`, `control/routeweights.txt`, `src/Network/Receive.pm`, `src/Network/Send.pm`, `src/Task/CalcMapRoute.pm`, `src/Task/MapRoute.pm`; adds `src/WarpPortal.pm` | The upstream-file changes and copied patch context are `GPL-2.0-only`. The independently authored `src/WarpPortal.pm` contribution is KoreLab-authored material under MIT, subject to the root license. | The development history records the optional routing change. The added module is identified separately from modified upstream files. |
| `patches/openkore/xstools-perl-ccflags-128e464c.patch` | OpenKore `128e464c131623900d4542ceddf8fe9a061dbcc3`, `SConstruct` | `GPL-2.0-only` for the patch modifying OpenKore source | Patch purpose and attribution are recorded in the KoreLab development history. It targets an older build script; the inspected newer OpenKore revision already includes Perl `ccflags`. |
| `patches/rathena/1ca3e770-null-character-session.patch` | rAthena `1ca3e7700939ea40120cb51a777f8681ba5e84ae`, `src/char/char_clif.cpp` | `GPL-3.0-only` for the patch modifying rAthena source | The patch header describes the managed-lab acceptance purpose; the development history records the patch commit. |

OpenKore's LICENSE file at both pinned targets identifies the project as distributed under GNU GPL version 2. The affected OpenKore source does not establish a later-version grant, so these patches are identified conservatively as GPL-2.0-only. The rAthena target's LICENSE is GNU GPL version 3. The exact license texts from the pinned revisions are included under [LICENSES/](LICENSES/).

Patch commit authorship and purpose are recorded in the private KoreLab development history. This public distribution attributes the independent contribution to `boscv` under the owner's stated public copyright identification; it does not assert ownership of upstream-authored code. The patch files remain optional. Review the exact revision and use a disposable upstream worktree before applying them.

## Other included components and limits

- OpenKore and rAthena source trees, binaries, database dumps, and credentials are not included. Their own projects and license terms remain separate.
- The optional `PyMySQL` dependency is installed by the user and is not vendored here.
- No local lab fixtures or historical eventMacro scripts are distributed. Operators supply eventMacro files.
- This inventory describes the checked-in release candidate and its identified origins. It is not a legal opinion. If additional upstream-derived material is identified before publication, its source notice and distribution terms must be reviewed before release.
