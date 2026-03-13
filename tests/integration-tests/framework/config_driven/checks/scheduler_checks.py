# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Scheduler checks: job submission, queue validation, compute node health."""

import logging

from assertpy import assert_that

from framework.config_driven.models import Check, CheckContext, Phase
from framework.config_driven.registry import default_registry


# ---------------------------------------------------------------------------
# Slurm Daemon Running
# ---------------------------------------------------------------------------

def check_slurm_daemon(ctx: CheckContext):
    """Verify slurmctld is running on the head node."""
    logging.info("Checking slurmctld is active")
    result = ctx.remote_command_executor.run_remote_command(
        "systemctl is-active slurmctld", raise_on_error=False
    )
    assert_that(result.stdout.strip()).is_equal_to("active")
    logging.info("slurmctld is active")


default_registry.register(Check(
    name="Slurm Daemon Running",
    category="scheduler",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.scheduler == "slurm",
    run=check_slurm_daemon,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# Slurm Partitions Match Config
# ---------------------------------------------------------------------------

def check_slurm_partitions(ctx: CheckContext):
    """Verify that Slurm partitions match the queues defined in the cluster config."""
    logging.info("Verifying Slurm partitions match config queues")
    expected_partitions = {q.name for q in ctx.features.queues}
    actual_partitions = set(ctx.scheduler_commands.get_partitions())
    logging.info("Expected partitions: %s", expected_partitions)
    logging.info("Actual partitions: %s", actual_partitions)
    assert_that(actual_partitions).contains(*expected_partitions)
    logging.info("All expected partitions present")


default_registry.register(Check(
    name="Slurm Partitions Match Config",
    category="scheduler",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.scheduler == "slurm" and len(f.queues) > 0,
    run=check_slurm_partitions,
    depends_on=["Slurm Daemon Running"],
))


# ---------------------------------------------------------------------------
# Basic Job Submission
# ---------------------------------------------------------------------------

def check_basic_job_submission(ctx: CheckContext):
    """Submit a simple job and verify it completes successfully."""
    logging.info("Submitting basic test job")
    # Pick the first queue/partition
    partition = ctx.features.queues[0].name if ctx.features.queues else None
    if partition:
        result = ctx.scheduler_commands.submit_command("hostname", partition=partition)
    else:
        result = ctx.scheduler_commands.submit_command("hostname")
    job_id = ctx.scheduler_commands.assert_job_submitted(result.stdout)
    ctx.scheduler_commands.wait_job_completed(job_id)
    ctx.scheduler_commands.assert_job_succeeded(job_id)
    logging.info("Basic job %s completed successfully", job_id)


default_registry.register(Check(
    name="Basic Job Submission",
    category="scheduler",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.scheduler in ("slurm", "awsbatch"),
    run=check_basic_job_submission,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# Compute Nodes Healthy
# ---------------------------------------------------------------------------

def check_compute_nodes_online(ctx: CheckContext):
    """Verify that compute nodes are online and in a healthy state."""
    logging.info("Checking compute node status")
    nodes = ctx.scheduler_commands.get_compute_nodes()
    logging.info("Compute nodes found: %d", len(nodes))
    # For clusters with MinCount > 0, we expect at least some nodes
    expected_min = sum(q.min_count for q in ctx.features.queues)
    if expected_min > 0:
        assert_that(len(nodes)).is_greater_than_or_equal_to(expected_min)
        logging.info("At least %d compute nodes online (expected min: %d)", len(nodes), expected_min)
    else:
        logging.info("No minimum node count configured; %d nodes currently online", len(nodes))


default_registry.register(Check(
    name="Compute Nodes Online",
    category="scheduler",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.scheduler == "slurm",
    run=check_compute_nodes_online,
    depends_on=["Slurm Daemon Running"],
))
