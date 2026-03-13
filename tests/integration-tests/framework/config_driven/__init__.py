# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
# See the License for the specific language governing permissions and limitations under the License.

"""Config-driven integration test framework.

Instead of writing per-feature test functions, this framework:
1. Parses a ParallelCluster config YAML to extract which features are present.
2. Queries runtime context (instance capabilities, region, OS) from AWS APIs.
3. Selects applicable validation checks from a registry.
4. Executes checks in phase order with structured logging and reporting.
"""
