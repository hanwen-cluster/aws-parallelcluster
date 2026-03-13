#!/usr/bin/env python3
"""
Feature detection functions for config-driven testing.

This module contains functions that analyze cluster configurations to determine
which features are enabled and should be tested.
"""

from typing import Dict, List


def has_fsx_lustre(config: Dict) -> bool:
    """Check if cluster config contains FSx Lustre storage."""
    shared_storage = config.get("SharedStorage", [])
    return any(storage.get("StorageType") == "FsxLustre" for storage in shared_storage)


def has_fsx_ontap(config: Dict) -> bool:
    """Check if cluster config contains FSx ONTAP storage."""
    shared_storage = config.get("SharedStorage", [])
    return any(storage.get("StorageType") == "FsxOntap" for storage in shared_storage)


def has_efs(config: Dict) -> bool:
    """Check if cluster config contains EFS storage."""
    shared_storage = config.get("SharedStorage", [])
    return any(storage.get("StorageType") == "Efs" for storage in shared_storage)


def has_p_series_instances(config: Dict) -> bool:
    """Check if cluster config contains P-series instances."""
    return _has_instance_type_pattern(config, lambda t: t.startswith("p"))


def has_gpu_instances(config: Dict) -> bool:
    """Check if cluster config contains GPU instances."""
    gpu_families = ["p", "g", "trn", "inf"]
    return _has_instance_type_pattern(config, lambda t: any(t.startswith(f) for f in gpu_families))


def has_efa_enabled(config: Dict) -> bool:
    """Check if cluster config has EFA enabled."""
    queues = _get_slurm_queues(config)
    for queue in queues:
        for compute_resource in queue.get("ComputeResources", []):
            if compute_resource.get("Efa", {}).get("Enabled", False):
                return True
    return False


def has_dcv_enabled(config: Dict) -> bool:
    """Check if cluster config has DCV enabled."""
    return config.get("HeadNode", {}).get("Dcv", {}).get("Enabled", False)


def has_login_nodes(config: Dict) -> bool:
    """Check if cluster config has login nodes."""
    login_nodes = config.get("LoginNodes", {}).get("Pools", [])
    return len(login_nodes) > 0


def has_spot_instances(config: Dict) -> bool:
    """Check if cluster config uses spot instances."""
    queues = _get_slurm_queues(config)
    for queue in queues:
        for compute_resource in queue.get("ComputeResources", []):
            capacity_type = compute_resource.get("CapacityType", "ONDEMAND")
            if capacity_type == "SPOT":
                return True
    return False


def has_placement_group(config: Dict) -> bool:
    """Check if cluster config uses placement groups."""
    queues = _get_slurm_queues(config)
    for queue in queues:
        networking = queue.get("Networking", {})
        if networking.get("PlacementGroup", {}).get("Enabled", False):
            return True
    return False


def has_custom_actions(config: Dict) -> bool:
    """Check if cluster config has custom actions (pre/post install scripts)."""
    # Check head node custom actions
    head_node_actions = config.get("HeadNode", {}).get("CustomActions", {})
    if head_node_actions.get("OnNodeStart") or head_node_actions.get("OnNodeConfigured"):
        return True

    # Check queue-level custom actions
    queues = _get_slurm_queues(config)
    for queue in queues:
        queue_actions = queue.get("CustomActions", {})
        if queue_actions.get("OnNodeStart") or queue_actions.get("OnNodeConfigured"):
            return True

    return False


def has_cloudwatch_logging(config: Dict) -> bool:
    """Check if cluster config has CloudWatch logging enabled."""
    monitoring = config.get("Monitoring", {})
    logs = monitoring.get("Logs", {})
    cloudwatch = logs.get("CloudWatch", {})
    return cloudwatch.get("Enabled", False)


def has_multiple_queues(config: Dict) -> bool:
    """Check if cluster config has multiple queues."""
    queues = _get_slurm_queues(config)
    return len(queues) > 1


def has_multiple_compute_resources(config: Dict) -> bool:
    """Check if any queue has multiple compute resources."""
    queues = _get_slurm_queues(config)
    for queue in queues:
        compute_resources = queue.get("ComputeResources", [])
        if len(compute_resources) > 1:
            return True
    return False


def has_hyperthreading_disabled(config: Dict) -> bool:
    """Check if any compute resource has hyperthreading disabled."""
    queues = _get_slurm_queues(config)
    for queue in queues:
        for compute_resource in queue.get("ComputeResources", []):
            if compute_resource.get("DisableSimultaneousMultithreading", False):
                return True
    return False


def get_configured_instance_types(config: Dict) -> List[str]:
    """Get all instance types configured in the cluster."""
    instance_types = set()

    # Head node instance type
    head_node_type = config.get("HeadNode", {}).get("InstanceType")
    if head_node_type:
        instance_types.add(head_node_type)

    # Compute instance types
    queues = _get_slurm_queues(config)
    for queue in queues:
        for compute_resource in queue.get("ComputeResources", []):
            instances = compute_resource.get("Instances", [])
            for instance in instances:
                instance_type = instance.get("InstanceType")
                if instance_type:
                    instance_types.add(instance_type)

    # Login node instance types
    login_pools = config.get("LoginNodes", {}).get("Pools", [])
    for pool in login_pools:
        instance_type = pool.get("InstanceType")
        if instance_type:
            instance_types.add(instance_type)

    return list(instance_types)


def get_storage_mount_points(config: Dict) -> Dict[str, List[str]]:
    """Get mount points organized by storage type."""
    mount_points = {}
    shared_storage = config.get("SharedStorage", [])

    for storage in shared_storage:
        storage_type = storage.get("StorageType")
        mount_dir = storage.get("MountDir")
        if storage_type and mount_dir:
            if storage_type not in mount_points:
                mount_points[storage_type] = []
            mount_points[storage_type].append(mount_dir)

    return mount_points


# Helper functions
def _get_slurm_queues(config: Dict) -> List[Dict]:
    """Get SlurmQueues from config, handling both Slurm and AwsBatch schedulers."""
    scheduling = config.get("Scheduling", {})
    return scheduling.get("SlurmQueues", scheduling.get("AwsBatchQueues", []))


def _has_instance_type_pattern(config: Dict, pattern_func) -> bool:
    """Check if any configured instance type matches the given pattern function."""
    instance_types = get_configured_instance_types(config)
    return any(pattern_func(instance_type) for instance_type in instance_types)


# Feature detection registry
FEATURE_DETECTORS = {
    "fsx_lustre": has_fsx_lustre,
    "fsx_ontap": has_fsx_ontap,
    "efs": has_efs,
    "p_series_instances": has_p_series_instances,
    "gpu_instances": has_gpu_instances,
    "efa": has_efa_enabled,
    "dcv": has_dcv_enabled,
    "login_nodes": has_login_nodes,
    "spot_instances": has_spot_instances,
    "placement_group": has_placement_group,
    "custom_actions": has_custom_actions,
    "cloudwatch_logging": has_cloudwatch_logging,
    "multiple_queues": has_multiple_queues,
    "multiple_compute_resources": has_multiple_compute_resources,
    "hyperthreading_disabled": has_hyperthreading_disabled,
}
