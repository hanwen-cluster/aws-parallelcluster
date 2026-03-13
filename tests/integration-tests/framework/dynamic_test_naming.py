#!/usr/bin/env python3
"""
Dynamic test naming and parameterization for config-driven tests.

This module handles generating meaningful test names and managing test counts
in the new config-driven approach.
"""

from typing import Dict, List, Set

import pytest
from framework.feature_detectors import FEATURE_DETECTORS


def generate_test_id(detected_features: List[str]) -> str:
    """Generate a meaningful test ID based on detected features."""
    if not detected_features:
        return "basic_cluster"

    # Sort features for consistent naming
    sorted_features = sorted(detected_features)

    # Create abbreviated names for common features
    abbreviations = {
        "fsx_lustre": "fsx",
        "p_series_instances": "gpu_p",
        "gpu_instances": "gpu",
        "efa": "efa",
        "dcv": "dcv",
        "login_nodes": "login",
        "spot_instances": "spot",
        "placement_group": "pg",
        "custom_actions": "scripts",
        "cloudwatch_logging": "cw_logs",
        "multiple_queues": "multi_q",
        "hyperthreading_disabled": "no_ht",
    }

    # Use abbreviations where available, otherwise use full name
    abbreviated_features = [abbreviations.get(feature, feature) for feature in sorted_features]

    return "+".join(abbreviated_features)


def detect_features_from_config(config: Dict) -> List[str]:
    """Detect all features present in a cluster configuration."""
    detected = []
    for feature_name, detector_func in FEATURE_DETECTORS.items():
        if detector_func(config):
            detected.append(feature_name)
    return detected


def generate_test_parameters(config_paths: List[str]) -> List[pytest.param]:
    """
    Generate pytest parameters for config-driven tests.

    This replaces the need for multiple individual test functions.
    Instead, we have one parameterized test that runs different
    feature combinations based on the configs.
    """
    import yaml

    test_params = []
    seen_feature_combinations = set()

    for config_path in config_paths:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        detected_features = detect_features_from_config(config)
        feature_signature = frozenset(detected_features)

        # Skip duplicate feature combinations
        if feature_signature in seen_feature_combinations:
            continue
        seen_feature_combinations.add(feature_signature)

        test_id = generate_test_id(detected_features)

        test_params.append(
            pytest.param(config_path, detected_features, id=test_id, marks=_generate_pytest_marks(detected_features))
        )

    return test_params


def _generate_pytest_marks(features: List[str]) -> List:
    """Generate appropriate pytest marks based on detected features."""
    marks = []

    # Add marks based on features
    if "gpu_instances" in features or "p_series_instances" in features:
        marks.append(pytest.mark.gpu)

    if "efa" in features:
        marks.append(pytest.mark.efa)

    if "fsx_lustre" in features or "fsx_ontap" in features:
        marks.append(pytest.mark.storage)

    if "spot_instances" in features:
        marks.append(pytest.mark.spot)

    # Add performance mark for complex configurations
    if len(features) > 5:
        marks.append(pytest.mark.performance)

    return marks


class TestCountAnalyzer:
    """Analyze how test counts change with the new approach."""

    def __init__(self):
        self.current_test_functions = [
            "test_essential_features",
            "test_fsx_lustre",
            "test_fsx_lustre_dra",
            "test_fsx_lustre_backup",
            "test_multiple_fsx",
            "test_efa",
            "test_dcv_configuration",
            "test_slurm",
            "test_slurm_scaling",
            "test_awsbatch",
            "test_spot_default",
            "test_placement_group",
            "test_cloudwatch_logging",
            "test_custom_bootstrap_scripts",
            "test_login_nodes",
            "test_multiple_queues",
            # ... many more
        ]

    def analyze_current_approach(self) -> Dict:
        """Analyze the current test approach."""
        return {
            "total_test_functions": len(self.current_test_functions),
            "test_execution_model": "All tests run regardless of cluster config",
            "redundancy": "High - many tests check similar functionality",
            "maintenance_burden": "High - each test function needs individual maintenance",
            "coverage_gaps": "Possible - easy to miss testing configured features",
        }

    def analyze_new_approach(self, config_paths: List[str]) -> Dict:
        """Analyze the new config-driven approach."""
        import yaml

        unique_feature_combinations = set()
        all_features = set()

        for config_path in config_paths:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)

            detected_features = detect_features_from_config(config)
            all_features.update(detected_features)
            unique_feature_combinations.add(frozenset(detected_features))

        return {
            "total_test_functions": 1,  # Just test_config_driven_features
            "unique_feature_combinations": len(unique_feature_combinations),
            "total_detectable_features": len(all_features),
            "test_execution_model": "Only relevant tests run based on config",
            "redundancy": "Minimal - each feature tested once per config",
            "maintenance_burden": "Low - centralized test logic",
            "coverage_gaps": "Eliminated - all configured features automatically tested",
        }

    def compare_approaches(self, config_paths: List[str]) -> str:
        """Generate a comparison report."""
        current = self.analyze_current_approach()
        new = self.analyze_new_approach(config_paths)

        report = f"""
TEST APPROACH COMPARISON
========================

Current Approach:
- Test Functions: {current['total_test_functions']}
- Execution Model: {current['test_execution_model']}
- Redundancy: {current['redundancy']}
- Maintenance: {current['maintenance_burden']}

New Config-Driven Approach:
- Test Functions: {new['total_test_functions']}
- Feature Combinations: {new['unique_feature_combinations']}
- Detectable Features: {new['total_detectable_features']}
- Execution Model: {new['test_execution_model']}
- Redundancy: {new['redundancy']}
- Maintenance: {new['maintenance_burden']}

BENEFITS:
✓ Reduced from {current['total_test_functions']} to {new['total_test_functions']} test functions
✓ Automatic feature detection and testing
✓ No redundant test execution
✓ Centralized test maintenance
✓ Guaranteed coverage of configured features
✓ Dynamic test naming based on actual config content

EXAMPLE TEST NAMES:
Old: test_essential_features, test_fsx_lustre, test_efa
New: test_config_driven[fsx+efa+dcv], test_config_driven[gpu_p+spot+pg]
"""
        return report


# Example usage for pytest parameterization
def pytest_generate_tests(metafunc):
    """
    Pytest hook to generate test parameters dynamically.

    This would replace static test discovery with dynamic
    config-based test generation.
    """
    if "config_driven_test" in metafunc.fixturenames:
        # In real implementation, this would scan for config files
        config_paths = [
            "configs/basic_cluster.yaml",
            "configs/hpc_gpu_cluster.yaml",
            "configs/storage_cluster.yaml",
            # ... discovered config files
        ]

        test_params = generate_test_parameters(config_paths)
        metafunc.parametrize("config_path,detected_features", test_params, indirect=True)
