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
import json
import logging
import re
import tarfile
import tempfile
import time

import boto3
import pytest
from assertpy import assert_that, soft_assertions
from botocore.exceptions import ClientError
from cfn_stacks_factory import CfnStack
from remote_command_executor import RemoteCommandExecutor
from retrying import RetryError, retry
from time_utils import minutes, seconds
from troposphere import Template, iam
from utils import generate_stack_name, get_arn_partition, get_gpu_count, run_command

from tests.common.assertions import (
    _assert_build_image_stack_deleted,
    assert_head_node_is_running,
    assert_instance_has_desired_imds_v2_setting,
    assert_instance_has_desired_tags,
    assert_lambda_vpc_settings_are_correct,
    assert_no_msg_in_logs,
)
from tests.common.utils import (
    export_image_logs,
    generate_random_string,
    get_aws_domain,
    get_installed_parallelcluster_base_version,
    get_installed_parallelcluster_version,
    keep_recent_image_logs,
    retrieve_latest_ami,
    upload_github_artifacts_to_s3,
    wait_for_build_image_complete,
    wait_for_image_build_status,
)
from tests.proxy.test_proxy import proxy_stack_factory  # noqa: F401
from tests.storage import storage_common


class ImageNotFound(Exception):
    """Exception when image is not found."""

    pass


def _get_base_ami(region, os, architecture):
    """Select the appropriate base AMI and feature flags based on OS.

    Uses first-stage AMIs for RHEL/Rocky/Ubuntu (kernel version requirements),
    remarkable (Deep Learning) AMIs for ubuntu2204, and official AMIs for everything else.
    Returns (base_ami, feature_flags) where feature_flags is a dict with keys:
      enable_nvidia, update_os_packages, enable_lustre_client.
    """
    enable_nvidia = True
    update_os_packages = os in ["alinux2023", "rocky9"]
    enable_lustre_client = True

    if os in ["ubuntu2204"]:
        # Test Deep Learning AMIs
        base_ami = retrieve_latest_ami(region, os, ami_type="remarkable", architecture=architecture)
        enable_nvidia = False  # Deep learning AMIs have Nvidia pre-installed
    elif "rhel" in os or "ubuntu" in os or os == "rocky8":
        # Use official AMIs. First stage AMIs must not be used as parent image in this test.
        base_ami = retrieve_latest_ami(region, os, ami_type="official", architecture=architecture)
        update_os_packages = True
        if os in ["ubuntu2204", "rhel9", "ubuntu2404"]:
            enable_lustre_client = False
    else:
        # Test vanilla AMIs.
        base_ami = retrieve_latest_ami(region, os, ami_type="official", architecture=architecture)
        if os in ["rocky9"]:
            enable_lustre_client = False

    feature_flags = {
        "enable_nvidia": enable_nvidia,
        "update_os_packages": update_os_packages,
        "enable_lustre_client": enable_lustre_client,
    }
    return base_ami, feature_flags


@pytest.mark.usefixtures("instance")
def test_build_image_no_internet(
    region,
    os,
    pcluster_config_reader,
    architecture,
    proxy_stack_factory,  # noqa: F811
    images_factory,
    s3_bucket_factory,
    request,
):
    """Test build image in a private subnet with no internet access, only VPC endpoints and a proxy for OS repos."""
    base_ami, feature_flags = _get_base_ami(region, os, architecture)
    # Enable NVIDIA installation so the offline NVIDIA/imex install path is exercised through the
    # proxy. It is disabled for the Deep Learning AMI parent, which already ships the driver (the
    # nvidia_install recipe skips the whole stack when the driver is already present).
    enable_nvidia = feature_flags["enable_nvidia"]

    # Create proxy stack with build-image mode enabled
    no_internet_proxy_stack = proxy_stack_factory(enable_build_image_proxy=True)

    # Upload dev packages to S3 so the build instance can access them via the S3 VPC endpoint
    # instead of GitHub (which is blocked in the no-internet environment).
    bucket_name = s3_bucket_factory()
    s3_artifacts = upload_github_artifacts_to_s3(bucket_name, region, request)

    # Get the proxy URL from the stack output
    install_http_proxy_address = no_internet_proxy_stack.cfn_outputs["ProxyAddress"]

    image_id = generate_stack_name("integ-tests-build-image-no-internet", request.config.getoption("stackname_suffix"))
    image_config = pcluster_config_reader(
        config_file="image.config.yaml",
        parent_image=base_ami,
        subnet_id=no_internet_proxy_stack.cfn_outputs["PrivateSubnet"],
        security_group_id=no_internet_proxy_stack.cfn_outputs["DefaultSecurityGroupId"],
        chef_cookbook=s3_artifacts["chef_cookbook"],
        node_package=s3_artifacts["node_package"],
        install_http_proxy_address=install_http_proxy_address,
        enable_nvidia=str(enable_nvidia).lower(),
        enable_lustre_client=str(feature_flags["enable_lustre_client"]).lower(),
        update_os_packages=str(feature_flags["update_os_packages"]).lower(),
    )

    image = images_factory(image_id, image_config, region)
    wait_for_build_image_complete(image, request.config.getoption("output_dir"))


