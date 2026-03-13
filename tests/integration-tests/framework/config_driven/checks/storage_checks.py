# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Storage checks: EBS mounts, EFS mounts, FSx Lustre, RAID configuration.

These checks reuse validation logic from tests.storage.storage_common where possible,
making the functions callable from both the old test functions and the new framework.
"""

import logging

from assertpy import assert_that

from framework.config_driven.models import Check, CheckContext, Phase
from framework.config_driven.registry import default_registry


# ---------------------------------------------------------------------------
# EBS Mount
# ---------------------------------------------------------------------------

def check_ebs_mounts(ctx: CheckContext):
    """Verify all EBS volumes are correctly mounted at their configured mount dirs."""
    ebs_mounts = [m for m in ctx.features.storage_mounts if m.storage_type == "Ebs" and m.raid_type is None]
    for mount in ebs_mounts:
        logging.info("Checking EBS mount at %s", mount.mount_dir)
        result = ctx.remote_command_executor.run_remote_command(
            f"df -h -t ext4 | tail -n +2 | awk '{{print $2, $6}}' | grep '{mount.mount_dir}'"
        )
        assert_that(result.stdout).contains(mount.mount_dir)

        # Verify fstab entry
        result = ctx.remote_command_executor.run_remote_command("cat /etc/fstab")
        assert_that(result.stdout).contains(mount.mount_dir)
        logging.info("EBS mount at %s verified", mount.mount_dir)


default_registry.register(Check(
    name="EBS Mounts",
    category="storage",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: any(
        m.storage_type == "Ebs" and m.raid_type is None for m in f.storage_mounts
    ),
    run=check_ebs_mounts,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# RAID Configuration
# ---------------------------------------------------------------------------

def check_raid_configuration(ctx: CheckContext):
    """Verify RAID arrays are correctly configured and mounted."""
    raid_mounts = [m for m in ctx.features.storage_mounts if m.raid_type is not None]
    for mount in raid_mounts:
        logging.info("Checking RAID at %s (type=%s, devices=%s)", mount.mount_dir, mount.raid_type, mount.raid_devices)

        # Check mdadm RAID status
        result = ctx.remote_command_executor.run_remote_command("sudo mdadm --detail /dev/md0")
        assert_that(result.stdout).contains(f"Raid Level : raid{mount.raid_type}")
        if mount.raid_devices:
            assert_that(result.stdout).contains(f"Raid Devices : {mount.raid_devices}")

        # Check mount
        result = ctx.remote_command_executor.run_remote_command(f"mount | grep '{mount.mount_dir}'")
        assert_that(result.stdout).contains(mount.mount_dir)
        logging.info("RAID at %s verified", mount.mount_dir)


default_registry.register(Check(
    name="RAID Configuration",
    category="storage",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.has_raid,
    run=check_raid_configuration,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# EFS Mount
# ---------------------------------------------------------------------------

def check_efs_mounts(ctx: CheckContext):
    """Verify EFS file systems are mounted at their configured mount dirs."""
    efs_mounts = [m for m in ctx.features.storage_mounts if m.storage_type == "Efs"]
    for mount in efs_mounts:
        logging.info("Checking EFS mount at %s", mount.mount_dir)
        result = ctx.remote_command_executor.run_remote_command(f"mount | grep '{mount.mount_dir}'")
        assert_that(result.stdout).contains("nfs")
        assert_that(result.stdout).contains(mount.mount_dir)
        logging.info("EFS mount at %s verified", mount.mount_dir)


default_registry.register(Check(
    name="EFS Mounts",
    category="storage",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.has_efs,
    run=check_efs_mounts,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# FSx Lustre Mount
# ---------------------------------------------------------------------------

def check_fsx_lustre_mounts(ctx: CheckContext):
    """Verify FSx Lustre file systems are mounted at their configured mount dirs."""
    fsx_mounts = [m for m in ctx.features.storage_mounts if m.storage_type == "FsxLustre"]
    for mount in fsx_mounts:
        logging.info("Checking FSx Lustre mount at %s", mount.mount_dir)
        result = ctx.remote_command_executor.run_remote_command(f"mount | grep '{mount.mount_dir}'")
        assert_that(result.stdout).contains("lustre")
        assert_that(result.stdout).contains(mount.mount_dir)
        logging.info("FSx Lustre mount at %s verified", mount.mount_dir)


default_registry.register(Check(
    name="FSx Lustre Mounts",
    category="storage",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: f.has_fsx_lustre,
    run=check_fsx_lustre_mounts,
    depends_on=["SSH Connectivity"],
))


# ---------------------------------------------------------------------------
# Shared Storage Write Test
# ---------------------------------------------------------------------------

def check_shared_storage_writable(ctx: CheckContext):
    """Verify each shared storage mount is writable from the head node."""
    for mount in ctx.features.storage_mounts:
        logging.info("Testing write to %s", mount.mount_dir)
        test_file = f"{mount.mount_dir}/.config_driven_test_write"
        ctx.remote_command_executor.run_remote_command(
            f"echo 'config-driven-test' > {test_file} && cat {test_file} && rm -f {test_file}"
        )
        logging.info("Write test passed for %s", mount.mount_dir)


default_registry.register(Check(
    name="Shared Storage Writable",
    category="storage",
    phase=Phase.FUNCTIONAL,
    condition=lambda f, r: len(f.storage_mounts) > 0,
    run=check_shared_storage_writable,
    depends_on=["SSH Connectivity"],
))
