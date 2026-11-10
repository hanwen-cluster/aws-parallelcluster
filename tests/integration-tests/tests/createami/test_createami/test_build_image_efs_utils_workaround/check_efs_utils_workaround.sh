#!/bin/bash
# Verify a node launched from an image built with one of the P558591845 workarounds.
# Usage: check_efs_utils_workaround.sh <efs_utils_rpm|go_fips_off> <expected amazon-efs-utils version>
set -euxo pipefail

workaround="$1"
expected_version="$2"

installed_version=$(rpm -q --qf '%{VERSION}' amazon-efs-utils)
[[ "${installed_version}" == "${expected_version}" ]]

# ParallelCluster raises the watchdog poll interval when it installs efs-utils.
# With the efs_utils_rpm workaround the cookbook skips that step, so the parent image must have done it.
grep -qx 'poll_interval_sec = 10' /etc/amazon/efs/efs-utils.conf

# A TLS mount is configured on every node, so the watchdog must be running.
systemctl is-active --quiet amazon-efs-mount-watchdog

case "${workaround}" in
  efs_utils_rpm)
    # The efs-utils repo must not be configured, otherwise UpdateOsPackages would upgrade efs-utils to 3.x.
    if compgen -G "/etc/yum.repos.d/*efs*" > /dev/null; then
      echo "Unexpected efs-utils yum repository found" >&2
      exit 1
    fi
    ;;
  go_fips_off)
    grep -qx 'GOFIPS140=off' /usr/lib/golang/go.env
    grep -q '^golang-' /etc/dnf/plugins/versionlock.list
    [[ "$(env -i PATH=/usr/bin:/bin go env GOFIPS140)" == "off" ]]
    ;;
  *)
    echo "Unknown workaround ${workaround}" >&2
    exit 1
    ;;
esac

echo "efs-utils workaround checks passed on $(hostname)"