@pytest.mark.usefixtures("instance")
def test_invalid_config(
    region,
    os,
    pcluster_config_reader,
    architecture,
    s3_bucket_factory,
    build_image_custom_resource,
    images_factory,
):
    # Test validation error
    arm64_ami = retrieve_latest_ami(region, os, architecture="arm64")
    image_id = f"integ-test-build-image-{generate_random_string()}"

    # Get custom S3 bucket
    bucket_name = s3_bucket_factory()
    image_config = pcluster_config_reader(
        config_file="image.config.yaml", parent_image=arm64_ami, bucket_name=bucket_name
    )
    image = images_factory(image_id, image_config, region, raise_on_error=False, log_error=False)

    assert_that(image.configuration_errors).is_length(1)
    assert_that(image.configuration_errors[0]).contains("level")
    assert_that(image.configuration_errors[0]).contains("type")
    assert_that(image.configuration_errors[0]).contains("message")
    assert_that(image.configuration_errors[0]["type"]).is_equal_to("InstanceTypeBaseAMICompatibleValidator")

    # Test Suppression of a validator

    base_ami = retrieve_latest_ami(region, os, ami_type="remarkable", architecture=architecture)

    image_config = pcluster_config_reader(
        config_file="warnings.image.config.yaml", parent_image=base_ami, bucket_name=bucket_name
    )
    suppressed = images_factory(
        image_id,
        image_config,
        region,
        raise_on_error=False,
        log_error=False,
        dryrun=True,
        suppress_validators="type:UrlValidator",
    )
    assert_that(suppressed.message).contains("Request would have succeeded")


@pytest.mark.flaky(only_rerun=["ImageNotFound"])
@pytest.mark.usefixtures("scheduler")
def test_build_image(
    region,
    os,
    instance,
    pcluster_config_reader,
    architecture,
    s3_bucket_factory,
    build_image_custom_resource,
    images_factory,
    noexec_tmp_ami_factory,
    request,
    clusters_factory,
    scheduler_commands_factory,
):
    """
    Test build image for given region and os.

    In the cluster config there is DisableValidateAndTest:False to enable kitchen tests in the validate phase.
    The created AMI is also used for a cluster.
    Also check that the build instance has the desired ImdsSupport setting (v2.0, so IMDSv2 is required).
    Also check that the build-image stack can be fully self deleted.
    """
    image_id = generate_stack_name("integ-tests-build-image", request.config.getoption("stackname_suffix"))

    # Get custom instance role
    instance_role = build_image_custom_resource(image_id=image_id)

    # Get custom S3 bucket
    bucket_name = s3_bucket_factory()
    _set_s3_bucket_policy(bucket_name, get_arn_partition(region), region)

    # Get base AMI and feature flags
    base_ami, flags = _get_base_ami(region, os, architecture)

    enable_nvidia = flags["enable_nvidia"] and get_gpu_count(instance) > 0
    if not enable_nvidia:
        # Build from an AMI where /tmp is already noexec at boot.
        base_ami = noexec_tmp_ami_factory(base_ami, instance)

    image_config = pcluster_config_reader(
        config_file="image.config.yaml",
        parent_image=base_ami,
        instance_role=instance_role,
        bucket_name=bucket_name,
        enable_nvidia=str(enable_nvidia).lower(),
        update_os_packages=str(flags["update_os_packages"]).lower(),
        enable_lustre_client=str(flags["enable_lustre_client"]).lower(),
    )

    image = images_factory(image_id, image_config, region)
    _test_build_tag(image)
    _test_image_stack_events(image)

    cfn_client = boto3.client("cloudformation", region_name=region)
    _wait_for_creation_of_delete_stack_function(image.image_id, cfn_client)

    lamda_vpc_config = image.config["DeploymentSettings"]["LambdaFunctionsVpcConfig"]
    assert_lambda_vpc_settings_are_correct(
        image.image_id, region, lamda_vpc_config["SecurityGroupIds"], lamda_vpc_config["SubnetIds"]
    )

    with soft_assertions():
        _wait_for_build_instance(image)
        _test_build_instances_tags(image, image.config["Build"]["Tags"])
        _test_build_imds_settings(image, "required")

        wait_for_build_image_complete(image, request.config.getoption("output_dir"))

        # Only validate export-image-logs if the build did NOT complete successfully. On a successful
        # build the build-image stack self-deletes, which races the export command and causes
        # intermittent "stack does not exist" failures; a successful build also does not need its logs
        # exported. If the build failed the stack is retained, so the export reliably has its stack.
        #
        # On a successful build we still want evidence that the build logs reached CloudWatch, so we tail
        # the image build log group instead: `aws logs tail` reads the log group directly and has none of
        # export's dependencies (no S3 bucket, no build-image stack), so it cannot hit the deletion race.
        if image.image_status != "BUILD_COMPLETE":
            _test_export_logs(s3_bucket_factory, image, region)
            _test_export_logs(s3_bucket_factory, image, region, True)
        else:
            _test_tail_image_logs(image, region)

        _test_image_tag_and_volume(image)
        _test_list_image_log_streams(image)
        _test_get_image_log_events(image)
        _test_list_images(image)

    _test_cluster_creation(
        image.ec2_image_id, pcluster_config_reader, region, clusters_factory, scheduler_commands_factory
    )
    _assert_build_image_stack_deleted(image.image_id, region, 900, 30)


def _test_cluster_creation(image_id, pcluster_config_reader, region, clusters_factory, scheduler_commands_factory):
    """Create cluster with given image id and verify it's possible to run jobs on it."""
    cluster_config = pcluster_config_reader(custom_ami=image_id)
    cluster = clusters_factory(cluster_config, raise_on_error=True)

    assert_head_node_is_running(region, cluster)
    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)
    node_number = 2

    result = scheduler_commands.submit_command(command="uptime", nodes=node_number)
    job_id = scheduler_commands.assert_job_submitted(result.stdout)
    scheduler_commands.wait_job_completed(job_id)
    scheduler_commands.assert_job_succeeded(job_id, children_number=node_number)

    assert_no_msg_in_logs(remote_command_executor, ["/var/log/slurmctld.log"], ["launch failure"])


