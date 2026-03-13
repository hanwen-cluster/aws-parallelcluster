# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Config-driven integration test entry point.

This single test function replaces many per-feature test functions. Given a
ParallelCluster config YAML, it:
1. Analyzes the config to determine which features are present.
2. Builds runtime context from AWS APIs.
3. Selects applicable checks from the registry.
4. Creates the cluster.
5. Runs all selected checks with structured reporting.

Usage:
    pytest tests/config_driven/test_config_driven.py \
        --region us-east-1 --os alinux2023 --scheduler slurm \
        --instance c5n.18xlarge
"""

import logging

import pytest
import yaml
from remote_command_executor import RemoteCommandExecutor

from framework.config_driven.config_analyzer import analyze_config
from framework.config_driven.executor import CheckExecutor
from framework.config_driven.models import CheckContext
from framework.config_driven.registry import default_registry
from framework.config_driven.runtime_context import build_runtime_context

# Import checks package to trigger self-registration
import framework.config_driven.checks  # noqa: F401

from tests.common.schedulers_common import get_scheduler_commands


@pytest.mark.usefixtures("serial_execution_by_instance")
def test_cluster_config(
    os,
    region,
    scheduler,
    instance,
    architecture,
    pcluster_config_reader,
    clusters_factory,
    scheduler_commands_factory,
    test_datadir,
    request,
):
    """Config-driven test: deploy a cluster and run all applicable checks."""
    # Step 1: Render the cluster config
    cluster_config = pcluster_config_reader()

    # Step 2: Parse the rendered config to extract features
    with open(str(cluster_config), encoding="utf-8") as f:
        config_dict = yaml.safe_load(f)
    features = analyze_config(config_dict)

    # Step 3: Build runtime context
    runtime = build_runtime_context(region, os, architecture, features)

    # Step 4: Select checks
    checks = default_registry.select_checks(features, runtime)
    logging.info("Selected %d checks for this config", len(checks))

    if not checks:
        pytest.skip("No checks applicable for this config")

    # Step 5: Create cluster
    cluster = clusters_factory(cluster_config)
    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)

    # Step 6: Build check context
    ctx = CheckContext(
        cluster=cluster,
        remote_command_executor=remote_command_executor,
        scheduler_commands=scheduler_commands,
        features=features,
        runtime=runtime,
        test_datadir=test_datadir,
        request=request,
    )

    # Step 7: Execute checks
    executor = CheckExecutor()
    report = executor.run_checks(checks, ctx)

    # Step 8: Report and assert
    report.report()
    report.assert_all_passed()
