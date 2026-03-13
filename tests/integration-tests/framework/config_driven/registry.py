# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Check registry — maps config features + runtime context to validation checks."""

import logging
from typing import List

from framework.config_driven.models import Check, ClusterFeatures, RuntimeContext


class CheckRegistry:
    """Central registry of all available checks.

    Checks are registered at import time via ``register()``. At test time,
    ``select_checks()`` filters to only those whose condition is met.
    """

    def __init__(self):
        self._checks: List[Check] = []

    def register(self, check: Check):
        """Add a check to the registry."""
        self._checks.append(check)
        logging.debug("Registered check: %s [%s]", check.name, check.category)

    def select_checks(
        self, features: ClusterFeatures, runtime: RuntimeContext
    ) -> List[Check]:
        """Return checks whose conditions are satisfied, sorted by phase."""
        selected = []
        for check in self._checks:
            try:
                if check.condition(features, runtime):
                    selected.append(check)
                    logging.info("Check selected: %s", check.name)
                else:
                    logging.info("Check skipped (condition not met): %s", check.name)
            except Exception:
                logging.exception("Error evaluating condition for check %s", check.name)

        selected.sort(key=lambda c: c.phase)
        return selected

    @property
    def all_checks(self) -> List[Check]:
        return list(self._checks)


# Module-level singleton — checks register themselves here on import.
default_registry = CheckRegistry()