@retry(
    retry_on_result=lambda result: result == "CREATE_IN_PROGRESS",
    retry_on_exception=lambda exception: (
        isinstance(exception, ClientError)
        and any(
            message in str(exception) for message in {"Rate exceeded", "Resource DeleteStackFunction does not exist"}
        )
    ),
    wait_fixed=seconds(10),
    stop_max_delay=minutes(30),
)
def _wait_for_creation_of_delete_stack_function(stack_name, cfn_client):
    return (
        cfn_client.describe_stack_resource(StackName=stack_name, LogicalResourceId="DeleteStackFunction")
        .get("StackResourceDetail")
        .get("ResourceStatus")
    )


def _test_list_images(image):
    images = image.list_images(region=image.region, image_status="AVAILABLE")["images"]
    matches = [img for img in images if img["imageId"] == image.image_id]
    assert_that(matches).is_length(1)
    assert_that(matches[0]["imageId"]).is_equal_to(image.image_id)
    assert_that(matches[0]["region"]).is_equal_to(image.region)
    image.describe()
    assert_that(matches[0]["ec2AmiInfo"]["amiId"]).is_equal_to(image.ec2_image_id)
    assert_that(matches[0]["imageBuildStatus"]).is_equal_to("BUILD_COMPLETE")
    assert_that(matches[0]).contains("version")


def _test_image_stack_events(image):
    stack_events_resp = image.get_stack_events()
    assert_that(stack_events_resp).is_not_none()
    assert_that(stack_events_resp).contains("events")
    assert_that(stack_events_resp["events"]).is_not_empty()

    first_event = stack_events_resp["events"][0]
    assert_that(first_event).contains("eventId")
    assert_that(first_event).contains("logicalResourceId")
    assert_that(first_event).contains("physicalResourceId")
    assert_that(first_event).contains("stackId")
    assert_that(first_event).contains("timestamp")


def _test_list_image_log_streams(image):
    logging.info("Testing that pcluster list-image-log-streams is working as expected")
    list_streams_result = image.list_log_streams()
    streams = list_streams_result["logStreams"]

    stream_names = {stream["logStreamName"] for stream in streams}
    expected_log_stream = image.build_log_stream_name
    assert_that(stream_names).contains(expected_log_stream)


def _test_get_image_log_events(image):
    """Test pcluster get-image-log-events functionality."""
    logging.info("Testing that pcluster get-image-log-events is working as expected")
    log_stream_name = image.build_log_stream_name

    # GetLogEvents can return empty pages while more events are available,
    # so page forward until the token stops changing, which marks the end of the stream.
    events = []
    next_token = None
    while True:
        response = image.get_log_events(log_stream_name, start_from_head=True, next_token=next_token)
        events.extend(response["events"])
        if not response.get("nextToken") or response["nextToken"] == next_token:
            break
        next_token = response["nextToken"]

    assert_that(events).is_not_empty()


def _set_s3_bucket_policy(bucket_name, partition, region):
    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "s3:GetBucketAcl",
                "Effect": "Allow",
                "Resource": f"arn:{partition}:s3:::{bucket_name}",
                "Principal": {"Service": f"logs.{region}.amazonaws.com"},
            },
            {
                "Action": "s3:PutObject",
                "Effect": "Allow",
                "Resource": f"arn:{partition}:s3:::{bucket_name}/*",
                "Condition": {"StringEquals": {"s3:x-amz-acl": "bucket-owner-full-control"}},
                "Principal": {"Service": f"logs.{region}.amazonaws.com"},
            },
        ],
    }
    boto3.client("s3").put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(bucket_policy))


def _test_tail_image_logs(image, region):
    """Assert the image build logs reached CloudWatch by tailing the image build log group.

    Used on a successful build, where export-image-logs is skipped (the build-image stack self-deletes
    and races the export). ``aws logs tail`` reads the log group directly -- no S3 bucket and no
    dependency on the build-image stack -- so it gives race-free evidence that logs are being pushed.
    The image build log group is ``/aws/imagebuilder/ParallelClusterImage-<image_id>``.
    """
    logging.info("Testing that image build logs reached CloudWatch via aws logs tail")
    log_group_name = f"/aws/imagebuilder/ParallelClusterImage-{image.image_id}"
    # `aws logs tail` defaults to only the last 10 minutes; an image build takes much longer, so widen
    # the window with --since so an empty result means "no logs" rather than "no logs in the last 10 min".
    result = run_command(
        ["aws", "logs", "tail", log_group_name, "--since", "1h", "--region", region],
        raise_on_error=True,
    )
    assert_that(result.stdout.strip()).described_as(
        f"no log events found in {log_group_name}; build logs were not pushed to CloudWatch"
    ).is_not_empty()


def _test_export_logs(s3_bucket_factory, image, region, use_pcluster_bucket=False):
    bucket_name = None
    if not use_pcluster_bucket:
        bucket_name = s3_bucket_factory()
        logging.info("bucket is %s", bucket_name)

        # set bucket permissions
        partition = get_arn_partition(region)
        _set_s3_bucket_policy(bucket_name, partition, image.region)
    else:
        logging.info("Using default pcluster bucket.")

    with tempfile.TemporaryDirectory() as tempdir:
        output_file = f"{tempdir}/testfile.tar.gz"
        bucket_prefix = "test_prefix"

        ret = image.export_logs(
            bucket=bucket_name,
            output_file=output_file,
            bucket_prefix=bucket_prefix,
        )

        assert_that(ret["path"]).contains(output_file)

        rexp = rf"{image.image_id}-logs.*/cloudwatch-logs/{get_installed_parallelcluster_base_version()}-1"
        with tarfile.open(output_file) as archive:
            match = any(re.match(rexp, logfile.name) for logfile in archive)
        assert_that(match).is_true()


