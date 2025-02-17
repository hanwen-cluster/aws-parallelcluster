# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
import logging
import time

import boto3
import pytest
from assertpy import assert_that
from constants import NodeType
from remote_command_executor import RemoteCommandExecutor
from utils import get_username_for_os, wait_for_computefleet_changed

from tests.common.assertions import (
    assert_aws_identity_access_is_correct,
    assert_cluster_imds_v2_requirement_status,
    assert_default_user_has_desired_sudo_access,
    assert_head_node_is_running,
    assert_lines_in_logs,
)
from tests.common.utils import reboot_head_node, retrieve_latest_ami


@pytest.mark.usefixtures("instance", "scheduler")
def test_create_wrong_os(region, os, pcluster_config_reader, clusters_factory, architecture, request):
    """Test error message when os provide is different from the os of custom AMI"""
    # rhel8 is specified in the config file but an AMI of ubuntu2004 is provided
    wrong_os = "ubuntu2004"
    logging.info("Asserting os fixture is different from wrong_os variable")
    assert_that(os != wrong_os).is_true()
    custom_ami = retrieve_latest_ami(region, wrong_os, ami_type="pcluster", architecture=architecture, request=request)
    cluster_config = pcluster_config_reader(custom_ami=custom_ami)
    cluster = clusters_factory(cluster_config, raise_on_error=False)

    assert_head_node_is_running(region, cluster)
    username = get_username_for_os(wrong_os)
    remote_command_executor = RemoteCommandExecutor(cluster, username=username)

    logging.info("Verifying error in logs")
    assert_lines_in_logs(
        remote_command_executor,
        ["/var/log/chef-client.log"],
        ["RuntimeError", rf"custom AMI.+{wrong_os}.+base.+os.+config file.+{os}"],
    )


@pytest.mark.usefixtures("instance", "scheduler")
@pytest.mark.parametrize(
    "imds_secured, users_allow_list",
    [
        (True, {"root": True, "pcluster-admin": True, "slurm": False}),
        (False, {"root": True, "pcluster-admin": True, "slurm": True}),
    ],
)
def test_create_imds_secured(
    imds_secured, users_allow_list, region, os, pcluster_config_reader, clusters_factory, architecture
):
    """
    Test IMDS access with different configurations.
    In particular, it also verifies that IMDS access is preserved on instance reboot.
    Also checks that the cluster instances respect the desired ImdsSupport setting.
    """
    cluster_config = pcluster_config_reader(imds_secured=imds_secured)
    cluster = clusters_factory(cluster_config, raise_on_error=True)
    status = "required"

    logging.info("Checking cluster access after cluster creation")
    assert_head_node_is_running(region, cluster)
    assert_aws_identity_access_is_correct(cluster, users_allow_list)
    assert_cluster_imds_v2_requirement_status(region, cluster, status)

    reboot_head_node(cluster)

    logging.info("Checking cluster access after head node reboot")
    assert_head_node_is_running(region, cluster)
    assert_aws_identity_access_is_correct(cluster, users_allow_list)
    assert_cluster_imds_v2_requirement_status(region, cluster, status)


@pytest.mark.usefixtures("instance", "scheduler")
def test_create_disable_sudo_access_for_default_user(
    region, os, pcluster_config_reader, clusters_factory, architecture
):
    """
    Verify that the cluster removes the Sudo access for default user
    in all the nodes of the Cluster if the DisableSudoAccessForDefaultUser is enabled.
    """
    login_nodes_count = 1
    disable_sudo_access_default_user = False
    cluster_config = pcluster_config_reader(
        disable_sudo_access_default_user=disable_sudo_access_default_user, login_nodes_count=login_nodes_count
    )
    cluster = clusters_factory(cluster_config)

    logging.info("Checking default user has disabled sudo access after cluster creation")
    assert_head_node_is_running(region, cluster)
    wait_for_computefleet_changed(cluster, "RUNNING")
    for node_type in NodeType:
        assert_default_user_has_desired_sudo_access(cluster, node_type, region, disable_sudo_access_default_user)

    logging.info("Updating Cluster to enable sudo access")
    # Compute fleet shutdown
    cluster.stop()
    wait_for_computefleet_changed(cluster, "STOPPED")
    # Login node stop by updating the cluster with count to 0
    login_nodes_count = 0
    updated_config_file = pcluster_config_reader(
        config_file="pcluster_update_login_nodes_count_to_0.config.yaml",
        output_file="pcluster_update_login_nodes_count_to_0.yaml",
        disable_sudo_access_default_user=disable_sudo_access_default_user,
        login_nodes_count=login_nodes_count,
    )
    cluster.update(str(updated_config_file))

    # Start Login Node by updating the count to 1 and update the value of DisableSudoAccessForDefaultUser
    login_nodes_count = 1
    disable_sudo_access_default_user = not disable_sudo_access_default_user
    updated_config_file = pcluster_config_reader(
        config_file="pcluster_update_login_nodes_count_to_1.config.yaml",
        output_file="pcluster_update_login_nodes_count_to_1.config.yaml",
        disable_sudo_access_default_user=disable_sudo_access_default_user,
        login_nodes_count=login_nodes_count,
    )
    cluster.update(str(updated_config_file), force_update="true")

    # Compute fleet Start
    cluster.start()
    wait_for_computefleet_changed(cluster, "RUNNING")
    time.sleep(20)
    logging.info("Checking default user's sudo access after cluster Update")
    for node_type in NodeType:
        assert_default_user_has_desired_sudo_access(cluster, node_type, region, disable_sudo_access_default_user)


