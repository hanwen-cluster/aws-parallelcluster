# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import hashlib
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import boto3
from assertpy import assert_that
from retrying import retry
from time_utils import minutes, seconds
from utils import describe_cluster_instances

_INSTALLER_REGION = "us-east-1"
_INSTALLER_BUCKET = "aws-parallelcluster-dev-build-dependencies"
_INSTALLER_KEY = "install_software.sh"
_COMPUTE_INSTANCE_STATES = ["pending", "running", "shutting-down", "stopping", "stopped"]
_COMPUTE_FLEET_TERMINAL_STATES = {"RUNNING", "STOPPED", "PROTECTED"}
_CAPACITY_WAIT_MINUTES = 15
# Login nodes are drained before they are terminated, and loginmgtd waits out the pool's grace time period (10 minutes
# by default) before it lets the Auto Scaling group reclaim them, so a pool routinely takes over 10 minutes and has
# been measured at more than 15 to disappear. This wait therefore gets a budget of its own.
_TERMINATION_WAIT_MINUTES = 30
_PARTITION_WAIT_MINUTES = 10
_STATE_CHECK_RESERVATION = "slurm-upgrade-state-check"
# The reservation only has to be persisted in StateSaveLocation, so keep it far in the future:
# an active maintenance reservation would take nodes out of the scheduler for the rest of the test.
_STATE_CHECK_RESERVATION_START = "now+7days"
# The name above must stay outside the "pcluster-" namespace: clustermgtd's capacity block manager deletes every
# reservation whose name starts with that prefix and does not belong to a Capacity Block in the cluster
# configuration, which used to make this reservation disappear a couple of minutes after the compute fleet came
# back up, without a trace in slurmctld.log.
_CLUSTERMGTD_LOG = "/var/log/parallelcluster/clustermgtd"
_SLURM_VERSION_COMMANDS = ("sinfo --version", "/opt/slurm/sbin/slurmdbd -V", "sacctmgr --version")
_SLURM_CONF = "/opt/slurm/etc/slurm.conf"
_SLURMCTLD_LOG = "/var/log/slurmctld.log"
_INSTALL_BACKUP_GLOB = "/opt/slurm_backup_*.tar.gz"
_STATE_BACKUP_GLOB = "/opt/slurm_state_backup_*.tar.gz"


@lru_cache(maxsize=None)
def _download_artifact(key, suffix, mode):
    """Download an artifact bucket object once per session and return the local path it was cached at."""
    file_descriptor, downloaded_path = tempfile.mkstemp(prefix="pcluster-software-installer-", suffix=suffix)
    os.close(file_descriptor)
    try:
        logging.info("Downloading s3://%s/%s in region %s", _INSTALLER_BUCKET, key, _INSTALLER_REGION)
        boto3.client("s3", region_name=_INSTALLER_REGION).download_file(_INSTALLER_BUCKET, key, downloaded_path)
        os.chmod(downloaded_path, mode)
    except Exception as error:
        raise RuntimeError(
            f"Failed to download s3://{_INSTALLER_BUCKET}/{key} in region {_INSTALLER_REGION}: {error}"
        ) from error

    # The digest identifies which revision of a mutable bucket object a run actually used, which the object key
    # alone does not: the installer script is overwritten in place whenever it is fixed.
    digest = hashlib.sha256(Path(downloaded_path).read_bytes()).hexdigest()
    logging.info("Downloaded s3://%s/%s (SHA-256: %s)", _INSTALLER_BUCKET, key, digest)
    return downloaded_path


def _download_software_installer_script():
    return _download_artifact(_INSTALLER_KEY, ".sh", 0o700)


def _installer_constant(script_text, name, substitutions=None):
    """Return a constant declared by the installer script, so that the tests never restate its values."""
    match = re.search(rf'^(?:readonly\s+)?{name}="([^"]*)"', script_text, re.MULTILINE)
    assert_that(match).described_as(f"declaration of {name} in the software installer script").is_not_none()
    value = match.group(1)
    for variable, replacement in (substitutions or {}).items():
        value = value.replace(f"${{{variable}}}", replacement)
    return value


