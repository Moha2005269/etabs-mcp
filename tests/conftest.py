from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeBackend  # noqa: E402

from etabs_mcp.server import create_server  # noqa: E402
from etabs_mcp.session import EtabsSession  # noqa: E402
from etabs_mcp.settings import Settings  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="Run tests against a real local ETABS installation.")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live and a local ETABS")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


class Harness:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.backend = FakeBackend()
        self.session = EtabsSession(self.settings, self.backend)
        self.mcp = create_server(self.settings, self.session)

    async def call(self, tool: str, **arguments: Any) -> Any:
        result = await self.mcp.call_tool(tool, arguments)
        if isinstance(result, tuple):
            content, structured = result
            if structured is not None:
                return structured.get("result", structured) if set(structured) == {"result"} else structured
            return content
        return result

    def called(self, qualified: str) -> list[dict[str, Any]]:
        return [args for name, args in self.backend.calls if name == qualified]


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def make_harness():
    return Harness
