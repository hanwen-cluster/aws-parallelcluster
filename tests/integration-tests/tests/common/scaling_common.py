# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import datetime
import json
import logging
import os
import pathlib
import time

import boto3
import yaml
from framework.metrics_publisher import Metric, MetricsPublisher
from pykwalify.core import Core
from remote_command_executor import RemoteCommandExecutor
from retrying import RetryError, retry
from time_utils import seconds
from utils import get_compute_nodes_instance_count

SCALING_COMMON_DATADIR = pathlib.Path(__file__).parent / "scaling"


def validate_and_get_scaling_test_config(scaling_test_config_file):
    """Get and validate the scaling test parameters"""
    scaling_test_schema = str(SCALING_COMMON_DATADIR / "scaling_test_config_schema.yaml")
    logging.info("Parsing scaling test config file: %s", scaling_test_config_file)
    with open(scaling_test_config_file) as file:
        scaling_test_config = yaml.safe_load(file)
        logging.info(scaling_test_config)

        # Validate scaling test config file against defined schema
        logging.info("Validating config file against the schema")
        try:
            c = Core(source_data=scaling_test_config, schema_files=[scaling_test_schema])
            c.validate(raise_exception=True)
        except Exception as e:
            logging.error("Failed when validating schema: %s", e)
            logging.info("Dumping rendered template:\n%s", yaml.dump(scaling_test_config, default_flow_style=False))
            raise

    return scaling_test_config


def retry_if_scaling_target_not_reached(
    ec2_capacity_time_series,
    compute_nodes_time_series,
    target_cluster_size,
    use_ec2_limit=True,  # Stop monitoring after all EC2 instances have been launched
    use_compute_nodes_limit=True,  # Stop monitoring after all nodes have joined the cluster
):
    # Return True if we should retry, which is when the target cluster size
    # (either EC2 or scheduler compute nodes) is not reached yet
    return (
        (use_ec2_limit and max(ec2_capacity_time_series) != target_cluster_size)
        or (use_compute_nodes_limit and max(compute_nodes_time_series) != target_cluster_size)
        or (use_ec2_limit and max(ec2_capacity_time_series) == 0)
        or (use_compute_nodes_limit and max(compute_nodes_time_series) == 0)
    )


def _check_no_node_log_exists_for_ip_address(path, ip_address):
    for file_name in os.listdir(path):
        if file_name.startswith(ip_address):
            return False
    return True


def _sort_instances_by_launch_time(describe_instances_page_iterator):
    instances = []
    for page in describe_instances_page_iterator:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                instances.append(instance)
    instances.sort(key=lambda inst: inst["LaunchTime"])
    return instances


def get_bootstrap_errors(remote_command_executor: RemoteCommandExecutor, cluster_name, output_dir, region):
    logging.info("Checking for bootstrap errors...")
    remote_command_executor.run_remote_script(script_file=str(SCALING_COMMON_DATADIR / "get_bootstrap_errors.sh"))
    ip_addresses_with_bootstrap_errors = remote_command_executor.run_remote_command(
        command="cat $HOME/bootstrap_errors.txt"
    ).stdout

    path = os.path.join(output_dir, "bootstrap_errors")
    os.makedirs(path, exist_ok=True)

    client = boto3.client("ec2", region_name=region)
    for ip_address in ip_addresses_with_bootstrap_errors.splitlines():
        # Since the same cluster is re-used for multiple scale up tests, the script may find the same bootstrap error
        # multiple times and then get the wrong instance logs since the IP address would be attached to a new instance.
        # Therefore, only write the compute node logs for the IP address if the file doesn't exist yet.
        if _check_no_node_log_exists_for_ip_address(path, ip_address):
            try:
                logging.warning(f"Compute node with IP {ip_address} had bootstrap errors. Getting instance id...")
                # Get the latest launched instance with the IP address since the most recent one should have the error
                paginator = client.get_paginator("describe_instances")
                instance_id = _sort_instances_by_launch_time(
                    paginator.paginate(Filters=[{"Name": "private-ip-address", "Values": [ip_address]}])
                )[-1]["InstanceId"]
                logging.warning(f"Instance {instance_id} had bootstrap errors. Check the test outputs for details.")
                compute_node_log = client.get_console_output(InstanceId=instance_id, Latest=True)["Output"]
                with open(os.path.join(path, f"{ip_address}-{cluster_name}-{instance_id}-{region}-log.txt"), "w") as f:
                    f.write(compute_node_log)
            except IndexError:
                # If the instance with the IP address can't be found, continue to get any other bootstrap errors
                logging.warning("Couldn't find instance with IP %s but could have a bootstrap error.", ip_address)
            except Exception:
                logging.error("Error when retrieving the compute node logs for instance with ip address %s", ip_address)
                raise


