# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""EFA checks: installation, ENI configuration, MPI, SHM transfer, NCCL.

These checks are extracted from tests/efa/test_efa.py and tests/common/mpi_common.py
so they can be triggered automatically when a config has EFA enabled on a supported instance.
"""

import logging

import boto3
from assertpy import assert_that
from utils import get_compute_nodes_instance_ids

from framework.config_driven.models import Check, CheckContext, ClusterFeatures, Phase, RuntimeContext
from framework.config_driven.registry import default_registry


def _any_compute_supports_efa(features: ClusterFeatures, runtime: RuntimeContext) -> bool:
    """True if config has EFA enabled AND at least one compute instance type supports it."""
    if not features.has_efa:
        return False
    for q in features.queues:
        if q.efa_enabled:
            for it in q.instance_types:
                if runtime.supports_efa(it):
                    return True
    return False


def _efa_partition(features: ClusterFeatures) -> str:
    """Return the name of the first EFA-enabled queue/partition."""
    for q in features.queues:
        if q.efa_enabled:
            return q.name
    return ""


# ---------------------------------------------------------------------------
# EFA Installation
# ---------------------------------------------------------------------------

def check_efa_installation(ctx: CheckContext):
    """Verify EFA device is present on compute nodes and absent on head node.

    Extracted from test_efa._test_efa_installation.
    """
    partition = _efa_partition(ctx.features)
    logging.info("Checking EFA installation on partition '%s'", partition)

    result = ctx.scheduler_commands.submit_command(
        "lspci -n > /shared/lspci.out", partition=partition
    )
    job_id = ctx.scheduler_commands.assert_job_submitted(result.stdout)
    ctx.scheduler_commands.wait_job_completed(job_id)
    ctx.scheduler_commands.assert_job_succeeded(job_id)

    # EFA device on compute
    result = ctx.remote_command_executor.run_remote_command("cat /shared/lspci.out")
    assert_that(result.stdout).contains("1d0f:efa")

    # EFA device NOT on head node
    result = ctx.remote_command_executor.run_remote_command("lspci -n")
    assert_that(result.stdout).does_not_contain("1d0f:efa")
    logging.info("EFA device present on compute, absent on head node")


default_registry.register(Check(
    name="EFA Installation",
    category="efa",
    phase=Phase.FUNCTIONAL,
    condition=_any_compute_supports_efa,
    run=check_efa_installation,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# EFA ENI Configuration
# ---------------------------------------------------------------------------

def check_efa_eni_configuration(ctx: CheckContext):
    """Verify compute nodes have correct EFA network interface configuration.

    Extracted from test_efa._test_efa_eni_configuration.
    Under PC 3.15+:
    - Each compute node should have exactly one ENI with a private IP.
    - All other ENIs should be efa-only.
    """
    region = ctx.runtime.region
    ec2_client = boto3.client("ec2", region_name=region)
    compute_ids = get_compute_nodes_instance_ids(ctx.cluster.cfn_name, region)

    for instance_id in compute_ids:
        instance_info = ec2_client.describe_instances(
            InstanceIds=[instance_id]
        )["Reservations"][0]["Instances"][0]
        enis = instance_info["NetworkInterfaces"]
        logging.info("Instance %s has %d ENIs", instance_id, len(enis))

        enis_with_ip = [e for e in enis if e.get("InterfaceType", "interface") != "efa-only"]
        efa_only = [e for e in enis if e.get("InterfaceType") == "efa-only"]

        assert_that(
            len(enis_with_ip),
            description=f"Instance {instance_id} should have exactly 1 ENI with a private IP",
        ).is_equal_to(1)

        if len(enis) > 1:
            assert_that(
                len(efa_only),
                description=f"Instance {instance_id}: all ENIs except one should be efa-only",
            ).is_equal_to(len(enis) - 1)

        logging.info("Instance %s: %d IP ENI(s), %d efa-only ENI(s)", instance_id, len(enis_with_ip), len(efa_only))


default_registry.register(Check(
    name="EFA ENI Configuration",
    category="efa",
    phase=Phase.FUNCTIONAL,
    condition=_any_compute_supports_efa,
    run=check_efa_eni_configuration,
    depends_on=["EFA Installation"],
))


# ---------------------------------------------------------------------------
# SHM Transfer Enabled
# ---------------------------------------------------------------------------

def check_shm_transfer(ctx: CheckContext):
    """Verify SHM transfer is not disabled by ptrace protection.

    Extracted from test_efa._test_shm_transfer_is_enabled.
    """
    partition = _efa_partition(ctx.features)
    logging.info("Checking SHM transfer on partition '%s'", partition)

    result = ctx.scheduler_commands.submit_command(
        "fi_info -p efa 2>&1 > /shared/fi_info.out", partition=partition
    )
    job_id = ctx.scheduler_commands.assert_job_submitted(result.stdout)
    ctx.scheduler_commands.wait_job_completed(job_id)
    ctx.scheduler_commands.assert_job_succeeded(job_id)

    result = ctx.remote_command_executor.run_remote_command("cat /shared/fi_info.out")
    assert_that(result.stdout).does_not_contain(
        "SHM transfer will be disabled because of ptrace protection"
    )
    logging.info("SHM transfer is enabled")


default_registry.register(Check(
    name="SHM Transfer Enabled",
    category="efa",
    phase=Phase.FUNCTIONAL,
    condition=_any_compute_supports_efa,
    run=check_shm_transfer,
    depends_on=["EFA Installation"],
))


# ---------------------------------------------------------------------------
# MPI Ring Test
# ---------------------------------------------------------------------------

def check_mpi_ring(ctx: CheckContext):
    """Run an MPI ring test across compute nodes.

    Extracted from tests.common.mpi_common._test_mpi.
    """
    from tests.common.mpi_common import _test_mpi
    partition = _efa_partition(ctx.features)

    # Find slots for the first EFA-enabled instance type
    slots = 0
    for q in ctx.features.queues:
        if q.efa_enabled and q.instance_types:
            slots = ctx.runtime.get_slots(q.instance_types[0])
            break
    if slots == 0:
        slots = 2  # fallback

    logging.info("Running MPI ring test (partition=%s, slots_per_instance=%d)", partition, slots)
    _test_mpi(
        ctx.remote_command_executor,
        slots,
        ctx.features.scheduler,
        ctx.scheduler_commands,
        partition=partition,
    )
    logging.info("MPI ring test passed")


default_registry.register(Check(
    name="MPI Ring Test",
    category="efa",
    phase=Phase.FUNCTIONAL,
    condition=_any_compute_supports_efa,
    run=check_mpi_ring,
    depends_on=["EFA Installation"],
))


# ---------------------------------------------------------------------------
# NCCL Benchmarks (GPU only)
# ---------------------------------------------------------------------------

def _efa_on_p_instance(features: ClusterFeatures, runtime: RuntimeContext) -> bool:
    """True if EFA is enabled on a p-series (GPU) instance."""
    if not features.has_efa:
        return False
    for q in features.queues:
        if q.efa_enabled:
            for it in q.instance_types:
                if runtime.is_p_instance(it) and runtime.is_nvidia_gpu(it):
                    return True
    return False


def check_nccl_benchmarks(ctx: CheckContext):
    """Run NCCL benchmarks on GPU instances with EFA.

    Extracted from tests.common.nccl_common.install_and_run_nccl_benchmarks.
    """
    from tests.common.nccl_common import install_and_run_nccl_benchmarks

    # Find the p-series instance type and its partition
    for q in ctx.features.queues:
        if q.efa_enabled:
            for it in q.instance_types:
                if ctx.runtime.is_p_instance(it) and ctx.runtime.is_nvidia_gpu(it):
                    logging.info("Running NCCL benchmarks on %s", it)
                    install_and_run_nccl_benchmarks(
                        ctx.remote_command_executor,
                        "openmpi",
                        ctx.scheduler_commands,
                        it,
                        ctx.runtime.os,
                    )
                    logging.info("NCCL benchmarks passed for %s", it)
                    return


default_registry.register(Check(
    name="NCCL Benchmarks",
    category="efa",
    phase=Phase.PERFORMANCE,
    condition=_efa_on_p_instance,
    run=check_nccl_benchmarks,
    depends_on=["EFA Installation", "MPI Ring Test"],
))
