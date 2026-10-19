#!/usr/bin/env bash
# Integration test for the ceph stack: bootstrap a single-node cluster and
# verify HEALTH_OK with every OSD up, plus a live dashboard API endpoint.
set -euo pipefail

STACK=ceph
CONTAINER=ceph_node

cd "$(dirname "$0")/../.."

# Reads the global [containers.ceph_node] config; per-stack overrides
# ([stacks.ceph.containers]) are not applied, so don't set them for this test.
node_config() { uv run ceph-devstack config get "containers.ceph_node.$1"; }
DASHBOARD_PORT="$(node_config dashboard_port)"
DASHBOARD_USER="$(node_config dashboard_user)"
DASHBOARD_PASSWORD="$(node_config dashboard_password)"
# CephNode creates one OSD per loop device.
EXPECTED_OSDS="$(node_config loop_device_count)"

cleanup() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "=== ${CONTAINER} logs (last 200 lines) ===" >&2
        podman logs "$CONTAINER" 2>&1 | tail -200 >&2 || true
    fi
    uv run ceph-devstack --stack "$STACK" -v stop || true
    uv run ceph-devstack --stack "$STACK" -v remove || true
    exit "$status"
}
trap cleanup EXIT

uv run ceph-devstack --stack "$STACK" -v doctor --fix
uv run ceph-devstack --stack "$STACK" -v pull
uv run ceph-devstack --stack "$STACK" -v create
uv run ceph-devstack --stack "$STACK" -v start

# HEALTH_OK alone is not enough: a cluster with no OSDs is healthy too.
health=""
up=""
for _ in $(seq 1 90); do
    health="$(
        podman exec "$CONTAINER" ceph health 2>/dev/null \
            | awk '{print $1}' || true
    )"
    up="$(
        podman exec "$CONTAINER" ceph osd stat -f json 2>/dev/null \
            | grep -o '"num_up_osds":[0-9]*' | cut -d: -f2 || true
    )"
    if [[ "$health" == "HEALTH_OK" && "${up:-0}" -ge "$EXPECTED_OSDS" ]]; then
        echo "Cluster reached HEALTH_OK with ${up} OSD(s) up"
        break
    fi
    sleep 2
done

if [[ "$health" != "HEALTH_OK" || "${up:-0}" -lt "$EXPECTED_OSDS" ]]; then
    echo "Expected HEALTH_OK with ${EXPECTED_OSDS} OSD(s) up," \
        "got: ${health:-<empty>} with ${up:-0} up" >&2
    exit 1
fi

dashboard_ok=false
for _ in $(seq 1 30); do
    if curl -sf -X POST "http://127.0.0.1:${DASHBOARD_PORT}/api/auth" \
        -H 'Accept: application/vnd.ceph.api.v1.0+json' \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${DASHBOARD_USER}\",\"password\":\"${DASHBOARD_PASSWORD}\"}" \
        >/dev/null; then
        dashboard_ok=true
        break
    fi
    sleep 2
done

if [[ "$dashboard_ok" != "true" ]]; then
    echo "Dashboard API did not become ready on port ${DASHBOARD_PORT}" >&2
    exit 1
fi

echo "Dashboard API is live on port ${DASHBOARD_PORT}"
echo "Ceph stack integration test passed"
