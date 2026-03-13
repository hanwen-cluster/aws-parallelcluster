# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Base checks that apply to every cluster: SSH connectivity, log error scan."""

import logging

from assertpy import assert_that

from framework.config_driven.models import Check, CheckContext, ClusterFeatures, Phase, RuntimeContext
from framework.config_driven.registry import default_registry


# ---------------------------------------------------------------------------
# SSH Connectivity
# ---------------------------------------------------------------------------

def check_ssh_connectivity(ctx: CheckContext):
    """Verify we can execute a command on the head node via SSH."""
    logging.info("Verifying SSH connectivity to head node")
    result = ctx.remote_command_executor.run_remote_command("hostname")
    assert_that(result.stdout.strip()).is_not_empty()
    logging.info("SSH connectivity OK — hostname: %s", result.stdout.strip())


default_registry.register(Check(
    name="SSH Connectivity",
    category="base",
    phase=Phase.INFRASTRUCTURE,
    condition=lambda features, runtime: True,  # always run
    run=check_ssh_connectivity,
))


# ---------------------------------------------------------------------------
# Head Node Instance Type
# ---------------------------------------------------------------------------

def check_head_node_instance_type(ctx: CheckContext):
    """Verify the head node is running the expected instance type."""
    logging.info("Verifying head node instance type")
    result = ctx.remote_command_executor.run_remote_command(
        "curl -s http://169.254.169.254/latest/meta-data/instance-type"
    )
    actual = result.stdout.strip()
    expected = ctx.features.head_node_instance_type
    if expected:
        assert_that(actual).is_equal_to(expected)
        logging.info("Head node instance type matches: %s", actual)
    else:
        logging.info("Head node instance type (no expected value to check): %s", actual)


default_registry.register(Check(
    name="Head Node Instance Type",
    category="base",
    phase=Phase.INFRASTRUCTURE,
    condition=lambda f, r: bool(f.head_node_instance_type),
    run=check_head_node_instance_type,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# Log Error Scan
# ---------------------------------------------------------------------------

def check_no_errors_in_logs(ctx: CheckContext):
    """Scan ParallelCluster daemon logs for CRITICAL/ERROR entries.

    This reuses the same logic as tests.common.assertions.assert_no_errors_in_logs
    but is callable from the config-driven framework.
    """
    from tests.common.assertions import assert_no_errors_in_logs
    logging.info("Scanning cluster logs for errors (scheduler=%s)", ctx.features.scheduler)
    assert_no_errors_in_logs(ctx.remote_command_executor, ctx.features.scheduler, skip_ice=True)
    logging.info("No errors found in cluster logs")


default_registry.register(Check(
    name="Log Error Scan",
    category="base",
    phase=Phase.TEARDOWN,
    condition=lambda features, runtime: True,  # always run
    run=check_no_errors_in_logs,
    depends_on=["SSH Connectivity"],
))
