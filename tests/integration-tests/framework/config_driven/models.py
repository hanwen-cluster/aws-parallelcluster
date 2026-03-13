# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Data models for the config-driven test framework."""

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Cluster features extracted from config YAML
# ---------------------------------------------------------------------------

@dataclass
class StorageMount:
    """Represents a single SharedStorage entry from the cluster config."""
    storage_type: str       # Ebs, Efs, FsxLustre, FsxOntap, FsxOpenZfs
    mount_dir: str
    name: str
    volume_size: Optional[int] = None
    raid_type: Optional[int] = None       # 0 or 1
    raid_devices: Optional[int] = None
    encrypted: Optional[bool] = None
    file_system_id: Optional[str] = None  # for existing FS


@dataclass
class QueueInfo:
    """Represents a single Slurm queue from the cluster config."""
    name: str
    instance_types: List[str] = field(default_factory=list)
    efa_enabled: bool = False
    placement_group_enabled: bool = False
    capacity_type: str = "ONDEMAND"
    min_count: int = 0
    max_count: int = 0


@dataclass
class ClusterFeatures:
    """Features extracted from a parsed ParallelCluster config YAML.

    This is a pure data object — no AWS calls needed to build it.
    """
    scheduler: str = "slurm"
    head_node_instance_type: str = ""
    queues: List[QueueInfo] = field(default_factory=list)
    storage_mounts: List[StorageMount] = field(default_factory=list)

    # Convenience booleans derived during analysis
    has_efa: bool = False
    has_placement_group: bool = False
    has_dcv: bool = False
    has_login_nodes: bool = False
    has_ebs: bool = False
    has_efs: bool = False
    has_fsx_lustre: bool = False
    has_fsx_ontap: bool = False
    has_fsx_openzfs: bool = False
    has_raid: bool = False
    has_multi_queue: bool = False

    @property
    def compute_instance_types(self) -> List[str]:
        types = []
        for q in self.queues:
            types.extend(q.instance_types)
        return list(set(types))


# ---------------------------------------------------------------------------
# Runtime context (requires AWS API calls)
# ---------------------------------------------------------------------------

@dataclass
class InstanceInfo:
    """Capabilities of a single EC2 instance type."""
    efa_supported: bool = False
    gpu_manufacturer: Optional[str] = None
    gpu_count: int = 0
    architecture: str = "x86_64"
    vcpus: int = 0
    network_interfaces: int = 1


@dataclass
class RuntimeContext:
    """Runtime information that supplements ClusterFeatures with live AWS data."""
    region: str = ""
    os: str = ""
    architecture: str = "x86_64"
    instance_info: Dict[str, InstanceInfo] = field(default_factory=dict)

    def supports_efa(self, instance_type: str) -> bool:
        info = self.instance_info.get(instance_type)
        return info.efa_supported if info else False

    def is_gpu_instance(self, instance_type: str) -> bool:
        info = self.instance_info.get(instance_type)
        return (info.gpu_count > 0) if info else False

    def is_nvidia_gpu(self, instance_type: str) -> bool:
        info = self.instance_info.get(instance_type)
        return info is not None and info.gpu_manufacturer == "NVIDIA"

    def is_p_instance(self, instance_type: str) -> bool:
        return instance_type.startswith("p")

    def get_slots(self, instance_type: str) -> int:
        info = self.instance_info.get(instance_type)
        return info.vcpus if info else 0


# ---------------------------------------------------------------------------
# Check lifecycle
# ---------------------------------------------------------------------------

class CheckStatus(enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass
class CheckResult:
    """Outcome of a single check execution."""
    name: str
    status: CheckStatus
    duration_seconds: float = 0.0
    message: str = ""
    error: Optional[Exception] = None


class Phase(enum.IntEnum):
    """Execution phases — checks run in phase order."""
    INFRASTRUCTURE = 1
    FUNCTIONAL = 2
    PERFORMANCE = 3
    TEARDOWN = 4


@dataclass
class CheckContext:
    """Everything a check function needs to do its work."""
    cluster: Any                    # clusters_factory Cluster object
    remote_command_executor: Any    # RemoteCommandExecutor
    scheduler_commands: Any         # SlurmCommands / AWSBatchCommands
    features: ClusterFeatures = field(default_factory=ClusterFeatures)
    runtime: RuntimeContext = field(default_factory=RuntimeContext)
    test_datadir: Optional[Any] = None
    request: Optional[Any] = None   # pytest request for options


@dataclass
class Check:
    """A single validation check that can be selected and executed by the framework."""
    name: str
    category: str
    phase: Phase
    condition: Callable[[ClusterFeatures, RuntimeContext], bool]
    run: Callable[[CheckContext], None]
    depends_on: List[str] = field(default_factory=list)
