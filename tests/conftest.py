from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _allow_localhost_sockets(socket_enabled):
    """Tests run a fake Salus cloud on 127.0.0.1."""
    yield
