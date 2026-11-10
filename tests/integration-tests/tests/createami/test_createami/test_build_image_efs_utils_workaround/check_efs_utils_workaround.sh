#!/bin/bash
# Verify a node launched from an image built with the P558591845 workarounds.
# Usage: check_efs_utils_workaround.sh <efs workaround> <efs-utils version> <NVIDIA driver version> <EFA installer version>
#   efs workaround: efs_utils_rpm, go_fips_off (RHEL/Rocky) or none
#   versions: the expected version, "any" to only check presence, or "none" (NVIDIA only) when not installed
set -euxo pipefail

workaround="$1"
expected_efs_utils_version="$2"
expected_nvidia_driver_version="$3"
expected_efa_installer_version="$4"

if command -v rpm > /dev/null && rpm -q amazon-efs-utils > /dev/null; then
  efs_utils_version=$(rpm -q --qf '%{VERSION}' amazon-efs-utils)
else
  efs_utils_version=$(dpkg-query -W -f='${Version}' amazon-efs-utils | cut -d- -f1)
fi
[[ -n "${efs_utils_version}" ]]
[[ "${expected_efs_utils_version}" == "any" || "${efs_utils_version}" == "${expected_efs_utils_version}" ]]

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
  none)
    ;;
  *)
    echo "Unknown workaround ${workaround}" >&2
    exit 1
    ;;
esac

if [[ "${expected_nvidia_driver_version}" != "none" ]]; then
  nvidia_driver_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
  [[ "${expected_nvidia_driver_version}" == "any" || "${nvidia_driver_version}" == "${expected_nvidia_driver_version}" ]]
fi

grep "EFA installer version" /opt/amazon/efa_installed_packages
if [[ "${expected_efa_installer_version}" != "any" ]]; then
  grep -q "EFA installer version: ${expected_efa_installer_version}" /opt/amazon/efa_installed_packages
fi
modinfo efa > /dev/null

echo "efs-utils workaround checks passed on $(hostname)"
