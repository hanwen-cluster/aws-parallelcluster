#!/usr/bin/env python3
"""
Minimal config-driven approach: Add automatic feature detection to existing tests.
"""

from functools import wraps

import yaml
from framework.feature_detectors import FEATURE_DETECTORS


def config_driven(required_features):
    """
    Decorator to make existing tests config-driven with minimal changes.

    Usage:
    @config_driven(["fsx_lustre"])
    def test_fsx_lustre(cluster, region, ...):
        # existing test code unchanged
    """

    def decorator(test_func):
        @wraps(test_func)
        def wrapper(*args, **kwargs):
            # Extract cluster config from test parameters
            cluster_config = kwargs.get("cluster_config") or _extract_config_from_cluster(kwargs.get("cluster"))

            # Check if required features are present
            if cluster_config:
                for feature in required_features:
                    if feature in FEATURE_DETECTORS:
                        if not FEATURE_DETECTORS[feature](cluster_config):
                            import pytest

                            pytest.skip(f"Skipping {test_func.__name__} - {feature} not configured")

            # Run original test unchanged
            return test_func(*args, **kwargs)

        return wrapper

    return decorator


def _extract_config_from_cluster(cluster):
    """Extract config from cluster object if available."""
    if hasattr(cluster, "config"):
        return cluster.config
    return None


# Minimal changes to existing test files - just add decorators:


@config_driven(["fsx_lustre"])
def test_fsx_lustre(region, pcluster_config_reader, clusters_factory, scheduler_commands_factory, **kwargs):
    """Existing FSx test - no changes to implementation."""
    # Original test code remains exactly the same
    pass


@config_driven(["efa"])
def test_efa(region, pcluster_config_reader, clusters_factory, scheduler_commands_factory, **kwargs):
    """Existing EFA test - no changes to implementation."""
    # Original test code remains exactly the same
    pass


@config_driven(["gpu_instances", "p_series_instances"])  # OR condition
def test_gpu_workloads(region, pcluster_config_reader, clusters_factory, scheduler_commands_factory, **kwargs):
    """Existing GPU test - no changes to implementation."""
    # Original test code remains exactly the same
    pass


@config_driven(["dcv"])
def test_dcv_configuration(region, pcluster_config_reader, clusters_factory, scheduler_commands_factory, **kwargs):
    """Existing DCV test - no changes to implementation."""
    # Original test code remains exactly the same
    pass


# For tests that should always run, no decorator needed:
def test_essential_features(region, pcluster_config_reader, clusters_factory, scheduler_commands_factory, **kwargs):
    """Core test - always runs, no decorator needed."""
    # Original test code remains exactly the same
    pass