def _test_build_tag(image):
    logging.info("Check the build tag is present as specified in config file.")
    stack_list = boto3.client("cloudformation").describe_stacks(StackName=image.image_id).get("Stacks")
    logging.info(stack_list)
    assert_that(len(stack_list)).is_equal_to(1)
    stack_tags = stack_list[0].get("Tags")
    logging.info(stack_tags)
    assert_that(stack_tags).contains({"Key": "dummyBuildTag", "Value": "dummyBuildTag"})


def _test_image_tag_and_volume(image):
    logging.info("Check the image tag is present as specified in config file.")
    image_list = (
        boto3.client("ec2")
        .describe_images(
            ImageIds=[], Filters=[{"Name": "tag:parallelcluster:image_id", "Values": [image.image_id]}], Owners=["self"]
        )
        .get("Images")
    )
    logging.info(f"Image List: {image_list}, length {len(image_list)}")
    if not image_list:
        raise ImageNotFound()
    assert_that(len(image_list)).is_equal_to(1)

    if len(image_list) > 0:
        created_image = image_list[0]
        volume_size = created_image.get("BlockDeviceMappings")[0].get("Ebs").get("VolumeSize")
        assert_that(volume_size).is_equal_to(200)
        assert_that(created_image["Tags"]).contains({"Key": "dummyImageTag", "Value": "dummyImageTag"})


@pytest.fixture()
def build_image_custom_resource(cfn_stacks_factory, region, request):
    """
    Define a fixture to manage the creation and destruction of build image resource( custom instance role).

    return instance role
    """
    stack_name_post_test = None

    def _custom_resource(image_id):
        nonlocal stack_name_post_test
        # custom resource stack
        custom_resource_stack_name = generate_stack_name(
            "-".join([image_id, "custom", "resource"]), request.config.getoption("stackname_suffix")
        )
        stack_name_post_test = custom_resource_stack_name
        custom_resource_template = Template()
        custom_resource_template.set_version()
        custom_resource_template.set_description("Create build image custom resource stack")

        # Create a instance role
        partition = get_arn_partition(region)
        managed_policy_arns = [
            f"arn:{partition}:iam::aws:policy/AmazonSSMManagedInstanceCore",
            f"arn:{partition}:iam::aws:policy/EC2InstanceProfileForImageBuilder",
        ]

        policy_document = iam.Policy(
            PolicyName="myInstanceRoleInlinePolicy",
            PolicyDocument={
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ec2:CreateTags",
                            "ec2:ModifyImageAttribute",
                            "s3:GetObject",
                            "cloudformation:ListStacks",
                        ],
                        "Resource": "*",
                    }
                ]
            },
        )
        instance_role = iam.Role(
            "CustomInstanceRole",
            AssumeRolePolicyDocument={
                "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": ["ec2.amazonaws.com"]}, "Action": ["sts:AssumeRole"]}
                ]
            },
            Description="custom instance role for build image test.",
            ManagedPolicyArns=managed_policy_arns,
            Path="/parallelcluster/",
            Policies=[policy_document],
        )

        custom_resource_template.add_resource(instance_role)
        custom_resource_stack = CfnStack(
            name=custom_resource_stack_name,
            region=region,
            template=custom_resource_template.to_json(),
            capabilities=["CAPABILITY_NAMED_IAM"],
        )
        cfn_stacks_factory.create_stack(custom_resource_stack)

        role_name = custom_resource_stack.cfn_resources["CustomInstanceRole"]
        instance_role_arn = boto3.client("iam").get_role(RoleName=role_name).get("Role").get("Arn")
        logging.info("Custom instance role arn %s", instance_role_arn)

        return instance_role_arn

    yield _custom_resource
    if stack_name_post_test and not request.config.getoption("no_delete"):
        cfn_stacks_factory.delete_stack(stack_name_post_test, region)
    else:
        logging.warning("Skipping deletion of CFN image stacks because --no-delete option is set")


def test_build_image_custom_components(
    region, os, instance, test_datadir, pcluster_config_reader, architecture, s3_bucket_factory, images_factory, request
):
    """Test custom components and base AMI is ParallelCluster AMI"""
    # Custom script
    custom_script_file = "custom_script_ubuntu.sh" if os.startswith("ubuntu") else "custom_script.sh"

    # Create S3 bucket for pre install scripts, to remove epel package if it is installed
    bucket_name = s3_bucket_factory()
    bucket = boto3.resource("s3", region_name=region).Bucket(bucket_name)
    time.sleep(60)
    bucket.upload_file(str(test_datadir / custom_script_file), "scripts/custom_script.sh")

    # Get ParallelCluster AMI as base AMI
    base_ami = retrieve_latest_ami(region, os, ami_type="pcluster", architecture=architecture, request=request)

    image_id = generate_stack_name(
        "integ-tests-build-image-custom-components", request.config.getoption("stackname_suffix")
    )
    image_config = pcluster_config_reader(
        config_file="image.config.yaml",
        parent_image=base_ami,
        instance_type=instance,
        bucket_name=bucket_name,
        region=region,
    )

    image = images_factory(image_id, image_config, region)

    wait_for_build_image_complete(image, request.config.getoption("output_dir"))


@retry(wait_fixed=seconds(10), stop_max_delay=minutes(10))
def _wait_for_build_instance(image):
    """Wait until the ImageBuilder build instance is launched.

    The build instance exists only while the image is being built; it is terminated and the build-image
    stack self-deletes once the image reaches BUILD_COMPLETE. Waiting for it here lets the tag and IMDS
    checks run against a live instance instead of racing the teardown or passing vacuously.
    """
    build_instance_name = f"Build instance for ParallelClusterImage-{image.image_id}"
    return [
        instance
        for instance in image.get_imagebuilder_instances()
        if instance.get("State", {}).get("Name") == "running"
        and {"Key": "Name", "Value": build_instance_name} in instance.get("Tags", [])
    ]


