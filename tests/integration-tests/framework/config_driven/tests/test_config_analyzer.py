# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Unit tests for config_analyzer — no AWS calls needed."""

import pytest

from framework.config_driven.config_analyzer import analyze_config


class TestAnalyzeConfig:
    """Test that analyze_config correctly extracts features from config dicts."""

    def test_basic_slurm_cluster(self):
        config = {
            "Scheduling": {"Scheduler": "slurm", "SlurmQueues": [
                {"Name": "compute", "ComputeResources": [
                    {"Name": "cr1", "InstanceType": "c5.xlarge", "MaxCount": 4}
                ], "Networking": {}}
            ]},
            "HeadNode": {"InstanceType": "c5.xlarge"},
            "SharedStorage": [{"StorageType": "Ebs", "MountDir": "/shared", "Name": "ebs1"}],
        }
        features = analyze_config(config)
        assert features.scheduler == "slurm"
        assert features.head_node_instance_type == "c5.xlarge"
        assert len(features.queues) == 1
        assert features.queues[0].name == "compute"
        assert "c5.xlarge" in features.compute_instance_types
        assert features.has_ebs is True
        assert features.has_efa is False
        assert features.has_dcv is False

    def test_efa_enabled(self):
        config = {
            "Scheduling": {"Scheduler": "slurm", "SlurmQueues": [
                {"Name": "efa-q", "ComputeResources": [
                    {"Name": "cr1", "InstanceType": "c5n.18xlarge", "MaxCount": 2,
                     "Efa": {"Enabled": True}}
                ], "Networking": {"PlacementGroup": {"Enabled": True}}}
            ]},
            "HeadNode": {"InstanceType": "c5.xlarge"},
        }
        features = analyze_config(config)
        assert features.has_efa is True
        assert features.has_placement_group is True
        assert features.queues[0].efa_enabled is True

    def test_multiple_storage_types(self):
        config = {
            "Scheduling": {"Scheduler": "slurm", "SlurmQueues": []},
            "HeadNode": {"InstanceType": "c5.xlarge"},
            "SharedStorage": [
                {"StorageType": "Ebs", "MountDir": "/ebs", "Name": "ebs1"},
                {"StorageType": "Efs", "MountDir": "/efs", "Name": "efs1"},
                {"StorageType": "FsxLustre", "MountDir": "/fsx", "Name": "fsx1"},
            ],
        }
        features = analyze_config(config)
        assert features.has_ebs is True
        assert features.has_efs is True
        assert features.has_fsx_lustre is True
        assert len(features.storage_mounts) == 3

    def test_dcv_detected(self):
        config = {
            "Scheduling": {"Scheduler": "slurm", "SlurmQueues": []},
            "HeadNode": {"InstanceType": "g4dn.xlarge", "Dcv": {"Enabled": True}},
        }
        features = analyze_config(config)
        assert features.has_dcv is True

    def test_login_nodes_detected(self):
        config = {
            "Scheduling": {"Scheduler": "slurm", "SlurmQueues": []},
            "HeadNode": {"InstanceType": "c5.xlarge"},
            "LoginNodes": {"Pools": [{"Name": "login", "InstanceType": "c5.xlarge"}]},
        }
        features = analyze_config(config)
        assert features.has_login_nodes is True

    def test_multi_queue(self):
        config = {
            "Scheduling": {"Scheduler": "slurm", "SlurmQueues": [
                {"Name": "q1", "ComputeResources": [
                    {"Name": "cr1", "InstanceType": "c5.xlarge", "MaxCount": 2}
                ], "Networking": {}},
                {"Name": "q2", "ComputeResources": [
                    {"Name": "cr2", "InstanceType": "m5.xlarge", "MaxCount": 2}
                ], "Networking": {}},
            ]},
            "HeadNode": {"InstanceType": "c5.xlarge"},
        }
        features = analyze_config(config)
        assert features.has_multi_queue is True
        assert len(features.queues) == 2
