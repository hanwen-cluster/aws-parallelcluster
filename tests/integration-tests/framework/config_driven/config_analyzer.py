# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Analyze a rendered ParallelCluster config YAML and extract ClusterFeatures."""

import logging
from typing import Any, Dict

from framework.config_driven.models import ClusterFeatures, QueueInfo, StorageMount


def analyze_config(config: Dict[str, Any]) -> ClusterFeatures:
    """Parse a rendered pcluster config dict into a ClusterFeatures object.

    Args:
        config: The parsed YAML dict (already rendered, no Jinja placeholders).

    Returns:
        ClusterFeatures with all flags and structured data populated.
    """
    features = ClusterFeatures()

    # -- Scheduler --
    scheduling = config.get("Scheduling", {})
    features.scheduler = scheduling.get("Scheduler", "slurm")

    # -- Head node --
    head_node = config.get("HeadNode", {})
    features.head_node_instance_type = head_node.get("InstanceType", "")

    # -- DCV --
    features.has_dcv = "Dcv" in head_node

    # -- Login nodes --
    features.has_login_nodes = "LoginNodes" in config

    # -- Queues (Slurm) --
    slurm_queues = scheduling.get("SlurmQueues", [])
    for q in slurm_queues:
        queue = _parse_queue(q)
        features.queues.append(queue)
        if queue.efa_enabled:
            features.has_efa = True
        if queue.placement_group_enabled:
            features.has_placement_group = True

    features.has_multi_queue = len(features.queues) > 1

    # -- Shared storage --
    for storage_entry in config.get("SharedStorage", []):
        mount = _parse_storage(storage_entry)
        features.storage_mounts.append(mount)
        _set_storage_flags(features, mount)

    logging.info("Config analysis complete: scheduler=%s, queues=%d, storage=%d, efa=%s, dcv=%s",
                 features.scheduler, len(features.queues), len(features.storage_mounts),
                 features.has_efa, features.has_dcv)
    return features


def _parse_queue(q: Dict[str, Any]) -> QueueInfo:
    """Parse a single SlurmQueues entry."""
    queue = QueueInfo(name=q.get("Name", ""))

    networking = q.get("Networking", {})
    pg = networking.get("PlacementGroup", {})
    queue.placement_group_enabled = pg.get("Enabled", False)
    queue.capacity_type = q.get("CapacityType", "ONDEMAND")

    for cr in q.get("ComputeResources", []):
        instance_type = cr.get("InstanceType")
        if instance_type:
            queue.instance_types.append(instance_type)
        # Also handle InstanceTypeList for flexible instance types
        for it in cr.get("Instances", []):
            if it.get("InstanceType"):
                queue.instance_types.append(it["InstanceType"])

        efa = cr.get("Efa", {})
        if efa.get("Enabled", False):
            queue.efa_enabled = True

        queue.min_count = max(queue.min_count, cr.get("MinCount", 0))
        queue.max_count = max(queue.max_count, cr.get("MaxCount", 0))

    return queue


def _parse_storage(entry: Dict[str, Any]) -> StorageMount:
    """Parse a single SharedStorage entry."""
    storage_type = entry.get("StorageType", "Ebs")
    mount = StorageMount(
        storage_type=storage_type,
        mount_dir=entry.get("MountDir", "/shared"),
        name=entry.get("Name", ""),
    )

    if storage_type == "Ebs":
        ebs = entry.get("EbsSettings", {})
        mount.volume_size = ebs.get("Size", ebs.get("VolumeSize"))
        raid = ebs.get("Raid", {})
        if raid:
            mount.raid_type = raid.get("Type")
            mount.raid_devices = raid.get("NumberOfVolumes")
        mount.encrypted = ebs.get("Encrypted", False)

    elif storage_type == "Efs":
        efs = entry.get("EfsSettings", {})
        mount.encrypted = efs.get("Encrypted", False)
        mount.file_system_id = efs.get("FileSystemId")

    elif storage_type == "FsxLustre":
        fsx = entry.get("FsxLustreSettings", {})
        mount.file_system_id = fsx.get("FileSystemId")

    return mount


def _set_storage_flags(features: ClusterFeatures, mount: StorageMount):
    """Set convenience boolean flags on features based on storage type."""
    type_map = {
        "Ebs": "has_ebs",
        "Efs": "has_efs",
        "FsxLustre": "has_fsx_lustre",
        "FsxOntap": "has_fsx_ontap",
        "FsxOpenZfs": "has_fsx_openzfs",
    }
    attr = type_map.get(mount.storage_type)
    if attr:
        setattr(features, attr, True)
    if mount.raid_type is not None:
        features.has_raid = True
