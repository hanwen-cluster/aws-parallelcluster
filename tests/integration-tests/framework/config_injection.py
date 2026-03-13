#!/usr/bin/env python3
"""
Config injection for existing test infrastructure with minimal changes.
"""

from pathlib import Path

import pytest
import yaml


def pytest_configure(config):
    """Add config-driven markers."""
    config.addinivalue_line("markers", "config_driven: mark test as config-driven")


@pytest.fixture
def cluster_config(pcluster_config_reader):
    """Inject cluster config into tests automatically."""
    config_path = pcluster_config_reader()
    if isinstance(config_path, Path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    return None


# Minimal conftest.py addition - just add this fixture to existing conftest.py:
def pytest_runtest_setup(item):
    """Auto-skip tests based on config content."""
    # Check if test has config_driven marker
    config_driven_marker = item.get_closest_marker("config_driven")
    if config_driven_marker:
        required_features = config_driven_marker.args[0] if config_driven_marker.args else []

        # Get cluster config from fixtures
        cluster_config = None
        if "cluster_config" in item.fixturenames:
            # Config will be injected by fixture
            pass  # Skip logic will be handled by decorator

        # Additional skip logic can be added here if needed