@pytest.mark.usefixtures("instance", "os", "scheduler")
def test_cluster_creation_with_problematic_preinstall_script(
    region, pcluster_config_reader, s3_bucket_factory, clusters_factory, test_datadir
):
    """Test cluster creation fail with OnNodeStart Script."""
    bucket_name = s3_bucket_factory()
    bucket = boto3.resource("s3", region_name=region).Bucket(bucket_name)
    script_name = "pre_install.sh"
    bucket.upload_file(str(test_datadir / script_name), f"scripts/{script_name}")
    cluster_config = pcluster_config_reader(resource_bucket=bucket_name)
    cluster = clusters_factory(cluster_config, raise_on_error=False)

    assert_head_node_is_running(region, cluster)
    remote_command_executor = RemoteCommandExecutor(cluster)

    logging.info("Verifying error in logs")
    assert_lines_in_logs(
        remote_command_executor,
        ["/var/log/cfn-init.log"],
        [f"Failed to execute OnNodeStart script 1 s3://{ bucket_name }/scripts/{script_name}"],
    )
    logging.info("Verifying error in cloudformation failure reason")
    stack_events = cluster.get_stack_events().get("events")
    cfn_failure_reason = _get_failure_reason(stack_events)
    expected_cfn_failure_reason = (
        "Failed to execute OnNodeStart script 1, "
        "return code: 1. Please check /var/log/cfn-init.log in the head node, or check the "
        "cfn-init.log in CloudWatch logs. Please refer to https://docs.aws.amazon.com/"
        "parallelcluster/latest/ug/troubleshooting-v3.html#troubleshooting-v3-get-logs for "
        "more details on ParallelCluster logs."
    )

    assert_that(cfn_failure_reason).contains(expected_cfn_failure_reason)
    assert_that(cfn_failure_reason).does_not_contain(f"s3://{ bucket_name }/scripts/{script_name}")

    logging.info("Verifying failures in describe-clusters output")
    expected_failures = [
        {
            "failureCode": "OnNodeStartExecutionFailure",
            "failureReason": "Failed to execute OnNodeStart script.",
        }
    ]
    assert_that(cluster.creation_response.get("failures")).is_equal_to(expected_failures)


@pytest.mark.usefixtures("instance", "os", "scheduler")
def test_cluster_creation_timeout(region, pcluster_config_reader, s3_bucket_factory, clusters_factory, test_datadir):
    """Test cluster creation fail with cluster creation timeout."""
    cluster_config = pcluster_config_reader()
    cluster = clusters_factory(cluster_config, raise_on_error=False)

    assert_head_node_is_running(region, cluster)
    logging.info("Verifying error in cloudformation failure reason")
    stack_events = cluster.get_stack_events().get("events")
    cfn_failure_reason = _get_failure_reason(stack_events)
    expected_cfn_failure_reason = "WaitCondition timed out. Received 0 conditions when expecting 1"

    assert_that(cfn_failure_reason).is_equal_to(expected_cfn_failure_reason)

    logging.info("Verifying failures in describe-clusters output")
    expected_failures = [
        {
            "failureCode": "HeadNodeBootstrapFailure",
            "failureReason": "Cluster creation timed out.",
        }
    ]
    assert_that(cluster.creation_response.get("failures")).is_equal_to(expected_failures)


@pytest.mark.usefixtures("instance", "os", "scheduler")
def test_cluster_creation_with_invalid_ebs(
    region, pcluster_config_reader, s3_bucket_factory, clusters_factory, test_datadir
):
    """Test cluster creation fail with cluster creation timeout."""
    cluster_config = pcluster_config_reader(problematic_volume_id="vol-00000000000000000")
    cluster = clusters_factory(cluster_config, raise_on_error=False, suppress_validators="ALL")
    remote_command_executor = RemoteCommandExecutor(cluster)
    assert_head_node_is_running(region, cluster)
    logging.info("Verifying error in logs")
    assert_lines_in_logs(
        remote_command_executor,
        ["/var/log/chef-client.log"],
        [r"Error executing action `mount` on resource 'manage_ebs\[add ebs\]'"],
    )
    logging.info("Verifying error in cloudformation failure reason")
    stack_events = cluster.get_stack_events().get("events")
    cfn_failure_reason = _get_failure_reason(stack_events)
    expected_cfn_failure_reason = (
        "Failed to mount EBS volume. Please check /var/log/chef-client.log in the head "
        "node, or check the chef-client.log in CloudWatch logs. Please refer to "
        "https://docs.aws.amazon.com/parallelcluster/latest/ug/troubleshooting-v3.html for more details."
    )

    assert_that(cfn_failure_reason).contains(expected_cfn_failure_reason)

    logging.info("Verifying failures in describe-clusters output")
    expected_failures = [
        {
            "failureCode": "EbsMountFailure",
            "failureReason": "Failed to mount EBS volume.",
        }
    ]
    assert_that(cluster.creation_response.get("failures")).is_equal_to(expected_failures)


def _get_failure_reason(events):
    """Reason of the failure when the cluster_status is in CREATE_FAILED."""

    def _is_failed_wait(event):
        if (
            event.get("resourceType") == "AWS::CloudFormation::WaitCondition"
            and event.get("resourceStatus") == "CREATE_FAILED"
        ):
            return True
        return False

    failure_event = next(filter(_is_failed_wait, events), None)
    return failure_event.get("resourceStatusReason") if failure_event else None
