# Contributing

Contributions should preserve OpenKore/rAthena as the native gameplay authorities and keep laboratory ownership, identity checks, bounded execution, secret handling, independent verification, and cleanup intact.

Before opening a pull request:

1. Explain the user-visible behavior or demonstrated defect.
2. Keep changes focused and include a regression for behavior changes.
3. Run `python -m pytest tests -q` and the relevant Perl/source-compatibility checks described in `tests/README.md`.
4. Separate offline results from live results and state which source revisions were exercised.
5. Confirm no credentials, private profiles, runtime output, or machine-specific data are included.

For bugs, include the KoreLab version, OS/architecture, Python/Perl versions, relevant OpenKore/rAthena revisions, sanitized command/config snippets, and expected versus observed outcome. Do not attach passwords, database dumps, private profiles, or unredacted logs. Use GitHub Issues for ordinary bugs and feature requests. Security reports belong in a private channel; see `SECURITY.md`.

Independently authored KoreLab code and documentation are distributed under the MIT License in `LICENSE`, with public copyright identification `boscv` (2026). This grant does not cover upstream-derived patches or other third-party material; consult `THIRD_PARTY_NOTICES.md` and `LICENSE_REVIEW.md`. Contributions should identify their origin and any third-party terms, and contributors must have authority to submit the material under the applicable terms. Do not copy code from OpenKore, rAthena, or another project into KoreLab core without documenting its source and reviewing compatibility with KoreLab's licensing boundaries.