def _source_archive_location(script_path):
    """Return the S3 key of the Slurm source archive and the path the installer expects it at on the host."""
    script_text = Path(script_path).read_text(encoding="utf-8")
    source_name = _installer_constant(script_text, "TARGET_SOURCE_NAME")
    cache_dir = _installer_constant(script_text, "CACHE_DIR")
    key = _installer_constant(script_text, "DEFAULT_ARCHIVE_KEY", {"TARGET_SOURCE_NAME": source_name})
    path = _installer_constant(
        script_text, "ARCHIVE_PATH", {"CACHE_DIR": cache_dir, "TARGET_SOURCE_NAME": source_name}
    )
    return key, path


def _stage_source_archive(executor, script_path):
    """Upload the Slurm source archive to a host that has no read access to the artifact bucket.

    The installer downloads the archive itself, which every cluster node can do because the cluster configuration
    grants it. An external slurmdbd instance is not a cluster node: its instance role comes from the customer-facing
    CloudFormation template, which grants S3 access to its own bucket only and takes no parameter to extend that. So
    the archive travels over the same SSH connection as the installer, on the test runner's credentials, and the
    installer picks up an archive that is already in place instead of downloading one.
    """
    archive_key, remote_path = _source_archive_location(script_path)
    local_path = _download_artifact(archive_key, ".tar.gz", 0o600)
    remote_name = os.path.basename(remote_path)
    logging.info("Staging s3://%s/%s at %s on the target host", _INSTALLER_BUCKET, archive_key, remote_path)
    executor.run_remote_command(
        f"sudo install -o root -g root -m 644 {remote_name} {remote_path} && rm -f {remote_name}",
        additional_files={local_path: remote_name},
        hide=True,
    )


def _compute_instance_ids(cluster):
    """Return the IDs of the compute instances of a cluster that are not gone yet.

    Instances that are shutting down still count as consumers: they keep the shared /opt/slurm of the head node
    mounted, and their slurmd is still running the version the installer is about to replace.
    """
    instances = describe_cluster_instances(
        cluster.cfn_name,
        cluster.region,
        filter_by_node_type="Compute",
        filter_by_instance_states=_COMPUTE_INSTANCE_STATES,
    )
    return sorted(instance["InstanceId"] for instance in instances)


def _describe_login_asg(snapshot, asg_snapshot):
    """Return the Auto Scaling group of a login pool, checking it is the group the pool owns.

    The group name is built out of the cluster and pool names rather than read from the cluster, so the
    parallelcluster:login-nodes-pool tag is what confirms the guess landed on the right group before its capacity
    is changed.
    """
    asg_name = asg_snapshot["AutoScalingGroupName"]
    groups = snapshot["autoscaling"].describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])[
        "AutoScalingGroups"
    ]
    if len(groups) != 1 or groups[0]["AutoScalingGroupName"] != asg_name:
        raise RuntimeError(f"Expected exactly one login-node Auto Scaling group named {asg_name}, found {len(groups)}")

    group = groups[0]
    tag_values = [tag["Value"] for tag in group.get("Tags", []) if tag["Key"] == "parallelcluster:login-nodes-pool"]
    if tag_values != [asg_snapshot["pool_name"]]:
        raise RuntimeError(
            f"Auto Scaling group {asg_name} has unexpected parallelcluster:login-nodes-pool tag values "
            f"{tag_values}; expected [{asg_snapshot['pool_name']}]"
        )
    return group


