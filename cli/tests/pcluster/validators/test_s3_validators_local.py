import time
from collections import defaultdict

import boto3
import pytest

from cli.build.lib.pcluster.constants import SUPPORTED_OSES
from pcluster.aws.common import AWSClientError
from pcluster.validators.s3_validators import S3BucketRegionValidator, S3BucketUriValidator, UrlValidator
from tests.pcluster.validators.utils import assert_failure_messages
import datetime


def test_url_validator():
    dynamodb_client = boto3.client("dynamodb", region_name="us-east-1")
    current_time = int(time.time())
    one_month_ago = current_time - (300 * 24 * 60 * 60)

    filter_expression = "#call_start_time >= :one_month_ago"
    expression_attribute_values = {":one_month_ago": {"N": str(one_month_ago)}}
    all_items = []
    last_evaluated_key = None
    while True:
        projection_expression = (
            "#status, #avg_launch, #max_launch, #min_launch, #creation_time, #name, #os, #start_time"
        )
        expression_attribute_names = {
            "#call_start_time": "call_start_time",
            "#status": "call_status",
            "#avg_launch": "compute_average_launch_time",
            "#max_launch": "compute_max_launch_time",
            "#min_launch": "compute_min_launch_time",
            "#creation_time": "cluster_creation_time",
            "#name": "name",
            "#os": "os",
            "#start_time": "call_start_time",
        }
        # Parameters for the scan operation
        scan_params = {
            "TableName": "ParallelCluster-IntegTest-Metadata",
            "ProjectionExpression": projection_expression,
            "FilterExpression": filter_expression,
            "ExpressionAttributeNames": expression_attribute_names,
            "ExpressionAttributeValues": expression_attribute_values,
        }

        # Add ExclusiveStartKey if we're not on the first iteration
        if last_evaluated_key:
            scan_params["ExclusiveStartKey"] = last_evaluated_key

        response = dynamodb_client.scan(**scan_params)
        all_items.extend(response.get("Items", []))

        # Check if there are more items to fetch
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    all_items.sort(key=lambda x: x["call_start_time"]["N"], reverse=True)
    result = defaultdict(dict)
    for category_name in ["os", "name"]:
        category_name_processing = None
        if category_name == "name":
            category_name_processing = _remove_os_from_string
        for statistics_name in [
            "cluster_creation_time",
            "compute_average_launch_time",
            "compute_min_launch_time",
            "compute_max_launch_time",
        ]:
            if statistics_name in ["cluster_creation_time", "compute_average_launch_time"]:
                statistics_processing = _mean
            elif statistics_name in ["compute_max_launch_time"]:
                statistics_processing = max
            else:
                statistics_processing = min
            result[statistics_name][category_name] = _get_statistics_by_category(
                all_items,
                category_name,
                statistics_name,
                category_name_processing=category_name_processing,
                statistics_processing=statistics_processing,
            )
    print(all_items)


def _mean(x):
    return sum(x) / len(x)


def _remove_os_from_string(x):
    for os in SUPPORTED_OSES:
        x = x.replace(os, "")
    return x


def _get_statistics_by_category(
    all_items, category_name, statistics_name, category_name_processing=None, statistics_processing=None
):
    more_data = True
    lastest_time = float(all_items[0]["call_start_time"]["N"])
    window_length = 8
    result = {}
    while more_data:
        more_data = False
        os_cluster_creation_times = {}
        for item in all_items:
            if item["call_status"]["S"] != "passed":
                continue
            if statistics_name not in item:
                continue
            if float(item["call_start_time"]["N"]) < lastest_time - (window_length * 24 * 60 * 60):
                more_data = True
                continue
            if float(item["call_start_time"]["N"]) > lastest_time:
                continue
            cluster_creation_time = item[statistics_name]["N"]
            if cluster_creation_time == "0":
                continue
            os = item[category_name]["S"]
            if category_name_processing:
                os = category_name_processing(os)
            if os not in os_cluster_creation_times:
                os_cluster_creation_times[os] = [float(cluster_creation_time)]
            else:
                os_cluster_creation_times[os].append(float(cluster_creation_time))
        for os, cluster_creation_times in os_cluster_creation_times.items():
            if os not in result:
                result[os] = []
            os_time_key = f"{os}-time"
            if os_time_key not in result:
                result[os_time_key] = []
            result[os].insert(0, sum(cluster_creation_times) / len(cluster_creation_times))
            result[os_time_key].insert(0, datetime.datetime.fromtimestamp(lastest_time).strftime("%Y-%m-%d"))
        if os_cluster_creation_times:
            more_data = True
        lastest_time = lastest_time - 24 * 60 * 60
        print(lastest_time)

    plot_statistics(result, statistics_name)
    return result
    # return sorted(result.items(), key=lambda x: x[1], reverse=True)

import matplotlib.pyplot as plt
def plot_statistics(result, statistics_name):
    plt.figure(figsize=(12, 6))

    # Create x-axis values (assuming each point represents a day)
    for category, values in result.items():
        if "-time" in category:
            continue
        x_values = result[f"{category}-time"]
        plt.plot(x_values, values, marker='o', label=category)

    plt.title(statistics_name)
    plt.xlabel('Latest timestamp')
    plt.ylabel('Average Creation Time')
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.legend()

    # Rotate x-axis labels for better readability
    plt.xticks(rotation=45)

    # Adjust layout to prevent label cutoff
    plt.tight_layout()

    plt.show()

def _get_launch_time(logs, instance_id):
    for log in logs:
        if instance_id in log["message"]:
            return log["timestamp"]


@pytest.mark.parametrize(
    "url, expected_message",
    [
        ("s3://test/test1/test2", None),
        ("http://test/test.json", "is not a valid S3 URI"),
    ],
)
def test_s3_bucket_uri_validator(mocker, url, expected_message, aws_api_mock):
    aws_api_mock.s3.head_bucket.return_value = True
    actual_failures = S3BucketUriValidator().execute(url=url)
    assert_failure_messages(actual_failures, expected_message)
    if url.startswith("s3://"):
        aws_api_mock.s3.head_bucket.assert_called()


@pytest.mark.parametrize(
    "bucket, bucket_region, cluster_region, expected_message",
    [
        ("bucket", "us-east-1", "us-east-1", None),
        ("bucket", "us-west-1", "us-west-1", None),
        ("bucket", "eu-west-1", "us-east-1", "cannot be used because it is not in the same region of the cluster."),
    ],
)
def test_s3_bucket_region_validator(mocker, bucket, bucket_region, cluster_region, expected_message, aws_api_mock):
    aws_api_mock.s3.get_bucket_region.return_value = bucket_region
    actual_failures = S3BucketRegionValidator().execute(bucket=bucket, region=cluster_region)
    assert_failure_messages(actual_failures, expected_message)
    aws_api_mock.s3.get_bucket_region.assert_called_with(bucket)
