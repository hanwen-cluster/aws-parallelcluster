# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Check modules for the config-driven test framework.

Importing this package registers all built-in checks with the default registry.
"""

# Import all check modules so they self-register on import.
from framework.config_driven.checks import (  # noqa: F401
    base_checks,
    efa_checks,
    scheduler_checks,
    storage_checks,
)