def _snapshot_cluster(cluster):
    snapshot = {
        "cluster": cluster,
        "autoscaling": boto3.client("autoscaling", region_name=cluster.region),
        "login_asgs": [],
    }
    compute_status = cluster.describe_compute_fleet()["status"]
    if compute_status not in _COMPUTE_FLEET_TERMINAL_STATES:
        raise RuntimeError(
            f"Cluster {cluster.name} compute fleet must be in RUNNING, STOPPED, or PROTECTED state before maintenance; "
            f"found {compute_status}"
        )
    snapshot["compute_status"] = compute_status

    for pool in cluster.config.get("LoginNodes", {}).get("Pools", []):
        asg_snapshot = {
            "pool_name": pool["Name"],
            "AutoScalingGroupName": f"{cluster.name}-{pool['Name']}-AutoScalingGroup",
        }
        group = _describe_login_asg(snapshot, asg_snapshot)
        asg_snapshot.update({key: group[key] for key in ("MinSize", "MaxSize", "DesiredCapacity")})
        snapshot["login_asgs"].append(asg_snapshot)

    logging.info(
        "Snapshotted cluster %s in %s: compute fleet %s, %d login pools",
        cluster.name,
        cluster.region,
        compute_status,
        len(snapshot["login_asgs"]),
    )
    return snapshot


def _wait_for_compute_status(snapshot, expected_status):
    cluster = snapshot["cluster"]

    @retry(
        wait_fixed=seconds(15),
        stop_max_delay=minutes(_CAPACITY_WAIT_MINUTES),
        retry_on_result=lambda status: status != expected_status,
    )
    def _poll():
        status = cluster.describe_compute_fleet()["status"]
        logging.info("Cluster %s compute fleet status is %s; waiting for %s", cluster.name, status, expected_status)
        return status

    return _poll()


def _login_asg_instance_ids(snapshot, asg_snapshot, lifecycle_state=None):
    """Return the instances of a login pool's Auto Scaling group, optionally only those in a given state."""
    instances = _describe_login_asg(snapshot, asg_snapshot).get("Instances", [])
    return sorted(
        instance["InstanceId"]
        for instance in instances
        if lifecycle_state is None
        or (instance["LifecycleState"] == lifecycle_state and instance["HealthStatus"] == "Healthy")
    )


def _wait_for_consumers_terminated(snapshots):
    @retry(
        wait_fixed=seconds(15),
        stop_max_delay=minutes(_TERMINATION_WAIT_MINUTES),
        retry_on_result=lambda consumers_remain: consumers_remain,
    )
    def _poll():
        consumers_remain = False
        for snapshot in snapshots:
            compute_instance_ids = _compute_instance_ids(snapshot["cluster"])
            if compute_instance_ids:
                consumers_remain = True
                logging.info(
                    "Waiting for cluster %s compute instances to terminate: %s",
                    snapshot["cluster"].name,
                    compute_instance_ids,
                )

            for asg_snapshot in snapshot["login_asgs"]:
                login_instance_ids = _login_asg_instance_ids(snapshot, asg_snapshot)
                if login_instance_ids:
                    consumers_remain = True
                    logging.info(
                        "Waiting for login Auto Scaling group %s instances to terminate: %s",
                        asg_snapshot["AutoScalingGroupName"],
                        login_instance_ids,
                    )
        return consumers_remain

    _poll()


def _pause_consumers(snapshots):
    for snapshot in snapshots:
        if snapshot["compute_status"] != "STOPPED":
            # A PROTECTED fleet is stopped as well: protected mode disables the queues but leaves the compute
            # instances of the healthy compute resources running, and those keep the old Slurm mounted.
            logging.info("Requesting compute fleet stop for cluster %s", snapshot["cluster"].name)
            snapshot["cluster"].stop()

    for snapshot in snapshots:
        for asg_snapshot in snapshot["login_asgs"]:
            logging.info("Scaling login Auto Scaling group %s to zero", asg_snapshot["AutoScalingGroupName"])
            snapshot["autoscaling"].update_auto_scaling_group(
                AutoScalingGroupName=asg_snapshot["AutoScalingGroupName"],
                MinSize=0,
                DesiredCapacity=0,
            )

    for snapshot in snapshots:
        if snapshot["compute_status"] != "STOPPED":
            _wait_for_compute_status(snapshot, "STOPPED")
    _wait_for_consumers_terminated(snapshots)
    logging.info("All non-head-node consumers are stopped")


