from pathlib import Path
import tempfile

import pytest


_RUNTIME_TEST_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "pytest"


@pytest.fixture
def tmp_path() -> Path:
    """Create retained test state under the repository's ignored runtime tree."""

    _RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="case-", dir=_RUNTIME_TEST_ROOT))
