from __future__ import annotations

import os
from pathlib import Path

import pytest

from korelab.server_tables import native_table_search_path


OPENKORE = Path(os.environ.get("KORELAB_OPENKORE", "/__korelab_openkore_unset__")).expanduser()


def test_profile_table_folders_precede_upstream_and_private_overlay(tmp_path):
    overlay = tmp_path / "run-tables"
    upstream = tmp_path / "openkore-tables"
    result = native_table_search_path(overlay, upstream, ("kRO", "iRO"))

    assert result.split(os.pathsep) == [
        str((upstream / "kRO").resolve()),
        str((upstream / "iRO").resolve()),
        str(upstream.resolve()),
        str(overlay.resolve()),
    ]


def test_effective_server_list_is_first_and_overlay_remains_last(tmp_path):
    overlay = tmp_path / "run-tables"
    upstream = tmp_path / "openkore-tables"
    result = native_table_search_path(overlay, upstream, ("kRO", "iRO"),
                                      server_list_overlay=True)

    paths = result.split(os.pathsep)
    assert paths[0] == str(overlay.resolve())
    assert paths[-1] == str(overlay.resolve())
    assert paths[1:3] == [str((upstream / "kRO").resolve()),
                          str((upstream / "iRO").resolve())]


@pytest.mark.source_compatibility
@pytest.mark.skipif(not OPENKORE.is_dir(), reason="set KORELAB_OPENKORE")
def test_search_order_matches_native_openkore_table_resolution():
    settings = (OPENKORE / "src/Settings.pm").read_text()
    body = settings[settings.index("sub _findFileFromFolders {"):]
    assert body.index("foreach my $dir (@{$folders})") < body.index("return $filename")
    assert "unshift @tablesFolders" in settings[settings.index("sub addTablesFolders {"):]
    assert "return $tablesFolders[$#tablesFolders]" in settings
    assert "undef %{$r_hash};" in (OPENKORE / "src/FileParsers.pm").read_text()