def _wait_for_login_asg_restored(snapshot, asg_snapshot):
    desired_capacity = asg_snapshot["DesiredCapacity"]

    @retry(
        wait_fixed=seconds(15),
        stop_max_delay=minutes(_CAPACITY_WAIT_MINUTES),
        retry_on_result=lambda instance_ids: len(instance_ids) < desired_capacity,
    )
    def _poll():
        instance_ids = _login_asg_instance_ids(snapshot, asg_snapshot, lifecycle_state="InService")
        logging.info(
            "Login Auto Scaling group %s has %d/%d instances in service",
            asg_snapshot["AutoScalingGroupName"],
            len(instance_ids),
            desired_capacity,
        )
        return instance_ids

    _poll()


def _restore_consumers(snapshots):
    errors = []

    for snapshot in snapshots:
        for asg_snapshot in snapshot["login_asgs"]:
            logging.info(
                "Restoring login Auto Scaling group %s capacity to min=%d max=%d desired=%d",
                asg_snapshot["AutoScalingGroupName"],
                asg_snapshot["MinSize"],
                asg_snapshot["MaxSize"],
                asg_snapshot["DesiredCapacity"],
            )
            snapshot["autoscaling"].update_auto_scaling_group(
                AutoScalingGroupName=asg_snapshot["AutoScalingGroupName"],
                MinSize=asg_snapshot["MinSize"],
                MaxSize=asg_snapshot["MaxSize"],
                DesiredCapacity=asg_snapshot["DesiredCapacity"],
            )

        if snapshot["compute_status"] == "RUNNING":
            try:
                snapshot["cluster"].start()
            except Exception as error:
                errors.append((f"start compute fleet for cluster {snapshot['cluster'].name}", error))
        elif snapshot["compute_status"] != "STOPPED":
            # PROTECTED is not a status the compute fleet API accepts, so it cannot be requested back. Leaving the
            # fleet STOPPED is reported here rather than silently, because it changes what the caller sees next.
            logging.warning(
                "Cluster %s compute fleet was %s before the maintenance and is left STOPPED",
                snapshot["cluster"].name,
                snapshot["compute_status"],
            )

    for snapshot in snapshots:
        if snapshot["compute_status"] == "RUNNING":
            try:
                _wait_for_compute_status(snapshot, "RUNNING")
            except Exception as error:
                errors.append((f"wait for cluster {snapshot['cluster'].name} compute fleet RUNNING", error))

        for asg_snapshot in snapshot["login_asgs"]:
            try:
                _wait_for_login_asg_restored(snapshot, asg_snapshot)
            except Exception as error:
                errors.append((f"wait for login Auto Scaling group {asg_snapshot['AutoScalingGroupName']}", error))

    return errors


@contextmanager
def stopped_shared_slurm_consumers(*clusters):
    """Temporarily stop all compute fleets and login pools shared by a maintenance operation."""
    snapshots = []
    mutation_started = False
    primary_error = None
    try:
        # A cluster passed twice, for example because two head nodes share an external slurmdbd, must only be
        # snapshotted and restored once.
        unique_clusters = {(cluster.name, cluster.region): cluster for cluster in clusters}.values()
        snapshots = [_snapshot_cluster(cluster) for cluster in unique_clusters]
        mutation_started = True
        _pause_consumers(snapshots)
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if mutation_started:
            restoration_errors = _restore_consumers(snapshots)
            if restoration_errors:
                details = "\n".join(f"- {operation}: {error}" for operation, error in restoration_errors)
                message = f"Failed to fully restore cluster consumers:\n{details}"
                if primary_error is not None:
                    logging.error("%s; preserving the original failure: %s", message, primary_error)
                else:
                    raise RuntimeError(message)


def get_slurm_version(executor):
    """Return the Slurm version reported by the target host, or None when no Slurm binary answers.

    The installer is opaque, so without this the test logs never record which Slurm versions a run
    actually exercised, and a cross-major upgrade is indistinguishable from a no-op reinstall.
    """
    for command in _SLURM_VERSION_COMMANDS:
        result = executor.run_remote_command(command, raise_on_error=False, hide=True)
        if result.return_code == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    return None


