# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at http://aws.amazon.com/apache2.0/
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

from datetime import date, datetime  # noqa: F401
from typing import Dict, List  # noqa: F401

from pcluster.api import util
from pcluster.api.models.base_model_ import Model
from pcluster.api.models.login_nodes_state import LoginNodesState  # noqa: E501


class LoginNodesPool(Model):
    """NOTE: This class is auto generated by OpenAPI Generator (https://openapi-generator.tech).

    Do not edit the class manually.
    """

    def __init__(
        self, status=None, pool_name=None, address=None, scheme=None, healthy_nodes=None, unhealthy_nodes=None
    ):  # noqa: E501
        """LoginNodesPool - a model defined in OpenAPI

        :param status: The status of this LoginNodesPool.  # noqa: E501
        :type status: LoginNodesState
        :param pool_name: The pool_name of this LoginNodesPool.  # noqa: E501
        :type pool_name: str
        :param address: The address of this LoginNodesPool.  # noqa: E501
        :type address: str
        :param scheme: The scheme of this LoginNodesPool.  # noqa: E501
        :type scheme: str
        :param healthy_nodes: The healthy_nodes of this LoginNodesPool.  # noqa: E501
        :type healthy_nodes: int
        :param unhealthy_nodes: The unhealthy_nodes of this LoginNodesPool.  # noqa: E501
        :type unhealthy_nodes: int
        """
        self.openapi_types = {
            "status": LoginNodesState,
            "pool_name": str,
            "address": str,
            "scheme": str,
            "healthy_nodes": int,
            "unhealthy_nodes": int,
        }

        self.attribute_map = {
            "status": "status",
            "pool_name": "poolName",
            "address": "address",
            "scheme": "scheme",
            "healthy_nodes": "healthyNodes",
            "unhealthy_nodes": "unhealthyNodes",
        }

        self._status = status
        self._pool_name = pool_name
        self._address = address
        self._scheme = scheme
        self._healthy_nodes = healthy_nodes
        self._unhealthy_nodes = unhealthy_nodes

    @classmethod
    def from_dict(cls, dikt) -> "LoginNodesPool":
        """Returns the dict as a model

        :param dikt: A dict.
        :type: dict
        :return: The LoginNodesPool of this LoginNodesPool.  # noqa: E501
        :rtype: LoginNodesPool
        """
        return util.deserialize_model(dikt, cls)

    @property
    def status(self):
        """Gets the status of this LoginNodesPool.


        :return: The status of this LoginNodesPool.
        :rtype: LoginNodesState
        """
        return self._status

    @status.setter
    def status(self, status):
        """Sets the status of this LoginNodesPool.


        :param status: The status of this LoginNodesPool.
        :type status: LoginNodesState
        """
        if status is None:
            raise ValueError("Invalid value for `status`, must not be `None`")  # noqa: E501

        self._status = status

    @property
    def pool_name(self):
        """Gets the pool_name of this LoginNodesPool.


        :return: The pool_name of this LoginNodesPool.
        :rtype: str
        """
        return self._pool_name

    @pool_name.setter
    def pool_name(self, pool_name):
        """Sets the pool_name of this LoginNodesPool.


        :param pool_name: The pool_name of this LoginNodesPool.
        :type pool_name: str
        """

        self._pool_name = pool_name

    @property
    def address(self):
        """Gets the address of this LoginNodesPool.


        :return: The address of this LoginNodesPool.
        :rtype: str
        """
        return self._address

    @address.setter
    def address(self, address):
        """Sets the address of this LoginNodesPool.


        :param address: The address of this LoginNodesPool.
        :type address: str
        """

        self._address = address

    @property
    def scheme(self):
        """Gets the scheme of this LoginNodesPool.


        :return: The scheme of this LoginNodesPool.
        :rtype: str
        """
        return self._scheme

    @scheme.setter
    def scheme(self, scheme):
        """Sets the scheme of this LoginNodesPool.


        :param scheme: The scheme of this LoginNodesPool.
        :type scheme: str
        """

        self._scheme = scheme

    @property
    def healthy_nodes(self):
        """Gets the healthy_nodes of this LoginNodesPool.


        :return: The healthy_nodes of this LoginNodesPool.
        :rtype: int
        """
        return self._healthy_nodes

    @healthy_nodes.setter
    def healthy_nodes(self, healthy_nodes):
        """Sets the healthy_nodes of this LoginNodesPool.


        :param healthy_nodes: The healthy_nodes of this LoginNodesPool.
        :type healthy_nodes: int
        """

        self._healthy_nodes = healthy_nodes

    @property
    def unhealthy_nodes(self):
        """Gets the unhealthy_nodes of this LoginNodesPool.


        :return: The unhealthy_nodes of this LoginNodesPool.
        :rtype: int
        """
        return self._unhealthy_nodes

    @unhealthy_nodes.setter
    def unhealthy_nodes(self, unhealthy_nodes):
        """Sets the unhealthy_nodes of this LoginNodesPool.


        :param unhealthy_nodes: The unhealthy_nodes of this LoginNodesPool.
        :type unhealthy_nodes: int
        """

        self._unhealthy_nodes = unhealthy_nodes
