# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Build RuntimeContext by querying EC2 for instance capabilities."""

import logging

import boto3

from framework.config_driven.models import ClusterFeatures, InstanceInfo, RuntimeContext


def build_runtime_context(
    region: str,
    os_name: str,
    architecture: str,
    features: ClusterFeatures,
) -> RuntimeContext:
    """Query AWS APIs to build a RuntimeContext for the given cluster features.

    Args:
        region: AWS region.
        os_name: OS identifier (e.g. "alinux2023").
        architecture: "x86_64" or "arm64".
        features: Parsed cluster features (used to know which instance types to query).

    Returns:
        Populated RuntimeContext.
    """
    ctx = RuntimeContext(region=region, os=os_name, architecture=architecture)

    # Collect all instance types we need info about
    instance_types = set(features.compute_instance_types)
    if features.head_node_instance_type:
        instance_types.add(features.head_node_instance_type)

    if not instance_types:
        return ctx

    ec2 = boto3.client("ec2", region_name=region)
    paginator = ec2.get_paginator("describe_instance_types")

    types_list = list(instance_types)
    # DescribeInstanceTypes has a limit of 100 per call
    batch_size = 100
    for i in range(0, len(types_list), batch_size):
        batch = types_list[i:i + batch_size]
        for page in paginator.paginate(InstanceTypes=batch):
            for it in page["InstanceTypes"]:
                info = _parse_instance_type(it)
                ctx.instance_info[it["InstanceType"]] = info
                logging.info(
                    "Instance %s: efa=%s, gpu=%s(%s), vcpus=%d",
                    it["InstanceType"], info.efa_supported,
                    info.gpu_manufacturer, info.gpu_count, info.vcpus,
                )

    return ctx


def _parse_instance_type(it: dict) -> InstanceInfo:
    """Parse a single DescribeInstanceTypes response entry."""
    info = InstanceInfo()
    info.efa_supported = it.get("NetworkInfo", {}).get("EfaSupported", False)
    info.network_interfaces = it.get("NetworkInfo", {}).get("MaximumNetworkInterfaces", 1)

    gpu_info = it.get("GpuInfo", {})
    gpus = gpu_info.get("Gpus", [])
    if gpus:
        info.gpu_manufacturer = gpus[0].get("Manufacturer", "")
        info.gpu_count = gpus[0].get("Count", 0)

    info.vcpus = it.get("VCpuInfo", {}).get("DefaultVCpus", 0)

    archs = it.get("ProcessorInfo", {}).get("SupportedArchitectures", [])
    if archs:
        info.architecture = archs[0]

    return info