def slurm_major_version(version):
    """Return the major.minor Slurm release of a version string such as "slurm 24.11.6", or None."""
    match = re.search(r"\d+\.\d+", version or "")
    return match.group(0) if match else None


def _assert_backup_archive_readable(executor, pattern):
    """Assert the newest archive matching a glob exists and can be listed by tar."""
    newest = executor.run_remote_command(f"ls -1t {pattern} | head -n 1", raise_on_error=False, hide=True)
    assert_that(newest.return_code).described_as(
        f"the installer left no backup archive matching {pattern}: {newest.stderr}"
    ).is_equal_to(0)
    archive = newest.stdout.strip()
    assert_that(archive).described_as(f"newest backup archive matching {pattern}").is_not_empty()

    # Listing the archive proves it is a valid gzip stream with contents, which "the file exists" does not: a
    # backup truncated by a full disk looks identical until someone actually needs to restore it.
    listing = executor.run_remote_command(f"sudo tar -tzf {archive} | head -n 5", raise_on_error=False, hide=True)
    assert_that(listing.stdout.strip()).described_as(
        f"contents of backup archive {archive}: {listing.stderr}"
    ).is_not_empty()
    logging.info("Verified upgrade backup archive %s is readable", archive)


def assert_upgrade_backups_readable(executor):
    """Assert the installer left a readable backup of everything it converts in place.

    These archives are the only way back from an upgrade: one holds the previous /opt/slurm build, the other the
    pre-conversion StateSaveLocation. Cross-major the accounting database conversion cannot be undone at all, so
    an unusable backup turns a recoverable upgrade into an unrecoverable one.
    """
    patterns = [_INSTALL_BACKUP_GLOB]
    # The installer only backs up StateSaveLocation when slurm.conf declares one, which excludes an external
    # slurmdbd node.
    state_save_location = executor.run_remote_command(
        f"grep -iE '^[[:space:]]*StateSaveLocation[[:space:]]*=' {_SLURM_CONF}", raise_on_error=False, hide=True
    )
    if state_save_location.return_code == 0:
        patterns.append(_STATE_BACKUP_GLOB)

    for pattern in patterns:
        _assert_backup_archive_readable(executor, pattern)


def _log_file_size(executor, path):
    """Return the current size of a root-owned log file, so a later check can be scoped to what is appended after."""
    result = executor.run_remote_command(f"sudo wc -c {path}", raise_on_error=False, hide=True)
    return int(result.stdout.split()[0]) if result.return_code == 0 else 0


def _report_slurmctld_log_of_install(executor, offset):
    """Report what slurmctld logged while the installer replaced Slurm under it.

    Error-level lines are expected here rather than exceptional: every daemon of the new Slurm re-validates the
    slurm.conf written by the ParallelCluster release under test, which is what known_harmless_slurm_daemon_errors
    exists for. They are logged instead of asserted on because a "defunct" or "obsolete" parameter reported here
    is a finding about the release, not a failure of the upgrade. A fatal line is different: it means the daemon
    gave up, which no ParallelCluster release may cause.
    """
    log = executor.run_remote_command(
        f"sudo tail -c +{offset + 1} {_SLURMCTLD_LOG}", raise_on_error=False, hide=True
    ).stdout
    error_lines = [line for line in log.splitlines() if re.search(r"error|fatal|defunct|obsolete", line, re.I)]
    if error_lines:
        logging.warning(
            "slurmctld reported %d error-level lines while Slurm was being replaced:\n%s",
            len(error_lines),
            "\n".join(error_lines),
        )
    assert_that([line for line in error_lines if "fatal" in line.lower()]).described_as(
        "fatal slurmctld messages logged during the install"
    ).is_empty()