def _test_build_imds_settings(image, status):
    logging.info(f"Checking that the ImageBuilder instances have IMDSv2 {status}")
    for instance in image.get_imagebuilder_instances():
        assert_instance_has_desired_imds_v2_setting(instance, status)


def _test_build_instances_tags(image, build_tags):
    logging.info("Checking that the ImageBuilder instances have the build tags")
    for instance in image.get_imagebuilder_instances():
        assert_instance_has_desired_tags(instance, build_tags)


@pytest.mark.usefixtures("os")
def test_build_image_wrong_pcluster_version(
    region,
    instance,
    pcluster_config_reader,
    architecture,
    pcluster_ami_without_standard_naming,
    images_factory,
    s3_bucket_factory,
    request,
):
    """Test error message when AMI provided was baked by a pcluster whose version is different from current version"""
    current_version = get_installed_parallelcluster_version()
    # Compute a wrong version that is 2 minor versions before the current one, with patch set to 0.
    # For example, if the current version is 3.15.1, the wrong_version is 3.13.0.
    major, minor, _ = (int(part) for part in current_version.split("."))
    wrong_version = f"{major}.{minor - 2}.0"
    logging.info("Asserting wrong_version is different from current_version")
    assert_that(current_version != wrong_version).is_true()
    # Retrieve an AMI without 'aws-parallelcluster-<version>' in its name.
    # Therefore, we can bypass the version check in CLI and test version check of .bootstrapped file in Cookbook.
    wrong_ami = pcluster_ami_without_standard_naming(wrong_version)

    image_config = pcluster_config_reader(
        config_file="image.config.yaml", parent_image=wrong_ami, instance_type=instance
    )
    image_id = generate_stack_name(
        "integ-tests-build-image-wrong-version", request.config.getoption("stackname_suffix")
    )

    image = images_factory(image_id, image_config, region)

    _test_build_image_failed(image, request.config.getoption("output_dir"))

    # This build deterministically fails, so the build-image stack is retained (it does not
    # self-delete). That makes it a reliable place to validate export-image-logs end to end, for
    # both a custom bucket and the default pcluster bucket.
    _test_export_logs(s3_bucket_factory, image, region)
    _test_export_logs(s3_bucket_factory, image, region, True)

    log_stream_name = image.build_log_stream_name
    log_data = " ".join(log["message"] for log in image.get_log_events(log_stream_name, limit=100)["events"])
    assert_that(log_data).matches(rf"AMI was created.+{wrong_version}.+is.+used.+{current_version}")


def _test_build_image_failed(image, output_dir):
    logging.info("Test build image process for image %s.", image.image_id)
    wait_for_image_build_status(image, ["BUILD_COMPLETE", "BUILD_FAILED"])
    if image.image_status == "BUILD_FAILED":
        export_image_logs(image, output_dir)
        keep_recent_image_logs(image)
    assert_that(image.image_status).is_equal_to("BUILD_FAILED")


# P558591845: on RHEL/Rocky the 3.14.x/3.15.x cookbooks build efs-utils from source, and the AWS-LC FIPS build runs
# `go run`. Red Hat golang >= 1.26.10-1 defaults GOFIPS140 to "certified", so every go command needs a module cache,
# which cannot be found in the Image Builder environment because HOME is not set.
# The tests below build an intermediate parent image with one of the proposed workarounds and run the full
# `pcluster build-image` (including the validate/test phase) and a cluster on top of it.
EFS_UTILS_WORKAROUND_RPM_VERSION = "2.4.2"
# efs-utils version and source archive used by the ParallelCluster 3.14.2 / 3.15.x cookbook
PCLUSTER_EFS_UTILS_VERSION = "2.4.0"
PCLUSTER_EFS_UTILS_TARBALL_SHA256 = "9b60c039c162388091d6fab6e9c6cfc5832f34b26b6d05b0a68b333147d78a25"
EFS_UTILS_WORKAROUND_RESULT_MARKER = "PCLUSTER_EFS_WORKAROUND_RESULT"
GO_BUG_REPRODUCED_MARKER = "PCLUSTER_GO_BUG_REPRODUCED"

_EFS_UTILS_WORKAROUND_HEADER = r"""#!/bin/bash
# Apply a P558591845 workaround, then power off.
# The result is written to the serial console, where the test reads it with GetConsoleOutput.
set -euxo pipefail
report() {
  echo "@RESULT_MARKER@=$1" > /dev/console
  sync
  poweroff
}
trap 'report FAILED' ERR
"""

# Workaround 1: preinstall the prebuilt efs-utils RPM so that the cookbook skips the source build.
# The cookbook then also skips the source archive download and the poll interval change,
# both checked by the InSpec tests of the build-image validate phase, so they are done here.
_EFS_UTILS_RPM_WORKAROUND = r"""
rpm --import https://amazon-efs-utils.aws.com/efs-utils-armored.gpg
# Install the RPM directly: a configured efs-utils repo would let UpdateOsPackages upgrade efs-utils to 3.x
dnf install -y '@EFS_UTILS_RPM_URL@'
rpm -q amazon-efs-utils | grep '^amazon-efs-utils-@EFS_UTILS_RPM_VERSION@-'
tarball=/opt/parallelcluster/sources/efs-utils-@PCLUSTER_EFS_UTILS_VERSION@.tar.gz
mkdir -p /opt/parallelcluster/sources
curl -fsSL --retry 3 -o "${tarball}" '@EFS_UTILS_TARBALL_URL@'
echo "@EFS_UTILS_TARBALL_SHA256@  ${tarball}" | sha256sum -c
chmod 0644 "${tarball}"
sed -i 's/^poll_interval_sec = 1$/poll_interval_sec = 10/' /etc/amazon/efs/efs-utils.conf
grep -x 'poll_interval_sec = 10' /etc/amazon/efs/efs-utils.conf
report SUCCESS
"""

