#!/usr/bin/env python3
"""
Config-driven test framework for ParallelCluster integration tests.

This framework automatically determines which tests to run based on the cluster configuration content,
eliminating the need to manually specify test checks in each test function.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

import yaml
from framework.feature_detectors import FEATURE_DETECTORS


@dataclass
class TestCheck:
    """Represents a single test check that can be performed."""

    name: str
    feature_key: str  # Key in FEATURE_DETECTORS
    test_function: Callable  # The actual test function to execute
    priority: int = 1  # Higher priority tests run first


class ConfigDrivenTestRunner:
    """Main class that runs tests based on cluster configuration content."""

    def __init__(self):
        self.registered_checks: List[TestCheck] = []
        self.logger = logging.getLogger(__name__)

    def register_check(self, name: str, feature_key: str, test_function: Callable, priority: int = 1):
        """Register a test check with its feature key and test function."""
        if feature_key not in FEATURE_DETECTORS:
            raise ValueError(f"Unknown feature key: {feature_key}")
        self.registered_checks.append(TestCheck(name, feature_key, test_function, priority))

    def run_tests_for_config(self, cluster_config: Dict, cluster, region, scheduler_commands_factory, **kwargs):
        """Run all applicable tests based on the cluster configuration."""
        applicable_tests = []

        # Find all applicable tests
        for check in self.registered_checks:
            detector_func = FEATURE_DETECTORS[check.feature_key]
            if detector_func(cluster_config):
                applicable_tests.append(check)

        # Sort by priority (higher priority first)
        applicable_tests.sort(key=lambda x: x.priority, reverse=True)

        self.logger.info(f"Running {len(applicable_tests)} tests based on cluster configuration:")
        for test in applicable_tests:
            self.logger.info(f"  - {test.name} (priority: {test.priority})")

        # Execute all applicable tests
        results = {}
        for test in applicable_tests:
            try:
                self.logger.info(f"Running test: {test.name}")
                test.test_function(cluster, region, scheduler_commands_factory, cluster_config, **kwargs)
                results[test.name] = "PASSED"
                self.logger.info(f"✓ {test.name} passed")
            except Exception as e:
                results[test.name] = f"FAILED: {e}"
                self.logger.error(f"✗ {test.name} failed: {e}")
                raise

        return results


# Global test runner instance
test_runner = ConfigDrivenTestRunner()


# Test implementation functions


# Test functions for different features
def test_fsx_lustre_functionality(cluster, region, scheduler_commands_factory, config, **kwargs):
    """Test FSx Lustre functionality."""
    from tests.storage.storage_common import check_fsx

    # Extract mount directories from config
    mount_dirs = []
    for storage in config.get("SharedStorage", []):
        if storage.get("StorageType") == "FsxLustre":
            mount_dirs.append(storage.get("MountDir"))

    check_fsx(cluster, region, scheduler_commands_factory, mount_dirs, None)


def test_nccl_on_p_series(cluster, region, scheduler_commands_factory, config, **kwargs):
    """Test NCCL on P-series instances."""
    from remote_command_executor import RemoteCommandExecutor

    from tests.common.nccl_common import test_nccl

    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)

    # Run NCCL test
    test_nccl(remote_command_executor, scheduler_commands)


def test_efa_functionality(cluster, region, scheduler_commands_factory, config, **kwargs):
    """Test EFA functionality."""
    from remote_command_executor import RemoteCommandExecutor

    from tests.common.mpi_common import _test_mpi
    from tests.common.utils import fetch_instance_slots

    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)

    # Get instance type for slot calculation
    queues = config.get("Scheduling", {}).get("SlurmQueues", [])
    instance_type = None
    for queue in queues:
        for compute_resource in queue.get("ComputeResources", []):
            if compute_resource.get("Efa", {}).get("Enabled", False):
                instances = compute_resource.get("Instances", [])
                if instances:
                    instance_type = instances[0].get("InstanceType")
                    break

    if instance_type:
        slots_per_instance = fetch_instance_slots(region, instance_type)
        _test_mpi(
            remote_command_executor,
            slots_per_instance,
            "slurm",
            scheduler_commands,
            region,
            cluster.cfn_name,
            3,  # scaledown_idletime
            verify_scaling=True,
            num_computes=2,
        )


def test_dcv_functionality(cluster, region, scheduler_commands_factory, config, **kwargs):
    """Test DCV functionality."""
    from remote_command_executor import RemoteCommandExecutor

    remote_command_executor = RemoteCommandExecutor(cluster)

    # Basic DCV connectivity test
    result = remote_command_executor.run_remote_command("sudo systemctl is-active dcv-server")
    assert result.stdout.strip() == "active", "DCV server should be active"


# Register all test checks with the new API
test_runner.register_check("FSx Lustre", "fsx_lustre", test_fsx_lustre_functionality, priority=2)
test_runner.register_check("NCCL on P-series", "p_series_instances", test_nccl_on_p_series, priority=3)
test_runner.register_check("EFA", "efa", test_efa_functionality, priority=3)
test_runner.register_check("DCV", "dcv", test_dcv_functionality, priority=1)


def load_cluster_config(config_path: str) -> Dict:
    """Load and parse cluster configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def config_driven_test(cluster_config_path: str, cluster, region, scheduler_commands_factory, **kwargs):
    """
    Main entry point for config-driven testing.

    Args:
        cluster_config_path: Path to the cluster configuration YAML file
        cluster: Cluster object
        region: AWS region
        scheduler_commands_factory: Factory for scheduler commands
        **kwargs: Additional test parameters
    """
    config = load_cluster_config(cluster_config_path)
    test_runner.run_tests_for_config(config, cluster, region, scheduler_commands_factory, **kwargs)
