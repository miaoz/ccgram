import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _instant_shell_setup_sleep(monkeypatch):
    """setup_shell_prompt has two real sleeps (0.1s + 0.3s) for keyboard
    pacing. Tests don't need to wait."""
    from ccgram.providers import shell_infra

    monkeypatch.setattr(shell_infra.asyncio, "sleep", AsyncMock())
