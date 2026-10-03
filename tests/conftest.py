from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    # anyio ships a pytest plugin (it's already a dependency of httpx and mcp),
    # so async tests need no pytest-asyncio. Only run them on asyncio.
    return "asyncio"