# Workaround 2: preinstall golang with GOFIPS140=off in GOROOT/go.env (read even without HOME),
# and lock the golang version because go.env is not a %config file and is replaced on upgrade.
_GO_FIPS_OFF_WORKAROUND = r"""
dnf install -y golang python3-dnf-plugin-versionlock
rpm -q golang
# Run Go the way aws-lc-fips-sys does inside Image Builder: no HOME, GOPATH or GOMODCACHE, explicit GOCACHE
mkdir -p /tmp/go-check
cd /tmp/go-check
printf 'module check\n\ngo 1.13\n' > go.mod
printf 'package main\n\nfunc main() { println("ok") }\n' > main.go
if env -i PATH=/usr/bin:/bin GOCACHE=/tmp/go-check/cache go run .; then
  echo "@GO_BUG_MARKER@=no" > /dev/console
else
  echo "@GO_BUG_MARKER@=yes" > /dev/console
fi
echo 'GOFIPS140=off' >> /usr/lib/golang/go.env
dnf versionlock add golang golang-bin golang-src
env -i PATH=/usr/bin:/bin GOCACHE=/tmp/go-check/cache go run .
cd /
rm -rf /tmp/go-check
report SUCCESS
"""


def _efs_utils_workaround_user_data(workaround, region, os, architecture):
    """Return the user data script that applies the given workaround on a RHEL/Rocky instance."""
    os_major = re.search(r"\d+", os).group()
    if architecture == "arm64":
        rpm_dir, rpm_arch = "arm64", "aarch64"
    else:
        rpm_dir, rpm_arch = "x86_64", "x86_64"
    substitutions = {
        "@RESULT_MARKER@": EFS_UTILS_WORKAROUND_RESULT_MARKER,
        "@GO_BUG_MARKER@": GO_BUG_REPRODUCED_MARKER,
        "@EFS_UTILS_RPM_URL@": (
            f"https://amazon-efs-utils.aws.com/repo/rpm/redhat/{os_major}.%2A/{rpm_dir}/"
            f"amazon-efs-utils-{EFS_UTILS_WORKAROUND_RPM_VERSION}-1.el{os_major}.{rpm_arch}.rpm"
        ),
        "@EFS_UTILS_RPM_VERSION@": EFS_UTILS_WORKAROUND_RPM_VERSION,
        "@EFS_UTILS_TARBALL_URL@": (
            f"https://{region}-aws-parallelcluster.s3.{region}.{get_aws_domain(region)}"
            f"/archives/dependencies/efs/v{PCLUSTER_EFS_UTILS_VERSION}.tar.gz"
        ),
        "@EFS_UTILS_TARBALL_SHA256@": PCLUSTER_EFS_UTILS_TARBALL_SHA256,
        "@PCLUSTER_EFS_UTILS_VERSION@": PCLUSTER_EFS_UTILS_VERSION,
    }
    body = {"efs_utils_rpm": _EFS_UTILS_RPM_WORKAROUND, "go_fips_off": _GO_FIPS_OFF_WORKAROUND}[workaround]
    script = _EFS_UTILS_WORKAROUND_HEADER + body
    for placeholder, value in substitutions.items():
        script = script.replace(placeholder, value)
    return script


def _get_console_marker(ec2_client, instance_id, marker):
    output = ec2_client.get_console_output(InstanceId=instance_id).get("Output", "")
    values = re.findall(rf"{marker}=(\w+)", output)
    return values[-1] if values else None


@retry(retry_on_result=lambda result: result is None, wait_fixed=seconds(30), stop_max_delay=minutes(10))
def _wait_for_console_marker(ec2_client, instance_id, marker):
    # Console output of a stopped instance is published shortly after the state transition
    return _get_console_marker(ec2_client, instance_id, marker)


def _log_console_output(ec2_client, instance_id):
    output = ec2_client.get_console_output(InstanceId=instance_id).get("Output", "")
    logging.info("Console output of %s (last 20000 chars):\n%s", instance_id, output[-20000:])


@retry(
    retry_on_result=lambda state: state != "stopped",
    # The instance may not be visible yet right after RunInstances
    retry_on_exception=lambda exception: (
        isinstance(exception, ClientError) and exception.response["Error"]["Code"] == "InvalidInstanceID.NotFound"
    ),
    wait_fixed=seconds(15),
    stop_max_delay=minutes(45),
)
def _wait_for_instance_stopped(ec2_client, instance_id):
    # Not using the InstanceStopped waiter: it fails as soon as it sees the instance in "pending" state
    instance = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    state = instance["State"]["Name"]
    logging.info("Instance %s is %s", instance_id, state)
    if state in ["shutting-down", "terminated"]:
        raise RuntimeError(f"Instance {instance_id} is {state}: {instance.get('StateReason')}")
    return state


def _wait_for_workaround_result(ec2_client, instance_id, workaround):
    """Wait for the instance applying the workaround to power off and assert the user data succeeded."""
    try:
        _wait_for_instance_stopped(ec2_client, instance_id)
    except Exception:
        _log_console_output(ec2_client, instance_id)
        raise
    try:
        result = _wait_for_console_marker(ec2_client, instance_id, EFS_UTILS_WORKAROUND_RESULT_MARKER)
    except RetryError:
        result = None
    go_bug_reproduced = _get_console_marker(ec2_client, instance_id, GO_BUG_REPRODUCED_MARKER)
    logging.info(
        "Workaround %s result on %s: %s (Go failure reproduced before the fix: %s)",
        workaround,
        instance_id,
        result,
        go_bug_reproduced,
    )
    if result != "SUCCESS":
        _log_console_output(ec2_client, instance_id)
    assert_that(result).described_as(f"{workaround} user data result").is_equal_to("SUCCESS")