def get_scaling_metrics(
    remote_command_executor: RemoteCommandExecutor,
    region,
    cluster_name,
    max_monitoring_time,
    target_cluster_size=0,
    publish_metrics=False,
):
    ec2_capacity_time_series = []
    compute_nodes_time_series = []
    timestamps = []
    metrics_pub = MetricsPublisher(region=region)

    @retry(
        # Retry until EC2 and Scheduler capacities scale to specified target
        retry_on_result=lambda _: retry_if_scaling_target_not_reached(
            ec2_capacity_time_series, compute_nodes_time_series, target_cluster_size
        ),
        wait_fixed=seconds(1),
        stop_max_delay=max_monitoring_time,
    )
    def _collect_metrics():
        remote_command_executor.run_remote_script(
            script_file=str(SCALING_COMMON_DATADIR / "collect_metrics_on_headnode.sh")
        )
        headnode_metrics = json.loads(
            remote_command_executor.run_remote_command(command="cat $HOME/scaling_metrics.json").stdout
        )
        logging.info(f"Metrics from HeadNode: {headnode_metrics}")
        compute_node_count = int(headnode_metrics.get("NodeCount"))
        ec2_capacity = get_compute_nodes_instance_count(cluster_name, region)
        if publish_metrics:
            # Use the cluster name as a dimension for each scaling metric
            pending_jobs_count = int(headnode_metrics.get("PendingJobsCount"))
            running_jobs_count = int(headnode_metrics.get("RunningJobsCount"))
            scaling_metrics = [
                Metric(name="ComputeNodesCount", value=compute_node_count, unit="Count"),
                Metric(name="EC2NodesCount", value=ec2_capacity, unit="Count"),
                Metric(name="PendingJobsCount", value=pending_jobs_count, unit="Count"),
                Metric(name="RunningJobsCount", value=running_jobs_count, unit="Count"),
            ]
            for metric in scaling_metrics:
                metric.dimensions = [{"Name": "ClusterName", "Value": cluster_name}]

            logging.info(f"Publishing metrics: {scaling_metrics}")
            metrics_pub.publish_metrics_to_cloudwatch(
                namespace="ParallelCluster/ScalingStressTest",
                metrics=scaling_metrics,
            )

        # add values only if there is a transition.
        if (
            len(ec2_capacity_time_series) == 0
            or ec2_capacity_time_series[-1] != ec2_capacity
            or compute_nodes_time_series[-1] != compute_node_count
        ):
            ec2_capacity_time_series.append(ec2_capacity)
            compute_nodes_time_series.append(compute_node_count)
            timestamps.append(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())

    try:
        _collect_metrics()
    except RetryError:
        # ignoring this error in order to perform assertions on the collected data.
        pass

    end_time = datetime.datetime.now(tz=datetime.timezone.utc)
    logging.info(
        "Monitoring completed: %s, %s, %s",
        "ec2_capacity_time_series [" + " ".join(map(str, ec2_capacity_time_series)) + "]",
        "compute_nodes_time_series [" + " ".join(map(str, compute_nodes_time_series)) + "]",
        "timestamps [" + " ".join(map(str, timestamps)) + "]",
    )

    logging.info("Sleeping for 3 minutes to wait for the metrics to propagate...")
    time.sleep(180)

    return ec2_capacity_time_series, compute_nodes_time_series, timestamps, end_time


def get_compute_nodes_allocation(
    scheduler_commands,
    region,
    stack_name,
    max_monitoring_time,
):
    """
    Watch periodically the number of compute nodes in the cluster.

    :return: (ec2_capacity_time_series, compute_nodes_time_series, timestamps): three lists describing
        the variation over time in the number of compute nodes and the timestamp when these fluctuations occurred.
        ec2_capacity_time_series describes the variation in the desired ec2 capacity. compute_nodes_time_series
        describes the variation in the number of compute nodes seen by the scheduler. timestamps describes the
        time since epoch when the variations occurred.
    """
    ec2_capacity_time_series = []
    compute_nodes_time_series = []
    timestamps = []

    @retry(
        # Retry until EC2 and Scheduler capacities scale down to 0
        # Also make sure cluster scaled up before scaling down
        retry_on_result=lambda _: retry_if_scaling_target_not_reached(
            ec2_capacity_time_series, compute_nodes_time_series, 0
        ),
        wait_fixed=seconds(20),
        stop_max_delay=max_monitoring_time,
    )
    def _watch_compute_nodes_allocation():
        compute_nodes = len(scheduler_commands.get_compute_nodes())
        ec2_capacity = get_compute_nodes_instance_count(stack_name, region)
        timestamp = time.time()

        # add values only if there is a transition.
        if (
            len(ec2_capacity_time_series) == 0
            or ec2_capacity_time_series[-1] != ec2_capacity
            or compute_nodes_time_series[-1] != compute_nodes
        ):
            ec2_capacity_time_series.append(ec2_capacity)
            compute_nodes_time_series.append(compute_nodes)
            timestamps.append(timestamp)

    try:
        _watch_compute_nodes_allocation()
    except RetryError:
        # ignoring this error in order to perform assertions on the collected data.
        pass

    logging.info(
        "Monitoring completed: %s, %s, %s",
        "ec2_capacity_time_series [" + " ".join(map(str, ec2_capacity_time_series)) + "]",
        "compute_nodes_time_series [" + " ".join(map(str, compute_nodes_time_series)) + "]",
        "timestamps [" + " ".join(map(str, timestamps)) + "]",
    )
    return ec2_capacity_time_series, compute_nodes_time_series, timestamps


