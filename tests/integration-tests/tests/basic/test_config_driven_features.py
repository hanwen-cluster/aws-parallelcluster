#!/usr/bin/env python3
"""
Example test using the config-driven test framework.

This test automatically runs checks based on what's configured in the cluster YAML,
eliminating the need to manually specify which tests to run.
"""

import logging

from framework.config_driven_test_framework import config_driven_test


def test_config_driven_features(
    region,
    pcluster_config_reader,
    clusters_factory,
    scheduler_commands_factory,
    os,
    instance,
    scheduler,
    request,
):
    """
    Test cluster features automatically based on configuration content.

    This test will automatically:
    - Test FSx if FSx is configured
    - Test NCCL if P-series instances are configured
    - Test EFA if EFA is enabled
    - Test DCV if DCV is enabled
    - And so on...
    """
    logging.info("Starting config-driven feature testing")

    # Create cluster with the provided configuration
    cluster_config = pcluster_config_reader()
    cluster = clusters_factory(cluster_config)

    # Run tests automatically based on configuration content
    config_driven_test(
        cluster_config_path=str(cluster_config),
        cluster=cluster,
        region=region,
        scheduler_commands_factory=scheduler_commands_factory,
        os=os,
        instance=instance,
        scheduler=scheduler,
        request=request,
    )

    logging.info("Config-driven feature testing completed")
