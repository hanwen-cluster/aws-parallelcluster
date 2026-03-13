#!/usr/bin/env python3
"""
Matrix-based test approach: Run each test once with the most appropriate cluster configuration.
"""

from typing import Dict, List, Optional, Tuple

import yaml


class TestConfigurationMatrix:
    """Manages mapping between tests and optimal cluster configurations."""

    def __init__(self):
        # Define test requirements and preferences
        self.test_requirements = {
            "test_basic_cluster_functionality": {"required": [], "preferred": []},
            "test_scheduler_operations": {"required": [], "preferred": ["multiple_queues"]},
            "test_fsx_lustre": {"required": ["fsx_lustre"], "preferred": ["fsx_lustre"]},
            "test_efa_networking": {"required": ["efa"], "preferred": ["efa", "placement_group"]},
            "test_gpu_workloads": {"required": ["gpu_instances"], "preferred": ["p_series_instances", "efa"]},
            "test_dcv_remote_desktop": {"required": ["dcv"], "preferred": ["dcv", "gpu_instances"]},
            "test_spot_instance_handling": {"required": ["spot_instances"], "preferred": ["spot_instances"]},
            "test_login_node_access": {"required": ["login_nodes"], "preferred": ["login_nodes"]},
            "test_custom_bootstrap_scripts": {"required": ["custom_actions"], "preferred": ["custom_actions"]},
            "test_cloudwatch_monitoring": {"required": ["cloudwatch_logging"], "preferred": ["cloudwatch_logging"]},
        }

    def generate_test_matrix(self, available_configs: List[Dict]) -> Dict[str, str]:
        """
        Generate optimal test-to-config mapping.
        Each test runs exactly once with the best matching configuration.

        Returns: {test_name: config_path}
        """
        from framework.feature_detectors import FEATURE_DETECTORS

        # Analyze each config to determine its features
        config_features = {}
        for i, config_data in enumerate(available_configs):
            config_path = config_data["path"]
            config_content = config_data["content"]

            features = []
            for feature_name, detector_func in FEATURE_DETECTORS.items():
                if detector_func(config_content):
                    features.append(feature_name)

            config_features[config_path] = features

        # Find optimal config for each test
        test_matrix = {}
        used_configs = set()

        # First pass: assign tests that have specific requirements
        for test_name, requirements in self.test_requirements.items():
            required_features = requirements["required"]
            preferred_features = requirements["preferred"]

            if not required_features:
                # Tests with no requirements can use any config
                continue

            best_config = self._find_best_config(config_features, required_features, preferred_features, used_configs)

            if best_config:
                test_matrix[test_name] = best_config
                # Don't mark as used yet - multiple tests can share configs if needed

        # Second pass: assign tests without requirements to available configs
        for test_name, requirements in self.test_requirements.items():
            if test_name in test_matrix:
                continue

            # Use any available config, prefer unused ones
            available_configs_list = list(config_features.keys())
            if available_configs_list:
                test_matrix[test_name] = available_configs_list[0]

        return test_matrix

    def _find_best_config(
        self, config_features: Dict[str, List[str]], required: List[str], preferred: List[str], avoid_configs: set
    ) -> Optional[str]:
        """Find the best configuration for a test's requirements."""

        candidates = []

        for config_path, features in config_features.items():
            # Must have all required features
            if not all(req in features for req in required):
                continue

            # Calculate preference score
            preference_score = sum(1 for pref in preferred if pref in features)

            # Prefer unused configs
            usage_penalty = 1 if config_path in avoid_configs else 0

            candidates.append((config_path, preference_score - usage_penalty))

        if not candidates:
            return None

        # Return config with highest score
        return max(candidates, key=lambda x: x[1])[0]

    def print_matrix(self, test_matrix: Dict[str, str], config_features: Dict[str, List[str]]):
        """Print the test execution matrix."""
        print("TEST EXECUTION MATRIX")
        print("=" * 60)
        print(f"{'Test Name':<30} {'Configuration':<20} {'Features'}")
        print("-" * 60)

        for test_name, config_path in test_matrix.items():
            config_name = config_path.split("/")[-1].replace(".yaml", "")
            features = config_features.get(config_path, [])
            feature_str = ", ".join(features[:3])  # Show first 3 features
            if len(features) > 3:
                feature_str += f" +{len(features)-3} more"

            print(f"{test_name:<30} {config_name:<20} {feature_str}")


def demonstrate_matrix_approach():
    """Show how the matrix approach works with multiple configurations."""

    # Sample cluster configurations
    configs = [
        {
            "path": "configs/basic.yaml",
            "content": {
                "HeadNode": {"InstanceType": "t3.medium"},
                "Scheduling": {"SlurmQueues": [{"ComputeResources": [{"Instances": [{"InstanceType": "c5.large"}]}]}]},
            },
        },
        {
            "path": "configs/fsx_cluster.yaml",
            "content": {
                "HeadNode": {"InstanceType": "t3.medium"},
                "SharedStorage": [{"StorageType": "FsxLustre", "MountDir": "/fsx"}],
                "Scheduling": {"SlurmQueues": [{"ComputeResources": [{"Instances": [{"InstanceType": "c5.large"}]}]}]},
            },
        },
        {
            "path": "configs/hpc_gpu.yaml",
            "content": {
                "HeadNode": {"InstanceType": "m5.large", "Dcv": {"Enabled": True}},
                "LoginNodes": {"Pools": [{"InstanceType": "t3.medium", "Count": 2}]},
                "Scheduling": {
                    "SlurmQueues": [
                        {
                            "Name": "gpu-queue",
                            "ComputeResources": [
                                {
                                    "Instances": [{"InstanceType": "p3.8xlarge"}],
                                    "Efa": {"Enabled": True},
                                    "CapacityType": "SPOT",
                                }
                            ],
                            "Networking": {"PlacementGroup": {"Enabled": True}},
                        },
                        {"Name": "cpu-queue", "ComputeResources": [{"Instances": [{"InstanceType": "c5n.18xlarge"}]}]},
                    ]
                },
                "SharedStorage": [{"StorageType": "FsxLustre", "MountDir": "/fsx"}],
                "Monitoring": {"Logs": {"CloudWatch": {"Enabled": True}}},
            },
        },
    ]

    matrix = TestConfigurationMatrix()
    test_matrix = matrix.generate_test_matrix(configs)

    # Analyze features for display
    from framework.feature_detectors import FEATURE_DETECTORS

    config_features = {}
    for config_data in configs:
        config_path = config_data["path"]
        config_content = config_data["content"]
        features = [name for name, detector in FEATURE_DETECTORS.items() if detector(config_content)]
        config_features[config_path] = features

    matrix.print_matrix(test_matrix, config_features)

    print(f"\nSUMMARY:")
    print(f"• Configurations available: {len(configs)}")
    print(f"• Tests to run: {len(test_matrix)}")
    print(f"• Each test runs exactly once with optimal config")
    print(f"• No redundant FSx testing across multiple configs")

    # Show comparison
    print(f"\nCOMPARISON:")
    print(f"Old approach: Each test runs once, regardless of config relevance")
    print(f"Naive config-driven: test_fsx_lustre runs {sum(1 for c in configs if any('fsx' in str(c).lower()))} times")
    print(f"Matrix approach: test_fsx_lustre runs exactly 1 time with best FSx config")


if __name__ == "__main__":
    demonstrate_matrix_approach()