def watch_compute_nodes(scheduler_commands, max_monitoring_time, number_of_nodes):
    """Watch periodically the number of nodes seen by the scheduler."""
    compute_nodes_time_series = []
    timestamps = []

    @retry(
        # Retry until the given number_of_nodes is equal to the number of compute nodes
        retry_on_result=lambda _: compute_nodes_time_series[-1] != number_of_nodes,
        wait_fixed=seconds(20),
        stop_max_delay=max_monitoring_time,
    )
    def _watch_compute_nodes_allocation():
        compute_nodes = scheduler_commands.compute_nodes_count()
        timestamp = time.time()

        # add values only if there is a transition.
        if len(compute_nodes_time_series) == 0 or compute_nodes_time_series[-1] != compute_nodes:
            compute_nodes_time_series.append(compute_nodes)
            timestamps.append(timestamp)

    try:
        _watch_compute_nodes_allocation()
    except RetryError:
        # ignoring this error in order to perform assertions on the collected data.
        pass

    logging.info(
        "Monitoring completed: %s, %s",
        "compute_nodes_time_series [" + " ".join(map(str, compute_nodes_time_series)) + "]",
        "timestamps [" + " ".join(map(str, timestamps)) + "]",
    )


def get_stack(stack_name, region, cfn_client=None):
    """
    Get the output for a DescribeStacks action for the given Stack.

    :return: the Stack data type
    """
    if not cfn_client:
        cfn_client = boto3.client("cloudformation", region_name=region)
    return cfn_client.describe_stacks(StackName=stack_name).get("Stacks")[0]


def get_stack_output_value(stack_outputs, output_key):
    """
    Get output value from Cloudformation Stack Output.

    :return: OutputValue if that output exists, otherwise None
    """
    return next((o.get("OutputValue") for o in stack_outputs if o.get("OutputKey") == output_key), None)


def get_batch_ce(stack_name, region):
    """
    Get name of the AWS Batch Compute Environment.

    :return: ce_name or exit if not found
    """
    outputs = get_stack(stack_name, region).get("Outputs")
    return get_stack_output_value(outputs, "BatchComputeEnvironmentArn")


def get_batch_ce_max_size(stack_name, region):
    """Get max vcpus for Batch Compute Environment."""
    client = boto3.client("batch", region_name=region)

    return (
        client.describe_compute_environments(computeEnvironments=[get_batch_ce(stack_name, region)])
        .get("computeEnvironments")[0]
        .get("computeResources")
        .get("maxvCpus")
    )


def get_batch_ce_min_size(stack_name, region):
    """Get min vcpus for Batch Compute Environment."""
    client = boto3.client("batch", region_name=region)

    return (
        client.describe_compute_environments(computeEnvironments=[get_batch_ce(stack_name, region)])
        .get("computeEnvironments")[0]
        .get("computeResources")
        .get("minvCpus")
    )


def setup_ec2_launch_override_to_emulate_ice(
    cluster, single_instance_type_ice_cr="", multi_instance_types_ice_cr="", multi_instance_types_exp_cr=""
):
    """
    Includes an override file that emulates an ICE error in a cluster.

    It applies the override patch to launch ice for nodes in
    <single_instance_type_ice_cr> and/or <multi_instance_types_ice_cr> compute resources with an ICE error
    """
    remote_command_executor = RemoteCommandExecutor(cluster)

    # fmt: off
    remote_command_executor.run_remote_script(
        script_file=str(SCALING_COMMON_DATADIR / "overrides.sh"),
        args=[
            f"--single-instance-type-ice-cr \"{single_instance_type_ice_cr}\"",
            f"--multi-instance-types-ice-cr \"{multi_instance_types_ice_cr}\"",
            f"--multi-instance-types-exp-cr \"{multi_instance_types_exp_cr}\"",
        ],
        run_as_root=True,
    )
    # fmt: on