def _delete_image_and_snapshots(ec2_client, image_id):
    images = ec2_client.describe_images(ImageIds=[image_id])["Images"]
    snapshot_ids = [
        mapping["Ebs"]["SnapshotId"]
        for image in images
        for mapping in image.get("BlockDeviceMappings", [])
        if "SnapshotId" in mapping.get("Ebs", {})
    ]
    logging.info("Deregistering image %s and deleting snapshots %s", image_id, snapshot_ids)
    ec2_client.deregister_image(ImageId=image_id)
    for snapshot_id in snapshot_ids:
        ec2_client.delete_snapshot(SnapshotId=snapshot_id)


@pytest.fixture()
def efs_utils_workaround_parent_image(region, os, architecture, instance, vpc_stack, request):
    """
    Build an intermediate parent image with a P558591845 workaround.

    An instance is launched from the base AMI with a user data script that applies the workaround and powers the
    instance off. The image is created from the stopped instance and deregistered at the end of the test.
    """
    ec2_client = boto3.client("ec2", region_name=region)
    no_delete = request.config.getoption("no_delete")
    created_image_ids = []

    def _create_parent_image(workaround, base_ami):
        name = generate_stack_name(
            f"integ-tests-efs-wa-parent-{workaround.replace('_', '-')}", request.config.getoption("stackname_suffix")
        )
        logging.info("Launching instance from %s to build parent image %s", base_ami, name)
        instance_id = ec2_client.run_instances(
            ImageId=base_ami,
            InstanceType=instance,
            MinCount=1,
            MaxCount=1,
            UserData=_efs_utils_workaround_user_data(workaround, region, os, architecture),
            InstanceInitiatedShutdownBehavior="stop",
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": vpc_stack.get_public_subnet(),
                    "Groups": [vpc_stack.cfn_outputs["DefaultVpcSecurityGroupId"]],
                    "AssociatePublicIpAddress": True,
                }
            ],
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}],
        )["Instances"][0]["InstanceId"]
        try:
            _wait_for_workaround_result(ec2_client, instance_id, workaround)
            image_id = ec2_client.create_image(
                InstanceId=instance_id,
                Name=name,
                Description=f"P558591845 {workaround} workaround on top of {base_ami}",
            )["ImageId"]
            created_image_ids.append(image_id)
            ec2_client.get_waiter("image_available").wait(
                ImageIds=[image_id], WaiterConfig={"Delay": 15, "MaxAttempts": 240}
            )
            logging.info("Parent image %s with workaround %s is available", image_id, workaround)
            return image_id
        finally:
            if no_delete:
                logging.warning("Not terminating %s because --no-delete option is set", instance_id)
            else:
                ec2_client.terminate_instances(InstanceIds=[instance_id])

    yield _create_parent_image

    if no_delete:
        logging.warning("Skipping deletion of parent images %s because --no-delete option is set", created_image_ids)
        return
    for image_id in created_image_ids:
        _delete_image_and_snapshots(ec2_client, image_id)


# Upgrades of 3.14.2 dependencies that no longer build on current OS releases, applied through
# DevSettings.Cookbook.ExtraChefAttributes:
# - EFA installer 1.44.0 fails on EL 9.8 (infiniband-diags-compat is gone) and its kernel module fails to build
#   on current Ubuntu kernels. 1.49.0 is the version used by 3.16 (workaround from aws-parallelcluster#7406).
# - NVIDIA driver 570.172.08 kernel modules fail to build on current EL9, AL2023 and Ubuntu 24.04 kernels.
#   580.126.20 is the driver of 3.15.1, available in the ParallelCluster artifacts bucket.
#   Fabric Manager and IMEX versions follow the driver version.
EFA_UPGRADE = {"version": "1.49.0", "sha256": "cf2e9281a2328a243c76f911a490faed43ca0fecfe4733c25e34b2e92a32c309"}
NVIDIA_DRIVER_UPGRADE = "580.126.20"
# OSes where the 3.14.2 EFA installer and NVIDIA driver still work, so no upgrade is applied
OSES_WITHOUT_DEPENDENCY_UPGRADE = ["alinux2", "rhel8", "rocky8"]
# Same Lustre client exclusions as test_build_image: these OSes release new kernels more often than Lustre supports
OSES_WITHOUT_LUSTRE = ["ubuntu2204", "ubuntu2404", "rhel9", "rocky9"]
# Known 3.14.2 build failures, used to classify the build failures of the "none" case
KNOWN_BUILD_FAILURES = {
    "go_module_cache": "module cache not found",
    "efa_installer": "failed to install EFA packages",
    "nvidia_driver": "nvidia.run advanced",
}


def _is_el(os):
    return re.match(r"^(rhel|rocky)\d+$", os) is not None


def _build_log_contains(image, region, text):
    # Search the whole log group: GetLogEvents pages read backwards can be empty even when events exist
    log_group_name = f"/aws/imagebuilder/ParallelClusterImage-{image.image_id}"
    paginator = boto3.client("logs", region_name=region).get_paginator("filter_log_events")
    pages = paginator.paginate(logGroupName=log_group_name, filterPattern=f'"{text}"')
    return any(page["events"] for page in pages)


def _test_baseline_build(image, region, os, output_dir):
    """Baseline without workarounds: the build must either complete or fail with a known 3.14.2 failure."""
    wait_for_image_build_status(image, ["BUILD_COMPLETE", "BUILD_FAILED"])
    export_image_logs(image, output_dir)
    if image.image_status == "BUILD_COMPLETE":
        logging.info("Baseline build of %s completed without workarounds", os)
        return
    keep_recent_image_logs(image)
    known_failures = [name for name, text in KNOWN_BUILD_FAILURES.items() if _build_log_contains(image, region, text)]
    logging.info("Baseline build of %s failed with known failures: %s", os, known_failures)
    assert_that(known_failures).described_as(f"known 3.14.2 build failures on {os}").is_not_empty()