def install_test_software(executor, stage_source_archive=False, assert_controller=True):
    """Download the installer from us-east-1, run it on the target host and verify what it left behind.

    Set stage_source_archive when the target host cannot read the artifact bucket itself, see _stage_source_archive.
    Clear assert_controller for a host that runs no slurmctld, such as an external slurmdbd instance.
    """
    script_path = _download_software_installer_script()
    if stage_source_archive:
        _stage_source_archive(executor, script_path)
    target_version = _installer_constant(Path(script_path).read_text(encoding="utf-8"), "TARGET_RUNTIME_VERSION")
    version_before = get_slurm_version(executor)
    slurmctld_log_size = _log_file_size(executor, _SLURMCTLD_LOG)
    result = executor.run_remote_script(script_path, run_as_root=True, timeout=3600, pty=False)

    version_after = get_slurm_version(executor)
    logging.info(
        "Slurm version after the install: %s (was %s before), a %s",
        version_after,
        version_before,
        (
            "same-major reinstall"
            if slurm_major_version(version_before) == slurm_major_version(version_after)
            else "cross-major upgrade"
        ),
    )
    # The installer checks the version it installed itself, but only against its own constant. Checking it here as
    # well is what fails a run whose installer targets a different version than the run is meant to exercise, for
    # example because the mutable bucket object was replaced between two runs of the same suite.
    assert_that(version_after).described_as("Slurm version reported by the host after the install").contains(
        target_version
    )
    if version_after != version_before:
        # Only when the installer actually replaced something: it skips the backups, and everything else, when
        # the target version is already installed.
        assert_upgrade_backups_readable(executor)
    if assert_controller:
        assert_slurm_controller_healthy(executor)
        _report_slurmctld_log_of_install(executor, slurmctld_log_size)
    return result


def install_test_software_with_stopped_consumers(executor, *clusters):
    """Run the test software installer while the clusters have no compute or login consumers."""
    with stopped_shared_slurm_consumers(*clusters):
        return install_test_software(executor)


def assert_slurm_controller_healthy(executor):
    """Retry until scontrol reports a successful response from a healthy Slurm controller."""

    @retry(wait_fixed=seconds(10), stop_max_attempt_number=6)
    def _assert_controller_healthy():
        result = executor.run_remote_command("scontrol ping", raise_on_error=False)
        assert_that(result.return_code).described_as(
            f"scontrol ping failed with stderr: {result.stderr}"
        ).is_equal_to(0)
        assert_that(result.stdout).described_as("scontrol ping did not report a healthy controller").contains("is UP")
        return result

    return _assert_controller_healthy()


def wait_for_partitions_up(scheduler_commands, partitions=None):
    """Wait until the requested partitions (all of them by default) leave the INACTIVE state.

    The compute fleet API reports RUNNING as soon as the status is persisted, but clustermgtd needs
    another iteration to bring the Slurm partitions back UP. Submitting before that happens fails
    with "Requested partition configuration not available now".
    """

    @retry(
        wait_fixed=seconds(15),
        stop_max_delay=minutes(_PARTITION_WAIT_MINUTES),
        retry_on_result=lambda unavailable_partitions: bool(unavailable_partitions),
    )
    def _poll():
        target_partitions = partitions if partitions is not None else scheduler_commands.get_partitions()
        partition_states = {
            partition: scheduler_commands.get_partition_state(partition).strip() for partition in target_partitions
        }
        unavailable_partitions = {
            partition: state for partition, state in partition_states.items() if state.upper() != "UP"
        }
        if unavailable_partitions:
            logging.info("Waiting for Slurm partitions to be UP: %s", unavailable_partitions)
        return unavailable_partitions

    _poll()


def _scontrol_field(text, field):
    """Return the value of a `Key=Value` field of a scontrol one-line output, or None when absent."""
    match = re.search(rf"\b{field}=(\S+)", text)
    return match.group(1) if match else None


def _read_batch_script(remote_command_executor, job_id):
    """Return the batch script slurmctld stored for a job, read back from StateSaveLocation."""
    script_path = f"/tmp/{_STATE_CHECK_RESERVATION}-{job_id}.sh"
    remote_command_executor.run_remote_command(
        f"rm -f {script_path} && scontrol write batch_script {job_id} {script_path}"
    )
    return remote_command_executor.run_remote_command(f"cat {script_path}", hide=True).stdout


