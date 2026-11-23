from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_native_warp_portal_memo_contracts():
    perl = shutil.which("perl")
    if not perl:
        pytest.skip("Perl is required for native memo contract tests")
    patch = (ROOT / "patches/openkore/warp-portal-routing-128e464c.patch").read_text()
    marker = "+++ b/src/WarpPortal.pm\n"
    assert marker in patch, "versioned OpenKore patch must include WarpPortal.pm"
    source = patch.split(marker, 1)[1].split("\ndiff --git ", 1)[0]
    module = "\n".join(line[1:] for line in source.splitlines() if line.startswith("+")) + "\n"
    with tempfile.TemporaryDirectory(prefix="korelab-warp-module-") as temp_dir:
        source_dir = pathlib.Path(temp_dir)
        (source_dir / "WarpPortal.pm").write_text(module)
        env = os.environ.copy()
        env["OPENKORE_SRC"] = str(source_dir)
        result = subprocess.run(
            [perl, str(ROOT / "tests/perl/warp_portal_memos.t")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1..29" in result.stdout