# Each case takes ~2h, do not rerun on failure
@pytest.mark.flaky(reruns=0)
@pytest.mark.parametrize("workaround", ["none", "efs_utils_rpm", "go_fips_off"])
@pytest.mark.usefixtures("scheduler")
def test_build_image_efs_utils_workaround(
    workaround,
    region,
    os,
    instance,
    architecture,
    pcluster_config_reader,
    images_factory,
    efs_utils_workaround_parent_image,
    efs_stack_factory,
    efs_mount_target_stack_factory,
    clusters_factory,
    scheduler_commands_factory,
    test_datadir,
    request,
):
    """
    Validate the workarounds for 3.14.2 `pcluster build-image` failures (P558591845) with UpdateOsPackages enabled.

    - none: baseline, the official base AMI is used as is. The build must complete or fail with a known failure
      (Go module cache on EL, EFA installer on EL 9.8 and Ubuntu, NVIDIA kernel module on new kernels).
    - efs_utils_rpm (RHEL/Rocky): parent image with the prebuilt efs-utils RPM, the cookbook skips the source build.
    - go_fips_off (RHEL/Rocky): parent image with golang (versionlocked) and GOFIPS140=off.
    On all OSes but alinux2 and EL8, the workaround cases also upgrade EFA and the NVIDIA driver with
    ExtraChefAttributes. On OSes other than RHEL/Rocky, efs-utils needs no workaround: efs_utils_rpm only applies
    the upgrades and go_fips_off would be a duplicate of it, so it is skipped.

    For the workarounds, the build must complete including the validate phase (InSpec tests), and a cluster created
    from the image must mount EFS (with and without TLS/IAM) on head and compute nodes, with the expected
    efs-utils, NVIDIA driver and EFA installer versions.
    """
    is_el = _is_el(os)
    if not is_el and workaround == "go_fips_off":
        pytest.skip(f"No efs-utils workaround needed on {os}: same as the efs_utils_rpm case (upgrades only)")
    enable_nvidia = get_gpu_count(instance) > 0
    upgrade_dependencies = workaround != "none" and os not in OSES_WITHOUT_DEPENDENCY_UPGRADE

    base_ami = retrieve_latest_ami(region, os, ami_type="official", architecture=architecture)
    if workaround != "none" and is_el:
        parent_image = efs_utils_workaround_parent_image(workaround, base_ami)
    else:
        parent_image = base_ami

    extra_chef_attributes = ""
    if upgrade_dependencies:
        cluster_attributes = {"efa": EFA_UPGRADE}
        if enable_nvidia:
            cluster_attributes["nvidia"] = {"driver_version": NVIDIA_DRIVER_UPGRADE}
        extra_chef_attributes = json.dumps({"cluster": cluster_attributes})

    image_id = generate_stack_name(
        f"integ-tests-efs-wa-{workaround.replace('_', '-')}", request.config.getoption("stackname_suffix")
    )
    image_config = pcluster_config_reader(
        config_file="image.config.yaml",
        parent_image=parent_image,
        enable_nvidia=str(enable_nvidia).lower(),
        enable_lustre_client=str(os not in OSES_WITHOUT_LUSTRE).lower(),
        extra_chef_attributes=extra_chef_attributes,
    )
    image = images_factory(image_id, image_config, region)

    if workaround == "none":
        _test_baseline_build(image, region, os, request.config.getoption("output_dir"))
        return

    wait_for_build_image_complete(image, request.config.getoption("output_dir"))

    tls_mount_dir = "/efs_tls_iam"
    plain_mount_dir = "/efs_plain"
    existing_efs_id = efs_stack_factory()[0]
    efs_mount_target_stack_factory([existing_efs_id])
    cluster_config = pcluster_config_reader(
        custom_ami=image.ec2_image_id,
        tls_mount_dir=tls_mount_dir,
        plain_mount_dir=plain_mount_dir,
        existing_efs_id=existing_efs_id,
    )
    cluster = clusters_factory(cluster_config)
    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)

    storage_common.test_efs_correctly_mounted(remote_command_executor, tls_mount_dir, tls=True, iam=True)
    storage_common.test_efs_correctly_mounted(remote_command_executor, plain_mount_dir)
    for mount_dir in (tls_mount_dir, plain_mount_dir):
        storage_common.verify_directory_correctly_shared(remote_command_executor, mount_dir, scheduler_commands)

    if workaround == "efs_utils_rpm" and is_el:
        expected_efs_utils_version = EFS_UTILS_WORKAROUND_RPM_VERSION
    elif is_el or os.startswith("ubuntu"):
        expected_efs_utils_version = PCLUSTER_EFS_UTILS_VERSION  # built from source by the cookbook
    else:
        expected_efs_utils_version = "any"  # installed from the Amazon Linux repositories
    check_args = [
        workaround if is_el else "none",
        expected_efs_utils_version,
        (NVIDIA_DRIVER_UPGRADE if upgrade_dependencies else "any") if enable_nvidia else "none",
        EFA_UPGRADE["version"] if upgrade_dependencies else "any",
    ]
    check_script = str(test_datadir / "check_efs_utils_workaround.sh")
    remote_command_executor.run_remote_script(check_script, args=check_args)
    result = scheduler_commands.submit_script(check_script, script_args=check_args)
    job_id = scheduler_commands.assert_job_submitted(result.stdout)
    scheduler_commands.wait_job_completed(job_id)
    scheduler_commands.assert_job_succeeded(job_id)