def snapshot_slurm_state(remote_command_executor, scheduler_commands):
    """Create Slurm state that a subsequent upgrade must preserve, and return a snapshot describing it.

    The accounting database is not the only thing a Slurm upgrade converts: slurmctld rewrites the contents
    of StateSaveLocation, which holds the queued jobs, their batch scripts and the reservations. Submitting a
    job after the upgrade only proves the controller is up; the state captured here is what proves the upgrade
    converted the state it inherited instead of discarding it.

    Note this deliberately covers pending state only. Running jobs cannot be covered by this harness because
    the installer scales the compute fleet to zero, so verifying that running jobs survive an upgrade needs a
    dedicated test that keeps the fleet up and follows the documented daemon upgrade order.
    """
    held_job_id = scheduler_commands.submit_command_and_assert_job_accepted(
        {"command": "hostname", "nodes": 1, "slots": 1, "other_options": "--hold"}
    )
    job_details = remote_command_executor.run_remote_command(f"scontrol --oneliner show job {held_job_id}").stdout
    snapshot = {
        "held_job_id": held_job_id,
        "held_job_submit_time": _scontrol_field(job_details, "SubmitTime"),
        "held_job_batch_script": _read_batch_script(remote_command_executor, held_job_id),
        "reservation": None,
    }

    nodes = scheduler_commands.get_compute_nodes(all_nodes=True)
    if nodes:
        remote_command_executor.run_remote_command(
            f"sudo -i scontrol delete ReservationName={_STATE_CHECK_RESERVATION}", raise_on_error=False
        )
        # Not best effort: a reservation covers a StateSaveLocation record type the held job does not, and a
        # controller that cannot create one is already a finding, whether the upgrade is to blame or not.
        created = remote_command_executor.run_remote_command(
            f"sudo -i scontrol create reservation ReservationName={_STATE_CHECK_RESERVATION} "
            f"user=$(id -un) starttime={_STATE_CHECK_RESERVATION_START} duration=1:00:00 "
            f"flags=maint,ignore_jobs nodes={nodes[0]}",
            raise_on_error=False,
        )
        assert_that(created.return_code).described_as(
            f"creation of reservation {_STATE_CHECK_RESERVATION} on node {nodes[0]}: {_failed_command_output(created)}"
        ).is_equal_to(0)
        reservation_details = remote_command_executor.run_remote_command(
            f"scontrol --oneliner show ReservationName={_STATE_CHECK_RESERVATION}"
        ).stdout
        snapshot["reservation"] = {
            "nodes": _scontrol_field(reservation_details, "Nodes"),
            "start_time": _scontrol_field(reservation_details, "StartTime"),
        }
    else:
        logging.info("No compute node is configured, skipping the reservation part of the Slurm state snapshot")

    logging.info("Captured Slurm state to verify across the upgrade: %s", snapshot)
    return snapshot


def _failed_command_output(result):
    """Return the output that explains a failed remote command.

    Remote commands run over a pty by default, which merges stderr into stdout, so an assertion message built out
    of stderr alone is empty for most of the commands here.
    """
    return result.stderr.strip() or result.stdout.strip()


def _reservation_diagnostics(remote_command_executor):
    """Describe what the controller knows about reservations, for when the state check finds one missing.

    A reservation can disappear for two very different reasons: the upgrade failed to convert the record, or
    something in the cluster deleted the reservation deliberately. Which reservations survived and what slurmctld
    logged about them is what separates the two, and neither is recoverable from the test's own commands.

    clustermgtd is included because a deletion it performs leaves no trace in slurmctld.log: a successful delete
    is only logged by slurmctld under DebugFlags=Reservation.
    """
    reservations = remote_command_executor.run_remote_command(
        "scontrol show reservation", raise_on_error=False, hide=True
    )
    controller_log = remote_command_executor.run_remote_command(
        f"sudo grep -i -e reservation -e 'recovered state' {_SLURMCTLD_LOG} | tail -n 40",
        raise_on_error=False,
        hide=True,
    )
    node_daemon_log = remote_command_executor.run_remote_command(
        f"sudo grep -i reservation {_CLUSTERMGTD_LOG} | tail -n 20",
        raise_on_error=False,
        hide=True,
    )
    return (
        f"reservations known to the controller: {reservations.stdout.strip() or '<none>'}. "
        f"slurmctld log: {controller_log.stdout.strip() or '<nothing about reservations>'}. "
        f"clustermgtd log: {node_daemon_log.stdout.strip() or '<nothing about reservations>'}"
    )


