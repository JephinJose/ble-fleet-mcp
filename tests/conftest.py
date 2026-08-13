from __future__ import annotations

import pytest

from fleet_mcp.transports.fake import FakeTransport, make_simulated_fleet


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport(max_concurrent=2)


@pytest.fixture
def simulated_fleet_50() -> FakeTransport:
    return make_simulated_fleet(50, max_concurrent=4)
