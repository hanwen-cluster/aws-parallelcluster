# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
from collections import namedtuple
from typing import Dict

from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as awslambda
from aws_cdk import aws_route53 as route53
from aws_cdk.core import CfnCustomResource, CfnDeletionPolicy, CfnOutput, CfnParameter, Construct, Fn, Stack

from pcluster.aws.aws_api import AWSApi
from pcluster.config.cluster_config import SlurmClusterConfig
from pcluster.constants import PCLUSTER_DYNAMODB_PREFIX
from pcluster.models.s3_bucket import S3Bucket
from pcluster.templates.cdk_builder_utils import (
    PclusterLambdaConstruct,
    add_lambda_cfn_role,
    create_hash_suffix,
    get_cloud_watch_logs_policy_statement,
)

CustomDns = namedtuple("CustomDns", ["ref", "name"])


class SlurmConstruct(Construct):
    """Create the resources required when using Slurm as a scheduler."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        stack_name: str,
        cluster_config: SlurmClusterConfig,
        bucket: S3Bucket,
        managed_head_node_instance_role: iam.CfnRole,
        managed_compute_instance_roles: Dict[str, iam.CfnRole],
        cleanup_lambda_role: iam.CfnRole,
        cleanup_lambda: awslambda.CfnFunction,
    ):
        super().__init__(scope, id)
        self.stack_scope = scope
        self.stack_name = stack_name
        self.config = cluster_config
        self.bucket = bucket
        self.cleanup_lambda_role = cleanup_lambda_role
        self.cleanup_lambda = cleanup_lambda
        self.managed_head_node_instance_role = managed_head_node_instance_role
        self.managed_compute_instance_roles = managed_compute_instance_roles

        self._add_parameters()
        self._add_resources()

    # -- Utility methods --------------------------------------------------------------------------------------------- #

    @property
    def _stack_region(self):
        return Stack.of(self).region

    @property
    def _stack_account(self):
        return Stack.of(self).account

    def _stack_unique_id(self):
        return Fn.select(2, Fn.split("/", Stack.of(self).stack_id))

    def _format_arn(self, **kwargs):
        return Stack.of(self).format_arn(**kwargs)

    # -- Parameters -------------------------------------------------------------------------------------------------- #

    def _add_parameters(self):
        if self._condition_custom_cluster_dns():
            domain_name = AWSApi.instance().route53.get_hosted_zone_domain_name(
                self.config.scheduling.settings.dns.hosted_zone_id
            )
        else:
            domain_name = "pcluster."
        cluster_dns_domain = f"{self.stack_name.lower()}.{domain_name}"

        self.cluster_dns_domain = CfnParameter(
            self.stack_scope,
            "ClusterDNSDomain",
            description="DNS Domain of the private hosted zone created within the cluster",
            default=cluster_dns_domain,
        )

    # -- Resources --------------------------------------------------------------------------------------------------- #

    def _add_resources(self):
        # DynamoDB to store cluster states
        self._add_dynamodb_table()

        # Add Slurm Policies to new instances roles
        if self.managed_head_node_instance_role:
            self._add_policies_to_head_node_role("HeadNode", self.managed_head_node_instance_role.ref)
        for queue_name, role in self.managed_compute_instance_roles.items():
            if role:
                self._add_policies_to_compute_node_role(queue_name, role.ref)

        self.cluster_hosted_zone = None
        if not self._condition_disable_cluster_dns():
            self.cluster_hosted_zone = self._add_private_hosted_zone()

    def _add_policies_to_compute_node_role(self, node_name, role):
        suffix = create_hash_suffix(node_name)

        policy_statements = [
            {
                "sid": "DynamoDBTableQuery",
                "effect": iam.Effect.ALLOW,
                "actions": ["dynamodb:Query"],
                "resources": [
                    self._format_arn(service="dynamodb", resource=f"table/{PCLUSTER_DYNAMODB_PREFIX}{self.stack_name}"),
                    self._format_arn(
                        service="dynamodb",
                        resource=f"table/{PCLUSTER_DYNAMODB_PREFIX}{self.stack_name}/index/*",
                    ),
                ],
            },
        ]
        policy_name = "parallelcluster-slurm-compute"
        iam.CfnPolicy(
            self.stack_scope,
            f"SlurmPolicies{suffix}",
            policy_name=policy_name,
            policy_document=iam.PolicyDocument(
                statements=[iam.PolicyStatement(**statement) for statement in policy_statements]
            ),
            roles=[role],
        )

    def _add_policies_to_head_node_role(self, node_name, role):
        suffix = create_hash_suffix(node_name)

        policy_statements = [
            {
                "sid": "DynamoDBTable",
                "actions": [
                    "dynamodb:PutItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:GetItem",
                ],
                "effect": iam.Effect.ALLOW,
                "resources": [
                    self._format_arn(service="dynamodb", resource=f"table/{PCLUSTER_DYNAMODB_PREFIX}{self.stack_name}")
                ],
            },
        ]
        policy_name = "parallelcluster-slurm-head-node"

        iam.CfnPolicy(
            self.stack_scope,
            f"SlurmPolicies{suffix}",
            policy_name=policy_name,
            policy_document=iam.PolicyDocument(
                statements=[iam.PolicyStatement(**statement) for statement in policy_statements]
            ),
            roles=[role],
        )

    def _add_dynamodb_table(self):
        table = dynamodb.CfnTable(
            self.stack_scope,
            "DynamoDBTable",
            table_name=PCLUSTER_DYNAMODB_PREFIX + self.stack_name,
            attribute_definitions=[
                dynamodb.CfnTable.AttributeDefinitionProperty(attribute_name="Id", attribute_type="S"),
                dynamodb.CfnTable.AttributeDefinitionProperty(attribute_name="InstanceId", attribute_type="S"),
            ],
            key_schema=[dynamodb.CfnTable.KeySchemaProperty(attribute_name="Id", key_type="HASH")],
            global_secondary_indexes=[
                dynamodb.CfnTable.GlobalSecondaryIndexProperty(
                    index_name="InstanceId",
                    key_schema=[dynamodb.CfnTable.KeySchemaProperty(attribute_name="InstanceId", key_type="HASH")],
                    projection=dynamodb.CfnTable.ProjectionProperty(projection_type="ALL"),
                )
            ],
            billing_mode="PAY_PER_REQUEST",
        )
        table.cfn_options.update_replace_policy = CfnDeletionPolicy.RETAIN
        table.cfn_options.deletion_policy = CfnDeletionPolicy.DELETE
        self.dynamodb_table = table

    def _add_private_hosted_zone(self):
        if self._condition_custom_cluster_dns():
            hosted_zone_id = self.config.scheduling.settings.dns.hosted_zone_id
            cluster_hosted_zone = CustomDns(ref=hosted_zone_id, name=self.cluster_dns_domain.value_as_string)
        else:
            cluster_hosted_zone = route53.CfnHostedZone(
                self.stack_scope,
                "Route53HostedZone",
                name=self.cluster_dns_domain.value_as_string,
                vpcs=[route53.CfnHostedZone.VPCProperty(vpc_id=self.config.vpc_id, vpc_region=self._stack_region)],
            )

        # If Headnode InstanceRole is created by ParallelCluster, add Route53 policy for InstanceRole
        if self.managed_head_node_instance_role:
            iam.CfnPolicy(
                self.stack_scope,
                "ParallelClusterSlurmRoute53Policies",
                policy_name="parallelcluster-slurm-route53",
                policy_document=iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="Route53Add",
                            effect=iam.Effect.ALLOW,
                            actions=["route53:ChangeResourceRecordSets"],
                            resources=[
                                self._format_arn(
                                    service="route53",
                                    region="",
                                    account="",
                                    resource=f"hostedzone/{cluster_hosted_zone.ref}",
                                ),
                            ],
                        ),
                    ]
                ),
                roles=[self.managed_head_node_instance_role.ref],
            )

        cleanup_route53_lambda_execution_role = None
        if self.cleanup_lambda_role:
            cleanup_route53_lambda_execution_role = add_lambda_cfn_role(
                scope=self.stack_scope,
                function_id="CleanupRoute53",
                statements=[
                    iam.PolicyStatement(
                        actions=["route53:ListResourceRecordSets", "route53:ChangeResourceRecordSets"],
                        effect=iam.Effect.ALLOW,
                        resources=[
                            self._format_arn(
                                service="route53",
                                region="",
                                account="",
                                resource=f"hostedzone/{cluster_hosted_zone.ref}",
                            ),
                        ],
                        sid="Route53DeletePolicy",
                    ),
                    get_cloud_watch_logs_policy_statement(
                        resource=self._format_arn(
                            service="logs",
                            account=self._stack_account,
                            region=self._stack_region,
                            resource="log-group:/aws/lambda/pcluster-CleanupRoute53-*",
                        )
                    ),
                ],
            )

        cleanup_route53_lambda = PclusterLambdaConstruct(
            scope=self.stack_scope,
            id="CleanupRoute53FunctionConstruct",
            function_id="CleanupRoute53",
            bucket=self.bucket,
            config=self.config,
            execution_role=cleanup_route53_lambda_execution_role.attr_arn
            if cleanup_route53_lambda_execution_role
            else self.config.iam.roles.lambda_functions_role,
            handler_func="cleanup_resources",
        ).lambda_func

        self.cleanup_route53_custom_resource = CfnCustomResource(
            self.stack_scope,
            "CleanupRoute53CustomResource",
            service_token=cleanup_route53_lambda.attr_arn,
        )
        self.cleanup_route53_custom_resource.add_property_override("ClusterHostedZone", cluster_hosted_zone.ref)
        self.cleanup_route53_custom_resource.add_property_override("Action", "DELETE_DNS_RECORDS")
        self.cleanup_route53_custom_resource.add_property_override("ClusterDNSDomain", cluster_hosted_zone.name)

        CfnOutput(
            self.stack_scope,
            "ClusterHostedZone",
            description="Id of the private hosted zone created within the cluster",
            value=cluster_hosted_zone.ref,
        )

        return cluster_hosted_zone

    # -- Conditions -------------------------------------------------------------------------------------------------- #

    def _condition_disable_cluster_dns(self):
        return (
            self.config.scheduling.settings
            and self.config.scheduling.settings.dns
            and self.config.scheduling.settings.dns.disable_managed_dns
        )

    def _condition_custom_cluster_dns(self):
        return (
            self.config.scheduling.settings
            and self.config.scheduling.settings.dns
            and self.config.scheduling.settings.dns.hosted_zone_id
        )
