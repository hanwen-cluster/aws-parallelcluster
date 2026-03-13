#!/usr/bin/env python3
"""
Hybrid test approach: Config-driven execution with stable test names for historical tracking.
"""

from typing import Dict, List

import pytest


class HybridTestRunner:
    """Combines config-driven logic with stable test names for historical data."""

    def __init__(self):
        self.stable_tests = {
            # Core functionality tests - always run
            "test_basic_cluster_functionality": self._test_basic_functionality,
            "test_scheduler_operations": self._test_scheduler_operations,
            # Feature-specific tests - run only if feature detected
            "test_fsx_lustre": self._test_fsx_lustre,
            "test_efa_networking": self._test_efa_networking,
            "test_gpu_workloads": self._test_gpu_workloads,
            "test_dcv_remote_desktop": self._test_dcv_remote_desktop,
            "test_spot_instance_handling": self._test_spot_instance_handling,
            "test_login_node_access": self._test_login_node_access,
            "test_custom_bootstrap_scripts": self._test_custom_bootstrap_scripts,
            "test_cloudwatch_monitoring": self._test_cloudwatch_monitoring,
        }

    def should_run_test(self, test_name: str, config: Dict) -> bool:
        """Determine if a test should run based on config content."""
        from framework.feature_detectors import FEATURE_DETECTORS

        # Core tests always run
        if test_name in ["test_basic_cluster_functionality", "test_scheduler_operations"]:
            return True

        # Feature-specific test conditions
        conditions = {
            "test_fsx_lustre": FEATURE_DETECTORS["fsx_lustre"],
            "test_efa_networking": FEATURE_DETECTORS["efa"],
            "test_gpu_workloads": lambda c: FEATURE_DETECTORS["gpu_instances"](c)
            or FEATURE_DETECTORS["p_series_instances"](c),
            "test_dcv_remote_desktop": FEATURE_DETECTORS["dcv"],
            "test_spot_instance_handling": FEATURE_DETECTORS["spot_instances"],
            "test_login_node_access": FEATURE_DETECTORS["login_nodes"],
            "test_custom_bootstrap_scripts": FEATURE_DETECTORS["custom_actions"],
            "test_cloudwatch_monitoring": FEATURE_DETECTORS["cloudwatch_logging"],
        }

        return conditions.get(test_name, lambda c: False)(config)

    def _test_basic_functionality(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Always-run basic cluster functionality test."""
        from remote_command_executor import RemoteCommandExecutor

        from tests.common.utils import run_system_analyzer

        remote_command_executor = RemoteCommandExecutor(cluster)
        scheduler_commands = scheduler_commands_factory(remote_command_executor)

        # Basic connectivity and scheduler tests
        result = scheduler_commands.submit_command("echo 'basic test'")
        job_id = scheduler_commands.assert_job_submitted(result.stdout)
        scheduler_commands.wait_job_completed(job_id)
        scheduler_commands.assert_job_succeeded(job_id)

    def _test_scheduler_operations(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test core scheduler functionality."""
        from remote_command_executor import RemoteCommandExecutor

        from tests.common.mpi_common import _test_mpi
        from tests.common.utils import fetch_instance_slots

        remote_command_executor = RemoteCommandExecutor(cluster)
        scheduler_commands = scheduler_commands_factory(remote_command_executor)

        # Get any instance type for basic MPI test
        instance_types = self._get_instance_types_from_config(config)
        if instance_types:
            slots_per_instance = fetch_instance_slots(region, instance_types[0])
            _test_mpi(
                remote_command_executor,
                slots_per_instance,
                "slurm",
                scheduler_commands,
                region,
                cluster.cfn_name,
                3,
                verify_scaling=False,
                num_computes=2,
            )

    def _test_fsx_lustre(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test FSx Lustre functionality - only runs if FSx Lustre configured."""
        from tests.storage.storage_common import check_fsx

        mount_dirs = self._get_fsx_mount_dirs(config)
        check_fsx(cluster, region, scheduler_commands_factory, mount_dirs, None)

    def _test_efa_networking(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test EFA functionality - only runs if EFA enabled."""
        from remote_command_executor import RemoteCommandExecutor

        from tests.common.mpi_common import _test_mpi
        from tests.common.utils import fetch_instance_slots

        remote_command_executor = RemoteCommandExecutor(cluster)
        scheduler_commands = scheduler_commands_factory(remote_command_executor)

        # Find EFA-enabled instance type
        efa_instance = self._get_efa_instance_type(config)
        if efa_instance:
            slots_per_instance = fetch_instance_slots(region, efa_instance)
            _test_mpi(
                remote_command_executor,
                slots_per_instance,
                "slurm",
                scheduler_commands,
                region,
                cluster.cfn_name,
                3,
                verify_scaling=True,
                num_computes=2,
            )

    def _test_gpu_workloads(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test GPU functionality - only runs if GPU instances configured."""
        from remote_command_executor import RemoteCommandExecutor

        from tests.common.nccl_common import test_nccl

        remote_command_executor = RemoteCommandExecutor(cluster)
        scheduler_commands = scheduler_commands_factory(remote_command_executor)
        test_nccl(remote_command_executor, scheduler_commands)

    def _test_dcv_remote_desktop(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test DCV functionality - only runs if DCV enabled."""
        from remote_command_executor import RemoteCommandExecutor

        remote_command_executor = RemoteCommandExecutor(cluster)
        result = remote_command_executor.run_remote_command("sudo systemctl is-active dcv-server")
        assert result.stdout.strip() == "active"

    def _test_spot_instance_handling(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test spot instance behavior - only runs if spot instances configured."""
        # Spot-specific tests would go here
        pass

    def _test_login_node_access(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test login node functionality - only runs if login nodes configured."""
        # Login node tests would go here
        pass

    def _test_custom_bootstrap_scripts(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test custom actions - only runs if custom actions configured."""
        # Custom script tests would go here
        pass

    def _test_cloudwatch_monitoring(self, cluster, region, scheduler_commands_factory, config, **kwargs):
        """Test CloudWatch logging - only runs if CloudWatch enabled."""
        from tests.cloudwatch_logging.cloudwatch_logging_boto3_utils import get_cluster_log_groups_from_boto3

        log_groups = get_cluster_log_groups_from_boto3(f"/aws/parallelcluster/{cluster.name}")
        assert len(log_groups) > 0

    # Helper methods
    def _get_instance_types_from_config(self, config: Dict) -> List[str]:
        """Extract instance types from config."""
        from framework.feature_detectors import get_configured_instance_types

        return get_configured_instance_types(config)

    def _get_fsx_mount_dirs(self, config: Dict) -> List[str]:
        """Get FSx Lustre mount directories."""
        mount_dirs = []
        for storage in config.get("SharedStorage", []):
            if storage.get("StorageType") == "FsxLustre":
                mount_dirs.append(storage.get("MountDir"))
        return mount_dirs

    def _get_efa_instance_type(self, config: Dict) -> str:
        """Get first EFA-enabled instance type."""
        queues = config.get("Scheduling", {}).get("SlurmQueues", [])
        for queue in queues:
            for compute_resource in queue.get("ComputeResources", []):
                if compute_resource.get("Efa", {}).get("Enabled", False):
                    instances = compute_resource.get("Instances", [])
                    if instances:
                        return instances[0].get("InstanceType")
        return None


# Global hybrid runner
hybrid_runner = HybridTestRunner()


# Pytest integration for stable test names with conditional execution
def pytest_runtest_setup(item):
    """Skip tests that shouldn't run based on cluster config."""
    if hasattr(item, "config_dict"):
        test_name = item.name
        if not hybrid_runner.should_run_test(test_name, item.config_dict):
            pytest.skip(f"Test {test_name} skipped - feature not configured in cluster")


# Individual test functions with stable names for historical tracking
def test_basic_cluster_functionality(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """Basic cluster functionality - always runs."""
    hybrid_runner.stable_tests["test_basic_cluster_functionality"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_scheduler_operations(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """Core scheduler operations - always runs."""
    hybrid_runner.stable_tests["test_scheduler_operations"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_fsx_lustre(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """FSx Lustre functionality - runs only if FSx Lustre configured."""
    hybrid_runner.stable_tests["test_fsx_lustre"](cluster, region, scheduler_commands_factory, cluster_config, **kwargs)


def test_efa_networking(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """EFA networking - runs only if EFA enabled."""
    hybrid_runner.stable_tests["test_efa_networking"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_gpu_workloads(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """GPU workloads - runs only if GPU instances configured."""
    hybrid_runner.stable_tests["test_gpu_workloads"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_dcv_remote_desktop(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """DCV remote desktop - runs only if DCV enabled."""
    hybrid_runner.stable_tests["test_dcv_remote_desktop"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_spot_instance_handling(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """Spot instance handling - runs only if spot instances configured."""
    hybrid_runner.stable_tests["test_spot_instance_handling"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_login_node_access(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """Login node access - runs only if login nodes configured."""
    hybrid_runner.stable_tests["test_login_node_access"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_custom_bootstrap_scripts(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """Custom bootstrap scripts - runs only if custom actions configured."""
    hybrid_runner.stable_tests["test_custom_bootstrap_scripts"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )


def test_cloudwatch_monitoring(cluster, region, scheduler_commands_factory, cluster_config, **kwargs):
    """CloudWatch monitoring - runs only if CloudWatch enabled."""
    hybrid_runner.stable_tests["test_cloudwatch_monitoring"](
        cluster, region, scheduler_commands_factory, cluster_config, **kwargs
    )
