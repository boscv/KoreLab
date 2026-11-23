from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PINNED_OPENKORE_REVISION = "26a8ea5959119081689521a0a7e25fabd9a31980"


def test_native_macro_listitem_expansion_preserves_zero_values():
    source = os.environ.get("OPENKORE_SRC")
    if not source:
        pytest.skip("requires OPENKORE_SRC pointing to the pinned OpenKore source")
    source_path = pathlib.Path(source).resolve()
    if not (source_path / "plugins/macro/Macro/Parser.pm").is_file():
        pytest.fail("OPENKORE_SRC must be the OpenKore repository root")

    perl = shutil.which("perl")
    if not perl:
        pytest.skip("Perl is required for the installed macro parser regression")

    revision = subprocess.run(
        ["git", "-C", str(source_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert revision.returncode == 0, revision.stderr
    assert revision.stdout.strip() == PINNED_OPENKORE_REVISION, (
        "regression is pinned to OpenKore 26a8ea5959119081689521a0a7e25fabd9a31980; "
        f"received {revision.stdout.strip()}"
    )

    env = os.environ.copy()
    env["OPENKORE_SRC"] = str(source_path)
    result = subprocess.run(
        [perl, str(ROOT / "tests/perl/macro_listitem.t")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1..6" in result.stdout