def assert_slurm_state_preserved(remote_command_executor, snapshot):
    """Verify the state captured by snapshot_slurm_state survived the upgrade, then clean it up.

    Every command here runs through the given executor, which must target the head node: the installer scales
    the login pools to zero and back, so any executor bound to a login node before the upgrade now points at a
    terminated instance.
    """
    held_job_id = snapshot["held_job_id"]
    logging.info("Verifying the Slurm state captured before the upgrade is intact")

    job_details = remote_command_executor.run_remote_command(
        f"scontrol --oneliner show job {held_job_id}", raise_on_error=False
    )
    assert_that(job_details.return_code).described_as(
        f"job {held_job_id} is unknown to slurmctld after the upgrade: {_failed_command_output(job_details)}"
    ).is_equal_to(0)
    assert_that(_scontrol_field(job_details.stdout, "JobState")).described_as(
        f"state of job {held_job_id}"
    ).is_equal_to("PENDING")
    # A converted record keeps its original submission time; a recreated or defaulted one does not.
    assert_that(_scontrol_field(job_details.stdout, "SubmitTime")).described_as(
        f"submit time of job {held_job_id}"
    ).is_equal_to(snapshot["held_job_submit_time"])
    assert_that(_read_batch_script(remote_command_executor, held_job_id)).described_as(
        f"batch script of job {held_job_id}"
    ).is_equal_to(snapshot["held_job_batch_script"])

    expected_reservation = snapshot["reservation"]
    if expected_reservation:
        reservation_details = remote_command_executor.run_remote_command(
            f"scontrol --oneliner show ReservationName={_STATE_CHECK_RESERVATION}", raise_on_error=False
        )
        diagnostics = (
            "" if reservation_details.return_code == 0 else f" {_reservation_diagnostics(remote_command_executor)}"
        )
        assert_that(reservation_details.return_code).described_as(
            f"reservation {_STATE_CHECK_RESERVATION} is gone after the upgrade: "
            f"{_failed_command_output(reservation_details)}.{diagnostics}"
        ).is_equal_to(0)
        for field, expected_value in (("Nodes", "nodes"), ("StartTime", "start_time")):
            assert_that(_scontrol_field(reservation_details.stdout, field)).described_as(
                f"{field} of reservation {_STATE_CHECK_RESERVATION}"
            ).is_equal_to(expected_reservation[expected_value])
        remote_command_executor.run_remote_command(
            f"sudo -i scontrol delete ReservationName={_STATE_CHECK_RESERVATION}", raise_on_error=False
        )

    remote_command_executor.run_remote_command(f"scancel {held_job_id}", raise_on_error=False)


def run_scheduler_smoke_test(
    scheduler_commands,
    partition=None,
    nodes=1,
    slots=1,
    other_options=None,
    command="hostname",
    timeout=None,
):
    """Submit a short scheduler job, wait for completion, assert success, and return its job ID."""
    wait_for_partitions_up(scheduler_commands, [partition] if partition is not None else None)
    submit_command_args = {
        "command": command,
        "nodes": nodes,
        "slots": slots,
    }
    if partition is not None:
        submit_command_args["partition"] = partition
    if other_options is not None:
        submit_command_args["other_options"] = other_options

    job_id = scheduler_commands.submit_command_and_assert_job_accepted(submit_command_args)
    scheduler_commands.wait_job_completed(job_id, timeout=timeout)
    scheduler_commands.assert_job_succeeded(job_id)
    return job_id
