"""pytest over the self-tests, without a second copy of what they are.

``.githooks/checks.sh`` stays the one place the checked modules are listed —
it is the push gate, and it runs where a push happens, which may have no
pytest. This shim asks it for the list and runs each self-test as its own
test item, so ``pytest`` (and anything that speaks pytest) sees them all.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _modules() -> list[str]:
    out = subprocess.run(
        ["bash", str(ROOT / ".githooks" / "checks.sh"), "--list"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.split()


@pytest.mark.parametrize("module", _modules())
def test_selftest(module: str) -> None:
    proc = subprocess.run(
        [sys.executable, f"{module}.py", "--self-test"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"{module} --self-test failed\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    )
