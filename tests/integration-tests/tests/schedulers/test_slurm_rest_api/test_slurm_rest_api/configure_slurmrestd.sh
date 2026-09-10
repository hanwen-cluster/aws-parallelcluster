#!/bin/bash
#
# OnNodeConfigured custom action for the test_slurm_rest_api integration test.
#
# Based on the upstream Slurm REST API postinstall script:
#   https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-post-install-scripts/main/rest-api/postinstall.sh
#
# Differences from upstream:
#   - Uses a local slurm_rest_api.rb (uploaded to S3 by the test) instead of downloading
#     it from GitHub. The local copy includes an `apt-get update` before installing nginx
#     on Debian/Ubuntu to avoid stale package index 404 errors.
#   - Makes libhttp_parser discoverable by the dynamic linker before configuring slurmrestd,
#     see fix_http_parser_linker_cache below.
#   - Dumps the slurmrestd unit status and journal on failure, because the recipe only reports
#     "Timeout waiting for slurmrestd startup" and never why the daemon died.
#
# Arguments:
#   $1 - S3 URI of the adapted slurm_rest_api.rb (e.g. s3://bucket/scripts/slurm_rest_api.rb)

set -ex

SLURM_REST_API_RB_S3_URI="${1:?Usage: configure_slurmrestd.sh <s3-uri-of-slurm_rest_api.rb>}"

SLURMRESTD_BIN=/opt/slurm/sbin/slurmrestd

dump_slurmrestd_diagnostics() {
    set +e
    echo "=== slurmrestd failed to come up, collecting diagnostics ==="
    ldd "${SLURMRESTD_BIN}" 2>&1 | tail -n 20
    systemctl status slurmrestd --no-pager -l 2>&1 | tail -n 30
    journalctl -u slurmrestd --no-pager -n 100 2>&1
}

# ParallelCluster <= 3.15 builds http-parser from source on Amazon Linux 2023 (the package was dropped
# from the AL2023 repos) and installs it under /usr/local/lib without refreshing the linker cache.
# In Slurm <= 24.11 the slurmrestd binary links libhttp_parser directly, so on those AMIs it dies at
# exec time with "error while loading shared libraries: libhttp_parser.so.2.9.4" and never creates its
# socket. The cookbook fixed this for 3.16 by installing to /usr/lib64 and running ldconfig
# (aws-parallelcluster-cookbook 7068f908), but already-released AMIs need the cache repaired here.
fix_http_parser_linker_cache() {
    [ -x "${SLURMRESTD_BIN}" ] || return 0
    ldd "${SLURMRESTD_BIN}" | grep -q "not found" || return 0

    local lib_dir
    lib_dir=$(dirname "$(ls /usr/local/lib*/libhttp_parser.so* /usr/local/lib/*/libhttp_parser.so* 2>/dev/null | head -n 1)")
    if [ -z "${lib_dir}" ] || [ ! -d "${lib_dir}" ]; then
        echo "ERROR: slurmrestd has unresolved shared libraries and no libhttp_parser was found under /usr/local"
        ldd "${SLURMRESTD_BIN}"
        return 1
    fi

    echo "${lib_dir}" > /etc/ld.so.conf.d/http_parser.conf
    ldconfig

    if ldd "${SLURMRESTD_BIN}" | grep -q "not found"; then
        echo "ERROR: slurmrestd still has unresolved shared libraries after adding ${lib_dir} to the linker cache"
        ldd "${SLURMRESTD_BIN}"
        return 1
    fi
}

fix_http_parser_linker_cache

# Copy Slurm REST API configuration files and scripts
tmp_dir=/tmp/slurm_rest_api
mkdir -p $tmp_dir

source_path=https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-post-install-scripts/main/rest-api
files=(slurmrestd.service nginx.conf)
for file in "${files[@]}"
do
    wget -qO- $source_path/$file > $tmp_dir/$file
done

rotate_jwt_path=/opt/parallelcluster/scripts/rotate_jwt.sh
wget -qO- $source_path/rotate_jwt.sh > $rotate_jwt_path
chmod +x $rotate_jwt_path

# Download the adapted slurm_rest_api.rb from S3
aws s3 cp "${SLURM_REST_API_RB_S3_URI}" $tmp_dir/slurm_rest_api.rb

# Setup Slurm REST API
trap dump_slurmrestd_diagnostics ERR
sudo cinc-client \
  --local-mode \
  --config /etc/chef/client.rb \
  --log_level auto \
  --force-formatter \
  --chef-zero-port 8889 \
  -j /etc/chef/dna.json \
  -z $tmp_dir/slurm_rest_api.rb
